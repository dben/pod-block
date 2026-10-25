# Making the screenshots

The README's screenshots come from three made-up podcasts put through a
fresh pod-block. To take them again after the pages change:

```sh
docs/screenshots/make/run.sh
```

It overwrites the PNGs in `docs/screenshots/`. Needs uv, ffmpeg, node and
Playwright with Chromium; takes about 6 minutes, most of it making and
cutting the audio. Pass a folder to keep the working files (feeds, the
database, uncompressed shots) there instead of a temporary one.

| File | Does |
| --- | --- |
| `feeds.py` | makes the shows: synthetic audio with an intro, two ad breaks between bumpers and an outro, ads shared across two shows, RSS feeds and artwork. Placeholder FM also has a segment theme, an ad read its feed's transcript leaves out, and credits after "This was a production of Podblock Studios" |
| `transcriber.py` | stands in for the transcription service, answering with each episode's script |
| `seed.py` | adds the feeds to the app, downloads episodes, marks intros and outros, and sets up filters, phrases, a hand cut, clean-ups and grabs |
| `shots.js` | logs in and takes each screenshot |
| `run.sh` | runs them all, with the feeds served on :8765, the transcriber on :9000 and the app on :8000, then shrinks the PNGs |

To change a screenshot, change its function in `shots.js`, and the state it
needs in `seed.py`.
