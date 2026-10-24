# Captions with faster-whisper

An optional second container that transcribes episodes, so the private feeds
carry captions (`podcast:transcript`, WebVTT, `rel="captions"`) for apps that
show them.

When `PODBLOCK_WHISPER_URL` points at it, each episode pod-block downloads
and cuts gets a **captions** job, unless its feed already has a timed
transcript. The job sends the download to this service, which streams back
what it hears with a time for each word. pod-block splits that into captions
of up to two 42-character lines and saves them beside the episode. They are
served like a feed's own transcript, with times moved to fit the cut audio,
so re-cutting an episode needs no new transcript.

- Captions jobs have their own worker threads, so a long transcription
  doesn't hold up downloads. They show on the Jobs page as "Captions:
  *episode*". One runs at a time, unless `PODBLOCK_CAPTION_WORKERS` says
  more (see [More than one replica](#more-than-one-replica)).
- A transcription cut off part way, by a restart say, is asked for once more
  before the job fails.
- The episode stays in the feeds while its captions are made; the feed picks
  them up on the app's next refresh.
- The show's `<language>` is passed to whisper when it knows it; otherwise
  whisper works out the language from the first 30 seconds.
- Music and silence are skipped (whisper's voice detection), which keeps it
  from making up lyrics for theme tunes.
- Episodes downloaded before this was set up get captions on **Process
  again**. Episodes whose download was cleaned up need downloading again.

## Running it

### Docker

```sh
docker network create pods
docker run -d --name pod-block-whisper --network pods \
  -v pb-models:/models ghcr.io/dben/pod-block-whisper:latest
docker run -d --name pod-block --network pods -p 8000:8000 \
  -v pb-data:/data -v pb-media:/media \
  -e PODBLOCK_WHISPER_URL=http://pod-block-whisper:9000 \
  ghcr.io/dben/pod-block:latest
```

The first start downloads the model into `/models` (small: about 0.5 GB).

### Kubernetes

```sh
kubectl apply -k deployment/whisper/
```

Then uncomment `PODBLOCK_WHISPER_URL` and `PODBLOCK_CAPTION_WORKERS` in
`deployment/kustomization.yaml` and apply that too. Set `WHISPER_THREADS` in
`deployment/whisper/kustomization.yaml` to the cores it may use.

The example runs two replicas sharing one ReadWriteMany models volume, so it
needs a storage class that can do that (NFS, CephFS, Longhorn RWX, EFS, Azure
Files); `whisper.yaml` says what to do without one.

### More than one replica

Each replica transcribes one episode at a time, so more replicas, spread over
more nodes, transcribe more episodes at once.

- **pod-block's side.** Set `PODBLOCK_CAPTION_WORKERS` to the number of
  replicas: that many captions jobs run at once.
- **Finding a free replica.** The Service hands each request to any replica,
  busy or not. A busy one says so straight away, and pod-block then hangs up
  and sends the episode again a few seconds later, for the Service to try
  another. After 10 minutes of that (every replica busy, say) it waits its
  turn where it is. It only does this with `PODBLOCK_CAPTION_WORKERS` above
  1. Each new try sends the episode again, which in a cluster takes a second
  or two.
- **The models volume.** Replicas share it. The first to start downloads the
  models while holding a lock file in it (`/models/.fetch.lock`); the others
  wait for it, then load what it fetched.
- **Stopping.** On SIGTERM a replica finishes the episode in hand, sends away
  those waiting for it (pod-block asks again, and the Service sends them
  elsewhere), refuses new ones and exits. The example allows it an hour
  (`terminationGracePeriodSeconds`). Updates roll one replica at a time and
  start the new one before stopping the old.
- **Spreading.** The example asks Kubernetes to put replicas on different
  nodes where it can. With a GPU node, add a `nodeSelector` and
  `nvidia.com/gpu: 1` to the limits, and a CUDA image (`WHISPER_DEVICE`).

With Docker, more than one container behind one network alias
(`--network-alias pod-block-whisper`) works the same way: Docker's DNS hands
out each one in turn.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3-turbo`, or an `.en` model (`small.en`) for English-only shows |
| `WHISPER_COMPUTE_TYPE` | `int8` | quickest on a CPU; `float32` is a little more accurate and much slower |
| `WHISPER_THREADS` | 4 | CPU threads |
| `WHISPER_BEAM_SIZE` | 5 | 1 is about twice as quick, with a few more mistakes |
| `WHISPER_BATCH` | 8 | transcribe this many pieces of an episode at once (faster-whisper's batched pipeline): about twice as quick on a CPU; 0 for one after another |
| `WHISPER_SPEAKERS` | off | `true` to also find who speaks when (below) |
| `SPEAKER_THRESHOLD` | 0.8 | how different two voices must be to count as two speakers; higher, fewer |
| `SPEAKER_COUNT` | | the number of speakers, when every episode has the same |
| `SPEAKER_MIN_ON` | 0.3 | the shortest turn kept, in seconds |
| `SPEAKER_MIN_OFF` | 0.5 | one speaker's turns closer than this are joined, in seconds |
| `WHISPER_DEVICE` | `cpu` | `cuda` for an Nvidia GPU (needs a CUDA base image) |
| `WHISPER_PORT` | 9000 | |

### Speed

Time taken as a share of the episode's length, on 4 cores of a 2.8 GHz Xeon,
`int8`, from a 15-minute stretch of a real episode:

| Model | One at a time | `WHISPER_BATCH=8` |
| --- | --- | --- |
| `base` | | 0.07 |
| `small` | 0.38 | 0.20 (0.15 with `WHISPER_BEAM_SIZE=1`) |
| `large-v3-turbo` | | 0.43 |

Speakers add about 0.17 on the same cores (52 and 67 minute episodes).
`large-v3-turbo` gets names and the show's own words noticeably more right
than `small`; with speakers on it takes about 0.6 of an episode's length, so
an hour-long episode takes about 35 minutes. Batching also punctuates better.

## Who speaks when

With `WHISPER_SPEAKERS=true` the service also works out who speaks when:
pyannote's segmentation model finds the turns and a WeSpeaker voice model
(trained on English speech) tells the voices apart, both run by sherpa-onnx
on the CPU, with no PyTorch and no Hugging Face token. The models (about
35 MB) are fetched into `/models/speakers` on first start.

pod-block saves the turns beside the episode's captions. They don't go into
the captions yet. `podblock speakers EPISODE_ID` sets them beside the
episode's repeats: each voice's talk time, how much of it is inside a
repeated section, and **lone voices**, heard briefly and in one stretch only.
The idea is that an ad read by someone who isn't on the show is a lone voice.
A lone voice outside every repeat may be an ad the fingerprints haven't
caught yet. It also calls out voices heard mostly inside the repeats: on a
network show, often the announcer or a voice the ads share.

To tune it, ask again with other settings; only the speakers part runs, not
the transcription:

```sh
podblock speakers 42 --threshold 0.7
podblock speakers 42 --count 3       # when you know how many people talk
```

`--json` gives the turns and the comparison as JSON.

What three real shows showed so far:

- Two shows read their own ads, so their ads come out as the hosts' voices
  and there is nothing for this to find.
- On a network show with dynamic ad breaks, 0.8 kept the two hosts apart
  but folded most ad voices into them; one announcer voice still came out
  57% inside the repeats. At 0.65 the ad voices came apart (two voices 67%
  and 88% inside the repeats, one reading a whole spot alone), at the cost
  of small fragments split off the hosts.
- So far it backs up the repeats it was compared with rather than finding
  new ones. Lone voices need more shows with guest-read ads to judge.

## Intel integrated graphics

faster-whisper runs on CTranslate2, which uses the CPU or Nvidia GPUs only,
so it can't use an Intel iGPU. On an Intel CPU it already uses Intel's
oneDNN/MKL kernels, so `int8` on the CPU is often as quick as an iGPU would
be for the smaller models.

If it isn't quick enough, the way onto the iGPU is OpenVINO
(`openvino-genai`'s `WhisperPipeline`), with a model converted to OpenVINO's
format, and `/dev/dri` passed into the container (`--device /dev/dri`, or the
Intel GPU device plugin's `gpu.intel.com/i915` resource on Kubernetes). The
service's protocol below would stay the same, so pod-block wouldn't change.

## The protocol

`POST /transcribe` with the audio as the body, and `?language=en` if known.
The answer is one JSON object per line:

```
{"waiting": true}                                   another episode is being transcribed
{"working": true}                                   its turn has come
{"language": "en", "duration": 3605.2}              the audio is read
{"start": 1.2, "end": 4.8, "text": " Hello there", "words": [[1.2, 1.5, " Hello"], [1.5, 1.9, " there"]]}
{"step": "speakers"}                                with WHISPER_SPEAKERS: who speaks when
{"progress": 0.4}
{"speakers": [[1.0, 9.5, 0], [9.5, 12.0, 1]], "options": {"threshold": 0.8, ...}}   start, end, speaker
{"done": true}                                      or {"error": "..."}
```

`POST /speakers` does the speakers part alone. Both take `threshold`,
`count`, `min_on` and `min_off` to override the settings for one request, and
`speakers=0` leaves the speakers out.

One episode at a time; others wait, and get their first `{"waiting": true}`
straight away, then one every 15 seconds. Hanging up stops the work at the
next segment. `GET /healthz` answers 200 once the models are loaded. When
stopping (SIGTERM), those waiting see the answer end with no `done`, and new
requests get 503.

`requirements.txt` pins every package: faster-whisper 1.2.1 passes PyAV an
argument PyAV 19 dropped, so `av` stays at 18 until faster-whisper catches up.
