import unittest

from db_store import _rebuild_document_version_chain


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, statement, params):
        self.executed.append((" ".join(statement.split()), params))

    def fetchall(self):
        return list(self.rows)


class DocumentVersionChainTests(unittest.TestCase):
    def test_chain_handles_out_of_order_uploads_and_duplicate_labels(self):
        cursor = FakeCursor([
            {"id": "v1-a", "version_sort_key": [1, 0, 0, 0]},
            {"id": "v1-b", "version_sort_key": [1, 0, 0, 0]},
            {"id": "v2", "version_sort_key": [2, 0, 0, 0]},
            {"id": "v3", "version_sort_key": [3, 0, 0, 0]},
        ])

        _rebuild_document_version_chain(
            cursor,
            (None, None, None, "методика", "semantic"),
        )

        updates = [params for statement, params in cursor.executed if statement.startswith("UPDATE documents")]
        self.assertEqual(updates, [
            (None, "v1-a"),
            (None, "v1-b"),
            ("v1-a", "v2"),
            ("v2", "v3"),
        ])


if __name__ == "__main__":
    unittest.main()
