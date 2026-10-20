# pod-block command line

The `podblock` command runs the detector on its own: point it at feeds or
audio files, see what repeats, and change what it found, with no web app.
It also runs the web app (`podblock serve`) and has a few commands for
looking after it. `README.md` explains what the detector finds and how;
this file lists the commands.

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
| `analyze SOURCE... [-n 5] [--podcast NAME]` | Download and fingerprint the newest *n* episodes of each feed (or local files), compare each with the episodes closest to it (see "Comparing" in `README.md`), and store new blocks. Episodes already fingerprinted are skipped. |
| `report TARGET` | List the repeated sections in one episode. `TARGET` is a stored episode id, an audio file or an audio URL; or use `--feed URL [--index N]` (0 = newest). A feed or stored episode is first compared with the episodes closest to it, and the report is saved. |
| `list [--podcast P] [--type T]` | Stored blocks, how many episodes each turns up in, and their types. |
| `podcasts` | Podcasts in the store. |
| `podcast P [--outro-bed]` | Show or change a podcast's settings (see "Outros read over music" below). |
| `breaks [EPISODE_ID...]` | Ad breaks found in stored episodes. |
| `show ID` | Where a block occurs. |
| `type ID TYPE` | Set a block's type by hand (see "Block types" in `README.md`). `auto` hands it back to automatic tagging. |
| `label ID TEXT` | Give a block a name. |
| `export ID [-o file.mp3]` | Cut a block out of its source episode, to listen to it. |
| `delete ID` | Remove a block. |
| `prune` | Clear out old audio, fingerprints and blocks (see "Keeping it small" below). |
| `serve [--host H] [--port P]` | Run the web app (see `WEBAPP.md`). |
| `api-key` | Make a new login key for the web app, logging everyone out. |
| `backup DEST [--force]` | Copy the database to `DEST`, safely, even while the web app runs. |

## Options for every command

- `--data-dir DIR`: where the database, fingerprints and audio live
  (default `$PODBLOCK_DATA_DIR`, else `./podblock-data`). Point it at the web
  app's data folder to work on the same store.
- `--high-band` / `--no-high-band` (default on): also fingerprint 4-8 kHz as
  a band of its own, where music under speech shows best.
- `--split-bands` / `--no-split-bands` (default off): pick peaks within
  several bands separately.
- `-v`: say more about what it is doing.

The two band settings only count when a store is created; it keeps them from
then on, since fingerprints made with different settings never match.

`analyze` and `report` also take:

- `--min-length 1.5`: the shortest run of matching audio, in seconds, that
  counts as shared.
- `--min-hits N`: how many matching hashes a second it takes to call audio
  shared.
- `--compare-same N` / `--compare-other N`: how many episodes of the same
  show (default 4), and of each other show (default 1), to compare with.

`analyze` alone takes:

- `--delete-audio`: drop downloads once fingerprinted (`export` then can't
  cut their blocks).
- `--no-history`: compare only the episodes named in this run, not ones
  analyzed before.

## `report` options

- `--min-section 15`: leave out sections shorter than this (default 15 s),
  unless they are ad breaks, intros, outros, show blocks or typed by hand.
  `0` lists them all.
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
- `--json`: machine-readable output:

```json
{"episode_id": 3, "title": "...", "duration": 5593.2, "repeated_sec": 912.4,
 "sections": [{"start": 0.0, "end": 193.2, "length": 193.2,
               "types": ["intro", "repeated"], "blocks": [12, 47],
               "unique_sec": 0.0, "parts": [{"start": 0.0, "end": 94.3, "type": "intro", "block": 47}]}]}
```

A report on a stored episode takes about 1 s, or 0.6 s with `--cached`.

## Outros read over music

`README.md` explains how pod-block finds an outro read over a music bed. The
setting is per podcast, off by default, and only the command line changes
it. The web app follows it once set.

```sh
podblock podcast "My Show" --outro-bed
podblock report --feed https://example.com/my-show.rss
```

## Keeping it small

`podblock prune` clears out what is no longer needed. The web app runs it
once a day on its own; run it by hand when you use the command line alone.

| Option | Default | What goes |
| --- | --- | --- |
| `--audio-days` | 0 | downloaded audio; only `export` needs it (local files you gave are never touched) |
| `--fingerprint-days` | 30 | fingerprints; those episodes are fetched again if asked for |
| `--block-days` | 30 | blocks no processed episode has played in that long, unless typed by hand |
| `--bumper-days` | 365 | bumpers not heard in that long; a show's bumpers rarely change, and they mark breaks even when every ad is new |

`--dry-run` says what would go without deleting it.

Episode rows, saved reports and the record of where blocks played all stay
after an episode's fingerprint goes; each report is about 1 KB. When a block
is split, where it played is carried over to the pieces by its timings, so
fingerprints only need to live as long as you want episodes on hand for
comparing (a week is fine).
