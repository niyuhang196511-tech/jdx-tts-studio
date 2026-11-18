"""Project recycling preserves user assets and serializes with active work."""

import asyncio
import hashlib
import io
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from jdx_backend.app import create_app
from jdx_backend.schemas import Line, new_id
from jdx_backend.storage import ExportRow, JobRow, ProjectTrash, Store

TOKEN = "project-lifecycle-session-" * 3


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, TOKEN, browser_dev=True)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as value:
        yield value


def create_project(client, name="回收站验证"):
    response = client.post("/api/v1/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def trash(client, project):
    return client.request("DELETE", f"/api/v1/projects/{project['id']}", json={"revision": project["revision"]})


def audio():
    output = io.BytesIO()
    sf.write(output, np.zeros(4800), 48000, format="WAV")
    return output.getvalue()


def test_delete_preserves_assets_revokes_access_and_restore_keeps_state(client):
    project = create_project(client, "  中文项目  ")
    other = create_project(client, "另一个项目")
    prefix = f"/api/v1/projects/{project['id']}"
    project = client.post(prefix + "/import", files={"file": ("原录音.wav", audio())}).json()
    asset = project["assets"][0]
    path, _ = client.app.state.store.asset_path(project["id"], asset["id"])
    digest = hashlib.sha256(path.read_bytes()).digest()
    assert client.get(asset["media_url"]).status_code == 200
    deleted = trash(client, project)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["name"] == "中文项目" and deleted.json()["deleted_at"]
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert (path.parent.parent / "project.sqlite").is_file()
    assert [item["id"] for item in client.get("/api/v1/projects").json()] == [other["id"]]
    assert [item["id"] for item in client.get("/api/v1/bootstrap").json()["projects"]] == [other["id"]]
    assert client.get(prefix).status_code == 410
    assert client.get(asset["media_url"]).status_code == 410
    assert client.patch(prefix, json={"revision": project["revision"], "name": "不应改写"}).status_code == 410
    assert client.post(prefix + "/tasks", json={"line_ids": [new_id()]}).status_code == 410
    assert client.post(prefix + "/export", json={"format": "wav"}).status_code == 410
    assert client.post(prefix + "/import", files={"file": ("新音频.wav", audio())}).status_code == 410
    recycled = client.get("/api/v1/projects/trash")
    assert recycled.status_code == 200
    assert [row["id"] for row in recycled.json()] == [project["id"]]
    restored = client.post(prefix + "/restore-trash")
    assert restored.status_code == 200, restored.text
    value = restored.json()
    assert value["revision"] == project["revision"] and value["clips"] == project["clips"]
    assert value["assets"][0]["id"] == asset["id"]
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    assert client.get(value["assets"][0]["media_url"]).status_code == 200
    assert client.get("/api/v1/projects/trash").json() == []
    assert client.post(prefix + "/restore-trash").status_code == 404


def test_rename_changes_recent_list_and_history_and_rejects_stale_delete(client):
    project = create_project(client)
    prefix = f"/api/v1/projects/{project['id']}"
    renamed = client.patch(prefix, json={"revision": project["revision"], "name": "  新名称 · 中文  "})
    assert renamed.status_code == 200 and renamed.json()["name"] == "新名称 · 中文"
    assert client.get("/api/v1/projects").json()[0]["name"] == "新名称 · 中文"
    assert trash(client, project).status_code == 409
    assert client.get("/api/v1/projects/trash").json() == []
    for value in ["   ", "", None]:
        refused = client.patch(prefix, json={"revision": renamed.json()["revision"], "name": value})
        assert refused.status_code == 422
    assert client.post("/api/v1/projects", json={"name": "   "}).status_code == 422
    undone = client.post(prefix + "/undo").json()
    assert undone["name"] == project["name"]
    assert client.get("/api/v1/projects").json()[0]["name"] == project["name"]
    redone = client.post(prefix + "/redo").json()
    assert redone["name"] == "新名称 · 中文"
    assert client.request("DELETE", prefix).status_code == 422
    assert trash(client, redone).status_code == 200


@pytest.mark.parametrize("kind,status", [
    ("generation", "queued"), ("generation", "running"), ("generation", "cancel_requested"),
    ("export", "queued"), ("export", "running"),
])
def test_active_tasks_block_trash_without_cancelling_or_erasing(client, kind, status):
    project = create_project(client)
    store = client.app.state.store
    identifier = new_id()
    with store.project_session(project["id"]) as session:
        if kind == "generation":
            session.add(JobRow(id=identifier, line_id=new_id(), status=status, fingerprint="test",
                               data={"id": identifier, "status": status}, snapshot={}))
        else:
            session.add(ExportRow(id=identifier, data={"id": identifier, "status": status}))
    response = trash(client, project)
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROJECT_BUSY"
    assert client.get("/api/v1/projects/trash").json() == []
    with store.project_session(project["id"]) as session:
        row = session.get(JobRow if kind == "generation" else ExportRow, identifier)
        assert row.data["status"] == status
        if kind == "generation":
            row.status = "uncertain"
        row.data = dict(row.data, status="uncertain" if kind == "generation" else "failed")
    assert trash(client, project).status_code == 200


def test_long_import_blocks_trash_until_atomic_publication(client, monkeypatch):
    import jdx_backend.app as app_module

    project = create_project(client)
    started, release = threading.Event(), threading.Event()
    original = app_module.normalize_import

    async def delayed(source, destination):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return await original(source, destination)

    monkeypatch.setattr(app_module, "normalize_import", delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, f"/api/v1/projects/{project['id']}/import", files={"file": ("音频.wav", audio())})
        try:
            assert started.wait(3)
            denied = trash(client, project)
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "PROJECT_BUSY"
        finally:
            release.set()
        imported = future.result(timeout=10)
    assert imported.status_code == 200, imported.text
    assert trash(client, imported.json()).status_code == 200


def test_trash_survives_restart_and_startup_recovery_skips_it(tmp_path):
    store = Store(tmp_path)
    project_id = store.create_project("重启后恢复")
    store.trash_project(project_id, 1)
    # Simulate a legacy interrupted record in a trashed project. Startup must
    # leave archived content untouched rather than writing recovery history.
    with Session(store.engine(project_id, include_trashed=True)) as session:
        identifier = new_id()
        session.add(JobRow(id=identifier, line_id=new_id(), status="running", fingerprint="test",
                           data={"status": "running"}, snapshot={}))
        session.commit()
    store.close()
    restored_store = Store(tmp_path)
    assert restored_store.list_projects() == []
    assert restored_store.list_trash()[0]["id"] == project_id
    assert restored_store.recover() == 0
    with Session(restored_store.engine(project_id, include_trashed=True)) as session:
        assert session.get(JobRow, identifier).status == "running"
    restored_store.restore_project(project_id)
    assert restored_store.read_project(project_id)["name"] == "重启后恢复"
    restored_store.close()


def test_app_v2_migrates_with_backup_without_changing_project_schema(tmp_path):
    store = Store(tmp_path)
    project_id = store.create_project("迁移保留")
    store.close()
    app_database = tmp_path / "studio.sqlite"
    with sqlite3.connect(app_database) as connection:
        connection.execute("DROP TABLE project_trash")
        connection.execute("PRAGMA user_version=2")
    migrated = Store(tmp_path)
    with sqlite3.connect(app_database.with_suffix(".pre-migration.sqlite")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
        assert backup.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()[0] == "迁移保留"
    with migrated.app_engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar_one() == 3
    with migrated.engine(project_id).connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar_one() == 2
    migrated.trash_project(project_id, 1)
    migrated.restore_project(project_id)
    assert migrated.read_project(project_id)["name"] == "迁移保留"
    migrated.close()


def test_imported_lines_inherit_configured_connection_without_rewriting_existing(client):
    project = create_project(client)
    prefix = f"/api/v1/projects/{project['id']}"
    original = Line(text="已有台词")
    client.patch(prefix, json={"revision": project["revision"], "lines": [original.model_dump()]})
    client.post("/api/v1/connections", json={"name": "未就绪", "workspace_id": "old"})
    connection = client.post("/api/v1/connections", json={"name": "已配置", "workspace_id": "ready", "api_key": "test-only",
                                                             "model_id": "qwen-audio-3.1-tts-flash", "voice_id": "longanhuan_v3.1"}).json()
    # 0.1.2 remembers an explicit default instead of silently switching accounts.
    client.patch("/api/v1/preferences", json={"default_connection_id": connection["id"]})
    subtitle = "1\n00:00:01,000 --> 00:00:02,500\n新导入台词。\n"
    imported = client.post(prefix + "/import", files={"file": ("字幕.srt", subtitle.encode())})
    assert imported.status_code == 200, imported.text
    lines = imported.json()["lines"]
    assert lines[0]["settings"]["connection_id"] is None
    assert lines[1]["settings"]["connection_id"] == connection["id"]
    assert lines[1]["settings"]["model_id"] == connection["model_id"]
    assert lines[1]["settings"]["target_duration_ms"] == 1500
