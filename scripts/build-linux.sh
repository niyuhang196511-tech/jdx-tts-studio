#!/usr/bin/env bash
set -euo pipefail

JDX_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$JDX_ROOT"

for tool in uv pnpm cargo pkg-config; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Missing build tool: %s\n' "$tool" >&2
    exit 1
  fi
done
if ! pkg-config --exists gtk+-3.0 webkit2gtk-4.1 javascriptcoregtk-4.1 libsoup-3.0; then
  printf '%s\n' 'Missing Tauri development libraries. See docs/DESKTOP.md for Ubuntu dependencies.' >&2
  exit 1
fi

uv sync --project backend --group dev --locked
pnpm install --frozen-lockfile
backend/.venv/bin/python scripts/build-backend.py
pnpm exec tauri icon src-tauri/icons/app-icon.svg
pnpm exec tauri build --bundles deb,appimage
printf '%s\n' 'Linux packages: src-tauri/target/release/bundle/'
