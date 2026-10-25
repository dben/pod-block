"""A stand-in for the transcription service (whisper/), for the screenshots.

It knows each made-up episode by its file's SHA-256 and answers with the script feeds.py wrote for it, in the
service's protocol (see "The protocol" in whisper/README.md).

usage: transcriber.py SITE_DIR   (listens on 127.0.0.1:9000)
"""

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(sys.argv[1]) / "script"


class Transcriber(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        found = SCRIPTS / f"{hashlib.sha256(body).hexdigest()}.json"
        if not found.exists():
            self.send_response(404)
            self.end_headers()
            return
        heard = json.loads(found.read_text())
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        lines = [{"language": "en", "duration": heard["duration"]}]
        if self.path.startswith("/transcribe"):
            lines += heard["segments"]
        lines += [{"step": "speakers"}, {"speakers": heard["speakers"], "options": {"threshold": 0.8}}, {"done": True}]
        self.wfile.write("".join(json.dumps(line) + "\n" for line in lines).encode())

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", 9000), Transcriber).serve_forever()
