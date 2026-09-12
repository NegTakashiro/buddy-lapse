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
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ExifTags
except ImportError:
    Image = None
    ExifTags = None

DEFAULT_EXTENSIONS = ["jpg", "jpeg", "png", "bmp", "tif", "tiff"]

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


def discover_images(input_dir: Path, extensions: list[str], recursive: bool) -> list[Path]:
    exts = {e.lower().lstrip(".") for e in extensions}
    pattern = "**/*" if recursive else "*"
    files = []
    for p in input_dir.glob(pattern):
        if p.is_file() and p.suffix.lower().lstrip(".") in exts:
            files.append(p)
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
    frame_duration = 1.0 / fps
    lines = []
    for frame in session.frames:
        lines.append(f"file '{escape_concat_path(frame.path)}'")
        lines.append(f"duration {frame_duration:.6f}")
    # ffmpeg's concat demuxer ignores the duration on the final entry, so the
    # last file must be repeated without a duration line to display fully.
    lines.append(f"file '{escape_concat_path(session.frames[-1].path)}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")


def run_ffmpeg(
    list_path: Path,
    output_path: Path,
    fps: float,
    codec: str,
    crf: int,
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
        "-crf", str(crf),
        str(output_path),
    ]
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

    p.add_argument("--codec", type=str, default="libx264", help="ffmpeg video codec (default: libx264).")
    p.add_argument("--crf", type=int, default=18, help="ffmpeg quality (lower = better, default: 18).")
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

    print(f"Found {len(paths)} photo(s). Reading timestamps...")
    frames: list[Frame] = []
    exif_count = 0
    for path in paths:
        ts, source = get_timestamp(path)
        if source == "exif":
            exif_count += 1
        frames.append(Frame(path=path, ts=ts))
    frames.sort(key=lambda f: f.ts)
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
    lists_dir = args.output / "_concat_lists"
    if args.keep_lists:
        lists_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nEncoding {len(kept_sessions)} video(s) to {args.output} at {args.fps} fps...\n")
    failures = 0
    for i, session in enumerate(kept_sessions, start=1):
        name = format_name(args.name_template, session, i)
        output_path = args.output / name

        if args.keep_lists:
            list_path = lists_dir / f"{name}.txt"
        else:
            list_path = Path(tempfile.mktemp(suffix=".txt"))

        write_concat_file(session, args.fps, list_path)
        try:
            ok, msg = run_ffmpeg(
                list_path, output_path, args.fps, args.codec, args.crf,
                args.ffmpeg, args.overwrite,
            )
        finally:
            if not args.keep_lists and list_path.exists():
                list_path.unlink()

        if ok:
            print(f"  [{i}/{len(kept_sessions)}] wrote {output_path} ({session.n} frames)")
        else:
            failures += 1
            print(f"  [{i}/{len(kept_sessions)}] FAILED: {name}\n    {msg}")

    if failures:
        print(f"\nDone with {failures} failure(s).")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
