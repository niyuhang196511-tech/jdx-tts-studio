import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from jdx_backend import cloud


def config(client=None, **changes):
    return cloud.CloudConfig(id="connection", workspace_id="workspace123", api_key="secret-test-api-key", http_client=client, **changes)


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 480)
    return output.getvalue()


@pytest.mark.asyncio
async def test_http_synthesis_downloads_without_forwarding_key():
    requests = []
    audio = wav_bytes()

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer secret-test-api-key"
            body = json.loads(request.content)
            assert body["model"] == "qwen-audio-3.1-tts-plus"
            assert body["input"]["rate"] == 1.2
            assert body["input"]["sample_rate"] == 48000
            assert "开心" in body["input"]["instruction"]
            assert "pause_after_ms" not in body["input"]
            return httpx.Response(200, json={"request_id": "cloud-request", "output": {"audio": {"url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.wav?signature=private"}}, "usage": {"characters": 5}})
        assert request.url.scheme == "https"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=audio)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await cloud.synthesize(config(client), "你好世界。", {"rate": 1.2, "emotion": "开心", "pause_after_ms": 200})
    assert result.audio == audio
    assert result.request_id == "cloud-request"
    assert result.usage == {"characters": 5}
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_http_blocks_untrusted_audio_url():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"request_id": "r", "output": {"audio": {"url": "http://127.0.0.1/private"}}, "usage": {"characters": 3}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(client), "测试。", {})
    assert error.value.code == "UNSAFE_AUDIO_URL"
    assert error.value.request_id == "r"
    assert error.value.usage == {"characters": 3}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_audio_redirect_is_validated():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"output": {"audio": {"url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.wav"}}})
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(client), "测试。", {})
    assert error.value.code == "UNSAFE_AUDIO_URL"
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable,uncertain", [(401, "AUTH_FAILED", False, False), (429, "RATE_LIMIT", True, False), (503, "PROVIDER_UNAVAILABLE", False, True)])
async def test_provider_failures_are_not_automatically_retried(status, code, retryable, uncertain):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"request_id": "failed-request", "message": "secret-test-api-key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(client), "测试。", {})
    assert count == 1
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert error.value.uncertain is uncertain
    assert "secret-test-api-key" not in str(error.value)


@pytest.mark.asyncio
async def test_gateway_auth_failure_with_html_has_clear_safe_message():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="<html>gateway refused secret-test-api-key</html>"))) as client:
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(client), "测试。", {})
    assert error.value.code == "AUTH_FAILED"
    assert not error.value.uncertain
    assert "secret-test-api-key" not in str(error.value)


@pytest.mark.asyncio
async def test_read_timeout_is_uncertain_and_not_replayed():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("provider took too long", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(client), "测试。", {})
    assert error.value.uncertain
    assert not error.value.retryable
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_cancellation_does_not_claim_cloud_confirmed():
    entered = asyncio.Event()
    cancellation = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(30)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(cloud.synthesize(config(client), "测试。", {}, cancel_event=cancellation))
        await entered.wait()
        cancellation.set()
        with pytest.raises(cloud.CloudError) as error:
            await asyncio.wait_for(task, 2)
    assert error.value.code == "CANCEL_UNCONFIRMED"
    assert error.value.uncertain


def test_ssml_escapes_text_and_does_not_add_postprocessing_pauses():
    model, payload = cloud.synthesis_input(config(), '你好 <break time="5s"/> & 世界', {"ssml": True, "pause_before_ms": 500, "pause_after_ms": 800})
    assert model == "qwen-audio-3.1-tts-plus"
    assert payload["enable_ssml"]
    assert payload["text"] == '<speak>你好 &lt;break time="5s"/&gt; &amp; 世界</speak>'
    assert "pause_before_ms" not in payload
    assert "<break " not in payload["text"]


@pytest.mark.parametrize("settings,code", [({"rate": 3}, "INVALID_PARAMETER"), ({"volume": 50.5}, "INVALID_PARAMETER"), ({"seed": -1}, "INVALID_PARAMETER"), ({"voice_id": "longanlufeng"}, "VOICE_MODEL_MISMATCH"), ({"voice_id": "qwen-audio-3.1-tts-plus-vd-myvoice-123", "ssml": True}, "UNSUPPORTED_SSML")])
def test_unsupported_settings_fail_before_network(settings, code):
    with pytest.raises(cloud.CloudError) as error:
        cloud.synthesis_input(config(), "测试。", settings)
    assert error.value.code == code


def test_custom_model_capability_is_not_assumed():
    custom = config(model_id="future-qwen-model", voice_id="future-voice")
    model, payload = cloud.synthesis_input(custom, "测试。", {})
    assert model == "future-qwen-model"
    assert "rate" not in payload
    with pytest.raises(cloud.CloudError) as error:
        cloud.synthesis_input(custom, "测试。", {"instruction": "温柔"})
    assert error.value.code == "UNVERIFIED_CAPABILITY"


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1", "https://localhost", "https://example.com/api", "https://user:password@example.com", "https://example.com?key=secret"])
def test_endpoint_validation_and_secret_repr(url):
    connection = config(base_url=url)
    assert "secret-test-api-key" not in repr(connection)
    with pytest.raises(cloud.CloudError):
        cloud.endpoint(connection)


@pytest.mark.asyncio
async def test_clone_uses_data_uri_and_never_sends_unofficial_reference_text():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "voice-enrollment"
        assert body["input"]["action"] == "create_voice"
        assert body["input"]["target_model"] == "qwen-audio-3.1-tts-plus"
        assert body["input"]["audio"].startswith("data:audio/")
        assert "ref_text" not in body["input"]
        assert "reference_text" not in body["input"]
        return httpx.Response(200, json={"output": {"voice_id": "qwen-audio-3.1-tts-plus-jdx-123"}, "usage": {"count": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        voice = await cloud.enroll_voice(config(client), "qwen-audio-3.1-tts-plus", "中文角色", audio=wav_bytes(), reference_text="参考文本")
    assert voice["status"] == "DEPLOYING"
    assert voice["usage"] == {"count": 1}


@pytest.mark.asyncio
async def test_design_decodes_preview_and_restricts_region():
    audio = wav_bytes()

    def handler(request):
        body = json.loads(request.content)
        assert body["input"]["voice_prompt"] == "低沉稳重的中文男声"
        assert body["parameters"]["response_format"] == "wav"
        return httpx.Response(200, json={"output": {"voice_id": "qwen-audio-3.1-tts-plus-vd-jdx-123", "preview_audio": {"data": base64.b64encode(audio).decode()}}, "request_id": "design-request"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        voice = await cloud.enroll_voice(config(client), "qwen-audio-3.1-tts-plus", "旁白", voice_prompt="低沉稳重的中文男声", preview_text="各位听众朋友们大家好，欢迎收听本期节目。")
    assert voice["preview_audio"] == audio
    with pytest.raises(cloud.CloudError) as error:
        await cloud.enroll_voice(config(region="ap-southeast-1"), "qwen-audio-3.1-tts-plus", "旁白", voice_prompt="低沉稳重的中文男声", preview_text="各位听众朋友们大家好，欢迎收听本期节目。")
    assert error.value.code == "UNSUPPORTED_REGION"


@pytest.mark.asyncio
async def test_connection_check_only_lists_voices():
    def handler(request):
        body = json.loads(request.content)
        assert body["input"]["action"] == "list_voice"
        assert "SpeechSynthesizer" not in str(request.url)
        return httpx.Response(200, json={"output": {"voice_list": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await cloud.check_connection(config(client)))["ok"]


@pytest.mark.asyncio
async def test_websocket_protocol_audio_and_cumulative_usage(monkeypatch):
    actions = []
    received_chunks = []
    pcm = b"\x10\x00" * 480

    async def handler(ws):
        run = json.loads(await ws.recv())
        actions.append(run["header"]["action"])
        task_id = run["header"]["task_id"]
        assert run["payload"]["parameters"]["format"] == "pcm"
        assert run["payload"]["input"] == {}
        assert "text" not in run["payload"]["parameters"]
        await ws.send(json.dumps({"header": {"event": "task-started", "task_id": task_id, "task_uuid": "request-id"}, "payload": {}}))
        continuation = json.loads(await ws.recv())
        actions.append(continuation["header"]["action"])
        assert continuation["payload"]["input"]["text"] == "测试。"
        finish = json.loads(await ws.recv())
        actions.append(finish["header"]["action"])
        assert finish["payload"]["input"] == {}
        sentence = {"index": 0, "words": [{"text": "测", "begin_index": 0, "end_index": 1, "begin_time": 0, "end_time": 100}]}
        for _ in range(2):
            await ws.send(json.dumps({"header": {"event": "result-generated", "task_id": task_id}, "payload": {"output": {"sentence": sentence}, "usage": {"characters": 1}}}))
        await ws.send(pcm)
        await ws.send(json.dumps({"header": {"event": "task-finished", "task_id": task_id}, "payload": {"usage": {"characters": 3}}}))

    async def chunk(data):
        received_chunks.append(data)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(cloud, "websocket_connect", lambda *args, **kwargs: connect(f"ws://127.0.0.1:{port}"))
        result = await cloud.synthesize(config(), "测试。", {}, transport="websocket", on_chunk=chunk)
    assert actions == ["run-task", "continue-task", "finish-task"]
    assert result.usage == {"characters": 3}
    assert result.request_id == "request-id"
    assert len(result.word_timestamps) == 1
    assert received_chunks == [pcm]
    with wave.open(io.BytesIO(result.audio), "rb") as wav:
        assert wav.getframerate() == 48000
        assert wav.readframes(480) == pcm


@pytest.mark.asyncio
async def test_websocket_cancel_requires_finished_ack(monkeypatch):
    cancellation = asyncio.Event()
    cancel_seen = asyncio.Event()
    may_ack = asyncio.Event()

    async def handler(ws):
        run = json.loads(await ws.recv())
        task_id = run["header"]["task_id"]
        await ws.send(json.dumps({"header": {"event": "task-started", "task_id": task_id}, "payload": {}}))
        await ws.recv()
        await ws.recv()
        cancellation.set()
        cancel = json.loads(await ws.recv())
        assert cancel["header"]["action"] == "finish-task"
        assert cancel["payload"]["input"] == {"directive": "cancel"}
        cancel_seen.set()
        await may_ack.wait()
        await ws.send(json.dumps({"header": {"event": "task-finished", "task_id": task_id}, "payload": {"usage": {"characters": 1}}}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(cloud, "websocket_connect", lambda *args, **kwargs: connect(f"ws://127.0.0.1:{port}"))
        task = asyncio.create_task(cloud.synthesize(config(), "测试。", {}, transport="websocket", cancel_event=cancellation))
        await asyncio.wait_for(cancel_seen.wait(), 2)
        assert not task.done()
        may_ack.set()
        with pytest.raises(cloud.CloudError) as error:
            await task
    assert error.value.code == "CANCELLED"
    assert not error.value.uncertain
    assert error.value.usage == {"characters": 1}


@pytest.mark.asyncio
async def test_websocket_disconnect_is_uncertain(monkeypatch):
    async def handler(ws):
        run = json.loads(await ws.recv())
        task_id = run["header"]["task_id"]
        await ws.send(json.dumps({"header": {"event": "task-started", "task_id": task_id}, "payload": {}}))
        await ws.recv()
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(cloud, "websocket_connect", lambda *args, **kwargs: connect(f"ws://127.0.0.1:{port}"))
        with pytest.raises(cloud.CloudError) as error:
            await cloud.synthesize(config(), "测试。", {}, transport="websocket")
    assert error.value.code == "REQUEST_UNCERTAIN"
    assert error.value.uncertain
