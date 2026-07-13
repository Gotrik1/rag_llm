import unittest
from unittest.mock import AsyncMock, Mock, patch

from jobs import JobQueue


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_aborts_the_arq_job_and_persists_state(self) -> None:
        store = Mock()
        store.request_cancel.return_value = True
        queue = JobQueue("redis://example.invalid:6379/0")
        queue._redis = Mock()
        arq_job = Mock()
        arq_job.abort = AsyncMock()

        with patch("jobs.get_store", return_value=store), patch("jobs.Job", return_value=arq_job):
            self.assertTrue(await queue.cancel("job-1"))

        arq_job.abort.assert_awaited_once_with()
        store.update_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
