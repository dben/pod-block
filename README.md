# pod-block

Finds audio that repeats across podcast episodes (intros, outros, ad reads,
promos, theme music, and the short bumpers around ad breaks) and keeps it in a
database so new episodes can be checked against it. From the bumpers and
repeated ads it also marks where each ad break starts and ends.

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
   long (1.5 s by default; edges land within a fraction of a second). Runs
   shorter than 10 s are kept only if they turn up at least 3 times, since a
   pair of short clips can match by chance.
4. **Store.** Each shared run becomes a *block*: its hashes go into SQLite,
   along with where it came from. Before a run is stored it is checked against
   the blocks already known, and only the parts not yet covered are added, so
   an ad found in five episodes is stored once.
5. **Split.** A block first found as one long run, such as a five-minute ad
   break, often turns up elsewhere only in parts (one ad from that break).
   The points where those partial matches start and stop are cut points, so
   the break gets split into single-ad blocks. Short slivers that nearly
   always play inside a longer block are dropped.
6. **Bumpers.** In each episode, matches that sit within 8 s of each other
   form a group, such as a run of ads. A block whose matches are short (under
   10 s) and that keeps opening such groups becomes a `bumper-in`; one that
   keeps closing them becomes a `bumper-out`. Blocks longer than 20 s are
   never bumpers.
7. **Ad breaks.** A break runs from a bumper-in to the next bumper-out (within
   10 minutes), even when the ads between them are new. A bumper found on its
   own still marks one end of a break. Shows without bumpers still get their
   breaks found when 60 s or more of known blocks play back to back.
8. **Scan.** Any audio file can be checked against the stored blocks, which
   also lists its ad breaks. Every episode in the store is rescanned after new
   blocks are added, so the `occurrences` table always says where each block
   appears.

## Setup

Needs Python 3.10+ and `ffmpeg` on the `PATH`.

```sh
pip install -e .
```

## Use

```sh
FEED=https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/e5f91208-cc7e-4726-a312-ae280140ad11/d64f756d-6d5e-4fae-b24f-ae280140ad36/podcast.rss

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

# Ad breaks in the stored episodes (all, or the ids given).
podblock breaks
podblock breaks 1 3

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

- `--min-length` (default 1.5 s): shortest block to report. Raise it to 10
  or 30 to skip bumpers and keep only long blocks.
- `--min-hits` (default 3): matching hashes needed per second. Raise it if
  you see false matches, lower it for heavily processed audio.

## Database

```
episodes(id, guid, feed_url, title, pub_date, audio_url, audio_path, fingerprint_path, duration)
segments(id, source_episode_id, source_start, source_end, label, kind)  -- kind: bumper-in, bumper-out, bumper
segment_hashes(segment_id, hash, t)          -- t in 32 ms frames from the block start
occurrences(segment_id, episode_id, start, end, segment_start, hits)
compared_pairs(a_id, b_id)                   -- which episode pairs are done
```

## Tests

```sh
pip install -e '.[test]'
pytest
```
