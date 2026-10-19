# pod-block web app

The second half of pod-block: a web app that follows feeds, cuts the repeats
that the detector finds out of the episodes you pick, and serves the cut
episodes as a private podcast feed. This file tracks what we are building,
what is done, and what comes later. Update it as work lands.

## Status

| Area | State |
| --- | --- |
| Repeat detection (`podblock` CLI, `podblock/*.py`) | done; see `README.md` |
| Web app | not started; plan below |

Last updated: 2026-10-19.

## Stack

| Part | Pick | Why |
| --- | --- | --- |
| Web framework | FastAPI on Uvicorn | modern, typed, async where it helps (streaming files), plain sync code where it doesn't; easy to grow a JSON API next to the pages |
| Pages | Jinja2 templates, HTMX for partial updates, Alpine.js for small bits of client state | server-rendered, no Node build step; JS libraries are vendored into `static/` so the container needs no CDN |
| Timeline and player | wavesurfer.js with its regions plugin | waveform, draggable regions, click-to-play at a region |
| Styling | hand-written CSS with custom properties | dark by default, mobile first, no build step |
| App database | SQLite through SQLAlchemy 2, migrations with Alembic | one file on a volume today; Postgres later means a connection string, not a rewrite |
| Detection database | the existing `podblock.sqlite` (`Store`) | unchanged; the app links to it by episode guid |
| Background work | a worker thread in the same process, taking jobs from a table in the app database | survives restarts, needs no broker; can move to its own process or pod later without code changes |
| Audio cutting | ffmpeg | already required by the detector |

## How it fits together

```
browser ──login──▶ management UI (FastAPI + Jinja + HTMX)
                        │  writes jobs
                        ▼
                   jobs table ◀── worker thread: refresh feeds, download,
                        │                         fingerprint, detect, render cut audio,
                        │                         expire old downloads
podcast app ──no login, secret path──▶ /p/<feed-token>/feed.xml
                                       /p/<feed-token>/<episode-token>.mp3
```

- The worker is the only thing that writes to the detection store. The UI
  queues small jobs for block edits, so the store's in-memory hash index
  never goes stale behind its back.
- Cut audio is rendered to a file once, after detection and again after each
  edit. Serving a plain file gives podcast apps what they expect: a fixed
  `Content-Length`, byte ranges for seeking, and an enclosure length that is
  right. Cutting on the fly would break all three.
- The original download is kept beside the cut file for as long as the
  episode is kept, so the timeline can play any part of it and edits can
  re-render without downloading again.

## Storage and deployment

Three paths, each its own volume in Kubernetes:

| Env var | Default | Holds |
| --- | --- | --- |
| `PODBLOCK_DB_DIR` | `./podblock-data` | `app.sqlite`, `podblock.sqlite` (small; back this up) |
| `PODBLOCK_MEDIA_DIR` | `./podblock-data/media` | original and cut episode audio (large, can be lost) |
| `PODBLOCK_CACHE_DIR` | `./podblock-data` | fingerprints and the detector's working audio |

Other settings:

| Env var | What |
| --- | --- |
| `PODBLOCK_PUBLIC_URL` | base URL for links in the public feed, e.g. `https://pods.example.com`; falls back to the request's host |
| `PODBLOCK_API_KEY` | fixed login key; when unset, one is made on first start, stored, and printed to the log |
| `PODBLOCK_WORKERS` | worker threads (default 1) |
| `PODBLOCK_SECRET_KEY` | signs session cookies; made and stored on first start when unset |

Run notes:

- One replica. SQLite needs a block volume (`ReadWriteOnce`); NFS and other
  shared file systems corrupt it. Moving to Postgres lifts this.
- Databases open in WAL mode so the web thread can read while the worker
  writes.
- The image is `python:3.12-slim` plus ffmpeg; the entry point is a new
  `podblock serve` command.
- `/healthz` for liveness and readiness probes.

## Requirements

Ticked items are built. Each has an id so commits and issues can point at it.

### Feeds and episode list

- [ ] **F1** Add a feed by RSS URL. Fetch it, store the show's metadata
  (title, author, description, artwork, link, language, categories, explicit
  flag) and every episode.
- [ ] **F2** Show page with a header built from the feed: artwork, title,
  author, description (collapsed on phones), episode count, links to the
  private feed and settings.
- [ ] **F3** Episode list: artwork, title, date, length, short description,
  and state (`new`, `queued`, `processing`, `ready`, `failed`, `expired`).
  Newest first, paged or loaded on scroll.
- [ ] **F4** Refresh feeds on a schedule (default hourly, per feed setting)
  and by hand.
- [ ] **F5** Store our own copy of each episode's metadata, including the raw
  `<item>` XML, so the episode survives after it leaves the feed.
- [ ] **F6** When an episode leaves the feed, delete it from our list unless
  it was ever downloaded. Downloaded ones stay, marked "no longer in feed".
- [ ] **F7** Archive episodes to hide them from the list, with a toggle to
  show archived ones and a way to unarchive.
  - Select one with a click, a range with shift-click, and add or remove one
    with ctrl/cmd-click.
  - On touch screens, a long press starts select mode; tapping then toggles
    and a second long press selects the range since the last tap.
  - A bar with the selection count and actions (archive, unarchive,
    download) appears while anything is selected.
  - "Archive everything older than this" from one episode's menu.
- [ ] **F8** Remove a feed, with what happens to its downloads spelled out
  before confirming.

### Processing

- [ ] **P1** "Download" on an episode, or on a selection, queues a job: download,
  fingerprint, compare with nearby episodes (fetching and fingerprinting up
  to 4 neighbours from the feed when the store has too few), build the
  report, render the cut audio.
- [ ] **P2** Jobs run in the background; the UI shows progress per episode
  and a jobs page shows the queue, running, done and failed jobs with their
  errors, and lets you retry or cancel.
- [ ] **P3** Jobs left running when the app stops are picked up again on
  start.
- [ ] **P4** Cut audio keeps the source format and about the source bitrate,
  with short fades at each cut so joins don't click.
- [ ] **P5** Per-podcast setting for which section types are cut (default:
  everything except `show` blocks).

### Timeline editor

- [ ] **T1** For a ready episode, a timeline of the whole episode with each
  detected section drawn on it, coloured by type, plus a list of the same
  sections for small screens.
- [ ] **T2** Play the original audio from any point; tap a section to play
  from a few seconds before it.
- [ ] **T3** Mark a section as:
  - **intro**: cut everything up to its end;
  - **outro**: cut everything from its start;
  - **ignored**: keep it (a false positive);
  - back to **auto**.
- [ ] **T4** Add a section by hand: drag on the timeline or type start and
  end times, then nudge the edges. Choose whether it is only cut here or also
  saved as a block so later episodes catch it.
- [ ] **T5** Preview the result: play across a cut to hear the join.
- [ ] **T6** Saving queues a re-render; the public feed keeps serving the old
  cut file until the new one is done.

### Login

- [ ] **A1** Every management page and API route needs a login. Round one:
  one API key, from `PODBLOCK_API_KEY` or made on first start and printed to
  the log. A login page takes the key and sets a signed, HTTP-only session
  cookie. A `podblock api-key --reset` command makes a new one.
- [ ] **A2** Public feed and audio routes need no login.

### Private feed and streaming

- [ ] **S1** Each podcast gets a random token (128 bits, URL-safe). Its feed
  is at `/p/<token>/feed.xml` and lists only episodes with cut audio on
  disk.
- [ ] **S2** The feed keeps the show's channel metadata and each episode's
  `<item>` as the source had them, changing only the enclosure (URL, length,
  type) and `itunes:duration`. Chapters and transcripts would be out of step
  with the cut audio, so they are dropped until we can shift them (see
  future features).
- [ ] **S3** Each episode's audio is at `/p/<token>/<episode-token>.<ext>`,
  with byte-range support, `HEAD`, `ETag` and caching headers.
- [ ] **S4** Make a new token for a podcast, which breaks the old feed URL.
- [ ] **S5** Unknown tokens get a plain 404 that doesn't say whether the
  podcast exists.
- [ ] **S6** Copy-link and QR code for the private feed on the show page.

### Retention

- [ ] **R1** Downloaded episodes are deleted after a set time (default 7
  days, set per podcast). Audio goes; the episode's metadata and report stay,
  and it shows as `expired` and can be downloaded again while the source
  audio is still online.
- [ ] **R2** A daily job does the deleting and also runs the detector's
  `prune` with settings that fit the app.
- [ ] **R3** Show on each ready episode when it will be deleted.

### Look and feel

- [ ] **U1** Dark theme by default, built on CSS custom properties so a light
  theme can follow.
- [ ] **U2** Mobile first: a single column and bottom navigation on phones,
  touch targets of at least 44 px, no hover-only controls.
- [ ] **U3** Pages work without JavaScript where it costs little (lists,
  forms); the timeline needs it.

## Data model (draft)

App database (`app.sqlite`):

```
settings(key, value)                      -- api key hash, secret key, schema version
podcasts(id, feed_url, title, author, description, image_url, link,
         channel_xml,                     -- raw <channel> minus items, for the public feed
         public_token, retention_days, refresh_minutes, cut_types,
         store_podcast_id,                -- podcasts.id in podblock.sqlite
         last_refreshed_at, last_error, created_at)
episodes(id, podcast_id, guid, title, description, pub_date, duration,
         image_url, audio_url, audio_type, audio_length,
         item_xml,                        -- raw <item>, for the public feed
         in_feed, archived, ever_downloaded,
         state, public_token,
         original_path, cut_path, cut_duration, cut_length,
         processed_at, expires_at, store_episode_id)
edits(id, episode_id, kind, start, end,   -- kind: intro | outro | ignore | cut
      segment_id, save_as_block, created_at)
jobs(id, kind, episode_id, podcast_id, state, progress, error,
     attempts, created_at, started_at, finished_at)
```

## Routes (draft)

| Route | Login | What |
| --- | --- | --- |
| `GET /` | yes | podcasts |
| `POST /podcasts` | yes | add a feed |
| `GET /podcasts/{id}` | yes | show header and episode list |
| `POST /podcasts/{id}/refresh` | yes | refresh now |
| `POST /episodes/bulk` | yes | archive, unarchive, download a selection |
| `GET /episodes/{id}` | yes | timeline editor |
| `POST /episodes/{id}/edits` | yes | save timeline edits |
| `GET /episodes/{id}/original` | yes | original audio for the timeline |
| `GET /jobs` | yes | job queue |
| `GET /login`, `POST /login`, `POST /logout` | no | session |
| `GET /p/{token}/feed.xml` | no | private feed |
| `GET /p/{token}/{episode_token}.{ext}` | no | cut audio |
| `GET /healthz` | no | probe |

## Milestones

1. **Skeleton**: `podblock/web/` package, settings from env vars, app
   database and migrations, API-key login, base layout and theme,
   `podblock serve`, Dockerfile.
2. **Feeds**: add, refresh, show header, episode list, prune, archive with
   multi-select.
3. **Processing**: jobs table and worker, download, detect, render cut
   audio, jobs page.
4. **Private feed**: tokens, feed XML, audio with byte ranges.
5. **Retention**: expiry job and settings.
6. **Timeline editor**: waveform, sections, marks, manual sections,
   re-render.
7. **Deploy**: Kubernetes manifests (Deployment, PVCs, Service, Ingress
   example), health checks, backup notes.

## Open questions

1. When you mark a block intro, outro or ignored on one episode, should that
   change the block itself (so every episode of the show follows, past and
   future, as `podblock type` does now) or only that episode?
2. Which detected sections are cut by default? Plan: all of them except
   `show` blocks, with a per-podcast setting.
3. Detection needs other episodes of the show to compare with. OK to fetch
   and fingerprint up to 4 neighbours per download (their audio deleted
   afterwards)?
4. Does the week count from when the episode was processed, or from its
   publish date? Do you want a "keep" pin that stops one from expiring?
5. One user (or one household) with one private feed per podcast, or will
   several people need their own feeds and logins?
6. Should new episodes download on their own (per podcast setting) in the
   first round, or wait for later?

## Future features

- Username and password login, then several users, each with their own
  podcasts and private feeds.
- Download new episodes on their own, per podcast.
- Shift chapter and transcript timestamps to match the cut audio, and add
  chapters marking where cuts were made.
- OPML import and export.
- One combined private feed across podcasts.
- Postgres, and the worker as its own Deployment, so the web side can run
  more than one replica.
- Push or webhook notice when an episode is ready.
- A "this was a repeat" button in the player that sends the spot back as a
  manual section.
- Stats: time cut per episode and per podcast.
- Helm chart.

## Changelog

- 2026-10-19: plan written.
