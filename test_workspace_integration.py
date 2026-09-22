"""HTTP and PostgreSQL integration checks for workspace persistence contracts."""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import db_store
from persistence import Base, Persistence
from security import Principal
from web_ui import Handler


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL is required for PostgreSQL integration tests")
class WorkspacePersistenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_store.run_migrations()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        db_store.close_thread_connection()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_project_chat_message_contract_and_persistence(self) -> None:
        suffix = uuid.uuid4().hex
        project_id = chat_id = message_id = None
        initial_workspace: dict | None = None
        try:
            status, initial_workspace = self.request_json("GET", "/api/workspace")
            self.assertEqual(status, 200)

            status, project = self.request_json("POST", "/api/projects", {
                "name": f"integration-project-{suffix}",
                "memory": {"text": "Integration memory"},
            })
            self.assertEqual(status, 201)
            self.assertEqual(project["memory"], {"text": "Integration memory"})
            project_id = project["id"]

            status, project = self.request_json("PATCH", f"/api/projects/{project_id}", {
                "name": f"renamed-project-{suffix}",
            })
            self.assertEqual(status, 200)
            self.assertEqual(project["name"], f"renamed-project-{suffix}")

            status, chat = self.request_json("POST", f"/api/projects/{project_id}/chats", {
                "title": f"integration-chat-{suffix}",
                "mode": "python",
            })
            self.assertEqual(status, 201)
            self.assertEqual(chat["project_id"], project_id)
            chat_id = chat["id"]

            status, chat = self.request_json("PATCH", f"/api/chats/{chat_id}", {
                "title": f"renamed-chat-{suffix}",
            })
            self.assertEqual(status, 200)
            self.assertEqual(chat["title"], f"renamed-chat-{suffix}")

            status, workspace = self.request_json("POST", "/api/settings", {
                "active_project_id": project_id,
                "active_chat_id": chat_id,
                "settings": {**initial_workspace["settings"], "last_integration_test": suffix},
            })
            self.assertEqual(status, 200)
            self.assertEqual(workspace["active_project_id"], project_id)
            self.assertEqual(workspace["active_chat_id"], chat_id)

            status, current_workspace = self.request_json("GET", "/api/workspace")
            self.assertEqual(status, 200)
            self.assertEqual(current_workspace["active_project_id"], project_id)
            self.assertEqual(current_workspace["active_chat_id"], chat_id)

            status, message = self.request_json("POST", f"/api/chats/{chat_id}/messages", {
                "role": "user",
                "content": "integration message",
                "metadata": {"origin": "workspace-integration-test"},
            })
            self.assertEqual(status, 201)
            self.assertEqual(message["chat_id"], chat_id)
            message_id = message["id"]

            status, project_chats = self.request_json("GET", f"/api/projects/{project_id}/chats")
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in project_chats["items"]], [chat_id])

            status, chat_messages = self.request_json("GET", f"/api/chats/{chat_id}/messages")
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in chat_messages["items"]], [message_id])

            with db_store.db().cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.name, p.memory, c.title, m.content, m.metadata
                    FROM projects p
                    JOIN chats c ON c.project_id = p.id
                    JOIN messages m ON m.chat_id = c.id
                    WHERE p.id = %s AND c.id = %s AND m.id = %s
                    """,
                    (project_id, chat_id, message_id),
                )
                stored = cursor.fetchone()
            self.assertIsNotNone(stored)
            self.assertEqual(stored["name"], project["name"])
            self.assertEqual(stored["memory"], {"text": "Integration memory"})
            self.assertEqual(stored["title"], chat["title"])
            self.assertEqual(stored["content"], "integration message")
            self.assertEqual(stored["metadata"], {"origin": "workspace-integration-test"})
        finally:
            if initial_workspace is not None:
                restore_workspace = {"settings": initial_workspace["settings"]}
                if initial_workspace.get("active_project_id"):
                    restore_workspace["active_project_id"] = initial_workspace["active_project_id"]
                if initial_workspace.get("active_chat_id"):
                    restore_workspace["active_chat_id"] = initial_workspace["active_chat_id"]
                self.request_json("POST", "/api/settings", restore_workspace)
            if message_id:
                self.request_json("DELETE", f"/api/messages/{message_id}")
            if chat_id:
                self.request_json("DELETE", f"/api/chats/{chat_id}")
            if project_id:
                self.request_json("DELETE", f"/api/projects/{project_id}")

            if project_id:
                with db_store.db().cursor() as cursor:
                    cursor.execute("SELECT deleted_at FROM projects WHERE id = %s", (project_id,))
                    deleted = cursor.fetchone()
                self.assertIsNotNone(deleted)
                self.assertIsNotNone(deleted["deleted_at"])

    def test_free_chat_can_be_created_and_moved_into_project(self) -> None:
        suffix = uuid.uuid4().hex
        chat_id = project_id = None
        try:
            status, chat = self.request_json("POST", "/api/chats", {"title": f"free-chat-{suffix}"})
            self.assertEqual(status, 201)
            self.assertIsNone(chat["project_id"])
            chat_id = chat["id"]

            status, chats = self.request_json("GET", "/api/chats")
            self.assertEqual(status, 200)
            self.assertIn(chat_id, [item["id"] for item in chats["items"] if item["project_id"] is None])

            status, project = self.request_json("POST", "/api/projects", {"name": f"move-target-{suffix}"})
            self.assertEqual(status, 201)
            project_id = project["id"]

            status, moved = self.request_json("PATCH", f"/api/chats/{chat_id}", {"project_id": project_id})
            self.assertEqual(status, 200)
            self.assertEqual(moved["project_id"], project_id)

            _, project_chats = self.request_json("GET", f"/api/projects/{project_id}/chats")
            self.assertIn(chat_id, [item["id"] for item in project_chats["items"]])
        finally:
            if chat_id:
                self.request_json("DELETE", f"/api/chats/{chat_id}")
            if project_id:
                self.request_json("DELETE", f"/api/projects/{project_id}")

    def test_deleting_project_releases_its_chats(self) -> None:
        suffix = uuid.uuid4().hex
        chat_id = project_id = None
        try:
            _, project = self.request_json("POST", "/api/projects", {"name": f"release-project-{suffix}"})
            project_id = project["id"]
            _, chat = self.request_json("POST", f"/api/projects/{project_id}/chats", {"title": f"release-chat-{suffix}"})
            chat_id = chat["id"]

            status, deleted = self.request_json("DELETE", f"/api/projects/{project_id}")
            self.assertEqual(status, 200)
            self.assertTrue(deleted["deleted"])
            project_id = None

            _, chats = self.request_json("GET", "/api/chats")
            released = next(item for item in chats["items"] if item["id"] == chat_id)
            self.assertIsNone(released["project_id"])
        finally:
            if chat_id:
                self.request_json("DELETE", f"/api/chats/{chat_id}")
            if project_id:
                self.request_json("DELETE", f"/api/projects/{project_id}")

    def test_document_registry_tracks_scope_status_and_source_name(self) -> None:
        suffix = uuid.uuid4().hex
        project_id = chat_id = document_id = None
        try:
            project = db_store.create_project({"name": f"documents-{suffix}"})
            project_id = str(project["id"])
            chat = db_store.create_chat(project_id, {"title": "documents"}, "python", "ollama", "test-model")
            chat_id = str(chat["id"])
            stored_name = f"{suffix}_manual.pdf"
            document = db_store.create_document(project_id, chat_id, {
                "file_name": "manual.pdf",
                "source_path": f"/app/.data/uploads/{stored_name}",
                "mime_type": "application/pdf",
                "status": "queued",
                "processing_state": {"progress": 0},
            })
            document_id = str(document["id"])
            ready = db_store.update_document(document_id, {
                "status": "ready",
                "processing_state": {"progress": 100},
                "metadata": {"chunks": 12},
            })

            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["metadata"]["chunks"], 12)
            self.assertEqual([str(item["id"]) for item in db_store.list_documents(project_id)], [document_id])
            sources = db_store.document_sources([stored_name, "manual.pdf", "MANUAL.PDF", "other.pdf"])
            self.assertEqual(str(sources[stored_name]["id"]), document_id)
            self.assertEqual(str(sources["manual.pdf"]["id"]), document_id)
            self.assertEqual(str(sources["MANUAL.PDF"]["id"]), document_id)
            self.assertNotIn("other.pdf", sources)

            tombstone = db_store.tombstone_document(document_id)
            self.assertEqual(tombstone["status"], "deleting")
            self.assertIsNotNone(tombstone["deleted_at"])
            self.assertEqual(db_store.list_documents(project_id), [])
            self.assertNotIn("manual.pdf", db_store.document_sources(["manual.pdf"]))
            registry = db_store.list_all_documents_for_retrieval()
            self.assertIn(document_id, [str(item["id"]) for item in registry if item.get("deleted_at")])
        finally:
            if document_id:
                with db_store.db().cursor() as cursor:
                    cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))
                db_store.db().commit()
            if chat_id:
                db_store.delete_chat(chat_id)
            if project_id:
                db_store.delete_project(project_id)

    def test_project_conversation_isolated_and_moves_with_chat(self) -> None:
        project_ids: list[str] = []
        chat_ids: list[str] = []
        message_ids: list[str] = []
        suffix = uuid.uuid4().hex
        try:
            _, first_project = self.request_json("POST", "/api/projects", {"name": f"scope-a-{suffix}"})
            _, second_project = self.request_json("POST", "/api/projects", {"name": f"scope-b-{suffix}"})
            project_ids.extend([first_project["id"], second_project["id"]])

            _, first_chat = self.request_json("POST", f"/api/projects/{first_project['id']}/chats", {"title": "chat-a"})
            _, second_chat = self.request_json("POST", f"/api/projects/{second_project['id']}/chats", {"title": "chat-b"})
            chat_ids.extend([first_chat["id"], second_chat["id"]])

            _, first_message = self.request_json("POST", f"/api/chats/{first_chat['id']}/messages", {
                "role": "user", "content": "alpha-only context"
            })
            _, second_message = self.request_json("POST", f"/api/chats/{second_chat['id']}/messages", {
                "role": "user", "content": "beta-only context"
            })
            message_ids.extend([first_message["id"], second_message["id"]])

            first_scope = db_store.get_project_conversation(first_chat["id"])
            self.assertIsNotNone(first_scope)
            self.assertEqual(str(first_scope["project_id"]), first_project["id"])
            first_contents = [message["content"] for message in first_scope["messages"]]
            self.assertIn("alpha-only context", first_contents)
            self.assertNotIn("beta-only context", first_contents)

            status, moved_chat = self.request_json("PATCH", f"/api/chats/{first_chat['id']}", {
                "project_id": second_project["id"]
            })
            self.assertEqual(status, 200)
            self.assertEqual(moved_chat["project_id"], second_project["id"])

            moved_scope = db_store.get_project_conversation(first_chat["id"])
            self.assertIsNotNone(moved_scope)
            self.assertEqual(str(moved_scope["project_id"]), second_project["id"])
            moved_contents = [message["content"] for message in moved_scope["messages"]]
            self.assertIn("alpha-only context", moved_contents)
            self.assertIn("beta-only context", moved_contents)

            _, first_project_chats = self.request_json("GET", f"/api/projects/{first_project['id']}/chats")
            self.assertEqual(first_project_chats["items"], [])
        finally:
            for message_id in message_ids:
                self.request_json("DELETE", f"/api/messages/{message_id}")
            for chat_id in chat_ids:
                self.request_json("DELETE", f"/api/chats/{chat_id}")
            for project_id in project_ids:
                self.request_json("DELETE", f"/api/projects/{project_id}")

    def test_users_share_global_documents_but_not_project_documents(self) -> None:
        suffix = uuid.uuid4().hex
        subject_a = f"local:test-a-{suffix}"
        subject_b = f"local:test-b-{suffix}"
        project_id = private_document_id = global_document_id = None
        try:
            global_document = db_store.create_document(None, None, {
                "file_name": f"shared-{suffix}.pdf",
                "source_path": f"/app/.data/uploads/shared-{suffix}.pdf",
                "mime_type": "application/pdf",
                "status": "ready",
            })
            global_document_id = str(global_document["id"])

            with db_store.request_scope(subject_a):
                project = db_store.create_project({
                    "name": f"private-{suffix}",
                    "knowledge_scope": "project_and_global",
                })
                project_id = str(project["id"])
                private_document = db_store.create_document(project_id, None, {
                    "file_name": f"private-{suffix}.docx",
                    "source_path": f"/app/.data/uploads/private-{suffix}.docx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "status": "ready",
                })
                private_document_id = str(private_document["id"])
                combined = db_store.list_documents(project_id)
                self.assertEqual({item["scope"] for item in combined}, {"global", "project"})
                self.assertTrue({global_document_id, private_document_id}.issubset({str(item["id"]) for item in combined}))

                db_store.update_project(project_id, {"knowledge_scope": "project_only"})
                project_only = db_store.list_documents(project_id)
                self.assertEqual([str(item["id"]) for item in project_only], [private_document_id])
                self.assertTrue(all(item["scope"] == "global" for item in db_store.list_documents()))

            with db_store.request_scope(subject_b):
                self.assertIsNone(db_store.get_project(project_id))
                self.assertIsNone(db_store.get_document(private_document_id))
                self.assertIn(global_document_id, [str(item["id"]) for item in db_store.list_documents()])
        finally:
            with db_store.db().cursor() as cursor:
                if private_document_id:
                    cursor.execute("DELETE FROM documents WHERE id = %s", (private_document_id,))
                if global_document_id:
                    cursor.execute("DELETE FROM documents WHERE id = %s", (global_document_id,))
                if project_id:
                    cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            db_store.db().commit()

    def test_global_document_management_is_shared_without_private_scope_bypass(self) -> None:
        suffix = uuid.uuid4().hex
        subject_a = f"local:global-manager-a-{suffix}"
        subject_b = f"local:global-manager-b-{suffix}"
        document_id = None
        try:
            with db_store.request_scope(subject_a, can_manage_global_documents=True):
                document = db_store.create_document(None, None, {
                    "file_name": f"shared-managed-{suffix}.md",
                    "source_path": f"/app/.data/uploads/shared-managed-{suffix}.md",
                    "mime_type": "text/markdown",
                    "status": "queued",
                })
                document_id = str(document["id"])
                self.assertIsNone(document["owner_subject"])

            with db_store.request_scope(subject_b):
                self.assertEqual(db_store.get_document(document_id)["scope"], "global")
                with self.assertRaises(PermissionError):
                    db_store.update_document(document_id, {"status": "ready"})
                with self.assertRaises(PermissionError):
                    db_store.tombstone_document(document_id)

            with db_store.request_scope(subject_b, can_manage_global_documents=True):
                updated = db_store.update_document(document_id, {"status": "ready"})
                self.assertEqual(updated["status"], "ready")
                deleted = db_store.tombstone_document(document_id)
                self.assertEqual(deleted["status"], "deleting")
        finally:
            if document_id:
                with db_store.db().cursor() as cursor:
                    cursor.execute("DELETE FROM documents WHERE id = %s", (document_id,))
                db_store.db().commit()

    def test_concurrent_first_requests_create_one_postgresql_principal(self) -> None:
        store = Persistence(os.environ["DATABASE_URL"])
        Base.metadata.create_all(store.engine)
        subject = f"local:principal-race-{uuid.uuid4().hex}"
        barrier = threading.Barrier(8)
        try:
            def enrich() -> Principal:
                barrier.wait(timeout=10)
                return store.enrich_principal(Principal(subject, provider="local"))

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                enriched = list(executor.map(lambda _: enrich(), range(8)))

            self.assertEqual([item.subject for item in enriched], [subject] * 8)
            with store.engine.connect() as connection:
                count = connection.exec_driver_sql(
                    "SELECT count(*) FROM principals WHERE subject = %s",
                    (subject,),
                ).scalar_one()
            self.assertEqual(count, 1)
        finally:
            with store.engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM principals WHERE subject = %s", (subject,))
            store.engine.dispose()
