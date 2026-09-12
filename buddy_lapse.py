#!/usr/bin/env python3
"""
buddy-lapse: turn a folder of timestamped photos into one or more timelapse
.mp4 videos, automatically splitting into separate videos wherever there's a
big gap in time (e.g. different days / different sessions on an SD card).

Usage:
    python buddy_lapse.py <input_dir> -o <output_dir> [options]

Requires ffmpeg to be installed and on PATH (or pass --ffmpeg <path>).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ExifTags
except ImportError:
    Image = None
    ExifTags = None

DEFAULT_EXTENSIONS = ["jpg", "jpeg", "png", "bmp", "tif", "tiff"]
CACHE_FILENAME = ".buddy-lapse-cache.json"
_SOFTWARE_CODECS = frozenset({"libx264", "libx265"})

# EXIF tag ids for date fields, in priority order.
_EXIF_DATE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime


@dataclass
class Frame:
    path: Path
    ts: datetime


@dataclass
class Session:
    frames: list[Frame]

    @property
    def start(self) -> datetime:
        return self.frames[0].ts

    @property
    def end(self) -> datetime:
        return self.frames[-1].ts

    @property
    def n(self) -> int:
        return len(self.frames)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def default_workers() -> int:
    return min(32, (os.cpu_count() or 4) + 4)


def discover_images(input_dir: Path, extensions: list[str], recursive: bool) -> list[Path]:
    exts = {e.lower().lstrip(".") for e in extensions}
    # One walk, matching on the lowercased suffix, so every case variant is
    # caught (.jpg, .JPG, .Jpg) on case-sensitive filesystems too. Sorted so
    # that photos sharing a timestamp always land in the same frame order.
    pattern = "**/*" if recursive else "*"
    files = [
        p for p in input_dir.glob(pattern)
        if p.suffix.lower().lstrip(".") in exts and p.is_file()
    ]
    files.sort()
    return files


def get_exif_timestamp(path: Path) -> datetime | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            for tag_id in _EXIF_DATE_TAGS:
                raw = exif.get(tag_id)
                if raw:
                    # EXIF format: "YYYY:MM:DD HH:MM:SS"
                    try:
                        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        continue
    except Exception:
        return None
    return None


def get_timestamp(path: Path) -> tuple[datetime, str]:
    """Returns (timestamp, source) where source is 'exif' or 'mtime'."""
    exif_ts = get_exif_timestamp(path)
    if exif_ts is not None:
        return exif_ts, "exif"
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def load_timestamp_cache(cache_path: Path) -> dict:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_timestamp_cache(cache_path: Path, cache: dict, create_dirs: bool = True) -> bool:
    """Write the cache. Returns False if it was skipped.

    With create_dirs=False the write is skipped rather than creating the parent
    directory, which keeps --dry-run from materialising an output folder.
    """
    if not cache_path.parent.is_dir():
        if not create_dirs:
            return False
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    return True


def _cache_lookup(
    path: Path,
    cache: dict,
) -> tuple[datetime, str] | None:
    key = str(path.resolve())
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    try:
        st = path.stat()
        if entry.get("mtime_ns") != st.st_mtime_ns or entry.get("size") != st.st_size:
            return None
        ts = datetime.fromisoformat(entry["ts_iso"])
        source = entry.get("source", "mtime")
        if source not in ("exif", "mtime"):
            return None
        return ts, source
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _cache_store(path: Path, ts: datetime, source: str, cache: dict) -> None:
    try:
        st = path.stat()
        cache[str(path.resolve())] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "ts_iso": ts.isoformat(sep=" "),
            "source": source,
        }
    except OSError:
        pass


def collect_timestamps(
    paths: list[Path],
    workers: int,
    cache: dict | None,
) -> tuple[list[Frame], int, int, dict]:
    """Read timestamps in parallel.

    Returns (frames, exif_count, cache_hits, fresh_cache). Frames come back in
    the order of `paths` rather than the order workers happen to finish, so the
    frame order is reproducible. fresh_cache holds an entry only for the paths
    seen this run, which drops entries for photos that no longer exist instead
    of letting them pile up.
    """
    total = len(paths)
    # Cap progress output at ~20 updates, and stay quiet on small batches where
    # one line per photo is just noise.
    progress_every = max(1, total // 20)
    show_progress = total >= 200

    def resolve_one(path: Path) -> tuple[datetime, str, bool]:
        # Cache is read-only while workers run; fresh_cache is built on the
        # main thread once they're done.
        if cache is not None:
            hit = _cache_lookup(path, cache)
            if hit is not None:
                return hit[0], hit[1], True
        ts, source = get_timestamp(path)
        return ts, source, False

    results: list[tuple[datetime, str, bool]] = [(datetime.min, "mtime", False)] * total
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(resolve_one, p): i for i, p in enumerate(paths)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
            done += 1
            if show_progress and (done == total or done % progress_every == 0):
                print(f"  ... {done}/{total} timestamps", flush=True)

    frames: list[Frame] = []
    exif_count = 0
    cache_hits = 0
    fresh_cache: dict = {}
    for path, (ts, source, from_cache) in zip(paths, results):
        if from_cache:
            cache_hits += 1
        if source == "exif":
            exif_count += 1
        if cache is not None:
            _cache_store(path, ts, source, fresh_cache)
        frames.append(Frame(path=path, ts=ts))

    return frames, exif_count, cache_hits, fresh_cache


def build_sessions(
    frames: list[Frame],
    gap_seconds: float | None,
    gap_multiplier: float,
    min_gap_floor: float,
) -> tuple[list[Session], float, float]:
    """Groups sorted frames into sessions by inter-photo time gaps.

    Returns (sessions, median_interval_used, threshold_used).
    """
    if not frames:
        return [], 0.0, 0.0

    deltas = [
        (frames[i].ts - frames[i - 1].ts).total_seconds()
        for i in range(1, len(frames))
    ]
    positive_deltas = [d for d in deltas if d > 0]
    median_interval = statistics.median(positive_deltas) if positive_deltas else 0.0

    if gap_seconds is not None:
        threshold = gap_seconds
    else:
        threshold = max(median_interval * gap_multiplier, min_gap_floor)

    sessions: list[list[Frame]] = [[frames[0]]]
    for i in range(1, len(frames)):
        delta = (frames[i].ts - frames[i - 1].ts).total_seconds()
        if delta > threshold:
            sessions.append([])
        sessions[-1].append(frames[i])

    return [Session(s) for s in sessions], median_interval, threshold


def escape_concat_path(path: Path) -> str:
    # ffmpeg concat demuxer: paths go inside single quotes; forward slashes
    # work fine on Windows and sidestep backslash-escaping headaches.
    s = str(path.resolve()).replace("\\", "/")
    s = s.replace("'", "'\\''")
    return s


def write_concat_file(session: Session, fps: float, list_path: Path) -> None:
    duration_line = f"duration {1.0 / fps:.6f}"
    with list_path.open("w", encoding="utf-8", newline="\n") as f:
        last_escaped = ""
        for frame in session.frames:
            last_escaped = escape_concat_path(frame.path)
            f.write(f"file '{last_escaped}'\n")
            f.write(f"{duration_line}\n")
        # ffmpeg's concat demuxer ignores the duration on the final entry, so the
        # last file must be repeated without a duration line to display fully.
        f.write(f"file '{last_escaped}'\n")


def run_ffmpeg(
    list_path: Path,
    output_path: Path,
    fps: float,
    codec: str,
    crf: int,
    preset: str | None,
    threads: int,
    ffmpeg_bin: str,
    overwrite: bool,
) -> tuple[bool, str]:
    if output_path.exists() and not overwrite:
        return False, f"skipped (already exists: {output_path})"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-fps_mode", "cfr",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:v", codec,
        "-threads", str(threads),
    ]
    if codec in _SOFTWARE_CODECS:
        cmd.extend(["-preset", preset or "medium", "-crf", str(crf)])
    elif preset:
        # Hardware encoders use different preset names (e.g. nvenc p1–p7).
        cmd.extend(["-preset", preset])
    cmd.append(str(output_path))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr[-2000:]
    return True, "ok"


def format_name(template: str, session: Session, index: int) -> str:
    return template.format(
        start=session.start.strftime("%Y%m%d_%H%M%S"),
        end=session.end.strftime("%Y%m%d_%H%M%S"),
        n=session.n,
        index=index,
    )


def encode_session(
    index: int,
    session: Session,
    name_template: str,
    output_dir: Path,
    lists_dir: Path | None,
    fps: float,
    codec: str,
    crf: int,
    preset: str | None,
    threads: int,
    ffmpeg_bin: str,
    overwrite: bool,
) -> tuple[int, str, Path, bool, str, int]:
    name = format_name(name_template, session, index)
    output_path = output_dir / name

    if lists_dir is not None:
        list_path = lists_dir / f"{name}.txt"
        keep_list = True
    else:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmp.close()
        list_path = Path(tmp.name)
        keep_list = False

    write_concat_file(session, fps, list_path)
    try:
        ok, msg = run_ffmpeg(
            list_path, output_path, fps, codec, crf, preset, threads,
            ffmpeg_bin, overwrite,
        )
    finally:
        if not keep_list and list_path.exists():
            list_path.unlink()

    return index, name, output_path, ok, msg, session.n


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build timelapse .mp4 videos from a folder of timestamped photos, "
        "splitting into separate videos wherever there's a large time gap.",
    )
    p.add_argument("input_dir", type=Path, help="Folder to scan for photos (e.g. an SD card).")
    p.add_argument("-o", "--output", type=Path, default=Path("./output"),
                    help="Folder to write .mp4 files into (default: ./output).")
    p.add_argument("--fps", type=float, default=24.0, help="Output video frame rate (default: 24).")

    p.add_argument("--gap-seconds", type=float, default=None,
                    help="Fixed session-split threshold in seconds. If omitted, it's "
                    "auto-detected from the typical interval between photos.")
    p.add_argument("--gap-multiplier", type=float, default=6.0,
                    help="When auto-detecting, split when a gap exceeds this many times "
                    "the median interval between photos (default: 6).")
    p.add_argument("--min-gap-floor", type=float, default=10.0,
                    help="Minimum split threshold in seconds regardless of detected "
                    "interval, to avoid over-splitting bursts (default: 10).")
    p.add_argument("--min-frames", type=int, default=3,
                    help="Skip sessions with fewer than this many frames (default: 3).")

    p.add_argument("--ext", type=str, default=",".join(DEFAULT_EXTENSIONS),
                    help=f"Comma-separated file extensions to include (default: {','.join(DEFAULT_EXTENSIONS)}).")
    p.add_argument("--no-recursive", action="store_true", help="Don't scan subfolders.")

    p.add_argument("--workers", type=int, default=None,
                    help="Parallel workers for reading timestamps "
                    f"(default: {default_workers()}).")
    p.add_argument("--jobs", type=int, default=1,
                    help="Number of concurrent ffmpeg encodes (default: 1). "
                    "Use 2+ when photos are on a fast local disk.")
    p.add_argument("--cache", type=Path, default=None,
                    help="Timestamp cache file (default: <output>/.buddy-lapse-cache.json).")
    p.add_argument("--no-cache", action="store_true",
                    help="Disable the timestamp cache.")

    p.add_argument("--codec", type=str, default="libx264",
                    help="ffmpeg video codec (default: libx264). "
                    "Hardware options include h264_nvenc, h264_qsv, h264_amf, "
                    "h264_videotoolbox.")
    p.add_argument("--crf", type=int, default=18,
                    help="ffmpeg quality for libx264/libx265 (lower = better, default: 18). "
                    "Ignored for hardware codecs.")
    p.add_argument("--preset", type=str, default=None,
                    help="ffmpeg -preset (default: medium for libx264/libx265). "
                    "For speed try veryfast; for h264_nvenc try p4.")
    p.add_argument("--threads", type=int, default=0,
                    help="ffmpeg -threads (default: 0 = auto).")
    p.add_argument("--ffmpeg", type=str, default="ffmpeg", help="Path to the ffmpeg binary.")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")

    p.add_argument("--name-template", type=str, default="{start}_{n}frames.mp4",
                    help="Output filename template. Placeholders: {start} {end} {n} {index}.")

    p.add_argument("--dry-run", action="store_true",
                    help="Only report detected sessions; don't run ffmpeg.")
    p.add_argument("--json-report", type=Path, default=None,
                    help="Optional path to write a JSON summary of detected sessions.")
    p.add_argument("--keep-lists", action="store_true",
                    help="Keep the intermediate ffmpeg concat list files (saved next to output).")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.input_dir.is_dir():
        print(f"error: input dir not found: {args.input_dir}", file=sys.stderr)
        return 1

    if args.workers is not None and args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 1
    if args.jobs < 1:
        print("error: --jobs must be >= 1", file=sys.stderr)
        return 1

    if Image is None:
        print("warning: Pillow is not installed, so EXIF timestamps can't be read. "
              "Falling back to file modification time for all photos. "
              "Install with: pip install Pillow", file=sys.stderr)

    extensions = [e.strip() for e in args.ext.split(",") if e.strip()]
    paths = discover_images(args.input_dir, extensions, recursive=not args.no_recursive)
    if not paths:
        print(f"error: no matching photos found in {args.input_dir} (extensions: {extensions})",
              file=sys.stderr)
        return 1

    workers = args.workers if args.workers is not None else default_workers()
    use_cache = not args.no_cache
    cache_path = args.cache if args.cache is not None else (args.output / CACHE_FILENAME)
    cache: dict | None = load_timestamp_cache(cache_path) if use_cache else None

    print(f"Found {len(paths)} photo(s). Reading timestamps "
          f"({workers} worker{'s' if workers != 1 else ''}"
          f"{', cache on' if use_cache else ', cache off'})...")
    frames, exif_count, cache_hits, fresh_cache = collect_timestamps(paths, workers, cache)
    # Tie-break on path: EXIF timestamps only have second resolution, so burst
    # shots share one, and sorting on ts alone would leave their order up to
    # directory iteration.
    frames.sort(key=lambda f: (f.ts, f.path))

    if use_cache and cache is not None:
        # A dry run must not create the output directory, so skip the write if
        # the cache's folder isn't there yet.
        wrote = save_timestamp_cache(cache_path, fresh_cache, create_dirs=not args.dry_run)
        if wrote:
            print(f"  Cache: {cache_hits}/{len(frames)} hit(s); wrote {cache_path}")
        else:
            print(f"  Cache: {cache_hits}/{len(frames)} hit(s); not written "
                  f"(dry run, and {cache_path.parent} doesn't exist yet)")

    print(f"  {exif_count}/{len(frames)} timestamps came from EXIF; "
          f"{len(frames) - exif_count} fell back to file modification time.")

    sessions, median_interval, threshold = build_sessions(
        frames, args.gap_seconds, args.gap_multiplier, args.min_gap_floor
    )

    print(f"\nMedian interval between photos: {median_interval:.2f}s")
    print(f"Session-split threshold: {threshold:.2f}s "
          f"({'fixed' if args.gap_seconds is not None else f'auto = median x {args.gap_multiplier}'})")
    print(f"Detected {len(sessions)} session(s):\n")

    kept_sessions = []
    for i, s in enumerate(sessions, start=1):
        skip = s.n < args.min_frames
        status = "SKIP (too few frames)" if skip else "ok"
        print(f"  [{i}] {s.start} -> {s.end}  "
              f"({s.n} frames, {s.duration_seconds:.0f}s span)  {status}")
        if not skip:
            kept_sessions.append(s)

    if args.json_report:
        report = [
            {
                "index": i,
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "n_frames": s.n,
                "duration_seconds": s.duration_seconds,
                "files": [str(f.path) for f in s.frames],
            }
            for i, s in enumerate(sessions, start=1)
        ]
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nWrote JSON report to {args.json_report}")

    if args.dry_run:
        print("\nDry run: no videos were created.")
        return 0

    if not kept_sessions:
        print("\nNo sessions met --min-frames; nothing to encode.")
        return 0

    args.output.mkdir(parents=True, exist_ok=True)
    lists_dir: Path | None = None
    if args.keep_lists:
        lists_dir = args.output / "_concat_lists"
        lists_dir.mkdir(parents=True, exist_ok=True)

    jobs = min(args.jobs, len(kept_sessions))
    print(f"\nEncoding {len(kept_sessions)} video(s) to {args.output} at {args.fps} fps "
          f"({jobs} concurrent job{'s' if jobs != 1 else ''})...\n")

    failures = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [
            pool.submit(
                encode_session,
                i,
                session,
                args.name_template,
                args.output,
                lists_dir,
                args.fps,
                args.codec,
                args.crf,
                args.preset,
                args.threads,
                args.ffmpeg,
                args.overwrite,
            )
            for i, session in enumerate(kept_sessions, start=1)
        ]
        # Report each session as it finishes rather than after the whole batch,
        # so a long encode isn't silent. With --jobs 2+ these arrive out of
        # order; the [index] matches the session list printed above.
        for fut in as_completed(futures):
            index, name, output_path, ok, msg, n_frames = fut.result()
            if ok:
                print(f"  [{index}/{len(kept_sessions)}] wrote {output_path} "
                      f"({n_frames} frames)", flush=True)
            else:
                failures += 1
                print(f"  [{index}/{len(kept_sessions)}] FAILED: {name}\n    {msg}", flush=True)

    if failures:
        print(f"\nDone with {failures} failure(s).")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
