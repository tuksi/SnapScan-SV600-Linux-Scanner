"""Command-line tools: sv600scan (scan), sv600correct (fix a raw scan), sv600crop (split)."""
import argparse
import os
import sys

from . import output, paths, settings
from .output import DPI, FORMATS, save_image


def _load_settings(ap, path):
    try:
        return settings.load(path)
    except (OSError, ValueError) as e:
        ap.error(str(e))


def scan_main(argv=None):
    """Scan, correct, optionally split, save."""
    onoff = argparse.BooleanOptionalAction
    ap = argparse.ArgumentParser(
        prog='sv600scan', description='Scan with the ScanSnap SV600.',
        epilog=f'Defaults come from {paths.SETTINGS_FILE} (every value is explained there); '
               'options given here override it for one run.')
    ap.add_argument('-o', '--output', help='output file; format from extension: jpg, png (lossless), '
                    'tif, pdf, webp. Default: output.folder / output.name . output.format')
    ap.add_argument('-q', '--quality', type=int, metavar='1-100',
                    help='JPEG/PDF/WebP quality (settings: output.jpeg_quality, default 100)')
    ap.add_argument('--raw', help='also save raw RGB bytes here')
    ap.add_argument('--brighten', action=onoff, help='auto-levels stretch (settings: brighten.enabled)')
    ap.add_argument('--correct', action=onoff, help='geometry/colour/brightness correction; '
                    '--no-correct saves the raw distorted frame (settings: correction.enabled)')
    ap.add_argument('--area', choices=('inscribed', 'full'),
                    help='largest fully covered rectangle (~490x370 mm) or the whole trapezoid '
                         '(~704x374 mm) (settings: correction.area)')
    ap.add_argument('--flat', action=onoff, help='far-side brightness correction (settings: correction.flat_field)')
    ap.add_argument('--colour', action=onoff, help='colour calibration (settings: correction.colour)')
    ap.add_argument('--calibration', help='calibration file (settings: correction.calibration)')
    ap.add_argument('--split', action=onoff,
                    help='find each object (page, book), straighten it and save it as NAME_1.EXT, '
                         'NAME_2.EXT, ... in reading order (settings: split.enabled)')
    ap.add_argument('--margin', type=float, metavar='MM',
                    help='margin around each split object (settings: split.margin_mm, default 10)')
    ap.add_argument('--height', type=float, metavar='MM',
                    help='height of the objects\' top above the desk, e.g. 40 for a closed 4 cm book: '
                         'cut-outs are scaled to true size (settings: split.object_height_mm)')
    ap.add_argument('--settings', help=f'settings file (default: {paths.SETTINGS_FILE})')
    ap.add_argument('-v', '--verbose', action='store_true', help='also log every USB command')
    args = ap.parse_args(argv)
    cfg = _load_settings(ap, args.settings)
    pick = lambda arg, section, key: cfg[section][key] if arg is None else arg
    quality = pick(args.quality, 'output', 'jpeg_quality')
    brighten = pick(args.brighten, 'brighten', 'enabled')
    correct = pick(args.correct, 'correction', 'enabled')
    area = pick(args.area, 'correction', 'area')
    flat = pick(args.flat, 'correction', 'flat_field')
    colour = pick(args.colour, 'correction', 'colour')
    split = pick(args.split, 'split', 'enabled')
    margin = pick(args.margin, 'split', 'margin_mm')
    height = pick(args.height, 'split', 'object_height_mm')
    if args.output:
        out_path = args.output
    else:
        o = cfg['output']
        out_path = output.next_base(o['folder'], o['name']) + '.' + o['format']
    if out_path.rsplit('.', 1)[-1].lower() not in FORMATS:
        ap.error(f'unknown output format for {out_path!r}; use one of: {", ".join(FORMATS)}')
    if not 1 <= quality <= 100:
        ap.error('quality must be between 1 and 100')
    if split and not correct:
        ap.error('split needs the corrected image; turn correction on')

    from .console import ConsoleProgress
    from .scanner import SV600, ScannerError
    cal = None
    if correct:
        from . import correct as corr
        cal = corr.Calibration(paths.calibration_file(args.calibration or cfg['correction']['calibration']))
    save_full = not split or cfg['split']['save_full_scan']
    png_level = cfg['output']['png_compression']

    ui = ConsoleProgress(total_steps=9 + bool(brighten) + bool(cal) + save_full + 2 * bool(split),
                         verbose=args.verbose)
    try:
        ui.step('Connecting to the scanner over USB')
        sv = SV600(ui, cfg['scanner'])
        try:
            width, height, rgb = sv.scan()
        finally:
            sv.close()
    except ScannerError as e:
        sys.stderr.write('\n')
        sys.exit(f'error: {e}')

    if args.raw:
        open(args.raw, 'wb').write(rgb)

    import numpy as np
    from PIL import Image
    ui.step('Building the image')
    img = Image.frombytes('RGB', (width, height), rgb)
    det_small, colour = None, colour and bool(cal)
    if cal:
        from . import correct as corr
        ui.step('Correcting geometry, colour registration and brightness')
        colour = colour and cal.colour is not None
        fixed, det_small = ui.run_with_spinner('resampling', corr.correct, np.asarray(img), cal, area, flat,
                                               colour, True)
        if not colour:
            det_small = None
        img = Image.fromarray(fixed)
        ui.end(f'{img.width} x {img.height} px = {img.width / DPI * 25.4:.0f} x '
               f'{img.height / DPI * 25.4:.0f} mm')
    if brighten:
        from PIL import ImageOps
        ui.step('Brightening (auto levels)')
        img = ImageOps.autocontrast(img, cutoff=cfg['brighten']['cutoff_percent'])
    if save_full:
        ui.step(f'Saving {out_path}')
        ui.run_with_spinner('compressing', save_image, img, out_path, quality, png_level,
                            output.COLOUR_MARK if colour else None)
        ui.end(f'{os.path.getsize(out_path) / 1e6:.1f} MB')
    if not split:
        ui.finish(f'Scan saved to {out_path}')
        return

    from . import crop
    ui.step('Finding objects (edges and non-desk colour)')
    full = np.asarray(img)
    bg = crop.load_background(full.shape) if cfg['detection']['background'] == 'scanned' else None
    rects, _ = ui.run_with_spinner('detecting', crop.find_objects, full, cfg['detection'],
                                   None, None, None, det_small, bg)
    try:
        k = crop.height_scale(height, cfg['correction']['head_height_mm'] or cal.head_height)
    except ValueError as e:
        sys.exit(f'error: {e}')
    ui.end(f'{len(rects)} found')
    base, ext = os.path.splitext(out_path)
    ui.step(f'Straightening and saving {len(rects)} object(s), {margin:g} mm margin')
    saved = []
    fill = tuple(cfg['split']['fill_colour'])
    for i, r in enumerate(rects, 1):
        cut, ang = crop.straighten(full, r, margin, fill, k)
        path = f'{base}_{i}{ext}'
        save_image(Image.fromarray(cut), path, quality, png_level)
        w_mm = cut.shape[1] / DPI * 25.4 - 2 * margin
        h_mm = cut.shape[0] / DPI * 25.4 - 2 * margin
        saved.append(f'  {path}: {w_mm:.0f} x {h_mm:.0f} mm, straightened by {ang:+.1f} deg')
    ui.end()
    for line in saved:
        print(line, file=sys.stderr)
    what = f'{len(rects)} object(s)' if rects else 'no objects found'
    ui.finish(f'Scan saved to {out_path} plus {what}' if save_full else f'Saved {what}')


def correct_main(argv=None):
    """Correct an existing raw (--no-correct) scan."""
    ap = argparse.ArgumentParser(prog='sv600correct', description='Correct a raw SV600 scan.')
    ap.add_argument('input', help='raw scan saved with --no-correct (5572x4429)')
    ap.add_argument('-o', '--output', required=True)
    ap.add_argument('--area', choices=('inscribed', 'full'), default='inscribed')
    ap.add_argument('--no-flat', action='store_true', help='skip brightness correction')
    ap.add_argument('--no-colour', action='store_true', help='skip the colour calibration')
    ap.add_argument('--calibration', default='')
    ap.add_argument('-q', '--quality', type=int, default=100)
    args = ap.parse_args(argv)
    import numpy as np
    from PIL import Image
    from . import correct as corr
    cal = corr.Calibration(paths.calibration_file(args.calibration))
    out = corr.correct(np.asarray(Image.open(args.input).convert('RGB')), cal, args.area, not args.no_flat,
                       not args.no_colour)
    save_image(Image.fromarray(out), args.output, args.quality)
    x0, x1, y0, y1 = cal.output_area(args.area)
    print(f'{args.output}: {out.shape[1]}x{out.shape[0]} px = {x1 - x0:.0f} x {y1 - y0:.0f} mm at {DPI} dpi',
          file=sys.stderr)


def crop_main(argv=None):
    """Split/straighten objects in an existing corrected scan."""
    ap = argparse.ArgumentParser(prog='sv600crop',
                                 description='Find objects on a corrected scan, straighten and save each.')
    ap.add_argument('input', help='corrected scan')
    ap.add_argument('-o', '--outdir', default=None, help='output folder (default: next to input)')
    ap.add_argument('--margin', type=float, help='margin around each object in mm (settings: split.margin_mm)')
    ap.add_argument('--min-size', type=float, help='ignore objects with a side shorter than this, mm '
                    '(settings: detection.min_side_mm)')
    ap.add_argument('-q', '--quality', type=int, help='JPEG quality (settings: output.jpeg_quality)')
    ap.add_argument('--height', type=float, metavar='MM',
                    help='height of the objects\' top above the desk (settings: split.object_height_mm)')
    ap.add_argument('--debug', help='save an overlay of what was detected')
    ap.add_argument('--settings', help=f'settings file (default: {paths.SETTINGS_FILE})')
    args = ap.parse_args(argv)
    cfg = _load_settings(ap, args.settings)
    height = cfg['split']['object_height_mm'] if args.height is None else args.height
    det = dict(cfg['detection'])
    if args.min_size is not None:
        det['min_side_mm'] = args.min_size
    margin = cfg['split']['margin_mm'] if args.margin is None else args.margin
    quality = cfg['output']['jpeg_quality'] if args.quality is None else args.quality
    fill = tuple(cfg['split']['fill_colour'])

    import cv2
    from PIL import Image
    from . import crop
    bgr = cv2.imread(args.input)
    if bgr is None:
        sys.exit(f'cannot read {args.input}')
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    cands = []
    rects, dbg = crop.find_objects(rgb, det, debug=cands)
    k = 1.0
    if height:
        from . import correct as corr
        head = cfg['correction']['head_height_mm'] or corr.Calibration(
            paths.calibration_file(cfg['correction']['calibration'])).head_height
        try:
            k = crop.height_scale(height, head)
        except ValueError as e:
            sys.exit(f'error: {e}')
    if args.debug:
        os.makedirs(os.path.dirname(os.path.abspath(args.debug)), exist_ok=True)
        crop.draw_debug(dbg['small'], cands, args.debug)
    base, ext = os.path.splitext(os.path.basename(args.input))
    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input))
    if not rects:
        print('no objects found', file=sys.stderr)
    for i, r in enumerate(rects, 1):
        cut, ang = crop.straighten(rgb, r, margin, fill, k)
        path = os.path.join(outdir, f'{base}_{i}{ext}')
        save_image(Image.fromarray(cut), path, quality, cfg['output']['png_compression'])
        w_mm = cut.shape[1] / crop.PX_PER_MM - 2 * margin
        h_mm = cut.shape[0] / crop.PX_PER_MM - 2 * margin
        print(f'{path}: object {w_mm:.0f} x {h_mm:.0f} mm, rotated {ang:+.2f} deg', file=sys.stderr)
