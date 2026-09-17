import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psycopg

from db_store import read_cursor


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, status):
        self.closed = False
        self.info = SimpleNamespace(transaction_status=status)
        self.cursor_value = object()
        self.rollback_calls = 0

    def cursor(self):
        return _CursorContext(self.cursor_value)

    def rollback(self):
        self.rollback_calls += 1
        self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE


class ReadCursorTests(unittest.TestCase):
    def test_rolls_back_select_transaction_after_success(self):
        connection = _Connection(psycopg.pq.TransactionStatus.INTRANS)

        with patch("db_store.db", return_value=connection):
            with read_cursor() as cursor:
                self.assertIs(cursor, connection.cursor_value)

        self.assertEqual(connection.rollback_calls, 1)

    def test_rolls_back_failed_transaction_before_reraising(self):
        connection = _Connection(psycopg.pq.TransactionStatus.INERROR)

        with self.assertRaisesRegex(RuntimeError, "boom"):
            with patch("db_store.db", return_value=connection):
                with read_cursor():
                    raise RuntimeError("boom")

        self.assertEqual(connection.rollback_calls, 1)

    def test_does_not_touch_idle_connection(self):
        connection = _Connection(psycopg.pq.TransactionStatus.IDLE)

        with patch("db_store.db", return_value=connection):
            with read_cursor():
                pass

        self.assertEqual(connection.rollback_calls, 0)


if __name__ == "__main__":
    unittest.main()
