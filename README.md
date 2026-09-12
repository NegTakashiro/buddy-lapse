# buddy-lapse

Turn a folder of timestamped photos into timelapse `.mp4` videos —
built for [Prusa BuddyCam](https://www.prusa3d.com/product/buddycam/) owners,
but works with any camera or SD card full of JPEGs.

BuddyCam (and similar setups) snap a photo at a fixed interval throughout a
print, and photos from many different prints often end up mixed together on
the same SD card or storage drive. **buddy-lapse** automatically splits them
back into one video per print by detecting the time gap between prints, so
you don't have to manually sort files or run ffmpeg by hand.

## Features

- **Automatic session detection** — groups photos into separate videos
  wherever there's a large gap in time, so one card with several prints on it
  produces one video per print instead of one giant video.
- **EXIF-aware** — reads the real capture time from each photo's EXIF data
  when available, falling back to file modification time otherwise.
- **Just works** — sensible defaults; point it at a folder and go.
- **Configurable** — override frame rate, split sensitivity, codec, quality,
  output naming, and more.
- **Safe to re-run** — `--dry-run` shows you exactly how photos would be
  grouped before any video is encoded, and existing outputs are never
  overwritten unless you pass `--overwrite`.

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH`
- [Pillow](https://pypi.org/project/Pillow/) (optional but recommended, for
  reading EXIF timestamps)

## Install

```
git clone https://github.com/NegTakashiro/buddy-lapse.git
cd buddy-lapse
pip install -r requirements.txt
```

**ffmpeg:**
- Windows: `winget install ffmpeg` (restart your terminal afterward), or `choco install ffmpeg`
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg` (or your distro's package manager)

## Quick start

Point it at the folder your printer/BuddyCam saves photos to (an SD card,
USB drive, or a folder you've copied/synced them into):

```
python buddy_lapse.py E:\DCIM -o C:\Users\me\Videos\timelapses --fps 30
```

Not sure how it'll group things? Preview first, no video encoding:

```
python buddy_lapse.py E:\DCIM --dry-run
```

That prints each detected session (print job) with its time range and frame
count so you can sanity-check before committing to an encode.

## How grouping works

1. Each photo's timestamp is read from EXIF `DateTimeOriginal` (falls back to
   file modification time if EXIF is unavailable or Pillow isn't installed).
2. Photos are sorted by timestamp and the time gaps between consecutive
   photos are measured.
3. The typical (median) interval between photos is used to pick a split
   threshold: a gap bigger than `median_interval x --gap-multiplier`
   (default multiplier: 6) starts a new session/video.
4. Each session becomes one `.mp4`.

This adapts automatically to whatever interval your printer was shooting at
(every 2 seconds, every 30 seconds, etc.) without you needing to know it in
advance. If auto-detection doesn't split where you want, override it
directly:

```
# Force a hard 5-minute gap to mean "new video"
python buddy_lapse.py E:\DCIM -o out --gap-seconds 300

# Be more/less sensitive than the default 6x median interval
python buddy_lapse.py E:\DCIM -o out --gap-multiplier 3
```

## Options

| Option | Default | Description |
|---|---|---|
| `-o`, `--output` | `./output` | Folder to write `.mp4` files into |
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
| `--keep-lists` | off | Keep the intermediate ffmpeg concat list files |
| `--ffmpeg PATH` | `ffmpeg` | Path to the ffmpeg binary, if not on `PATH` |

Run `python buddy_lapse.py --help` for the full list.

## Troubleshooting

- **`error: no matching photos found`** — check `--ext` covers your file
  type, and that `--no-recursive` isn't hiding photos in subfolders.
- **ffmpeg errors** — make sure ffmpeg is installed and on your `PATH`, or
  pass its full path with `--ffmpeg`.
- **Videos are being split too often / not often enough** — run with
  `--dry-run` first, then tune `--gap-multiplier` (or set a fixed
  `--gap-seconds`) until the sessions match your prints.

## Contributing

Issues and pull requests are welcome — this started as a personal tool for
sorting through BuddyCam SD cards, so real-world feedback from other Prusa
owners is very much appreciated.

## License

[MIT](LICENSE)
