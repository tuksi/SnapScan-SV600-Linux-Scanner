#!/bin/bash
# Build SV600_Scanner-<version>-x86_64.AppImage (portable: GUI + command-line tools).
#
# Base: a manylinux_2_28 Python 3.12 AppImage from python-appimage. Our wheel and its dependencies
# are pip-installed into it, unused parts of Qt are removed, libxcb-cursor (needed by Qt's X11
# plugin, missing on some distros) is bundled, and appimagetool packs it. Downloads are cached in
# build/downloads.
#
# Minimum system: glibc 2.34 (Ubuntu 22.04, Debian 12, Fedora 35 or newer), set by the PySide6 6.12
# wheel (manylinux_2_34). libxcb-cursor is taken from Debian 12, not from the build host: a copy
# from a newer distro would raise that minimum (Ubuntu 24.04's needs glibc 2.38).
#
# usage: tools/build_appimage.sh            (run from anywhere; needs internet on first run)
set -euo pipefail
# AppImageLauncher (if installed) intercepts every AppImage start and is itself a Qt program:
# bypass it, and do not pass Qt settings meant for tests (QT_QPA_PLATFORM=offscreen makes it abort).
export APPIMAGELAUNCHER_DISABLE=1
unset QT_QPA_PLATFORM
cd "$(dirname "$0")/.."
ROOT=$PWD
PY_IMG=python3.12.15-cp312-cp312-manylinux_2_28_x86_64.AppImage
PY_URL=https://github.com/niess/python-appimage/releases/download/python3.12/$PY_IMG
PY_SHA=929ce69b45955bfc74af81fd65a65c3ad240593c99b3574466a94ebf0f987ebf
TOOL=appimagetool-x86_64.AppImage
XCB_DEB=libxcb-cursor0_0.1.4-1_amd64.deb                      # Debian 12 (bookworm)
XCB_URL=https://deb.debian.org/debian/pool/main/x/xcb-util-cursor/$XCB_DEB
XCB_SHA=a4b3c32dc008275ffcacccc1c77c030f01aad38e232e05d5ad116b76656c607c
TOOL_URL=https://github.com/AppImage/appimagetool/releases/download/continuous/$TOOL
DL=$ROOT/build/downloads
B=$ROOT/build/appimage
VERSION=$(sed -n "s/^__version__ = '\(.*\)'/\1/p" sv600/__init__.py)
OUT=$ROOT/dist/SV600_Scanner-$VERSION-x86_64.AppImage

step() { printf '\n== %s\n' "$*"; }

step "Downloads"
mkdir -p "$DL"
[ -s "$DL/$PY_IMG" ] || curl -sSL -o "$DL/$PY_IMG" "$PY_URL"
[ -s "$DL/$TOOL" ] || curl -sSL -o "$DL/$TOOL" "$TOOL_URL"
[ -s "$DL/$XCB_DEB" ] || curl -sSLf -o "$DL/$XCB_DEB" "$XCB_URL"
echo "$PY_SHA  $DL/$PY_IMG" | sha256sum -c -
echo "$XCB_SHA  $DL/$XCB_DEB" | sha256sum -c -
chmod +x "$DL/$PY_IMG" "$DL/$TOOL"

step "Wheel"
rm -rf build/lib build/bdist* ./*.egg-info dist/sv600_linux-*.whl
"$ROOT/.venv/bin/pip" wheel --no-deps -q -w dist .
rm -rf build/lib build/bdist* ./*.egg-info
WHEEL=$(ls dist/sv600_linux-"$VERSION"-*.whl)
echo "$WHEEL"

step "AppDir from the Python AppImage"
rm -rf "$B"
mkdir -p "$B"
(cd "$B" && "$DL/$PY_IMG" --appimage-extract >/dev/null && mv squashfs-root AppDir)
APPDIR=$B/AppDir
PY=$APPDIR/opt/python3.12/bin/python3.12
rm -f "$APPDIR"/python*.desktop "$APPDIR"/python.png "$APPDIR"/.DirIcon
rm -rf "$APPDIR"/usr/share/applications "$APPDIR"/usr/share/metainfo "$APPDIR"/usr/share/icons

step "Install sv600 and dependencies"
"$PY" -s -m pip install --no-cache-dir --no-warn-script-location -q "$WHEEL"
"$PY" -s -m pip list --format=freeze | grep -iE 'sv600|pyside|shiboken|numpy|opencv|pillow|pyusb|libusb'

step "Trim"
SITE=$("$PY" -s -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
Q=$SITE/PySide6
before=$(du -sm "$APPDIR" | cut -f1)
# Qt modules the GUI does not use (Widgets/Gui/Core/DBus/XcbQpa/Wayland stay)
rm -rf "$Q"/Qt/qml "$Q"/Qt/translations "$Q"/include "$Q"/typesystems "$Q"/glue "$Q"/scripts \
       "$Q"/examples "$Q"/assistant "$Q"/designer "$Q"/linguist "$Q"/lupdate "$Q"/lrelease \
       "$Q"/qmlformat "$Q"/qmllint "$Q"/qmlls "$Q"/qmlcachegen "$Q"/qmlimportscanner "$Q"/qmltyperegistrar \
       "$Q"/uic "$Q"/rcc "$Q"/Qt/libexec
# by name pattern: QML/Quick (all variants and styles), Labs, Designer, Wayland compositor, ...
for pat in 'Qml*' 'Quick*' 'Labs*' 'Designer*' 'UiTools' 'Help' 'Sql' 'Test' 'Concurrent' 'Network*' \
           'Svg*' 'Xml' 'PrintSupport' 'ShaderTools' 'StateMachine*' 'Pdf*' 'Positioning*' \
           'Multimedia*' 'WaylandCompositor*' 'WebSockets' 'SpatialAudio' 'TextToSpeech' 'Graphs*' \
           'Charts*' 'DataVisualization*' '3D*' 'RemoteObjects*' 'Scxml*' 'Sensors*' 'SerialPort' \
           'Bluetooth' 'Nfc' 'HttpServer' 'WebChannel*' 'WebEngine*' 'VirtualKeyboard*'; do
    rm -f "$Q"/Qt/lib/libQt6$pat.so* "$Q"/Qt$pat.abi3.so "$Q"/Qt$pat.*.so
done
rm -f "$Q"/Qt/plugins/imageformats/libqpdf.so "$Q"/Qt/plugins/imageformats/libqsvg.so "$Q"/Qt/plugins/iconengines/libqsvgicon.so
rm -rf "$Q"/Qt/plugins/{qmltooling,sqldrivers,designer,assetimporters,sceneparsers,renderers,networkinformation,tls,canvasbackends,scenegraph,qmllint,egldeviceintegrations,position}
find "$APPDIR" -name '__pycache__' -prune -o -name '*.pyi' -print | xargs -r rm -f
rm -rf "$SITE"/pip "$SITE"/pip-* "$APPDIR"/opt/python3.12/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip}
rm -rf "$APPDIR"/usr/share/tcltk "$APPDIR"/opt/python3.12/lib/libtk* "$APPDIR"/opt/python3.12/lib/libtcl*
echo "AppDir: ${before} MB -> $(du -sm "$APPDIR" | cut -f1) MB"
# what we use must still load: fail the build if trimming broke a dependency
missing=$(for f in "$Q"/QtCore.abi3.so "$Q"/QtGui.abi3.so "$Q"/QtWidgets.abi3.so "$Q"/Qt/plugins/platforms/*.so \
                   "$Q"/Qt/plugins/imageformats/*.so "$Q"/Qt/plugins/xcbglintegrations/*.so; do
              LD_LIBRARY_PATH="$Q/Qt/lib:$SITE/shiboken6" ldd "$f" | sed -n "s|^\s*\(\S*\) => not found|$(basename "$f"): \1|p"
          done | grep -v libxcb-cursor || true)
if [ -n "$missing" ]; then echo "missing libraries after trimming:"; echo "$missing"; exit 1; fi
echo "dependency check ok"

step "Bundle libxcb-cursor (Qt >= 6.5 X11 plugin dependency, absent on some distros)"
mkdir -p "$B"/xcb
(cd "$B"/xcb && ar x "$DL/$XCB_DEB" && tar -xf data.tar.*)
cp -L "$B"/xcb/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0 "$Q"/Qt/lib/
echo "$XCB_DEB"

step "AppRun, desktop entry, icon, licences"
rm -f "$APPDIR"/AppRun          # it is a symlink to usr/bin/python3.12: do not write through it
rm -f "$APPDIR"/AppRun          # a symlink to usr/bin/python3.12: do not write through it
cat > "$APPDIR"/AppRun <<'EOF'
#!/bin/bash
# SV600 Scanner: GUI by default; `scan`, `correct`, `crop`, `paths` for the command line.
if [ -z "${APPDIR:-}" ]; then
    APPDIR="$(dirname "$(readlink -f -- "$0")")"
    export APPDIR
fi
export SSL_CERT_FILE="${APPDIR}/opt/_internal/certs.pem"
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
exec "$APPDIR/opt/python3.12/bin/python3.12" -I -m sv600 "$@"     # -I: never import from the current folder
EOF
chmod +x "$APPDIR"/AppRun
cp sv600/data/sv600-scanner.desktop "$APPDIR"/
cp sv600/data/sv600-scanner.png "$APPDIR"/
ln -sf sv600-scanner.png "$APPDIR"/.DirIcon
mkdir -p "$APPDIR"/usr/share/applications "$APPDIR"/usr/share/icons/hicolor/256x256/apps
cp sv600/data/sv600-scanner.desktop "$APPDIR"/usr/share/applications/
cp sv600/data/sv600-scanner.png "$APPDIR"/usr/share/icons/hicolor/256x256/apps/
mkdir -p "$APPDIR"/usr/share/licenses/sv600-scanner
cp LICENSE THIRD_PARTY_LICENSES.md licenses/*.txt "$APPDIR"/usr/share/licenses/sv600-scanner/

step "glibc requirement (must stay <= 2.34, see the top of this file)"
GLIBC_MAX=$(find "$APPDIR" -name '*.so*' -type f -exec objdump -T {} + 2>/dev/null \
            | grep -o 'GLIBC_2\.[0-9]*' | sort -t. -k2 -n -u | tail -1)
echo "$GLIBC_MAX"
if [ "${GLIBC_MAX#GLIBC_2.}" -gt 34 ]; then
    find "$APPDIR" -name '*.so*' -type f | while read -r f; do
        objdump -T "$f" 2>/dev/null | grep -q "$GLIBC_MAX" && echo "  needs $GLIBC_MAX: $f"
    done
    exit 1
fi

step "Smoke test inside the AppDir"
"$APPDIR"/AppRun --version
(cd / && "$APPDIR"/AppRun paths | head -3)     # from another folder: must use the bundled package
QT_QPA_PLATFORM=offscreen "$PY" -s -c "from PySide6 import QtWidgets; from sv600 import gui, crop, correct, scanner; print('imports ok')"

step "Pack"
mkdir -p dist
rm -f "$OUT"
ARCH=x86_64 "$DL/$TOOL" --no-appstream "$APPDIR" "$OUT" >/dev/null 2>"$B"/appimagetool.log || {
    cat "$B"/appimagetool.log; exit 1; }
ls -la "$OUT"
echo "built $OUT"
