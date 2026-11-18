"""Real local-media and archive validation; cloud work is an explicit test double."""

import io
import json
import sqlite3
import zipfile

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from jdx_backend import cloud
from jdx_backend.app import create_app
from jdx_backend.schemas import Line, new_id, now
from jdx_backend.storage import JobRow

TOKEN = "integrity-session-token-" * 3


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, TOKEN, browser_dev=True)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as value:
        yield value


def audio(seconds=0.2, rate=48000):
    output = io.BytesIO()
    samples = 0.2 * np.sin(2 * np.pi * 220 * np.arange(round(seconds * rate)) / rate)
    sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
    return output.getvalue()


def create_project(client):
    return client.post("/api/v1/projects", json={"name": "完整性验证"}).json()


def create_connection(client):
    response = client.post("/api/v1/connections", json={"name": "测试连接", "workspace_id": "test-workspace", "api_key": "test-only"})
    assert response.status_code == 200
    return response.json()


def archive_for(client, project_id, mutate=None):
    store = client.app.state.store
    destination = store.root / f"{new_id()}.jdx"
    client.app.state.manager._backup(project_id, destination)
    if mutate:
        database = store.root / f"{new_id()}.sqlite"
        with zipfile.ZipFile(destination) as archive:
            entries = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
        database.write_bytes(entries["project.sqlite"])
        connection = sqlite3.connect(database)
        try:
            mutate(connection)
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        entries["project.sqlite"] = database.read_bytes()
        with zipfile.ZipFile(destination, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        database.unlink()
    return destination.read_bytes()


def test_relink_only_missing_asset_and_preserves_timeline(client):
    project = create_project(client)
    prefix = f"/api/v1/projects/{project['id']}"
    imported = client.post(prefix + "/import", files={"file": ("原录音.wav", audio())}).json()
    asset = imported["assets"][0]
    route = prefix + f"/assets/{asset['id']}/relink"
    original, _ = client.app.state.store.asset_path(project["id"], asset["id"])
    source_bytes = original.read_bytes()
    refused = client.post(route, files={"file": ("新录音.wav", audio())})
    assert refused.status_code == 409
    assert original.read_bytes() == source_bytes
    original.unlink()
    wrong = client.post(route, files={"file": ("长度不符.wav", audio(0.8))})
    assert wrong.status_code == 400 and not original.exists()
    restored = client.post(route, files={"file": ("原录音.wav", audio())})
    assert restored.status_code == 200, restored.text
    assert restored.json()["assets"][0]["id"] == asset["id"]
    assert restored.json()["assets"][0]["missing"] is False
    assert restored.json()["clips"] == imported["clips"]


@pytest.mark.parametrize("seconds", [1, 61])
def test_clone_rejects_invalid_duration_before_remote_call(client, monkeypatch, seconds):
    calls = []

    async def enrolled(*args, **kwargs):
        calls.append(kwargs)
        return {"voice_id": "unreachable"}

    monkeypatch.setattr(cloud, "enroll_voice", enrolled)
    connection = create_connection(client)
    response = client.post("/api/v1/voices/clone", data={"connection_id": connection["id"], "target_model": connection["model_id"], "name": "测试", "authorized": "true"},
                           files={"file": ("reference.wav", audio(seconds))})
    assert response.status_code == 400
    assert "5 至 60 秒" in response.text
    assert calls == []


def test_clone_normalizes_and_requires_authorization(client, monkeypatch):
    captured = []

    async def enrolled(config, target_model, name, **kwargs):
        captured.append(kwargs)
        return {"voice_id": target_model + "-clone-test", "status": "DEPLOYING"}

    monkeypatch.setattr(cloud, "enroll_voice", enrolled)
    connection = create_connection(client)
    data = {"connection_id": connection["id"], "target_model": connection["model_id"], "name": "测试声音", "authorized": "false"}
    response = client.post("/api/v1/voices/clone", data=data, files={"file": ("reference.wav", audio(5))})
    assert response.status_code == 400 and not captured
    data["authorized"] = "true"
    data["reference_text"] = "参考语句仅作为本地备注保存"
    response = client.post("/api/v1/voices/clone", data=data, files={"file": ("reference.wav", audio(5))})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DEPLOYING"
    assert response.json()["notes"] == "参考文本：参考语句仅作为本地备注保存"
    assert response.json()["workspace_id"] == connection["workspace_id"]
    assert response.json()["preview_source"] == "reference"
    assert client.get(response.json()["preview_url"]).status_code == 200
    info = sf.info(io.BytesIO(captured[0]["audio"]))
    assert info.samplerate == 24000 and info.channels == 1 and info.subtype == "PCM_16"


def test_clone_rejects_playlist_input(client, monkeypatch):
    connection = create_connection(client)
    data = {"connection_id": connection["id"], "target_model": connection["model_id"], "name": "无效", "authorized": "true"}
    response = client.post("/api/v1/voices/clone", data=data, files={"file": ("reference.wav", b"#EXTM3U\nhttp://127.0.0.1/internal.wav\n")})
    assert response.status_code == 400


def test_generation_rejects_voice_from_other_connection(client, monkeypatch):
    connection, another = create_connection(client), create_connection(client)
    voice_id = connection["model_id"] + "-clone-private"
    saved = client.post("/api/v1/voices", json={"name": "专属", "kind": "clone", "connection_id": connection["id"], "target_model": connection["model_id"], "voice_id": voice_id, "authorized": True, "status": "OK"})
    assert saved.status_code == 200
    project = create_project(client)
    line = Line(text="不能跨账户使用的声音")
    line.settings.connection_id, line.settings.voice_id = another["id"], voice_id
    client.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    response = client.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 400
    assert "其他百炼连接或模型" in response.text


def test_generation_checks_remote_voice_deployment_state(client, monkeypatch):
    connection = create_connection(client)
    project = create_project(client)
    line = Line(text="等待云端审核")
    line.settings.connection_id = connection["id"]
    line.settings.voice_id = connection["model_id"] + "-clone-test"
    client.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    queried = []

    async def query(config, voice_id):
        queried.append((config.id, voice_id))
        return {"target_model": config.model_id, "status": "DEPLOYING"}

    monkeypatch.setattr(cloud, "query_voice", query)
    response = client.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 400 and len(queried) == 1
    assert "当前不可用" in response.text


@pytest.mark.parametrize("mutation", [
    lambda db: db.execute("CREATE TRIGGER invalid AFTER UPDATE ON project_state BEGIN DELETE FROM assets; END"),
    lambda db: db.execute("CREATE TABLE unexpected (key TEXT)"),
    lambda db: db.execute("UPDATE project_state SET data=json_set(data,'$.lines',json('[{\"id\":\"bad\",\"text\":\"bad\"}]'))"),
])
def test_restore_rejects_invalid_schema_and_state(client, mutation):
    project = create_project(client)
    archive = archive_for(client, project["id"], mutation)
    response = client.post("/api/v1/projects/restore", files={"file": ("bad.jdx", archive)})
    assert response.status_code == 400
    assert len(client.get("/api/v1/projects").json()) == 1


def test_restore_does_not_interrupt_other_projects(client):
    project = create_project(client)
    store = client.app.state.store
    line = Line(text="正在生成")
    identifier = new_id()
    job = {"id": identifier, "line_id": line.id, "status": "running", "progress": 0.2, "created_at": now()}
    with store.project_session(project["id"]) as session:
        session.add(JobRow(id=identifier, line_id=line.id, status="running", fingerprint="test", data=job, snapshot=line.model_dump()))
    archive = archive_for(client, project["id"])
    response = client.post("/api/v1/projects/restore", files={"file": ("项目.jdx", archive)})
    assert response.status_code == 200, response.text
    with store.project_session(project["id"]) as session:
        assert session.get(JobRow, identifier).status == "running"
    with store.project_session(response.json()["id"]) as session:
        assert session.get(JobRow, identifier).status == "interrupted"


def test_restore_rejects_embedded_credentials(client):
    project = create_project(client)
    line, identifier = Line(text="测试"), new_id()
    with client.app.state.store.project_session(project["id"]) as session:
        session.add(JobRow(id=identifier, line_id=line.id, status="succeeded", fingerprint="test",
                           data={"id": identifier, "line_id": line.id, "status": "succeeded", "progress": 1, "created_at": now()},
                           snapshot=dict(line.model_dump(), connection={"api_key": "should-not-import"})))
    archive = archive_for(client, project["id"])
    response = client.post("/api/v1/projects/restore", files={"file": ("项目.jdx", archive)})
    assert response.status_code == 400


def test_design_preview_is_local_signed_normalized_and_revoked_on_removal(client, monkeypatch):
    connection = create_connection(client)

    async def enrolled(config, target_model, name, **kwargs):
        return {"voice_id": target_model + "-vd-test", "status": "OK", "preview_audio": audio(0.2, 24000)}

    monkeypatch.setattr(cloud, "enroll_voice", enrolled)
    response = client.post("/api/v1/voices/design", json={"connection_id": connection["id"], "target_model": connection["model_id"],
                          "name": "设计音色", "voice_prompt": "温暖的成年女声", "preview_text": "欢迎来到我们的故事，一起听见每一个精彩瞬间。"})
    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["preview_url"] and TOKEN not in profile["preview_url"]
    assert profile["preview_source"] == "design"
    preview = client.get(profile["preview_url"], headers={"Authorization": ""})
    assert preview.status_code == 200
    info = sf.info(io.BytesIO(preview.content))
    assert info.samplerate == 48000 and info.subtype == "PCM_24"
    ranged = client.get(profile["preview_url"], headers={"Range": "bytes=0-15"})
    assert ranged.status_code == 206 and len(ranged.content) == 16
    assert client.get(profile["preview_url"].replace("signature=", "signature=wrong")).status_code == 403
    listed = next(voice for voice in client.get("/api/v1/bootstrap").json()["voices"] if voice["id"] == profile["id"])
    assert listed["preview_url"]
    client.delete(f"/api/v1/voices/{profile['id']}")
    assert client.get(profile["preview_url"]).status_code == 404


def test_invalid_design_preview_preserves_created_voice(client, monkeypatch):
    connection = create_connection(client)

    async def enrolled(config, target_model, name, **kwargs):
        return {"voice_id": target_model + "-vd-broken-preview", "status": "OK", "preview_audio": b"not audio"}

    monkeypatch.setattr(cloud, "enroll_voice", enrolled)
    response = client.post("/api/v1/voices/design", json={"connection_id": connection["id"], "target_model": connection["model_id"],
                          "name": "音色仍应保留", "voice_prompt": "温暖的成年男声", "preview_text": "欢迎来到我们的故事，一起听见每一个精彩瞬间。"})
    assert response.status_code == 200
    assert response.json()["preview_url"] is None
    assert "预览音频不可用" in response.json()["notes"]
    assert any(voice["id"] == response.json()["id"] for voice in client.get("/api/v1/voices").json())


def test_changed_workspace_does_not_rebind_existing_voice(client, monkeypatch):
    connection = create_connection(client)
    voice_id = connection["model_id"] + "-clone-workspace"
    profile = client.post("/api/v1/voices", json={"name": "原业务空间声音", "kind": "clone", "connection_id": connection["id"],
                         "target_model": connection["model_id"], "voice_id": voice_id, "authorized": True, "status": "OK"}).json()
    assert profile["workspace_id"] == "test-workspace"
    assert client.patch(f"/api/v1/connections/{connection['id']}", json={"workspace_id": "another-workspace"}).status_code == 200
    profile["favorite"] = True
    updated = client.post("/api/v1/voices", json=profile)
    assert updated.status_code == 200 and updated.json()["workspace_id"] == "test-workspace"
    project = create_project(client)
    line = Line(text="不能隐式切换业务空间")
    line.settings.connection_id, line.settings.voice_id = connection["id"], voice_id
    client.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    response = client.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 400
    assert "业务空间或地域已变化" in response.text


def test_preference_patches_merge_instead_of_replacing(client):
    assert client.patch("/api/v1/preferences", json={"theme": "light"}).status_code == 200
    result = client.patch("/api/v1/preferences", json={"first_run_complete": True})
    assert result.status_code == 200
    assert result.json() == {"theme": "light", "first_run_complete": True}
    assert client.get("/api/v1/bootstrap").json()["preferences"] == result.json()
