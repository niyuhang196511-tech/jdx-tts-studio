"""Verified Qwen-Audio-TTS cloud protocol, independent of project persistence.

Contract checked against Alibaba Cloud Model Studio HTTP/client/server events,
voice list and voice-enrollment documentation on 2026-11-17. Cloud requests are
never automatically replayed: a disconnect may follow a billable submission.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import ipaddress
import json
import mimetypes
import re
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import uuid
from xml.etree import ElementTree as ET

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .network import ProxyConfigurationError, http_client, proxy_for_url


MODELS = [
    {
        "id": model,
        "name": name,
        "description": description,
        "instruction": True,
        "ssml": True,
        "streaming": True,
        "custom_voice": True,
        "verified": True,
    }
    for model, name, description in [
        ("qwen-audio-3.1-tts-plus", "千问 3.1 · Plus", "旗舰语音合成，支持情绪指令及专属音色"),
        ("qwen-audio-3.1-tts-flash", "千问 3.1 · Flash", "快速语音合成，支持多语种及情绪指令"),
        ("qwen-audio-3.0-tts-plus", "千问 3.0 · Plus", "语音合成，支持克隆与设计音色"),
        ("qwen-audio-3.0-tts-flash", "千问 3.0 · Flash", "快速语音合成，支持情绪指令"),
    ]
]


from .voice_catalog import SYSTEM_VOICES

_KNOWN_MODELS = frozenset(item["id"] for item in MODELS)
_LANGUAGES = frozenset("zh en fr de ja ko ru pt th id vi es it ms fil ar other".split())
_MAX_AUDIO_BYTES = 256 * 1024 * 1024
_SYNTHESIS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
_ENROLLMENT_PATH = "/api/v1/services/audio/tts/customization"


@dataclass
class CloudConfig:
    id: str = ""
    workspace_id: str = ""
    region: str = "cn-beijing"
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model_id: str = "qwen-audio-3.1-tts-plus"
    voice_id: str = "longanhuan_v3.1"
    verified_capabilities: frozenset[str] = field(default_factory=frozenset)
    http_client: httpx.AsyncClient | None = field(default=None, repr=False, compare=False)


class CloudError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        uncertain: bool = False,
        request_id: str | None = None,
        retryable: bool = False,
        *,
        usage: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain
        self.request_id = request_id
        self.retryable = retryable
        self.usage = usage or {}


@dataclass
class SynthesisResult:
    audio: bytes
    request_id: str | None
    usage: dict
    format: str = "wav"
    word_timestamps: list = field(default_factory=list)


Progress = Callable[[float], Awaitable[None]]
Chunk = Callable[[bytes], Awaitable[None]]


def endpoint(config: CloudConfig) -> str:
    if config.region not in {"cn-beijing", "ap-southeast-1", "custom"}:
        raise CloudError("INVALID_REGION", "请选择北京或新加坡地域。")
    if not config.api_key.strip():
        raise CloudError("MISSING_CREDENTIAL", "请先为当前百炼连接配置 API Key。")
    if config.base_url.strip():
        value = config.base_url.strip().rstrip("/")
    else:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", config.workspace_id):
            raise CloudError("INVALID_WORKSPACE", "请输入正确的百炼业务空间 ID。")
        value = f"https://{config.workspace_id}.{config.region}.maas.aliyuncs.com"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CloudError("INVALID_ENDPOINT", "服务地址须为 HTTPS 根地址，不能包含凭据、查询参数或接口路径。")
    _reject_private_host(parsed.hostname)
    return value


def normalize_connection(data: dict[str, Any]) -> dict[str, Any]:
    """Accept an explicit base URL; retain old workspace profiles compatibly."""
    result = dict(data)
    base_url = str(result.get("base_url") or "").strip().rstrip("/")
    if not base_url and result.get("workspace_id"):
        workspace = result["workspace_id"]
        if re.fullmatch(r"[A-Za-z0-9-]{1,128}", workspace):
            base_url = f"https://{workspace}.{result.get('region', 'cn-beijing')}.maas.aliyuncs.com"
    if base_url:
        # Local validation only; never send a key or request while saving.
        endpoint(CloudConfig(base_url=base_url, api_key="validation-only"))
        parsed = urlsplit(base_url)
        match = re.fullmatch(r"([A-Za-z0-9-]+)\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com",
                             parsed.hostname or "")
        if match:
            result["workspace_id"], result["region"] = match.groups()
        else:
            result["workspace_id"] = ""
            result["region"] = "custom"
        result["base_url"] = base_url
    return result


def _reject_private_host(host: str) -> None:
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local")):
        raise CloudError("UNSAFE_ENDPOINT", "远程服务地址不能指向本机或内网地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise CloudError("UNSAFE_ENDPOINT", "远程服务地址不能指向本机或内网地址。")


async def _validate_endpoint_dns(base_url: str) -> None:
    host = urlsplit(base_url).hostname or ""
    if host.endswith(".aliyuncs.com"):
        return
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CloudError("DNS_FAILED", "无法解析远程服务地址，请检查网络或代理。") from exc
    for address in addresses:
        _reject_private_host(address[4][0])


def _headers(config: CloudConfig) -> dict:
    result = {"Authorization": f"Bearer {config.api_key}", "User-Agent": "Jdx-TTS-Studio/0.1"}
    if config.workspace_id:
        result["X-DashScope-WorkSpace"] = config.workspace_id
    return result


def _usage(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if key in {"input_tokens", "output_tokens", "total_tokens", "characters", "count"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    }


def _request_id(value: Any) -> str | None:
    return value[:128] if isinstance(value, str) else None


def _provider_error(status: int, body: Any = None, *, submitted: bool = False) -> CloudError:
    data = body if isinstance(body, dict) else {}
    request_id = _request_id(data.get("request_id"))
    code = str(data.get("code", ""))
    if status in (401, 403) or "ApiKey" in code or "Authentication" in code:
        return CloudError("AUTH_FAILED", "百炼鉴权失败，请检查 API Key、地域和业务空间。", request_id=request_id)
    if status == 429 or "Throttl" in code or "RateLimit" in code:
        return CloudError("RATE_LIMIT", "百炼请求过于频繁，请降低并发后重试。", request_id=request_id, retryable=not submitted)
    if status == 402 or any(word in code.lower() for word in ("arrear", "quota", "balance")):
        return CloudError("QUOTA_EXCEEDED", "百炼额度不足或账户欠费，请检查账户余额与模型配额。", request_id=request_id)
    if status >= 500:
        return CloudError("PROVIDER_UNAVAILABLE", "百炼服务暂时异常；请求可能已受理，请检查任务记录后再重试。", uncertain=True, request_id=request_id)
    return CloudError("PROVIDER_REJECTED", "百炼拒绝了请求，请核对模型、音色及参数是否匹配。", request_id=request_id)


def _response_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CloudError("INVALID_RESPONSE", "百炼返回了无法解析的数据，请检查服务地址。", uncertain=True) from exc
    if not isinstance(data, dict):
        raise CloudError("INVALID_RESPONSE", "百炼返回格式不正确。", uncertain=True)
    return data


def _voice_capability(model: str, voice_id: str, feature: str) -> bool:
    for voice in SYSTEM_VOICES:
        if voice["target_model"] == model and voice["voice_id"] == voice_id:
            return bool(voice.get(feature))
    if voice_id.startswith(model + "-"):
        if "-vd-" in voice_id:
            return feature == "instruction"
        return True
    return False


def synthesis_input(config: CloudConfig, text: str, settings: dict) -> tuple[str, dict]:
    """Build one plain-text/escaped-SSML request without local audio transforms."""
    model = settings.get("model_id") or config.model_id
    voice = settings.get("voice_id") or config.voice_id
    if not isinstance(text, str) or not text.strip():
        raise CloudError("EMPTY_TEXT", "台词内容不能为空。")
    if len(text) > 20_000:
        raise CloudError("TEXT_TOO_LONG", "单句超过 20000 字符，请先拆分台词。")
    if not model or not voice:
        raise CloudError("MISSING_MODEL_OR_VOICE", "请选择模型与音色。")
    known_voice = any(v["voice_id"] == voice for v in SYSTEM_VOICES)
    if known_voice and not any(v["voice_id"] == voice and v["target_model"] == model for v in SYSTEM_VOICES):
        raise CloudError("VOICE_MODEL_MISMATCH", "当前音色不属于所选模型，请重新选择音色。")
    profile = next((v for v in SYSTEM_VOICES if v["voice_id"] == voice and v["target_model"] == model), None)
    if profile and profile.get("regions") and config.region not in profile["regions"]:
        raise CloudError("VOICE_REGION_MISMATCH", "此音色暂未在当前服务地址开放，请选择该连接可用的音色。")
    if voice.startswith("qwen-audio-") and not voice.startswith(model + "-"):
        raise CloudError("VOICE_MODEL_MISMATCH", "专属音色绑定了其他目标模型，请选择对应模型。")
    params: dict[str, Any] = {"voice": voice, "format": "wav", "sample_rate": 48000}
    capabilities = config.verified_capabilities
    verified = model in _KNOWN_MODELS
    for key, default, low, high in (("rate", 1.0, 0.5, 2.0), ("pitch", 1.0, 0.5, 2.0), ("volume", 50, 0, 100)):
        value = settings.get(key, default)
        if not isinstance(value, (float, int)) or isinstance(value, bool) or not low <= value <= high:
            raise CloudError("INVALID_PARAMETER", f"{ {'rate': '语速', 'pitch': '音调', 'volume': '音量'}[key]}超出支持范围。")
        if key == "volume" and value != int(value):
            raise CloudError("INVALID_PARAMETER", "音量应为 0 到 100 的整数。")
        if verified or key in capabilities:
            params[key] = value
        elif value != default:
            raise CloudError("UNVERIFIED_CAPABILITY", "自定义模型尚未验证此参数能力，请先使用默认参数。")
    language = settings.get("language", "zh")
    if language not in _LANGUAGES or (language == "other" and model != "qwen-audio-3.1-tts-flash"):
        raise CloudError("UNSUPPORTED_LANGUAGE", "当前模型不支持所选语言。")
    if verified or "language" in capabilities:
        params["language_hints"] = [language]
    seed = settings.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 65535:
        raise CloudError("INVALID_PARAMETER", "随机种子应为 0 到 65535 的整数。")
    if verified or "seed" in capabilities:
        params["seed"] = seed
    instruction = str(settings.get("instruction", "")).strip()
    emotion = str(settings.get("emotion", "自然")).strip()
    instructions = []
    if emotion and emotion != "自然":
        instructions.append(f"请用{emotion}的情绪表达。")
    emphasis = [str(word).strip() for word in settings.get("emphasis_words", []) if str(word).strip()]
    if emphasis:
        instructions.append("请适当强调这些词语：" + "、".join(emphasis) + "。")
    if instruction:
        instructions.append(instruction)
    if instructions:
        if not (verified or "instruction" in capabilities):
            raise CloudError("UNVERIFIED_CAPABILITY", "自定义模型尚未验证情绪和导演指令能力。")
        if known_voice and not _voice_capability(model, voice, "instruction"):
            raise CloudError("UNSUPPORTED_INSTRUCTION", "当前音色不支持情绪或导演指令，请切换音色。")
        params["instruction"] = "\n".join(instructions)
    pauses = settings.get("pause_after_words") or {}
    pronunciations = settings.get("pronunciation_overrides") or {}
    if (pauses or pronunciations) and not settings.get("ssml"):
        raise CloudError("SSML_REQUIRED", "句内停顿和发音修正需要启用精细控制，并选择支持的音色。")
    if settings.get("ssml"):
        if not (verified or "ssml" in capabilities) or not _voice_capability(model, voice, "ssml"):
            raise CloudError("UNSUPPORTED_SSML", "当前模型或音色未确认支持 SSML；设计音色不支持此功能。")
        root = ET.Element("speak")
        words = set(pauses) | set(pronunciations)
        if any(not word or word not in text for word in words):
            raise CloudError("SSML_WORD_MISSING", "精细控制中有词语不在当前台词中，请修改或移除该设置。")
        if words:
            pattern = re.compile("|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)))
            cursor = 0
            last = None
            for match in pattern.finditer(text):
                before = text[cursor:match.start()]
                if last is None:
                    root.text = (root.text or "") + before
                else:
                    last.tail = (last.tail or "") + before
                word = match.group()
                if word in pronunciations:
                    last = ET.SubElement(root, "phoneme", {"alphabet": "py", "ph": pronunciations[word]})
                    last.text = word
                elif last is None:
                    root.text = (root.text or "") + word
                else:
                    last.tail = (last.tail or "") + word
                if word in pauses:
                    duration = pauses[word]
                    if not isinstance(duration, int) or isinstance(duration, bool) or not 50 <= duration <= 10000:
                        raise CloudError("INVALID_PAUSE", "句内停顿须为 50–10000 毫秒。")
                    last = ET.SubElement(root, "break", {"time": f"{duration}ms"})
                cursor = match.end()
            if last is None:
                root.text = (root.text or "") + text[cursor:]
            else:
                last.tail = (last.tail or "") + text[cursor:]
        else:
            root.text = text
        text = ET.tostring(root, encoding="unicode")
        params["enable_ssml"] = True
    if settings.get("word_timestamp_enabled"):
        if not _voice_capability(model, voice, "word_timestamps"):
            raise CloudError("UNSUPPORTED_TIMESTAMPS", "当前音色不支持字级时间戳。")
        params["word_timestamp_enabled"] = True
    return model, {"text": text, **params}


@asynccontextmanager
async def _client(config: CloudConfig, url: str):
    if config.http_client is not None:
        yield config.http_client
    else:
        try:
            client = http_client(url)
        except ProxyConfigurationError as exc:
            raise CloudError("INVALID_PROXY_CONFIGURATION", str(exc)) from None
        async with client:
            yield client


async def _notify(callback: Progress | None, value: float) -> None:
    if callback is not None:
        await callback(value)


async def _post(config: CloudConfig, path: str, body: dict) -> dict:
    base = endpoint(config)
    await _validate_endpoint_dns(base)
    try:
        async with _client(config, base + path) as client:
            response = await client.post(base + path, headers=_headers(config), json=body)
    except httpx.ProxyError:
        raise CloudError("PROXY_CONNECT_FAILED", "无法连接系统代理，请确认代理软件正在运行、代理端口及认证信息正确。") from None
    except httpx.ConnectError as exc:
        raise CloudError("CONNECT_FAILED", "无法连接百炼服务，请检查网络或代理。") from exc
    except httpx.RequestError as exc:
        raise CloudError("REQUEST_UNCERTAIN", "远程连接中断，请求可能已被受理，请检查记录后再重试。", uncertain=True) from exc
    if response.is_error:
        try:
            error_body = response.json()
        except ValueError:
            error_body = None
        raise _provider_error(response.status_code, error_body)
    data = _response_json(response)
    if data.get("code"):
        raise _provider_error(response.status_code, data)
    return data


def _audio_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise CloudError("UNSAFE_AUDIO_URL", "远程音频地址包含无效端口，已停止下载。", uncertain=True) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
        or not host.endswith((".aliyuncs.com", ".alicdn.com", ".aliyun.com"))
    ):
        raise CloudError("UNSAFE_AUDIO_URL", "远程音频地址不在百炼可信存储域名内，已停止下载。", uncertain=True)
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


async def _download_audio(config: CloudConfig, value: str) -> bytes:
    url = _audio_url(value)
    try:
        # No API Authorization headers are forwarded to object storage.
        for _ in range(4):
            async with _client(config, url) as client:
                async with client.stream("GET", url, headers={"User-Agent": "Jdx-TTS-Studio/0.1"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        url = _audio_url(str(response.url.join(location)))
                        continue
                    if response.is_error:
                        raise CloudError("AUDIO_DOWNLOAD_FAILED", "语音已生成，但音频下载失败；请保留任务记录。", uncertain=True)
                    audio = bytearray()
                    async for chunk in response.aiter_bytes():
                        audio.extend(chunk)
                        if len(audio) > _MAX_AUDIO_BYTES:
                            raise CloudError("AUDIO_TOO_LARGE", "远程音频超过单次下载大小限制。", uncertain=True)
                    if not audio:
                        raise CloudError("EMPTY_AUDIO", "百炼返回了空音频。", uncertain=True)
                    return bytes(audio)
    except CloudError as exc:
        # A synthesis request has already succeeded before this stage. Even a
        # local proxy configuration failure must not trigger a new paid call.
        exc.uncertain = True
        raise
    except httpx.RequestError as exc:
        raise CloudError("AUDIO_DOWNLOAD_FAILED", "语音已生成，但下载连接中断；请检查网络。", uncertain=True) from exc
    raise CloudError("AUDIO_REDIRECT_LIMIT", "音频下载重定向次数过多，已停止下载。", uncertain=True)


async def synthesize(
    config: CloudConfig,
    text: str,
    settings: dict,
    *,
    transport: str = "http",
    cancel_event: asyncio.Event | None = None,
    on_progress: Progress | None = None,
    on_chunk: Chunk | None = None,
) -> SynthesisResult:
    if cancel_event is not None and cancel_event.is_set():
        raise CloudError("CANCELLED", "任务已取消。")
    model, payload = synthesis_input(config, text, settings)
    if transport == "websocket":
        return await _synthesize_websocket(config, model, payload, cancel_event, on_progress, on_chunk)
    if transport != "http":
        raise CloudError("INVALID_TRANSPORT", "请选择 HTTP 或 WebSocket 合成方式。")
    await _notify(on_progress, 0.05)
    request = asyncio.create_task(_post(config, _SYNTHESIS_PATH, {"model": model, "input": payload}))
    cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        if cancellation is not None:
            done, _ = await asyncio.wait({request, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done and request not in done:
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
                raise CloudError("CANCEL_UNCONFIRMED", "本地已停止等待；HTTP 请求可能仍在云端执行，费用以百炼账单为准。", uncertain=True)
        data = await request
    finally:
        if cancellation is not None:
            cancellation.cancel()
            with suppress(asyncio.CancelledError):
                await cancellation
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
    request_id = _request_id(data.get("request_id"))
    usage = _usage(data.get("usage"))
    await _notify(on_progress, 0.7)
    if cancel_event is not None and cancel_event.is_set():
        raise CloudError("CANCELLED", "任务已取消；云端已发生的用量仍会计费。", request_id=request_id, usage=usage)
    output = data.get("output", {})
    audio_data = output.get("audio", {}) if isinstance(output, dict) else {}
    if audio_data.get("data"):
        try:
            audio = base64.b64decode(audio_data["data"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CloudError("INVALID_AUDIO", "百炼返回的音频数据损坏。", uncertain=True, request_id=request_id) from exc
    elif isinstance(audio_data.get("url"), str):
        try:
            audio = await _download_audio(config, audio_data["url"])
        except CloudError as exc:
            exc.request_id = request_id
            exc.usage = usage
            raise
    else:
        raise CloudError("MISSING_AUDIO", "百炼未返回可保存的音频。", uncertain=True, request_id=request_id, usage=usage)
    if cancel_event is not None and cancel_event.is_set():
        raise CloudError("CANCELLED", "任务已取消，未发布生成音频。", request_id=request_id, usage=usage)
    if not audio or len(audio) > _MAX_AUDIO_BYTES:
        raise CloudError("INVALID_AUDIO", "音频为空或超过大小限制。", uncertain=True, request_id=request_id)
    if on_chunk is not None:
        await on_chunk(audio)
    await _notify(on_progress, 1.0)
    return SynthesisResult(audio, request_id, usage)


async def _synthesize_websocket(
    config: CloudConfig,
    model: str,
    payload: dict,
    cancel_event: asyncio.Event | None,
    on_progress: Progress | None,
    on_chunk: Chunk | None,
) -> SynthesisResult:
    base = endpoint(config)
    await _validate_endpoint_dns(base)
    parsed = urlsplit(base)
    uri = urlunsplit(("wss", parsed.netloc, "/api-ws/v1/inference", "", ""))
    task_id = str(uuid.uuid4())
    text = payload["text"]
    parameters = {key: value for key, value in payload.items() if key != "text"}
    parameters["text_type"] = "PlainText"
    parameters["format"] = "pcm"
    pcm = bytearray()
    usage: dict = {}
    words: dict[tuple, dict] = {}
    request_id: str | None = None
    submitted = False
    started = False
    cancel_sent = False

    def command(action: str, body: dict) -> str:
        return json.dumps({"header": {"action": action, "task_id": task_id, "streaming": "duplex"}, "payload": body}, ensure_ascii=False)

    try:
        proxy = proxy_for_url(uri)
        async with websocket_connect(uri, proxy=proxy, additional_headers=_headers(config), open_timeout=15, close_timeout=5, max_size=16 * 1024 * 1024) as ws:
            await ws.send(command("run-task", {"task_group": "audio", "task": "tts", "function": "SpeechSynthesizer", "model": model, "parameters": parameters, "input": {}}))
            submitted = True
            await _notify(on_progress, 0.1)
            cancel_waiter = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            receive: asyncio.Task | None = None
            try:
                while True:
                    if receive is None:
                        receive = asyncio.create_task(ws.recv())
                    waiting = {receive}
                    if cancel_waiter is not None and not cancel_sent:
                        waiting.add(cancel_waiter)
                    done, _ = await asyncio.wait(waiting, timeout=15 if cancel_sent else 180, return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        raise CloudError("CANCEL_UNCONFIRMED" if cancel_sent else "REQUEST_UNCERTAIN", "未收到云端结束确认，请检查任务记录后再重试。", uncertain=True, request_id=request_id, usage=usage)
                    if cancel_waiter is not None and cancel_waiter in done and not cancel_sent:
                        cancel_sent = True
                        if started:
                            await ws.send(command("finish-task", {"input": {"directive": "cancel"}}))
                    if receive not in done:
                        continue
                    message = receive.result()
                    receive = None
                    if isinstance(message, bytes):
                        if not cancel_sent:
                            pcm.extend(message)
                            if len(pcm) > _MAX_AUDIO_BYTES:
                                raise CloudError("AUDIO_TOO_LARGE", "流式音频超过单次大小限制。", uncertain=True, request_id=request_id)
                            if on_chunk is not None:
                                await on_chunk(message)
                            await _notify(on_progress, min(0.9, 0.2 + len(pcm) / (48000 * 2 * max(1, len(text) / 3))))
                        continue
                    try:
                        event = json.loads(message)
                    except (ValueError, TypeError) as exc:
                        raise CloudError("INVALID_RESPONSE", "云端流式事件格式不正确。", uncertain=True, request_id=request_id) from exc
                    if not isinstance(event, dict) or not isinstance(event.get("header"), dict) or not isinstance(event.get("payload", {}), dict):
                        raise CloudError("INVALID_RESPONSE", "云端流式事件结构不正确。", uncertain=True, request_id=request_id)
                    header = event["header"]
                    if header.get("task_id") != task_id:
                        raise CloudError("PROTOCOL_MISMATCH", "云端任务标识不匹配，已停止接收音频。", uncertain=True)
                    request_id = _request_id(header.get("task_uuid")) or request_id or task_id
                    body = event.get("payload", {})
                    if body.get("usage"):
                        usage = _usage(body["usage"])
                    kind = header.get("event")
                    if kind == "task-started":
                        if started:
                            raise CloudError("PROTOCOL_MISMATCH", "云端重复启动任务，已停止接收。", uncertain=True)
                        started = True
                        if cancel_sent or (cancel_event is not None and cancel_event.is_set()):
                            cancel_sent = True
                            await ws.send(command("finish-task", {"input": {"directive": "cancel"}}))
                        else:
                            await ws.send(command("continue-task", {"input": {"text": text}}))
                            await ws.send(command("finish-task", {"input": {}}))
                    elif kind == "task-finished":
                        if cancel_sent:
                            raise CloudError("CANCELLED", "云端已确认取消；已发生用量以百炼账单为准。", request_id=request_id, usage=usage)
                        if not started or not pcm:
                            raise CloudError("EMPTY_AUDIO", "云端任务结束但没有返回音频。", request_id=request_id, usage=usage)
                        break
                    elif kind == "task-failed":
                        error = _provider_error(400, {"code": header.get("error_code"), "request_id": request_id}, submitted=started)
                        error.usage = usage
                        raise error
                    elif kind == "result-generated":
                        output = body.get("output", {})
                        sentence = output.get("sentence", {})
                        for word in sentence.get("words", []):
                            if isinstance(word, dict):
                                key = (sentence.get("index"), word.get("begin_index"), word.get("end_index"), word.get("begin_time"))
                                words[key] = word
            finally:
                for task in (receive, cancel_waiter):
                    if task is not None:
                        task.cancel()
                        with suppress(asyncio.CancelledError, WebSocketException):
                            await task
    except CloudError:
        raise
    except ProxyConfigurationError as exc:
        raise CloudError("INVALID_PROXY_CONFIGURATION", str(exc)) from None
    except ImportError:
        raise CloudError("MISSING_PROXY_COMPONENT", "流式代理运行组件缺失，请更新或重新安装完整应用。") from None
    except InvalidStatus as exc:
        raise _provider_error(exc.response.status_code) from exc
    except (WebSocketException, OSError, TimeoutError) as exc:
        raise CloudError("REQUEST_UNCERTAIN" if submitted else "CONNECT_FAILED", "云端流式连接中断；请检查任务记录后再重试。" if submitted else "无法建立百炼流式连接，请检查网络和连接设置。", uncertain=submitted, request_id=request_id, usage=usage) from exc
    if len(pcm) % 2:
        raise CloudError("INVALID_AUDIO", "流式音频采样不完整，未发布结果。", uncertain=True, request_id=request_id, usage=usage)
    # The protocol sends signed 16-bit, mono PCM. Wrap only after completion.
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(bytes(pcm))
    await _notify(on_progress, 1.0)
    return SynthesisResult(buffer.getvalue(), request_id, usage, word_timestamps=list(words.values()))


async def check_connection(config: CloudConfig) -> dict:
    """A read-only voice listing authenticates without a synthesis charge."""
    data = await _post(config, _ENROLLMENT_PATH, {"model": "voice-enrollment", "input": {"action": "list_voice", "page_index": 0, "page_size": 1}})
    return {"ok": True, "message": "连接与鉴权通过，未执行付费语音生成。", "request_id": _request_id(data.get("request_id")), "model_verified": config.model_id in _KNOWN_MODELS}


async def enroll_voice(
    config: CloudConfig,
    target_model: str,
    name: str,
    *,
    audio: bytes | None = None,
    audio_filename: str = "reference.wav",
    voice_prompt: str | None = None,
    preview_text: str | None = None,
    reference_text: str | None = None,
) -> dict:
    if target_model not in _KNOWN_MODELS:
        raise CloudError("UNVERIFIED_CAPABILITY", "自定义模型尚未验证声音克隆或设计能力。")
    if bool(audio) == bool(voice_prompt):
        raise CloudError("INVALID_VOICE_INPUT", "请提供参考录音或声音描述中的一种。")
    prefix = re.sub(r"[^A-Za-z0-9]", "", name)[:6] or "jdx"
    prefix += uuid.uuid4().hex[: 10 - len(prefix)]
    data: dict[str, Any] = {"action": "create_voice", "target_model": target_model, "prefix": prefix, "language_hints": ["zh"]}
    body: dict[str, Any] = {"model": "voice-enrollment", "input": data}
    if audio:
        if len(audio) > 10 * 1024 * 1024:
            raise CloudError("REFERENCE_TOO_LARGE", "参考录音超过 10 MB，请裁剪后重试。")
        mime = mimetypes.guess_type(audio_filename)[0]
        if mime not in {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/flac", "audio/x-flac", "audio/ogg", "audio/mp4"}:
            raise CloudError("UNSUPPORTED_REFERENCE", "请使用 WAV、MP3、FLAC、OGG 或 M4A 参考录音。")
        data["audio"] = f"data:{mime};base64," + base64.b64encode(audio).decode("ascii")
        data["max_prompt_audio_length"] = 30.0
        data["enable_preprocess"] = False
        data["enable_volume_normalization"] = "false"
        # This API extracts the voice from audio; ref_text is not an input field.
    else:
        if config.region != "cn-beijing":
            raise CloudError("UNSUPPORTED_REGION", "当前已验证的声音设计接口仅支持北京地域。")
        if not voice_prompt or len(voice_prompt) > 500:
            raise CloudError("INVALID_VOICE_PROMPT", "声音描述应为 1 到 500 个字符。")
        if not preview_text or not 15 <= len(preview_text) <= 200:
            raise CloudError("INVALID_PREVIEW_TEXT", "试听文本应为 15 到 200 个字符。")
        data["voice_prompt"] = voice_prompt
        data["preview_text"] = preview_text
        body["parameters"] = {"sample_rate": 24000, "response_format": "wav"}
    response = await _post(config, _ENROLLMENT_PATH, body)
    output = response.get("output", {})
    voice_id = output.get("voice_id")
    if not isinstance(voice_id, str) or not voice_id:
        raise CloudError("MISSING_VOICE", "云端未返回音色 ID，请检查任务记录后再重试。", uncertain=True, request_id=_request_id(response.get("request_id")))
    result = {"voice_id": voice_id, "status": output.get("status", "DEPLOYING"), "request_id": _request_id(response.get("request_id")), "usage": _usage(response.get("usage")), "target_model": target_model}
    preview = output.get("preview_audio", {})
    if isinstance(preview, dict) and preview.get("data"):
        try:
            result["preview_audio"] = base64.b64decode(preview["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CloudError("INVALID_AUDIO", "音色已创建，但预览音频数据损坏。", uncertain=True, request_id=result["request_id"]) from exc
    return result


async def query_voice(config: CloudConfig, voice_id: str) -> dict:
    data = await _post(config, _ENROLLMENT_PATH, {"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": voice_id}})
    output = data.get("output", {})
    return {"voice_id": output.get("voice_id", voice_id), "status": output.get("status", "DEPLOYING"), "target_model": output.get("target_model"), "request_id": _request_id(data.get("request_id")), "usage": _usage(data.get("usage"))}


async def delete_remote_voice(config: CloudConfig, voice_id: str) -> dict:
    return await _post(config, _ENROLLMENT_PATH, {"model": "voice-enrollment", "input": {"action": "delete_voice", "voice_id": voice_id}})
