# pod-block web app

The second half of pod-block: a web app that follows feeds, cuts the repeats
that the detector finds out of the episodes you pick, and serves the cut
episodes as private podcast feeds. `README.md` covers the detector and
`CLI.md` the command line; this file covers the app: what it does, how it is
built, and what could come next.

## Decisions

| Topic | Decision |
| --- | --- |
| Database | One database for the detector and the app: the app's tables and columns go in `podblock.sqlite` beside the detector's. If we move off SQLite, both move to the same host together. |
| Marking blocks | Marking a block intro, outro, show or ignored on one episode changes the block itself, for every episode, as `podblock type` does. |
| Block types | `show` (Local in the app): a block checked only against its own podcast's episodes, never other feeds'. `ignored`: a false positive, never reported, so never cut. Both are choices in the timeline. Every section a report lists is cut. |
| Comparison episodes | When a show has fewer than 4 fingerprinted episodes, a download also pulls the episodes published after the chosen one, nearest first, and older ones only when it reaches the newest episode. These are fully processed too: shows often run in parts, so they are likely next anyway. |
| Retention | Counts from when an episode was processed, not its publish date. A keep pin stops the clock. |
| Users | Built for a few users. The admin logs in with the API key and adds the others, with user names and passwords. Feeds, downloads, processing, blocks and podcast settings are shared, and any user can add a feed, download and edit timelines. Removing a podcast is the admin's, since it changes things for everyone. Archived, kept, filters, which podcasts are on the home page, and private feeds are per user. |
| Downloads | By hand, apart from the comparison episodes above and podcasts set to download new episodes on their own. |
| Short repeats | Sections under 15 seconds aren't cut (`report.MIN_SECTION_SEC`; the CLI's `--min-section`), unless they are ad breaks, intros, outros, show blocks or blocks typed by hand. Short blocks are still stored, since bumpers are short and are how breaks are found. |
| Intro and outro marks | Marking a section Intro types only its last block, and Outro only its first: the report runs those to the ends anyway, and a section's other blocks are often ads that also play in mid-roll breaks. Keep and Local type every block in the section. |
| Grabs | A grab is a user getting an episode: through their private feed or omni feed, by playing it in its page's player (not the timeline's), or with its Download button. Hits within 10 minutes of the last one, or starting partway into the file, are the same grab, since apps stream and download at once and seek with byte ranges. A play on the page counts after the longer of 5 minutes and 5% of the episode (90% of one shorter than that). |
| Keeping it generic | No real feed URLs, show names or audio in the repo, its history or its tests. Real feeds for trying things by hand live in the environment variable `PODBLOCK_TEST_FEEDS` (space-separated), set in the cloud environment's settings. |

## Stack

| Part | Pick | Why |
| --- | --- | --- |
| Web framework | FastAPI on Uvicorn | typed, async where it helps (streaming files), plain sync code where it doesn't; easy to grow a JSON API next to the pages |
| Pages | Jinja2 templates, HTMX for partial updates, Alpine.js for small bits of client state | server-rendered, no Node build step; JS libraries are copied into `podblock/web/static/vendor/` so the container needs no CDN |
| Timeline and player | `static/timeline.js`, no library | a canvas over the part in view, with waveform data worked out on the server |
| Styling | hand-written CSS with custom properties | dark by default, mobile first, no build step |
| Database | `podblock.sqlite`, through `sqlite3` as the detector's `Store` does | one file on a volume; the app's tables are added by numbered steps in `podblock/app/db.py` |
| Background work | a worker process, `podblock worker`, that `serve` starts and restarts; inside it, threads (`PODBLOCK_WORKERS`, default 1) take jobs from a table in the database, and one more refreshes feeds and cleans up | survives restarts, needs no broker; a crash or memory spike in a job can't take the pages down; the two share only the database, so the worker can move to its own container later |
| Audio cutting | `podblock/cut.py`: one ffmpeg decodes to raw samples, Python drops the cut stretches and fades each join, a second ffmpeg encodes | flat memory whatever the length, cuts on the exact sample, real fades; one decode and one encode |

## How it fits together

```
browser ──login──▶ management UI (FastAPI + Jinja + HTMX)     ── podblock serve
                        │  writes jobs, then SIGUSR1 ─────────────────┐
                        ▼                                             ▼
                   jobs table ◀── worker threads: download, fingerprint, detect,   ── podblock worker
                        │                         render cut audio, apply edits       (a child of serve)
                        │         refresh thread: refresh feeds
                        │         retention:      expire old downloads
podcast app ──no login, secret path──▶ /p/<user's feed token>/feed.xml
                                       /p/<user's feed token>/<episode key>.mp3
```

- Jobs, feed refreshes and clean-up run in a process of their own,
  `podblock worker`, which `serve` starts as its child
  (`podblock/app/worker.py`). A crash or a memory spike while
  fingerprinting can't take the pages down. If the worker dies, `serve`
  starts it again, waiting a second, then twice as long each time it dies
  soon after starting (up to a minute); the Jobs page says so meanwhile, and
  jobs it was running go back in the queue. Stopping `serve` stops it, and a
  worker whose `serve` is gone (killed outright) stops within two seconds.
- The two share only the database. After queuing a job a page sends the
  worker SIGUSR1 so it starts at once; it also looks every 5 seconds. The
  worker tells `serve` over a pipe when it is ready for that signal, as one
  sent sooner would kill it. Only one worker may run on a database (a lock
  file, `worker.lock`, beside it), since on starting it puts jobs left
  running back in the queue. Feed refreshes, which the pages run too, and
  database upgrades take a lock file each, so they happen one at a time
  across both (`podblock/app/locks.py`).
- `PODBLOCK_JOBS=threads` runs the worker in `serve`'s own process instead,
  as before; `none` runs none, for a `podblock worker` you start yourself
  (another container in the same pod, say, sharing the volumes).

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

### Code layout

Three layers, each importing only the ones below it
(`tests/test_layers.py` checks this):

| Layer | Where | What |
| --- | --- | --- |
| Web app | `podblock/web/` | `app.py` puts the FastAPI app together; `routes/` has one module per part of the site, each parsing a request, calling the app and rendering; `deps.py` is what routes ask for (the database, the user); `templating.py`, `templates/` and `static/` are the pages |
| App | `podblock/app/` | everything else the app does, with no HTTP in it: following feeds (`following.py`), the job queue (`jobs.py`) and what the jobs do (`processing.py`), the states episodes and jobs move through (`states.py`), the worker process that runs them (`worker.py`), private feeds (`private_feeds.py`), users, filters, grabs, retention, and the database's tables and transactions (`db.py`) |
| Detector | `podblock/*.py` | fingerprinting, matching, the block store, reports and cutting; the command line (`cli.py`) starts all three |

The worker process imports only the app and the detector, so it runs
without the web app's extras.

## Settings

Two paths, each its own volume in Kubernetes:

| Env var | Default | Holds |
| --- | --- | --- |
| `PODBLOCK_DATA_DIR` | `./podblock-data` | `podblock.sqlite` and fingerprints (small; back this up) |
| `PODBLOCK_MEDIA_DIR` | `$PODBLOCK_DATA_DIR/media` | original and cut episode audio (large, can be lost) |

Others:

| Env var | What |
| --- | --- |
| `PODBLOCK_API_KEY` | fixed login key; when unset, one is made on first start, its hash stored, and the key printed to the log once |
| `PODBLOCK_SECRET_KEY` | signs session cookies; made and stored on first start when unset |
| `PODBLOCK_SECURE_COOKIES` | `true` to send the session cookie over HTTPS only; set it behind TLS |
| `PODBLOCK_PUBLIC_URL` | base URL for links in the private feed, e.g. `https://pods.example.com`; falls back to the address the request came to |
| `PODBLOCK_WORKERS` | threads running jobs (default 1). Fingerprinting takes about 0.3 GB and one core per thread |
| `PODBLOCK_JOBS` | where jobs run: `process` (default, a `podblock worker` that `serve` starts), `threads` (inside `serve`) or `none` (run `podblock worker` yourself; only one may run) |
| `FORWARDED_ALLOW_IPS` | Uvicorn's own: which proxies to trust for `X-Forwarded-*` headers; set it to the ingress's address range |

The image (`Dockerfile`) is `python:3.12-slim` plus ffmpeg, runs as uid
1000, and starts `podblock serve --host 0.0.0.0 --port 8000`. The database
runs in WAL mode so pages can read while the worker writes. For running it
on Kubernetes, with backups and upgrades, see `deployment/README.md`.

## Features

### Feeds and episode list

- Add a feed by RSS URL. The app fetches it and stores the show's metadata
  (title, author, description, artwork, link, language, categories, explicit
  flag) and every episode. Feeds over 64 MB, and pages that aren't RSS, are
  refused with a reason.
- The show page has a header built from the feed: artwork (also blurred
  behind the header), author, title, episode count, categories, description
  (three lines, then "More"), Subscribe, Refresh and Website, when the feed
  was last checked, and the last error. How much has been cut shows here and
  on the podcast's card on the home page.
- The home page shows, under its heading, a small bar of how much of the
  media volume the downloads take, with the free space. For the admin it
  links to the Downloads page.
- Below the header, tabs: Episodes, Private feed, Filters and Settings
  (`?tab=`; links land on the tabs with `#tabs`, so a phone needn't scroll
  past the header again). Only the open tab is rendered. Remove is at the
  foot of Settings, for the admin.
- Episode list: artwork, title, date, length, season and episode number,
  trailer and bonus badges, two lines of show notes as plain text. Newest
  first, or oldest first if set per podcast; 50 at a time, with the next 50
  loading as "Load more" scrolls into view (a plain link without
  JavaScript). Search by title as you type.
- Each row shows its processing state: Queued, what it is doing
  ("Downloading 45%", "Fingerprinting", "Cutting"), Ready with the cut
  length, Failed, or Cleaned up with the date. A row being processed
  refreshes itself every 3 seconds.
- Feeds refresh by hand, and in the background when due (every 60 minutes
  by default, set per podcast). Refreshes send the last ETag and
  Last-Modified, so an unchanged feed costs one short request.
- The app keeps its own copy of each episode's metadata, including the raw
  `<item>` XML, so the episode survives after it leaves the feed.
- When an episode leaves the feed, it is deleted unless it was ever
  downloaded or the detector knows it (fingerprint, block matches or
  report). Those stay, marked "No longer in feed", and lose the mark if they
  return. A refresh that finds no episodes at all deletes nothing.
- Feeds that move are followed: a permanent redirect (301 or 308) or
  `itunes:new-feed-url` updates the stored address, unless another podcast
  has it already.
- New episodes can download on their own, set per podcast (off by default).
  Only episodes that appear after the feed was added count, and at most 5 a
  refresh, so a feed that re-issues its back catalogue can't queue hundreds.
- Removing a podcast asks first and says what happens: episodes never
  downloaded are deleted; downloaded ones, and the blocks learned from the
  show, stay (blocks may be ads other shows share). The podcast is hidden
  and no longer refreshed; adding the same feed again brings it back.

### Selecting episodes

- Each row's artwork is its checkbox: click it to select, shift-click
  another to select everything in between. While anything is selected, a
  click anywhere on a row toggles it too. Esc clears.
- On touch screens, tap the artwork, or press and hold a row, to select;
  while selecting, a tap toggles and a second press and hold selects the
  range since the last one.
- A bar with the count, select-all, Download, Keep, Archive (or Unarchive)
  and Clear rises while anything is selected. Archiving takes the rows out
  without a page load, with Undo in the toast. Archiving is per user, with
  an Archived tab to bring episodes back.
- Each row has a download button ("Download and cut", or "Download again"
  once cleaned up or after a failure); a spinner takes its place while
  queued or running, and a tick once ready. Its menu has "Process again",
  Keep and "Archive this and older".

### Processing

- Downloading an episode queues a job: download, fingerprint, compare with
  nearby episodes, build the report, render the cut audio. Episodes already
  ready or queued are skipped; "Process again" redoes a ready one, reusing
  its download.
- When the show has fewer than 4 fingerprinted episodes, the job takes the
  episodes after the chosen one until it has 4, then older ones if the feed
  runs out. They are fingerprinted first and compared as one group, then
  each is reported and rendered like a chosen one. If the job fails or is
  cancelled, companions not yet ready go back to rest quietly.
- The jobs page (with a count in the nav) shows running, queued and recent
  jobs with their step, progress bar and errors, and lets you cancel or
  retry. Cancelling stops a running job between steps or mid-download.
  Jobs left running when the worker stops go back in the queue when it
  starts again, unless they were being cancelled. The jobs run in a process
  of their own (see "How it fits together").
- Only one job works on an episode at a time, however many workers there
  are. A running job holds its episode and the ones it took along
  (`episodes.held_by`); a job asked for one of those meanwhile (a re-cut,
  say) waits in the queue until it is let go, and the other workers take
  the next job instead. An episode has at most one job waiting. The
  episode's state can't decide this, as a job moves it back and forth as it
  goes.
- Cut audio keeps the source format (MP3, AAC in M4A, Opus, Vorbis; anything
  else becomes MP3) at the nearest standard bitrate, with a 40 ms fade
  either side of each join. Tags and cover art are copied. Kept stretches
  under a second between two cuts go too.
- Chapters and transcripts are moved to fit the cut (`podblock/app/chapters.py`).
  The first time an episode is cut, its chapters are read: the feed's
  `podcast:chapters` JSON, else its inline `psc:chapters`, else the ones in
  the audio file. Its timed transcripts (`podcast:transcript` as WebVTT, SRT
  or JSON) are saved under `media/transcripts/`. A time in a kept stretch
  moves back by everything cut before it; a time inside a cut moves to the
  join; a chapter or cue left with nothing (a sponsor chapter, an ad read)
  goes. Transcripts with no times (HTML, plain text) are left out.
- Each join gets a chapter saying what was cut and how much: "1:32 ad break
  cut", or, inside a chapter of the source's, "Interview (after 1:32 ad
  break cut)". An episode with no chapters of its own gets a "Start"
  chapter before its first cut. The chapters are written into the cut file
  (ID3 chapters in MP3, chapters in M4A and Ogg) and served beside it.
  Episodes cut before this get chapters in their feed at once, worked out
  from their report, and in the file when next cut.
- Every section the report lists is cut. Up to 4 seconds of new audio
  between two repeats is cut with them (set per podcast, 0 to 120 seconds),
  so a cut never leaves a scrap of sound between two others, and a new ad
  between two known ones goes too.
- When a download finds new blocks, the show's other ready episodes are
  queued to be cut again; those whose cut wouldn't change are done in
  moments. A re-cut never sets off more re-cuts.
- The episode page shows the status (queued and running ones update
  themselves), a player and a download for the cut file, how much was cut,
  the timeline, and the show notes.

### Timeline editor

- For a ready episode, a timeline of the whole episode: the waveform, each
  cut section drawn on it in its type's colour, the bumpers around ad
  breaks as darker bands, a time ruler, and a strip above with the whole
  episode and a box for the part in view (drag it to move). Zoom with −,
  Fit and +, or ctrl/⌘ and the wheel. Below it, a card per section with the
  same controls.
- The waveform is worked out once on the server (loudness in dB, ten values
  a second) and saved under `media/peaks/`, so the browser never decodes the
  audio. The canvas only ever covers what is in view, so zooming into a long
  episode stays smooth.
- Labels sit in two rows above the waveform: a dropdown where there is
  room, a small icon for the type where there isn't (click it to zoom in),
  else nothing; the selected section gets room first. Play buttons sit
  under each section when there is room. The cards always have it all.
- Phones get the same timeline, horizontal and fitted to the screen, with
  the cards below. Vertical was ruled out: dragging to mark a section and
  dragging to scroll the page would fight.
- Click the waveform to play the original from that point, or a section's
  play button to play from two seconds before it. "Skip cuts" jumps over
  everything that would be cut, unsaved changes included, to hear the
  joins.
- Set a section to Intro, Outro, Local, Keep, or back to how it was
  found. This changes its blocks everywhere (see Decisions for which
  blocks), so other ready episodes they play in are cut again. Keep on an
  ad break also marks its bumpers as false hits, so the break stays.
- Add a section by hand: drag across the waveform (even over a section
  already there), or on a phone tap "Add section" at the player's place.
  Drag its edges, or type times and nudge them a second at a time or set
  them to the player's place. Choose to cut it here only, or to catch it in
  other episodes too (saved as a block of the chosen type, and the show's
  other ready episodes are checked again). Hand-cut sections can be put
  back.
- Nothing is written until Save, in a bar that counts the unsaved changes;
  Discard puts it all back, and leaving the page asks first. Saving queues
  an `edit` job; the episode page shows it working and reloads when done.
  The old cut file is served until the new one replaces it. Episodes whose
  cut would come out the same aren't cut again.

### Login and users

- Every page needs a login, apart from `/healthz` and the private feeds and
  their audio under `/p/`. The login page sets a signed, HTTP-only session
  cookie (30 days). Scripts can send `Authorization: Bearer <key>` instead.
- The admin's user name is `admin` and its password is the API key, from
  `PODBLOCK_API_KEY` or made on first start and printed to the log (the
  label changes to "API key" when you type `admin`). `podblock api-key`
  makes a new key and logs everyone out.
- Everyone else has a user name and password. Passwords are stored as
  scrypt hashes; names are 1 to 40 letters, digits, dots, dashes or
  underscores, kept in lower case.
- The admin's Users page adds a user with a password, sets a new password
  for one, or removes one (after a confirmation). Removing a user deletes
  their archived and kept flags, podcast picks, own filters and feeds; jobs
  and timeline changes they made stay. The list shows how many podcasts
  each has picked and when they last logged in.
- The Account page lets users change their own password. A new password,
  whether they set it or the admin did, ends the user's other sessions; the
  session that changed it carries on.
- Each user picks the podcasts on their home page: "My podcasts" lists
  them, "All podcasts" lists every one with Subscribe and Unsubscribe, and
  the show page has the same button. Adding a feed puts it on the adder's
  page, even if someone else added it first. The admin starts with every
  podcast. Removing a podcast takes it off everyone's page.
- The admin's Downloads page (`/downloads`) lists every episode with audio
  on disk, from every podcast (removed ones too), with the size of its
  download and cut file; at the top the total, the originals and cut files
  apart, and the media volume's free space with a bar of how much pod-block
  takes. Sort by size, newest or podcast. Delete one, or select several as
  in the episode list: the download, cut file and waveform go, and a ready
  episode becomes cleaned up, as retention does it, so it can be downloaded
  again and cut the same way. Episodes being processed are left alone.

### Filters

- A filter is a regular expression matched anywhere in an episode's title,
  ignoring case, for one podcast or for all of them. It hides matching
  episodes from your lists (and from the counts on the home page); a
  Filtered tab lists them. Bad patterns, and ones that match every title,
  are refused. The Filters tab says how many of the podcast's episodes each
  filter matches.
- "Filter from processing" hides a filter's episodes from everyone and
  keeps them out of finding repeats, for compilation, "best of" and rewind
  episodes, which replay whole earlier episodes and would otherwise make
  them look like one long repeat. Such episodes never download on their
  own, are never taken along as comparison episodes, and are never compared
  with. Downloading one by hand still works: it is cut using repeats found
  elsewhere, and nothing is learned from it.
- When an episode the detector already learned from is filtered from
  processing, an `unlearn` job forgets what it taught: every block not typed
  by hand that, leaving filtered episodes out, plays in fewer than two
  episodes is deleted, and the ready episodes that played one are cut
  again. Its comparison records go too, so taking the filter away lets it
  be compared afresh.
- Your filters are yours; anyone sees everyone's filters from processing,
  with who added them. Only the owner or the admin removes a filter.

### Private feeds

- Each user gets a feed of each podcast, at `/p/<token>/feed.xml` with a
  random token of their own (128 bits, URL-safe), made the first time they
  open the show page. It lists ready episodes with their cut audio on disk,
  newest first, except the ones that user archived or that a filter hides
  from them. The Private feed tab shows the address with Copy and a QR code (made on
  the server with `segno`).
- Each user also gets an omni feed, at the same kind of address: every
  ready episode of the podcasts on their home page, newest first, without
  the ones they archived or filtered. Its channel is pod-block's own ("Pod
  Block Omni Feed", the logo in `static/omni.jpg` as artwork,
  `itunes:block` and `podcast:locked`). Each item has the show's name
  before its title ("Show: Episode"), the show as `itunes:author`, and the
  show's artwork when the episode has none. Guids are the store's, which
  are unique across shows. The "Omni feed" button on the home page opens
  its address and QR code.
- Each feed, the omni feed too, has a second address, `downloaded.xml` in
  place of `feed.xml`, with the same episodes newest downloaded first.
  Apps sort by `pubDate`, so there each item's `pubDate` is when its
  episode was downloaded and first cut (`ready_at`). The feed panel has a
  switch, "By release date" or "By download date", that picks which
  address and QR code it shows.
- A feed keeps the show's channel and each episode's `<item>` as the source
  had them (`podblock/app/private_feeds.py`), with these changes: the enclosure
  points at the cut file with its length and type, and `itunes:duration` is
  the cut length; `podcast:chapters` and `podcast:transcript` point at our
  copies, moved to fit the cut (see Processing), and the source's chapters,
  other transcripts and soundbites go, as their times no longer fit; every
  other link to the uncut audio goes (`media:content` for
  audio, `podcast:alternateEnclosure`), since some apps play those instead;
  `itunes:new-feed-url` goes, as it would send apps back to the source;
  `atom:link rel="self"` points at the private feed; and `itunes:block`
  asks directories not to list it. Items with no artwork get the show's.
  Every feed says what made it (`generator`) and when it last changed
  (`lastBuildDate`, the newest processing time, so the ETag holds steady
  between changes). Namespace prefixes are kept as the source wrote them,
  and guids stay the source's.
- Each episode's audio is at `/p/<token>/<key>.<ext>`, where the token is
  the feed's own, so each fetch says whose it is (see Grabs), and the key is
  the one its files are named by. Its chapters are at
  `/p/<token>/<key>.chapters.json`, and its transcripts at
  `/p/<token>/<key>.transcript.<vtt|srt|json>`. Byte ranges, `HEAD`, `ETag`
  and caching headers come with the audio; the feed itself answers
  `If-None-Match` with 304. Audio links from before feeds carried their own
  token used the podcast's (`podcasts.public_token`); those still work, and
  count for nobody.
- "Make a new address" gives your own feed, or your omni feed, a new token
  after a confirmation; the old address stops working at once, in both
  orders, and so do the audio links that came through it.
- Unknown tokens, and podcasts removed in the app, get a plain "Not found"
  that says nothing more.
- Feed links use `PODBLOCK_PUBLIC_URL`, else the address the request came
  to. Behind a proxy, set one or the other (`FORWARDED_ALLOW_IPS`), or
  links will point at the inside address.

### Grabs

- Every time a user's podcast app fetches an episode through their private
  feed or omni feed, it counts toward a grab by that user (`podblock/app/grabs.py`,
  table `grabs`). Apps fetch audio in pieces, some stream while a download
  runs alongside, and a seek is another request; so a hit within 10 minutes
  of the last one, or one that starts partway into the file, adds to the
  grab before it. A `HEAD`, and the chapters and transcripts, count for
  nothing.
- Playing an episode on its page counts too, once its player has played
  the longer of 5 minutes and 5% of it (or 90% of an episode shorter than 5
  minutes); seeks don't add to the time. The timeline's player is for
  editing and doesn't count. The page's Download button counts too.
- Each row in the episode list, and the episode page, shows "Grabbed" (with
  "×2" and so on) for your own grabs, with when you last did in its tooltip.
  A Grabbed tab lists the episodes you grabbed, until you archive them. The
  Users page counts each user's grabs.

### Retention

- Downloads are cleaned up a set time after the episode was first made
  ready (default 7 days, set per podcast; 0 keeps them for good).
  Processing again or saving timeline changes doesn't restart the clock.
  The download, cut file, waveform and saved transcripts go (and the next
  download looks for chapters and transcripts again); the episode's metadata, report and
  hand-cut sections stay. It shows as "Cleaned up" with the date, leaves the
  private feed, and "Download again" brings it back, cut the same way.
  Downloads that never became ready (failed jobs, companions of a cancelled
  one) go on the same clock. The check runs every minute in the background
  (`podblock/app/retention.py`).
- Keep (per user) stops an episode from being cleaned up while anyone keeps
  it: in each row's menu, in the selection bar, and on the episode page.
  Kept and ready episodes keep their fingerprints too.
- Ready rows say "Cleaned up in 5 days"; the episode page gives the date,
  with Keep beside it.
- Cleaned-up episodes show dimmed in the list, and in a Downloaded tab
  (everything downloaded at some point, until archived). A note above the
  list counts them, with "Archive them all"; the home page shows each
  podcast's ready and cleaned-up counts and an "Archive N cleaned-up
  episodes" button across your podcasts.
- Once a day the detector's own clean-up runs: fingerprints no longer in
  use go after 30 days, blocks not heard in 90 days (unless typed by hand),
  bumpers after a year. Fingerprints of ready and kept episodes are marked
  fresh first, since the detector ages them by file time. Audio the CLI
  downloaded is left alone.
- The Settings tab sets days before clean-up, how often to check the feed
  (15 minutes to a week), how close two repeats must be to be combined,
  oldest first, and whether new episodes download on their own. Settings
  are shared.

### Look and feel

- Dark theme, built on CSS custom properties so a light theme can follow.
- Mobile first: a bottom tab bar on phones, touch targets of at least
  44 px, inputs at 16 px so iOS doesn't zoom, no hover-only controls.
- Pages work without JavaScript where it costs little: adding, refreshing,
  paging, searching and archiving all work as plain forms and links. The
  timeline needs it.

## Data model

The app's tables and columns, added by migrations 1 to 15 in
`podblock/app/db.py`, beside the detector's (see `README.md`):

```
settings: app_schema, api_key_hash, auth_epoch, secret_key   -- rows beside the detector's own

users          + session_version,                            -- goes up on a new password, ending other sessions
                 last_login_at, omni_token
user_episodes(user_id, episode_id, archived, kept)
user_podcasts(user_id, podcast_id, added_at)                 -- the podcasts on each user's home page
user_feeds(user_id, podcast_id, token, created_at)           -- each user's private feed of each podcast
episode_filters(id, user_id, podcast_id, pattern, processing, created_at)  -- podcast_id NULL: every podcast
grabs(id, user_id, episode_id, via, first_at, last_at, hits)  -- via: feed, omni, app or download

podcasts  + author, description, image_url, link, language, categories (JSON), explicit,
            channel_xml,                                     -- raw <channel> minus items
            followed, refresh_minutes, feed_etag, feed_last_modified,
            last_refreshed_at, last_error, added_at,
            public_token,                                    -- guards audio links
            retention_days, auto_download, fill_gaps, sort_oldest
episodes  + pub_ts, summary, image_url, link, audio_type, audio_length, feed_duration,
            season, episode_number, episode_type, explicit,
            item_xml,                                        -- raw <item>
            in_feed, ever_downloaded, skip_processing,
            state, state_note,                               -- NULL, queued, running, ready, failed, expired
            original_path, cut_path, cut_type, cut_duration, cut_bytes, processed_at,
            cut_spec,                                        -- the stretches kept in the cut file
            peaks_path,                                      -- the waveform
            source_chapters, transcripts,                    -- the source's, as found when first cut
            cut_chapters,                                    -- the chapters written into the cut file
            held_by                                          -- the running job working on it
            ready_at, expired_at                             -- the retention clock

jobs(id, kind, episode_id, state, step, progress, error, cancel_requested, attempts,
     requested_by, created_at, started_at, finished_at, payload)
edits(id, episode_id, start, end, created_by, created_at)    -- sections cut by hand, this episode only
```

Connections get a `REGEXP` function (`db.connect`), so filters run in SQL.

Audio lives under the media folder: `original/<key>.<ext>` and
`cut/<key>.<ext>`, where the key is the first 16 hex digits of the guid's
SHA-1. Fingerprints stay under the data folder, named as the CLI names them,
so episodes the CLI fingerprinted are reused.

Guids are unique across the whole store. If two shows use the same guid,
the second show's copy is stored under `feed URL#guid`.

Episode rows from feeds have no fingerprint until downloaded; the detector
only looks at fingerprinted ones, so they don't affect it.

## Routes

| Route | Login | What |
| --- | --- | --- |
| `GET /login`, `POST /login`, `POST /logout` | no | session |
| `GET /healthz` | no | probe |
| `GET /p/{token}/feed.xml` | no | a private feed or an omni feed |
| `GET /p/{token}/downloaded.xml` | no | the same feed, newest downloaded first |
| `GET /p/{token}/{key}.{ext}` | no | cut audio, with byte ranges; counts toward a grab |
| `GET /p/{token}/{key}.chapters.json` | no | its chapters, moved to fit the cut |
| `GET /p/{token}/{key}.transcript.{vtt,srt,json}` | no | its transcript, moved to fit the cut |
| `GET /` (`?view=all`) | yes | your podcasts, or every podcast with Subscribe |
| `POST /podcasts` | yes | add a feed |
| `GET /podcasts/{id}?view=&q=&page=&tab=` | yes | show header and a tab: episodes (`view` = active, archived, downloaded, grabbed or filtered), `feed`, `filters` or `settings`; `partial=1` gives only the rows |
| `POST /podcasts/{id}/refresh` | yes | refresh now |
| `POST /podcasts/{id}/episodes` | yes | `action` on `ids`: archive, unarchive, archive_older, download, reprocess, keep, unkeep or archive_expired; JSON when asked |
| `POST /podcasts/{id}/settings` | yes | clean-up days, refresh interval, gap, auto-download, `sort` (newest or oldest) |
| `POST /podcasts/{id}/subscribe` | yes | `on` = true or false: put it on your home page or take it off |
| `POST /podcasts/{id}/feed-token` | yes | new address for your own private feed |
| `POST /podcasts/{id}/filters` | yes | add a filter: `pattern`, `scope` (podcast or all), `processing` |
| `POST /podcasts/{id}/remove` | admin | stop following |
| `POST /filters/{id}/remove` | owner or admin | remove a filter |
| `POST /omni-token` | yes | new address for your omni feed |
| `POST /episodes/archive-expired` | yes | archive every cleaned-up episode of your podcasts |
| `GET /episodes/{id}` | yes | episode page and timeline |
| `GET /episodes/{id}/row`, `/status` | yes | pieces of pages that poll while an episode is processed |
| `GET /episodes/{id}/audio[?download=1]` | yes | cut audio, with byte ranges; a download counts as a grab |
| `POST /episodes/{id}/played` | yes | the page's player played enough to count as a grab |
| `GET /episodes/{id}/original` | yes | original audio, with byte ranges |
| `GET /episodes/{id}/peaks` | yes | the waveform, made on first ask |
| `POST /episodes/{id}/edits` | yes | save timeline changes (JSON: `types`, `add`, `remove`); queues an edit job |
| `POST /episodes/{id}/process` | yes | queue it, or process it again |
| `POST /episodes/{id}/keep` | yes | keep it or stop keeping it |
| `GET /jobs`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` | yes | job queue |
| `GET /account`, `POST /account/password` | yes | change your own password |
| `GET /users`, `POST /users` | admin | list users; add one (`username`, `password`) |
| `POST /users/{id}/password`, `POST /users/{id}/remove` | admin | new password; remove |
| `GET /downloads?sort=` | admin | every download on disk, with sizes |
| `POST /downloads/delete` | admin | delete the files of `ids` |

## Future features

- Run each job in a child process of the worker, so cancelling can stop
  fingerprinting too (today it stops only between steps), and a memory
  spike in one job can't take the other threads' jobs down with it.
- Move the edges of a section the detector found on the timeline (today:
  mark the extra by hand, or Keep it and mark the right span).
- Let the admin hand out more admin rights, and give the admin a password of
  its own beside the API key.
- Just-in-time streaming of episodes not yet downloaded: a quick pass and a
  stream that starts before the podcast app gives up.
- A per-podcast choice of which section types to cut.
- Join two particular sections on the timeline (the per-podcast gap setting
  joins every pair that close). In the test show, a new ad played between two
  known ones at about 23:00 to 24:00 and was left in.
- Find bumpers that end up inside ad blocks. The test show seems to have
  stings around its breaks, but they got stored as the edges of longer ad
  blocks rather than as short blocks of their own, so they never became
  bumpers, and breaks with new ads in them weren't spanned. Ideas: cut
  blocks where the same short piece keeps turning up at their edges; look
  for short repeats just inside the ends of known ads; let the user mark a
  bumper on the timeline (a Bumper type that splits it out of its block).
- Captions and transcripts with faster-whisper: transcribe each episode
  (int8 on a CPU), shift the times to the cut audio, and serve them in the
  private feed as `podcast:transcript` (SRT or WebVTT). The text could also
  help name ad blocks and find repeats that differ in sound but not words.
- OPML import and export.
- When one episode of a job's group repeats from end to end (an unfiltered
  rewind, say), the whole job fails, the other episodes with it. It could
  fail that one episode and carry on with the rest.
- Filters on show notes as well as titles, and a page listing all of your
  filters across podcasts.
- Postgres, and the worker as its own Deployment, so the web side can run
  more than one replica.
- CSRF tokens on forms. For now the session cookie is `SameSite=Lax`, which
  keeps other sites from posting with it.
- Push or webhook notice when an episode is ready.
- A "this was a repeat" button in the player that sends the spot back as a
  manual section.
- Cache and shrink artwork on our side. Pages now load each show's and
  episode's artwork straight from the feed's host, lazily, and some feeds
  use 3000 px images.
- Run feed refreshes as jobs, so they show on the jobs page.
- A Helm chart, if the kustomize example isn't enough.
