# Changelog

## 1.1.0 (2026-10-24)

### Added
- **"Also save the full scan" checkbox** in the Output panel, above the Save button. Untick it
  to save only the cut-outs of the boxes. It is the same setting as Settings > Split > Save full
  scan (`split.save_full_scan`) and is remembered between runs. A scan without boxes is still
  always saved whole. Saving a scan again with the box unticked removes the full-scan file of the
  earlier save.
- **Fine rotation of boxes:** hold **Shift** while dragging the round rotate handle to turn the
  box 10x slower. Shift can be pressed or released mid-drag without the box jumping.
- **Angle display:** the box's current angle (e.g. `9.00°`) is shown next to the rotate handle
  while rotating.
- **New session** button on the toolbar and below the session list (**Ctrl+N**).

- **Background setting** (Settings > Detection > Background): `auto` (tell from the scan, as
  before), `black_mat`, `desk`, or `scanned`.
- **Scan background...** (the arrow next to Scan): scan the empty mat or desk once. It is
  stored in `~/.config/sv600-linux/background.png`, and with Background = `scanned` detection
  finds objects by comparing each scan with it, spot by spot (`detection.background_threshold`,
  default 12). Without a stored background, `auto` is used.

- **Versioning of settings and sessions.** `settings.toml` starts with `version = N`, and
  `session.json` records the session format and the app version that wrote it. An older version
  keeps working with newer files: it ignores what it does not know and carries it through
  unchanged (unknown settings, newer box options, unknown session fields). A setting value it
  cannot accept uses the default for that run, and its line in the file is left alone.

### Changed
- **The session is kept automatically.** Every scan, with its boxes, names, heights, prefix, saved
  state and undo history, is kept in `~/.local/share/sv600-linux/session/`. A small
  `session.json` is rewritten after every scan and change, and each scan is stored there once,
  lossless, in the background. Nothing is copied or packed when you save.
- **At startup the app asks whether to load the previous session.** If you choose to start a new
  one, it warns first (and says how many scans were never saved as images), then deletes those
  scans. Images already saved to the output folder are never touched.
- Closing the app no longer asks about the session, because nothing is lost.
- A second window opened while one is running gets a temporary session, like before.
- **Snapping to 15° steps while rotating moved from Shift to Ctrl.**
- **The margin around boxes can be negative** (`split.margin_mm`, the Settings dialog and
  `--margin` on the command line). A negative margin cuts that much off the object's edges.
  Boxes never shrink below 1 px.

### Fixed
- **Dark books on a black mat were not detected.** In the scanner's own colours, which detection
  uses, a dark cover (e.g. a dark green spine and deep purple cover) is almost as black as the mat,
  so its outline was too faint to find. On a black or grey mat, anything clearly coloured in the
  corrected scan now counts as an object (new setting `detection.mat_colour_chroma`, default 12).
- **A work area drawn tightly around books on a black mat could make detection treat a book's
  colour as the desk colour.** The desk colour now has to appear along the edges of the area, as a
  real desk does; otherwise the background counts as a mat. Results on the wooden desk are
  unchanged.
- **Books with a black part of the cover at their edge were rejected on a black mat** (e.g. a
  shrink-wrapped book with black hair in a corner). That part is exactly as dark and colourless as
  the mat, so it bites a piece out of the detected shape. The default `detection.min_rectangularity`
  is now 0.70 (was 0.80): such books measured 0.75-0.79, clutter 0.35-0.5.

- **A session saved by a newer version crashed the app at startup** (e.g. after switching git
  branches). It now opens normally. A session that cannot be opened at all is kept unchanged,
  unless you choose to delete it, and the window uses a temporary session.
- **A setting value this version does not accept made all settings fall back to defaults**, and
  the next save wrote those defaults over the file. Now only that one setting uses its default,
  and its line in the file is left alone.

### Removed
- `.sv600session` files: Save session, Save session as, Open session and Recent sessions are
  gone. The kept session folder replaces them, and older `.sv600session` files can no longer be
  opened.

## 1.0.0

First stable version.
