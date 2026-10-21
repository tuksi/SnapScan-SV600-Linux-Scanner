"""Where things live. The program itself may be read-only (AppImage), so everything the
user changes goes to the XDG config/cache folders:

  ~/.config/sv600-linux/settings.toml          user settings (created on first run)
  ~/.config/sv600-linux/sv600_calibration.json  this scanner's calibration (optional)
  ~/.config/sv600-linux/state.json             GUI state (work area, window size)
  ~/.cache/sv600-linux/                        precomputed correction maps
"""
import os

APP = 'sv600-linux'
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PKG_DIR, 'data')

CONFIG_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), APP)
CACHE_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), APP)

SETTINGS_FILE = os.path.join(CONFIG_DIR, 'settings.toml')
SETTINGS_TEMPLATE = os.path.join(DATA_DIR, 'settings.toml')
STATE_FILE = os.path.join(CONFIG_DIR, 'state.json')
USER_CALIBRATION = os.path.join(CONFIG_DIR, 'sv600_calibration.json')
BUNDLED_CALIBRATION = os.path.join(DATA_DIR, 'sv600_calibration.json')


def calibration_file(explicit=''):
    """Calibration to use: explicit setting, else the user's own, else the bundled one."""
    if explicit:
        return os.path.expanduser(explicit)
    if os.path.exists(USER_CALIBRATION):
        return USER_CALIBRATION
    return BUNDLED_CALIBRATION
