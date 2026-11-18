import asyncio
import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import select

from jdx_backend.app import create_app
from jdx_backend import cloud
from jdx_backend.schemas import Line, new_id
from jdx_backend.storage import AssetRow, JobRow, StateRow

TOKEN = "test-token-" * 5


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, TOKEN, browser_dev=True)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as value:
        yield value


def project(client, name="配音项目"):
    response = client.post("/api/v1/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def tone(duration=0.2, frequency=440):
    output = io.BytesIO()
    t = np.arange(round(48000*duration)) / 48000
    sf.write(output, 0.2*np.sin(2*np.pi*frequency*t), 48000, format="WAV", subtype="PCM_24")
    return output.getvalue()


def poll_export(client, project_id, job_id):
    for _ in range(200):
        result = client.get(f"/api/v1/projects/{project_id}/exports/{job_id}").json()
        if result["status"] in {"succeeded", "failed"}:
            assert result["status"] == "succeeded", result
            return result
        time.sleep(0.05)
    raise AssertionError("export timed out")


def test_authentication_and_no_secret_persistence(client):
    assert client.get("/api/v1/bootstrap", headers={"Authorization": "Bearer invalid"}).status_code == 401
    secret = "sk-test-do-not-persist"
    response = client.post("/api/v1/connections", json={"name": "北京", "workspace_id": "test", "api_key": secret})
    assert response.status_code == 200
    assert response.json()["has_credential"] is True
    assert secret not in response.text
    assert secret not in client.get("/api/v1/bootstrap").text
    app_bytes = (client.app.state.store.root / "studio.sqlite").read_bytes()
    assert secret.encode() not in app_bytes


def test_edit_undo_conflict_and_versions_owned_by_server(client):
    value = project(client)
    line = Line(text="第一句").model_dump()
    changed = client.patch(f"/api/v1/projects/{value['id']}", json={"revision": value["revision"], "lines": [line]}).json()
    assert changed["lines"][0]["revision"] == 1
    assert changed["can_undo"]
    stale = client.patch(f"/api/v1/projects/{value['id']}", json={"revision": value["revision"], "name": "冲突"})
    assert stale.status_code == 409
    undone = client.post(f"/api/v1/projects/{value['id']}/undo").json()
    assert not undone["lines"] and undone["can_redo"]
    restored = client.post(f"/api/v1/projects/{value['id']}/redo").json()
    assert restored["lines"][0]["text"] == "第一句"


def test_subtitle_import_and_large_project(client):
    value = project(client)
    subtitle = "1\n00:00:01,000 --> 00:00:02,500\n你好，世界。\n\n2\n00:00:03,000 --> 00:00:04,500\n第二句话。\n"
    result = client.post(f"/api/v1/projects/{value['id']}/import", files={"file": ("字幕.srt", subtitle.encode(), "text/plain")})
    assert result.status_code == 200, result.text
    lines = result.json()["lines"]
    assert lines[0]["subtitle_start_ms"] == 1000 and lines[1]["settings"]["target_duration_ms"] == 1500
    many = [Line(text=f"这是第{index}句台词。").model_dump() for index in range(500)]
    result = client.patch(f"/api/v1/projects/{value['id']}", json={"revision": result.json()["revision"], "lines": many})
    assert result.status_code == 200 and len(result.json()["lines"]) == 500
    assert client.get(f"/api/v1/projects/{value['id']}").json()["lines"][499]["position"] == 499


def test_audio_import_render_range_backup_restore(client):
    value = project(client, "中文音频")
    result = client.post(f"/api/v1/projects/{value['id']}/import", files={"file": ("参考.wav", tone(), "audio/wav")})
    assert result.status_code == 200, result.text
    value = result.json()
    assert len(value["clips"]) == 1
    media = value["assets"][0]["media_url"]
    assert client.get(media, headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get(media.replace("signature=", "signature=invalid")).status_code == 403
    job = client.post(f"/api/v1/projects/{value['id']}/export", json={"format": "wav", "normalize": False}).json()
    exported = poll_export(client, value["id"], job["job_id"])
    audio = client.get(exported["download_url"]).content
    info = sf.info(io.BytesIO(audio))
    assert info.samplerate == 48000 and info.subtype == "PCM_24"
    assert abs(info.duration - 0.2) < 0.02
    backup = client.post(f"/api/v1/projects/{value['id']}/backup").json()
    archive = client.get(poll_export(client, value["id"], backup["job_id"])["download_url"]).content
    restored = client.post("/api/v1/projects/restore", files={"file": ("备份.jdx", archive, "application/zip")})
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] != value["id"]
    assert len(restored.json()["clips"]) == 1
    assert all(not asset["missing"] for asset in restored.json()["assets"])


def test_zip_path_traversal_rejected(client, tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")
    result = client.post("/api/v1/projects/restore", files={"file": ("evil.jdx", archive.getvalue())})
    assert result.status_code == 400
    assert not (tmp_path / "escape.txt").exists()


def test_real_queue_protocol_fake_cloud_dedup_and_stale_result(client, monkeypatch):
    started = []

    async def synthesize(config, text, settings, **kwargs):
        started.append(text)
        await asyncio.sleep(0.15)
        return cloud.SynthesisResult(tone(), "request-test", {"characters": len(text)})

    monkeypatch.setattr(cloud, "synthesize", synthesize)
    connection = client.post("/api/v1/connections", json={"name": "测试", "workspace_id": "test", "api_key": "test-secret"}).json()
    value = project(client)
    line = Line(text="不会覆盖修改后的台词")
    line.settings.connection_id = connection["id"]
    value = client.patch(f"/api/v1/projects/{value['id']}", json={"revision": value["revision"], "lines": [line.model_dump()]}).json()
    route = f"/api/v1/projects/{value['id']}/tasks"
    first = client.post(route, json={"line_ids": [line.id]}).json()["jobs"][0]
    second = client.post(route, json={"line_ids": [line.id]}).json()["jobs"][0]
    assert first["id"] == second["id"]
    value["lines"][0]["text"] = "已经修改"
    changed = client.patch(f"/api/v1/projects/{value['id']}", json={"revision": value["revision"], "lines": value["lines"]})
    assert changed.status_code == 200
    for _ in range(100):
        jobs = client.get(route).json()
        if jobs[0]["status"] == "succeeded":
            break
        time.sleep(0.03)
    assert jobs[0]["status"] == "succeeded", jobs
    current = client.get(f"/api/v1/projects/{value['id']}").json()
    assert len(current["lines"][0]["versions"]) == 1
    assert current["lines"][0]["selected_version_id"] is None
    assert started == ["不会覆盖修改后的台词"]


def test_recovery_and_migration_backup(tmp_path):
    app = create_app(tmp_path, TOKEN)
    store = app.state.store
    pid = store.create_project("恢复")
    with store.project_session(pid) as session:
        session.add(JobRow(id=new_id(), line_id="test", status="running", fingerprint="f",
                           data={"status": "running"}, snapshot={}))
    assert store.recover() == 1
    with store.project_session(pid) as session:
        assert session.scalar(select(JobRow)).status == "interrupted"
    store.close()
    path = tmp_path / "projects" / pid / "project.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
    app2 = create_app(tmp_path, TOKEN)
    app2.state.store.engine(pid)
    assert path.with_suffix(".pre-migration.sqlite").is_file()
    app2.state.store.close()


def test_director_is_editable_rule_plan(client):
    value = project(client)
    response = client.post(f"/api/v1/projects/{value['id']}/director",
                           json={"text": "欢迎回来。现在开始！", "instruction": "前面温柔，最后坚定，停顿0.5秒"})
    assert response.status_code == 200
    result = response.json()
    assert len(result["segments"]) == 2 and result["segments"][0]["pause_after_ms"] == 500
