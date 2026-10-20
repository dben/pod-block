FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/dben/pod-block \
      org.opencontainers.image.description="Podcasts with the repeats cut out" \
      org.opencontainers.image.licenses=MIT

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv only exports the versions uv.lock pins, for pip to install; the image doesn't keep it.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY podblock ./podblock
RUN uv export --frozen --extra web --no-emit-project --no-hashes --quiet -o /tmp/pins.txt \
    && pip install --no-cache-dir -c /tmp/pins.txt '.[web]' \
    && rm /tmp/pins.txt /usr/local/bin/uv

RUN groupadd --system --gid 1000 podblock \
    && useradd --system --uid 1000 --gid 1000 --home /data podblock \
    && mkdir -p /data /media && chown podblock:podblock /data /media
USER podblock

# /data: the database and fingerprints (small; back it up). /media: episode audio (large).
ENV PODBLOCK_DATA_DIR=/data PODBLOCK_MEDIA_DIR=/media PYTHONUNBUFFERED=1
VOLUME ["/data", "/media"]
EXPOSE 8000
CMD ["podblock", "serve", "--host", "0.0.0.0", "--port", "8000"]
