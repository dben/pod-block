"""A small transcription service for pod-block: faster-whisper behind one HTTP endpoint.

POST /transcribe with an episode's audio as the body (and `?language=en` if the
show's language is known) answers with one JSON object per line, as they come:

    {"waiting": true}                          while another episode is being transcribed
    {"language": "en", "duration": 3605.2}     once the audio is read and its language known
    {"start": 1.2, "end": 4.8, "text": "...", "words": [[1.2, 1.5, " Hello"], ...]}
    {"done": true}                             the last line, when it all went well
    {"error": "..."}                           the last line, when it didn't

One episode is transcribed at a time; others wait their turn. A client that
hangs up stops its transcription at the next segment. GET /healthz answers
once the model is loaded.

Settings, from the environment:

    WHISPER_MODEL         a faster-whisper model name or path (default: small)
    WHISPER_DEVICE        cpu (default), or cuda, or auto
    WHISPER_COMPUTE_TYPE  int8 (default, quickest on CPU), int8_float32, float32, ...
    WHISPER_THREADS       CPU threads (default: 4, or OMP_NUM_THREADS)
    WHISPER_BEAM_SIZE     5 (default); 1 is about twice as quick and a little less accurate
    WHISPER_PORT          9000 (default)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("whisper")

WAIT_NOTE = 15  # seconds between "still waiting" lines, so the client's read timeout doesn't run out
MAX_BYTES = 2 << 30  # an episode bigger than this is something else

model = None  # a faster_whisper.WhisperModel, once loaded
turn = threading.Lock()  # one transcription at a time: each takes every core it is given


def options() -> dict:
    return {"beam_size": int(os.environ.get("WHISPER_BEAM_SIZE") or 5),
            "vad_filter": True,  # skips music and silence, where whisper makes things up
            "word_timestamps": True}  # so pod-block can split captions where words fall


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # the answer ends when the connection closes; no chunking needed

    def log_message(self, fmt, *args):
        if not self.path.startswith("/healthz"):  # probes come every few seconds
            log.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        if urlparse(self.path).path != "/healthz":
            return self.send_error(404)
        self.send_response(200 if model is not None else 503)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok\n" if model is not None else b"loading\n")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/transcribe":
            return self.send_error(404)
        if model is None:
            return self.send_error(503, "the model is still loading")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BYTES:
            return self.send_error(411 if not size else 413)
        language = (parse_qs(url.query).get("language") or [None])[0]
        if language not in model.supported_languages:  # one whisper doesn't know: let it listen for it
            language = None
        with tempfile.NamedTemporaryFile(prefix="episode-") as audio:
            left = size
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    return
                audio.write(chunk)
                left -= len(chunk)
            audio.flush()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                while not turn.acquire(timeout=WAIT_NOTE):
                    self._line({"waiting": True})
                try:
                    self._transcribe(audio.name, language)
                finally:
                    turn.release()
            except (BrokenPipeError, ConnectionResetError):
                log.info("the client hung up; stopped")

    def _line(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        self.wfile.flush()

    def _transcribe(self, path: str, language: str | None) -> None:
        try:
            segments, info = model.transcribe(path, language=language, **options())
            self._line({"language": info.language, "duration": round(info.duration, 3)})
            for seg in segments:
                words = [[round(w.start, 3), round(w.end, 3), w.word] for w in seg.words or []]
                self._line({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text,
                            "words": words})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:
            log.exception("transcribing failed")
            self._line({"error": f"{type(e).__name__}: {e}"[:500]})
            return
        self._line({"done": True})


def main() -> None:
    global model
    from faster_whisper import WhisperModel  # here, so the tests can run the server with a stand-in

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # each request of the model's download
    name = os.environ.get("WHISPER_MODEL") or "small"
    device = os.environ.get("WHISPER_DEVICE") or "cpu"
    compute = os.environ.get("WHISPER_COMPUTE_TYPE") or "int8"
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("WHISPER_PORT") or 9000)), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()  # /healthz says "loading" meanwhile
    log.info("loading %s on %s (%s); a first start downloads it", name, device, compute)
    model = WhisperModel(name, device=device, compute_type=compute,
                         cpu_threads=int(os.environ.get("WHISPER_THREADS") or 0))
    log.info("ready on port %d", server.server_port)
    threading.Event().wait()


if __name__ == "__main__":
    main()
