"""Explicit downloads of fixed official voice samples; no synthesis requests."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import httpx
import soundfile as sf

from .audio import normalize_import
from .network import ProxyConfigurationError, http_client


async def cache_official_sample(url: str, destination: Path) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
        "g-adoc.alcasset.com", "help-static-aliyun-doc.aliyuncs.com",
    } or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in (None, 443) or not parsed.path.endswith(".wav")):
        raise ValueError("官方试听地址不受支持，请更新声音库")
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="official-", dir=destination.parent) as temporary:
        raw, normalized = Path(temporary) / "sample.wav", Path(temporary) / "preview.wav"
        try:
            async with http_client(url) as client:
                async with client.stream("GET", url, timeout=30) as response:
                    response.raise_for_status()
                    size = 0
                    with raw.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 20 * 1024 * 1024:
                                raise ValueError("官方试听音频超过大小限制")
                            await asyncio.to_thread(output.write, chunk)
            info = await asyncio.to_thread(sf.info, raw)
            if not 0 < info.duration <= 180 or info.format != "WAV":
                raise ValueError("官方试听音频格式或时长不正确")
            await normalize_import(raw, normalized)
            # Never replace another process's valid cached sample.
            try:
                os.link(normalized, destination)
            except FileExistsError:
                pass
        except (httpx.HTTPError, ProxyConfigurationError):
            raise ValueError("官方样音下载失败，请检查网络或代理后重试。已缓存的音色仍可离线试听。") from None
        except sf.LibsndfileError:
            raise ValueError("官方样音格式不可读取，请稍后重试") from None
