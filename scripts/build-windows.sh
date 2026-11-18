#!/usr/bin/env sh
set -eu

case "$(uname -s)" in
  MINGW*|MSYS*) ;;
  *)
    printf '%s\n' '请在 Windows 的 Git Bash 或 MSYS2 中运行此脚本。Linux 构建请使用 scripts/build-linux.sh。' >&2
    exit 1
    ;;
esac

JDX_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$JDX_ROOT"

if ! command -v cmd.exe >/dev/null 2>&1; then
  printf '%s\n' '未找到 cmd.exe，请检查 Windows PATH 配置。' >&2
  exit 1
fi

# Keep CMD switches and the command string intact in Git Bash/MSYS2.
MSYS2_ARG_CONV_EXCL='*' cmd.exe /d /c 'call scripts\build-windows.cmd'
