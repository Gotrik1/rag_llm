import cgi
import sys
import types
import unittest

from legacy_adapter import invoke_legacy


class LegacyAdapterMultipartTests(unittest.TestCase):
    def test_multipart_boundary_reaches_legacy_handler(self):
        boundary = "----rag-adapter-test"
        raw_body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="regulation.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "# regulation\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        class Handler:
            def do_POST(self):
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": self.headers["Content-Type"],
                        "CONTENT_LENGTH": self.headers["Content-Length"],
                    },
                )
                item = form["files"]
                self.send_json({"filename": item.filename, "content": item.file.read().decode()})

        fake_web_ui = types.ModuleType("web_ui")
        fake_web_ui.Handler = Handler
        previous = sys.modules.get("web_ui")
        sys.modules["web_ui"] = fake_web_ui
        try:
            payload, status = invoke_legacy(
                "POST",
                "/api/upload",
                content_type=f"multipart/form-data; boundary={boundary}",
                raw_body=raw_body,
            )
        finally:
            if previous is None:
                del sys.modules["web_ui"]
            else:
                sys.modules["web_ui"] = previous

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"filename": "regulation.md", "content": "# regulation"})


if __name__ == "__main__":
    unittest.main()
