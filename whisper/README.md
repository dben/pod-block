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

- Captions jobs have their own worker thread, so a long transcription doesn't
  hold up downloads. They show on the Jobs page as "Captions: *episode*".
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

Then uncomment `PODBLOCK_WHISPER_URL` in `deployment/kustomization.yaml` and
apply that too. Set `WHISPER_THREADS` in `deployment/whisper/kustomization.yaml`
to the cores it may use.

## Settings

| Variable | Default | |
| --- | --- | --- |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3-turbo`, or an `.en` model (`small.en`) for English-only shows |
| `WHISPER_COMPUTE_TYPE` | `int8` | quickest on a CPU; `float32` is a little more accurate and much slower |
| `WHISPER_THREADS` | 4 | CPU threads |
| `WHISPER_BEAM_SIZE` | 5 | 1 is about twice as quick, with a few more mistakes |
| `WHISPER_DEVICE` | `cpu` | `cuda` for an Nvidia GPU (needs a CUDA base image) |
| `WHISPER_PORT` | 9000 | |

### Speed

On a CPU, expect `small` to take somewhere between a third of an episode's
length and its full length, depending on the cores; `base` is about three
times quicker and `tiny` quicker again, with more mistakes in names and
crosstalk. Try `small` first, look at how long a job took on the Jobs page,
and move down a size if it can't keep up with the shows you follow.

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
{"language": "en", "duration": 3605.2}              the audio is read
{"start": 1.2, "end": 4.8, "text": " Hello there", "words": [[1.2, 1.5, " Hello"], [1.5, 1.9, " there"]]}
{"done": true}                                      or {"error": "..."}
```

One episode at a time; others wait. Hanging up stops a transcription at the
next segment. `GET /healthz` answers 200 once the model is loaded.

`requirements.txt` pins every package: faster-whisper 1.2.1 passes PyAV an
argument PyAV 19 dropped, so `av` stays at 18 until faster-whisper catches up.
