import { describe, expect, it } from "vitest";
import {
  clipDuration,
  reindexLines,
  secondsToSamples,
  splitText,
  timecode,
  defaultSettings,
  estimateGeneration,
  referenceUsageCost,
} from "./lib";
import type { CloudConnection, Line, Project, TimelineClip } from "./types";
import { mergeServerProject } from "./mergeProject";

describe("editor data helpers", () => {
  it("splits Chinese, English punctuation and lines without dropping closing punctuation", () => {
    expect(splitText("第一句。第二句！\nHello world! 还有一句？")).toEqual([
      "第一句。",
      "第二句！",
      "Hello world!",
      "还有一句？",
    ]);
  });
  it("keeps integer sample positions and calculates stretched duration", () => {
    const clip = {
      source_start_sample: 48000,
      source_end_sample: 192000,
      stretch: 1.5,
    } as TimelineClip;
    expect(secondsToSamples(0.123456)).toBe(5926);
    expect(clipDuration(clip)).toBe(2);
  });
  it("normalizes positions after a reorder", () => {
    expect(
      reindexLines([
        { id: "b", position: 8 },
        { id: "a", position: 2 },
      ] as Line[]).map((line) => line.position),
    ).toEqual([0, 1]);
  });
  it("formats invalid and extended times safely", () => {
    expect(timecode(Number.NaN)).toBe("00:00.000");
    expect(timecode(125.125)).toBe("02:05.125");
  });
  it("does not invent a cost for token or unverified models", () => {
    const connection = { id: "account", price_per_10k_chars: 2, input_price_per_million_tokens: 1, output_price_per_million_tokens: 3 } as CloudConnection;
    const line = { id: "line", text: "你好🙂", settings: { ...defaultSettings(), connection_id: "account" } } as Line;
    expect(estimateGeneration([line], [connection])).toEqual({ characters: 3, amount: 0.0006, unknown: 0 });
    expect(estimateGeneration([{ ...line, settings: { ...line.settings, model_id: "qwen-audio-3.1-tts-flash" } }], [connection]).unknown).toBe(1);
    expect(referenceUsageCost("qwen-audio-3.1-tts-flash", { input_tokens: 1000, output_tokens: 2000 }, connection)).toBe(0.007);
    expect(referenceUsageCost("custom-model", { characters: 100 }, connection)).toBeNull();
  });
  it("merges typing with a concurrent generated version and clip", () => {
    const line = { id: "line", text: "旧台词", revision: 1, position: 0, versions: [], settings: defaultSettings() } as Line;
    const base = { id: "project", name: "项目", revision: 1, lines: [line], roles: [], clips: [], assets: [] } as unknown as Project;
    const local = { ...base, lines: [{ ...line, text: "我正在输入的新台词" }] };
    const remote = { ...base, revision: 2, lines: [{ ...line, selected_version_id: "new-version", versions: [{ id: "new-version" }] }], clips: [{ id: "new-clip" }], assets: [{ id: "new-audio" }] } as unknown as Project;
    const merged = mergeServerProject(base, local, remote);
    expect(merged.lines[0].text).toBe("我正在输入的新台词");
    expect(merged.lines[0].versions[0].id).toBe("new-version");
    expect(merged.clips[0].id).toBe("new-clip");
    expect(merged.assets[0].id).toBe("new-audio");
    expect(merged.revision).toBe(2);
  });
  it("reports a genuine overlapping edit instead of discarding text", () => {
    const line = { id: "line", text: "原文", settings: defaultSettings() } as Line;
    const base = { id: "project", name: "项目", lines: [line], roles: [], clips: [], assets: [] } as unknown as Project;
    expect(() => mergeServerProject(base, { ...base, lines: [{ ...line, text: "本地修改" }] }, { ...base, lines: [{ ...line, text: "另一处修改" }] })).toThrow("当前编辑已保留");
  });
});
