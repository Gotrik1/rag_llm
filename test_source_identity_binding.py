"""Offline regressions for binding citations to immutable upload identities."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

import db_store
import web_ui


UPLOAD_A = "a" * 32 + "_manual.md"
UPLOAD_B = "b" * 32 + "_manual.md"


def record(identifier, stored_name, *, owner="user-a", title="manual.md"):
    return {"id": identifier, "source_path": "/uploads/" + stored_name,
            "file_name": title, "owner_subject": owner, "status": "ready", "deleted_at": None}


def registry(monkeypatch, rows):
    cursor = Mock()
    cursor.fetchall.return_value = rows

    @contextmanager
    def read_cursor():
        yield cursor

    monkeypatch.setattr(db_store, "read_cursor", read_cursor)
    return cursor


@pytest.mark.parametrize("reverse", [False, True])
def test_uuid_source_binding_cannot_be_overwritten_by_another_upload_with_same_name(monkeypatch, reverse):
    rows = [record("doc-a", UPLOAD_A), record("doc-b", UPLOAD_B)]
    cursor = registry(monkeypatch, rows[::-1] if reverse else rows)
    with db_store.request_scope("user-a"):
        resolved = db_store.document_sources([UPLOAD_A, UPLOAD_B])
    assert resolved[UPLOAD_A]["id"] == "doc-a"
    assert resolved[UPLOAD_B]["id"] == "doc-b"
    query, parameters = cursor.execute.call_args.args
    assert "owner_subject = %s OR owner_subject IS NULL" in query
    assert "deleted_at IS NULL AND status = 'ready'" in query
    assert parameters == ("user-a",)


@pytest.mark.parametrize("candidate", [UPLOAD_A, UPLOAD_A.upper(), "/uploads/" + UPLOAD_A])
def test_exact_stored_identity_supports_case_and_path_without_discarding_uuid(monkeypatch, candidate):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("doc-b", UPLOAD_B)])
    with db_store.request_scope("user-a"):
        assert db_store.document_sources([candidate])[candidate]["id"] == "doc-a"


def test_unknown_uuid_does_not_link_to_an_existing_same_named_upload(monkeypatch):
    registry(monkeypatch, [record("doc-b", UPLOAD_B)])
    with db_store.request_scope("user-a"):
        assert db_store.document_sources([UPLOAD_A]) == {}


@pytest.mark.parametrize("candidate", ["manual.md", "MANUAL.MD", "/old/path/manual.md"])
def test_ambiguous_human_filename_is_not_a_document_identity(monkeypatch, candidate):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("doc-b", UPLOAD_B, owner=None)])
    with db_store.request_scope("user-a"):
        assert db_store.document_sources([candidate]) == {}


def test_unique_legacy_display_name_and_uuid_free_source_remain_compatible(monkeypatch):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("legacy", "other.md", title="Other.md")])
    with db_store.request_scope("user-a"):
        resolved = db_store.document_sources(["MANUAL.MD", "other.md", "missing.md"])
    assert resolved["MANUAL.MD"]["id"] == "doc-a"
    assert resolved["other.md"]["id"] == "legacy"
    assert "missing.md" not in resolved


def test_nested_upload_prefix_is_preserved_as_one_physical_identity(monkeypatch):
    nested = "c" * 32 + "_" + UPLOAD_A
    registry(monkeypatch, [record("nested", nested), record("doc-a", UPLOAD_A)])
    with db_store.request_scope("user-a"):
        resolved = db_store.document_sources([nested, UPLOAD_A, "manual.md"])
    assert resolved[nested]["id"] == "nested"
    assert resolved[UPLOAD_A]["id"] == "doc-a"
    assert "manual.md" not in resolved


def test_multiple_rows_for_one_stored_identity_fail_closed_instead_of_selecting_last(monkeypatch):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("duplicate", UPLOAD_A)])
    with db_store.request_scope("user-a"):
        assert db_store.document_sources([UPLOAD_A]) == {}


def test_clickable_citation_uses_the_retrieved_upload_not_the_last_same_named_record(monkeypatch):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("doc-b", UPLOAD_B)])
    with db_store.request_scope("user-a"):
        sources = web_ui.enrich_source_links([
            {"file": UPLOAD_A, "section": "Calibration", "score": 1.0},
        ])
    assert sources[0]["url"] == "/api/documents/doc-a/content"
    assert sources[0]["file"] == "manual.md"
    answer = web_ui.enrich_answer_source_citations("Result. [Источник: manual.md — Calibration]", sources)
    assert "](/api/documents/doc-a/content)" in answer
    assert "doc-b" not in answer


def test_ambiguous_display_citation_remains_unlinked_instead_of_linking_wrong_document(monkeypatch):
    registry(monkeypatch, [record("doc-a", UPLOAD_A), record("doc-b", UPLOAD_B)])
    with db_store.request_scope("user-a"):
        sources = web_ui.enrich_source_links([{"file": "manual.md", "section": "Calibration", "score": 1.0}])
    assert sources[0]["file"] == "manual.md"
    assert "url" not in sources[0]
