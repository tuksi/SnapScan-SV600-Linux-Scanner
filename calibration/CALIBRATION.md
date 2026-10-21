# Calibrating an SV600

The app ships with the calibration of one particular SV600 (`sv600/data/sv600_calibration.json`).
Another unit, or this one after it has been moved or serviced, can be recalibrated with the steps
below. Everything is done from the project folder with the development environment:

```
python3 -m venv .venv && .venv/bin/pip install -e '.[calibration]'
```

| What | Measured from | Result in the calibration file |
|---|---|---|
| Geometry: overhead sweep, keystone, line spacing | 8 scans of a printed checkerboard | `geometry` (rms ~0.3 mm) |
| Colour fringing: red/blue line offsets | same scans | `channel_shift_poly` |
| Far-side darkening (flat field) | white squares of the same scans | `flatfield` |
| Head height (for raised objects) | same scans | `head_height_mm` |
| Colours | 4 scans of a Datacolor SpyderCHECKR 24 (optional) | `colour` |

## 1. Print the checkerboard

| Paper | File |
|---|---|
| A4 | [sv600_checkerboard_A4.pdf](sv600_checkerboard_A4.pdf) (11 x 17 inner corners) |
| US Letter | [sv600_checkerboard_Letter.pdf](sv600_checkerboard_Letter.pdf) (11 x 15 inner corners) |

Both are in this folder of the repository
at https://github.com/tuksi/SnapScan-SV600-Linux-Scanner. To make them
again: `.venv/bin/python tools/make_checkerboard.py`.

- Print at **100 % / Actual size**, never "Fit to page", on a laser printer if you can.
- Measure the line above the board with a ruler; it should be 100 mm. Put your reading into
  `calibration/board.json` as `print_scale` = measured mm / 100 (e.g. 96.5 mm -> `0.965`).
  An error here scales every corrected scan by the same amount.
- The sheet must lie **flat**: tape it to a stiff board or the desk. Waves in the paper limit the
  accuracy.

## 2. Scan the checkerboard (geometry, colour fringing, darkening, head height)

Make 8 raw (uncorrected) scans, moving the sheet between them:

```
.venv/bin/python sv600scan.py --no-correct -o calibration/cal1.png
.venv/bin/python sv600scan.py --no-correct -o calibration/cal2.png
...
.venv/bin/python sv600scan.py --no-correct -o calibration/cal8.png
```

- Keep the sheet **upright** (portrait, dot top-left as seen from your seat) and only move it:
  near the scanner, far away, left, right, centre, and the far corners. Together the scans
  should cover the whole scan area, especially the far side, where the distortion and the
  darkening are strongest.
- A sheet partly outside the scan area is fine; partial boards are detected.
- Nothing else on the desk needs to move. Avoid shadows across the sheet.

Then run the steps in order:

```
.venv/bin/python tools/find_corners.py calibration/cal*.png           # -> calibration/corners.npz
.venv/bin/python tools/measure_channels_flat.py calibration/cal*.png  # -> channel_shift.npy, white_samples.npy
.venv/bin/python tools/fit_geometry.py                                # -> geometry.json (head height)
.venv/bin/python tools/build_calibration.py                           # -> calibration/sv600_calibration.json
```

Check what `find_corners.py` prints: every scan should report a board (e.g. `11x17 corners`). If a
scan reports `no board found`, retake it (glare, shadow or the sheet too far outside). At the end
`build_calibration.py` prints the geometry error; about 0.3 mm rms is good, above ~1 mm look for
a curled sheet or a scan with the board found wrongly.

## 3. Colours (optional, needs a SpyderCHECKR 24)

Skip this step and the previous colour calibration in `calibration/colour/colour_fit.json` stays
in use (delete that file to build without colour calibration).

Scan the card 4 times at different places on the desk, corrected but **without** colour
calibration, saved lossless:

```
.venv/bin/python sv600scan.py --no-colour --no-brighten -o calibration/colour/card1.png
...
.venv/bin/python sv600scan.py --no-colour --no-brighten -o calibration/colour/card4.png
```

The card can lie in any orientation; it is recognised by its colours. Keep it out of the lamp's
glare (the bright band near the scanner) and lying flat. Then:

```
.venv/bin/python tools/colour_patches.py calibration/colour/card*.png   # -> calibration/colour/patches.json
.venv/bin/python tools/colour_fit.py                                    # -> calibration/colour/colour_fit.json
.venv/bin/python tools/build_calibration.py                             # picks up the colour fit
```

`colour_patches.py` writes `calibration/colour/<scan>_patches.jpg` for each scan: every magenta
square must sit inside one patch. `colour_fit.py` prints the colour error (CIEDE2000) before and
after; this unit went from 17.9 to 2.5 on average.

## 4. Check and install

Correct one of the checkerboard scans and look at it in an image viewer:

```
.venv/bin/python sv600correct.py calibration/cal1.png -o /tmp/cal1_corrected.png
```

The board must be straight, and the colour edges must be free of red/blue fringes, also at the far
side. At 300 dpi a 15 mm square is 177 px (at print scale 0.965: 14.475 mm = 171 px).

Then install the new `calibration/sv600_calibration.json`, either way:

- **Built into the app**: copy it to `sv600/data/sv600_calibration.json` and rebuild the AppImage
  with `tools/build_appimage.sh`. Every computer using that AppImage gets it.
- **On one computer only**: copy it to `~/.config/sv600-linux/sv600_calibration.json`; it
  overrides the built-in one. Remove that file to go back.

The correction maps are cached by calibration content in `~/.cache/sv600-linux/`, so a new
calibration takes effect on the next scan without clearing anything.

## Files in `calibration/`

| File | Made by | In git |
|---|---|---|
| `sv600_checkerboard_A4.pdf`, `sv600_checkerboard_Letter.pdf` | `tools/make_checkerboard.py` | yes |
| `board.json` | you (step 1) | yes |
| `corners.npz`, `channel_shift.npy`, `white_samples.npy`, `geometry.json` | step 2 tools | yes |
| `colour/patches.json`, `colour/colour_fit.json` | step 3 tools | yes |
| `sv600_calibration.json` | `build_calibration.py` | yes |
| `cal*.png`, `colour/*.png` | step 2 and 3 scans (40-50 MB each) | no |
| `*.jpg` | previews and checks | no |

The scans of this unit are not kept: once measured they are not needed any more. The measurement
files above are enough to run `build_calibration.py` again (e.g. after a change to the model);
re-measuring needs new scans.
