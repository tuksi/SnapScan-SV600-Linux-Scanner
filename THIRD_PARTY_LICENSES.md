# Third-party software

The PolyForm Noncommercial licence in `LICENSE` covers this project's own code, calibration and
documents only. The AppImage also contains the software below. Each part stays under its own
licence (none of them is restricted to non-commercial use), and the AppImage carries every
licence text: the projects' own files in their `*.dist-info` folders, plus the texts in
`usr/share/licenses/sv600-scanner/` for those that do not ship one.

| Component | Used for | Licence |
|---|---|---|
| Python 3.12 (python-appimage build) | runtime | PSF License |
| Qt 6 / PySide6 / Shiboken6 | GUI | LGPL-3.0 ([LGPL-3.0.txt](licenses/LGPL-3.0.txt), [GPL-3.0.txt](licenses/GPL-3.0.txt)) |
| OpenCV (opencv-python-headless) | image correction, detection | Apache-2.0; bundles FFmpeg (LGPL-2.1+, [LGPL-2.1.txt](licenses/LGPL-2.1.txt)), libvpx, libaom, OpenBLAS (BSD) |
| NumPy | arrays | BSD-3-Clause (bundles OpenBLAS: BSD-3-Clause, libgfortran: GPL-3.0 with GCC Runtime Library Exception) |
| Pillow | image files | MIT-CMU (bundles libjpeg-turbo, libpng, libtiff, libwebp, FreeType, HarfBuzz, Little CMS, zstd, ...) |
| PyUSB | USB | BSD |
| libusb-package (libusb 1.0) | USB | Apache-2.0; libusb itself LGPL-2.1 ([LGPL-2.1.txt](licenses/LGPL-2.1.txt)) |
| libxcb-cursor | X11 mouse cursors | MIT/X11 |
| appimagetool runtime | AppImage container | MIT |

**LGPL libraries (Qt, FFmpeg, libusb)** are included unmodified as separate shared libraries. You
may replace them with your own builds: extract the AppImage with `--appimage-extract`, swap the
`.so` files, and run `squashfs-root/AppRun`. Their source code is available from the projects:
https://download.qt.io/official_releases/, https://ffmpeg.org/download.html,
https://github.com/libusb/libusb.
