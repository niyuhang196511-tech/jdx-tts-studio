from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloudConnection(StrictModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(default="百炼北京", min_length=1, max_length=100)
    workspace_id: str = ""
    region: Literal["cn-beijing", "ap-southeast-1", "custom"] = "cn-beijing"
    base_url: str = ""
    model_id: str = "qwen-audio-3.1-tts-plus"
    voice_id: str = "longanhuan_v3.1"
    has_credential: bool = False
    max_concurrency: int = Field(default=2, ge=1, le=8)
    requests_per_minute: int = Field(default=120, ge=1, le=600)
    price_per_10k_chars: float | None = Field(default=None, ge=0, le=10000)
    input_price_per_million_tokens: float | None = Field(default=None, ge=0, le=100000)
    output_price_per_million_tokens: float | None = Field(default=None, ge=0, le=100000)


class ConnectionInput(CloudConnection):
    api_key: str | None = Field(default=None, exclude=True, max_length=4096)


class ModelProfile(StrictModel):
    id: str
    name: str
    description: str
    instruction: bool = False
    ssml: bool = False
    streaming: bool = False
    custom_voice: bool = False
    verified: bool = False


class VoiceProfile(StrictModel):
    id: str = Field(default_factory=new_id)
    name: str
    kind: Literal["system", "clone", "design"] = "system"
    connection_id: str | None = None
    target_model: str = "qwen-audio-3.1-tts-plus"
    voice_id: str
    status: str = "OK"
    notes: str = ""
    authorized: bool = False
    ssml: bool = False
    instruction: bool = False
    word_timestamps: bool = False
    favorite: bool = False
    workspace_id: str | None = None
    region: str | None = None
    preview_url: str | None = None
    preview_available: bool = False
    preview_source: Literal["official", "design", "reference"] | None = None
    gender: str = ""
    languages: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    category: str = ""
    regions: list[str] = Field(default_factory=list)
    source_url: str | None = None


class LineSettings(StrictModel):
    connection_id: str | None = None
    model_id: str = "qwen-audio-3.1-tts-plus"
    voice_id: str = "longanhuan_v3.1"
    language: str = "zh"
    emotion: str = "自然"
    instruction: str = Field(default="", max_length=4000)
    rate: float = Field(default=1, ge=0.5, le=2)
    pitch: float = Field(default=1, ge=0.5, le=2)
    volume: int = Field(default=50, ge=0, le=100)
    pause_before_ms: int = Field(default=0, ge=0, le=30000)
    pause_after_ms: int = Field(default=200, ge=0, le=30000)
    target_duration_ms: int | None = Field(default=None, gt=0, le=3600000)
    emphasis_words: list[str] = Field(default_factory=list)
    ssml: bool = False
    seed: int = Field(default=0, ge=0, le=65535)
    pause_after_words: dict[str, int] = Field(default_factory=dict, max_length=100)
    pronunciation_overrides: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("pause_after_words")
    @classmethod
    def valid_pauses(cls, values: dict[str, int]) -> dict[str, int]:
        if any(not word.strip() or len(word) > 100 or not 50 <= duration <= 10000 for word, duration in values.items()):
            raise ValueError("句内停顿须指定词语和 50–10000 毫秒时长")
        return values

    @field_validator("pronunciation_overrides")
    @classmethod
    def valid_pronunciations(cls, values: dict[str, str]) -> dict[str, str]:
        import re
        for word, pinyin in values.items():
            if not word.strip() or len(word) > 100 or not re.fullmatch(r"[a-zA-ZüÜvV]+[1-5](?: [a-zA-ZüÜvV]+[1-5])*", pinyin):
                raise ValueError("发音请使用带声调的拼音，以空格分隔，例如 chong2 qing4")
            if len(pinyin.split()) != len(word):
                raise ValueError("拼音数量必须与汉字数量一致")
        return values


class AudioVersion(StrictModel):
    id: str
    line_id: str
    asset_id: str
    duration_ms: int
    created_at: str
    settings: dict[str, Any]
    model_id: str
    request_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    best: bool = False
    text_revision: int


class Line(StrictModel):
    id: str = Field(default_factory=new_id)
    text: str = Field(default="", max_length=20000)
    position: int = 0
    revision: int = 1
    role_id: str | None = None
    settings: LineSettings = Field(default_factory=LineSettings)
    selected_version_id: str | None = None
    subtitle_start_ms: int | None = Field(default=None, ge=0)
    subtitle_end_ms: int | None = Field(default=None, ge=0)
    versions: list[AudioVersion] = Field(default_factory=list)


class Role(StrictModel):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=100)
    color: str = "#56c9b4"
    voice_id: str | None = None
    connection_id: str | None = None
    model_id: str | None = None


class TimelineClip(StrictModel):
    id: str = Field(default_factory=new_id)
    asset_id: str
    line_id: str | None = None
    track_id: str = "narrator"
    label: str = "音频片段"
    start_sample: int = Field(default=0, ge=0, le=48000 * 86400)
    source_start_sample: int = Field(default=0, ge=0)
    source_end_sample: int = Field(ge=1)
    gain_db: float = Field(default=0, ge=-60, le=24)
    fade_in_ms: int = Field(default=0, ge=0, le=30000)
    fade_out_ms: int = Field(default=0, ge=0, le=30000)
    muted: bool = False
    stretch: float = Field(default=1, ge=0.5, le=2)

    @field_validator("source_end_sample")
    @classmethod
    def valid_end(cls, value: int, info: Any) -> int:
        if value <= info.data.get("source_start_sample", 0):
            raise ValueError("片段结束位置必须晚于开始位置")
        return value


class Asset(StrictModel):
    id: str
    name: str
    kind: Literal["audio", "video", "export"]
    duration_ms: int = 0
    sample_rate: int = 48000
    channels: int = 1
    missing: bool = False
    media_url: str = ""


class Project(StrictModel):
    id: str
    name: str
    revision: int
    created_at: str
    updated_at: str
    lines: list[Line]
    roles: list[Role]
    clips: list[TimelineClip]
    assets: list[Asset]
    video_asset_id: str | None = None
    can_undo: bool = False
    can_redo: bool = False


class ProjectCreate(StrictModel):
    name: str = Field(default="未命名配音项目", min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value


class ProjectDelete(StrictModel):
    revision: int = Field(ge=1)


class TrashedProject(StrictModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    deleted_at: str


class ProjectDeleted(StrictModel):
    id: str
    name: str
    deleted_at: str


class ProjectPatch(StrictModel):
    revision: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    lines: list[Line] | None = Field(default=None, max_length=10000)
    clips: list[TimelineClip] | None = Field(default=None, max_length=20000)
    roles: list[Role] | None = None
    video_asset_id: str | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("项目名称不能为空")
        return value.strip()


class TaskInput(StrictModel):
    line_ids: list[str] = Field(min_length=1, max_length=1000)
    transport: Literal["http", "websocket"] = "http"


class GenerationJob(StrictModel):
    id: str
    line_id: str
    status: Literal["queued", "running", "cancel_requested", "cancelled", "succeeded", "failed", "uncertain", "interrupted"]
    progress: float
    error: str | None = None
    created_at: str
    request_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None


class ExportInput(StrictModel):
    format: Literal["wav", "mp3", "csv", "sentences", "project"] = "wav"
    sample_rate: Literal[8000, 16000, 22050, 24000, 44100, 48000, 96000] = 48000
    bit_depth: Literal[16, 24, 32] = 24
    normalize: bool = True
    filename: str = Field(default="", max_length=200)


class DirectorInput(StrictModel):
    text: str = Field(max_length=20000)
    instruction: str = Field(default="", max_length=4000)


class VoiceDesignInput(StrictModel):
    connection_id: str
    target_model: str
    name: str = Field(min_length=1, max_length=100)
    voice_prompt: str = Field(min_length=1, max_length=500)
    preview_text: str = Field(min_length=15, max_length=200)
