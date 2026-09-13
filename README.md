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
  overwritten unless you pass `--overwrite`. Timestamp caching makes repeat
  scans much faster.
- **Works with always-on cameras** — `--detect-activity` figures out when the
  printer was actually printing, so a continuous capture becomes one video per
  print instead of one enormous video of a mostly empty bed.

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

## Faster encoding

Timestamps are read in parallel by default, and a cache under the output
folder (`.buddy-lapse-cache.json`) skips unchanged files on re-runs. A file is
re-read whenever its size or modification time changes, and entries for photos
that no longer exist are dropped. `--dry-run` updates the cache too, but never
creates the output folder just to store it. For encode speed:

```
# Faster software encode (photos already copied to SSD)
python buddy_lapse.py E:\DCIM -o out --preset veryfast --jobs 2

# Trade a little quality for speed/size
python buddy_lapse.py E:\DCIM -o out --preset veryfast --crf 23

# Hardware encode examples (keep --jobs 1; one encode usually saturates the GPU)
python buddy_lapse.py E:\DCIM -o out --codec h264_nvenc --preset p4
python buddy_lapse.py E:\DCIM -o out --codec h264_qsv
python buddy_lapse.py E:\DCIM -o out --codec h264_amf
python buddy_lapse.py E:\DCIM -o out --codec h264_videotoolbox
```

`--crf` applies only to `libx264` / `libx265`. Hardware encoders ignore it, so
use `--cq` for those — it becomes `-cq` for nvenc, `-global_quality` for qsv,
`-qp_i`/`-qp_p` for amf, and `-q:v` for videotoolbox. Without `--cq` a hardware
encoder falls back to its own default bitrate, which is usually low, so you get
a small file at a quality you didn't choose. Prefer copying photos off the SD
card before using `--jobs` greater than 1.

## Continuous cameras: keeping only the printing

By default videos are split wherever there's a large gap in time between
photos. That works when the camera only runs during a print. If it shoots
continuously — every 10 seconds, day and night — there are no gaps, so a
fortnight of captures collapses into one enormous video that is mostly an
empty bed.

`--detect-activity` keeps only the frames where the printer is working, one
video per print:

```
# See what it finds before encoding anything
python buddy_lapse.py C:\photos -o out --detect-activity --dry-run

# Then encode one video per print
python buddy_lapse.py C:\photos -o out --detect-activity
```

**How it decides.** Each photo is decoded by ffmpeg to a 32×18 thumbnail, and
each frame is scored by the share of the image that moved since the previous
one, after evening out brightness and contrast. A moving toolhead or a growing
part moves a solid chunk of the picture; an idle bed moves none of it, even
while the sun, the room light, or the camera's auto-exposure changes. The camera
also flips between colour and IR night mode, sometimes every few frames, so each
frame is only compared with the last one taken in the same mode.

Pauses inside a print — the toolhead working out of view for a few minutes —
are cut from the video but don't end it. Whether a gap is a pause or a plate
change is decided by comparing the plate on either side of it, so a 13-minute
plate swap still starts a new video while a 17-minute in-print pause doesn't.

On a real 13-day, 145,744-photo capture this found 44 prints and cut 63.9% of
the frames. Checked by eye against a 32-hour stretch, every printing sample
scored at least 0.012 and every idle sample exactly 0 — including hours of
mode flipping and moving sunlight that an earlier approach counted as printing.

Detection needs ffmpeg even with `--dry-run`. The first run over a large capture
decodes every photo — about a minute for 145,000 photos on a 20-core machine.
Scores are cached alongside the timestamps, so a re-run decodes nothing (the
same capture took 27 seconds end to end, mostly reading the cache) and newly
added photos are the only ones decoded.

**Tuning.** The defaults came from one camera, so check a dry run before
trusting a long encode:

```
python buddy_lapse.py C:\photos -o out --dry-run --detect-activity --activity-report
```

That prints, hour by hour, the average score and the share of frames above
`--activity-enter`. Then adjust:

| Flag | Default | Use when |
|---|---|---|
| `--activity-enter` | 0.006 | Idle stretches are being kept — raise it |
| `--activity-leave` | 0.003 | Prints are cut short — lower it |
| `--activity-window` | 31 | Brief stops are chopping prints — raise it (frames, ~5 min at 10s) |
| `--min-active-seconds` | 900 | Short bogus clips appear — raise it. Very short prints go missing — lower it |
| `--min-idle-seconds` | 1800 | Longest pause that can still be the same print |

The known false positive is **someone reaching into the printer** to clear the
bed: that genuinely moves. It rarely lasts long, which is what
`--min-active-seconds` is for — it counts time actually moving, and in the test
capture every such clip was under 11 minutes while the shortest real print was
21.

## Smaller files, shorter waits

A long print at one photo every 10 seconds produces a *lot* of photos, and every
photo is one frame. 113,000 photos is a 79-minute video at 24 fps, which is both
enormous and longer than anyone will watch.

Encoder settings can't fix that — frame count is the dominant term in both file
size and encode time. `--every-nth` and `--target-seconds` cut it directly:

```
# Use every 10th photo: a tenth the length, roughly a tenth the size
python buddy_lapse.py C:\photos -o out --every-nth 10

# Or say how long you want each video and let it pick the stride
python buddy_lapse.py C:\photos -o out --target-seconds 60
```

Thinning happens *after* session splitting, so dropping frames never affects
where videos get divided. The first and last photo of each session are always
kept, so the finished print still ends the video.

Measured on 6,571 real 1080p photos (one print, RTX 3080 Ti, 20-core CPU):

| Settings | Time | Size |
|---|---|---|
| default (`libx264`, crf 18, medium) | 88.5s | 415.3 MB |
| `--preset veryfast --crf 26` | 37.3s | 120.2 MB |
| `--codec h264_nvenc --preset p5 --cq 26` | 30.8s | 200.0 MB |
| `--target-seconds 60` + nvenc cq 26 | 7.2s | 43.8 MB |
| `--every-nth 10` + nvenc cq 26 | 4.0s | 22.8 MB |

Raising `--fps` shortens the video but does **not** shrink it — same frames,
same quality, fewer seconds. Use `--every-nth` or `--target-seconds` to shrink.

## Options

| Option | Default | Description |
|---|---|---|
| `-o`, `--output` | `./output` | Folder to write `.mp4` files into |
| `--fps` | 24 | Output video frame rate |
| `--gap-seconds` | (auto) | Fixed session-split threshold, in seconds |
| `--gap-multiplier` | 6 | Auto threshold = median interval x this |
| `--min-gap-floor` | 10 | Minimum split threshold regardless of interval |
| `--min-frames` | 3 | Skip sessions with fewer photos than this (counted after thinning) |
| `--detect-activity` | off | Keep only frames where the printer is working, one video per print |
| `--activity-enter` | 0.006 | Score (share of image moving) at which a print counts as started |
| `--activity-leave` | 0.003 | Score below which a print counts as paused |
| `--activity-window` | 31 | Frames to take the median score over |
| `--min-active-seconds` | 900 | Discard prints with less moving time than this |
| `--min-idle-seconds` | 1800 | Longest pause that can still be the same print |
| `--activity-report` | off | Print the hourly activity score, for tuning |
| `--every-nth N` | off | Use only every Nth photo — biggest lever on size and time |
| `--target-seconds S` | off | Thin each session so its video runs about S seconds |
| `--ext` | jpg,jpeg,png,bmp,tif,tiff | File extensions to include |
| `--no-recursive` | off | Don't scan subfolders |
| `--workers` | auto | Parallel workers for reading timestamps |
| `--jobs` | 1 | Concurrent ffmpeg encodes (use 2+ on fast local disk) |
| `--cache PATH` | `<output>/.buddy-lapse-cache.json` | Timestamp cache file |
| `--no-cache` | off | Disable the timestamp cache |
| `--codec` | libx264 | ffmpeg video codec (also: h264_nvenc, h264_qsv, h264_amf, h264_videotoolbox) |
| `--crf` | 18 | ffmpeg quality for libx264/libx265 (lower = better/larger) |
| `--cq` | off | Constant quality for hardware codecs (try 23–28); required for real control |
| `--preset` | medium (software) | ffmpeg `-preset` (e.g. `veryfast`, nvenc `p4`) |
| `--threads` | 0 | ffmpeg `-threads` (0 = auto) |
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
- **One giant video full of an empty bed** — your camera runs continuously, so
  there are no time gaps to split on. Use `--detect-activity`; see
  [Continuous cameras](#continuous-cameras-keeping-only-the-printing).
- **Activity detection found nothing / kept everything** — run
  `--dry-run --activity-report` and compare the printed scores against
  `--activity-enter` and `--activity-leave`.
- **Slow encodes from an SD card** — copy photos to a local SSD first, then
  use `--jobs 2` (or a hardware `--codec`). Keep `--jobs 1` when reading
  directly from the card.
- **The video is enormous and takes forever** — you almost certainly have more
  frames than you want. Check the "min video" figure in the `--dry-run` session
  list, then use `--every-nth` or `--target-seconds`. See
  [Smaller files, shorter waits](#smaller-files-shorter-waits).
- **Hardware encode looks bad** — pass `--cq` (try 23–28). Without it the
  encoder picks its own, usually low, default bitrate.

## Contributing

Issues and pull requests are welcome — this started as a personal tool for
sorting through BuddyCam SD cards, so real-world feedback from other Prusa
owners is very much appreciated.

## License

[MIT](LICENSE)
