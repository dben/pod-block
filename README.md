# pod-block

Finds audio that repeats across podcast episodes (intros, outros, ad reads,
promos, theme music) and keeps it in a database so new episodes can be checked
against it.

## How it works

1. **Fetch.** Reads the RSS feed and downloads the newest *n* episodes.
2. **Fingerprint.** Decodes each episode to 8 kHz mono with ffmpeg, builds a
   spectrogram, and keeps only its strongest peaks. Each peak is paired with a
   few peaks just after it, and each pair becomes a 32-bit hash of
   (frequency, frequency gap, time gap), stamped with the time it starts at.
   This is the same idea Shazam uses. The hashes survive re-encoding, volume
   changes and moderate noise, and an hour of audio yields a few hundred
   thousand of them.
3. **Compare.** For each pair of episodes, look up which hashes they share.
   When both contain the same stretch of audio, the matching hashes all sit at
   one fixed time offset. The matcher counts hits per offset in one-second
   bins and reports runs of densely hit bins at least `--min-length` seconds
   long (10 s by default; time resolution is about one second).
4. **Store.** Each shared run becomes a *block*: its hashes go into SQLite,
   along with where it came from. Before a run is stored it is checked against
   the blocks already known, and only the parts not yet covered are added, so
   an ad found in five episodes is stored once.
5. **Split.** A block first found as one long run, such as a five-minute ad
   break, often turns up elsewhere only in parts (one ad from that break).
   The points where those partial matches start and stop are cut points, so
   the break gets split into single-ad blocks.
6. **Scan.** Any audio file can be checked against the stored blocks. Every
   episode in the store is rescanned after new blocks are added, so the
   `occurrences` table always says where each block appears.

## Setup

Needs Python 3.10+ and `ffmpeg` on the `PATH`.

```sh
pip install -e .
```

## Use

```sh
FEED=https://example.com/podcast.rss

# Download, fingerprint and compare the 5 newest episodes.
podblock analyze "$FEED" -n 5

# Run it again later: new episodes are compared with each other and with
# everything analyzed before. Episodes already fingerprinted are skipped.
podblock analyze "$FEED" -n 5

# Local files work too, and can be mixed with feeds.
podblock analyze episode1.mp3 episode2.mp3

# What has been found, and where each block occurs.
podblock list
podblock show 3

# Listen to a block, then name it.
podblock export 3 -o block3.mp3
podblock label 3 "ad: mattress"

# Check a new file or URL against the store without changing it.
podblock scan new-episode.mp3
# ...or add it to the store and look for new blocks as well.
podblock scan new-episode.mp3 --record
```

Everything lives in `./podblock-data` (change with `--data-dir`):

| Path | What |
| --- | --- |
| `podblock.sqlite` | episodes, blocks, block hashes, occurrences |
| `fingerprints/*.npz` | full fingerprint for each episode, so comparisons never redecode |
| `audio/` | downloaded episodes (pass `--delete-audio` to drop them after fingerprinting; `export` then stops working for their blocks) |

### Tuning

- `--min-length` (default 10 s): shortest block to report. Raise it to 30 to
  keep only long blocks.
- `--min-hits` (default 3): matching hashes needed per second. Raise it if
  you see false matches, lower it for heavily processed audio.

## Database

```
episodes(id, guid, feed_url, title, pub_date, audio_url, audio_path, fingerprint_path, duration)
segments(id, source_episode_id, source_start, source_end, label)
segment_hashes(segment_id, hash, t)          -- t in 32 ms frames from the block start
occurrences(segment_id, episode_id, start, end, segment_start, hits)
compared_pairs(a_id, b_id)                   -- which episode pairs are done
```

## Tests

```sh
pip install -e '.[test]'
pytest
```
