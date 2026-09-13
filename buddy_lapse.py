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
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

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
    key: str = ""  # timestamp-cache key (resolved path), reused by activity detection


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


# How print activity is detected
# ------------------------------
# Each photo is decoded by ffmpeg to a 32x18 thumbnail. For every frame we
# count the share of pixels whose brightness moved by more than _CHANGE_SIGMA
# deviations since the previous frame, after normalising each thumbnail's
# brightness and contrast. A moving toolhead or a growing part moves a solid
# chunk of pixels; an idle bed moves none, even while the room light, the sun,
# or the camera's auto-exposure changes, because those shift the whole image
# and normalisation cancels them.
#
# The camera also flips between colour and IR night mode, sometimes every few
# frames, and the two modes render the same scene with different tones. So a
# frame is only compared with the previous frame taken in the same mode.
#
# Checked against a hand-labelled 32-hour stretch of a real capture: every
# printing sample scored >= 0.012 and every idle sample exactly 0, including
# hours of mode flipping and moving sunlight that a JPEG byte-size signal
# counted as printing.

THUMB_W, THUMB_H = 32, 18
_THUMB_PIXELS = THUMB_W * THUMB_H
_IR_SATURATION = 4.0      # mean chroma distance from grey below this = IR mode
_CHANGE_SIGMA = 0.75      # a pixel counts as changed past this many deviations
_ACTIVITY_VERSION = 1     # bump when the score definition changes; stale caches recompute
_DECODE_CHUNK = 500       # photos per ffmpeg process

# Pauses inside a print (the toolhead working out of frame, a slow section)
# look idle too. Whether a gap is a pause or a plate change is decided by how
# different the scene is on either side of it. A short pause can tolerate a lot
# of difference, since the toolhead simply lands somewhere else; a longer gap
# only counts as the same print if the plate looks unchanged. Tuned on the same
# capture: in-print pauses of up to 17 minutes changed <= 0.04, while a 13-minute
# plate swap changed 0.28.
_SHORT_PAUSE_SECONDS = 600
_SHORT_PAUSE_MAX_CHANGE = 0.25
_LONG_PAUSE_MAX_CHANGE = 0.06


@dataclass
class Thumb:
    luma: bytes  # THUMB_W x THUMB_H greyscale
    ir: bool


def _run_thumbnail_ffmpeg(paths: list[Path], ffmpeg_bin: str) -> bytes | None:
    fd, name = tempfile.mkstemp(suffix=".txt")
    list_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for p in paths:
                f.write(f"file '{escape_concat_path(p)}'\n")
        cmd = [
            ffmpeg_bin, "-v", "error",
            # Decode JPEGs at 1/8 scale straight from the DCT; other formats ignore it.
            "-lowres", "3",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            # One output frame per readable input. The default timing would put a
            # duplicate of a neighbour in place of an unreadable photo, hiding it.
            "-fps_mode", "passthrough",
            "-vf", f"scale={THUMB_W}:{THUMB_H}:flags=area,format=yuv444p",
            "-f", "rawvideo", "pipe:1",
        ]
        return subprocess.run(cmd, capture_output=True).stdout
    except OSError:
        return None
    finally:
        list_path.unlink(missing_ok=True)


def _decode_batch(paths: list[Path], ffmpeg_bin: str) -> list[Thumb | None]:
    """Thumbnails for `paths` in order; None for any photo ffmpeg can't read."""
    frame_bytes = 3 * _THUMB_PIXELS
    raw = _run_thumbnail_ffmpeg(paths, ffmpeg_bin)
    if raw is None or len(raw) != len(paths) * frame_bytes:
        # A frame went missing, so the output can no longer be lined up with the
        # input. Split and retry until the unreadable photo is isolated.
        if len(paths) == 1:
            return [None]
        mid = len(paths) // 2
        return _decode_batch(paths[:mid], ffmpeg_bin) + _decode_batch(paths[mid:], ffmpeg_bin)

    thumbs: list[Thumb | None] = []
    for i in range(len(paths)):
        o = i * frame_bytes
        chroma = raw[o + _THUMB_PIXELS:o + frame_bytes]
        saturation = sum(abs(c - 128) for c in chroma) / _THUMB_PIXELS
        thumbs.append(Thumb(luma=raw[o:o + _THUMB_PIXELS], ir=saturation < _IR_SATURATION))
    return thumbs


def decode_thumbnails(paths: list[Path], ffmpeg_bin: str, workers: int) -> list[Thumb | None]:
    """Decode many photos to thumbnails, several ffmpeg processes at a time."""
    # ffmpeg's concat demuxer decodes every entry with the first file's codec,
    # so a PNG in a batch of JPEGs would silently drop frames. Batch by format.
    by_format: dict[str, list[int]] = {}
    for i, p in enumerate(paths):
        ext = p.suffix.lower()
        by_format.setdefault(".jpg" if ext == ".jpeg" else ext, []).append(i)
    batches = [idxs[k:k + _DECODE_CHUNK]
               for idxs in by_format.values()
               for k in range(0, len(idxs), _DECODE_CHUNK)]

    out: list[Thumb | None] = [None] * len(paths)
    total, done = len(paths), 0
    step = max(1, total // 20)
    next_report = step
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_decode_batch, [paths[i] for i in b], ffmpeg_bin): b
                   for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            for i, thumb in zip(batch, fut.result()):
                out[i] = thumb
            done += len(batch)
            if total >= 200 and (done >= next_report or done == total):
                print(f"  ... {done:,}/{total:,} thumbnails", flush=True)
                next_report = (done // step + 1) * step
    return out


def _normalized(luma: bytes) -> list[float]:
    mean = sum(luma) / _THUMB_PIXELS
    dev = sum(abs(v - mean) for v in luma) / _THUMB_PIXELS or 1.0
    return [(v - mean) / dev for v in luma]


def _changed_fraction(a: list[float], b: list[float]) -> float:
    return sum(1 for x, y in zip(a, b) if abs(x - y) > _CHANGE_SIGMA) / _THUMB_PIXELS


def _key_hash(key: str) -> int:
    return zlib.crc32(key.encode("utf-8"))


class ActivityAnalyzer:
    """Per-frame change scores for a sorted list of frames, reusing the cache.

    A frame's score depends on the frame before it in the same camera mode, so
    a cached score is reused only while that neighbour is still the same photo.
    Re-running over an unchanged folder decodes nothing; when new photos
    arrive, only they and the frames they now follow get decoded.
    """

    def __init__(self, frames: list[Frame], cache: dict | None, ffmpeg_bin: str, workers: int):
        self.frames = frames
        self.ffmpeg_bin = ffmpeg_bin
        self.workers = workers
        self.thumbs: dict[int, Thumb] = {}
        self.decoded = 0
        n = len(frames)
        self.mode: list[int | None] = [None] * n    # 1 = IR, 0 = colour, -1 = unreadable
        self._entries = [cache.get(f.key) if cache is not None else None for f in frames]
        self._cached_score: list[float | None] = [None] * n
        self._cached_prev: list[int | None] = [None] * n
        for i, entry in enumerate(self._entries):
            act = entry.get("act") if isinstance(entry, dict) else None
            if (isinstance(act, list) and len(act) == 4 and act[0] == _ACTIVITY_VERSION
                    and act[1] in (-1, 0, 1)):
                self.mode[i], self._cached_score[i], self._cached_prev[i] = act[1], act[2], act[3]

    def decode(self, indices: list[int]) -> None:
        todo = sorted({i for i in indices if i not in self.thumbs and self.mode[i] != -1})
        if not todo:
            return
        if len(todo) >= 200:
            print(f"  Decoding {len(todo):,} thumbnail(s) for activity detection...", flush=True)
        results = decode_thumbnails([self.frames[i].path for i in todo], self.ffmpeg_bin, self.workers)
        for i, thumb in zip(todo, results):
            if thumb is None:
                self.mode[i] = -1
            else:
                self.thumbs[i] = thumb
                self.mode[i] = int(thumb.ir)
        self.decoded += len(todo)

    def scores(self) -> list[float]:
        n = len(self.frames)
        self.decode([i for i in range(n) if self.mode[i] is None])

        prev = [-1] * n
        last = {0: -1, 1: -1}
        for i in range(n):
            m = self.mode[i]
            if m in (0, 1):
                prev[i] = last[m]
                last[m] = i
        prev_hash = [-1 if p < 0 else _key_hash(self.frames[p].key) for p in prev]

        stale = [i for i in range(n) if self.mode[i] in (0, 1)
                 and (self._cached_score[i] is None or self._cached_prev[i] != prev_hash[i])]
        self.decode([j for i in stale for j in (i, prev[i]) if j >= 0])

        score = [float(s) if s is not None else 0.0 for s in self._cached_score]
        normals: dict[int, list[float]] = {}
        for i in stale:
            score[i] = 0.0
            p = prev[i]
            if p < 0 or i not in self.thumbs or p not in self.thumbs:
                continue
            if p not in normals:
                normals[p] = _normalized(self.thumbs[p].luma)
            normals[i] = _normalized(self.thumbs[i].luma)
            score[i] = _changed_fraction(normals[i], normals.pop(p))

        for i, entry in enumerate(self._entries):
            if isinstance(entry, dict) and self.mode[i] is not None:
                entry["act"] = [_ACTIVITY_VERSION, self.mode[i], score[i], prev_hash[i]]
        return score

    def scene_change(self, before: list[int], after: list[int]) -> float:
        """Smallest change between any frame in `before` and any in `after`."""
        self.decode(before + after)
        a = [_normalized(self.thumbs[i].luma) for i in before if i in self.thumbs]
        b = [_normalized(self.thumbs[i].luma) for i in after if i in self.thumbs]
        if not a or not b:
            return 1.0
        return min(_changed_fraction(x, y) for x in a for y in b)


def centered_median(values: list[float], window: int) -> list[float]:
    """Median over a window centred on each frame.

    A median ignores a lone spike, like a hand reaching past the camera, and
    centring the window keeps a print's start and end where they really are.
    """
    half = window // 2
    return [statistics.median(values[max(0, i - half):i + half + 1]) for i in range(len(values))]


def hysteresis_spans(signal: list[float], enter: float, leave: float) -> list[tuple[int, int]]:
    """[start, end] index spans where the signal is on.

    A span has to clear `enter` to start but only drop below `leave` to end, so
    a score hovering at the boundary doesn't flicker on and off.
    """
    spans: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(signal):
        if start is None and v >= enter:
            start = i
        elif start is not None and v < leave:
            spans.append((start, i - 1))
            start = None
    if start is not None:
        spans.append((start, len(signal) - 1))
    return spans


def _span_edge(span: tuple[int, int], at_end: bool) -> list[int]:
    """Up to 5 frames spread over the last (or first) ~30 frames of a span."""
    a, b = span
    if at_end:
        return list(range(b, max(a, b - 30) - 1, -6))[:5]
    return list(range(a, min(b, a + 30) + 1, 6))[:5]


def group_prints(
    frames: list[Frame],
    spans: list[tuple[int, int]],
    analyzer: ActivityAnalyzer,
    max_pause_seconds: float,
    min_print_seconds: float,
) -> tuple[list[list[tuple[int, int]]], int, int]:
    """Join active spans into prints. Returns (prints, pauses_bridged, plate_changes).

    Indices in `spans` refer to `frames`, which is the full sorted frame list
    the analyzer was built from.
    """
    if not spans:
        return [], 0, 0
    prints: list[list[tuple[int, int]]] = [[spans[0]]]
    bridged = plate_changes = 0
    for prev_span, span in zip(spans, spans[1:]):
        pause = (frames[span[0]].ts - frames[prev_span[1]].ts).total_seconds()
        if pause <= max_pause_seconds:
            change = analyzer.scene_change(_span_edge(prev_span, True), _span_edge(span, False))
            limit = (_SHORT_PAUSE_MAX_CHANGE if pause <= _SHORT_PAUSE_SECONDS
                     else _LONG_PAUSE_MAX_CHANGE)
            if change <= limit:
                prints[-1].append(span)
                bridged += 1
                continue
            plate_changes += 1
        prints.append([span])

    def active_seconds(p: list[tuple[int, int]]) -> float:
        # Time actually moving, not first-to-last: a few blips of glare spread
        # over a quarter of an hour shouldn't pass for a quarter-hour print.
        return sum((frames[b].ts - frames[a].ts).total_seconds() for a, b in p)

    kept = [p for p in prints if active_seconds(p) >= min_print_seconds]
    return kept, bridged, plate_changes


def report_activity(frames: list[Frame], smoothed: list[float], enter: float) -> None:
    """Print the activity score hour by hour, so thresholds can be eyeballed."""
    buckets: dict[datetime, list[float]] = {}
    for frame, value in zip(frames, smoothed):
        buckets.setdefault(frame.ts.replace(minute=0, second=0, microsecond=0), []).append(value)
    print("\nActivity by hour (score = share of the image moving; "
          f"% = frames at or above --activity-enter {enter:g}):")
    for hour in sorted(buckets):
        values = buckets[hour]
        mean = sum(values) / len(values)
        active = sum(1 for v in values if v >= enter) / len(values)
        bar = "#" * round(active * 40)
        print(f"  {hour:%Y-%m-%d %H:%M}  {len(values):4d}f  score {mean:.4f}  {active:4.0%}  {bar}")
    print()


def stride_for_target(n_frames: int, fps: float, target_seconds: float) -> int:
    """Frames to skip so n_frames plays for about target_seconds at fps."""
    wanted = max(1, round(target_seconds * fps))
    if n_frames <= wanted:
        return 1
    return math.ceil(n_frames / wanted)


def decimate(frames: list[Frame], stride: int) -> list[Frame]:
    """Keep every `stride`-th frame, always including the first and last.

    The last photo is the finished print, so it's worth keeping even when the
    stride would step past it.
    """
    if stride <= 1 or len(frames) <= 2:
        return frames
    kept = frames[::stride]
    if kept[-1] is not frames[-1]:
        kept.append(frames[-1])
    return kept


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
    key: str,
    cache: dict,
) -> tuple[datetime, str] | None:
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


def _cache_store(
    path: Path,
    key: str,
    ts: datetime,
    source: str,
    cache: dict,
    previous: dict | None = None,
) -> None:
    try:
        st = path.stat()
        entry = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "ts_iso": ts.isoformat(sep=" "),
            "source": source,
        }
        # Keep activity scores from a still-valid entry; they're costly to rebuild.
        if previous is not None and "act" in previous:
            entry["act"] = previous["act"]
        cache[key] = entry
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

    def resolve_one(path: Path) -> tuple[datetime, str, bool, str]:
        # Cache is read-only while workers run; fresh_cache is built on the
        # main thread once they're done.
        key = str(path.resolve())
        if cache is not None:
            hit = _cache_lookup(path, key, cache)
            if hit is not None:
                return hit[0], hit[1], True, key
        ts, source = get_timestamp(path)
        return ts, source, False, key

    results: list[tuple[datetime, str, bool, str]] = [(datetime.min, "mtime", False, "")] * total
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
    for path, (ts, source, from_cache, key) in zip(paths, results):
        if from_cache:
            cache_hits += 1
        if source == "exif":
            exif_count += 1
        if cache is not None:
            _cache_store(path, key, ts, source, fresh_cache,
                         previous=cache.get(key) if from_cache else None)
        frames.append(Frame(path=path, ts=ts, key=key))

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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class EncodeProgress:
    """Live progress across however many ffmpeg jobs are running.

    Encodes here run for tens of minutes, so the run has to say something
    while it works. Rewrites one line on a terminal; on a redirected stdout it
    falls back to an occasional full line so logs stay readable.
    """

    WIDTH = 78

    def __init__(self, total_frames: int, n_sessions: int, tty: bool):
        self.total_frames = max(1, total_frames)
        self.n_sessions = n_sessions
        self.tty = tty
        self.per_session: dict[int, int] = {}
        self.finished = 0
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_render = 0.0
        self.dirty = False

    def update(self, index: int, frames_done: int) -> None:
        with self.lock:
            self.per_session[index] = frames_done
            self._render()

    def complete(self, index: int, n_frames: int) -> None:
        with self.lock:
            self.per_session[index] = n_frames
            self.finished += 1

    def _render(self, force: bool = False) -> None:
        now = time.monotonic()
        interval = 0.5 if self.tty else 20.0
        if not force and now - self.last_render < interval:
            return
        self.last_render = now
        done = sum(self.per_session.values())
        elapsed = now - self.started
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (self.total_frames - done) / rate if rate > 0 else 0.0
        line = (f"  {done:,}/{self.total_frames:,} frames "
                f"({done / self.total_frames:.0%})  {rate:.0f} fps  "
                f"{self.finished}/{self.n_sessions} done  ETA {format_duration(eta)}")
        if self.tty:
            print("\r" + line.ljust(self.WIDTH)[:self.WIDTH], end="", flush=True)
            self.dirty = True
        else:
            print(line, flush=True)

    def clear_line(self) -> None:
        """Wipe the in-place line so a result can be printed under it."""
        with self.lock:
            if self.tty and self.dirty:
                print("\r" + " " * self.WIDTH + "\r", end="", flush=True)
                self.dirty = False


def quality_args(codec: str, cq: int) -> list[str]:
    """Map --cq onto whichever constant-quality flag `codec` actually takes.

    Hardware encoders each spell this differently, and ignore -crf entirely.
    Only the nvenc spelling is verified here; the others follow ffmpeg's docs.
    """
    if "nvenc" in codec:
        return ["-cq", str(cq)]
    if "qsv" in codec:
        return ["-global_quality", str(cq)]
    if "amf" in codec:
        return ["-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq)]
    if "videotoolbox" in codec:
        return ["-q:v", str(cq)]
    # Unknown encoder: -q:v is the most widely understood generic spelling.
    return ["-q:v", str(cq)]


def run_ffmpeg(
    list_path: Path,
    output_path: Path,
    fps: float,
    codec: str,
    crf: int,
    cq: int | None,
    preset: str | None,
    threads: int,
    ffmpeg_bin: str,
    overwrite: bool,
    on_progress: "Callable[[int], None] | None" = None,
) -> tuple[bool, str]:
    if output_path.exists() and not overwrite:
        return False, f"skipped (already exists: {output_path})"

    cmd = [
        ffmpeg_bin,
        "-y",
        # Machine-readable progress on stdout; -nostats drops the human version
        # that would otherwise fight with our own status line.
        "-progress", "pipe:1",
        "-nostats",
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
    else:
        if preset:
            # Hardware encoders use different preset names (e.g. nvenc p1–p7).
            cmd.extend(["-preset", preset])
        if cq is not None:
            cmd.extend(quality_args(codec, cq))
    cmd.append(str(output_path))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except FileNotFoundError:
        return False, f"ffmpeg not found: {ffmpeg_bin}"
    except OSError as exc:
        # Don't let one session's launch failure take down the whole batch.
        return False, f"could not run ffmpeg ({ffmpeg_bin}): {exc}"

    # Drain stderr on its own thread: a long encode can emit enough warnings to
    # fill the pipe buffer, which would deadlock us while we read stdout.
    stderr_chunks: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_chunks.append(line)

    pump = threading.Thread(target=drain, daemon=True)
    pump.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        if on_progress is not None and line.startswith("frame="):
            try:
                on_progress(int(line.split("=", 1)[1]))
            except ValueError:
                pass

    proc.wait()
    pump.join(timeout=5)
    if proc.returncode != 0:
        return False, "".join(stderr_chunks)[-2000:]
    return True, "ok"


def resolve_ffmpeg(ffmpeg_bin: str) -> str | None:
    """Return the resolved path to the ffmpeg binary, or None if it isn't there."""
    found = shutil.which(ffmpeg_bin)
    if found:
        return found
    # shutil.which misses an explicit path to an extensionless executable.
    p = Path(ffmpeg_bin)
    return str(p) if p.is_file() else None


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
    cq: int | None,
    preset: str | None,
    threads: int,
    ffmpeg_bin: str,
    overwrite: bool,
    progress: "EncodeProgress | None" = None,
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
            list_path, output_path, fps, codec, crf, cq, preset, threads,
            ffmpeg_bin, overwrite,
            on_progress=(None if progress is None
                         else lambda done: progress.update(index, done)),
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
                    help="Skip sessions with fewer than this many frames (default: 3). "
                    "Counted after --every-nth / --target-seconds thinning.")

    p.add_argument("--detect-activity", action="store_true",
                    help="Keep only the frames where the printer is actually working, "
                    "one video per print. For a camera that shoots continuously, this "
                    "is what removes the idle hours between prints. Needs ffmpeg, "
                    "even with --dry-run.")
    p.add_argument("--activity-enter", type=float, default=0.006, metavar="X",
                    help="Score (share of the image moving) at which a print is "
                    "considered started (default: 0.006). Idle scores 0.")
    p.add_argument("--activity-leave", type=float, default=0.003, metavar="X",
                    help="Score below which an in-progress print is considered paused "
                    "(default: 0.003). Must not exceed --activity-enter.")
    p.add_argument("--activity-window", type=int, default=31, metavar="N",
                    help="Frames to take the median score over (default: 31, about "
                    "5 minutes at one photo per 10s).")
    p.add_argument("--min-active-seconds", type=float, default=900.0, metavar="S",
                    help="Discard detected prints with less than this much time "
                    "actually moving (default: 900). Filters out someone reaching in "
                    "to clear the bed, or a moment of glare. Lower it if you run very "
                    "short prints.")
    p.add_argument("--min-idle-seconds", type=float, default=1800.0, metavar="S",
                    help="Longest pause that can still be part of the same print "
                    "(default: 1800). Pauses are cut from the video either way; this "
                    "only decides whether what follows is a new video. Within it, "
                    "the scene on each side decides.")
    p.add_argument("--activity-report", action="store_true",
                    help="Print the hourly activity score so the thresholds above can "
                    "be tuned to your camera. Best paired with --dry-run.")

    thin = p.add_mutually_exclusive_group()
    thin.add_argument("--every-nth", type=int, default=None, metavar="N",
                    help="Use only every Nth photo. The single biggest lever on both "
                    "file size and encode time: --every-nth 10 gives a video a tenth "
                    "as long for roughly a tenth the size.")
    thin.add_argument("--target-seconds", type=float, default=None, metavar="SECONDS",
                    help="Thin each session automatically so its video runs about this "
                    "long at --fps. Sessions already shorter are left alone.")

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
                    "Ignored for hardware codecs; use --cq for those.")
    p.add_argument("--cq", type=int, default=None,
                    help="Constant-quality level for hardware codecs (lower = better; "
                    "try 23-28). Becomes -cq for nvenc, -global_quality for qsv, "
                    "-qp_i/-qp_p for amf, -q:v for videotoolbox. Without it the "
                    "encoder falls back to its own default bitrate.")
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
    if args.every_nth is not None and args.every_nth < 1:
        print("error: --every-nth must be >= 1", file=sys.stderr)
        return 1
    if args.target_seconds is not None and args.target_seconds <= 0:
        print("error: --target-seconds must be > 0", file=sys.stderr)
        return 1
    if args.activity_leave > args.activity_enter:
        print("error: --activity-leave must be <= --activity-enter "
              f"(got {args.activity_leave} > {args.activity_enter})", file=sys.stderr)
        return 1
    if args.activity_window < 1:
        print("error: --activity-window must be >= 1", file=sys.stderr)
        return 1

    software = args.codec in _SOFTWARE_CODECS
    if args.cq is not None and software:
        print(f"error: --cq applies to hardware codecs; {args.codec} uses --crf",
              file=sys.stderr)
        return 1
    if args.cq is None and not software and not args.dry_run:
        print(f"warning: {args.codec} ignores --crf and no --cq was given, so quality is "
              "whatever the encoder defaults to (often a low fixed bitrate). "
              "Pass --cq 23-28 to control it.", file=sys.stderr)

    # Check for ffmpeg up front so a missing binary fails in a second, rather
    # than after a full scan of the card. A dry run doesn't encode, but activity
    # detection still decodes thumbnails with it.
    needs_activity = args.detect_activity or args.activity_report
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    if (not args.dry_run or needs_activity) and ffmpeg_path is None:
        print(f"error: ffmpeg not found: {args.ffmpeg}\n"
              "  Install it and make sure it's on PATH, or point --ffmpeg at the binary.\n"
              "  Windows: winget install ffmpeg   (then restart your terminal)\n"
              "  macOS:   brew install ffmpeg\n"
              "  Linux:   sudo apt install ffmpeg\n"
              "  Already installed? Your terminal may still have the old PATH.\n"
              "  --dry-run without --detect-activity checks grouping without ffmpeg.",
              file=sys.stderr)
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

    if use_cache:
        print(f"  Cache: {cache_hits}/{len(frames)} timestamp hit(s)")

    def save_cache() -> None:
        if not use_cache:
            return
        # A dry run must not create the output directory, so skip the write if
        # the cache's folder isn't there yet.
        if save_timestamp_cache(cache_path, fresh_cache, create_dirs=not args.dry_run):
            print(f"  Wrote cache {cache_path}")
        else:
            print(f"  Cache not written (dry run, and {cache_path.parent} doesn't exist yet)")

    print(f"  {exif_count}/{len(frames)} timestamps came from EXIF; "
          f"{len(frames) - exif_count} fell back to file modification time.")

    sessions, median_interval, threshold = build_sessions(
        frames, args.gap_seconds, args.gap_multiplier, args.min_gap_floor
    )

    print(f"\nMedian interval between photos: {median_interval:.2f}s")
    print(f"Session-split threshold: {threshold:.2f}s "
          f"({'fixed' if args.gap_seconds is not None else f'auto = median x {args.gap_multiplier}'})")

    if needs_activity:
        print("\nScoring print activity...")
        assert ffmpeg_path is not None
        analyzer = ActivityAnalyzer(frames, fresh_cache if use_cache else None,
                                    ffmpeg_path, workers)
        raw_scores = analyzer.scores()
        print(f"  {len(frames) - analyzer.decoded:,}/{len(frames):,} scores reused from cache; "
              f"decoded {analyzer.decoded:,} thumbnail(s)")
        unreadable = sum(1 for m in analyzer.mode if m == -1)
        if unreadable:
            print(f"  warning: ffmpeg couldn't read {unreadable:,} photo(s); they count as idle")

        # Smooth and detect inside each gap-split block rather than across the
        # whole capture: a real break in the capture is still a break.
        smoothed: list[float] = []
        block_spans: list[list[tuple[int, int]]] = []
        offset = 0
        for s in sessions:
            block = centered_median(raw_scores[offset:offset + s.n], args.activity_window)
            smoothed += block
            block_spans.append([(offset + a, offset + b) for a, b in
                                hysteresis_spans(block, args.activity_enter, args.activity_leave)])
            offset += s.n

        if args.activity_report:
            report_activity(frames, smoothed, args.activity_enter)

    if args.detect_activity:
        before_n = len(frames)
        found: list[Session] = []
        bridged = plate_changes = 0
        for spans in block_spans:
            prints, b, c = group_prints(frames, spans, analyzer,
                                        args.min_idle_seconds, args.min_active_seconds)
            bridged += b
            plate_changes += c
            for spans_in_print in prints:
                # Only the active spans go into the video; the motionless pauses
                # between them are cut, so no idle footage survives either way.
                found.append(Session(frames=[f for a, b2 in spans_in_print
                                             for f in frames[a:b2 + 1]]))
        after_n = sum(s.n for s in found)
        idle_dropped = before_n - after_n
        print(f"Activity detection: {len(found)} print(s) in {len(sessions)} capture "
              f"block(s); kept {after_n:,} frames, cut {idle_dropped:,} idle "
              f"({idle_dropped / before_n:.1%})")
        print(f"  Joined {bridged} pause(s) back into their print; split at "
              f"{plate_changes} short gap(s) where the plate had changed")
        if not found:
            print("  Nothing cleared the activity thresholds. Re-run with "
                  "--activity-report to see the scores, then lower --activity-enter.")
        sessions = found

    save_cache()

    # Thin after splitting, never before: the gap detection above needs every
    # timestamp to find the real breaks between prints.
    if args.every_nth is not None or args.target_seconds is not None:
        before = sum(s.n for s in sessions)
        thinned = []
        for s in sessions:
            stride = (args.every_nth if args.every_nth is not None
                      else stride_for_target(s.n, args.fps, args.target_seconds))
            thinned.append(Session(frames=decimate(s.frames, stride)))
        sessions = thinned
        after = sum(s.n for s in sessions)
        how = (f"1 in every {args.every_nth} photo(s)" if args.every_nth is not None
               else f"~{args.target_seconds:g}s per video at {args.fps:g} fps")
        print(f"Thinning: {how} -> {after:,} of {before:,} frames "
              f"({after / before:.1%})")

    print(f"Detected {len(sessions)} session(s):\n")

    kept_sessions = []
    for i, s in enumerate(sessions, start=1):
        skip = s.n < args.min_frames
        status = "SKIP (too few frames)" if skip else "ok"
        video_len = s.n / args.fps
        print(f"  [{i}] {s.start} -> {s.end}  "
              f"({s.n:,} frames, {s.duration_seconds:.0f}s span"
              f" -> {video_len / 60:.1f} min video)  {status}")
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
    total_frames = sum(s.n for s in kept_sessions)
    print(f"\nEncoding {len(kept_sessions)} video(s) ({total_frames:,} frames) to "
          f"{args.output} at {args.fps} fps "
          f"({jobs} concurrent job{'s' if jobs != 1 else ''})...\n")

    progress = EncodeProgress(total_frames, len(kept_sessions), sys.stdout.isatty())
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
                args.cq,
                args.preset,
                args.threads,
                args.ffmpeg,
                args.overwrite,
                progress,
            )
            for i, session in enumerate(kept_sessions, start=1)
        ]
        # Report each session as it finishes rather than after the whole batch,
        # so a long encode isn't silent. With --jobs 2+ these arrive out of
        # order; the [index] matches the session list printed above.
        for fut in as_completed(futures):
            index, name, output_path, ok, msg, n_frames = fut.result()
            progress.complete(index, n_frames)
            progress.clear_line()
            if ok:
                print(f"  [{index}/{len(kept_sessions)}] wrote {output_path} "
                      f"({n_frames:,} frames)", flush=True)
            else:
                failures += 1
                print(f"  [{index}/{len(kept_sessions)}] FAILED: {name}\n    {msg}", flush=True)
    progress.clear_line()

    if failures:
        print(f"\nDone with {failures} failure(s).")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
