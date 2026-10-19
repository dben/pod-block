# pod-block

Finds audio that repeats across podcast episodes (intros, outros, ad reads,
promos, theme music, and the short bumpers around ad breaks) and keeps it in
one database for every feed, so new episodes, of any show, can be checked
against it. For a given episode it lists every repeated section: known
blocks, ad breaks marked by bumpers, and intros and outros stretched to the
start and end.

## Quick start

Needs Python 3.10+ and `ffmpeg` on the `PATH`.

```sh
pip install -e .
FEED=https://example.com/podcast.rss  # any podcast RSS feed

podblock analyze "$FEED" -n 5         # learn repeats and bumpers from 5 episodes
podblock list                         # what was found
podblock report --feed "$FEED"        # repeated sections in the newest episode
```

## Commands

| Command | What it does |
| --- | --- |
| `analyze SOURCE... [-n 5] [--podcast NAME]` | Download and fingerprint the newest *n* episodes of each feed (or local files), compare each with the episodes closest to it (see "Comparing"), and store new blocks. Episodes already fingerprinted are skipped. |
| `report TARGET` | List the repeated sections in one episode. `TARGET` is a stored episode id, an audio file or an audio URL; or use `--feed URL [--index N]` (0 = newest). A feed or stored episode is first compared with the episodes closest to it, and the report is saved. |
| `list [--podcast P] [--type T]` | Stored blocks, how many episodes each turns up in, and their types. |
| `podcasts` | Podcasts in the store. |
| `podcast P [--outro-bed]` | Show or change a podcast's settings (see "Outros read over music"). |
| `breaks [EPISODE_ID...]` | Ad breaks found in stored episodes. |
| `show ID` | Where a block occurs. |
| `type ID TYPE` | Set a block's type by hand (see below). `auto` hands it back to automatic tagging. |
| `label ID TEXT` | Give a block a name. |
| `export ID [-o file.mp3]` | Cut a block out of its source episode, to listen to it. |
| `delete ID` | Remove a block. |
| `prune` | Clear out old audio, fingerprints and blocks (see "Keeping it small"). |
| `serve [--host H] [--port P]` | Run the web app (see "Web app"). |
| `api-key` | Make a new login key for the web app, logging everyone out. |

### `report` options

- `--fill-gaps 30`: take in up to 30 s of unique audio when it sits between
  two repeated parts, so a short gap between ads doesn't split a break.
- `--neighbors 3`: first fetch the 3 nearest episodes in the feed (newer and
  older), compare them with this one and store any new blocks. Good for
  catching ads that only ran for a week or two.
- `--podcast P`: for a file or URL, which podcast's `intro`, `outro` and
  `show` blocks apply.
- `--outro-bed`: look for an outro read over a music bed even when the
  podcast's setting is off.
- `--detail`: list the parts that make up each section.
- `--cached`: print the saved report for a stored episode without redoing it.
- `--compare-same N` / `--compare-other N`: size of the comparison set.
- `--json`: machine-readable output:

```json
{"episode_id": 3, "title": "...", "duration": 5593.2, "repeated_sec": 912.4,
 "sections": [{"start": 0.0, "end": 193.2, "length": 193.2,
               "types": ["intro", "repeated"], "blocks": [12, 47],
               "unique_sec": 0.0, "parts": [{"start": 0.0, "end": 94.3, "type": "intro", "block": 47}]}]}
```

## Web app

A web app to manage it all is under way; `WEBAPP.md` has the plan and its
status. So far you can log in, add feeds, browse and search their episodes,
and archive them a selection at a time.

```sh
pip install -e '.[web]'
podblock serve                        # http://127.0.0.1:8000; prints a login key on first start
```

Or in a container, with the database and the audio on their own volumes:

```sh
docker build -t pod-block .
docker run -p 8000:8000 -v pb-data:/data -v pb-media:/media pod-block
```

## Block types

| Type | Set by | Checked against | In a report |
| --- | --- | --- | --- |
| `repeated` | default | every podcast | its own span |
| `intro` | hand | its podcast only | the first one stretches back to 0:00 |
| `outro` | hand | its podcast only | the last one stretches to the end (patron names, trailers) |
| `show` | hand | its podcast only | its own span |
| `bumper-in` / `bumper-out` / `bumper` | automatic, or hand | every podcast | marks an ad break: what plays between a bumper-in and the next bumper-out is one section, even when the ads are new. The bumpers themselves are left out, since hosts often talk over them |
| `ignored` | hand | nowhere | a false positive: never reported, but kept so the same audio isn't taken up as a new block |

A block's podcast is the one it was first found in. Automatic tagging never
overwrites a type set by hand.

## Outros read over music

Some shows end with minutes of talk over a music bed: patron names, plugs,
announcements. The talk changes every week and buries the music, so the bed
seldom matches between episodes and no block covers it. It still stands out
in the loudness floor, the level of the quietest moments: during the show it
swings as people pause and talk over each other, while under a steady bed it
holds still. With the setting on, a report runs the steady stretch at the end
of each episode as `outro`, through to the end.

```sh
podblock podcast "My Show" --outro-bed
podblock report --feed https://example.com/my-show.rss
```

On four episodes of one show it found outros of 6 to 8.5 minutes; on the two
given by hand it started at 1:02:19 (given: 1:02:19) and 1:06:31 (given:
around 1:07:32). It found none in the eight episodes of other shows tried.
A loudness curve is stored with each fingerprint, so this needs no audio.

## How it works

1. **Fingerprint.** Each episode is decoded with ffmpeg and turned into a
   spectrogram. The strongest peaks are kept, each is paired with a few peaks
   just after it, and each pair becomes a 32-bit hash stamped with its time.
   This survives re-encoding, volume changes and moderate noise.
   - `--high-band` (default on) decodes at 16 kHz and fingerprints 4-8 kHz
     as a band of its own. Speech sits mostly below 4 kHz, so music played
     under a host's voice often only shows up there. On the test episodes
     this nearly tripled how much of a music bed under speech was found.
   - `--split-bands` (default off) picks peaks within several bands
     separately. It found under a second more on the same test, for 8% more
     hashes.
   - A store keeps the setting it was created with; fingerprints from
     different settings never match.
2. **Compare.** Each new episode is compared with its comparison set (see
   "Comparing"). Matching hashes from the same audio all sit at one time
   offset. Runs of densely matched seconds at least
   `--min-length` (1.5 s) long are shared audio. Runs under 10 s must turn up
   at least 3 times to be kept.
3. **Store.** Each shared run becomes a block, unless stored blocks already
   cover it. Long blocks are cut where other episodes match only part of them
   (an ad break splits into single ads), and slivers that always play inside
   a longer block are dropped.
4. **Bumpers.** Matches within 8 s of each other form a group. A block whose
   matches are short (under 10 s, and no longer than 20 s stored) and that
   keeps opening groups becomes a `bumper-in`; one that keeps closing them a
   `bumper-out`. Tags are worked out afresh on each run until a bumper has
   sat at a break edge 6 times in 3 episodes; it is then confirmed and keeps
   its tag (`list` shows `confirmed`; `type ID auto` undoes it).
5. **Breaks.** A break runs from a bumper-in to the next bumper-out (within
   10 minutes), even when the ads between them are all new. Some shows play
   one sting on both sides of a break (`bumper`); a pair of those only counts
   when known ads play between them, so the pairing can't slip out of step.
   A lone bumper-in or bumper-out still marks one end, and reaches over up to
   90 s of new ads to known ones. Shows without bumpers get breaks from 60 s
   or more of known blocks back to back.

## Comparing

A new episode is compared with the 4 episodes of the same show closest to it
in publish date, not the newest ones: people hop around a back catalogue, and
the ads that ran when an episode came out sit in its neighbours. It is also
compared with the closest episode of each other show, if published within 14
days, to catch network ads. Only those episodes are rescanned afterwards, so
a request costs the same however big the store grows. `report --neighbors K`
first fetches the K episodes next to the target in its feed, so they can be
in the set.

## Keeping it small

`podblock prune` clears out what is no longer needed:

| Option | Default | What goes |
| --- | --- | --- |
| `--audio-days` | 0 | downloaded audio; only `export` needs it (local files you gave are never touched) |
| `--fingerprint-days` | 30 | fingerprints; those episodes are fetched again if asked for |
| `--block-days` | 30 | blocks no processed episode has played in that long, unless typed by hand |
| `--bumper-days` | 365 | bumpers not heard in that long; a show's bumpers rarely change, and they mark breaks even when every ad is new |

Episode rows, saved reports and the record of where blocks played all stay
after an episode's fingerprint goes; each report is about 1 KB. When a block
is split, where it played is carried over to the pieces by its timings, so
fingerprints only need to live as long as you want episodes on hand for
comparing (a week is fine).

Measured on one CPU core:

| | |
| --- | --- |
| Fingerprinting | about 30 s per hour of audio, about 0.3 GB of memory whatever the length |
| Fingerprint file | about 2 MB per hour of audio |
| Stored block | about 60 KB per minute of block (was 380 KB as one row per hash) |
| Adding one 1-hour episode (download aside) | about 6 s after fingerprinting |
| Report on a stored episode | about 1 s; 0.6 s with `--cached` |

## Data

Everything lives in `./podblock-data` (change with `--data-dir`):

| Path | What |
| --- | --- |
| `podblock.sqlite` | podcasts, episodes, blocks with their hashes, occurrences, saved reports, settings |
| `fingerprints/*.npz` | full fingerprint of each episode, so comparisons never decode again |
| `audio/` | downloaded episodes (`--delete-audio` drops them after fingerprinting; `export` then can't cut their blocks) |

```
settings(key, value)                         -- schema version, fingerprint setting
podcasts(id, name, feed_url, outro_bed)
episodes(id, guid, podcast_id, title, pub_date, audio_url, audio_path, fingerprint_path, duration)
segments(id, source_episode_id, podcast_id, source_start, source_end, label, type, type_manual,
         hashes, times, created_at, last_seen)  -- hashes: packed uint32; times: packed int32,
                                                -- 32 ms frames from the block start
occurrences(segment_id, episode_id, start, end, segment_start, hits)
compared_pairs(a_id, b_id)
reports(episode_id, created_at, report)      -- the JSON `report --json` prints
users(id, username, password_hash, is_admin, created_at)  -- web app logins
user_episodes(user_id, episode_id, archived, kept)        -- web app, per user
```

The web app adds its own columns to `podcasts` and `episodes` (feed metadata,
raw XML); `WEBAPP.md` lists them.

## Tests

```sh
pip install -e '.[test]'
pytest
```
