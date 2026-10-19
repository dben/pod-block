"""Find audio blocks that repeat across podcast episodes."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from .feed import download
from .matcher import MatchParams
from .pipeline import Paths, find_common_blocks, ingest_feed, ingest_file, scan_audio
from .store import Store


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def params_from(args) -> MatchParams:
    return MatchParams(min_length_sec=args.min_length, min_hits_per_bin=args.min_hits)


def cmd_analyze(args, store: Store, paths: Paths) -> None:
    params = params_from(args)
    ids = []
    for source in args.sources:
        if Path(source).exists():
            ids.append(ingest_file(store, paths, Path(source)))
        else:
            ids += ingest_feed(store, paths, source, args.episodes, keep_audio=not args.delete_audio)
    added = find_common_blocks(store, ids, params, compare_with_history=not args.no_history)
    print(f"Added {added} new common block(s).")
    print_segments(store)


def cmd_scan(args, store: Store, paths: Paths) -> None:
    params = params_from(args)
    target = Path(args.target)
    if args.record:
        if not target.exists():
            sys.exit("--record needs a local file; download it first.")
        ep_id = ingest_file(store, paths, target)
        find_common_blocks(store, [ep_id], params)
        matches = store.occurrences_for_episode(ep_id)
        duration = store.episode(ep_id)["duration"]
    else:
        with tempfile.TemporaryDirectory() as tmp:
            if not target.exists():
                target = download(args.target, Path(tmp) / "audio")
            fp, matches = scan_audio(store, target, params)
        duration = fp.duration

    labels = {r["id"]: r["label"] for r in store.segment_summary()}
    print(f"Duration {fmt_time(duration)}, {len(matches)} known block(s):")
    total = 0.0
    for m in matches:
        total += m.end - m.start
        label = f"  [{labels[m.segment_id]}]" if labels.get(m.segment_id) else ""
        print(f"  {fmt_time(m.start):>8} - {fmt_time(m.end):>8}  ({m.end - m.start:5.0f}s)  "
              f"segment #{m.segment_id} from {fmt_time(m.segment_start)}  hits={m.hits}{label}")
    if duration:
        print(f"Known blocks cover {fmt_time(total)} ({100 * total / duration:.1f}%).")


def print_segments(store: Store) -> None:
    rows = store.segment_summary()
    if not rows:
        print("No common blocks stored yet.")
        return
    print(f"{'id':>4} {'length':>7} {'episodes':>8} {'times':>5}  source")
    for r in rows:
        label = f"  [{r['label']}]" if r["label"] else ""
        print(f"{r['id']:>4} {fmt_time(r['duration']):>7} {r['episodes']:>8} {r['occurrences']:>5}  "
              f"{(r['source_title'] or '?')[:50]} @ {fmt_time(r['source_start'])}{label}")


def cmd_list(args, store: Store, paths: Paths) -> None:
    print_segments(store)


def cmd_show(args, store: Store, paths: Paths) -> None:
    seg = store.segment(args.segment_id)
    if seg is None:
        sys.exit(f"No segment #{args.segment_id}")
    print(f"Segment #{seg['id']}  {fmt_time(seg['source_end'] - seg['source_start'])}"
          f"  label={seg['label'] or '-'}")
    for o in store.occurrences_for_segment(args.segment_id):
        print(f"  {fmt_time(o['start']):>8} - {fmt_time(o['end']):>8}  {o['title']}")


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="podblock", description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("podblock-data"),
                        help="where the database, fingerprints and audio live (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def matching_args(p):
        p.add_argument("--min-length", type=float, default=10.0,
                       help="shortest common block to report, in seconds (default: %(default)s)")
        p.add_argument("--min-hits", type=int, default=3,
                       help="matching hashes per second needed to call audio shared (default: %(default)s)")

    p = sub.add_parser("analyze", help="fingerprint feeds or files and store their common blocks")
    p.add_argument("sources", nargs="+", help="RSS feed URLs and/or local audio files")
    p.add_argument("-n", "--episodes", type=int, default=5, help="newest episodes per feed (default: %(default)s)")
    p.add_argument("--delete-audio", action="store_true", help="delete audio once fingerprinted")
    p.add_argument("--no-history", action="store_true",
                   help="compare only these episodes, not ones analyzed earlier")
    matching_args(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("scan", help="check an audio file or URL against stored blocks")
    p.add_argument("target")
    p.add_argument("--record", action="store_true",
                   help="also add the file to the store and look for new common blocks")
    matching_args(p)
    p.set_defaults(func=cmd_scan)

    sub.add_parser("list", help="list stored common blocks").set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show where a block occurs")
    p.add_argument("segment_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("label", help="name a block, e.g. 'intro' or 'ad: mattress'")
    p.add_argument("segment_id", type=int)
    p.add_argument("label", nargs="?", default="")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("export", help="cut a block out of its source episode")
    p.add_argument("segment_id", type=int)
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("delete", help="remove a block from the store")
    p.add_argument("segment_id", type=int)
    p.set_defaults(func=cmd_delete)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    paths = Paths(args.data_dir)
    store = Store(paths.db)
    try:
        args.func(args, store, paths)
    finally:
        store.close()


if __name__ == "__main__":
    main()
