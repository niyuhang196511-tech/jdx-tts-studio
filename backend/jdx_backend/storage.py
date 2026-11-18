from __future__ import annotations

import copy
import json
import shutil
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import JSON, Column, Integer, String, Text, create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session

from .schemas import new_id, now


class AppBase(DeclarativeBase):
    pass


class ProjectBase(DeclarativeBase):
    pass


class SettingRow(AppBase):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(JSON, nullable=False)


class ConnectionRow(AppBase):
    __tablename__ = "connections"
    id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False)


class VoiceRow(AppBase):
    __tablename__ = "voices"
    id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False)


class ProjectIndex(AppBase):
    __tablename__ = "projects"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class ProjectTrash(AppBase):
    __tablename__ = "project_trash"
    id = Column(String, primary_key=True)
    deleted_at = Column(String, nullable=False)


class StateRow(ProjectBase):
    __tablename__ = "project_state"
    id = Column(Integer, primary_key=True, default=1)
    revision = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)


class HistoryRow(ProjectBase):
    __tablename__ = "edit_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    before = Column(JSON, nullable=False)
    after = Column(JSON, nullable=False)
    applied = Column(Integer, nullable=False, default=1)


class AssetRow(ProjectBase):
    __tablename__ = "assets"
    id = Column(String, primary_key=True)
    relative_path = Column(String, nullable=False, unique=True)
    data = Column(JSON, nullable=False)


class VersionRow(ProjectBase):
    __tablename__ = "audio_versions"
    id = Column(String, primary_key=True)
    line_id = Column(String, nullable=False, index=True)
    data = Column(JSON, nullable=False)


class JobRow(ProjectBase):
    __tablename__ = "generation_jobs"
    id = Column(String, primary_key=True)
    line_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    fingerprint = Column(String, nullable=False, index=True)
    data = Column(JSON, nullable=False)
    snapshot = Column(JSON, nullable=False)


class ExportRow(ProjectBase):
    __tablename__ = "exports"
    id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False)


def validate_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("无效的资源标识") from exc


def safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("不允许访问项目目录之外的文件")
    return path


def make_engine(path: Path, base: type[DeclarativeBase]) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def set_pragmas(connection: Any, _: Any) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA trusted_schema=OFF")

    migrate(engine, path, base)
    return engine


def migrate(engine: Engine, path: Path, base: type[DeclarativeBase]) -> None:
    target_version = 3 if base is AppBase else 2
    with engine.connect() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
    if version > target_version:
        raise ValueError("数据库版本较新，请升级软件后打开")
    if 0 < version < target_version:
        import sqlite3

        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(path.with_suffix(".pre-migration.sqlite"))) as target:
            source.backup(target)
    base.metadata.create_all(engine)
    with engine.begin() as connection:
        if base is ProjectBase:
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_generation_jobs_status ON generation_jobs(status)"))
        connection.execute(text(f"PRAGMA user_version={target_version}"))


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.app_engine = make_engine(self.root / "studio.sqlite", AppBase)
        self._engines: dict[str, Engine] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._operations: dict[str, int] = {}
        self._map_lock = threading.RLock()

    def project_dir(self, project_id: str) -> Path:
        return self.root / "projects" / validate_id(project_id)

    def _assert_active(self, project_id: str, *, include_trashed: bool = False) -> None:
        with Session(self.app_engine) as session:
            if session.get(ProjectIndex, project_id) is None:
                raise KeyError("项目不存在")
            if not include_trashed and session.get(ProjectTrash, project_id) is not None:
                raise ProjectTrashedError("项目已移入回收站，请先恢复后再使用")

    @contextmanager
    def project_lock(self, project_id: str) -> Iterator[None]:
        project_id = validate_id(project_id)
        with self._map_lock:
            lock = self._locks.setdefault(project_id, threading.RLock())
        with lock:
            yield

    @contextmanager
    def project_operation(self, project_id: str) -> Iterator[None]:
        """Reserve a long import/relink without holding a lock across await."""
        project_id = validate_id(project_id)
        with self.project_lock(project_id):
            self._assert_active(project_id)
            self._operations[project_id] = self._operations.get(project_id, 0) + 1
        try:
            yield
        finally:
            with self.project_lock(project_id):
                remaining = self._operations.get(project_id, 1) - 1
                if remaining:
                    self._operations[project_id] = remaining
                else:
                    self._operations.pop(project_id, None)

    def engine(self, project_id: str, *, include_trashed: bool = False) -> Engine:
        project_id = validate_id(project_id)
        self._assert_active(project_id, include_trashed=include_trashed)
        path = self.project_dir(project_id) / "project.sqlite"
        if not path.is_file():
            raise KeyError("项目数据库文件不存在，请从备份恢复")
        with self._map_lock:
            if project_id not in self._engines:
                self._engines[project_id] = make_engine(path, ProjectBase)
            return self._engines[project_id]

    @contextmanager
    def project_session(self, project_id: str) -> Iterator[Session]:
        with self.project_lock(project_id), Session(self.engine(project_id)) as session:
            yield session
            session.commit()

    def create_project(self, name: str) -> str:
        project_id, timestamp = new_id(), now()
        directory = self.project_dir(project_id)
        (directory / "assets").mkdir(parents=True)
        (directory / "exports").mkdir()
        (directory / "temp").mkdir()
        engine = make_engine(directory / "project.sqlite", ProjectBase)
        self._engines[project_id] = engine
        state = {"id": project_id, "name": name, "created_at": timestamp, "updated_at": timestamp,
                 "lines": [], "roles": [{"id": "narrator", "name": "旁白", "color": "#56c9b4"}],
                 "clips": [], "video_asset_id": None}
        with Session(engine) as session:
            session.add(StateRow(id=1, revision=1, data=state))
            session.commit()
        with Session(self.app_engine) as session:
            session.add(ProjectIndex(id=project_id, name=name, created_at=timestamp, updated_at=timestamp))
            session.commit()
        return project_id

    def list_projects(self) -> list[dict[str, Any]]:
        with Session(self.app_engine) as session:
            return [{"id": row.id, "name": row.name, "created_at": row.created_at, "updated_at": row.updated_at}
                    for row in session.scalars(select(ProjectIndex).where(
                        ProjectIndex.id.not_in(select(ProjectTrash.id))
                    ).order_by(ProjectIndex.updated_at.desc()))]

    def list_trash(self) -> list[dict[str, Any]]:
        with Session(self.app_engine) as session:
            statement = select(ProjectIndex, ProjectTrash).join(ProjectTrash, ProjectIndex.id == ProjectTrash.id)
            return [{"id": project.id, "name": project.name, "created_at": project.created_at,
                     "updated_at": project.updated_at, "deleted_at": trash.deleted_at}
                    for project, trash in session.execute(statement.order_by(ProjectTrash.deleted_at.desc()))]

    def trash_project(self, project_id: str, revision: int) -> dict[str, Any]:
        """Hide a project atomically while keeping its database and media intact."""
        with self.project_session(project_id) as session:
            row = session.get(StateRow, 1)
            if row is None:
                raise KeyError("项目数据库缺少状态")
            if row.revision != revision:
                raise ConflictError("项目已更新，请刷新并确认后再移入回收站")
            pending_job = session.scalar(select(JobRow.id).where(
                JobRow.status.in_(["queued", "running", "cancel_requested"])
            ).limit(1))
            pending_export = any(item.data.get("status") in {"queued", "running"}
                                 for item in session.scalars(select(ExportRow)))
            if pending_job or pending_export or self._operations.get(project_id, 0):
                raise ProjectBusyError("项目正在生成、导出或导入素材，请等待完成或取消任务后再删除")
            deleted_at = now()
            with Session(self.app_engine) as app_session:
                app_session.add(ProjectTrash(id=project_id, deleted_at=deleted_at))
                app_session.commit()
            return {"id": project_id, "name": row.data["name"], "deleted_at": deleted_at}

    def restore_project(self, project_id: str) -> None:
        project_id = validate_id(project_id)
        with self.project_lock(project_id):
            self._assert_active(project_id, include_trashed=True)
            with Session(self.app_engine) as session:
                trash = session.get(ProjectTrash, project_id)
                if trash is None:
                    raise KeyError("该项目不在回收站中")
                # Verify a usable database before changing its visible state.
                with Session(self.engine(project_id, include_trashed=True)) as project_session:
                    if project_session.get(StateRow, 1) is None:
                        raise KeyError("项目数据库缺少状态，请从备份恢复")
                session.delete(trash)
                session.commit()

    def read_project(self, project_id: str) -> dict[str, Any]:
        with self.project_session(project_id) as session:
            row = session.get(StateRow, 1)
            if row is None:
                raise KeyError("项目数据库缺少状态")
            result = copy.deepcopy(row.data)
            result.pop("_auto_clip_positions", None)
            result["revision"] = row.revision
            versions: dict[str, list[dict[str, Any]]] = {}
            for version in session.scalars(select(VersionRow)):
                versions.setdefault(version.line_id, []).append(copy.deepcopy(version.data))
            for line in result["lines"]:
                line["versions"] = sorted(versions.get(line["id"], []), key=lambda item: item["created_at"])
            result["assets"] = [dict(asset.data, missing=not safe_path(self.project_dir(project_id), asset.relative_path).is_file())
                                for asset in session.scalars(select(AssetRow))]
            result["can_undo"] = session.scalar(select(HistoryRow.id).where(HistoryRow.applied == 1).limit(1)) is not None
            result["can_redo"] = session.scalar(select(HistoryRow.id).where(HistoryRow.applied == 0).limit(1)) is not None
            return result

    def save_state(self, project_id: str, changes: dict[str, Any], revision: int | None = None,
                   record_history: bool = True) -> None:
        with self.project_session(project_id) as session:
            row = session.get(StateRow, 1)
            assert row is not None
            if revision is not None and row.revision != revision:
                raise ConflictError("项目已更新，请刷新后重试；当前修改尚未写入")
            before = copy.deepcopy(row.data)
            after = copy.deepcopy(before)
            after.update(copy.deepcopy(changes))
            if record_history:
                for obsolete in session.scalars(select(HistoryRow).where(HistoryRow.applied == 0)):
                    session.delete(obsolete)
                session.add(HistoryRow(before=before, after=after, applied=1))
            after["updated_at"] = now()
            row.data, row.revision = after, row.revision + 1
        self.update_index(project_id)

    def update_index(self, project_id: str) -> None:
        with self.project_session(project_id) as session:
            row = session.get(StateRow, 1)
            assert row is not None
            name, updated_at = row.data["name"], row.data["updated_at"]
        with Session(self.app_engine) as session:
            index = session.get(ProjectIndex, project_id)
            if index:
                index.name, index.updated_at = name, updated_at
                session.commit()

    def history(self, project_id: str, redo: bool) -> None:
        with self.project_session(project_id) as session:
            statement = select(HistoryRow).where(HistoryRow.applied == (0 if redo else 1))
            statement = statement.order_by(HistoryRow.id.asc() if redo else HistoryRow.id.desc()).limit(1)
            change = session.scalar(statement)
            if change is None:
                return
            row = session.get(StateRow, 1)
            assert row is not None
            data = copy.deepcopy(change.after if redo else change.before)
            data["updated_at"] = now()
            row.data, row.revision = data, row.revision + 1
            change.applied = 1 if redo else 0
        self.update_index(project_id)

    def asset_path(self, project_id: str, asset_id: str) -> tuple[Path, dict[str, Any]]:
        validate_id(asset_id)
        with self.project_session(project_id) as session:
            row = session.get(AssetRow, asset_id)
            if not row:
                raise KeyError("素材不存在")
            return safe_path(self.project_dir(project_id), row.relative_path), copy.deepcopy(row.data)

    def list_rows(self, kind: type[ConnectionRow] | type[VoiceRow]) -> list[dict[str, Any]]:
        with Session(self.app_engine) as session:
            return [copy.deepcopy(row.data) for row in session.scalars(select(kind))]

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        from .cloud import normalize_connection
        with Session(self.app_engine) as session:
            row = session.get(ConnectionRow, connection_id)
            if not row:
                raise KeyError("请先配置百炼连接")
            return normalize_connection(copy.deepcopy(row.data))

    def default_connection(self, credential_ids: set[str] | None = None) -> dict[str, Any] | None:
        """A saved choice wins, so we never silently switch billable accounts."""
        rows = self.list_rows(ConnectionRow)
        with Session(self.app_engine) as session:
            preference = session.get(SettingRow, "preferences")
            preferred = preference.value.get("default_connection_id") if preference else None
        selected = next((row for row in rows if row["id"] == preferred), None)
        if not selected:
            selected = next((row for row in rows if row["id"] in (credential_ids or set())), rows[0] if rows else None)
        return self.get_connection(selected["id"]) if selected else None

    def recover(self, project_id: str | None = None) -> int:
        interrupted = 0
        # Import recovery must not interrupt jobs running in unrelated projects.
        projects = [{"id": validate_id(project_id)}] if project_id else self.list_projects()
        for project in projects:
            try:
                with self.project_session(project["id"]) as session:
                    for row in session.scalars(select(JobRow).where(JobRow.status.in_(["queued", "running", "cancel_requested"]))):
                        row.status = "interrupted"
                        row.data = dict(row.data, status="interrupted", error="上次运行意外中断，请确认后重新生成")
                        interrupted += 1
                    for row in session.scalars(select(ExportRow)):
                        if row.data.get("status") in {"queued", "running"}:
                            row.data = dict(row.data, status="failed", error="上次导出意外中断，请重新导出")
            except (KeyError, ValueError, ProjectTrashedError):
                continue
        return interrupted

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self.app_engine.dispose()


class ConflictError(Exception):
    pass


class ProjectTrashedError(Exception):
    pass


class ProjectBusyError(Exception):
    pass
