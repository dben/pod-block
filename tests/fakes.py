"""Stand-ins the tests share: feeds and the network they come from, and audio with repeats in it."""

import subprocess

import numpy as np

from podblock.app import following
from podblock.feed import Fetched

URL = "https://example.com/show.rss"
HTML = {"accept": "text/html"}
JSON = {"accept": "application/json"}

SR = 16000
JINGLE, AD, FILLER = 20.0, 18.0, 30.0  # seconds of each part of an episode from make_episodes


def item(n, day=None, guid=None, extra=""):
    day = day or n
    return f"""
    <item>
      <title>Episode {n}</title>
      <guid isPermaLink="false">{guid or f"ep-{n}"}</guid>
      <pubDate>{day:02d} Jan 2026 10:00:00 +0000</pubDate>
      <enclosure url="https://cdn.example.com/{n}.mp3" type="audio/mpeg" length="{n * 1000}"/>
      <description><![CDATA[<p>Notes for <b>episode {n}</b> &amp; more.</p><p>Second line.</p>]]></description>
      <itunes:duration>1:02:03</itunes:duration>
      <itunes:episode>{n}</itunes:episode>
      <podcast:chapters url="https://example.com/{n}.json" type="application/json+chapters"/>
      {extra}
    </item>"""


def rss(*items, title="Example Show", extra_channel=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{title}</title>
    <link>https://example.com/</link>
    <language>en</language>
    <description>A show about &lt;b&gt;examples&lt;/b&gt;.</description>
    <itunes:author>Example Network</itunes:author>
    <itunes:image href="https://example.com/art.jpg"/>
    <itunes:category text="Comedy"><itunes:category text="Improv"/></itunes:category>
    <itunes:explicit>yes</itunes:explicit>
    {extra_channel}
    {"".join(items)}
  </channel>
</rss>""".encode()


class FakeWeb:
    """Stands in for the network: serves whatever XML a test puts at a URL."""

    def __init__(self, monkeypatch):
        self.pages: dict[str, bytes] = {}
        self.requests: list[tuple] = []
        self.moves: dict[str, str] = {}  # url -> where a permanent redirect ends up
        monkeypatch.setattr(following, "get_feed", self.get)

    def get(self, url, etag=None, last_modified=None):
        self.requests.append((url, etag))
        if url not in self.pages:
            raise following.RefreshError("the server answered 404")
        body = self.pages[url]
        tag = f'"{hash(body)}"'
        return Fetched(None if etag == tag else body, tag, None, self.moves.get(url))


def fake_audio(seconds, seed):
    """Noise plus a random melody, which gives the fingerprinter plenty to grab."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = rng.normal(0, 0.02, n)
    note = int(0.12 * SR)
    t = np.arange(note) / SR
    for start in range(0, n - note, note):
        for freq in rng.uniform(150, 3500, size=3):
            out[start:start + note] += 0.1 * np.sin(2 * np.pi * freq * t) * np.hanning(note)
    return out


def make_episodes(root):
    """Five episodes as MP3s, by the address the feed gives each: a shared jingle, then show, a shared ad, more
    show. Then three of another show (11 to 13) with the same jingle and ad. Needs ffmpeg."""
    jingle, ad = fake_audio(JINGLE, 99), fake_audio(AD, 98)
    files = {}
    for n in (*range(1, 6), 11, 12, 13):
        samples = np.concatenate([jingle, fake_audio(FILLER, n), ad, fake_audio(FILLER, 100 + n)])
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        path = root / f"{n}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                        "-c:a", "libmp3lame", "-b:a", "64k", str(path)], input=pcm, check=True)
        files[f"https://cdn.example.com/{n}.mp3"] = path
    return files
