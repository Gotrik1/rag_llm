import sys
import types
import unittest

from legacy_adapter import invoke_legacy
from web_ui import parse_multipart_files


class LegacyAdapterMultipartTests(unittest.TestCase):
    def test_parser_rejects_header_injection_and_non_multipart_content(self):
        self.assertEqual(parse_multipart_files("text/plain", b"payload"), [])
        self.assertEqual(
            parse_multipart_files("multipart/form-data; boundary=x\r\nX-Evil: 1", b"payload"),
            [],
        )

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
                files = parse_multipart_files(
                    self.headers["Content-Type"],
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
                self.send_json({"filename": files[0]["filename"], "content": files[0]["content"].decode()})

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
