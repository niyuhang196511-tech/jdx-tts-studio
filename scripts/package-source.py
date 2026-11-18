"""Create a reproducible source delivery excluding credentials and user projects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("src", "backend", "src-tauri", "scripts", "tests", "docs")
ROOT_FILES = ("README.md", "AGENTS.md", ".gitignore", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
              "index.html", "vite.config.ts", "playwright.config.ts", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json")
EXCLUDE_PARTS = {".venv", "__pycache__", ".pytest_cache", "target", "node_modules", "resources"}
EXCLUDE_SUFFIXES = {".pyc", ".tsbuildinfo", ".log", ".appimage", ".deb", ".zip", ".wav", ".mp3", ".flac", ".ogg", ".m4a"}
EXCLUDE_NAMES = {".dev-session.json", ".env", ".env.local", ".env.production", "credentials.json"}


def main() -> None:
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    version = json.loads((ROOT / "package.json").read_text())["version"]
    output = release / f"Jdx-TTS-Studio-{version}-source.zip"
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for directory in DIRECTORIES:
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if path.is_file() and not path.is_symlink() and not any(part in EXCLUDE_PARTS for part in relative.parts):
                if path.name not in EXCLUDE_NAMES and path.suffix.lower() not in EXCLUDE_SUFFIXES:
                    files.append(path)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            archive.write(path, "jdx-tts-studio/" + path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n")
    print(f"{output} ({output.stat().st_size} bytes; {len(files)} source files)")


if __name__ == "__main__":
    main()
