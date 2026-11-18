"""Official catalog metadata, free sample cache and remembered connection workflows."""
import asyncio
import io
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from jdx_backend import cloud, voice_preview
from jdx_backend.app import create_app
from jdx_backend.schemas import Line, LineSettings
from jdx_backend.storage import Store

TOKEN = "voice-library-local-test-" * 3


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, TOKEN, browser_dev=True), headers={"Authorization": f"Bearer {TOKEN}"}) as value:
        yield value


def tone():
    stream = io.BytesIO()
    sf.write(stream, 0.2 * np.sin(np.arange(4800) * 2 * np.pi * 440 / 48000), 48000, format="WAV")
    return stream.getvalue()


def test_official_catalog_is_model_scoped_and_samples_are_fixed(client):
    voices = client.get("/api/v1/voices").json()
    counts = Counter(voice["target_model"] for voice in voices)
    assert counts == {"qwen-audio-3.1-tts-plus": 4, "qwen-audio-3.1-tts-flash": 139,
                      "qwen-audio-3.0-tts-plus": 2, "qwen-audio-3.0-tts-flash": 12}
    assert len({voice["id"] for voice in voices}) == len(voices)
    assert all(v["preview_available"] and v["preview_source"] == "official" and v["gender"] and v["languages"] for v in voices)
    assert all(v["preview_url"] is None and "official_sample_url" not in v for v in voices)
    assert all(urlsplit(v["official_sample_url"]).hostname in {"g-adoc.alcasset.com", "help-static-aliyun-doc.aliyuncs.com"}
               for v in cloud.SYSTEM_VOICES)
    english = next(v for v in voices if v["voice_id"] == "loongmary")
    assert not english["ssml"] and not english["instruction"] and not english["word_timestamps"]


def test_base_url_only_connection_and_legacy_url_compatibility(client):
    response = client.post("/api/v1/connections", json={"name": "制作室", "base_url": "https://workspace-a.ap-southeast-1.maas.aliyuncs.com/", "api_key": "test-only"})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["base_url"] == "https://workspace-a.ap-southeast-1.maas.aliyuncs.com"
    assert saved["workspace_id"] == "workspace-a" and saved["region"] == "ap-southeast-1"
    assert client.get("/api/v1/bootstrap").json()["preferences"]["default_connection_id"] == saved["id"]
    legacy = client.post("/api/v1/connections", json={"name": "旧连接", "workspace_id": "old"}).json()
    assert legacy["base_url"] == "https://old.cn-beijing.maas.aliyuncs.com"
    custom = client.post("/api/v1/connections", json={"name": "自定义服务", "base_url": "https://tts.vendor.example"}).json()
    assert custom["workspace_id"] == "" and custom["region"] == "custom"
    edited = client.patch(f"/api/v1/connections/{saved['id']}", json={"base_url": "https://tts.vendor.example"}).json()
    assert edited["workspace_id"] == "" and edited["region"] == "custom"
    invalid = client.post("/api/v1/connections", json={"base_url": "https://key:secret@example.com/"})
    assert invalid.status_code == 502
    assert "secret" not in invalid.text


def test_default_connection_is_remembered_and_does_not_replace_explicit_settings(client):
    first = client.post("/api/v1/connections", json={"workspace_id": "first"}).json()
    second = client.post("/api/v1/connections", json={"workspace_id": "second", "api_key": "test-only"}).json()
    manager = client.app.state.manager
    # Even missing credentials must not silently redirect work to another account.
    assert manager.resolve_settings(LineSettings().model_dump())["connection_id"] == first["id"]
    client.patch("/api/v1/preferences", json={"default_connection_id": second["id"]})
    assert manager.resolve_settings(LineSettings().model_dump())["connection_id"] == second["id"]
    reopened = Store(client.app.state.store.root)
    assert reopened.default_connection()["id"] == second["id"]
    reopened.close()
    explicit = LineSettings(connection_id=first["id"], model_id="qwen-audio-3.0-tts-plus", voice_id="longanlingxin").model_dump()
    assert manager.resolve_settings(explicit) == explicit
    assert LineSettings().connection_id is None
    client.delete(f"/api/v1/connections/{second['id']}")
    assert client.get("/api/v1/bootstrap").json()["preferences"]["default_connection_id"] == first["id"]


def test_fallback_generation_and_official_language_voice_do_not_query_custom_service(client, monkeypatch):
    connection = client.post("/api/v1/connections", json={"workspace_id": "only", "api_key": "test-only"}).json()
    project = client.post("/api/v1/projects", json={"name": "自动连接"}).json()
    line = Line(text="本地协议验证", settings=LineSettings(model_id="qwen-audio-3.1-tts-flash", voice_id="qwen-audio-3.1-tts-flash-zora_bg", language="other"))
    client.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    calls = []
    async def synthesize(config, text, settings, **kwargs):
        calls.append((config.id, settings["voice_id"]))
        return cloud.SynthesisResult(tone(), "local-test-only", {})
    async def forbidden(*args, **kwargs):
        raise AssertionError("Official voices must not call customization query")
    monkeypatch.setattr(cloud, "synthesize", synthesize)
    monkeypatch.setattr(cloud, "query_voice", forbidden)
    response = client.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 200, response.text
    for _ in range(100):
        jobs = client.get(f"/api/v1/projects/{project['id']}/tasks").json()
        if jobs[0]["status"] == "succeeded": break
        time.sleep(.02)
    assert jobs[0]["status"] == "succeeded", jobs
    assert calls == [(connection["id"], "qwen-audio-3.1-tts-flash-zora_bg")]


def test_official_preview_is_free_cached_signed_and_range_enabled(client, monkeypatch):
    calls = []
    def factory(url):
        calls.append(url)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=tone())), trust_env=False)
    monkeypatch.setattr(voice_preview, "http_client", factory)
    voice = client.get("/api/v1/voices").json()[0]
    route = f"/api/v1/voices/{voice['id']}/preview"
    assert client.post(route, headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.post(route)
    assert response.status_code == 200, response.text
    url = response.json()["preview_url"]
    assert client.get(url, headers={"Range": "bytes=0-19"}).status_code == 206
    assert client.get(url.replace("signature=", "signature=bad")).status_code == 403
    assert client.post(route).json()["preview_url"] == url
    assert len(calls) == 1
    assert client.get("/api/v1/voices").json()[0]["preview_url"]
    assert not client.app.state.secrets


def test_saved_copy_of_official_voice_keeps_official_sample_and_metadata(client, monkeypatch):
    monkeypatch.setattr(voice_preview, "http_client", lambda url: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=tone())), trust_env=False))
    original = next(v for v in client.get("/api/v1/voices").json() if v["voice_id"] == "yuxiaoyun_v3.1")
    client.post(f"/api/v1/voices/{original['id']}/preview")
    copied = client.post("/api/v1/voices", json={key: original[key] for key in (
        "id", "name", "kind", "connection_id", "target_model", "voice_id", "status", "notes", "authorized", "ssml",
    )}).json()
    assert copied["gender"] == original["gender"]
    assert copied["preview_source"] == "official" and copied["preview_url"]


def test_official_favorites_persist_by_stable_voice_record_id(client):
    voice = client.get("/api/v1/voices").json()[0]
    result = client.patch("/api/v1/preferences", json={"favorite_voice_ids": [voice["id"], voice["id"]]})
    assert result.status_code == 200
    assert result.json()["favorite_voice_ids"] == [voice["id"]]
    assert client.get("/api/v1/bootstrap").json()["preferences"]["favorite_voice_ids"] == [voice["id"]]
    assert client.patch("/api/v1/preferences", json={"favorite_voice_ids": ["missing"]}).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://evil.example/test.wav", "http://g-adoc.alcasset.com/test.wav", "https://user:pass@g-adoc.alcasset.com/test.wav", "https://g-adoc.alcasset.com/test.wav?secret=test"])
async def test_preview_rejects_non_catalog_remote_targets(tmp_path, url):
    with pytest.raises(ValueError, match="不受支持"):
        await voice_preview.cache_official_sample(url, tmp_path / "preview.wav")


@pytest.mark.asyncio
async def test_preview_refuses_redirect_or_invalid_audio_without_cache(tmp_path, monkeypatch):
    for response in (httpx.Response(302, headers={"location": "http://127.0.0.1/private"}), httpx.Response(200, content=b"<html>not audio</html>")):
        monkeypatch.setattr(voice_preview, "http_client", lambda url: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response), trust_env=False))
        with pytest.raises(ValueError):
            await voice_preview.cache_official_sample("https://g-adoc.alcasset.com/sample.wav", tmp_path / "preview.wav")
        assert not (tmp_path / "preview.wav").exists()


def test_flash_regional_availability_is_enforced_without_calling_cloud():
    with pytest.raises(cloud.CloudError, match="服务地址开放"):
        cloud.synthesis_input(cloud.CloudConfig(region="ap-southeast-1"), "测试", {"model_id": "qwen-audio-3.1-tts-flash", "voice_id": "yuxiaoyun_v3.1"})


def test_custom_endpoint_does_not_reject_personal_voice_for_stale_region_binding(client, monkeypatch):
    connection = client.post("/api/v1/connections", json={"base_url": "https://workspace.cn-beijing.maas.aliyuncs.com", "api_key": "test-only"}).json()
    voice_id = connection["model_id"] + "-clone-vendor"
    saved = client.post("/api/v1/voices", json={"name": "专属音色", "kind": "clone", "connection_id": connection["id"],
                                                   "target_model": connection["model_id"], "voice_id": voice_id,
                                                   "authorized": True, "status": "OK"})
    assert saved.status_code == 200
    custom = client.patch(f"/api/v1/connections/{connection['id']}", json={"base_url": "https://tts.vendor.example"}).json()
    assert custom["region"] == "custom" and custom["workspace_id"] == ""
    project = client.post("/api/v1/projects", json={"name": "迁移服务端点"}).json()
    line = Line(text="测试声音绑定", settings=LineSettings(connection_id=connection["id"], voice_id=voice_id))
    client.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    async def query(config, value):
        return {"target_model": config.model_id, "status": "OK"}
    async def synthesize(config, text, settings, **kwargs):
        assert config.region == "custom" and config.workspace_id == ""
        return cloud.SynthesisResult(tone(), "local-custom-host-test", {})
    monkeypatch.setattr(cloud, "query_voice", query)
    monkeypatch.setattr(cloud, "synthesize", synthesize)
    response = client.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 200, response.text
    for _ in range(100):
        job = client.get(f"/api/v1/projects/{project['id']}/tasks").json()[0]
        if job["status"] in {"succeeded", "failed"}: break
        time.sleep(.02)
    assert job["status"] == "succeeded", job
