"""Makes up three podcasts for the README screenshots: synthetic audio, RSS feeds and artwork.

Each episode also gets the script of what is said in it, timed to its audio, under SITE_DIR/script/ by the
file's SHA-256, for the stand-in transcriber (transcriber.py).

usage: feeds.py SITE_DIR
"""

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SR = 16000
BASE = "http://127.0.0.1:8765"
NOTES = "Plus listener mail and a few things we got wrong last week."
SITE = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
CUE = 4.2  # seconds from one line of the script to the next

TALK = [
    ("Host", "Okay, so last week we promised to talk about cables."),
    ("Guest", "And then we didn't, because the printer broke again."),
    ("Host", "I still think the printer knows when you're in a hurry."),
    ("Guest", "It definitely does. Anyway, first up this week."),
    ("Host", "Someone wrote in about the drawer of old chargers."),
    ("Guest", "Everyone has that drawer. Everyone."),
    ("Host", "Mine has a charger for a phone I gave away years ago."),
    ("Guest", "Keep it. You never know."),
]
AD_READ = [  # put in as each download is made, so the feed's transcript doesn't have it
    ("Announcer", "This episode is brought to you by Example Mattress Company."),
    ("Announcer", "Sleep on it for a hundred nights, and if you don't love it, send it back."),
    ("Announcer", "Go to example dot com slash podcast for twenty percent off your first order."),
    ("Announcer", "That's example dot com slash podcast. Example Mattress, rest made simple."),
]
CREDITS = [
    ("Host", "That's the show. This was a production of Podblock Studios."),
    ("Host", "Thanks to everyone who supports us, and to our producer for fixing the audio again."),
    ("Guest", "Write in with your questions and we might read them out next week."),
    ("Host", "You can find us wherever you get your podcasts, and on the website."),
    ("Guest", "Next week: the great keyboard debate. Bring snacks."),
    ("Host", "Bye for now."),
]


def tones(seconds, seed, note=0.12, amp=0.1):
    """A random melody over light noise: plenty of peaks for the fingerprinter."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = rng.normal(0, 0.01, n)
    k = int(note * SR)
    t = np.arange(k) / SR
    win = np.hanning(k)
    for start in range(0, n - k, k):
        for freq in rng.uniform(150, 3500, size=3):
            out[start:start + k] += amp * np.sin(2 * np.pi * freq * t) * win
    return out


def music(seconds, seed):
    """Steady and loud, like a jingle or an ad's bed."""
    return tones(seconds, seed, note=0.15, amp=0.16)


def talk(seconds, seed):
    """Tones gated into words and pauses, so the waveform looks like speech."""
    rng = np.random.default_rng(seed + 50_000)
    out = tones(seconds, seed, note=0.09, amp=0.12)
    env = np.zeros(len(out))
    i = 0
    while i < len(env):
        word = min(int(rng.uniform(0.15, 0.6) * SR), len(env) - i)
        env[i:i + word] = rng.uniform(0.35, 1.0) * np.hanning(word) ** 0.3
        i += word + int(rng.choice([rng.uniform(0.03, 0.1), rng.uniform(0.25, 0.7)], p=[0.8, 0.2]) * SR)
    return out * env + rng.normal(0, 0.004, len(out))


def write_mp3(samples, path):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                    "-c:a", "libmp3lame", "-b:a", "48k", str(path)], input=pcm, check=True)


def art(path, name, top, bottom):
    size = 600
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            f = (x * 0.35 + y * 0.65) / size
            px[x, y] = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3))
    d = ImageDraw.Draw(img)
    cx, cy = int(size * 0.72), int(size * 0.28)
    for r in range(30, 900, 46):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255), width=2)
    font = ImageFont.truetype("/usr/share/fonts/opentype/inter/Inter-Bold.otf", 64)
    lines, line = [], ""
    for w in name.split():
        if d.textlength((line + " " + w).strip(), font=font) > size - 80:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    lines.append(line)
    y = size - 50 - 76 * len(lines)
    for ln in lines:
        d.text((40, y), ln, font=font, fill="white")
        y += 76
    img.save(path)


def stamp(seconds):
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


SHOWS = [
    {
        "slug": "placeholder-fm", "title": "Placeholder FM", "author": "Sample Network", "category": "Technology",
        "colors": ((74, 63, 196), (220, 70, 150)),
        "about": "Two hosts talk through the week's gadgets, apps and the odd broken printer. Made-up show for "
                 "pod-block's screenshots.",
        "seed": 1000, "ads": "network", "transcripts": True, "segment_theme": True, "ad_read": True,
        "outro": "phrase",
        "episodes": ["Why is my laptop warm", "The cable drawer, sorted", "Printers, again",
                     "Rewind: the year in screens", "Batteries don't lie", "Five apps we deleted",
                     "Listener mail: chargers", "What's in the box"],
    },
    {
        "slug": "test-pattern", "title": "Test Pattern", "author": "Sample Network", "category": "Science",
        "colors": ((20, 140, 130), (240, 200, 60)),
        "about": "Short, curious episodes about how everyday things work. Made-up show for pod-block's screenshots.",
        "seed": 2000, "ads": "network",
        "episodes": ["How a fridge stays cold", "Why toast lands butter side down", "The bus timetable problem",
                     "Best of: kitchen science", "Where lost socks go", "Static, explained",
                     "The loudest sound", "Paper, folded"],
    },
    {
        "slug": "the-sample-show", "title": "The Sample Show", "author": "Example Media", "category": "Comedy",
        "colors": ((180, 30, 40), (245, 140, 40)),
        "about": "Three friends, one microphone, no plan. Made-up show for pod-block's screenshots.",
        "seed": 3000, "ads": "media", "transcripts": True,
        "episodes": ["We tried to bake bread", "The group chat incident", "Live from the sofa",
                     "Best of 2026, part one", "Is a hot dog a sandwich", "Bonus: outtakes",
                     "The worst haircut", "Our landlord's cat"],
    },
]

NETWORK_ADS = {i: music(30, 500 + i) for i in range(6)}
MEDIA_ADS = {i: music(28, 600 + i) for i in range(5)}


def episode(show, n):
    """The parts of one episode, in order: (kind, audio, script); script is the lines said in it, or None."""
    s = show["seed"]
    rng = np.random.default_rng(s + n)
    pool = NETWORK_ADS if show["ads"] == "network" else MEDIA_ADS
    keys = list(pool)
    a, b, c, d = (keys[(n + k * 2) % len(keys)] for k in range(4))
    bump_in, bump_out = music(4, s + 3), music(4, s + 4)
    second = [pool[c]] + ([music(30, s * 10 + n)] if n % 3 == 0 else []) + [pool[d]]  # a new ad now and then

    def chat(lo, hi, seed):
        return "talk", talk(rng.uniform(lo, hi), seed), TALK

    parts = [chat(12, 18, s + 100 + n), ("intro", music(20, s + 1), None), chat(150, 200, s + 200 + n),
             ("break", np.concatenate([bump_in, pool[a], pool[b], bump_out]), None)]
    if show.get("segment_theme"):
        parts += [chat(110, 140, s + 300 + n), ("theme", music(18, s + 5), None), chat(130, 170, s + 600 + n)]
    else:
        parts += [chat(260, 330, s + 300 + n)]
    parts += [("break", np.concatenate([bump_in, *second, bump_out]), None)]
    if show.get("ad_read"):
        parts += [chat(90, 120, s + 400 + n), ("ad read", talk(34, s + 700 + n), AD_READ),
                  chat(80, 110, s + 800 + n)]
    else:
        parts += [chat(200, 260, s + 400 + n)]
    if show.get("outro") == "phrase":
        parts += [("credits", talk(26, s + 500 + n), CREDITS)]
    else:
        parts += [("outro", music(24, s + 2), None), chat(8, 14, s + 500 + n)]
    return parts


def script(parts):
    """Each line said, as (start, end, speaker, text) in the episode, and whether the feed's transcript has it."""
    lines, pos = [], 0.0
    for kind, audio, said in parts:
        length = len(audio) / SR
        if said:
            t, k = pos + 0.5, 0
            while t + 3.6 <= pos + length:
                who, text = said[k % len(said)]
                lines.append((t, t + 3.6, who, text, kind != "ad read"))
                t, k = t + CUE, k + 1
        pos += length
    return lines


def feed_transcript(lines):
    """WebVTT as the show would publish it: made before the ad read went in, so without it, and timed without it."""
    cues, shift = [], 0.0
    reads = [(a, b) for a, b, _, _, in_feed in lines if not in_feed]
    if reads:
        start, end = reads[0][0] - 0.5, reads[-1][1] + 0.5
    for a, b, who, text, in_feed in lines:
        if not in_feed:
            continue
        shift = end - start if reads and a > start else 0.0
        cues.append(f"{stamp(a - shift)} --> {stamp(b - shift)}\n<v {who}>{text}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"


def heard(lines, duration):
    """What the transcriber would hear: every line, with a time for each word, and who said it."""
    voices = {}
    segments, turns = [], []
    for a, b, who, text, _ in lines:
        words = text.split()
        step = (b - a) / len(words)
        segments.append({"start": round(a, 2), "end": round(b, 2), "text": " " + text,
                         "words": [[round(a + i * step, 2), round(a + (i + 1) * step, 2), " " + w]
                                   for i, w in enumerate(words)]})
        turns.append([round(a, 2), round(b, 2), voices.setdefault(who, len(voices))])
    return {"duration": round(duration, 2), "segments": segments, "speakers": turns}


def build(show):
    out = SITE / show["slug"]
    out.mkdir(parents=True, exist_ok=True)
    (SITE / "script").mkdir(exist_ok=True)
    art(out / "art.png", show["title"], *show["colors"])
    items = []
    newest = datetime(2026, 10, 20, 10, tzinfo=timezone.utc) - timedelta(days=show["seed"] // 1000 - 1)
    count = len(show["episodes"])
    for n, title in enumerate(show["episodes"], 1):
        parts = episode(show, n)
        audio = np.concatenate([p for _, p, _ in parts])
        mp3 = out / f"{n}.mp3"
        write_mp3(audio, mp3)
        duration = len(audio) / SR
        lines = script(parts)
        digest = hashlib.sha256(mp3.read_bytes()).hexdigest()
        (SITE / "script" / f"{digest}.json").write_text(json.dumps(heard(lines, duration)))
        extra = ""
        if show.get("transcripts"):
            (out / f"{n}.vtt").write_text(feed_transcript(lines))
            extra = f'<podcast:transcript url="{BASE}/{show["slug"]}/{n}.vtt" type="text/vtt"/>'
        when = newest - timedelta(days=7 * (count - n))
        kind = "bonus" if title.startswith("Bonus") else "full"
        items.append(f"""
    <item>
      <title>{title}</title>
      <guid isPermaLink="false">{show["slug"]}-{n}</guid>
      <pubDate>{format_datetime(when)}</pubDate>
      <enclosure url="{BASE}/{show["slug"]}/{n}.mp3" type="audio/mpeg" length="{mp3.stat().st_size}"/>
      <description><![CDATA[<p>In this episode: {title[0].lower() + title[1:]}. {NOTES}</p>]]></description>
      <itunes:duration>{int(duration)}</itunes:duration>
      <itunes:episode>{n}</itunes:episode>
      <itunes:episodeType>{kind}</itunes:episodeType>
      {extra}
    </item>""")
        print(show["slug"], n, f"{duration / 60:.1f} min", flush=True)
    items.reverse()
    (out / "feed.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>{show["title"]}</title>
    <link>{BASE}/{show["slug"]}/</link>
    <language>en</language>
    <description>{show["about"]}</description>
    <itunes:author>{show["author"]}</itunes:author>
    <itunes:image href="{BASE}/{show["slug"]}/art.png"/>
    <itunes:category text="{show["category"]}"/>
    <itunes:explicit>no</itunes:explicit>
    {"".join(items)}
  </channel>
</rss>
""")


if __name__ == "__main__":
    for show in SHOWS:
        build(show)
