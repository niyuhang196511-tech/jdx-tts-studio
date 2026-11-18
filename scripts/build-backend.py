"""Build a native, self-contained backend directory for the current platform."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def find_binary(name: str, override: str | None, *, optional: bool = False) -> Path | None:
    candidate = override or os.environ.get(f"JDX_{name.upper()}") or shutil.which(name)
    if not candidate and name == "ffmpeg":
        try:
            import imageio_ffmpeg

            candidate = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    if not candidate and optional:
        return None
    if not candidate:
        raise RuntimeError(
            f"Missing {name}. Install a redistributable native FFmpeg build or set "
            f"JDX_{name.upper()} to its executable."
        )
    path = Path(candidate).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"{name} executable not found: {path}")
    subprocess.run([str(path), "-version"], check=True, capture_output=True, timeout=20)
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        if platform.system() not in {"Linux", "Windows"}:
            raise RuntimeError("Build on the target Windows or Linux platform; cross-freezing is unsupported.")
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("Use the backend's Python 3.12 virtual environment to build the runtime.")
        if importlib.util.find_spec("PyInstaller") is None:
            raise RuntimeError("PyInstaller is missing. Run uv sync --project backend --group dev --locked.")
        entry = ROOT / "backend" / "jdx_backend" / "__main__.py"
        if not entry.is_file():
            raise RuntimeError("The backend entrypoint does not exist.")
        ffmpeg = find_binary("ffmpeg", args.ffmpeg)
        ffprobe = find_binary("ffprobe", args.ffprobe, optional=True)
        if args.check_only:
            print(json.dumps({"platform": platform.system(), "python": platform.python_version(), "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe) if ffprobe else "FFmpeg metadata fallback"}))
            return 0
        build = ROOT / "build" / "backend"
        build.mkdir(parents=True, exist_ok=True)
        # Executing package __main__.py as a PyInstaller script loses its
        # package context and breaks relative imports. Keep a tiny absolute
        # import launcher inside ignored build output.
        launcher = build / "freeze_entry.py"
        launcher.write_text(
            "import multiprocessing\n"
            "if __name__ == '__main__':\n"
            "    multiprocessing.freeze_support()\n"
            "    from jdx_backend.__main__ import main\n"
            "    main()\n",
            encoding="utf-8",
        )
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
            "--name", "jdx-backend", "--paths", str(ROOT / "backend"),
            "--distpath", str(build / "dist"), "--workpath", str(build / "work"),
            "--specpath", str(build), "--collect-submodules", "jdx_backend",
            "--exclude-module", "pytest", "--exclude-module", "imageio_ffmpeg",
            "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
            "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
            "--hidden-import", "uvicorn.lifespan.on",
            # These are optional imports upstream but required by our proxy
            # support in both HTTPX and WebSocket remote requests.
            "--hidden-import", "socksio", "--hidden-import", "python_socks.async_.asyncio",
        ]
        extension = ".exe" if os.name == "nt" else ""
        staged_tools = build / "native-tools"
        staged_tools.mkdir(exist_ok=True)
        for name, binary in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
            if binary:
                # Let PyInstaller inspect shared-library dependencies as well.
                staged = staged_tools / f"{name}{extension}"
                shutil.copy2(binary, staged)
                command.extend(["--add-binary", f"{staged}{os.pathsep}bin"])
        command.append(str(launcher))
        subprocess.run(command, cwd=ROOT, check=True)
        frozen = build / "dist" / "jdx-backend"
        license_result = subprocess.run([str(ffmpeg), "-L"], capture_output=True, text=True, check=True, timeout=20)
        (frozen / "FFMPEG-LICENSE.txt").write_text(license_result.stdout + license_result.stderr, encoding="utf-8")
        manifest = {
            "format": 1,
            "platform": platform.system().lower(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "files": [
                {"path": str(path.relative_to(frozen)).replace("\\", "/"), "size": path.stat().st_size, "sha256": sha256(path)}
                for path in sorted(frozen.rglob("*")) if path.is_file()
            ],
        }
        (frozen / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        destination = ROOT / "src-tauri" / "resources" / "backend"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(frozen, destination)
        # Tauri's resource copying is additive. Remove only generated staging
        # trees so deleted/renamed runtime files cannot survive into a new
        # installer (for example duplicate FFmpeg copies from an older build).
        config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", str(ROOT / "src-tauri" / "target")))
        if not target_root.is_absolute():
            target_root = ROOT / target_root
        for profile in ("debug", "release"):
            copied_runtime = target_root / profile / "backend"
            if copied_runtime.exists():
                shutil.rmtree(copied_runtime)
        bundle_root = target_root / "release" / "bundle"
        app_dir = bundle_root / "appimage" / f"{config['productName']}.AppDir"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        for staging in (bundle_root / "deb").glob(f"{config['productName']}_{config['version']}_*"):
            if staging.is_dir():
                shutil.rmtree(staging)
        print(f"Native backend runtime: {destination}")
        print("Keep this entire directory, including _internal and bin, in the installer.")
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Backend build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
