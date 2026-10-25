# How pod-block works

How the detector finds repeats, what each block type does, and where it
keeps what it learns. [`README.md`](README.md) covers installing and using
pod-block; [`WEBAPP.md`](WEBAPP.md) covers the web app's design.

## Blocks

A **block** is a stretch of audio that pod-block has heard in more than one
episode: a theme tune, an ad, a promo, a sting. Each block has a type, and
the type decides two things:

- **Where pod-block looks for it.** Most blocks are looked for in every
  podcast, since networks run the same ads on all their shows. A show's own
  intro and outro are looked for in that show only.
- **What gets cut.** Most blocks are cut where they play and no more. Some
  stretch to cover the audio around them, as below.

pod-block types blocks itself. Change a type to mark which tune opens the
show, or to fix a mistake: in the web app, from a section's menu on the
timeline, or with `podblock type` (see [`CLI.md`](CLI.md)). A type set by
hand is never changed back.

Each block belongs to the podcast it was first found in.

In the web app, Intro, Outro and Keep belong to one show: they change the
block in that show's episodes, and other shows go on treating it as
pod-block found it. The timeline marks these "Custom (this show)", and
removing one puts the block back as it was found. `podblock type` sets a
block's type for every show at once.

### Repeated

The type every new block starts with: an ad, a promo, a jingle. Looked for
in every podcast and cut where it plays.

### Intro and outro

A show's opening and closing music. You set these by hand, since pod-block
can't tell an intro from any other jingle. Looked for in their own show
only.

An intro is cut from the start of the episode to the end of the tune, so
the cold open or the chat before the theme goes too. An outro is cut from
the start of the tune to the end of the episode, taking the patron
thank-yous and trailers that follow. Only the first intro, in the first
half of the episode, and the last outro, in the second half, stretch like
this. Any others are cut where they play.

Some outros are read over a music bed that rarely matches between episodes.
pod-block can find these from the sound alone (see
[Outros read over music](#outros-read-over-music)).

### Local (`show`)

Cut where it plays, like a repeated block, but looked for in its own show
only. In the web app, these are the sections you mark by hand and choose to
catch in the show's other episodes too. You can delete them again.

### Bumpers

The short stings that open and close an ad break: "We'll be right back",
"And we're back". pod-block finds and types these itself, from short blocks
that keep turning up at the edges of runs of ads.

- **bumper-in** opens a break.
- **bumper-out** closes one.
- **bumper** is a sting a show plays at both ends.

Bumpers are looked for in every podcast. They aren't cut themselves, since
hosts often talk over them. They mark a break instead: everything between a
bumper-in and the next bumper-out is cut as one, even ads pod-block has
never heard. In shows without bumpers, a minute or more of known ads back
to back counts as a break.

### Keep (`kept`)

Not cut in one show's episodes, and cut as usual in other shows. Only the
web app sets it, as **Keep (this show)** on the timeline. Keeping an ad
break also keeps its bumpers, so the break stays.

### Ignored

A mistake: audio pod-block took for a repeat. It is never cut in any show,
and it stays in the database so pod-block doesn't learn the same audio
again. In the web app, this is **Not a repeat** on the timeline. Each
episode lists the ones it plays under "Not a repeat", below its timeline,
where you can undo them.

### In short

| Type | Who sets it | Looked for in | What's cut |
| --- | --- | --- | --- |
| `repeated` | pod-block (the default) | every podcast | the block |
| `intro` | you | its own podcast | from the start of the episode to the end of the first intro |
| `outro` | you | its own podcast | from the start of the last outro to the end of the episode |
| `show` | you | its own podcast | the block |
| `kept` | you, for one show | that show | nothing, in that show |
| `bumper-in`, `bumper-out`, `bumper` | pod-block, or you | every podcast | the ad break between them; the bumpers stay |
| `ignored` | you | nowhere | nothing |

## Outros read over music

Some shows end with minutes of talk over a music bed: patron names, plugs,
announcements. The talk changes every week, so the bed seldom matches
between episodes and no block covers it. It shows up in the loudness floor
instead, the level of the quietest moments. During the show the floor
swings as people pause; under a steady bed it holds still. With the setting
on, that steady stretch at the end of an episode is cut as an outro.

The setting is off by default and set per podcast from the command line
(see [`CLI.md`](CLI.md)). It reads a loudness curve stored with each
fingerprint, so it needs no audio.

## Finding repeats

1. **Fingerprint.** Each episode is decoded with ffmpeg and turned into a
   spectrogram. The strongest peaks are kept, each is paired with a few peaks
   just after it, and each pair becomes a 32-bit hash stamped with its time.
   This survives re-encoding, volume changes and moderate noise.
   - Audio is decoded at 16 kHz, and 4-8 kHz is fingerprinted as a band of
     its own. Speech sits mostly below 4 kHz, so music under a host's voice
     often only shows up there.
   - A store keeps the settings it was created with; fingerprints from
     different settings never match.
2. **Compare.** Each new episode is compared with a few others (see
   [Which episodes are compared](#which-episodes-are-compared)). Matching
   hashes from the same audio all sit at one time offset. Runs of densely
   matched seconds at least 1.5 s long are shared audio. Runs under 10 s
   must turn up at least 3 times to be kept.
3. **Store.** Each shared run becomes a block, unless stored blocks already
   cover it. Long blocks are split where other episodes match only part of
   them (an ad break splits into single ads), and slivers that always play
   inside a longer block are dropped.
4. **Bumpers.** Matches within 8 s of each other form a group. A block whose
   matches are short (under 10 s, and no longer than 20 s stored) and that
   keeps opening groups becomes a `bumper-in`; one that keeps closing them a
   `bumper-out`. Tags are worked out afresh on each run until a bumper has
   sat at a break edge 6 times in 3 episodes. It is then confirmed and keeps
   its tag.
5. **Breaks.** A break runs from a bumper-in to the next bumper-out, even
   when the ads between them are all new. The two must be closer than the
   show's longest ad break (8 minutes by default, set per podcast).
   - A break never holds a second bumper-in. When one comes, the first was
     a false start (the same sting before a short notice, say), and the
     break starts again at the second.
   - Some shows play a closing sting and an opening one back to back going
     into a break. With no known ads since the break opened, that bumper-out
     is skipped and the bumper-in after it starts the break.
   - Some shows play one sting on both sides of a break (`bumper`). A pair of
     those only counts when known ads play between them, so the pairing
     can't slip out of step.
   - A lone bumper-in or bumper-out still marks one end, and reaches over up
     to 90 s of new ads to known ones.
   - Shows without bumpers get breaks from 60 s or more of known blocks back
     to back.

## Which episodes are compared

A new episode is compared with the 4 episodes of the same show closest to it
in publish date. People hop around a back catalogue, and the ads that ran
when an episode came out sit in its neighbours, so the newest episodes
aren't always the right ones. It is also compared with the closest episode
of each other show, if published within 14 days, to catch network ads.
Only those episodes are rescanned afterwards, so a request costs the same
however big the store grows.

## Size and speed

Measured on one CPU core:

| | |
| --- | --- |
| Fingerprinting | about 30 s per hour of audio, about 0.3 GB of memory whatever the length |
| Fingerprint file | about 2 MB per hour of audio |
| Stored block | about 60 KB per minute of block |
| Adding one 1-hour episode (download aside) | about 6 s after fingerprinting |
| Report on a stored episode | about 1 s |

The web app clears out old downloads, fingerprints and blocks it no longer
hears ("Retention" in [`WEBAPP.md`](WEBAPP.md)). From the command line,
`prune` does the same ("Keeping it small" in [`CLI.md`](CLI.md)).

## Data

The database and fingerprints live in the data folder
(`PODBLOCK_DATA_DIR`, default `./podblock-data`; `/data` in the container):

| Path | What |
| --- | --- |
| `podblock.sqlite` | podcasts, episodes, blocks with their hashes, occurrences, saved reports, settings |
| `fingerprints/*.npz` | full fingerprint of each episode, so comparisons never decode again |
| `audio/` | episodes the command line downloaded |

The web app keeps its audio apart, in the media folder (`PODBLOCK_MEDIA_DIR`).

```
settings(key, value)                         -- schema version, fingerprint setting
podcasts(id, name, feed_url, outro_bed)
episodes(id, guid, podcast_id, title, pub_date, audio_url, audio_path, fingerprint_path, duration, added_at)
segments(id, source_episode_id, podcast_id, source_start, source_end, label, type, type_manual,
         confirmed, by_hand,                    -- a bumper with enough evidence; marked by hand
         hashes, times, created_at, last_seen)  -- hashes: packed uint32; times: packed int32,
                                                -- 32 ms frames from the block start
occurrences(id, segment_id, episode_id, start, end, segment_start, hits)
podcast_types(podcast_id, segment_id, type, created_at)  -- Intro, Outro or Keep for one show
compared_pairs(a_id, b_id)
reports(episode_id, created_at, report)      -- a report's sections, as JSON
users(id, username, password_hash, is_admin, created_at)  -- web app logins
user_episodes(user_id, episode_id, archived, kept)        -- web app, per user
```

The web app adds its own tables, and columns on `podcasts` and `episodes`
(feed metadata, raw XML); "Data model" in [`WEBAPP.md`](WEBAPP.md) lists
them.
