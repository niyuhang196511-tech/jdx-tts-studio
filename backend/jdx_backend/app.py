from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
import numpy as np
import soundfile as sf

from . import __version__, cloud
from .audio import SAMPLE_RATE, clip_duration_samples, ffmpeg_path, normalize_import, run_ffmpeg, video_proxy
from .jobs import JobManager
from .schemas import (
    Asset, AudioVersion, CloudConnection, ConnectionInput, DirectorInput, ExportInput, GenerationJob, Line, ModelProfile,
    Project, ProjectCreate, ProjectDelete, ProjectDeleted, ProjectPatch, TrashedProject, Role, TimelineClip, VoiceDesignInput, VoiceProfile, TaskInput, LineSettings, new_id, now,
)
from .storage import (
    AssetRow, ConflictError, ConnectionRow, ExportRow, JobRow, ProjectBase, ProjectBusyError, ProjectIndex, ProjectTrashedError, SettingRow,
    StateRow, Store, VersionRow, VoiceRow, make_engine, safe_path, validate_id,
)
from .text_tools import director_plan, import_text
from .voice_preview import cache_official_sample

logger = logging.getLogger("jdx.api")
MAX_UPLOAD = 2 * 1024 * 1024 * 1024


def create_app(data_dir: Path, session_token: str, *, browser_dev: bool = False, secrets: dict[str, str] | None = None) -> FastAPI:
    if len(session_token) < 32:
        raise ValueError("本地会话令牌过短")
    store = Store(data_dir)
    key_store = secrets if secrets is not None else {}
    manager = JobManager(store, key_store)
    system_previews = {str(uuid5(NAMESPACE_URL, voice["id"])): voice for voice in cloud.SYSTEM_VOICES}
    preview_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.recover()
        yield
        await manager.close()
        store.close()

    app = FastAPI(title="Jdx TTS Studio 本地 API", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.manager, app.state.secrets = store, manager, key_store
    allowed_origins = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"]
    if browser_dev:
        allowed_origins += ["http://127.0.0.1:1420", "http://localhost:1420", "http://127.0.0.1:1421", "http://localhost:1421"]
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def guard(request: Request, call_next: Any):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return _error(403, "HOST_REJECTED", "不允许的本地服务地址")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def authenticated(request: Request) -> None:
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided, f"Bearer {session_token}"):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="本地会话已失效，请重新连接")

    api = "/api/v1"
    auth = [Depends(authenticated)]

    @app.exception_handler(KeyError)
    async def missing(_: Request, exc: KeyError):
        return _error(404, "NOT_FOUND", str(exc).strip("'"))

    @app.exception_handler(ValueError)
    async def invalid(_: Request, exc: ValueError):
        return _error(400, "INVALID_INPUT", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError):
        return _error(409, "REVISION_CONFLICT", str(exc))

    @app.exception_handler(ProjectTrashedError)
    async def project_trashed(_: Request, exc: ProjectTrashedError):
        return _error(410, "PROJECT_TRASHED", str(exc))

    @app.exception_handler(ProjectBusyError)
    async def project_busy(_: Request, exc: ProjectBusyError):
        return _error(409, "PROJECT_BUSY", str(exc))

    @app.exception_handler(cloud.CloudError)
    async def cloud_error(_: Request, exc: cloud.CloudError):
        return _error(502, exc.code, exc.message, request_id=exc.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation(_: Request, exc: RequestValidationError):
        fields = [".".join(str(value) for value in error["loc"][1:]) for error in exc.errors()]
        return _error(422, "VALIDATION_FAILED", "参数格式不正确：" + "、".join(fields[:8]))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        logger.error("api_error type=%s", type(exc).__name__)
        return _error(500, "INTERNAL_ERROR", "操作失败，原始素材已保留，请查看日志或重试")

    def media_url(request: Request, project_id: str, asset_id: str, download: bool = False) -> str:
        expires = int(time.time()) + 3600
        message = f"{project_id}:{asset_id}:{expires}:{int(download)}"
        signature = hmac.new(session_token.encode(), message.encode(), hashlib.sha256).hexdigest()
        return f"{str(request.base_url).rstrip('/')}{api}/media/{project_id}/{asset_id}?expires={expires}&download={int(download)}&signature={signature}"

    def project_response(request: Request, project_id: str) -> dict[str, Any]:
        project = store.read_project(project_id)
        for asset in project["assets"]:
            asset["media_url"] = media_url(request, project_id, asset["id"])
        return project

    def connections() -> list[dict[str, Any]]:
        return [dict(cloud.normalize_connection(row), has_credential=bool(key_store.get(row["id"]))) for row in store.list_rows(ConnectionRow)]

    def voice_response(request: Request, voice: dict[str, Any]) -> dict[str, Any]:
        if voice.get("kind") == "system":
            official = next((item for item in cloud.SYSTEM_VOICES
                             if item["voice_id"] == voice.get("voice_id")
                             and item["target_model"] == voice.get("target_model")), None)
            if official:
                merged = dict(official)
                for key in ("id", "name", "connection_id", "favorite", "notes"):
                    if key in voice:
                        merged[key] = voice[key]
                merged["favorite"] = bool(voice.get("favorite", merged.get("favorite", False)))
                voice = merged
        result = dict(voice, preview_url=None)
        result.pop("official_sample_url", None)
        identifier = str(uuid5(NAMESPACE_URL, voice["id"])) if voice["id"].startswith("system:") else validate_id(voice["id"])
        path = safe_path(store.root, f"voice-previews/{identifier}.wav")
        if path.is_file() and not path.is_symlink():
            expires = int(time.time()) + 3600
            signature = hmac.new(session_token.encode(), f"voice:{identifier}:{expires}".encode(), hashlib.sha256).hexdigest()
            result["preview_url"] = f"{str(request.base_url).rstrip('/')}{api}/voice-preview/{identifier}?expires={expires}&signature={signature}"
            result["preview_available"] = True
            if not result.get("preview_source"):
                result["preview_source"] = "design" if voice["kind"] == "design" else "reference"
        return result

    def voice_list(request: Request) -> list[dict[str, Any]]:
        unique = {row["id"]: row for row in cloud.SYSTEM_VOICES + store.list_rows(VoiceRow)}
        return [voice_response(request, row) for row in unique.values()]

    @app.get(f"{api}/health", dependencies=auth)
    def health():
        return {"status": "ok", "version": __version__}

    @app.get(f"{api}/openapi.json", dependencies=auth)
    def openapi():
        return app.openapi()

    @app.get(f"{api}/bootstrap", dependencies=auth)
    def bootstrap(request: Request):
        with Session(store.app_engine) as session:
            preferences = session.get(SettingRow, "preferences")
            preferences_value = preferences.value if preferences else {}
        try:
            ffmpeg_path()
            media_ready = True
        except RuntimeError:
            media_ready = False
        return {"projects": store.list_projects(), "connections": connections(), "models": cloud.MODELS,
                "voices": voice_list(request), "preferences": preferences_value,
                "environment": {"version": __version__, "ffmpeg_available": media_ready, "cloud_only": True,
                                "credential_storage": "session" if browser_dev else "desktop"}}

    @app.patch(f"{api}/preferences", dependencies=auth)
    def preferences(value: dict[str, Any]):
        if len(json.dumps(value)) > 32768:
            raise ValueError("偏好设置过大")
        if value.get("default_connection_id"):
            store.get_connection(value["default_connection_id"])
        if "favorite_voice_ids" in value:
            favorites = value["favorite_voice_ids"]
            if not isinstance(favorites, list) or len(favorites) > 512 or any(not isinstance(item, str) for item in favorites):
                raise ValueError("收藏的音色列表格式不正确")
            known = {item["id"] for item in cloud.SYSTEM_VOICES} | {item["id"] for item in store.list_rows(VoiceRow)}
            unknown = set(favorites) - known
            if unknown:
                raise ValueError("收藏列表包含不存在的音色")
            value["favorite_voice_ids"] = list(dict.fromkeys(favorites))
        with Session(store.app_engine) as session:
            # Acquire the SQLite writer reservation before reading, so two
            # independent preference patches cannot discard each other's keys.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(SettingRow, "preferences")
            if row:
                value = {**row.value, **value}
                if len(json.dumps(value)) > 32768:
                    raise ValueError("偏好设置过大")
                row.value = value
            else:
                session.add(SettingRow(key="preferences", value=value))
            session.commit()
        return value

    @app.get(f"{api}/connections", dependencies=auth, response_model=list[CloudConnection])
    def list_connections():
        return connections()

    @app.get(f"{api}/models", dependencies=auth, response_model=list[ModelProfile])
    def models():
        return cloud.MODELS

    @app.post(f"{api}/connections", dependencies=auth, response_model=CloudConnection)
    def create_connection(value: ConnectionInput):
        validate_id(value.id)
        data = cloud.normalize_connection(value.model_dump(exclude={"api_key"}))
        data["has_credential"] = False
        if value.api_key and not browser_dev:
            raise ValueError("桌面密钥必须通过系统凭据库保存")
        with Session(store.app_engine) as session:
            if session.get(ConnectionRow, value.id):
                raise ValueError("连接标识已存在")
            session.add(ConnectionRow(id=value.id, data=data))
            preference = session.get(SettingRow, "preferences")
            if not preference:
                session.add(SettingRow(key="preferences", value={"default_connection_id": value.id}))
            elif not preference.value.get("default_connection_id"):
                preference.value = dict(preference.value, default_connection_id=value.id)
            session.commit()
        if value.api_key:
            key_store[value.id] = value.api_key
        return dict(data, has_credential=bool(key_store.get(value.id)))

    @app.patch(f"{api}/connections/{{connection_id}}", dependencies=auth, response_model=CloudConnection)
    def update_connection(connection_id: str, value: dict[str, Any]):
        existing = store.get_connection(connection_id)
        previous_url = existing["base_url"]
        if "base_url" not in value and any(key in value and value[key] != existing[key] for key in ("workspace_id", "region")):
            existing["base_url"] = ""
        if "api_key" in value and value["api_key"] and not browser_dev:
            raise ValueError("桌面密钥必须通过系统凭据库保存")
        parsed = ConnectionInput.model_validate(dict(existing, **{key: item for key, item in value.items() if key != "id"}))
        data = cloud.normalize_connection(parsed.model_dump(exclude={"api_key"}))
        if data["base_url"] != previous_url:
            logger.info("connection_endpoint_changed connection_id=%s", connection_id)
        data["id"], data["has_credential"] = connection_id, False
        with Session(store.app_engine) as session:
            row = session.get(ConnectionRow, connection_id)
            assert row
            row.data = data
            session.commit()
        if parsed.api_key:
            key_store[connection_id] = parsed.api_key
        # Existing in-flight tasks retain their semaphore; new limits take effect on next idle request batch.
        if not any(job for job in manager.tasks.values() if not job.done()):
            manager.limiters.pop(connection_id, None)
        return dict(data, has_credential=bool(key_store.get(connection_id)))

    @app.delete(f"{api}/connections/{{connection_id}}", dependencies=auth)
    def delete_connection(connection_id: str):
        with Session(store.app_engine) as session:
            row = session.get(ConnectionRow, connection_id)
            if not row:
                raise KeyError("连接不存在")
            session.delete(row)
            preference = session.get(SettingRow, "preferences")
            if preference and preference.value.get("default_connection_id") == connection_id:
                replacement = session.scalar(select(ConnectionRow).where(ConnectionRow.id != connection_id))
                preference.value = dict(preference.value, default_connection_id=replacement.id if replacement else None)
            session.commit()
        key_store.pop(connection_id, None)
        return {"deleted": True}

    @app.post(f"{api}/connections/{{connection_id}}/test", dependencies=auth)
    async def test_connection(connection_id: str):
        return await cloud.check_connection(manager.connection_config(connection_id))

    @app.get(f"{api}/projects", dependencies=auth)
    def list_projects():
        return store.list_projects()

    @app.post(f"{api}/projects", dependencies=auth, response_model=Project)
    def create_project(value: ProjectCreate, request: Request):
        return project_response(request, store.create_project(value.name))

    @app.get(f"{api}/projects/trash", dependencies=auth, response_model=list[TrashedProject])
    def list_trashed_projects():
        return store.list_trash()

    @app.delete(f"{api}/projects/{{project_id}}", dependencies=auth, response_model=ProjectDeleted)
    def delete_project(project_id: str, value: ProjectDelete):
        result = store.trash_project(project_id, value.revision)
        logger.info("project_trashed project_id=%s", project_id)
        return result

    @app.post(f"{api}/projects/{{project_id}}/restore-trash", dependencies=auth, response_model=Project)
    def restore_trashed_project(project_id: str, request: Request):
        store.restore_project(project_id)
        logger.info("project_restored project_id=%s", project_id)
        return project_response(request, project_id)

    def protect_project_operation(project_id: str):
        with store.project_operation(project_id):
            yield

    @app.get(f"{api}/projects/{{project_id}}", dependencies=auth, response_model=Project)
    def get_project(project_id: str, request: Request):
        return project_response(request, project_id)

    @app.patch(f"{api}/projects/{{project_id}}", dependencies=auth, response_model=Project)
    def patch_project(project_id: str, value: ProjectPatch, request: Request):
        # Project lock spans validation + update, including concurrent cloud result publication.
        with store.project_session(project_id) as session:
            state = session.get(StateRow, 1)
            assert state
            if state.revision != value.revision:
                raise ConflictError("项目已更新，请刷新后重试")
            changes = value.model_dump(exclude_unset=True, exclude={"revision"})
            assets = {row.id: row for row in session.scalars(select(AssetRow))}
            if value.lines is not None:
                old_lines = {line["id"]: line for line in state.data["lines"]}
                line_ids = [line.id for line in value.lines]
                if len(set(line_ids)) != len(line_ids):
                    raise ValueError("台词标识不能重复")
                lines = []
                for index, line in enumerate(value.lines):
                    validate_id(line.id)
                    data = line.model_dump(exclude={"versions"})
                    old = old_lines.get(line.id)
                    changed = old is None or any(data.get(key) != old.get(key) for key in ("text", "settings", "role_id"))
                    data["revision"] = (old["revision"] + int(changed)) if old else 1
                    data["position"] = index
                    data["selected_version_id"] = old.get("selected_version_id") if old else None
                    lines.append(data)
                changes["lines"] = lines
                if "clips" not in changes:
                    changes["clips"] = [clip for clip in state.data["clips"] if not clip.get("line_id") or clip["line_id"] in line_ids]
            if value.clips is not None:
                if len({clip.id for clip in value.clips}) != len(value.clips):
                    raise ValueError("片段标识不能重复")
                for clip in value.clips:
                    asset = assets.get(clip.asset_id)
                    if not asset or asset.data["kind"] == "video":
                        raise ValueError("时间轴片段引用了不存在的音频")
                    if clip.source_end_sample > round(asset.data["duration_ms"] * SAMPLE_RATE / 1000) + SAMPLE_RATE // 100:
                        raise ValueError("裁剪范围超出了素材时长")
            if value.video_asset_id:
                asset = assets.get(value.video_asset_id)
                if not asset or asset.data["kind"] != "video":
                    raise ValueError("视频素材不存在")
            # save_state creates its own transaction; reentrant project lock prevents other writers.
            store.save_state(project_id, changes, value.revision)
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/undo", dependencies=auth, response_model=Project)
    def undo(project_id: str, request: Request):
        store.history(project_id, False)
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/redo", dependencies=auth, response_model=Project)
    def redo(project_id: str, request: Request):
        store.history(project_id, True)
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/import", dependencies=[*auth, Depends(protect_project_operation)], response_model=Project)
    async def import_file(project_id: str, request: Request, file: UploadFile = File(...)):
        directory = store.project_dir(project_id)
        store.engine(project_id)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix in {".txt", ".srt", ".vtt"}:
            data = await file.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("文本文件不能超过 16 MB")
            lines = await asyncio.to_thread(import_text, data, suffix)
            default_connection = store.default_connection(set(key_store))
            if default_connection:
                for line in lines:
                    line["settings"].update(connection_id=default_connection["id"],
                                            model_id=default_connection["model_id"],
                                            voice_id=default_connection["voice_id"])
            with store.project_session(project_id) as session:
                state = session.get(StateRow, 1)
                assert state
                current = copy.deepcopy(state.data["lines"])
                for line in lines:
                    line["position"] = len(current)
                    current.append(line)
                store.save_state(project_id, {"lines": current}, state.revision)
        elif suffix in {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".aiff", ".mp4", ".mov", ".mkv", ".webm", ".avi"}:
            asset_id = new_id()
            source = directory / "temp" / f"{asset_id}{suffix}"
            is_video = suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi"}
            output_suffix = ".mp4" if is_video else ".wav"
            output = directory / "temp" / f"{asset_id}.normalized{output_suffix}"
            try:
                await save_upload(file, source)
                metadata = await (video_proxy(source, output) if is_video else normalize_import(source, output))
                relative = f"assets/{asset_id}{output_suffix}"
                output.rename(directory / relative)
                with store.project_session(project_id) as session:
                    session.add(AssetRow(id=asset_id, relative_path=relative,
                                         data={"id": asset_id, "name": Path(file.filename or "素材").name,
                                               "kind": "video" if is_video else "audio", **metadata}))
                if is_video:
                    store.save_state(project_id, {"video_asset_id": asset_id})
                else:
                    with store.project_session(project_id) as session:
                        state = session.get(StateRow, 1)
                        assert state
                        clips = copy.deepcopy(state.data["clips"])
                        clips.append({"id": new_id(), "asset_id": asset_id, "line_id": None, "track_id": "narrator",
                                      "label": Path(file.filename or "素材").stem, "start_sample": max((clip["start_sample"]+clip_duration_samples(clip) for clip in clips), default=0),
                                      "source_start_sample": 0, "source_end_sample": max(1, round(metadata["duration_ms"]*SAMPLE_RATE/1000)),
                                      "gain_db": 0, "fade_in_ms": 0, "fade_out_ms": 0, "muted": False, "stretch": 1})
                        store.save_state(project_id, {"clips": clips})
            finally:
                source.unlink(missing_ok=True)
                output.unlink(missing_ok=True)
        else:
            raise ValueError("请选择 TXT、SRT、VTT 或支持的音视频文件")
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/tasks", dependencies=auth)
    async def generate(project_id: str, value: TaskInput):
        await validate_voice_bindings(store, manager, project_id, value.line_ids)
        jobs = await asyncio.to_thread(manager.enqueue, project_id, value.line_ids, value.transport,
                                       loop=asyncio.get_running_loop())
        return {"jobs": jobs}

    @app.post(f"{api}/projects/{{project_id}}/assets/{{asset_id}}/relink", dependencies=[*auth, Depends(protect_project_operation)], response_model=Project)
    async def relink_asset(project_id: str, asset_id: str, request: Request, file: UploadFile = File(...)):
        original, asset = store.asset_path(project_id, asset_id)
        if asset["kind"] not in {"audio", "video"}:
            raise ValueError("导出文件可重新导出，不支持重新链接")
        if original.exists():
            raise ConflictError("原素材仍然存在，重新链接不会覆盖它")
        directory = store.project_dir(project_id)
        source = directory / "temp" / f"{new_id()}.upload"
        is_video = asset["kind"] == "video"
        normalized = directory / "temp" / f"{new_id()}{'.mp4' if is_video else '.wav'}"
        try:
            await save_upload(file, source)
            metadata = await (video_proxy(source, normalized) if is_video else normalize_import(source, normalized))
            tolerance_ms = max(20, round(asset["duration_ms"] * 0.001))
            if abs(metadata["duration_ms"] - asset["duration_ms"]) > tolerance_ms:
                raise ValueError("新素材时长与原素材不一致，请选择原始文件；如需更换内容，请作为新素材导入")
            with store.project_session(project_id) as session:
                row = session.get(AssetRow, asset_id)
                if not row:
                    raise KeyError("素材不存在")
                state = session.get(StateRow, 1)
                assert state
                if not is_video and any(
                    clip["asset_id"] == asset_id
                    and clip["source_end_sample"] > round(metadata["duration_ms"] * SAMPLE_RATE / 1000) + SAMPLE_RATE // 100
                    for clip in state.data["clips"]
                ):
                    raise ValueError("新素材不能完整覆盖现有时间轴裁剪范围")
                # Same-filesystem link provides atomic no-clobber creation on both
                # platforms, even if another process restores the file meanwhile.
                try:
                    os.link(normalized, original)
                except FileExistsError as exc:
                    raise ConflictError("原素材已恢复，重新链接不会覆盖它") from exc
        finally:
            source.unlink(missing_ok=True)
            normalized.unlink(missing_ok=True)
        return project_response(request, project_id)

    @app.get(f"{api}/projects/{{project_id}}/tasks", dependencies=auth, response_model=list[GenerationJob])
    def list_tasks(project_id: str):
        with store.project_session(project_id) as session:
            return [row.data for row in session.scalars(select(JobRow).order_by(JobRow.id))]

    @app.post(f"{api}/projects/{{project_id}}/tasks/{{job_id}}/cancel", dependencies=auth, response_model=GenerationJob)
    def cancel(project_id: str, job_id: str):
        return manager.cancel(project_id, job_id)

    @app.post(f"{api}/projects/{{project_id}}/lines/{{line_id}}/versions/{{version_id}}/select", dependencies=auth, response_model=Project)
    def select_version(project_id: str, line_id: str, version_id: str, request: Request):
        with store.project_session(project_id) as session:
            version = session.get(VersionRow, version_id)
            state = session.get(StateRow, 1)
            if not version or version.line_id != line_id or not state:
                raise KeyError("音频版本不存在")
            data = copy.deepcopy(state.data)
            line = next((item for item in data["lines"] if item["id"] == line_id), None)
            if not line:
                raise KeyError("台词不存在")
            line["selected_version_id"] = version_id
            asset = session.get(AssetRow, version.data["asset_id"])
            assert asset
            manager._place_clip(data, line, version.data, asset.data)
            store.save_state(project_id, {"lines": data["lines"], "clips": data["clips"],
                                          "_auto_clip_positions": data.get("_auto_clip_positions", {})})
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/lines/{{line_id}}/versions/{{version_id}}/best", dependencies=auth, response_model=Project)
    def best_version(project_id: str, line_id: str, version_id: str, request: Request):
        with store.project_session(project_id) as session:
            target = session.get(VersionRow, version_id)
            if not target or target.line_id != line_id:
                raise KeyError("音频版本不存在")
            for version in session.scalars(select(VersionRow).where(VersionRow.line_id == line_id)):
                version.data = dict(version.data, best=version.id == version_id)
        return project_response(request, project_id)

    @app.post(f"{api}/projects/{{project_id}}/director", dependencies=auth)
    def director(project_id: str, value: DirectorInput):
        store.engine(project_id)
        return director_plan(value.text, value.instruction)

    @app.post(f"{api}/projects/{{project_id}}/export", dependencies=auth)
    async def export(project_id: str, value: ExportInput):
        return {"job_id": manager.export(project_id, value)}

    @app.post(f"{api}/projects/{{project_id}}/render", dependencies=auth)
    async def render(project_id: str):
        return {"job_id": manager.export(project_id, ExportInput(), preview=True)}

    @app.post(f"{api}/projects/{{project_id}}/backup", dependencies=auth)
    async def backup(project_id: str):
        return {"job_id": manager.export(project_id, ExportInput(format="project"))}

    @app.get(f"{api}/projects/{{project_id}}/exports/{{job_id}}", dependencies=auth)
    def export_status(project_id: str, job_id: str, request: Request):
        with store.project_session(project_id) as session:
            row = session.get(ExportRow, job_id)
            if not row:
                raise KeyError("导出任务不存在")
            data = copy.deepcopy(row.data)
            if data.get("asset_id"):
                data["download_url"] = media_url(request, project_id, data["asset_id"], True)
            return data

    @app.post(f"{api}/projects/restore", dependencies=auth, response_model=Project)
    async def restore(request: Request, file: UploadFile = File(...)):
        temporary_root = store.root / "temp"
        temporary_root.mkdir(exist_ok=True)
        archive_path = temporary_root / f"{new_id()}.jdx"
        try:
            await save_upload(file, archive_path)
            project_id = await asyncio.to_thread(restore_archive, store, archive_path)
            return project_response(request, project_id)
        finally:
            archive_path.unlink(missing_ok=True)

    @app.get(f"{api}/voices", dependencies=auth)
    def voices(request: Request):
        return voice_list(request)

    @app.post(f"{api}/voices/{{voice_id}}/preview", dependencies=auth, response_model=VoiceProfile)
    async def prepare_voice_preview(voice_id: str, request: Request):
        builtin = next((voice for voice in cloud.SYSTEM_VOICES if voice["id"] == voice_id), None)
        if builtin:
            identifier = str(uuid5(NAMESPACE_URL, builtin["id"]))
            destination = safe_path(store.root, f"voice-previews/{identifier}.wav")
            async with preview_locks.setdefault(identifier, asyncio.Lock()):
                await cache_official_sample(builtin["official_sample_url"], destination)
            logger.info("official_preview_ready voice_id=%s", builtin["voice_id"])
            return voice_response(request, builtin)
        with Session(store.app_engine) as session:
            row = session.get(VoiceRow, validate_id(voice_id))
            if not row:
                raise KeyError("声音配置不存在")
            voice = voice_response(request, row.data)
        if not voice.get("preview_url"):
            raise ValueError("此声音没有已保存的样音，可在项目中生成短句试听；不会自动发起收费生成")
        return voice

    @app.post(f"{api}/voices", dependencies=auth, response_model=VoiceProfile)
    def save_voice(value: VoiceProfile, request: Request):
        if value.id.startswith("system:"):
            if not any(item["id"] == value.id for item in cloud.SYSTEM_VOICES) or value.kind != "system":
                raise ValueError("系统音色标识无效")
        else:
            validate_id(value.id)
        data = value.model_dump(exclude={"preview_url"})
        official = next((item for item in cloud.SYSTEM_VOICES
                         if item["voice_id"] == value.voice_id and item["target_model"] == value.target_model), None)
        if official:
            for key in ("gender", "languages", "tags", "category", "regions", "source_url",
                        "preview_available", "preview_source", "ssml", "instruction", "word_timestamps"):
                data[key] = official.get(key, data.get(key))
        if value.connection_id:
            connection = store.get_connection(value.connection_id)
            data.update(workspace_id=connection["workspace_id"], region=connection["region"])
        with Session(store.app_engine) as session:
            row = session.get(VoiceRow, value.id)
            if row:
                if row.data.get("kind") != "system":
                    if any(data[key] != row.data.get(key) for key in ("connection_id", "target_model", "voice_id", "kind")):
                        raise ValueError("专属音色的连接和模型绑定不能直接改写，请复制为新的配置")
                    data["workspace_id"] = row.data.get("workspace_id")
                    data["region"] = row.data.get("region")
                row.data = data
            else:
                session.add(VoiceRow(id=value.id, data=data))
            session.commit()
        return voice_response(request, {**data, **(official or {})} if official else data)

    @app.delete(f"{api}/voices/{{voice_id}}", dependencies=auth)
    def delete_voice(voice_id: str):
        with Session(store.app_engine) as session:
            row = session.get(VoiceRow, voice_id)
            if not row:
                raise KeyError("只能移除本地保存的声音配置")
            session.delete(row)
            session.commit()
        return {"deleted": True, "message": "本地配置已移除，云端音色和历史音频保留"}

    async def record_enrollment(value: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        preview = response.pop("preview_audio", None)
        value["voice_id"], value["status"] = response["voice_id"], response.get("status", "DEPLOYING")
        # Keep the created remote identity even if its optional preview is corrupt:
        # retrying creation could otherwise produce duplicate billable voices.
        with Session(store.app_engine) as session:
            session.add(VoiceRow(id=value["id"], data=value))
            session.commit()
        if preview:
            directory = store.root / "voice-previews"
            directory.mkdir(exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(prefix="preview-", dir=directory) as temporary:
                    raw, normalized = Path(temporary) / "preview.upload", Path(temporary) / "preview.wav"
                    if not isinstance(preview, bytes) or len(preview) > 20 * 1024 * 1024:
                        raise ValueError("预览音频格式或大小不正确")
                    await asyncio.to_thread(raw.write_bytes, preview)
                    await normalize_import(raw, normalized)
                    os.link(normalized, directory / f"{value['id']}.wav")
            except (ValueError, RuntimeError, OSError):
                logger.warning("voice_preview_failed voice_id=%s", value["id"])
                value["notes"] = (value.get("notes", "") + "\n音色已保存，但预览音频不可用；请生成短句试听。").strip()
                with Session(store.app_engine) as session:
                    row = session.get(VoiceRow, value["id"])
                    assert row
                    row.data = value
                    session.commit()
        return value

    @app.post(f"{api}/voices/clone", dependencies=auth, response_model=VoiceProfile)
    async def clone(request: Request, file: UploadFile = File(...), connection_id: str = Form(...), target_model: str = Form(...),
                    name: str = Form(...), authorized: bool = Form(...), reference_text: str = Form("")):
        if not authorized:
            raise ValueError("请先确认已取得参考声音的使用授权")
        # Official voice cloning guidance: <=10 MB, <=60 s and at least
        # 5 seconds of continuous effective speech. Actual speech quality still
        # requires the user to preview and the provider to validate.
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("参考录音不能超过 10 MB，请裁剪后重试")
        if not data:
            raise ValueError("参考录音为空")
        temporary_root = store.root / "temp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="voice-", dir=temporary_root) as temporary:
            source, output = Path(temporary) / "reference.upload", Path(temporary) / "reference.wav"
            await asyncio.to_thread(source.write_bytes, data)
            await run_ffmpeg([
                "-protocol_whitelist", "file,pipe", "-format_whitelist", "wav,mp3,flac,ogg,mov,aac,aiff",
                "-i", str(source), "-map", "0:a:0", "-vn", "-t", "61", "-af", "pan=mono|c0=c0",
                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", "-n", str(output),
            ])
            samples, rate = await asyncio.to_thread(sf.read, output, dtype="float32", always_2d=True)
            duration = len(samples) / rate
            if not 5 <= duration <= 60:
                raise ValueError("参考录音须为 5 至 60 秒，建议提供 10 至 20 秒连续清晰的人声")
            if not np.isfinite(samples).all() or float(np.max(np.abs(samples))) < 1e-4:
                raise ValueError("参考录音没有可用声音，请更换包含清晰人声的录音")
            data = await asyncio.to_thread(output.read_bytes)
        config = manager.connection_config(connection_id)
        response = await cloud.enroll_voice(config, target_model, name,
                                           audio=data, audio_filename="reference.wav", reference_text=reference_text)
        # The preview is explicitly the user's reference recording, never
        # labelled as a synthesized demonstration of the cloned voice.
        response["preview_audio"] = data
        value = VoiceProfile(name=name, kind="clone", connection_id=connection_id, target_model=target_model,
                             voice_id=response["voice_id"], authorized=True, ssml=True, instruction=True, word_timestamps=True,
                             preview_source="reference",
                             workspace_id=config.workspace_id, region=config.region,
                             notes=("参考文本：" + reference_text) if reference_text else "").model_dump(exclude={"preview_url"})
        return voice_response(request, await record_enrollment(value, response))

    @app.post(f"{api}/voices/design", dependencies=auth, response_model=VoiceProfile)
    async def design(value: VoiceDesignInput, request: Request):
        config = manager.connection_config(value.connection_id)
        response = await cloud.enroll_voice(config, value.target_model, value.name,
                                           voice_prompt=value.voice_prompt, preview_text=value.preview_text)
        profile = VoiceProfile(name=value.name, kind="design", connection_id=value.connection_id,
                               target_model=value.target_model, voice_id=response["voice_id"],
                               notes=value.voice_prompt, authorized=True, instruction=True,
                               preview_source="design",
                               workspace_id=config.workspace_id, region=config.region).model_dump(exclude={"preview_url"})
        return voice_response(request, await record_enrollment(profile, response))

    @app.post(f"{api}/voices/{{voice_id}}/refresh", dependencies=auth, response_model=VoiceProfile)
    async def refresh_voice(voice_id: str, request: Request):
        with Session(store.app_engine) as session:
            row = session.get(VoiceRow, voice_id)
            if not row:
                raise KeyError("声音配置不存在")
            data = copy.deepcopy(row.data)
        response = await cloud.query_voice(manager.connection_config(data["connection_id"]), data["voice_id"])
        data["status"] = response.get("status", data["status"])
        with Session(store.app_engine) as session:
            row = session.get(VoiceRow, voice_id)
            assert row
            row.data = data
            session.commit()
        return voice_response(request, data)

    @app.get(f"{api}/events", dependencies=auth)
    async def events(request: Request):
        async def iterator():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
            manager.events.add(queue)
            try:
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), 15)
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                manager.events.discard(queue)
        return StreamingResponse(iterator(), media_type="text/event-stream")

    @app.get(f"{api}/voice-preview/{{voice_id}}")
    def voice_preview(voice_id: str, expires: int, signature: str):
        identifier = validate_id(voice_id)
        expected = hmac.new(session_token.encode(), f"voice:{identifier}:{expires}".encode(), hashlib.sha256).hexdigest()
        if expires < time.time() or expires > time.time() + 3660 or not hmac.compare_digest(signature, expected):
            return _error(403, "MEDIA_EXPIRED", "音色试听链接已过期，请刷新声音库")
        with Session(store.app_engine) as session:
            if identifier not in system_previews and not session.get(VoiceRow, identifier):
                raise KeyError("音色配置不存在")
        path = safe_path(store.root, f"voice-previews/{identifier}.wav")
        if not path.is_file() or path.is_symlink():
            raise KeyError("此音色没有本地试听音频")
        return FileResponse(path, media_type="audio/wav", content_disposition_type="inline")

    @app.get(f"{api}/media/{{project_id}}/{{asset_id}}")
    def media(project_id: str, asset_id: str, expires: int, signature: str, download: int = 0):
        message = f"{project_id}:{asset_id}:{expires}:{download}"
        expected = hmac.new(session_token.encode(), message.encode(), hashlib.sha256).hexdigest()
        if expires < time.time() or expires > time.time() + 3660 or not hmac.compare_digest(signature, expected):
            return _error(403, "MEDIA_EXPIRED", "素材播放链接已过期，请刷新项目")
        path, asset = store.asset_path(project_id, asset_id)
        if not path.is_file():
            raise KeyError("素材文件已丢失，请从备份恢复")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            filename=asset["name"] if download else None,
                            content_disposition_type="attachment" if download else "inline")

    return app


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


async def save_upload(file: UploadFile, destination: Path) -> None:
    count = 0
    with destination.open("xb") as output:
        while block := await file.read(1024 * 1024):
            count += len(block)
            if count > MAX_UPLOAD:
                raise ValueError("单个素材不能超过 2 GB，请先拆分")
            await asyncio.to_thread(output.write, block)
    if not count:
        raise ValueError("上传的文件为空")


async def validate_voice_bindings(store: Store, manager: JobManager, project_id: str, line_ids: list[str]) -> None:
    project = store.read_project(project_id)
    lines = {line["id"]: line for line in project["lines"]}
    library = store.list_rows(VoiceRow)
    checked: set[tuple[str, str, str]] = set()
    for line_id in dict.fromkeys(line_ids):
        line = lines.get(line_id)
        if not line:
            raise ValueError("所选台词不存在")
        settings = manager.resolve_settings(line["settings"])
        connection_id, model_id, voice_id = settings.get("connection_id"), settings["model_id"], settings["voice_id"]
        if not connection_id:
            raise ValueError("请先为所选台词选择百炼连接")
        config = manager.connection_config(connection_id, model_id, voice_id)
        # Run all local native-capability checks before sending any billable work.
        cloud.synthesis_input(config, line["text"], settings)
        key = (connection_id, model_id, voice_id)
        profiles = [voice for voice in library if voice["voice_id"] == voice_id and voice["kind"] != "system"]
        if profiles:
            matches = [voice for voice in profiles if voice.get("connection_id") == connection_id and voice["target_model"] == model_id]
            if not matches:
                raise ValueError("专属音色绑定了其他百炼连接或模型，请选择对应的连接和模型")
            profile = matches[0]
            if (config.region != "custom" and profile.get("region") is not None and profile["region"] != config.region) or (
                    config.workspace_id and profile.get("workspace_id") is not None and profile["workspace_id"] != config.workspace_id):
                raise ValueError("此连接的业务空间或地域已变化，不能继续使用原绑定音色；请恢复原连接或创建新音色")
            if profile["status"] != "OK":
                raise ValueError("专属音色尚未就绪，请在声音库刷新状态后再生成")
            if settings.get("ssml") and (profile["kind"] == "design" or not profile.get("ssml")):
                raise ValueError("当前专属音色不支持 SSML，请关闭该选项")
        # Verify custom remote identities against the active account/workspace.
        # This read-only request also catches a reused local connection whose
        # workspace or API key has changed since the voice was created.
        official = any(voice["voice_id"] == voice_id and voice["target_model"] == model_id for voice in cloud.SYSTEM_VOICES)
        is_remote_voice = profiles or (voice_id.startswith("qwen-audio-") and not official)
        if is_remote_voice and key not in checked:
            result = await cloud.query_voice(config, voice_id)
            if result.get("target_model") != model_id:
                raise ValueError("云端音色的目标模型与台词设置不一致")
            if result.get("status") != "OK":
                raise ValueError("云端音色当前不可用，请检查审核状态或重新创建")
            for profile in profiles:
                if profile.get("connection_id") == connection_id and profile["target_model"] == model_id:
                    profile.update(workspace_id=config.workspace_id, region=config.region)
                    with Session(store.app_engine) as session:
                        row = session.get(VoiceRow, profile["id"])
                        if row:
                            row.data = profile
                            session.commit()
            checked.add(key)


def _validated_state(state: Any, project_id: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("项目状态格式不正确")
    required = {"id", "name", "created_at", "updated_at", "lines", "roles", "clips"}
    if not required.issubset(state) or len(state["lines"]) > 10000 or len(state["clips"]) > 20000:
        raise ValueError("项目状态缺少必要字段或超过允许规模")
    lines = [Line.model_validate(value).model_dump(exclude={"versions"}) for value in state["lines"]]
    clips = [TimelineClip.model_validate(value).model_dump() for value in state["clips"]]
    roles = [Role.model_validate(value).model_dump() for value in state["roles"]]
    for line in lines:
        validate_id(line["id"])
    for clip in clips:
        validate_id(clip["id"])
        validate_id(clip["asset_id"])
    if len({line["id"] for line in lines}) != len(lines) or len({clip["id"] for clip in clips}) != len(clips):
        raise ValueError("项目包含重复的台词或片段标识")
    validated = {
        "id": project_id, "name": ProjectCreate(name=state["name"]).name,
        "created_at": str(state["created_at"])[:100], "updated_at": str(state["updated_at"])[:100],
        "lines": lines, "roles": roles, "clips": clips, "video_asset_id": state.get("video_asset_id"),
    }
    if validated["video_asset_id"] is not None:
        validate_id(validated["video_asset_id"])
    raw_positions = state.get("_auto_clip_positions", {})
    if not isinstance(raw_positions, dict):
        raise ValueError("项目时间轴布局记录格式不正确")
    clip_ids = {clip["id"] for clip in clips}
    positions = {}
    for identifier, value in raw_positions.items():
        if identifier not in clip_ids:
            continue
        if not isinstance(value, dict) or not all(isinstance(value.get(key), int) and value[key] >= 0 for key in ("start_sample", "source_length")):
            raise ValueError("项目时间轴布局记录格式不正确")
        positions[identifier] = {"start_sample": value["start_sample"], "source_length": value["source_length"]}
    if positions:
        validated["_auto_clip_positions"] = positions
    return validated


def restore_archive(store: Store, archive_path: Path) -> str:
    project_id = new_id()
    destination = store.project_dir(project_id)
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > 30000 or sum(entry.file_size for entry in entries) > 20 * 1024**3:
                raise ValueError("项目包超过允许大小")
            if len({entry.filename for entry in entries}) != len(entries):
                raise ValueError("项目包包含重复文件")
            for entry in entries:
                name = entry.filename
                if name != "project.sqlite" and not (name.startswith("assets/") and len(Path(name).parts) == 2):
                    raise ValueError("项目包包含不允许的文件")
                if "\\" in name or entry.is_dir():
                    raise ValueError("项目包包含不允许的路径")
                safe_path(destination, name)
                mode = (entry.external_attr >> 16) & 0o170000
                if mode not in {0, stat.S_IFREG}:
                    raise ValueError("项目包不能包含符号链接")
            archive.extractall(destination)
        database = destination / "project.sqlite"
        if not database.is_file():
            raise ValueError("项目包缺少数据库")
        # Disable schema triggers/views from imported databases and validate expected tables before ORM use.
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA trusted_schema=OFF")
            suspicious = connection.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall()
            if suspicious:
                raise ValueError("项目数据库包含不允许的结构")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in {1, 2}:
                raise ValueError("项目数据库版本不受支持，请使用对应版本的软件")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = set(ProjectBase.metadata.tables)
            if tables - {"sqlite_sequence"} != expected:
                raise ValueError("项目数据库表结构不正确")
            for name, table in ProjectBase.metadata.tables.items():
                columns = {row[1] for row in connection.execute(f'PRAGMA table_xinfo("{name}")')}
                if columns != set(table.columns.keys()):
                    raise ValueError("项目数据库字段结构不正确")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("项目数据库已损坏")
            raw = connection.execute("SELECT data FROM project_state WHERE id=1").fetchone()
            if not raw:
                raise ValueError("项目包缺少项目状态")
            state = _validated_state(json.loads(raw[0]), project_id)
            state["name"], state["updated_at"] = state["name"][:196] + "（恢复）", now()
            connection.execute("UPDATE project_state SET data=?, revision=revision+1 WHERE id=1", (json.dumps(state),))
            asset_ids = set()
            asset_records = {}
            for identifier, relative, data in connection.execute("SELECT id, relative_path, data FROM assets"):
                validate_id(identifier)
                asset = Asset.model_validate(json.loads(data))
                if asset.id != identifier or not relative.startswith("assets/") or len(Path(relative).parts) != 2 or "\\" in relative:
                    raise ValueError("项目素材记录格式不正确")
                safe_path(destination, relative)
                asset_ids.add(identifier)
                asset_records[identifier] = asset
                connection.execute("UPDATE assets SET data=? WHERE id=?",
                                   (json.dumps(asset.model_dump(exclude={"missing", "media_url"})), identifier))
            version_records = {}
            for identifier, line_id, data in connection.execute("SELECT id, line_id, data FROM audio_versions"):
                validate_id(identifier)
                validate_id(line_id)
                version = AudioVersion.model_validate(json.loads(data))
                if version.id != identifier or version.line_id != line_id or version.asset_id not in asset_ids:
                    raise ValueError("项目音频版本引用不正确")
                version.settings = LineSettings.model_validate(version.settings).model_dump()
                version_records[identifier] = version
                connection.execute("UPDATE audio_versions SET data=? WHERE id=?", (version.model_dump_json(), identifier))
            for identifier, before, after in connection.execute("SELECT id, before, after FROM edit_history").fetchall():
                validated_before = _validated_state(json.loads(before), project_id)
                validated_after = _validated_state(json.loads(after), project_id)
                connection.execute("UPDATE edit_history SET before=?, after=? WHERE id=?",
                                   (json.dumps(validated_before), json.dumps(validated_after), identifier))
            if any(clip["asset_id"] not in asset_ids for clip in state["clips"]):
                raise ValueError("项目时间轴引用了不存在的素材记录")
            for clip in state["clips"]:
                asset = asset_records[clip["asset_id"]]
                if asset.kind != "audio" or clip["source_end_sample"] > round(asset.duration_ms * SAMPLE_RATE / 1000) + SAMPLE_RATE // 100:
                    raise ValueError("项目时间轴裁剪范围超出音频素材")
            if state.get("video_asset_id") and (state["video_asset_id"] not in asset_records or asset_records[state["video_asset_id"]].kind != "video"):
                raise ValueError("项目关联的视频素材不存在")
            for line in state["lines"]:
                selected = line.get("selected_version_id")
                if selected and (selected not in version_records or version_records[selected].line_id != line["id"]):
                    raise ValueError("项目选择了不属于当前台词的音频版本")
            for identifier, line_id, status, data, snapshot in connection.execute(
                "SELECT id, line_id, status, data, snapshot FROM generation_jobs"
            ).fetchall():
                validate_id(identifier)
                validate_id(line_id)
                job = GenerationJob.model_validate(json.loads(data))
                if job.id != identifier or job.line_id != line_id or job.status != status:
                    raise ValueError("项目生成任务记录不一致")
                raw_snapshot = json.loads(snapshot)
                transport = raw_snapshot.pop("transport", "http")
                raw_connection = raw_snapshot.pop("connection", None)
                if transport not in {"http", "websocket"}:
                    raise ValueError("项目生成任务协议无效")
                parsed_snapshot = Line.model_validate(raw_snapshot).model_dump()
                if parsed_snapshot["id"] != line_id:
                    raise ValueError("项目任务台词快照不一致")
                parsed_snapshot["transport"] = transport
                if raw_connection is not None:
                    # Strict connection metadata rejects API keys in imported snapshots.
                    parsed_snapshot["connection"] = CloudConnection.model_validate(raw_connection).model_dump(exclude={"has_credential"})
                connection.execute("UPDATE generation_jobs SET data=?, snapshot=? WHERE id=?",
                                   (job.model_dump_json(), json.dumps(parsed_snapshot), identifier))
            # Export records are transient and their files are deliberately not
            # included by the project exporter. Never preserve foreign URLs.
            connection.execute("DELETE FROM exports")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        for folder in ("assets", "exports", "temp"):
            (destination / folder).mkdir(exist_ok=True)
        with Session(store.app_engine) as session:
            session.add(ProjectIndex(id=project_id, name=state["name"], created_at=state["created_at"], updated_at=state["updated_at"]))
            session.commit()
        store.recover(project_id)
        return project_id
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
