import io
import json
import unittest
from unittest.mock import Mock, patch

import deletion_task


class DeletionTaskTests(unittest.TestCase):
    def test_path_argument_is_required(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["deletion_task"]), patch("sys.stdout", output):
            status = deletion_task.main()
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "path argument is required"})

    def test_success_prints_deleted_chunk_count(self) -> None:
        agent = Mock()
        agent.delete_source.return_value = 4
        output = io.StringIO()
        with patch("sys.argv", ["deletion_task", "manual.pdf"]), \
             patch("sys.stdout", output), \
             patch("web_ui.get_agent", return_value=agent):
            status = deletion_task.main()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"chunks_deleted": 4})
        agent.delete_source.assert_called_once_with("manual.pdf")

    def test_cleanup_error_is_serialized_for_worker(self) -> None:
        agent = Mock()
        agent.delete_source.side_effect = RuntimeError("qdrant failed")
        output = io.StringIO()
        with patch("sys.argv", ["deletion_task", "manual.pdf"]), \
             patch("sys.stdout", output), \
             patch("web_ui.get_agent", return_value=agent):
            status = deletion_task.main()
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "qdrant failed"})


if __name__ == "__main__":
    unittest.main()
