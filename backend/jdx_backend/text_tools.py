from __future__ import annotations

import re
from typing import Any

import pysubs2

from .schemas import Line, LineSettings


def split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[。！？!?；;])(?:[ \t]*|(?=\S))|(?<=[.])\s+|\r?\n+", text)
    return [piece.strip() for piece in pieces if piece.strip()]


def import_text(data: bytes, suffix: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("无法识别文本编码，请保存为 UTF-8 后导入") from exc
    if suffix.lower() in {".srt", ".vtt"}:
        subtitles = pysubs2.SSAFile.from_string(text, format_="srt" if suffix.lower() == ".srt" else "vtt")
        return [Line(text=event.plaintext, position=index, subtitle_start_ms=event.start,
                     subtitle_end_ms=event.end, settings=LineSettings(target_duration_ms=max(1, event.end-event.start))).model_dump()
                for index, event in enumerate(subtitles) if event.plaintext.strip()]
    return [Line(text=value, position=index).model_dump() for index, value in enumerate(split_sentences(text))]


def director_plan(text: str, instruction: str) -> dict[str, Any]:
    pieces = split_sentences(text)
    if not pieces:
        raise ValueError("请先输入需要配音的台词")
    styles = [style for style in ["温柔", "坚定", "严肃", "开心", "悲伤", "愤怒", "紧张", "惊讶", "轻快", "缓慢"] if style in instruction]
    pauses = re.search(r"停顿\s*(\d+(?:\.\d+)?)\s*(秒|毫秒)", instruction)
    pause = min(10000, int(float(pauses[1]) * (1000 if pauses[2] == "秒" else 1))) if pauses else (400 if "停顿" in instruction else 200)
    warnings = ["这是可编辑的规则解析方案；情绪表达由模型生成，不保证逐字精确。"]
    segments = []
    for index, piece in enumerate(pieces):
        style = instruction
        if len(styles) > 1 and len(pieces) > 1:
            style = f"请用{styles[min(len(styles)-1, index*len(styles)//len(pieces))]}的语气表达。{instruction}"
        segments.append({"text": piece, "instruction": style, "pause_after_ms": pause})
    if len(pieces) == 1 and any(word in instruction for word in ["前面", "中间", "最后"]):
        warnings.append("当前只有一个分段；可在计划中手动拆句，为不同位置设置不同表达。")
    return {"segments": segments, "warnings": warnings}
