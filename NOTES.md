# ScanSnap SV600 on Linux — reverse-engineering notes

Source: ScanSnap Home 4.1.0 offline installer, driver `SV600u-x64.dll` v1.0.0.19
(Fujitsu, 2014) and `SV600rh-x64.dll` (Ricoh rebuild, 2025, same code).

## Status
- SANE: SV600 listed as `:unsupported` in fujitsu.desc ("Also has a second USB VID
  0x13ba. Will require some gymnastics"). Not in Debian's libsane udev rules.
- Goal: basic single-page scan, no page-turn detection / book flattening.

## USB IDs
| Variant | VID:PID |
|---|---|
| Fujitsu | 04c5:128e |
| Ricoh   | 05ca:03c7 |
| ? second device (camera?) | 13ba:???? (per SANE comment, unverified) |

## Transport (from `Usb_LynxLowDeviceIoControl`, RVA 0x803b0)
Identical to the SANE `fujitsu` backend USB framing:
1. Bulk OUT 31 bytes: `[0]=0x43`, CDB copied to offset 19 (0x13), rest zero.
2. Optional data phase: bulk OUT (flag 0x80) or bulk IN (flag 0x40).
   Reads larger than 0xFE00 are split into chunks.
3. Bulk IN 13-byte status block.

SV600 quirk: before every image `READ(10)` (`28 00 00 ...`) the driver first sends
`F1 10 <00|80 side> 00 00 00 <len24> 00` (SCANNER CONTROL fn 0x10, "READ IMAGE CHECK")
carrying the same transfer length.

## Command set
Full table: `protocol/command_table.txt` (emulated `Make_CdbCommand`, RVA 0x7e710, for every id).
Flags: 0x40 = data in, 0x80 = data out. Bytes `a1 b2 c3` = placeholder length.

Standard Fujitsu SCSI ops used: TEST UNIT READY 00, RESERVE 16, RELEASE 17,
INQUIRY 12 (std, VPD F0, VPD E0), OBJECT POSITION 31, SCAN 1B, READ 28,
SET WINDOW 24, REQUEST SENSE 03, GET HW STATUS C2, SCANNER CONTROL F1/F2,
MODE SELECT 15 (page codes 0x2c..0x3e), SEND 2A (gamma 03, LUT 83, halftone 02),
SEND DIAG 1D / RECEIVE DIAG 1C with 16-byte ASCII requests:
`GET FIRMVERSION`, `GET DEVICE ID`, `READ COUNT`, `SET SCAN MODE`, `SET PRE READMODE`,
`GET DEV PARAM  S/F`, `END WAITING SCAN`, `PAPER WAIT TIME`, `CHANGE IRLED`,
`GET LAMP OFF TIM`, `SET/GET POWOFF TIME`, `FIRST READ DATE`, `GET FIRST DATE`,
and DANGEROUS `DEBUG,E2P/E2T/RAM,...` (EEPROM/RAM read/write — never send these blind).

## Host-side work the Windows driver does (likely needed for good image quality)
- Calibration: AGC (gain/offset), shading (white/black), PRNU per-pixel correction,
  dust correction; cached as `SHADING_*.dat`, `AGC_*.dat`; factory calibration in EEPROM.
- `SwitchFirm` / `.nal` firmware upload code exists but belongs to the shared S300/S1100
  ("Lynx") codebase; no SV600 `.nal` ships, so SV600 probably takes the "not send" path.
- SV600Camera.exe: page-turn/hand detection via a camera ("AndOn"), not needed for basic scans.

## Plan
1. `protocol/sv600_probe.py` — read-only identification (INQUIRY/VPD/HW status/diag GETs).
2. USB capture of one low-res scan from the Win10 VirtualBox VM:
   `VBoxManage controlvm Win10 usbattach <uuid> --capturefile sv600-scan.pcap`
3. Replay the captured init + SET WINDOW + SCAN + READ sequence with pyusb; save raw image.
4. Add minimal shading correction if raw output has streaks; then optionally a SANE patch.

## Hardware results (2026-10-19, Fujitsu unit 04c5:128e, fw 0B00)
- SV600 is an internal USB hub: scanner 04c5:128e (vendor class, bulk 0x02 OUT / 0x81 IN)
  + UVC camera 04c5:13ba (works with uvcvideo). SANE's "VID 0x13ba" note = the camera PID.
- Firmware already running at power-on; no upload needed. Probe: all commands OK
  (status `53 ... 00`). Device ID `AT5H`, read count 0x06F9.
- Stock SANE fujitsu backend detects it (flatbed, color, 300 dpi, 474x355 mm).
  `scanimage` sent mode selects, SET WINDOW, SCANNER CONTROL fn 5 (lamp) — all OK —
  then SCAN (1B) got no status within 30 s; scanner went to blinking orange and stopped
  answering USB (timeouts) -> needs power cycle. Missing pre-scan step(s) still unknown.
  Debug log: protocol/sane_fujitsu_scan_hang.log.

## WORKING (2026-10-19): `sv600scan.py`
Capture `protocol/captures/scan1.pcap` (ScanSnap Home in Win10 VM, decoded with `protocol/fjcap.py`)
showed the missing vendor steps. Pre-scan sequence (replayed verbatim, verified byte-equal):
TUR; MODE SELECT 2c; DIAG "SET SCAN" 00; DIAG "SET SCAN MODE" 00 00 00; DIAG "SET SCAN" 01;
MODE SELECT 34; DIAG "CHANGE IRLED" 01, then 00; SEND LUT (2a 00 83, identity); MODE SELECT 3c;
SET WINDOW (300 dpi, full 0x5710 x 0x4534 /1200"); SCAN (window 0);
poll `F1 10 .. 07 65 9c` until status 00 (08 = busy); READ 0x07659C chunks (29 lines)
until status 02 + sense EOM; READ 28 00 80 -> pixel size (5572x4429); DIAG "SET SCAN" 02 at end.
Status byte = offset 9 of the 13-byte block. Image = pixel-interleaved RGB, 8-bit,
already shading-corrected by the scanner (just a bit dark -> `--brighten`).
SANE fujitsu backend hangs because it never sends the "SET SCAN" diagnostics
(and sends lamp-on F1 05 instead).

## Resolution (capture scan2_maxres.pcap, ScanSnap Home "Excellent")
SET WINDOW is byte-identical to the 300 dpi scan: the hardware only scans at 300 dpi
(VPD F0 basic/max/min resolution all 0x012C). Higher settings in ScanSnap Home are software
upscaling. The error ScanSnap Home showed was not on USB (all statuses normal).

## Geometry
Raw image is distorted by the rotating overhead head (non-uniform line spacing along the sweep,
scale varying with distance / keystone). ScanSnap Home corrects in software
(driver: MakeHorizonDistortionValue, ImgCorrectionTmp*.txt; EEPROM blocks read at connect).
Plan: measure it with calibration/sv600_checkerboard_A4.pdf (15 mm squares) in several
positions, fit a sweep-geometry model, apply in sv600scan.py.

## Correction (DONE 2026-10-19) — sv600correct.py, on by default in sv600scan.py
Calibrated from 8 scans of calibration/sv600_checkerboard_A4.pdf (15 mm squares):
- Geometry "separable-v1": y = poly5(v) + S(v)(e1 u' + e2 u'^2), x = S(v) g(u), S cubic, g cubic.
  Fit rms 0.31 mm; leave-one-scan-out 0.34 mm; corrected squares 14.98-15.01 mm.
  Raw frame covers ~511 mm (near head) to ~730 mm (far) wide, ~386 mm deep.
- Colour registration: R = G shifted +dv(v) lines, B = -dv(v); dv 0.8 px (near) -> 15 px (far),
  independent of u. Residual after correction <0.1 px.
- Flat-field: white level flat until v~3150, then -36% by v~4150 (far side). Gain table along v
  (PCHIP, linear extrapolation past v~4290 -> 1.75 at the last line) x small quadratic in u.
- Orientation: raw is rotated 180 deg vs a page placed readable from the user's seat.
- Output: 300 dpi on the desk plane; default 'inscribed' 490 x 370 mm, '--area full' ~704 x 374 mm
  (after the 96.5 % print-scale fix; before it the sizes were 3.6 % too large).
- Speed: coarse map (8 px grid) cached as ~/.cache/sv600-linux/maps_<digest>_<area>_c8.npz,
  strip-wise remap: ~0.5-1.2 s per scan.
Recalibrate: see calibration/CALIBRATION.md (find_corners, measure_channels_flat, fit_geometry,
build_calibration). Caveats: absolute scale assumes the print was 1:1 (all sheets had the
same orientation, so printer aspect error can't be separated); paper flatness limits accuracy.

## Object split / straighten (sv600crop.py, `sv600scan.py --split [--margin MM]`)
Detection at 75 dpi: Canny on R,G,B -> closed+filled outlines, OR chroma distance (Lab a/b)
from the dominant desk colour > 18 (lightness ignored: lamp glare near the base is ~7, paper ~30;
near-black L<45 excluded so pages touching the black scanner base don't merge with it).
Fill holes, open 4 mm (drops cables), keep blobs >= 30 cm2, side >= 30 mm, rectangularity >= 0.8.
minAreaRect -> angle normalised to (-45, 45] (no quarter turns) -> one warpAffine at 300 dpi.
Tested on 8 calibration scans + tissue-pack scan: all found, clutter rejected.
SCALE FIX: the checkerboard was printed at 96.5 % (100 mm line measured 96.5 mm), so squares are
14.475 mm; print_scale in calibration/board.json. After rebuild A4 sheets measure
209.7-212.4 x 297.1-299.8 mm (outline adds ~1 mm). Inscribed output is now 490 x 370 mm.

## Settings (settings.toml, sv600settings.py)
All tunables live in settings.toml (each documented): output (file, jpeg_quality=100,
png_compression), correction (enabled, area, flat_field, calibration), brighten, split (enabled,
margin_mm, save_full_scan, fill_colour), detection thresholds, scanner timeouts.
Order: built-in defaults -> settings.toml (or --settings FILE) -> command-line options.
Booleans have --x/--no-x forms. Wrong types error out, unknown keys warn.
Books test (books.jpg, 2026-10-19): first run merged everything into one 485x370 mm object because
the desk-colour reference landed on black/neutral (dark IKEA cover + black frame edges outnumber
any single wood shade). Fix: reference only from coloured pixels (chroma > 8, 25 < L < 235); if
<15 % of the image is coloured (black/grey mat), colour cue is disabled and outlines alone are used.
Result: 2 books, 167x236 mm (+7.6 deg) and 221x261 mm (+2.5 deg); earlier test scans unchanged.
