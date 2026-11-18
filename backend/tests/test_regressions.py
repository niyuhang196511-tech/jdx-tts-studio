"""Queue regression tests; cloud synthesis is an explicitly isolated test double."""

import asyncio
import copy
import io
import sqlite3
import zipfile

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from jdx_backend import cloud
from jdx_backend.app import create_app
from jdx_backend.jobs import JobManager
from jdx_backend.schemas import Line, new_id
from jdx_backend.storage import ConnectionRow, JobRow, Store, VersionRow


def audio_result(seconds=0.1):
    output = io.BytesIO()
    sf.write(output, np.zeros(round(48000 * seconds)), 48000, format="WAV")
    return cloud.SynthesisResult(output.getvalue(), "test-request", {"characters": 3})


@pytest.fixture
def queue(tmp_path, monkeypatch):
    store = Store(tmp_path)
    connection_id = new_id()
    with Session(store.app_engine) as session:
        session.add(ConnectionRow(id=connection_id, data={
            "id": connection_id, "name": "测试连接", "workspace_id": "test-workspace",
            "region": "cn-beijing", "base_url": "", "model_id": "qwen-audio-3.1-tts-plus",
            "voice_id": "longanhuan_v3.1", "max_concurrency": 2,
        }))
        session.commit()
    project_id = store.create_project("队列回归")
    lines = [Line(text=f"台词{index}", position=index).model_dump() for index in range(2)]
    for line in lines:
        line["settings"]["connection_id"] = connection_id
    store.save_state(project_id, {"lines": lines}, record_history=False)
    manager = JobManager(store, {connection_id: "test-key"})

    async def normalize(source, destination):
        destination.write_bytes(source.read_bytes())
        info = sf.info(destination)
        return {"duration_ms": round(info.duration * 1000), "sample_rate": info.samplerate, "channels": info.channels}

    monkeypatch.setattr("jdx_backend.jobs.normalize_import", normalize)
    yield store, manager, project_id, lines, connection_id
    store.close()


def read_jobs(store, project_id):
    with store.project_session(project_id) as session:
        return {row.line_id: copy.deepcopy(row.data) for row in session.scalars(select(JobRow))}


@pytest.mark.asyncio
async def test_reverse_completion_reflows_into_script_order(queue, monkeypatch):
    store, manager, project_id, lines, _ = queue
    second_finished = asyncio.Event()

    async def synthesis(config, text, settings, **kwargs):
        if text == lines[0]["text"]:
            await second_finished.wait()
            await asyncio.sleep(0.03)
            return audio_result(0.2)
        second_finished.set()
        return audio_result(0.1)

    monkeypatch.setattr(cloud, "synthesize", synthesis)
    manager.enqueue(project_id, [line["id"] for line in lines], "http")
    await asyncio.gather(*list(manager.tasks.values()))
    project = store.read_project(project_id)
    clips = {clip["line_id"]: clip for clip in project["clips"]}
    assert clips[lines[0]["id"]]["start_sample"] == 0
    # 200ms predecessor audio + 200ms explicit inter-line pause.
    assert clips[lines[1]["id"]]["start_sample"] == 19200
    assert all(job["status"] == "succeeded" for job in read_jobs(store, project_id).values())


@pytest.mark.asyncio
async def test_queued_cancellation_never_calls_provider(queue, monkeypatch):
    store, manager, project_id, lines, connection_id = queue
    manager.limiters[connection_id] = asyncio.Semaphore(1)
    first_started = asyncio.Event()
    release = asyncio.Event()
    called = []

    async def synthesis(config, text, settings, **kwargs):
        called.append(text)
        first_started.set()
        await release.wait()
        return audio_result()

    monkeypatch.setattr(cloud, "synthesize", synthesis)
    jobs = manager.enqueue(project_id, [line["id"] for line in lines], "http")
    await first_started.wait()
    result = manager.cancel(project_id, jobs[1]["id"])
    assert result["status"] == "cancelled"
    release.set()
    await asyncio.gather(*list(manager.tasks.values()))
    assert called == [lines[0]["text"]]
    assert read_jobs(store, project_id)[lines[1]["id"]]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_running_cancel_discards_late_audio_and_keeps_usage(queue, monkeypatch):
    store, manager, project_id, lines, _ = queue
    started, release = asyncio.Event(), asyncio.Event()

    async def synthesis(config, text, settings, **kwargs):
        started.set()
        await release.wait()
        # Deliberately simulate a provider completing despite local cancellation.
        return audio_result()

    monkeypatch.setattr(cloud, "synthesize", synthesis)
    job = manager.enqueue(project_id, [lines[0]["id"]], "http")[0]
    await started.wait()
    assert manager.cancel(project_id, job["id"])["status"] == "cancel_requested"
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*list(manager.tasks.values()))
    result = read_jobs(store, project_id)[lines[0]["id"]]
    assert result["status"] == "cancelled"
    assert result["usage"] == {"characters": 3}
    assert result["request_id"] == "test-request"
    with store.project_session(project_id) as session:
        assert list(session.scalars(select(VersionRow))) == []
    assert not store.read_project(project_id)["clips"]


@pytest.mark.asyncio
async def test_uncertain_submission_is_not_retried_and_duplicate_is_deduplicated(queue, monkeypatch):
    store, manager, project_id, lines, _ = queue
    called = 0

    async def synthesis(config, text, settings, **kwargs):
        nonlocal called
        called += 1
        await asyncio.sleep(0.02)
        raise cloud.CloudError("NETWORK_INTERRUPTED", "请求结果不确定", uncertain=True,
                               request_id="remote-request", usage={"characters": 4})

    monkeypatch.setattr(cloud, "synthesize", synthesis)
    first = manager.enqueue(project_id, [lines[0]["id"]], "http")[0]
    second = manager.enqueue(project_id, [lines[0]["id"]], "http")[0]
    assert first["id"] == second["id"]
    await asyncio.gather(*list(manager.tasks.values()))
    job = read_jobs(store, project_id)[lines[0]["id"]]
    assert called == 1 and job["status"] == "uncertain"
    assert job["usage"] == {"characters": 4} and job["request_id"] == "remote-request"


def test_auto_reflow_preserves_manual_anchor_and_audio_edits():
    first, second = Line(text="第一句", position=0).model_dump(), Line(text="第二句", position=1).model_dump()
    state = {"lines": [first, second], "clips": []}
    version1 = {"asset_id": new_id()}
    version2 = {"asset_id": new_id()}
    JobManager._place_clip(state, first, version1, {"duration_ms": 1000})
    JobManager._place_clip(state, second, version2, {"duration_ms": 1000})
    second_clip = state["clips"][1]
    second_clip.update(start_sample=240000, gain_db=-6, fade_in_ms=40, muted=True,
                       source_start_sample=2400, source_end_sample=24000)
    JobManager._place_clip(state, first, {"asset_id": new_id()}, {"duration_ms": 2000})
    assert state["clips"][1]["start_sample"] == 240000
    JobManager._place_clip(state, second, {"asset_id": new_id()}, {"duration_ms": 1200})
    selected = state["clips"][1]
    assert selected["start_sample"] == 240000
    assert selected["gain_db"] == -6 and selected["fade_in_ms"] == 40 and selected["muted"]
    assert selected["source_start_sample"] == 2400 and selected["source_end_sample"] == 24000
    assert state["clips"][0]["source_end_sample"] == 96000


def test_subtitle_anchors_survive_reflow():
    first = Line(text="前文", position=0).model_dump()
    second = Line(text="字幕", position=1, subtitle_start_ms=5000).model_dump()
    state = {"lines": [first, second], "clips": []}
    JobManager._place_clip(state, second, {"asset_id": new_id()}, {"duration_ms": 1000})
    JobManager._place_clip(state, first, {"asset_id": new_id()}, {"duration_ms": 2000})
    anchored = next(clip for clip in state["clips"] if clip["line_id"] == second["id"])
    assert anchored["start_sample"] == 240000


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path, "regression-token-" * 4, browser_dev=True)
    with TestClient(app, headers={"Authorization": "Bearer " + "regression-token-" * 4}) as client:
        yield client


def test_voice_connection_binding_and_readiness_before_submission(api, monkeypatch):
    connections = [
        api.post("/api/v1/connections", json={"name": f"连接{index}", "workspace_id": f"test-{index}",
                                            "api_key": "key"}).json()
        for index in range(2)
    ]
    voice_id = "qwen-audio-3.1-tts-plus-test-voice"
    profile = {"name": "克隆音色", "kind": "clone", "connection_id": connections[0]["id"],
               "target_model": "qwen-audio-3.1-tts-plus", "voice_id": voice_id,
               "status": "DEPLOYING", "authorized": True}
    assert api.post("/api/v1/voices", json=profile).status_code == 200
    project = api.post("/api/v1/projects", json={"name": "绑定检查"}).json()
    line = Line(text="测试绑定")
    line.settings.connection_id = connections[1]["id"]
    line.settings.voice_id = voice_id
    result = api.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "lines": [line.model_dump()]})
    assert result.status_code == 200
    response = api.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 400 and "绑定了其他" in response.json()["error"]["message"]
    line.settings.connection_id = connections[0]["id"]
    api.patch(f"/api/v1/projects/{project['id']}", json={"revision": result.json()["revision"], "lines": [line.model_dump()]})
    response = api.post(f"/api/v1/projects/{project['id']}/tasks", json={"line_ids": [line.id]})
    assert response.status_code == 400 and "尚未就绪" in response.json()["error"]["message"]
    assert api.get(f"/api/v1/projects/{project['id']}/tasks").json() == []


def test_clone_recording_decoded_and_length_checked_before_upload(api, monkeypatch):
    connection = api.post("/api/v1/connections", json={"name": "测试", "workspace_id": "test", "api_key": "key"}).json()
    calls = []

    async def enrollment(config, target_model, name, **kwargs):
        calls.append(kwargs)
        info = sf.info(io.BytesIO(kwargs["audio"]))
        assert info.samplerate == 24000 and info.channels == 1 and info.subtype == "PCM_16"
        return {"voice_id": "qwen-audio-3.1-tts-plus-test", "status": "DEPLOYING"}

    monkeypatch.setattr(cloud, "enroll_voice", enrollment)
    form = {"connection_id": connection["id"], "target_model": "qwen-audio-3.1-tts-plus",
            "name": "参考录音", "authorized": "true"}
    result = api.post("/api/v1/voices/clone", data=form, files={"file": ("bad.wav", b"not a recording")})
    assert result.status_code == 400 and calls == []
    result = api.post("/api/v1/voices/clone", data=form, files={"file": ("short.wav", audio_result(0.2).audio)})
    assert result.status_code == 400 and calls == []
    output = io.BytesIO()
    signal = 0.1 * np.sin(np.arange(6 * 48000) * 2 * np.pi * 200 / 48000)
    sf.write(output, np.stack([signal, signal], axis=1), 48000, format="WAV")
    result = api.post("/api/v1/voices/clone", data=form, files={"file": ("reference.wav", output.getvalue())})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "DEPLOYING" and len(calls) == 1


def test_relink_keeps_id_and_never_overwrites_existing_asset(api):
    project = api.post("/api/v1/projects", json={"name": "素材重连"}).json()
    project_id = project["id"]
    response = api.post(f"/api/v1/projects/{project_id}/import",
                        files={"file": ("reference.wav", audio_result(0.2).audio)})
    assert response.status_code == 200, response.text
    asset_id = response.json()["assets"][0]["id"]
    path, _ = api.app.state.store.asset_path(project_id, asset_id)
    original = path.read_bytes()
    route = f"/api/v1/projects/{project_id}/assets/{asset_id}/relink"
    assert api.post(route, files={"file": ("new.wav", audio_result(0.3).audio)}).status_code == 409
    assert path.read_bytes() == original
    path.unlink()
    assert api.post(route, files={"file": ("wrong.wav", audio_result(0.3).audio)}).status_code == 400
    assert not path.exists()
    restored = api.post(route, files={"file": ("original.wav", audio_result(0.2).audio)})
    assert restored.status_code == 200, restored.text
    assert restored.json()["assets"][0]["id"] == asset_id and not restored.json()["assets"][0]["missing"]
    assert path.is_file()


def test_restore_rejects_trigger_and_preserves_new_project_id_after_undo(api, tmp_path):
    project = api.post("/api/v1/projects", json={"name": "恢复检查"}).json()
    changed = api.patch(f"/api/v1/projects/{project['id']}", json={"revision": project["revision"], "name": "已修改"}).json()
    archive_path = tmp_path / "backup.jdx"
    api.app.state.manager._backup(project["id"], archive_path)
    response = api.post("/api/v1/projects/restore", files={"file": ("backup.jdx", archive_path.read_bytes())})
    assert response.status_code == 200, response.text
    restored = response.json()
    undone = api.post(f"/api/v1/projects/{restored['id']}/undo")
    assert undone.status_code == 200 and undone.json()["id"] == restored["id"]
    snapshot = tmp_path / "malicious.sqlite"
    with zipfile.ZipFile(archive_path) as archive:
        snapshot.write_bytes(archive.read("project.sqlite"))
    connection = sqlite3.connect(snapshot)
    connection.execute("CREATE TRIGGER injected AFTER UPDATE ON project_state BEGIN SELECT 1; END")
    connection.commit()
    connection.close()
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("project.sqlite", snapshot.read_bytes())
    response = api.post("/api/v1/projects/restore", files={"file": ("bad.jdx", malicious.getvalue())})
    assert response.status_code == 400 and "结构" in response.json()["error"]["message"]
