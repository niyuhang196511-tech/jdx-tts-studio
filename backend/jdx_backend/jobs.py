from __future__ import annotations

import asyncio
import base64
import copy
import csv
import hashlib
import io
import json
import logging
import sqlite3
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import cloud
from .audio import SAMPLE_RATE, audio_info, clip_duration_samples, normalize_import, render_timeline
from .schemas import ExportInput, new_id, now
from .storage import AssetRow, ExportRow, JobRow, StateRow, Store, VersionRow, safe_path

logger = logging.getLogger("jdx.jobs")


class JobManager:
    def __init__(self, store: Store, secrets: dict[str, str]):
        self.store, self.secrets = store, secrets
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.limiters: dict[str, asyncio.Semaphore] = {}
        self.events: set[asyncio.Queue[dict[str, Any]]] = set()
        self.stream_chunks: dict[str, bytearray] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.submit_times: dict[str, deque[float]] = defaultdict(deque)

    async def _wait_for_rate(self, connection: dict[str, Any], cancellation: asyncio.Event) -> bool:
        # Profiles sharing a workspace also share this local RPM budget.
        key = f"{connection.get('region')}:{connection.get('workspace_id')}:{connection.get('base_url')}"
        history = self.submit_times[key]
        limit = connection.get("requests_per_minute", 120)
        loop = asyncio.get_running_loop()
        while not cancellation.is_set():
            timestamp = loop.time()
            while history and history[0] <= timestamp - 60:
                history.popleft()
            if len(history) < limit:
                history.append(timestamp)
                return True
            try:
                await asyncio.wait_for(cancellation.wait(), max(0.01, 60 - (timestamp - history[0])))
            except TimeoutError:
                continue
        return False

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.events):
            if not queue.full():
                queue.put_nowait(event)

    def connection_config(self, connection_id: str, model_id: str | None = None, voice_id: str | None = None) -> cloud.CloudConfig:
        connection = self.store.get_connection(connection_id)
        return cloud.CloudConfig(
            id=connection_id, workspace_id=connection["workspace_id"], region=connection["region"],
            base_url=connection["base_url"], api_key=self.secrets.get(connection_id, ""),
            model_id=model_id or connection["model_id"], voice_id=voice_id or connection["voice_id"],
        )

    def resolve_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(settings)
        if not result.get("connection_id"):
            connection = self.store.default_connection(set(self.secrets))
            if not connection:
                raise ValueError("请先在云端连接中保存服务地址和 API Key")
            result["connection_id"] = connection["id"]
        return result

    def enqueue(self, project_id: str, line_ids: list[str], transport: str,
                *, loop: asyncio.AbstractEventLoop | None = None) -> list[dict[str, Any]]:
        self._loop = loop or asyncio.get_running_loop()
        result = []
        with self.store.project_session(project_id) as session:
            state = session.get(StateRow, 1)
            assert state
            lines = {line["id"]: copy.deepcopy(line) for line in state.data["lines"]}
            for line_id in dict.fromkeys(line_ids):
                if line_id not in lines:
                    raise ValueError("所选台词不存在")
                if not lines[line_id]["text"].strip():
                    raise ValueError("空台词无法生成，请先输入文本")
                lines[line_id]["settings"] = self.resolve_settings(lines[line_id]["settings"])
                config = self.connection_config(lines[line_id]["settings"]["connection_id"])
                cloud.endpoint(config)
            for line_id in dict.fromkeys(line_ids):
                line = copy.deepcopy(lines[line_id])
                fingerprint = hashlib.sha256(json.dumps(
                    [line_id, line["revision"], line["text"], line["settings"]], sort_keys=True, ensure_ascii=False
                ).encode()).hexdigest()
                existing = session.scalar(select(JobRow).where(JobRow.fingerprint == fingerprint,
                                                               JobRow.status.in_(["queued", "running", "cancel_requested"])))
                if existing:
                    result.append(copy.deepcopy(existing.data))
                    continue
                job_id = new_id()
                data = {"id": job_id, "line_id": line_id, "status": "queued", "progress": 0,
                        "created_at": now(), "error": None, "request_id": None, "usage": {}, "version_id": None}
                connection_snapshot = self.store.get_connection(line["settings"]["connection_id"])
                connection_snapshot.pop("has_credential", None)
                session.add(JobRow(id=job_id, line_id=line_id, status="queued", fingerprint=fingerprint,
                                   data=data, snapshot=dict(line, transport=transport, connection=connection_snapshot)))
                result.append(data)
        def launch() -> None:
            for item in result:
                if item["id"] not in self.tasks:
                    self.cancel_events[item["id"]] = asyncio.Event()
                    self.tasks[item["id"]] = asyncio.create_task(self._generate(project_id, item["id"]))
        if loop is None:
            launch()
        else:
            loop.call_soon_threadsafe(launch)
        return result

    def _update_job(self, project_id: str, job_id: str, **changes: Any) -> None:
        with self.store.project_session(project_id) as session:
            row = session.get(JobRow, job_id)
            if row:
                row.data = dict(row.data, **changes)
                row.status = row.data["status"]
        self.publish({"type": "job", "project_id": project_id, "job_id": job_id, **changes})

    async def _generate(self, project_id: str, job_id: str) -> None:
        cancellation = self.cancel_events[job_id]
        try:
            with self.store.project_session(project_id) as session:
                row = session.get(JobRow, job_id)
                assert row
                snapshot = copy.deepcopy(row.snapshot)
            settings = snapshot["settings"]
            connection = snapshot.get("connection") or self.store.get_connection(settings["connection_id"])
            limiter = self.limiters.setdefault(connection["id"], asyncio.Semaphore(connection.get("max_concurrency", 2)))
            async with limiter:
                if not await self._wait_for_rate(connection, cancellation):
                    return
                # The API may cancel from a worker thread while this coroutine waits
                # for a slot. Claiming under the same project lock avoids restarting
                # a task that has already transitioned to cancelled.
                with self.store.project_session(project_id) as session:
                    job = session.get(JobRow, job_id)
                    if not job or job.status != "queued" or cancellation.is_set():
                        return
                    job.status = "running"
                    job.data = dict(job.data, status="running", progress=0.05)
                self.publish({"type": "job", "project_id": project_id, "job_id": job_id,
                              "status": "running", "progress": 0.05})

                async def progress(value: float) -> None:
                    self._update_job(project_id, job_id, progress=min(0.85, max(0.05, value * 0.85)))

                async def chunk(value: bytes) -> None:
                    buffer = self.stream_chunks.setdefault(job_id, bytearray())
                    if len(buffer) + len(value) <= 96 * 1024 * 1024:
                        buffer.extend(value)
                    self.publish({"type": "audio_chunk", "project_id": project_id, "job_id": job_id,
                                  "audio": base64.b64encode(value).decode(), "sample_rate": 48000, "channels": 1})

                config = cloud.CloudConfig(
                    id=connection["id"], workspace_id=connection["workspace_id"], region=connection["region"],
                    base_url=connection["base_url"], api_key=self.secrets.get(connection["id"], ""),
                    model_id=settings["model_id"], voice_id=settings["voice_id"],
                )
                response = await cloud.synthesize(
                    config,
                    snapshot["text"], settings, transport=snapshot.get("transport", "http"),
                    cancel_event=cancellation, on_progress=progress, on_chunk=chunk,
                )
                if cancellation.is_set():
                    self._update_job(project_id, job_id, status="cancelled", request_id=response.request_id, usage=response.usage)
                    return
                self._update_job(project_id, job_id, progress=0.9, request_id=response.request_id, usage=response.usage)
                directory = self.store.project_dir(project_id)
                asset_id, version_id = new_id(), new_id()
                raw_path = directory / "temp" / f"{asset_id}.cloud"
                normalized = directory / "temp" / f"{asset_id}.wav"
                await asyncio.to_thread(raw_path.write_bytes, response.audio)
                try:
                    metadata = await normalize_import(raw_path, normalized)
                    if cancellation.is_set():
                        self._update_job(project_id, job_id, status="cancelled")
                        return
                    version = {
                        "id": version_id, "line_id": snapshot["id"], "asset_id": asset_id, **{"duration_ms": metadata["duration_ms"]},
                        "created_at": now(), "settings": settings, "model_id": settings["model_id"],
                        "request_id": response.request_id, "usage": response.usage, "best": False,
                        "text_revision": snapshot["revision"],
                    }
                    with self.store.project_session(project_id) as session:
                        job = session.get(JobRow, job_id)
                        if not job or job.status == "cancel_requested" or cancellation.is_set():
                            if job:
                                job.status = "cancelled"
                                job.data = dict(job.data, status="cancelled", request_id=response.request_id,
                                                usage=response.usage)
                            return
                        final = directory / "assets" / f"{asset_id}.wav"
                        normalized.rename(final)
                        session.add(AssetRow(id=asset_id, relative_path=f"assets/{asset_id}.wav",
                                             data={"id": asset_id, "name": snapshot["text"][:40] or "生成音频", "kind": "audio", **metadata}))
                        session.add(VersionRow(id=version_id, line_id=snapshot["id"], data=version))
                        state = session.get(StateRow, 1)
                        assert state
                        data = copy.deepcopy(state.data)
                        current = next((line for line in data["lines"] if line["id"] == snapshot["id"]), None)
                        if current and current["revision"] == snapshot["revision"]:
                            current["selected_version_id"] = version_id
                            self._place_clip(data, current, version, metadata)
                            data["updated_at"] = now()
                            state.data, state.revision = data, state.revision + 1
                        # Publication and completion are a single database commit.
                        # A cancellation arriving after this point observes success.
                        job.status = "succeeded"
                        job.data = dict(job.data, status="succeeded", progress=1, version_id=version_id)
                    self.publish({"type": "job", "project_id": project_id, "job_id": job_id,
                                  "status": "succeeded", "progress": 1, "version_id": version_id})
                    self.store.update_index(project_id)
                finally:
                    raw_path.unlink(missing_ok=True)
                    normalized.unlink(missing_ok=True)
        except cloud.CloudError as exc:
            status = "cancelled" if exc.code == "CANCELLED" else ("uncertain" if exc.uncertain else "failed")
            self._update_job(project_id, job_id, status=status, error=exc.message, request_id=exc.request_id, usage=exc.usage)
            logger.warning("cloud_job_failed job_id=%s code=%s", job_id, exc.code)
        except asyncio.CancelledError:
            self._update_job(project_id, job_id, status="interrupted", error="应用已关闭，请检查云端用量后重试")
            raise
        except Exception as exc:
            # Never log arbitrary provider bodies, URLs, request text or credentials.
            self._update_job(project_id, job_id, status="failed", error=f"生成后的媒体处理失败：{str(exc)[:400]}")
            logger.warning("job_failed job_id=%s error_type=%s", job_id, type(exc).__name__)
        finally:
            self.stream_chunks.pop(job_id, None)
            self.cancel_events.pop(job_id, None)
            self.tasks.pop(job_id, None)

    @staticmethod
    def _place_clip(data: dict[str, Any], line: dict[str, Any], version: dict[str, Any], metadata: dict[str, Any]) -> None:
        """Replace one line's primary clip, then order untouched automatic clips.

        The private map is never sent through the project API. A clip whose start
        differs from its last automatic position becomes a manual anchor. This
        makes completion order irrelevant without shifting user-placed clips.
        """
        positions = data.setdefault("_auto_clip_positions", {})
        valid_ids = {clip["id"] for clip in data["clips"]}
        for identifier in list(positions):
            if identifier not in valid_ids:
                positions.pop(identifier, None)
        for existing in data["clips"]:
            recorded = positions.get(existing["id"])
            if recorded is not None and existing["start_sample"] != recorded["start_sample"]:
                positions.pop(existing["id"], None)

        old = next((clip for clip in data["clips"] if clip.get("line_id") == line["id"]), None)
        previous_auto = positions.get(old["id"]) if old else None
        source_length = max(1, round(metadata["duration_ms"] * SAMPLE_RATE / 1000))
        clip = {
            "id": old["id"] if old else new_id(), "asset_id": version["asset_id"], "line_id": line["id"],
            "track_id": old["track_id"] if old else (line.get("role_id") or "narrator"), "label": line["text"][:30],
            "start_sample": old["start_sample"] if old else 0, "source_start_sample": 0,
            "source_end_sample": source_length,
            "gain_db": 0, "fade_in_ms": 0, "fade_out_ms": 0, "muted": False, "stretch": 1,
        }
        if old:
            for key in ("gain_db", "fade_in_ms", "fade_out_ms", "muted", "stretch"):
                clip[key] = old.get(key, clip[key])
            clip["source_start_sample"] = min(old["source_start_sample"], source_length - 1)
            was_full_source = previous_auto and old["source_end_sample"] == previous_auto["source_length"]
            clip["source_end_sample"] = (source_length if was_full_source else
                                         min(source_length, max(clip["source_start_sample"] + 1, old["source_end_sample"])))
            data["clips"][data["clips"].index(old)] = clip
        else:
            data["clips"].append(clip)
        if not old or previous_auto is not None:
            positions[clip["id"]] = {"start_sample": clip["start_sample"], "source_length": source_length}

        # Compute each successor again after any predecessor arrives or changes
        # duration. Subtitle starts and manually moved clips are fixed anchors.
        clips_by_line: dict[str, list[dict[str, Any]]] = {}
        for item in data["clips"]:
            if item.get("line_id"):
                clips_by_line.setdefault(item["line_id"], []).append(item)
        cursor, previous_pause = 0, 0
        for item in sorted(data["lines"], key=lambda value: value["position"]):
            matches = clips_by_line.get(item["id"], [])
            if not matches:
                continue
            settings = item["settings"]
            for candidate in matches:
                record = positions.get(candidate["id"])
                if record is not None:
                    subtitle = item.get("subtitle_start_ms")
                    if subtitle is not None:
                        candidate["start_sample"] = round(subtitle * SAMPLE_RATE / 1000)
                    else:
                        gap = previous_pause + settings.get("pause_before_ms", 0)
                        candidate["start_sample"] = cursor + round(gap * SAMPLE_RATE / 1000)
                    record["start_sample"] = candidate["start_sample"]
            cursor = max(cursor, *(candidate["start_sample"] + clip_duration_samples(candidate) for candidate in matches))
            previous_pause = settings.get("pause_after_ms", 200)

    def cancel(self, project_id: str, job_id: str) -> dict[str, Any]:
        with self.store.project_session(project_id) as session:
            row = session.get(JobRow, job_id)
            if not row:
                raise KeyError("生成任务不存在")
            if row.status in {"queued", "running", "cancel_requested"}:
                event = self.cancel_events.get(job_id)
                if event:
                    if self._loop:
                        self._loop.call_soon_threadsafe(event.set)
                    else:
                        event.set()
                status = "cancelled" if row.status == "queued" else "cancel_requested"
                row.status, row.data = status, dict(row.data, status=status)
            return copy.deepcopy(row.data)

    def export(self, project_id: str, options: ExportInput, preview: bool = False) -> str:
        job_id = new_id()
        project = self.store.read_project(project_id)
        data = {"id": job_id, "status": "queued", "progress": 0, "error": None, "asset_id": None}
        with self.store.project_session(project_id) as session:
            session.add(ExportRow(id=job_id, data=data))
        self.tasks[job_id] = asyncio.create_task(self._export(project_id, job_id, project, options, preview))
        return job_id

    def _update_export(self, project_id: str, job_id: str, **changes: Any) -> None:
        with self.store.project_session(project_id) as session:
            row = session.get(ExportRow, job_id)
            if row:
                row.data = dict(row.data, **changes)

    async def _export(self, project_id: str, job_id: str, project: dict[str, Any], options: ExportInput, preview: bool) -> None:
        directory = self.store.project_dir(project_id)
        extension = {"sentences": "zip", "project": "jdx"}.get(options.format, options.format)
        temporary = directory / "temp" / f"{job_id}.{extension}"
        final = directory / "exports" / f"{job_id}.{extension}"
        try:
            self._update_export(project_id, job_id, status="running", progress=0.1)
            paths = {}
            for asset in project["assets"]:
                paths[asset["id"]] = self.store.asset_path(project_id, asset["id"])[0]
            if options.format in {"wav", "mp3"}:
                await render_timeline(project["clips"], paths, temporary, output_format=options.format,
                                      sample_rate=options.sample_rate, bit_depth=options.bit_depth, normalize=options.normalize)
            elif options.format == "csv":
                await asyncio.to_thread(self._write_csv, project, temporary)
            elif options.format == "project":
                await asyncio.to_thread(self._backup, project_id, temporary)
            elif options.format == "sentences":
                await self._sentences(project, paths, temporary, options)
            temporary.rename(final)
            asset_id = new_id()
            metadata = await asyncio.to_thread(audio_info, final) if options.format in {"wav", "mp3"} else {"duration_ms": 0, "sample_rate": options.sample_rate, "channels": 2}
            name = options.filename or (f"{project['name']}-试听" if preview else project["name"])
            name = "".join(character for character in name if character not in '/\\:*?"<>|\x00')[:150] or "配音导出"
            if not name.lower().endswith(f".{extension}"):
                name += f".{extension}"
            with self.store.project_session(project_id) as session:
                session.add(AssetRow(id=asset_id, relative_path=f"exports/{job_id}.{extension}",
                                     data={"id": asset_id, "name": name, "kind": "export", **metadata}))
            self._update_export(project_id, job_id, status="succeeded", progress=1, asset_id=asset_id)
        except asyncio.CancelledError:
            self._update_export(project_id, job_id, status="failed", error="导出已中断，可以重新导出")
            raise
        except Exception as exc:
            self._update_export(project_id, job_id, status="failed", error=str(exc)[:500])
        finally:
            temporary.unlink(missing_ok=True)
            self.tasks.pop(job_id, None)

    @staticmethod
    def _write_csv(project: dict[str, Any], destination: Path) -> None:
        with destination.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["片段", "角色轨道", "开始秒", "结束秒", "音量dB", "静音", "素材ID"])
            for clip in sorted(project["clips"], key=lambda value: value["start_sample"]):
                label = clip["label"]
                if label.startswith(("=", "+", "-", "@")):
                    label = "'" + label
                writer.writerow([label, clip["track_id"], f"{clip['start_sample']/SAMPLE_RATE:.6f}",
                                 f"{(clip['start_sample']+clip_duration_samples(clip))/SAMPLE_RATE:.6f}",
                                 clip["gain_db"], "是" if clip["muted"] else "否", clip["asset_id"]])

    async def _sentences(self, project: dict[str, Any], paths: dict[str, Path], destination: Path, options: ExportInput) -> None:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, clip in enumerate(sorted(project["clips"], key=lambda value: value["start_sample"])):
                if clip["muted"]:
                    continue
                path = destination.parent / f"{destination.stem}-{index}.wav"
                try:
                    await render_timeline([dict(clip, start_sample=0)], paths, path, sample_rate=options.sample_rate,
                                          bit_depth=options.bit_depth, normalize=options.normalize)
                    archive.write(path, f"{index+1:04d}.wav")
                finally:
                    path.unlink(missing_ok=True)
            manifest = destination.parent / f"{destination.stem}-timecodes.csv"
            try:
                self._write_csv(project, manifest)
                archive.write(manifest, "时间码.csv")
            finally:
                manifest.unlink(missing_ok=True)

    def _backup(self, project_id: str, destination: Path) -> None:
        directory = self.store.project_dir(project_id)
        snapshot_path = destination.with_suffix(".snapshot.sqlite")
        rendered_assets: list[tuple[Path, str]] = []
        try:
            source = sqlite3.connect(directory / "project.sqlite")
            target = sqlite3.connect(snapshot_path)
            try:
                source.backup(target)
                # Audio exports may have been reused as timeline source material.
                # Preserve those as regular assets, while excluding archives and
                # manifests to avoid recursive backups.
                for identifier, relative, data in target.execute(
                    "SELECT id, relative_path, data FROM assets WHERE relative_path LIKE 'exports/%'"
                ).fetchall():
                    suffix = Path(relative).suffix.lower()
                    if suffix in {".wav", ".mp3"}:
                        archived_relative = f"assets/{identifier}{suffix}"
                        metadata = json.loads(data)
                        metadata["kind"] = "audio"
                        target.execute("UPDATE assets SET relative_path=?, data=? WHERE id=?",
                                       (archived_relative, json.dumps(metadata), identifier))
                        source_file = safe_path(directory, relative)
                        if source_file.is_file() and not source_file.is_symlink():
                            rendered_assets.append((source_file, archived_relative))
                    else:
                        target.execute("DELETE FROM assets WHERE id=?", (identifier,))
                target.execute("DELETE FROM exports")
                target.commit()
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
                source.close()
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot_path, "project.sqlite")
                for path in (directory / "assets").iterdir():
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, f"assets/{path.name}")
                for path, relative in rendered_assets:
                    archive.write(path, relative)
        finally:
            snapshot_path.unlink(missing_ok=True)

    async def close(self) -> None:
        for event in self.cancel_events.values():
            event.set()
        if self.tasks:
            pending = list(self.tasks.values())
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 3)
            except TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
