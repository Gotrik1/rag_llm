import io
import json
import unittest
from unittest.mock import Mock, patch

import ingestion_task


class IngestionTaskTests(unittest.TestCase):
    def test_path_argument_is_required(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["ingestion_task"]), patch("sys.stdout", output):
            status = ingestion_task.main()
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "path argument is required"})

    def test_success_prints_chunk_count(self) -> None:
        agent = Mock()
        agent.ingest.return_value = 17
        output = io.StringIO()
        with patch("sys.argv", ["ingestion_task", "manual.pdf"]), \
             patch("sys.stdout", output), \
             patch("web_ui.get_agent", return_value=agent):
            status = ingestion_task.main()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"chunks": 17})
        agent.ingest.assert_called_once_with("manual.pdf")

    def test_ingestion_error_is_serialized_for_worker(self) -> None:
        agent = Mock()
        agent.ingest.side_effect = RuntimeError("parser failed")
        output = io.StringIO()
        with patch("sys.argv", ["ingestion_task", "broken.pdf"]), \
             patch("sys.stdout", output), \
             patch("web_ui.get_agent", return_value=agent):
            status = ingestion_task.main()
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "parser failed"})


if __name__ == "__main__":
    unittest.main()
