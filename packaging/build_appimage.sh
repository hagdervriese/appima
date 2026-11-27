#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained appima-<version>-x86_64.AppImage using PyInstaller + appimagetool.
# Designed to run both locally and in GitHub Actions (no FUSE required during build).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

ARCH="${ARCH:-$(uname -m)}"
VERSION="$(python3 -c 'import appima; print(appima.__version__)')"
BUILD_DIR="${REPO_ROOT}/build/appimage"
APPDIR="${BUILD_DIR}/AppDir"
DIST_DIR="${REPO_ROOT}/dist"
OUT_NAME="appima-${VERSION}-${ARCH}.AppImage"
OUT_PATH="${DIST_DIR}/${OUT_NAME}"

echo "==> Building appima ${VERSION} (${ARCH}) AppImage"
rm -rf "${BUILD_DIR}"
mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/lib" "${APPDIR}/usr/share/icons/hicolor/scalable/apps" "${DIST_DIR}"

# 1. Freeze Python + PySide6 (Qt 6) + appima with PyInstaller
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name appima-bin \
  --distpath "${BUILD_DIR}/pyinstaller-dist" \
  --workpath "${BUILD_DIR}/pyinstaller-work" \
  --specpath "${BUILD_DIR}" \
  --add-data "appima/gui/assets:appima/gui/assets" \
  --collect-submodules appima \
  --hidden-import PySide6.QtSvg \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtQuick \
  --exclude-module PySide6.QtQml \
  --exclude-module PySide6.QtMultimedia \
  --exclude-module PySide6.QtMultimediaWidgets \
  --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.Qt3DRender \
  --exclude-module PySide6.QtPdf \
  --exclude-module PySide6.QtSql \
  --exclude-module PySide6.QtCharts \
  --exclude-module PySide6.QtDataVisualization \
  --exclude-module PySide6.QtSensors \
  --exclude-module PySide6.QtPositioning \
  --exclude-module PySide6.QtBluetooth \
  --exclude-module PySide6.QtNfc \
  --exclude-module PySide6.QtSerialPort \
  --exclude-module PySide6.QtTest \
  --exclude-module tkinter \
  packaging/entrypoint.py

mv "${BUILD_DIR}/pyinstaller-dist/appima-bin" "${APPDIR}/usr/lib/appima"

# 2. Bundle unsquashfs (fallback for minimal/immutable distros without squashfs-tools)
if command -v unsquashfs >/dev/null 2>&1; then
  UNSQUASHFS_BIN="$(command -v unsquashfs)"
  mkdir -p "${APPDIR}/usr/lib/unsquashfs-bin" "${APPDIR}/usr/lib/unsquashfs-libs"
  cp "${UNSQUASHFS_BIN}" "${APPDIR}/usr/lib/unsquashfs-bin/unsquashfs"
  chmod 0755 "${APPDIR}/usr/lib/unsquashfs-bin/unsquashfs"

  # Copy non-core shared libraries required by unsquashfs (skip glibc / dynamic linker)
  ldd "${UNSQUASHFS_BIN}" | awk '/=> \// {print $3}' | while read -r lib; do
    base="$(basename "${lib}")"
    case "${base}" in
      libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|ld-linux*.so.*)
        continue
        ;;
      *)
        cp -L "${lib}" "${APPDIR}/usr/lib/unsquashfs-libs/${base}" || true
        ;;
    esac
  done

  cat > "${APPDIR}/usr/bin/unsquashfs" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export LD_LIBRARY_PATH="${HERE}/../lib/unsquashfs-libs${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
exec "${HERE}/../lib/unsquashfs-bin/unsquashfs" "$@"
EOF
  chmod 0755 "${APPDIR}/usr/bin/unsquashfs"
fi

# 3. Desktop entry & icons
cp packaging/appima.desktop "${APPDIR}/appima.desktop"
printf 'X-AppImage-Version=%s\n' "${VERSION}" >> "${APPDIR}/appima.desktop"
cp appima/gui/assets/appima.svg "${APPDIR}/appima.svg"
cp appima/gui/assets/appima.svg "${APPDIR}/.DirIcon"
cp appima/gui/assets/appima.svg "${APPDIR}/usr/share/icons/hicolor/scalable/apps/appima.svg"

# 4. AppRun entry script
cat > "${APPDIR}/AppRun" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(dirname "$(readlink -f "${0}")")"
export APPDIR="${HERE}"
export PATH="${HERE}/usr/bin:${PATH}"
exec "${HERE}/usr/lib/appima/appima-bin" "$@"
EOF
chmod 0755 "${APPDIR}/AppRun"

# 5. Fetch official appimagetool (static FUSE3-compatible runtime) if not already cached
APPIMAGETOOL="${BUILD_DIR}/appimagetool-${ARCH}.AppImage"
if [[ ! -x "${APPIMAGETOOL}" ]]; then
  echo "==> Downloading appimagetool (${ARCH})"
  curl -fsSL \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" \
    -o "${APPIMAGETOOL}"
  chmod 0755 "${APPIMAGETOOL}"
fi

# 6. Build the AppImage
echo "==> Packaging ${OUT_PATH}"
ARCH="${ARCH}" "${APPIMAGETOOL}" --appimage-extract-and-run "${APPDIR}" "${OUT_PATH}"
chmod 0755 "${OUT_PATH}"

# 7. Generate SHA256SUMS
(
  cd "${DIST_DIR}"
  sha256sum "${OUT_NAME}" > SHA256SUMS
)

echo "==> Built: ${OUT_PATH}"
cat "${DIST_DIR}/SHA256SUMS"
