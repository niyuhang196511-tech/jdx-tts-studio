"""Create checksums and a delivery manifest for the current release."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
EVIDENCE = (
    "docs/verification/official-preview-verification-0.1.2.json",
    "docs/verification/frozen-runtime-smoke-0.1.2.json",
    "docs/verification/frozen-proxy-smoke-0.1.2.json",
    "docs/verification/deb-extract-desktop-smoke-0.1.2.json",
    "docs/verification/appimage-desktop-smoke-0.1.2.json",
    "docs/verification/appimage-extract-run-smoke-0.1.2.json",
)
SOURCE_REQUIRED = (
    "README.md",
    "AGENTS.md",
    "backend/uv.lock",
    "docs/TEST_REPORT.md",
    "pnpm-lock.yaml",
    "scripts/build-linux.sh",
    "scripts/build-windows.ps1",
    "src-tauri/Cargo.lock",
    "src-tauri/tauri.conf.json",
    "tests/ui/workstation.spec.ts",
)
FORBIDDEN_PARTS = {".data", ".dev-session.json", ".venv", "node_modules", "target", "resources"}
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", "credentials.json"}
FORBIDDEN_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    version = package["version"]
    files = (
        f"Jdx-TTS-Studio-{version}-linux-amd64.deb",
        f"Jdx-TTS-Studio-{version}-linux-amd64.AppImage",
        f"Jdx-TTS-Studio-{version}-source.zip",
    )
    packages = []
    for name in files:
        path = RELEASE / name
        if not path.is_file():
            raise FileNotFoundError(path)
        packages.append({"file": name, "bytes": path.stat().st_size, "sha256": sha256(path)})

    source_path = RELEASE / files[-1]
    with zipfile.ZipFile(source_path) as source:
        names = set(source.namelist())
    prefix = "jdx-tts-studio/"
    relative_names = {name.removeprefix(prefix) for name in names if name.startswith(prefix)}
    forbidden = [name for name in relative_names if FORBIDDEN_PARTS.intersection(Path(name).parts)
                 or Path(name).name in FORBIDDEN_NAMES or Path(name).suffix.lower() in FORBIDDEN_SUFFIXES]
    if forbidden:
        raise ValueError(f"Source archive contains excluded paths: {forbidden[:5]}")
    required_present = all(name in relative_names for name in SOURCE_REQUIRED)
    if not required_present:
        raise ValueError("Source archive is missing required build or project files")
    checksum_lines = [f"{item['sha256']}  {item['file']}" for item in packages]
    (RELEASE / f"SHA256SUMS-{version}").write_text("\n".join(checksum_lines) + "\n", encoding="ascii")
    manifest = {
        "version": version,
        "platform": "Ubuntu 26.04.1 LTS x86_64",
        "packages": packages,
        "verification": list(EVIDENCE),
        "limits": [
            "No real cloud synthesis, cloning, or voice design; no billing calls",
            "No Windows build or runtime test",
            "No clean Ubuntu 22.04/24.04 installation test",
            "deb runtime tested after extraction; package not installed into system",
        ],
        "appimage_launch_modes": ["default", "--appimage-extract-and-run"],
        "source_archive_audit": {
            "status": "passed",
            "files": len(relative_names),
            "user_data_media_and_credential_files_excluded": True,
            "required_sources_docs_tests_lockfiles_present": required_present,
        },
    }
    (RELEASE / f"desktop-delivery-manifest-{version}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {RELEASE / f'SHA256SUMS-{version}'} and delivery manifest for {len(packages)} artifacts")


if __name__ == "__main__":
    main()
