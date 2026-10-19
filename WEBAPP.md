# pod-block web app

The second half of pod-block: a web app that follows feeds, cuts the repeats
that the detector finds out of the episodes you pick, and serves the cut
episodes as a private podcast feed. This file tracks what we are building,
what is done, and what comes later. Update it as work lands.

## Status

| Area | State |
| --- | --- |
| Repeat detection (`podblock` CLI, `podblock/*.py`) | done; see `README.md` |
| Milestone 1: skeleton | done: `podblock serve`, API-key login, dark theme, Dockerfile |
| Milestone 2: feeds | done: add and refresh feeds, show header, episode list, archive, remove |
| Milestone 3: processing | done: job queue and worker, download, detect, cut, episode and jobs pages |
| Milestone 4: private feed | next |

Last updated: 2026-10-19.

## Decisions

Answers to the first round of questions, kept here so they don't get lost.

| Topic | Decision |
| --- | --- |
| Database | One database for the detector and the app: the app's tables and columns go in `podblock.sqlite` beside the detector's. If we move off SQLite, both move to the same host together. |
| Marking blocks | Marking a block intro, outro, show or ignored on one episode changes the block itself, for every episode, as `podblock type` does. |
| Block types | `show`: a block checked only against its own podcast's episodes, never other feeds'. `ignored`: a false positive, never reported, so never cut. Both are choices in the timeline. Every section a report lists is cut. |
| Comparison episodes | When a show has fewer than 4 fingerprinted episodes, a download also pulls the episodes published after the chosen one, nearest first, and older ones only when it reaches the newest episode. These are fully processed too: shows often run in parts, so they are likely next anyway. |
| Retention | Counts from when an episode was processed, not its publish date. A keep pin stops the clock. |
| Users | Built for a few users, but round one has one login. Feeds, downloads, processing and blocks are shared. Archived and kept are per user. The private feed is per podcast and shared; per-user feeds come later. |
| Downloads | Manual for now, apart from the comparison episodes above. |
| Keeping it generic | No real feed URLs, show names or audio in the repo, its history or its tests. Real feeds for trying things by hand live in the environment variable `PODBLOCK_TEST_FEEDS` (space-separated), set in the cloud environment's settings. |

## Stack

| Part | Pick | Why |
| --- | --- | --- |
| Web framework | FastAPI on Uvicorn | modern, typed, async where it helps (streaming files), plain sync code where it doesn't; easy to grow a JSON API next to the pages |
| Pages | Jinja2 templates, HTMX for partial updates, Alpine.js for small bits of client state | server-rendered, no Node build step; JS libraries are copied into `podblock/web/static/vendor/` so the container needs no CDN |
| Timeline and player | wavesurfer.js with its regions plugin | waveform, draggable regions, click-to-play at a region |
| Styling | hand-written CSS with custom properties | dark by default, mobile first, no build step |
| Database | `podblock.sqlite`, through `sqlite3` as the detector's `Store` does | one file on a volume; the app's tables are added by numbered steps in `podblock/web/db.py` |
| Background work | worker threads in the same process (`PODBLOCK_WORKERS`, default 1), taking jobs from a table in the database; one more thread refreshes feeds | survives restarts, needs no broker; can move to its own process or pod later without code changes |
| Audio cutting | `podblock/cut.py`: one ffmpeg decodes to raw samples, Python drops the cut stretches and fades each join, a second ffmpeg encodes | flat memory whatever the length, cuts on the exact sample, real fades; one decode and one encode |

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

- The worker is the only thing that writes to the detector's tables. The UI
  queues small jobs for block edits, so the store's in-memory hash index
  never goes stale behind its back.
- Cut audio is rendered to a file once, after detection and again after each
  edit. Serving a plain file gives podcast apps what they expect: a fixed
  `Content-Length`, byte ranges for seeking, and an enclosure length that is
  right. Cutting on the fly would break all three.
- The original download is kept beside the cut file for as long as the
  episode is kept, so the timeline can play any part of it and edits can
  re-render without downloading again.
- The app keeps its audio in its own columns and under the media folder.
  The detector's `prune` deletes whatever `episodes.audio_path` points at,
  so the app never puts its files there.

## Storage and deployment

Two paths, each its own volume in Kubernetes:

| Env var | Default | Holds |
| --- | --- | --- |
| `PODBLOCK_DATA_DIR` | `./podblock-data` | `podblock.sqlite` and fingerprints (small; back this up) |
| `PODBLOCK_MEDIA_DIR` | `$PODBLOCK_DATA_DIR/media` | original and cut episode audio (large, can be lost) |

Other settings:

| Env var | What |
| --- | --- |
| `PODBLOCK_API_KEY` | fixed login key; when unset, one is made on first start, its hash stored, and the key printed to the log once |
| `PODBLOCK_SECRET_KEY` | signs session cookies; made and stored on first start when unset |
| `PODBLOCK_SECURE_COOKIES` | `true` to send the session cookie over HTTPS only; set it behind TLS |
| `PODBLOCK_PUBLIC_URL` | base URL for links in the private feed, e.g. `https://pods.example.com`; falls back to the request's host (not used yet) |
| `PODBLOCK_WORKERS` | threads running jobs (default 1). Fingerprinting takes about 0.3 GB and one core per thread |
| `FORWARDED_ALLOW_IPS` | Uvicorn's own: which proxies to trust for `X-Forwarded-*` headers; set it to the ingress's address range |

Run notes:

- One replica. SQLite needs a block volume (`ReadWriteOnce`); NFS and other
  shared file systems corrupt it.
- The database runs in WAL mode so pages can read while the worker writes.
- The image (`Dockerfile`) is `python:3.12-slim` plus ffmpeg, runs as uid
  1000, and starts `podblock serve --host 0.0.0.0 --port 8000`.
- `/healthz` for liveness and readiness probes.
- `kubectl exec ... -- podblock api-key` makes a new key if the first one is
  lost.

## Requirements

Ticked items are built. Each has an id so commits and issues can point at it.

### Feeds and episode list

- [x] **F1** Add a feed by RSS URL. Fetch it, store the show's metadata
  (title, author, description, artwork, link, language, categories, explicit
  flag) and every episode. Feeds over 64 MB, and pages that aren't RSS, are
  refused with a reason.
- [x] **F2** Show page with a header built from the feed: artwork (also
  blurred behind the header), author, title, episode count, categories,
  description (three lines, then "More"), Refresh, Website, and a menu with
  Remove. It shows when the feed was last checked and the last error.
  - [ ] Link to the private feed and to podcast settings (milestones 4 and 5).
- [x] **F3** Episode list: artwork, title, date, length, season and episode
  number, trailer and bonus badges, two lines of show notes as plain text.
  Newest first, 50 at a time with "Load more". Search by title as you type.
  - [x] Processing state per episode: Queued, what it is doing ("Downloading
    45%", "Fingerprinting", "Cutting"), Ready with the cut length, Failed.
    A row being processed refreshes itself every 3 seconds.
  - [ ] `expired` (milestone 5).
- [x] **F4** Refresh feeds by hand, and in the background when due (every 60
  minutes by default; `podcasts.refresh_minutes`). Refreshes send the last
  ETag and Last-Modified, so an unchanged feed costs one short request.
  - [ ] A page to change the interval per podcast.
- [x] **F5** Store our own copy of each episode's metadata, including the raw
  `<item>` XML (show notes come from it later), so the episode survives after
  it leaves the feed.
- [x] **F6** When an episode leaves the feed, delete it unless it was ever
  downloaded or the detector knows it (fingerprint, block matches or report).
  Those stay, marked "No longer in feed", and lose the mark if they return. A
  refresh that finds no episodes at all deletes nothing.
- [x] **F7** Archive episodes to hide them from your list (per user), with an
  Archived tab and a way to unarchive.
  - Each row's artwork is its checkbox: click it to select, shift-click
    another to select everything in between. While anything is selected, a
    click anywhere on a row toggles it too. Esc clears.
  - On touch screens, tap the artwork, or press and hold a row, to select;
    while selecting, a tap toggles and a second press and hold selects the
    range since the last one.
  - A bar with the count, select-all, Archive (or Unarchive) and Clear rises
    while anything is selected. Archiving takes the rows out without a page
    load, with Undo in the toast.
  - "Archive this and older" in each row's menu.
  - [x] Download in the bar and in each row's menu ("Download and cut";
    "Process again" once ready or failed).
  - [ ] Keep in the bar (milestone 5).
- [x] **F8** Remove a podcast, after a confirmation saying what happens:
  episodes never downloaded are deleted; downloaded ones, and the blocks
  learned from the show, stay (blocks may be ads other shows share). The
  podcast is hidden and no longer refreshed; adding the same feed again
  brings it back.

### Processing

- [x] **P1** "Download" on an episode, or on a selection, queues a job:
  download, fingerprint, compare with nearby episodes, build the report,
  render the cut audio. Episodes already ready or queued are skipped;
  "Process again" redoes a ready one, reusing its download.
- [x] **P2** When the show has fewer than 4 fingerprinted episodes, the job
  takes the episodes after the chosen one until it has 4, then older ones if
  the feed runs out. They are fingerprinted first and compared as one group,
  then each is reported and rendered like a chosen one. If the job fails or
  is cancelled, companions not yet ready go back to rest quietly.
- [x] **P3** Jobs run in the background; each row shows its progress, and
  the jobs page (with a count in the nav) shows running, queued and recent
  jobs with their step, progress bar and errors, and lets you cancel or
  retry. Cancelling stops a running job between steps or mid-download.
- [x] **P4** Jobs left running when the app stops go back in the queue on
  start, unless they were being cancelled.
- [x] **P5** Cut audio keeps the source format (MP3, AAC in M4A, Opus,
  Vorbis; anything else becomes MP3) at the nearest standard bitrate, with a
  40 ms fade either side of each join. Tags and cover art are copied;
  chapters are dropped. Kept stretches under a second between two cuts go
  too.
- [x] **P6** Every section the report lists is cut. Blocks marked `ignored`
  never show up in a report, so they stay.
  Up to 4 seconds of new audio between two repeats is cut with them
  (`processing.FILL_GAPS`), so a cut never leaves a scrap of sound between two
  others.
- [x] **P7** An episode page: status (queued and running ones update
  themselves), a player and a download for the cut file, how much was cut,
  the list of cut sections with a button to play the original from a few
  seconds before each, and the show notes.
- [ ] **P8** Run each job in a child process, so a crash or a memory spike
  in fingerprinting can't take the web side down, and so cancelling can stop
  fingerprinting too (today it stops only between steps).

### Timeline editor

- [ ] **T1** For a ready episode, a timeline of the whole episode with each
  detected section drawn on it, coloured by type, plus a list of the same
  sections for small screens.
- [ ] **T2** Play the original audio from any point; tap a section to play
  from a few seconds before it.
- [ ] **T3** Mark a section's block as:
  - **intro**: cut everything up to its end;
  - **outro**: cut everything from its start;
  - **show**: only matched within this podcast;
  - **ignored**: keep it (a false positive);
  - back to **auto**.

  This changes the block everywhere, so every ready episode it plays in is
  re-rendered.
- [ ] **T4** Add a section by hand: drag on the timeline or type start and
  end times, then nudge the edges. Choose whether it is only cut here or also
  saved as a block so later episodes catch it.
- [ ] **T5** Preview the result: play across a cut to hear the join.
- [ ] **T6** Saving queues a re-render; the private feed keeps serving the
  old cut file until the new one is done.

### Login and users

- [x] **A1** Every management page needs a login. Round one: one API key,
  from `PODBLOCK_API_KEY` or made on first start and printed to the log. The
  login page takes the key and sets a signed, HTTP-only session cookie
  (30 days). `podblock api-key` makes a new key and logs everyone out.
  Scripts can send `Authorization: Bearer <key>` instead.
- [x] **A2** A `users` table from the start; the API key logs in as `admin`,
  so per-user state has an owner.
- [ ] **A3** Public feed and audio routes need no login.

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

- [ ] **R1** Downloaded episodes are deleted a set time after processing
  (default 7 days, set per podcast). Audio goes; the episode's metadata and
  report stay, and it shows as `expired` and can be downloaded again while
  the source audio is still online.
- [ ] **R2** A keep pin (per user) stops an episode from being deleted while
  any user has it pinned. Kept episodes also keep their fingerprint, so
  timeline edits still work on them later.
- [ ] **R3** A daily job does the deleting and also runs the detector's
  `prune` with settings that fit the app.
- [ ] **R4** Show on each ready episode when it will be deleted.

### Look and feel

- [x] **U1** Dark theme by default, built on CSS custom properties so a light
  theme can follow.
- [x] **U2** Mobile first: a bottom tab bar on phones, touch targets of at
  least 44 px, inputs at 16 px so iOS doesn't zoom, no hover-only controls.
- [x] **U3** Pages work without JavaScript where it costs little: adding,
  refreshing, paging, searching, archiving (one at a time or by ticking
  boxes) all work as plain forms and links. The timeline will need it.

## Data model

Built (migrations 1 to 3 in `podblock/web/db.py`):

```
users(id, username, password_hash, is_admin, created_at)
user_episodes(user_id, episode_id, archived, kept)
settings: app_schema, api_key_hash, auth_epoch, secret_key   -- rows beside the detector's own

podcasts  + author, description, image_url, link, language, categories (JSON), explicit,
            channel_xml,                                     -- raw <channel> minus items
            followed, refresh_minutes, feed_etag, feed_last_modified,
            last_refreshed_at, last_error, added_at
episodes  + pub_ts, summary, image_url, link, audio_type, audio_length, feed_duration,
            season, episode_number, episode_type, explicit,
            item_xml,                                        -- raw <item>
            in_feed, ever_downloaded
```

Built in migration 4:

```
episodes  + state, state_note,                               -- NULL, queued, running, ready, failed
            original_path, cut_path, cut_type, cut_duration, cut_bytes, processed_at
jobs(id, kind, episode_id, state, step, progress, error, cancel_requested, attempts,
     requested_by, created_at, started_at, finished_at)
```

Audio lives under the media folder: `original/<key>.<ext>` and
`cut/<key>.<ext>`, where the key is the first 16 hex digits of the guid's
SHA-1. Fingerprints stay under the data folder, named as the CLI names them,
so episodes the CLI fingerprinted are reused.

Planned:

```
podcasts  + public_token, retention_days
episodes  + public_token, expires_at
edits(id, episode_id, kind, start, end, segment_id, created_at)  -- kind: cut (hand-added section)
```

Guids are unique across the whole store. If two shows use the same guid,
the second show's copy is stored under `feed URL#guid`.

Episode rows from feeds have no fingerprint until downloaded; the detector
only looks at fingerprinted ones, so they don't affect it.

## Routes

| Route | Login | What | State |
| --- | --- | --- | --- |
| `GET /` | yes | podcasts | built |
| `GET /login`, `POST /login`, `POST /logout` | no | session | built |
| `GET /healthz` | no | probe | built |
| `POST /podcasts` | yes | add a feed | built |
| `GET /podcasts/{id}?view=&q=&page=` | yes | show header and episode list; `partial=1` gives only the rows | built |
| `POST /podcasts/{id}/refresh` | yes | refresh now | built |
| `POST /podcasts/{id}/episodes` | yes | `action` = archive, unarchive or archive_older on `ids`; JSON when asked | built |
| `POST /podcasts/{id}/remove` | yes | stop following | built |
| `POST /podcasts/{id}/episodes` with `action` = download or reprocess | yes | queue jobs for a selection | built |
| `GET /episodes/{id}` | yes | episode page; the timeline editor later | built |
| `GET /episodes/{id}/row`, `/status` | yes | pieces of pages that poll while an episode is processed | built |
| `GET /episodes/{id}/audio[?download=1]` | yes | cut audio, with byte ranges | built |
| `GET /episodes/{id}/original` | yes | original audio, with byte ranges | built |
| `POST /episodes/{id}/process` | yes | queue it, or process it again | built |
| `GET /jobs`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` | yes | job queue | built |
| `POST /episodes/{id}/edits` | yes | save timeline edits | |
| `GET /p/{token}/feed.xml` | no | private feed | |
| `GET /p/{token}/{episode_token}.{ext}` | no | cut audio | |

## Milestones

1. **Skeleton** (done): `podblock/web/` package, settings from env vars, app
   tables in the shared database, API-key login, base layout and theme,
   `podblock serve`, `podblock api-key`, Dockerfile.
2. **Feeds** (done): add, refresh, show header, episode list, prune,
   archive with multi-select, remove.
3. **Processing** (done): jobs table and worker, download with comparison
   episodes, detect, render cut audio, episode page, jobs page.
4. **Private feed**: tokens, feed XML, audio with byte ranges.
5. **Retention**: expiry job, keep pins, settings.
6. **Timeline editor**: waveform, sections, marks, manual sections,
   re-render.
7. **Deploy**: Kubernetes manifests (Deployment, PVCs, Service, Ingress
   example), backup notes.

## Open questions

None right now.

## Future features

- Username and password logins and user management (the `users` table is
  ready for it), then per-user private feeds.
- Download new episodes on their own, per podcast.
- Just-in-time streaming of episodes not yet downloaded: a quick pass and a
  stream that starts before the podcast app gives up.
- Shift chapter and transcript timestamps to match the cut audio, and add
  chapters marking where cuts were made.
- A per-podcast choice of which section types to cut.
- A per-podcast setting for how much new audio between repeats is cut with
  them (4 seconds today).
- OPML import and export.
- One combined private feed across podcasts.
- Postgres, and the worker as its own Deployment, so the web side can run
  more than one replica.
- CSRF tokens on forms. For now the session cookie is `SameSite=Lax`, which
  keeps other sites from posting with it.
- Push or webhook notice when an episode is ready.
- A "this was a repeat" button in the player that sends the spot back as a
  manual section.
- Stats: time cut per episode and per podcast.
- Cache and shrink artwork on our side. Pages now load each show's and
  episode's artwork straight from the feed's host, lazily, and some feeds
  use 3000 px images.
- Follow feeds that move: a 301 redirect or `itunes:new-feed-url`.
- Cut ready episodes again when a later download finds new blocks in them
  (the detector already rescans those episodes; only their cut files go
  stale). For now, "Process again" does it by hand.
- A real job for feed refreshes, once the job queue exists, so they show on
  the jobs page.
- Helm chart.

## Changelog

- 2026-10-19: plan written; first round of questions answered; milestone 1
  built. Real feed URLs and show names taken out of the repo and its
  history. Milestone 2 built. Milestone 3 built.
