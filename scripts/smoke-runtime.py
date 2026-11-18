"""Exercise the frozen runtime with no Python environment or cloud account."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import wave

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    executable = args.runtime.resolve()
    token = secrets.token_hex(32)
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(name, None)
    if os.name != "nt":
        env["PATH"] = "/usr/bin:/bin"
    with tempfile.TemporaryDirectory(prefix="Jdx-独立运行验收-") as temporary:
        directory = Path(temporary)
        with (directory / "stderr.log").open("w+") as log:
            process = subprocess.Popen([str(executable), "--data-dir", str(directory / "项目数据"), "--parent-watch"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, env=env)
            try:
                assert process.stdin and process.stdout
                process.stdin.write(json.dumps({"type": "init", "session_token": token}) + "\n")
                process.stdin.flush()
                # Runtime smoke is bounded by the caller's test timeout too.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(process.stdout.readline)
                    try:
                        line = future.result(timeout=30)
                    except TimeoutError:
                        process.kill()
                        raise
                if not line:
                    log.seek(0)
                    raise AssertionError("Runtime failed before handshake: " + log.read()[-3000:])
                handshake = json.loads(line)
                assert handshake["protocol"] == 1 and handshake["pid"] == process.pid
                with httpx.Client(base_url=f"http://127.0.0.1:{handshake['port']}", headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False) as client:
                    assert client.get("/api/v1/health").json()["status"] == "ok"
                    assert client.get("/api/v1/health", headers={"Authorization": "wrong"}).status_code == 401
                    project = client.post("/api/v1/projects", json={"name": "中文路径验收"}).json()
                    audio = io.BytesIO()
                    with wave.open(audio, "wb") as output:
                        output.setparams((1, 2, 48000, 4800, "NONE", "not compressed"))
                        output.writeframes(b"\0\0" * 4800)
                    imported = client.post(f"/api/v1/projects/{project['id']}/import", files={"file": ("验收.wav", audio.getvalue(), "audio/wav")})
                    assert imported.status_code == 200, imported.text
                    export = client.post(f"/api/v1/projects/{project['id']}/export", json={"format": "wav"}).json()
                    for _ in range(200):
                        status = client.get(f"/api/v1/projects/{project['id']}/exports/{export['job_id']}").json()
                        if status["status"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.05)
                    assert status["status"] == "succeeded", status
                    response = client.get(status["download_url"])
                    assert response.status_code == 200 and response.content[:4] == b"RIFF"
                process.stdin.close()
                assert process.wait(timeout=10) == 0
                print(json.dumps({"status": "passed", "runtime": str(executable), "checks": ["isolated environment", "Chinese path", "dynamic port handshake", "authentication", "audio import", "FFmpeg WAV export", "parent EOF shutdown"]}, ensure_ascii=False))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
