"""Entry point: no arguments (or a file) = GUI; `scan`, `correct`, `crop` = command-line tools.

  SV600_Scanner.AppImage                     open the GUI
  SV600_Scanner.AppImage image.jpg           open the GUI with an existing scan
  SV600_Scanner.AppImage scan [--split] ...  scan from the command line (scan --help)
  SV600_Scanner.AppImage correct raw.png -o fixed.jpg
  SV600_Scanner.AppImage crop scan.jpg
  SV600_Scanner.AppImage paths               show where settings / calibration / cache live
"""
import sys


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ''
    if cmd in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return
    if cmd == 'scan':
        from .cli import scan_main
        return scan_main(args[1:])
    if cmd == 'correct':
        from .cli import correct_main
        return correct_main(args[1:])
    if cmd == 'crop':
        from .cli import crop_main
        return crop_main(args[1:])
    if cmd == 'paths':
        from . import __version__, paths
        print(f'sv600-linux {__version__}')
        print(f'settings     {paths.SETTINGS_FILE}')
        print(f'calibration  {paths.calibration_file()}')
        print(f'state        {paths.STATE_FILE}')
        print(f'cache        {paths.CACHE_DIR}')
        return
    if cmd == '--version':
        from . import __version__
        print(__version__)
        return
    from .gui import main as gui_main
    gui_main()


if __name__ == '__main__':
    main()
