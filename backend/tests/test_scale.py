"""500-line scheduler load test; provider and conversion are explicit test doubles."""

import asyncio
import io
import shutil

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import select
from sqlalchemy.orm import Session

from jdx_backend import cloud
from jdx_backend.jobs import JobManager
from jdx_backend.schemas import CloudConnection, Line
from jdx_backend.storage import ConnectionRow, JobRow, Store


@pytest.mark.asyncio
async def test_500_jobs_complete_with_bounded_concurrency_and_unique_versions(tmp_path, monkeypatch):
    store = Store(tmp_path)
    connection = CloudConnection(workspace_id="test-workspace", max_concurrency=2, requests_per_minute=600)
    with Session(store.app_engine) as session:
        session.add(ConnectionRow(id=connection.id, data=connection.model_dump()))
        session.commit()
    project_id = store.create_project("500句队列压力测试")
    lines = []
    for index in range(500):
        line = Line(text=f"第{index}句", position=index)
        line.settings.connection_id = connection.id
        lines.append(line.model_dump(exclude={"versions"}))
    store.save_state(project_id, {"lines": lines}, record_history=False)
    manager = JobManager(store, {connection.id: "test-only"})
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros(480), 48000, format="WAV")
    active = peak = completed = 0

    async def synthesize(config, text, settings, **kwargs):
        nonlocal active, peak, completed
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        completed += 1
        return cloud.SynthesisResult(buffer.getvalue(), f"test-{completed}", {"characters": len(text)})

    async def normalize(source, destination):
        shutil.copyfile(source, destination)
        return {"duration_ms": 10, "sample_rate": 48000, "channels": 1}

    monkeypatch.setattr(cloud, "synthesize", synthesize)
    monkeypatch.setattr("jdx_backend.jobs.normalize_import", normalize)
    try:
        jobs = manager.enqueue(project_id, [line["id"] for line in lines], "http")
        assert len(jobs) == 500
        await asyncio.wait_for(asyncio.gather(*list(manager.tasks.values())), 120)
        with store.project_session(project_id) as session:
            results = [row.data for row in session.scalars(select(JobRow))]
        assert len(results) == completed == 500
        assert peak == 2
        assert all(result["status"] == "succeeded" for result in results)
        assert len({result["version_id"] for result in results}) == 500
        project = store.read_project(project_id)
        assert len(project["clips"]) == 500
        starts = [clip["start_sample"] for clip in sorted(project["clips"], key=lambda clip: clip["start_sample"])]
        assert len(set(starts)) == 500
        assert all(len(line["versions"]) == 1 for line in project["lines"])
    finally:
        await manager.close()
        store.close()


@pytest.mark.asyncio
async def test_rate_wait_is_cancellable_without_submission(tmp_path):
    store = Store(tmp_path)
    manager = JobManager(store, {})
    cancellation = asyncio.Event()
    connection = {"region": "cn-beijing", "workspace_id": "same-workspace", "base_url": "", "requests_per_minute": 1}
    assert await manager._wait_for_rate(connection, cancellation)
    waiting = asyncio.create_task(manager._wait_for_rate(connection, cancellation))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    cancellation.set()
    assert await asyncio.wait_for(waiting, 0.1) is False
    store.close()
