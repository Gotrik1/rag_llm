"""State-transition regressions at the controller/application service boundary."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from application_services import DocumentService, GenerationService, ServiceError


class ApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    def generation(self, queue):
        return GenerationService(
            get_chat=Mock(return_value={"id": "chat-1"}),
            create_message=Mock(return_value={"id": "message-1"}),
            update_message=Mock(),
            run_ask=Mock(),
            queue=queue,
            tracked_tasks=set(),
            forget_task=lambda _: None,
            durable=True,
            wait_seconds=1,
        )

    def documents(self, directory, *, create=None, queue=None):
        return DocumentService(
            create_document=create or Mock(return_value={"id": "document-1"}),
            update_document=Mock(),
            get_document=Mock(),
            tombstone_document=Mock(),
            infer_domains=lambda _: (),
            safe_name=lambda n: n,
            queue=queue or SimpleNamespace(submit=AsyncMock()),
            upload_dir=Path(directory),
            allowed_suffixes={".md"},
            max_bytes=10,
            max_files=2,
        )

    def upload(self, data=b"hello"):
        return SimpleNamespace(
            filename="manual.md",
            content_type="text/markdown",
            read=AsyncMock(side_effect=[data, b""]),
        )

    async def test_generation_queue_failure_persists_error_before_returning_unavailable(self):
        service = self.generation(
            SimpleNamespace(submit=AsyncMock(side_effect=ConnectionError("offline")))
        )
        with self.assertRaises(ServiceError) as caught:
            await service.execute({"question": "test", "chat_id": "chat-1"}, "owner-1", False)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(service.update_message.call_args.args[1]["status"], "error")
        service.run_ask.assert_not_called()

    async def test_client_owner_fields_cannot_override_authenticated_owner(self):
        service = self.generation(SimpleNamespace())
        request = await service.prepare(
            {"question": "test", "_owner_subject": "other", "_owner_admin": True}, "owner-1", False
        )
        self.assertEqual(request["_owner_subject"], "owner-1")
        self.assertFalse(request["_owner_admin"])

    async def test_whitespace_question_fails_before_any_chat_or_generation_mutation(self):
        service = self.generation(SimpleNamespace())
        with self.assertRaises(ServiceError) as caught:
            await service.prepare({"question": " \n ", "chat_id": "chat-1"}, "owner-1", False)
        self.assertEqual(caught.exception.status_code, 400)
        service.get_chat.assert_not_called()
        service.create_message.assert_not_called()

    async def test_upload_rejected_by_registry_leaves_no_unowned_file_or_job(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = SimpleNamespace(submit=AsyncMock())
            service = self.documents(
                directory, create=Mock(side_effect=PermissionError("private project")), queue=queue
            )
            with self.assertRaises(PermissionError):
                await service.upload([self.upload()], "project-1", "", "owner-1", False)
            self.assertEqual(list(Path(directory).iterdir()), [])
            queue.submit.assert_not_awaited()

    async def test_oversized_upload_is_removed_before_registry_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.documents(directory)
            with self.assertRaises(ServiceError) as caught:
                await service.upload([self.upload(b"x" * 11)], "project-1", "", "owner-1", False)
            self.assertEqual(caught.exception.status_code, 413)
            self.assertEqual(list(Path(directory).iterdir()), [])
            service.create_document.assert_not_called()

    async def test_registered_upload_is_retained_as_failed_when_queue_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.documents(
                directory,
                queue=SimpleNamespace(submit=AsyncMock(side_effect=ConnectionError("offline"))),
            )
            with self.assertRaises(ServiceError) as caught:
                await service.upload([self.upload()], "project-1", "", "owner-1", False)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertEqual(service.update_document.call_args.args[1]["status"], "failed")

    async def test_deletion_hides_source_before_queue_failure_without_resurrection(self):
        events = []

        async def unavailable(*_args):
            events.append("enqueue")
            raise ConnectionError("offline")

        service = self.documents("unused", queue=SimpleNamespace(submit=unavailable))
        service.get_document.return_value = {
            "id": "document-1",
            "owner_subject": "owner-1",
            "source_path": "manual.md",
        }
        service.tombstone_document.side_effect = lambda _: events.append("hidden") or {
            "id": "document-1"
        }
        with self.assertRaises(ServiceError) as caught:
            await service.delete("document-1", "owner-1", False)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(events, ["hidden", "enqueue"])
        service.update_document.assert_not_called()


if __name__ == "__main__":
    unittest.main()
