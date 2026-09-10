import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import rag_agent


class DocstoreRefreshTests(unittest.TestCase):
    def test_delete_source_removes_matching_qdrant_and_bm25_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "docstore.json"
            snapshot.write_text("{}", encoding="utf-8")
            target = Path(directory) / "unique_manual.pdf"
            target.write_text("test", encoding="utf-8")
            matching = Mock(id_="node-1", metadata={"file_path": str(target), "file_name": target.name})
            other = Mock(id_="node-2", metadata={"file_path": str(Path(directory) / "other.pdf")})
            agent = object.__new__(rag_agent.RAGAgent)
            agent.docstore_path = snapshot
            agent._docstore_mtime_ns = snapshot.stat().st_mtime_ns
            agent._all_nodes = [matching, other]
            agent.vector_store = Mock()
            agent.docstore = Mock()
            agent._refresh_docstore_if_changed = Mock()
            agent.clear_cache = Mock()

            def persist_snapshot(*, persist_path: str) -> None:
                Path(persist_path).write_text("{}", encoding="utf-8")

            agent.docstore.persist.side_effect = persist_snapshot
            deleted = agent.delete_source(str(target))

            self.assertEqual(deleted, 1)
            agent.vector_store.delete_nodes.assert_called_once_with(["node-1"])
            agent.docstore.delete_document.assert_called_once_with("node-1", raise_error=False)
            self.assertEqual([node.id_ for node in agent._all_nodes], ["node-2"])
            agent.clear_cache.assert_called_once_with()

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
