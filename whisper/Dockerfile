# pod-block's transcription service: faster-whisper on the CPU. See whisper/README.md.
FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/dben/pod-block \
      org.opencontainers.image.description="Captions for pod-block, by faster-whisper" \
      org.opencontainers.image.licenses=MIT

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-deps -r requirements.txt
COPY server.py ./

RUN groupadd --system --gid 1000 whisper \
    && useradd --system --uid 1000 --gid 1000 --home /models whisper \
    && mkdir -p /models && chown whisper:whisper /models
USER whisper

# /models: the downloaded model (small: about 0.5 GB). Keep it on a volume so a restart doesn't fetch it again.
ENV HF_HOME=/models PYTHONUNBUFFERED=1 WHISPER_MODEL=small WHISPER_COMPUTE_TYPE=int8
VOLUME ["/models"]
EXPOSE 9000
CMD ["python", "server.py"]
