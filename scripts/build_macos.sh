#!/bin/bash
# Build a native app with its local service launchers. No Homebrew changes required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$ROOT/desktop/.runtime-cache"
DIST="$ROOT/dist"
mkdir -p "$CACHE" "$DIST"
case "$(uname -m)" in
  arm64) ARCH=arm64; UV_ARCH=aarch64; TEMPORAL_SHA=41e0425378fcb4fb5766340b97435e20fe47bbff2d7bf644ec2d51f7662b7c56 ;;
  x86_64) ARCH=amd64; UV_ARCH=x86_64; TEMPORAL_SHA=48cdd6c84c56e27ae8e4d9a47052b89289a9c552dee029b49dd312c6fb72873f ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
TEMPORAL_VERSION=1.9.1
UV_VERSION=0.13.4
STAGE="$(mktemp -d "$DIST/.rocky-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/Rocky.app"
RESOURCES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RESOURCES/backend" "$RESOURCES/runtime/bin"

verify_sha() {
  local file="$1" expected="$2" actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "Checksum mismatch: $file" >&2
    rm -f "$file"
    exit 1
  fi
}

echo "Building the native macOS interface…"
mkdir -p "$ROOT/desktop/.cache/modules" "$ROOT/desktop/.cache/swiftpm"
export CLANG_MODULE_CACHE_PATH="$ROOT/desktop/.cache/modules"
export SWIFTPM_MODULECACHE_OVERRIDE="$ROOT/desktop/.cache/modules"
swift build --package-path "$ROOT/desktop" --configuration release --disable-sandbox \
  --scratch-path "$ROOT/desktop/.build" --cache-path "$ROOT/desktop/.cache/swiftpm" \
  -Xswiftc -module-cache-path -Xswiftc "$ROOT/desktop/.cache/modules"
cp "$ROOT/desktop/.build/release/RockyDesktop" "$APP/Contents/MacOS/"
cp "$ROOT/desktop/Info.plist" "$APP/Contents/Info.plist"
cp "$ROOT/pyproject.toml" "$ROOT/uv.lock" "$ROOT/README.md" "$RESOURCES/backend/"
# Copy sources only: never include local environments, credentials, databases or test output.
(cd "$ROOT" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - src) | (cd "$RESOURCES/backend" && tar -xf -)

if [[ -n "${ROCKY_UV_BINARY:-}" ]]; then
  cp -L "$ROCKY_UV_BINARY" "$RESOURCES/runtime/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  cp -L "$(command -v uv)" "$RESOURCES/runtime/bin/uv"
else
  UV_ARCHIVE="uv-$UV_ARCH-apple-darwin.tar.gz"
  UV_PATH="$CACHE/$UV_VERSION-$UV_ARCHIVE"
  UV_BASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
  if [[ ! -f "$UV_PATH" ]]; then curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$UV_BASE/$UV_ARCHIVE" -o "$UV_PATH"; fi
  curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$UV_BASE/$UV_ARCHIVE.sha256" -o "$UV_PATH.sha256"
  UV_SHA="$(awk '{print $1}' "$UV_PATH.sha256")"
  [[ "$UV_SHA" =~ ^[a-f0-9]{64}$ ]] || { echo 'Invalid uv checksum file' >&2; exit 1; }
  verify_sha "$UV_PATH" "$UV_SHA"
  tar -xzf "$UV_PATH" -C "$STAGE" "uv-$UV_ARCH-apple-darwin/uv"
  cp "$STAGE/uv-$UV_ARCH-apple-darwin/uv" "$RESOURCES/runtime/bin/uv"
fi

if [[ -n "${ROCKY_TEMPORAL_BINARY:-}" ]]; then
  cp -L "$ROCKY_TEMPORAL_BINARY" "$RESOURCES/runtime/bin/temporal"
else
  TEMPORAL_ARCHIVE="temporal_cli_${TEMPORAL_VERSION}_darwin_${ARCH}.tar.gz"
  TEMPORAL_PATH="$CACHE/$TEMPORAL_ARCHIVE"
  if [[ ! -f "$TEMPORAL_PATH" ]]; then
    echo "Downloading the official Temporal local engine…"
    curl --proto '=https' --tlsv1.2 -fsSL --retry 2 \
      "https://github.com/temporalio/cli/releases/download/v$TEMPORAL_VERSION/$TEMPORAL_ARCHIVE" -o "$TEMPORAL_PATH"
  fi
  verify_sha "$TEMPORAL_PATH" "$TEMPORAL_SHA"
  tar -xzf "$TEMPORAL_PATH" -C "$STAGE" temporal
  cp "$STAGE/temporal" "$RESOURCES/runtime/bin/temporal"
fi
chmod 755 "$RESOURCES/runtime/bin/uv" "$RESOURCES/runtime/bin/temporal" "$APP/Contents/MacOS/RockyDesktop"
if [[ -f "$ROOT/scripts/make_app_icon.swift" ]]; then
  swift -module-cache-path "$ROOT/desktop/.cache/modules" "$ROOT/scripts/make_app_icon.swift" \
    "$ROOT/desktop/Branding/Rocky-Obsidian.png" "$STAGE/Rocky.iconset"
  iconutil -c icns "$STAGE/Rocky.iconset" -o "$RESOURCES/Rocky.icns"
fi
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
# Replace only the generated product after every build step has succeeded.
rm -rf "$DIST/Rocky.app"
mv "$APP" "$DIST/Rocky.app"
echo "Built: $DIST/Rocky.app"
echo "Open with: open \"$DIST/Rocky.app\""
