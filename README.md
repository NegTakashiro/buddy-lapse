# buddy-lapse

Turn a folder of timestamped photos (e.g. from an SD card) into one or more
timelapse `.mp4` videos. Photos are grouped into separate videos wherever
there's a large gap in time between shots (a new "session"), so pointing it
at a card with several days/outings of photos on it produces one video per
outing instead of one giant video.

## Setup

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) on your
PATH.

```
pip install -r requirements.txt
```

Windows: `winget install ffmpeg` (then restart your terminal), or `choco install ffmpeg`.

## Usage

```
python buddy_lapse.py <input_dir> -o <output_dir> [options]
```

Example — point at an SD card, get 30fps videos:

```
python buddy_lapse.py E:\DCIM -o C:\Users\me\Videos\timelapses --fps 30
```

Preview how photos would be grouped without encoding anything:

```
python buddy_lapse.py E:\DCIM --dry-run
```

## How grouping works

1. Each photo's timestamp is read from EXIF `DateTimeOriginal` (falls back to
   file modification time if EXIF is unavailable or Pillow isn't installed).
2. Photos are sorted by timestamp and the time gaps between consecutive
   photos are measured.
3. The typical (median) interval between photos is used to pick a
   split threshold: a gap bigger than `median_interval x --gap-multiplier`
   (default multiplier: 6) starts a new session/video.
4. Each session becomes one `.mp4`.

This adapts automatically to whatever interval your camera was shooting at
(every 2 seconds, every 30 seconds, etc.) without needing to know it in
advance. If the auto-detection doesn't split where you want, override it
directly:

```
# Force a hard 5-minute gap to mean "new video"
python buddy_lapse.py E:\DCIM -o out --gap-seconds 300

# Be more/less sensitive than the default 6x median interval
python buddy_lapse.py E:\DCIM -o out --gap-multiplier 3
```

## Useful options

| Option | Default | Description |
|---|---|---|
| `--fps` | 24 | Output video frame rate |
| `--gap-seconds` | (auto) | Fixed session-split threshold, in seconds |
| `--gap-multiplier` | 6 | Auto threshold = median interval x this |
| `--min-gap-floor` | 10 | Minimum split threshold regardless of interval |
| `--min-frames` | 3 | Skip sessions with fewer photos than this |
| `--ext` | jpg,jpeg,png,bmp,tif,tiff | File extensions to include |
| `--no-recursive` | off | Don't scan subfolders |
| `--codec` | libx264 | ffmpeg video codec |
| `--crf` | 18 | ffmpeg quality (lower = better/larger) |
| `--overwrite` | off | Overwrite existing output files |
| `--name-template` | `{start}_{n}frames.mp4` | Output filename; placeholders `{start} {end} {n} {index}` |
| `--dry-run` | off | Report sessions without encoding |
| `--json-report PATH` | — | Dump full session/frame breakdown as JSON |

Run `python buddy_lapse.py --help` for the full list.
