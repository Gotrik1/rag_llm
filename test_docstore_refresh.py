import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import rag_agent


class DocstoreRefreshTests(unittest.TestCase):
    def test_api_agent_reloads_snapshot_published_by_ingestion_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "docstore.json"
            snapshot.write_text("{}", encoding="utf-8")
            agent = object.__new__(rag_agent.RAGAgent)
            agent.docstore_path = snapshot
            agent._docstore_mtime_ns = 0
            agent._index_refresh_lock = threading.Lock()
            agent.vector_store = Mock()
            agent.docstore = Mock(name="old_docstore")
            agent.index = Mock(name="old_index")
            agent._all_nodes = []
            refreshed = Mock(name="refreshed_docstore")
            refreshed.docs = {}
            refreshed_index = Mock(name="refreshed_index")

            with patch.object(rag_agent.SimpleDocumentStore, "from_persist_path", return_value=refreshed), \
                 patch.object(rag_agent.StorageContext, "from_defaults", return_value=Mock()), \
                 patch.object(rag_agent.VectorStoreIndex, "from_vector_store", return_value=refreshed_index):
                agent._refresh_docstore_if_changed()

            self.assertIs(agent.docstore, refreshed)
            self.assertIs(agent.index, refreshed_index)
            self.assertEqual(agent._docstore_mtime_ns, snapshot.stat().st_mtime_ns)

    def test_partial_snapshot_keeps_previous_index_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "docstore.json"
            snapshot.write_text("partial", encoding="utf-8")
            agent = object.__new__(rag_agent.RAGAgent)
            agent.docstore_path = snapshot
            agent._docstore_mtime_ns = 0
            agent._index_refresh_lock = threading.Lock()
            previous_docstore = Mock()
            previous_index = Mock()
            agent.docstore = previous_docstore
            agent.index = previous_index

            with patch.object(rag_agent.SimpleDocumentStore, "from_persist_path", side_effect=ValueError("partial")):
                agent._refresh_docstore_if_changed()

            self.assertIs(agent.docstore, previous_docstore)
            self.assertIs(agent.index, previous_index)
            self.assertEqual(agent._docstore_mtime_ns, 0)


if __name__ == "__main__":
    unittest.main()
