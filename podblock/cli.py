"""Find audio blocks that repeat across podcast episodes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .breaks import AdBreak, find_breaks
from .feed import download, fetch_feed
from .fingerprint import FingerprintConfig, fingerprint_file
from .matcher import MatchParams
from .outro import music_bed_outro
from .pipeline import (
    COMPARE_OTHER,
    COMPARE_SAME,
    OTHER_WINDOW_DAYS,
    Paths,
    episode_report,
    find_common_blocks,
    ingest_episodes,
    ingest_feed,
    ingest_file,
    neighbours,
    prune,
    refingerprint,
)
from .report import MIN_SECTION_SEC, SKIPPED, build_sections
from .store import TYPES, Store, StoreError


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def params_from(args) -> MatchParams:
    return MatchParams(min_length_sec=args.min_length, min_hits_per_bin=args.min_hits)


def podcast_arg(store: Store, key: str | None, create: bool = False) -> int | None:
    if not key:
        return None
    found = store.find_podcast(key)
    if found is None and create:
        return store.podcast_id(name=key)
    if found is None:
        sys.exit(f"No podcast called {key!r}; see `podblock podcasts`.")
    return found


def cmd_analyze(args, store: Store, paths: Paths) -> None:
    params = params_from(args)
    ids = []
    podcast = podcast_arg(store, args.podcast, create=True)
    for source in args.sources:
        if Path(source).exists():
            ids.append(ingest_file(store, paths, Path(source), podcast))
        else:
            ids += ingest_feed(store, paths, source, args.episodes, keep_audio=not args.delete_audio)
    added = find_common_blocks(store, ids, params, same=args.compare_same, other=args.compare_other,
                               only_among_given=args.no_history)
    print(f"Added {added} new common block(s).")
    print_segments(store)


def cmd_report(args, store: Store, paths: Paths) -> None:
    params = params_from(args)
    if args.feed:
        feed = fetch_feed(args.feed)
        if not 0 <= args.index < len(feed.episodes):
            sys.exit(f"The feed has {len(feed.episodes)} episodes; --index must be below that.")
        known = store.episode_by_guid(feed.episodes[args.index].guid)
        if args.cached and known and show_saved(args, store, known["id"]):
            return
        [ep_id] = ingest_episodes(store, paths, feed, [feed.episodes[args.index]])
        if args.neighbors:
            ingest_episodes(store, paths, feed, neighbours(feed.episodes, args.index, args.neighbors))
    elif args.target and args.target.isdigit() and store.episode(int(args.target)):
        ep_id = int(args.target)
        if args.cached and show_saved(args, store, ep_id):
            return
        if not store.episode(ep_id)["fingerprint_path"]:
            refingerprint(store, paths, ep_id)
        if args.neighbors:
            fetch_neighbours(args, store, paths, ep_id)
    elif args.target:
        if args.neighbors:
            sys.exit("--neighbors needs a feed episode: use --feed URL --index N, or a stored episode id.")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(args.target)
            if not path.exists():
                path = download(args.target, Path(tmp) / "audio")
            fp = fingerprint_file(path, store.config)
        podcast = podcast_arg(store, args.podcast)
        bed = outro_start(args, store, podcast, fp.loudness)
        matches = store.scan(fp, params, podcast_id=podcast, heard=True)
        show(args, report_data(args, store, Path(args.target).name, fp.duration, matches, outro_bed_start=bed,
                               podcast_id=podcast))
        return
    else:
        sys.exit("Give an episode id, file or URL, or --feed URL [--index N].")

    find_common_blocks(store, [ep_id], params, same=args.compare_same, other=args.compare_other)
    show(args, episode_report(store, ep_id, params, fill_gaps=args.fill_gaps, outro_bed=args.outro_bed or None,
                              min_section=args.min_section))


def fetch_neighbours(args, store: Store, paths: Paths, ep_id: int) -> None:
    """Fetch the episodes next to a stored one in its feed, so they can be compared with it."""
    ep = store.episode(ep_id)
    pod = store.podcast(ep["podcast_id"])
    if not pod or not pod["feed_url"]:
        sys.exit("--neighbors needs an episode that came from a feed.")
    feed = fetch_feed(pod["feed_url"])
    index = next((i for i, e in enumerate(feed.episodes) if e.guid == ep["guid"]), None)
    if index is None:
        sys.exit("That episode is no longer in its feed, so its neighbours are unknown.")
    ingest_episodes(store, paths, feed, neighbours(feed.episodes, index, args.neighbors))


def show_saved(args, store: Store, ep_id: int) -> bool:
    saved = store.saved_report(ep_id)
    if saved:
        show(args, json.loads(saved))
    return saved is not None


def outro_start(args, store: Store, podcast: int | None, loudness) -> float | None:
    """Where a music-bed outro starts, when asked for (flag or podcast setting) and found."""
    pod = store.podcast(podcast)
    if not (args.outro_bed or (pod and pod["outro_bed"])) or loudness is None:
        return None
    return music_bed_outro(loudness)


def report_data(args, store: Store, title: str, duration: float, matches,
                ep_id: int | None = None, outro_bed_start: float | None = None, podcast_id: int | None = None) -> dict:
    sections = build_sections(matches, store.types(podcast_id), duration, fill_gaps=args.fill_gaps,
                              outro_bed_start=outro_bed_start, min_section=args.min_section,
                              pinned=store.hand_typed())
    return {"episode_id": ep_id, "title": title, "duration": round(duration, 2),
            "repeated_sec": round(sum(s.length for s in sections), 2),
            "sections": [s.to_dict() for s in sections]}


def show(args, data: dict) -> None:
    """Print a report, as JSON or as text."""
    if args.json:
        print(json.dumps(data, indent=2))
        return
    duration, sections = data["duration"], data["sections"]
    print(f"{data['title']} ({fmt_time(duration)})")
    if not sections:
        print("No repeated sections found.")
        return
    share = f" ({100 * data['repeated_sec'] / duration:.1f}%)" if duration else ""
    print(f"{len(sections)} repeated section(s), {fmt_time(data['repeated_sec'])} in all{share}:")
    for s in sections:
        unique = f", {s['unique_sec']:.0f}s unique filled" if s["unique_sec"] else ""
        blocks = " ".join(f"#{b}" for b in s["blocks"][:8]) + (" ..." if len(s["blocks"]) > 8 else "")
        print(f"  {fmt_time(s['start']):>8} - {fmt_time(s['end']):>8}  ({fmt_time(s['length']):>5})  "
              f"{', '.join(s['types'])}{unique}  {blocks}")
        if args.detail:
            for p in sorted(s["parts"], key=lambda p: p["start"]):
                block = f"#{p['block']}" if p["block"] is not None else ("music bed" if p["type"] == "outro" else "")
                print(f"      {fmt_time(p['start']):>8} - {fmt_time(p['end']):>8}  {p['type']:<10} {block}")


def describe(row) -> str:
    """Type and label of a block, for the end of an output line."""
    tags = []
    if row["type"] != "repeated" or row["type_manual"]:
        tags.append(row["type"] + ("*" if row["type_manual"] else ""))
    if row["confirmed"]:
        tags.append("confirmed")
    if row["label"]:
        tags.append(row["label"])
    return f"  [{', '.join(tags)}]" if tags else ""


def print_segments(store: Store, podcast: int | None = None, block_type: str | None = None) -> None:
    rows = [r for r in store.segment_summary()
            if (podcast is None or r["podcast_id"] == podcast)
            and (block_type is None or r["type"] == block_type)]
    if not rows:
        print("No matching blocks.")
        return
    print(f"{'id':>4} {'length':>7} {'episodes':>8} {'times':>5}  source")
    for r in rows:
        print(f"{r['id']:>4} {fmt_time(r['duration']):>7} {r['episodes']:>8} {r['occurrences']:>5}  "
              f"{(r['source_title'] or '?')[:50]} @ {fmt_time(r['source_start'])}{describe(r)}")
    if any(r["type_manual"] for r in rows):
        print("(* = type set by hand)")


def cmd_list(args, store: Store, paths: Paths) -> None:
    print_segments(store, podcast_arg(store, args.podcast), args.type)


def cmd_podcasts(args, store: Store, paths: Paths) -> None:
    for p in store.podcasts():
        feed = f"  {p['feed_url']}" if p["feed_url"] else ""
        bed = "  [outro-bed]" if p["outro_bed"] else ""
        print(f"{p['id']:>4}  {p['name']}  ({p['episodes']} episodes, {p['blocks']} blocks){bed}{feed}")


def cmd_podcast(args, store: Store, paths: Paths) -> None:
    pod_id = podcast_arg(store, args.podcast)
    if args.outro_bed is not None:
        store.set_outro_bed(pod_id, args.outro_bed)
    pod = store.podcast(pod_id)
    print(f"#{pod['id']} {pod['name']}: outro-bed {'on' if pod['outro_bed'] else 'off'}")


def cmd_show(args, store: Store, paths: Paths) -> None:
    seg = store.segment(args.segment_id)
    if seg is None:
        sys.exit(f"No segment #{args.segment_id}")
    how = " (set by hand)" if seg["type_manual"] else " (confirmed)" if seg["confirmed"] else ""
    print(f"Segment #{seg['id']}  {fmt_time(seg['source_end'] - seg['source_start'])}"
          f"  type={seg['type']}{how}"
          f"  label={seg['label'] or '-'}")
    for o in store.occurrences_for_segment(args.segment_id):
        print(f"  {fmt_time(o['start']):>8} - {fmt_time(o['end']):>8}  {o['title']}")


def print_breaks(breaks: list[AdBreak], indent: str = "") -> None:
    if not breaks:
        print(f"{indent}No ad breaks found.")
        return
    total = sum(b.length for b in breaks)
    print(f"{indent}{len(breaks)} ad break(s), {fmt_time(total)} in all:")
    for b in breaks:
        if b.complete:
            how = f"bumpers #{b.opened_by} / #{b.closed_by}"
        elif b.opened_by:
            how = f"bumper-in #{b.opened_by}; end not found"
        elif b.closed_by:
            how = f"bumper-out #{b.closed_by}; start not found"
        else:
            how = "no bumpers, known blocks back to back"
        print(f"{indent}  {fmt_time(b.start):>8} - {fmt_time(b.end):>8}  ({fmt_time(b.length):>5})  {how}")


def cmd_breaks(args, store: Store, paths: Paths) -> None:
    episodes = store.fingerprinted_episodes()
    if args.episode_ids:
        episodes = [e for e in episodes if e["id"] in args.episode_ids]
    for ep in episodes:
        print(f"#{ep['id']} {ep['title']} ({fmt_time(ep['duration'] or 0)})")
        types = store.types(ep["podcast_id"])
        matches = [m for m in store.occurrences_for_episode(ep["id"]) if types.get(m.segment_id) not in SKIPPED]
        print_breaks(find_breaks(matches, types), indent="  ")


def cmd_type(args, store: Store, paths: Paths) -> None:
    if store.segment(args.segment_id) is None:
        sys.exit(f"No segment #{args.segment_id}")
    store.set_type(args.segment_id, None if args.type == "auto" else args.type)


def cmd_label(args, store: Store, paths: Paths) -> None:
    store.set_label(args.segment_id, args.label or None)


def cmd_delete(args, store: Store, paths: Paths) -> None:
    store.delete_segment(args.segment_id)


def cmd_export(args, store: Store, paths: Paths) -> None:
    seg = store.segment(args.segment_id)
    if seg is None:
        sys.exit(f"No segment #{args.segment_id}")
    ep = store.episode(seg["source_episode_id"])
    if not ep or not ep["audio_path"] or not Path(ep["audio_path"]).exists():
        sys.exit("The source episode's audio is no longer on disk.")
    out = args.output or f"segment-{seg['id']}.mp3"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(seg["source_start"]),
         "-to", str(seg["source_end"]), "-i", ep["audio_path"], out],
        check=True,
    )
    print(out)


def cmd_prune(args, store: Store, paths: Paths) -> None:
    gone = prune(store, audio_days=args.audio_days, fingerprint_days=args.fingerprint_days,
                 block_days=args.block_days, bumper_days=args.bumper_days, dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb}: " + (", ".join(f"{n} {what}" for what, n in gone.items() if n) or "nothing"))


def cmd_serve(args, store: Store, paths: Paths) -> None:
    try:
        import uvicorn

        from .app.config import Settings
        from .web.app import create_app
    except ImportError:
        sys.exit("podblock: the web app needs its extras: pip install 'pod-block[web]'")
    app = create_app(Settings.from_env(data_dir=paths.root, media_dir=args.media_dir))
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)


def cmd_worker(args, store: Store, paths: Paths) -> None:
    from .app.config import Settings  # the app needs none of the web app's extras
    from .app.worker import run_worker

    settings = Settings.from_env(data_dir=paths.root, media_dir=args.media_dir)
    if args.workers:
        settings.workers = args.workers
    store.close()  # jobs open their own
    sys.exit(run_worker(settings, parent=args.parent, ready_fd=args.ready_fd))


def cmd_backup(args, store: Store, paths: Paths) -> None:
    """Copy the database while it is in use; a plain file copy can catch it half-written."""
    import sqlite3

    dest = Path(args.dest)
    if dest.exists() and not args.force:
        sys.exit(f"podblock: {dest} already exists; pass --force to replace it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.unlink(missing_ok=True)
    target = sqlite3.connect(tmp)
    try:
        store.db.backup(target)
    finally:
        target.close()
    tmp.replace(dest)
    print(f"Backed up {paths.db} to {dest} ({dest.stat().st_size / 1e6:.1f} MB).")


def cmd_api_key(args, store: Store, paths: Paths) -> None:
    from .app import accounts
    from .app.db import connect, prepare

    prepare(paths.db)
    db = connect(paths.db)
    try:
        print(accounts.new_api_key(db))
    finally:
        db.close()
    if os.environ.get("PODBLOCK_API_KEY"):
        print("podblock: PODBLOCK_API_KEY is set, and replaces this key when the server next starts",
              file=sys.stderr)


def open_store(args, paths: Paths) -> Store:
    """Open the store, checking any fingerprint flags against its setting."""
    flags = {k: v for k, v in (("high_band", args.high_band), ("split_bands", args.split_bands))
             if v is not None}
    if not paths.db.exists():
        return Store(paths.db, FingerprintConfig(**flags))
    store = Store(paths.db)
    clash = [k for k, v in flags.items() if getattr(store.config, k) != v]
    if clash:
        store.close()
        raise StoreError(f"this store uses '{store.config.name}' fingerprints, which "
                         f"--{'' if flags[clash[0]] else 'no-'}{clash[0].replace('_', '-')} does not fit; "
                         "use a new --data-dir")
    return store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="podblock", description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("PODBLOCK_DATA_DIR", "podblock-data")),
                        help="where the database, fingerprints and audio live "
                             "(default: $PODBLOCK_DATA_DIR, else %(default)s)")
    parser.add_argument("--high-band", action=argparse.BooleanOptionalAction, default=None,
                        help="also fingerprint 4-8 kHz, where music under speech shows best "
                             "(only when creating a store; default on)")
    parser.add_argument("--split-bands", action=argparse.BooleanOptionalAction, default=None,
                        help="pick peaks in each frequency band separately "
                             "(only when creating a store; default off)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def matching_args(p):
        p.add_argument("--min-length", type=float, default=MatchParams.min_length_sec,
                       help="shortest common block, in seconds; blocks under 10 s must also "
                            "occur at least 3 times (default: %(default)s)")
        p.add_argument("--min-hits", type=int, default=MatchParams.min_hits_per_bin,
                       help="matching hashes per second needed to call audio shared "
                            "(default: %(default)s)")

    def comparison_args(p):
        p.add_argument("--compare-same", type=int, default=COMPARE_SAME, metavar="N",
                       help="compare each episode with the N same-show episodes closest in publish "
                            "date (default: %(default)s)")
        p.add_argument("--compare-other", type=int, default=COMPARE_OTHER, metavar="N",
                       help="...and with the N closest of each other show, published within "
                            f"{OTHER_WINDOW_DAYS:.0f} days, for network ads (default: %(default)s)")

    p = sub.add_parser("analyze", help="fingerprint feeds or files and store their common blocks")
    p.add_argument("sources", nargs="+", help="RSS feed URLs and/or local audio files")
    p.add_argument("-n", "--episodes", type=int, default=5,
                   help="newest episodes per feed (default: %(default)s)")
    p.add_argument("--podcast", help="podcast name for local files")
    p.add_argument("--delete-audio", action="store_true", help="delete audio once fingerprinted")
    p.add_argument("--no-history", action="store_true",
                   help="compare only these episodes, not ones analyzed earlier")
    matching_args(p)
    comparison_args(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="list the repeated sections in one episode")
    p.add_argument("target", nargs="?", help="stored episode id, audio file or audio URL")
    p.add_argument("--feed", help="take the episode from this feed instead")
    p.add_argument("--index", type=int, default=0, help="with --feed: 0 = newest episode (default: 0)")
    p.add_argument("--podcast", help="for a file or URL: which podcast's intro/outro/show blocks apply")
    p.add_argument("--neighbors", type=int, default=0, metavar="K",
                   help="first fetch the K nearest episodes in the feed and look for new repeats")
    p.add_argument("--fill-gaps", type=float, default=0.0, metavar="SEC",
                   help="take in up to SEC seconds of unique audio between repeats (default: 0)")
    p.add_argument("--outro-bed", action="store_true",
                   help="look for an outro read over a music bed, even if the podcast setting is off")
    p.add_argument("--min-section", type=float, default=MIN_SECTION_SEC, metavar="SEC",
                   help="leave out sections shorter than this, unless they are breaks, intros, outros, "
                        "show blocks or typed by hand (default: %(default)s; 0 lists them all)")
    p.add_argument("--detail", action="store_true", help="list the parts of each section")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--cached", action="store_true",
                   help="print the saved report for a stored episode, if there is one, without redoing it")
    matching_args(p)
    comparison_args(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("list", help="list stored common blocks")
    p.add_argument("--podcast", help="only blocks found in this podcast (id, name or feed URL)")
    p.add_argument("--type", choices=TYPES, help="only blocks of this type")
    p.set_defaults(func=cmd_list)

    sub.add_parser("podcasts", help="list podcasts in the store").set_defaults(func=cmd_podcasts)

    p = sub.add_parser("podcast", help="show or change a podcast's settings")
    p.add_argument("podcast", help="podcast id, name or feed URL")
    p.add_argument("--outro-bed", action=argparse.BooleanOptionalAction, default=None,
                   help="its episodes end with talk over a music bed (patron names, plugs); "
                        "reports then run that stretch to the end as outro")
    p.set_defaults(func=cmd_podcast)

    p = sub.add_parser("breaks", help="show the ad breaks found in stored episodes")
    p.add_argument("episode_ids", type=int, nargs="*", help="episode ids (default: all)")
    p.set_defaults(func=cmd_breaks)

    p = sub.add_parser("show", help="show where a block occurs")
    p.add_argument("segment_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("type", help="set a block's type; intro, outro and show apply to its own "
                                    "podcast only, the rest to every podcast; ignored marks a false "
                                    "positive, which is then never reported")
    p.add_argument("segment_id", type=int)
    p.add_argument("type", choices=[*TYPES, "auto"], help="'auto' hands it back to automatic tagging")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("label", help="name a block, e.g. 'mattress ad'")
    p.add_argument("segment_id", type=int)
    p.add_argument("label", nargs="?", default="")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("export", help="cut a block out of its source episode")
    p.add_argument("segment_id", type=int)
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("prune", help="clear out old audio, fingerprints and blocks")
    p.add_argument("--audio-days", type=float, default=0.0,
                   help="delete downloaded audio older than this; only export needs it (default: 0)")
    p.add_argument("--fingerprint-days", type=float, default=30.0,
                   help="delete fingerprints older than this (default: %(default)s)")
    p.add_argument("--block-days", type=float, default=30.0,
                   help="drop blocks not heard for this long, unless typed by hand (default: %(default)s)")
    p.add_argument("--bumper-days", type=float, default=365.0,
                   help="drop bumpers not heard for this long (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="only say what would go")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("delete", help="remove a block from the store")
    p.add_argument("segment_id", type=int)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: %(default)s)")
    p.add_argument("--port", type=int, default=8000, help="(default: %(default)s)")
    p.add_argument("--media-dir", type=Path, default=None,
                   help="where episode audio lives (default: $PODBLOCK_MEDIA_DIR, else DATA_DIR/media)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="run the web app's jobs, feed refreshes and clean-up; `serve` starts one "
                                      "itself unless PODBLOCK_JOBS is threads or none")
    p.add_argument("--media-dir", type=Path, default=None,
                   help="where episode audio lives (default: $PODBLOCK_MEDIA_DIR, else DATA_DIR/media)")
    p.add_argument("--workers", type=int, default=None,
                   help="threads running jobs (default: $PODBLOCK_WORKERS, else 1)")
    p.add_argument("--parent", type=int, default=None, help=argparse.SUPPRESS)  # the web app that started it
    p.add_argument("--ready-fd", type=int, default=None, help=argparse.SUPPRESS)  # where to say it is running
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("backup", help="copy the database to a file, safely, even while the web app runs")
    p.add_argument("dest", help="file to write, e.g. /data/backup.sqlite")
    p.add_argument("--force", action="store_true", help="replace the file if it exists")
    p.set_defaults(func=cmd_backup)

    sub.add_parser("api-key", help="make a new login key for the web app, replacing the old one and "
                                   "logging everyone out").set_defaults(func=cmd_api_key)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    paths = Paths(args.data_dir)
    try:
        store = open_store(args, paths)
    except StoreError as e:
        sys.exit(f"podblock: {e}")
    try:
        args.func(args, store, paths)
    except BrokenPipeError:  # output piped into something that stopped reading, like head
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    finally:
        store.close()


if __name__ == "__main__":
    main()
