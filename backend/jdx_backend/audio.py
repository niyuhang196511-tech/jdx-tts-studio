from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

SAMPLE_RATE = 48000
INPUT_GUARD = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "wav,mp3,flac,ogg,mov,aac,aiff,matroska,avi"]


def ffmpeg_path() -> str:
    roots = [Path(sys.executable).parent, Path(getattr(sys, "_MEIPASS", Path(__file__).parent))]
    for root in roots:
        for candidate in (root / "ffmpeg", root / "ffmpeg.exe", root / "bin" / "ffmpeg", root / "bin" / "ffmpeg.exe"):
            if candidate.is_file():
                return str(candidate)
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("缺少音频处理组件 FFmpeg，请重新安装完整应用") from exc


async def run_ffmpeg(arguments: list[str], timeout: float = 600) -> bytes:
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        # FFmpeg sees only private staged paths; don't put user media contents in logs.
        reason = stderr.decode("utf-8", errors="replace")[-1600:]
        raise ValueError(f"媒体处理失败：{reason}")
    return stdout


async def normalize_import(source: Path, destination: Path) -> dict[str, Any]:
    await run_ffmpeg([*INPUT_GUARD, "-i", str(source), "-vn", "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s24le", "-n", str(destination)])
    return await asyncio.to_thread(audio_info, destination)


def audio_info(path: Path) -> dict[str, Any]:
    info = sf.info(path)
    if info.frames <= 0 or info.duration > 86400:
        raise ValueError("音频为空或超过 24 小时，请拆分后导入")
    return {"duration_ms": round(info.duration * 1000), "sample_rate": info.samplerate, "channels": info.channels}


async def video_proxy(source: Path, destination: Path) -> dict[str, Any]:
    # A standard proxy provides consistent behavior across WebView2 and WebKit/GStreamer.
    await run_ffmpeg([*INPUT_GUARD, "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", "23", "-pix_fmt", "yuv420p", "-vf", "scale='min(1920,iw)':-2",
                      "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-n", str(destination)], 3600)
    # ffprobe is used if bundled; otherwise inspect the proxy with FFmpeg, never trust upload metadata.
    probe = shutil.which("ffprobe")
    adjacent = Path(ffmpeg_path()).parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    if adjacent.is_file():
        probe = str(adjacent)
    if probe:
        process = await asyncio.create_subprocess_exec(probe, "-v", "error", "-show_format", "-of", "json", str(destination),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await process.communicate()
        metadata = json.loads(stdout)
        duration = float(metadata.get("format", {}).get("duration", 0))
    else:
        process = await asyncio.create_subprocess_exec(ffmpeg_path(), "-hide_banner", "-i", str(destination),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr.decode(errors="replace"))
        duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]) if match else 0
    if duration <= 0:
        raise ValueError("无法识别视频时长")
    return {"duration_ms": round(duration * 1000), "sample_rate": SAMPLE_RATE, "channels": 2}


def clip_duration_samples(clip: dict[str, Any]) -> int:
    return max(1, round((clip["source_end_sample"] - clip["source_start_sample"]) / clip.get("stretch", 1)))


async def render_timeline(
    clips: list[dict[str, Any]], asset_paths: dict[str, Path], destination: Path,
    *, output_format: str = "wav", sample_rate: int = SAMPLE_RATE, bit_depth: int = 24, normalize: bool = True,
) -> None:
    active = [clip for clip in clips if not clip.get("muted")]
    if not active:
        raise ValueError("时间轴没有可导出的音频片段")
    # Render in bounded batches, never open 500 files in one subprocess or allocate an entire project in RAM.
    total_frames = max(clip["start_sample"] + clip_duration_samples(clip) for clip in active)
    if total_frames > SAMPLE_RATE * 86400:
        raise ValueError("单次导出不能超过 24 小时")
    with tempfile.TemporaryDirectory(prefix="jdx-render-", dir=destination.parent) as temporary:
        root = Path(temporary)
        mixed = root / "mix.wav"
        with sf.SoundFile(mixed, "w", samplerate=SAMPLE_RATE, channels=2, subtype="FLOAT") as writer:
            silence = np.zeros((SAMPLE_RATE * 10, 2), dtype=np.float32)
            for position in range(0, total_frames, len(silence)):
                writer.write(silence[:min(len(silence), total_frames - position)])
                await asyncio.sleep(0)
        for index, clip in enumerate(active):
            source = asset_paths.get(clip["asset_id"])
            if not source or not source.is_file():
                raise ValueError(f"素材缺失：{clip.get('label', clip['asset_id'])}")
            rendered = root / f"clip-{index}.wav"
            seconds = clip_duration_samples(clip) / SAMPLE_RATE
            filters = [
                f"atrim=start={clip['source_start_sample']/SAMPLE_RATE:.9f}:end={clip['source_end_sample']/SAMPLE_RATE:.9f}",
                "asetpts=PTS-STARTPTS",
                f"volume={clip.get('gain_db', 0):.6f}dB",
            ]
            # Even at tempo=1 FFmpeg's WSOLA window can shift transients. Do
            # not apply a time-stretch processor when playback speed is normal.
            if clip.get("stretch", 1) != 1:
                filters.insert(2, f"atempo={clip['stretch']:.9f}")
            for mode in ("in", "out"):
                duration = min(clip.get(f"fade_{mode}_ms", 0) / 1000, seconds)
                if duration:
                    filters.append(f"afade=t={mode}:st={0 if mode == 'in' else seconds-duration:.9f}:d={duration:.9f}")
            await run_ffmpeg(["-i", str(source), "-af", ",".join(filters), "-ar", str(SAMPLE_RATE), "-ac", "2",
                              "-c:a", "pcm_f32le", "-n", str(rendered)])
            await asyncio.to_thread(_mix_clip, mixed, rendered, clip["start_sample"], total_frames)
            rendered.unlink(missing_ok=True)
        arguments = ["-i", str(mixed)]
        # The renderer is shared by preview and export, including limiting/normalization.
        if normalize:
            measured = await _loudness(mixed)
            if measured:
                arguments += ["-af", measured]
            else:
                arguments += ["-af", "alimiter=limit=0.891251:level=false:latency=true"]
        else:
            arguments += ["-af", "alimiter=limit=0.891251:level=false:latency=true"]
        codec = f"pcm_s{bit_depth}le" if output_format == "wav" else "libmp3lame"
        arguments += ["-ar", str(sample_rate), "-c:a", codec]
        if output_format == "mp3":
            arguments += ["-b:a", "192k"]
        arguments += ["-n", str(destination)]
        await run_ffmpeg(arguments, 3600)


def _mix_clip(mixed: Path, source: Path, start: int, limit: int) -> None:
    with sf.SoundFile(mixed, "r+") as target, sf.SoundFile(source) as audio:
        target.seek(start)
        position = start
        while position < limit:
            block = audio.read(min(SAMPLE_RATE * 5, limit - position), dtype="float32", always_2d=True)
            if not len(block):
                break
            existing = target.read(len(block), dtype="float32", always_2d=True)
            target.seek(position)
            target.write(existing + block)
            position += len(block)


async def _loudness(source: Path) -> str | None:
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-hide_banner", "-nostdin", "-i", str(source), "-af",
        "loudnorm=I=-16:TP=-1:LRA=11:print_format=json", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 3600)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    text = stderr.decode(errors="replace")
    match = re.search(r'\{\s*"input_i".*?\}', text, re.S)
    if not match:
        return None
    measured = json.loads(match[0])
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(math.isfinite(float(measured[key])) for key in keys):
        return None
    return ("loudnorm=I=-16:TP=-1:LRA=11:linear=true:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}")
