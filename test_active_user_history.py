"""Read-only SQL contract tests; no database service or credentials required."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

import db_store


def chat(identifier, *, project="project-a", owner="alice", deleted=False):
    return {"id": identifier, "project_id": project, "owner_subject": owner,
            "deleted_at": "deleted" if deleted else None}


def message(identifier, chat_id="active", *, role="user", at=0, status="complete", deleted=False):
    return {"id": identifier, "chat_id": chat_id, "role": role, "content": identifier,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=at),
            "status": status, "deleted_at": "deleted" if deleted else None}


class FakeCursor:
    """Evaluate the bounded SELECTs and assert their ownership/filter contract."""

    def __init__(self, chats, messages, subject):
        self.chats = {row["id"]: row for row in chats}
        self.messages = messages
        self.subject = subject
        self.calls = []
        self.rows = []

    def execute(self, sql, params):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        assert sql.startswith("SELECT ")
        assert "OFFSET" not in sql
        assert "c.deleted_at IS NULL" in sql
        assert ("c.owner_subject = %s" in sql) == bool(self.subject)
        if self.subject:
            assert params[1] == self.subject
        if "SELECT c.project_id, c.owner_subject" in sql:
            row = self.chats.get(params[0])
            allowed = row and not row["deleted_at"] and (
                not self.subject or row["owner_subject"] == self.subject)
            self.rows = [{"project_id": row["project_id"], "owner_subject": row["owner_subject"],
                          "project_name": "Project", "memory": {}, "knowledge_scope": "project_and_global"}] if allowed else []
            return
        assert "m.deleted_at IS NULL" in sql
        assert "m.status = 'complete'" in sql
        active = "SELECT m.id AS id" in sql
        if active:
            assert "WHERE c.id = %s" in sql
            assert "m.role = 'user'" in sql
            assert "ORDER BY m.created_at DESC, m.id DESC LIMIT %s" in sql
        project_feed = "WHERE c.project_id = %s" in sql
        rows = []
        for row in self.messages:
            owner_chat = self.chats[row["chat_id"]]
            scope_matches = owner_chat["project_id"] == params[0] if project_feed else row["chat_id"] == params[0]
            if (not scope_matches or owner_chat["deleted_at"] or row["deleted_at"]
                    or row["status"] != "complete"
                    or self.subject and owner_chat["owner_subject"] != self.subject
                    or active and row["role"] != "user"):
                continue
            if active:
                rows.append({key: row[key] for key in ("id", "chat_id", "role", "content", "created_at")})
            else:
                rows.append({key: row[key] for key in ("chat_id", "role", "content", "created_at")})
        rows.sort(key=lambda row: (row["created_at"], row["id"]) if active else row["created_at"], reverse=True)
        self.rows = rows[:params[-1]]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


def conversation(monkeypatch, messages, *, chats=None, subject="alice", limit=40, chat_id="active"):
    cursor = FakeCursor(chats or [chat("active")], messages, subject)

    @contextmanager
    def read_cursor():
        yield cursor

    monkeypatch.setattr(db_store, "read_cursor", read_cursor)
    with db_store.request_scope(subject):
        result = db_store.get_project_conversation(chat_id, limit)
    return result, cursor


def ids(result):
    return [row["id"] for row in result["active_user_messages"]]


def test_busy_sibling_chat_does_not_starve_active_user_history_or_change_project_feed(monkeypatch):
    rows = [message("anchor", at=1), message("refinement", at=2)]
    rows += [message(f"other-{i}", "sibling", at=100 + i) for i in range(50)]
    result, cursor = conversation(monkeypatch, rows, chats=[chat("active"), chat("sibling")])
    assert len(result["messages"]) == 40
    assert {row["chat_id"] for row in result["messages"]} == {"sibling"}
    assert ids(result) == ["anchor", "refinement"]
    assert result["active_user_history_limit"] == 40
    assert result["active_user_history_truncated"] is False
    assert cursor.calls[-1][1] == ("active", "alice", 41)


def test_free_chat_user_history_excludes_assistant_flood_and_other_chats(monkeypatch):
    rows = [message("anchor", at=1), message("refinement", at=2), message("other", "sibling", at=999)]
    rows += [message(f"assistant-{i}", role="assistant", at=100 + i) for i in range(50)]
    result, _ = conversation(monkeypatch, rows, chats=[chat("active", project=None), chat("sibling", project=None)])
    assert len(result["messages"]) == 40
    assert {row["role"] for row in result["messages"]} == {"assistant"}
    assert ids(result) == ["anchor", "refinement"]
    assert {row["chat_id"] for row in result["active_user_messages"]} == {"active"}


def test_limit_plus_one_detects_truncation_and_returns_newest_users_chronologically(monkeypatch):
    result, cursor = conversation(monkeypatch, [message(f"u{i}", at=i) for i in range(5)], limit=3)
    assert ids(result) == ["u2", "u3", "u4"]
    assert result["active_user_history_truncated"] is True
    assert cursor.calls[-1][1] == ("active", "alice", 4)


@pytest.mark.parametrize("count", [0, 2, 3])
def test_empty_and_exact_limit_histories_are_not_marked_truncated(monkeypatch, count):
    result, _ = conversation(monkeypatch, [message(f"u{i}", at=i) for i in range(count)], limit=3)
    assert ids(result) == [f"u{i}" for i in range(count)]
    assert result["active_user_history_truncated"] is False


def test_equal_timestamp_order_is_stable_by_message_id(monkeypatch):
    result, _ = conversation(monkeypatch, [message("u-c"), message("u-a"), message("u-b")], limit=2)
    assert ids(result) == ["u-b", "u-c"]
    assert result["active_user_history_truncated"] is True
    assert set(result["active_user_messages"][0]) == {"id", "chat_id", "content", "role", "created_at"}


@pytest.mark.parametrize("project", [None, "project-a"])
def test_running_error_deleted_and_nonuser_messages_are_excluded(monkeypatch, project):
    rows = [message("visible"), message("running", status="running"), message("error", status="error"),
            message("deleted", deleted=True), message("assistant", role="assistant"), message("system", role="system")]
    result, _ = conversation(monkeypatch, rows, chats=[chat("active", project=project)])
    assert ids(result) == ["visible"]


@pytest.mark.parametrize("project", [None, "project-a"])
@pytest.mark.parametrize("deleted,owner", [(True, "alice"), (False, "bob")])
def test_deleted_or_unauthorized_active_chat_returns_nothing_before_history_queries(monkeypatch, project, deleted, owner):
    result, cursor = conversation(monkeypatch, [message("private")], chats=[chat("active", project=project, owner=owner, deleted=deleted)])
    assert result is None
    assert len(cursor.calls) == 1


def test_project_feed_and_active_history_preserve_owner_filter(monkeypatch):
    rows = [message("mine"), message("foreign", "foreign", at=2), message("removed", "removed", at=3)]
    result, _ = conversation(monkeypatch, rows, chats=[chat("active"), chat("foreign", owner="bob"), chat("removed", deleted=True)])
    assert ids(result) == ["mine"]
    assert [row["content"] for row in result["messages"]] == ["mine"]


@pytest.mark.parametrize("project", [None, "project-a"])
def test_subject_none_retains_existing_policy_but_still_limits_history_to_active_chat(monkeypatch, project):
    result, cursor = conversation(monkeypatch, [message("active-user"), message("sibling-user", "sibling")],
                                  chats=[chat("active", project=project, owner="bob"), chat("sibling", project=project)], subject=None)
    assert ids(result) == ["active-user"]
    assert cursor.calls[-1][1] == ("active", 41)
    assert all("c.owner_subject = %s" not in sql for sql, _ in cursor.calls)


@pytest.mark.parametrize("requested,expected", [(-3, 1), (0, 1), (1, 1), (40, 40), (100, 100), (101, 100)])
def test_history_limit_uses_existing_bounds(monkeypatch, requested, expected):
    result, cursor = conversation(monkeypatch, [], limit=requested)
    assert result["active_user_history_limit"] == expected
    assert cursor.calls[-1][1][-1] == expected + 1
