"""A small transcription service for pod-block: faster-whisper, and who speaks when, over HTTP.

POST /transcribe with an episode's audio as the body (and `?language=en` if the
show's language is known) answers with one JSON object per line, as they come:

    {"waiting": true}                          at once, and every so often, while another episode is worked on
    {"language": "en", "duration": 3605.2}     once the audio is read and its language known
    {"start": 1.2, "end": 4.8, "text": "...", "words": [[1.2, 1.5, " Hello"], ...]}
    {"step": "speakers"}                       then, with speakers turned on, who speaks when:
    {"progress": 0.4}                          how far through that
    {"speakers": [[1.0, 9.5, 0], [9.5, 12.0, 1], ...], "options": {...}}   start, end, speaker
    {"done": true}                             the last line, when it all went well
    {"error": "..."}                           the last line, when it didn't

POST /speakers does the speakers part alone, to try other settings without
transcribing again; both take `threshold`, `count`, `min_on` and `min_off`
(see SPEAKER_* below) to override the defaults, and `speakers=0` turns them
off for one request.

One episode at a time; others wait their turn. The first "waiting" line comes
straight away, so a client with other replicas to try can hang up and ask
again. A client that hangs up stops its work at the next segment. GET /healthz
answers once the models are loaded.

On SIGTERM it finishes the episode in hand, sends away those waiting (they
see the answer end with no "done", and ask again), answers 503 to new ones,
and then exits.

Replicas can share one models folder (a ReadWriteMany volume): downloads take
a lock file in it (`.fetch.lock`), so the first replica to start fetches the
models and the others wait, then find them there.

Settings, from the environment:

    WHISPER_MODEL         a faster-whisper model name or path (default: small)
    WHISPER_DEVICE        cpu (default), or cuda, or auto
    WHISPER_COMPUTE_TYPE  int8 (default, quickest on CPU), int8_float32, float32, ...
    WHISPER_THREADS       CPU threads (default: 4, or OMP_NUM_THREADS)
    WHISPER_BEAM_SIZE     5 (default); 1 is about twice as quick and a little less accurate
    WHISPER_BATCH         transcribe this many pieces of the episode at once (default 8; 0 for one after another)
    WHISPER_SPEAKERS      true to say who speaks when (sherpa-onnx, models fetched on first start)
    SPEAKER_THRESHOLD     how different two voices must be to count as two speakers (default 0.8; higher, fewer)
    SPEAKER_COUNT         the number of speakers, when known (default: work it out)
    SPEAKER_MIN_ON        the shortest turn kept, in seconds (default 0.3)
    SPEAKER_MIN_OFF       gaps shorter than this between one speaker's turns are joined (default 0.5)
    WHISPER_PORT          9000 (default)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import tarfile
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger("whisper")

WAIT_NOTE = 15  # seconds between "still waiting" lines, so the client's read timeout doesn't run out
MAX_BYTES = 2 << 30  # an episode bigger than this is something else
RATE = 16000  # samples a second, as both models take them
MODELS = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION = f"{MODELS}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING = f"{MODELS}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"  # sic: the tag's name

model = None  # a faster_whisper.WhisperModel, once loaded
batched = None  # and its BatchedInferencePipeline, with WHISPER_BATCH
decode = None  # faster_whisper's decode_audio: a file to 16 kHz mono samples
speakers = None  # a Speakers, with WHISPER_SPEAKERS
turn = threading.Lock()  # one episode at a time: each takes every core it is given
stopping = threading.Event()  # set by SIGTERM: finish the episode in hand, take no more


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def options() -> dict:
    found = {"beam_size": int(os.environ.get("WHISPER_BEAM_SIZE") or 5),
             "vad_filter": True,  # skips music and silence, where whisper makes things up
             "word_timestamps": True}  # so pod-block can split captions where words fall
    if batched is not None:
        found["batch_size"] = int(os.environ.get("WHISPER_BATCH") or 8)
    return found


class Hangup(Exception):
    pass


class Speakers:
    """Who speaks when, by sherpa-onnx: pyannote's segmentation model finds turns, a speaker embedding model
    describes each voice, and voices closer than `threshold` count as one speaker."""

    def __init__(self, folder: Path, threads: int):
        self.segmentation = folder / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
        self.embedding = folder / EMBEDDING.rsplit("/", 1)[1]
        self.threads = threads
        self.fetch(folder)

    def fetch(self, folder: Path) -> None:
        """Download the models that aren't in `folder` yet. Each lands under a name of its own and is moved into
        place whole, so a replica that starts meanwhile never reads half of one."""
        folder.mkdir(parents=True, exist_ok=True)
        if not self.segmentation.exists():
            log.info("fetching %s", SEGMENTATION)
            tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=folder))
            try:
                with urllib.request.urlopen(SEGMENTATION) as resp, tarfile.open(fileobj=resp, mode="r|bz2") as tar:
                    tar.extractall(tmp, filter="data")
                if not self.segmentation.exists():
                    shutil.rmtree(self.segmentation.parent, ignore_errors=True)  # what an older version left half done
                    (tmp / self.segmentation.parent.name).replace(self.segmentation.parent)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        if not self.embedding.exists():
            log.info("fetching %s", EMBEDDING)
            fd, tmp = tempfile.mkstemp(prefix=".fetch-", dir=folder)
            os.close(fd)
            try:
                urllib.request.urlretrieve(EMBEDDING, tmp)
                Path(tmp).replace(self.embedding)
            finally:
                Path(tmp).unlink(missing_ok=True)

    @staticmethod
    def defaults() -> dict:
        return {"threshold": _env_float("SPEAKER_THRESHOLD", 0.8), "count": int(os.environ.get("SPEAKER_COUNT") or 0),
                "min_on": _env_float("SPEAKER_MIN_ON", 0.3), "min_off": _env_float("SPEAKER_MIN_OFF", 0.5)}

    def run(self, samples, opts: dict, progress) -> list[list]:
        """Turns as [start, end, speaker], speakers numbered from 0 in the order they first speak. `progress` is
        given the fraction done, and stops it by raising Hangup."""
        import sherpa_onnx as so

        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(self.segmentation)),
                num_threads=self.threads),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=str(self.embedding), num_threads=self.threads),
            clustering=so.FastClusteringConfig(num_clusters=opts["count"] or -1, threshold=opts["threshold"]),
            min_duration_on=opts["min_on"], min_duration_off=opts["min_off"])
        stopped = []

        def tell(done: int, total: int) -> int:
            try:
                progress(done / total if total else None)
            except Hangup:
                stopped.append(True)
                return 1  # sherpa-onnx stops on anything but 0
            return 0

        found = so.OfflineSpeakerDiarization(config).process(samples, callback=tell).sort_by_start_time()
        if stopped:
            raise Hangup()
        names: dict[int, int] = {}
        return [[round(s.start, 3), round(s.end, 3), names.setdefault(s.speaker, len(names))] for s in found]


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
        if url.path not in ("/transcribe", "/speakers"):
            return self.send_error(404)
        if model is None:
            return self.send_error(503, "the models are still loading")
        if stopping.is_set():
            return self.send_error(503, "stopping")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/speakers" and speakers is None:
            return self.send_error(404, "speakers are off (WHISPER_SPEAKERS)")
        try:
            opts = Speakers.defaults()
            opts.update({k: type(opts[k])(query[k]) for k in opts if query.get(k)})
        except ValueError:
            return self.send_error(400, "threshold, count, min_on and min_off are numbers")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BYTES:
            return self.send_error(411 if not size else 413)
        language = query.get("language")
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
                if not self._take_turn():
                    return  # stopping: the answer ends without "done", and the client asks again
                try:
                    self._work(audio.name, url.path == "/transcribe", language,
                               opts if speakers is not None and query.get("speakers") != "0" else None)
                finally:
                    turn.release()
            except (BrokenPipeError, ConnectionResetError, Hangup):
                log.info("the client hung up; stopped")

    def _take_turn(self) -> bool:
        """Wait for the one episode being worked on, saying so at once and then every WAIT_NOTE seconds. False
        if the service is stopping meanwhile."""
        if not turn.acquire(blocking=False):
            self._line({"waiting": True})
            while not turn.acquire(timeout=WAIT_NOTE):
                if stopping.is_set():
                    return False
                self._line({"waiting": True})
        if stopping.is_set():
            turn.release()
            return False
        return True

    def _line(self, obj: dict) -> None:
        try:
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise Hangup() from None

    def _work(self, path: str, words: bool, language: str | None, speaker_opts: dict | None) -> None:
        try:
            samples = decode(path)
            if words:
                segments, info = (batched or model).transcribe(samples, language=language, **options())
                self._line({"language": info.language, "duration": round(info.duration, 3)})
                for seg in segments:
                    found = [[round(w.start, 3), round(w.end, 3), w.word] for w in seg.words or []]
                    self._line({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text,
                                "words": found})
            else:
                self._line({"duration": round(len(samples) / RATE, 3)})
            if speaker_opts is not None:
                self._line({"step": "speakers"})
                turns = speakers.run(samples, speaker_opts, lambda f: self._line({"progress": round(f or 0, 3)}))
                self._line({"speakers": turns, "options": speaker_opts})
        except Hangup:
            raise
        except Exception as e:
            log.exception("transcribing failed")
            self._line({"error": f"{type(e).__name__}: {e}"[:500]})
            return
        self._line({"done": True})


@contextmanager
def fetching(folder: Path):
    """Hold the lock file in the models folder while models are downloaded, so replicas sharing the folder
    fetch them once. Where the volume can't lock (ENOLCK), each fetches on its own."""
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / ".fetch.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as e:
                log.warning("couldn't lock %s (%s); fetching without it", folder / ".fetch.lock", e)
        yield
    finally:
        os.close(fd)


def main() -> None:
    global model, batched, decode, speakers
    from faster_whisper import BatchedInferencePipeline, WhisperModel  # here, so tests can use stand-ins
    from faster_whisper.audio import decode_audio
    from faster_whisper.utils import download_model

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for name in ("httpx", "httpx2"):  # each request of the model's download
        logging.getLogger(name).setLevel(logging.WARNING)
    name = os.environ.get("WHISPER_MODEL") or "small"
    device = os.environ.get("WHISPER_DEVICE") or "cpu"
    compute = os.environ.get("WHISPER_COMPUTE_TYPE") or "int8"
    threads = int(os.environ.get("WHISPER_THREADS") or 0)
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("WHISPER_PORT") or 9000)), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()  # /healthz says "loading" meanwhile
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache")
    with fetching(home):
        if os.environ.get("WHISPER_SPEAKERS", "").strip().lower() in {"1", "true", "yes", "on"}:
            speakers = Speakers(home / "speakers", threads or 4)
        log.info("fetching %s, unless it is in %s already", name, home)
        path = name if os.path.isdir(name) else download_model(name)
    log.info("loading %s on %s (%s)", name, device, compute)
    whisper = WhisperModel(path, device=device, compute_type=compute, cpu_threads=threads)
    batched = BatchedInferencePipeline(whisper) if int(os.environ.get("WHISPER_BATCH") or 8) > 1 else None
    decode = decode_audio
    model = whisper
    log.info("ready on port %d%s%s", server.server_port, f", batches of {options()['batch_size']}" if batched
             else "", ", with speakers" if speakers else "")
    stopping.wait()
    log.info("stopping once the episode in hand is done")
    turn.acquire()  # those waiting are sent away at their next note, so this is the one being worked on
    log.info("stopped")


if __name__ == "__main__":
    main()
