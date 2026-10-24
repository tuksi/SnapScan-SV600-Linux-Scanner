"""Settings: built-in defaults, overridden by ~/.config/sv600-linux/settings.toml.

Lookup order (later wins): DEFAULTS -> settings file (or --settings FILE) -> command line.
The settings file is created from the commented template on first use; save() rewrites
values in place so the explanations in the file are kept.

Versioning: the file starts with `version = N` (the highest VERSION that saved it). Raise
VERSION whenever settings are added or change meaning. An older program reading a newer file
keeps working with it: unknown settings are ignored but stay in the file, a value it cannot
accept falls back to its default for this run only and its line is not overwritten, so switching
versions (another git branch, an older AppImage) never loses settings.
"""
import copy
import os
import re
import shutil
import sys
import tomllib

from . import paths

DEFAULTS = {
    'output': {
        'folder': '~/Pictures/SV600',
        'name': 'scan_{date}_{n:03}',
        'format': 'jpg',
        'jpeg_quality': 100,
        'png_compression': 1,
    },
    'correction': {
        'enabled': True,
        'area': 'inscribed',
        'flat_field': True,
        'colour': True,
        'calibration': '',
        'head_height_mm': 0.0,
    },
    'brighten': {
        'enabled': False,
        'cutoff_percent': 0.5,
    },
    'split': {
        'enabled': False,
        'margin_mm': 10.0,
        'object_height_mm': 0.0,
        'save_full_scan': True,
        'fill_colour': [0, 0, 0],
    },
    'detection': {
        'background': 'auto',
        'background_threshold': 12.0,
        'min_side_mm': 30.0,
        'min_area_cm2': 30.0,
        'min_rectangularity': 0.70,
        'desk_colour_threshold': 18.0,
        'dark_limit': 45,
        'dark_colour_chroma': 14.0,
        'edge_low': 30,
        'edge_high': 90,
        'outline_gap_mm': 1.5,
        'merge_gap_mm': 2.0,
        'outer_gap_mm': 5.0,
        'outer_max_inner_fraction': 0.6,
        'thin_object_mm': 4.0,
        'mat_colour_chroma': 12.0,
        'row_height_mm': 50.0,
    },
    'continuous': {
        'scanner_button': True,
        'timer_s': 0.0,
        'auto_save': False,
    },
    'gui': {
        'loupe': True,
        'loupe_zoom': 2.0,
        'loupe_size_px': 240,
    },
    'scanner': {
        'usb_timeout_s': 15.0,
        'read_timeout_s': 30.0,
        'image_ready_timeout_s': 60.0,
    },
}

VERSION = 2              # of the settings file format; see the module docstring

BACKGROUNDS = ('auto', 'black_mat', 'desk', 'scanned')
FORMATS = ('jpg', 'jpeg', 'png', 'tif', 'tiff', 'pdf', 'webp')


def ensure_user_file():
    """Create ~/.config/sv600-linux/settings.toml from the template if it is missing."""
    if not os.path.exists(paths.SETTINGS_FILE):
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        shutil.copyfile(paths.SETTINGS_TEMPLATE, paths.SETTINGS_FILE)
    return paths.SETTINGS_FILE


def _check(section, key, value, default, where):
    if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if type(value) is not type(default):
        raise ValueError(f'{where}: {section}.{key} should be {type(default).__name__}, got {value!r}')
    return value


REJECTED = {}            # (section, key) -> default used instead of a file value this version cannot accept


def load(path=None, create=True):
    """Return the merged settings dict. Unknown keys warn and are ignored; a value of the wrong
    type or out of range warns and the default is used (see REJECTED)."""
    REJECTED.clear()
    s = copy.deepcopy(DEFAULTS)
    if path is None:
        path = ensure_user_file() if create else paths.SETTINGS_FILE
        if not os.path.exists(path):
            return s
    elif not os.path.exists(path):
        raise FileNotFoundError(f'settings file {path} not found')
    with open(path, 'rb') as f:
        user = tomllib.load(f)
    user.pop('version', None)
    for section, values in user.items():
        if section not in s or not isinstance(values, dict):
            print(f'warning: {path}: unknown section [{section}] ignored', file=sys.stderr)
            continue
        for key, value in values.items():
            if key not in s[section]:
                print(f'warning: {path}: unknown setting {section}.{key} ignored', file=sys.stderr)
                continue
            default = s[section][key]
            try:
                s[section][key] = _check(section, key, value, default, path)
                validate(s)
            except ValueError as e:
                print(f'warning: {e}; using the default {default!r}', file=sys.stderr)
                s[section][key] = default
                REJECTED[(section, key)] = default
    validate(s)
    return s


def file_version(path=None):
    """Version of a settings file: 1 if it has none (written before versioning), 0 if missing."""
    path = path or paths.SETTINGS_FILE
    if not os.path.exists(path):
        return 0
    try:
        with open(path, 'rb') as f:
            v = tomllib.load(f).get('version', 1)
    except (OSError, tomllib.TOMLDecodeError):
        return 1
    return v if isinstance(v, int) else 1


def newer_file(path=None):
    """True if the settings file was saved by a newer version of the program."""
    return file_version(path) > VERSION


def validate(s):
    o = s['output']
    if o['format'].lower() not in FORMATS:
        raise ValueError(f'output.format must be one of {", ".join(FORMATS)}, not {o["format"]!r}')
    if not 1 <= o['jpeg_quality'] <= 100:
        raise ValueError('output.jpeg_quality must be 1-100')
    if not 0 <= o['png_compression'] <= 9:
        raise ValueError('output.png_compression must be 0-9')
    if s['detection']['background'] not in BACKGROUNDS:
        raise ValueError(f'detection.background must be one of {", ".join(BACKGROUNDS)}')
    if s['correction']['area'] not in ('inscribed', 'full'):
        raise ValueError('correction.area must be "inscribed" or "full"')
    if s['continuous']['timer_s'] < 0:
        raise ValueError('continuous.timer_s must not be negative')
    if not 0.25 <= s['gui']['loupe_zoom'] <= 16:
        raise ValueError('gui.loupe_zoom must be between 0.25 and 16')
    if not 80 <= s['gui']['loupe_size_px'] <= 1000:
        raise ValueError('gui.loupe_size_px must be between 80 and 1000')
    if s['correction']['head_height_mm'] < 0 or s['split']['object_height_mm'] < 0:
        raise ValueError('heights must not be negative')


def _toml_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, str):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, float):
        return repr(round(v, 6))
    if isinstance(v, list):
        return '[' + ', '.join(_toml_value(x) for x in v) + ']'
    return str(v)


def save(s, path=None):
    """Write settings, keeping the template's comments: values are replaced line by line."""
    validate(s)
    path = path or paths.SETTINGS_FILE
    version = max(VERSION, file_version(path))
    src = path if os.path.exists(path) else paths.SETTINGS_TEMPLATE
    lines = open(src, encoding='utf-8').read().splitlines()
    section, seen, headers = None, set(), {}
    out = []
    for line in lines:
        if section is None and re.match(r'\s*version\s*=', line):
            line = f'version = {version}'
            seen.add((None, 'version'))
        m = re.match(r'\s*\[([A-Za-z_][A-Za-z0-9_]*)\]\s*$', line)
        if m:
            section = m.group(1)
            headers[section] = len(out)
        else:
            m = re.match(r'(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$', line)
            if m and section in s and m.group(2) in s[section]:
                key = m.group(2)
                if REJECTED.get((section, key), object()) != s[section][key]:   # else: keep the file's value
                    line = f'{m.group(1)}{key}{m.group(3)}{_toml_value(s[section][key])}'
                seen.add((section, key))
        out.append(line)
    # values missing from the file (deleted by the user): put them back into their section
    # (a TOML table must not be declared twice), or add the whole section at the end
    for section in sorted(s, key=lambda sec: -headers.get(sec, -1)):
        missing = [f'{k} = {_toml_value(v)}' for k, v in s[section].items() if (section, k) not in seen]
        if not missing:
            continue
        if section in headers:
            at = headers[section] + 1
            out[at:at] = missing
        else:
            out += ['', f'[{section}]'] + missing
    if (None, 'version') not in seen:                 # a file from before versioning
        first = next((i for i, line in enumerate(out) if line.startswith('[')), len(out))
        out[first:first] = [f'version = {version}', '']
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, path)
