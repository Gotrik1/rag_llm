import unittest

from db_store import _backfill_document_versions


class _Cursor:
    def __init__(self, rows, statements):
        self.rows = rows
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement, params=None):
        self.statements.append((" ".join(statement.split()), params))

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)
        self.statements = []
        self.commit_calls = 0

    def cursor(self):
        rows = self.result_sets.pop(0) if self.result_sets else []
        return _Cursor(rows, self.statements)

    def commit(self):
        self.commit_calls += 1


class DocumentVersionBackfillTests(unittest.TestCase):
    def test_reclassifies_an_existing_record_when_detector_improves(self):
        connection = _Connection([[
            {
                "id": "doc-1",
                "file_name": "Методика расчеты на РРЭ в 1.docx",
                "metadata": {},
                "document_family": "методика расчеты на ррэ в 1",
                "version_label": None,
                "version_kind": None,
                "version_sort_key": None,
                "version_source": None,
                "effective_on": None,
            },
        ], []])

        _backfill_document_versions(connection)

        updates = [item for item in connection.statements if item[0].startswith("UPDATE documents")]
        self.assertEqual(len(updates), 1)
        params = updates[0][1]
        self.assertEqual(params[0], "методика расчеты на ррэ")
        self.assertEqual(params[1], "1")
        self.assertEqual(params[2], "semantic")
        self.assertEqual(params[3], [1, 0, 0, 0])
        self.assertEqual(params[4], "file_name")
        self.assertEqual(params[-1], "doc-1")
        self.assertEqual(connection.commit_calls, 2)

    def test_does_not_rewrite_unchanged_document_metadata(self):
        connection = _Connection([[
            {
                "id": "doc-1",
                "file_name": "Методика v2.docx",
                "metadata": {},
                "document_family": "методика",
                "version_label": "2",
                "version_kind": "semantic",
                "version_sort_key": [2, 0, 0, 0],
                "version_source": "file_name",
                "effective_on": None,
            },
        ], []])

        _backfill_document_versions(connection)

        updates = [item for item in connection.statements if item[0].startswith("UPDATE documents")]
        self.assertEqual(updates, [])
        self.assertEqual(connection.commit_calls, 1)


if __name__ == "__main__":
    unittest.main()
