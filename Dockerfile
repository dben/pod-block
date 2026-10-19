FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/dben/pod-block \
      org.opencontainers.image.description="Podcasts with the repeats cut out" \
      org.opencontainers.image.licenses=MIT

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY podblock ./podblock
RUN pip install --no-cache-dir '.[web]'

RUN groupadd --system --gid 1000 podblock \
    && useradd --system --uid 1000 --gid 1000 --home /data podblock \
    && mkdir -p /data /media && chown podblock:podblock /data /media
USER podblock

# /data: the database and fingerprints (small; back it up). /media: episode audio (large).
ENV PODBLOCK_DATA_DIR=/data PODBLOCK_MEDIA_DIR=/media PYTHONUNBUFFERED=1
VOLUME ["/data", "/media"]
EXPOSE 8000
CMD ["podblock", "serve", "--host", "0.0.0.0", "--port", "8000"]
