#!/usr/bin/env bash
# Retakes the README screenshots: makes up three podcasts, puts them through a fresh pod-block, and saves the
# pages to docs/screenshots/. Needs uv, ffmpeg, node and Playwright with Chromium. Takes about 6 minutes.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
work=${1:-$(mktemp -d)}
mkdir -p "$work/site" "$work/data" "$work/shots"
echo "working in $work"

uv run --project "$repo" --with pillow python "$here/feeds.py" "$work/site"

python3 -m http.server 8765 --bind 127.0.0.1 --directory "$work/site" > "$work/site.log" 2>&1 &
site=$!
python3 "$here/transcriber.py" "$work/site" > "$work/transcriber.log" 2>&1 &
transcriber=$!
(cd "$repo" && NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  PODBLOCK_DATA_DIR="$work/data" PODBLOCK_API_KEY=demo-key-for-screenshots PODBLOCK_WORKERS=3 \
  PODBLOCK_PUBLIC_URL=https://pods.example.com PODBLOCK_WHISPER_URL=http://127.0.0.1:9000 \
  exec uv run podblock serve --port 8000) > "$work/app.log" 2>&1 &
app=$!
trap 'kill $site $transcriber $app 2>/dev/null' EXIT
until curl -sf http://127.0.0.1:8000/healthz > /dev/null; do sleep 1; done

uv run --project "$repo" python "$here/seed.py" "$work/data"
(cd "$work" && node "$here/shots.js" "$work/shots")

# 256 colours keep each file to a fraction of the size, with no visible change on these flat pages.
uv run --project "$repo" --with pillow python - "$work/shots" "$repo/docs/screenshots" <<'EOF'
import sys
from pathlib import Path
from PIL import Image
for src in Path(sys.argv[1]).glob("*.png"):
    image = Image.open(src).convert("RGB").quantize(256, Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    image.save(Path(sys.argv[2]) / src.name, optimize=True)
    print(src.name)
EOF
