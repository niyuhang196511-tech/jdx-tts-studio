"""Verify frozen proxy dependencies against local rejecting peers; no cloud calls."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import secrets
import socketserver
import subprocess
import tempfile
import threading
import time

import httpx


class RejectingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.observed: list[str] = []
        super().__init__(("127.0.0.1", 0), ProxyHandler)


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        first = self.rfile.read(1)
        if first == b"\x05":
            methods = self.rfile.read(self.rfile.read(1)[0])
            assert 0 in methods
            self.wfile.write(b"\x05\x00")
            self.wfile.flush()
            header = self.rfile.read(4)
            assert header[:2] == b"\x05\x01"
            kind = header[3]
            size = self.rfile.read(1)[0] if kind == 3 else (4 if kind == 1 else 16)
            self.rfile.read(size + 2)
            self.server.observed.append("socks5")
            # Reject before opening any upstream socket or accepting TLS/data.
            self.wfile.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
        else:
            request = first + self.rfile.readline(8192)
            assert request.startswith(b"CONNECT ")
            self.server.observed.append("http-connect")
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        self.wfile.flush()


def check_runtime(executable: Path, variant: str) -> dict:
    with RejectingProxy() as proxy, tempfile.TemporaryDirectory(prefix="Jdx-代理验收-") as temporary:
        worker = threading.Thread(target=proxy.serve_forever, daemon=True)
        worker.start()
        env = {name: value for name, value in os.environ.items()
               if not name.lower().endswith("_proxy")
               and name not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "REQUEST_METHOD"}}
        if os.name != "nt":
            env["PATH"] = "/usr/bin:/bin"
        address = f"127.0.0.1:{proxy.server_address[1]}"
        env.update(ALL_PROXY=f"socks://{address}", all_proxy=f"socks://{address}", NO_PROXY="", no_proxy="")
        if variant == "mixed-http-socks":
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[name] = f"http://{address}"
        if variant == "websocket-socks5h":
            env["wss_proxy"] = f"socks5h://{address}"
        token = secrets.token_hex(32)
        fake_key = "fake-key-local-proxy-verification-only"
        with (Path(temporary) / "stderr.log").open("w+") as log:
            process = subprocess.Popen(
                [str(executable), "--data-dir", temporary, "--parent-watch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, env=env,
            )
            try:
                assert process.stdin and process.stdout
                process.stdin.write(json.dumps({"type": "init", "session_token": token}) + "\n")
                process.stdin.flush()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(process.stdout.readline)
                    try:
                        handshake = json.loads(future.result(timeout=30))
                    except TimeoutError:
                        process.kill()
                        raise
                with httpx.Client(base_url=f"http://127.0.0.1:{handshake['port']}/api/v1",
                                  headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=10) as client:
                    connection = client.post("/connections", json={
                        "name": "本地代理验收", "workspace_id": "local-probe",
                        "base_url": "https://proxy-probe.aliyuncs.com",
                    })
                    connection.raise_for_status()
                    identifier = connection.json()["id"]
                    process.stdin.write(json.dumps({"type": "secret", "connection_id": identifier, "key": fake_key}) + "\n")
                    process.stdin.flush()
                    deadline = time.monotonic() + 5
                    while not client.get("/connections").json()[0]["has_credential"]:
                        assert time.monotonic() < deadline, "Credential control channel timed out"
                        time.sleep(0.02)
                    if variant == "websocket-socks5h":
                        project = client.post("/projects", json={"name": "代理依赖验收"}).json()
                        imported = client.post(f"/projects/{project['id']}/import", files={
                            "file": ("验收.txt", "本地拒绝测试。".encode(), "text/plain"),
                        })
                        imported.raise_for_status()
                        response = client.post(f"/projects/{project['id']}/tasks", json={
                            "line_ids": [imported.json()["lines"][0]["id"]], "transport": "websocket",
                        })
                        response.raise_for_status()
                        deadline = time.monotonic() + 10
                        while True:
                            jobs = client.get(f"/projects/{project['id']}/tasks").json()
                            if jobs and jobs[0]["status"] in {"failed", "uncertain"}:
                                error = jobs[0]["error"]
                                break
                            assert time.monotonic() < deadline, "Proxy job failed to settle"
                            time.sleep(0.05)
                    else:
                        response = client.post(f"/connections/{identifier}/test")
                        assert response.status_code == 502, response.text
                        error = response.json()["error"]["message"]
                    assert "代理" in error or "网络" in error, error
                    assert not any(value in error for value in (fake_key, token, "Unknown scheme", "python-socks", "socksio")), error
                    expected = "http-connect" if variant == "mixed-http-socks" else "socks5"
                    assert proxy.observed == [expected], proxy.observed
                process.stdin.close()
                assert process.wait(timeout=10) == 0
                log.seek(0)
                logs = log.read()
                assert fake_key not in logs and token not in logs
                return {"variant": variant, "proxy_protocol": expected, "error": error, "status": "passed"}
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                proxy.shutdown()
                worker.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    executable = parser.parse_args().runtime.resolve()
    checks = [check_runtime(executable, variant) for variant in
              ("socks-alias", "mixed-http-socks", "websocket-socks5h")]
    print(json.dumps({"status": "passed", "runtime": str(executable), "cloud_requests": 0, "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
