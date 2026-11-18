from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import secrets
import socket
import sys
import threading

import uvicorn
from platformdirs import user_data_path

from .app import create_app


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=user_data_path("JdxTTSStudio", "Jdx"))
    parser.add_argument("--parent-watch", action="store_true")
    parser.add_argument("--browser-dev", action="store_true")
    options = parser.parse_args()
    options.data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = options.data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / "backend.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler(sys.stderr)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        init = json.loads(sys.stdin.readline())
        if init.get("type") != "init" or len(init.get("session_token", "")) < 32:
            raise ValueError("invalid bootstrap")
    except Exception:
        logging.error("invalid_bootstrap")
        raise SystemExit(2)
    key_store: dict[str, str] = {}
    app = create_app(options.data_dir, init["session_token"], browser_dev=options.browser_dev, secrets=key_store)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False))

    def controls() -> None:
        for line in sys.stdin:
            try:
                event = json.loads(line)
                if event.get("type") == "secret":
                    identifier = event.get("connection_id")
                    key = event.get("key", "")
                    if isinstance(identifier, str) and isinstance(key, str):
                        if key:
                            key_store[identifier] = key
                        else:
                            key_store.pop(identifier, None)
                elif event.get("type") == "shutdown":
                    server.should_exit = True
            except (ValueError, TypeError):
                logging.warning("invalid_control_message")
        if options.parent_watch:
            server.should_exit = True

    async def run() -> None:
        thread = threading.Thread(target=controls, name="jdx-parent-control", daemon=True)
        thread.start()
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if task.done():
                await task
                return
            await asyncio.sleep(0.025)
        print(json.dumps({"type": "ready", "protocol": 1, "port": sock.getsockname()[1], "pid": os.getpid()}), flush=True)
        await task

    try:
        asyncio.run(run())
    finally:
        sock.close()
        key_store.clear()


if __name__ == "__main__":
    main()
