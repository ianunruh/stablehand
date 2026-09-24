#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
binary="$root/.tailwindcss"
if [ ! -x "$binary" ]; then
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)
  case "$os" in
    darwin) os=macos ;;
  esac
  case "$arch" in
    x86_64) arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
  esac
  curl -fsSL -o "$binary" "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-${os}-${arch}"
  chmod +x "$binary"
fi
"$binary" -i "$root/src/stablehand/static/input.css" -o "$root/src/stablehand/static/app.css" --minify
