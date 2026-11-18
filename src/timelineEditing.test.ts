import { describe, expect, it } from "vitest";
import type { TimelineClip } from "./types";
import { MAX_TIMELINE_SAMPLE, clipEnd, duplicateClips, fadeClip, moveClips, snapEdge, snapMove, splitClip, trimClip } from "./timelineEditing";

function clip(values: Partial<TimelineClip> = {}): TimelineClip {
  return { id: "a", asset_id: "source", track_id: "main", label: "测试", start_sample: 48000,
    source_start_sample: 24000, source_end_sample: 168000, stretch: 1.5, gain_db: 0,
    fade_in_ms: 100, fade_out_ms: 100, muted: false, ...values };
}

describe("non-destructive timeline editing", () => {
  it("moves a group against time zero without collapsing relative timing or tracks", () => {
    const originals = [clip(), clip({ id: "b", start_sample: 144000, track_id: "voice" })];
    const moved = moveClips(originals, -100000, ["main", "voice", "music"], 2);
    expect(moved.map(item => [item.start_sample, item.track_id])).toEqual([[0, "voice"], [96000, "music"]]);
    expect(originals[0].start_sample).toBe(48000);
    expect(moved.every(item => item.asset_id === "source" && item.source_end_sample === 168000)).toBe(true);
  });
  it("snaps either edge of any selected clip while preserving group spacing", () => {
    const originals = [clip({ start_sample: 0 }), clip({ id: "b", start_sample: 192000 })];
    const result = snapMove(originals, 47999, [336000], 100);
    expect(result).toEqual({ delta: 48000, guide: 336000 });
    expect(moveClips(originals, result.delta, ["main"])[1].start_sample).toBe(240000);
  });
  it("does not snap outside a pixel-scaled tolerance or beyond timeline zero", () => {
    expect(snapMove([clip()], 0, [49000], 10).guide).toBeNull();
    expect(snapMove([clip({ start_sample: 0 })], -50, [-1], 10)).toEqual({ delta: 0, guide: null });
    expect(snapEdge(95998, [96000, 48000], 10)).toEqual({ position: 96000, guide: 96000 });
  });
  it("trims the left edge with stretch while anchoring the right edge", () => {
    const original = clip();
    const trimmed = trimClip(original, "start", 12345, 192000);
    expect(trimmed.start_sample).toBe(60345);
    expect(trimmed.source_start_sample).toBe(42518);
    expect(Math.abs(clipEnd(trimmed) - clipEnd(original))).toBeLessThanOrEqual(1);
    expect(original.source_start_sample).toBe(24000);
  });
  it("prevents left expansion before time zero rather than discarding source alignment", () => {
    const original = clip({ start_sample: 1000 });
    const trimmed = trimClip(original, "start", -100000, 192000);
    expect(trimmed.start_sample).toBe(0);
    expect(trimmed.source_start_sample).toBe(22500);
    expect(clipEnd(trimmed)).toBe(clipEnd(original));
  });
  it("bounds right trims by source duration and retains a valid minimum", () => {
    const original = clip();
    expect(trimClip(original, "end", 500000, 192000).source_end_sample).toBe(192000);
    const shortened = trimClip(original, "end", -500000, 192000);
    expect(shortened.source_end_sample - shortened.source_start_sample).toBe(1440);
    expect(shortened.fade_in_ms).toBe(20);
    expect(shortened.fade_out_ms).toBe(20);
  });
  it("keeps short imported clips valid and never manufactures unavailable samples", () => {
    const tiny = clip({ source_start_sample: 0, source_end_sample: 300, stretch: 1 });
    expect(trimClip(tiny, "start", 99999, 300).source_end_sample).toBe(300);
    expect(trimClip(tiny, "start", 99999, 300).source_start_sample).toBe(0);
    expect(trimClip(tiny, "end", 99999, 300).source_end_sample).toBe(300);
    expect(trimClip(clip({ start_sample: MAX_TIMELINE_SAMPLE }), "start", 48000, 192000).start_sample).toBe(MAX_TIMELINE_SAMPLE);
  });
  it("updates fades in output milliseconds and clamps to duration and API bounds", () => {
    expect(fadeClip(clip(), "in", 48000).fade_in_ms).toBe(1100);
    expect(fadeClip(clip(), "out", -48000).fade_out_ms).toBe(1100);
    expect(fadeClip(clip(), "in", 9999999).fade_in_ms).toBe(2000);
    expect(fadeClip(clip(), "out", 9999999).fade_out_ms).toBe(0);
  });
  it("splits a stretched clip without holes in its source and only keeps exterior fades", () => {
    const original = clip({ fade_in_ms: 1800, fade_out_ms: 1800 });
    const parts = splitClip(original, 96000, "second");
    expect(parts.map(item => [item.id, item.start_sample, item.source_start_sample, item.source_end_sample])).toEqual([
      ["a", 48000, 24000, 96000], ["second", 96000, 96000, 168000],
    ]);
    expect(parts.map(item => [item.fade_in_ms, item.fade_out_ms])).toEqual([[1000, 0], [0, 1000]]);
    expect(clipEnd(parts[1])).toBe(clipEnd(original));
    expect(splitClip(original, original.start_sample, "unused")).toEqual([original]);
  });
  it("duplicates references after the group without mutating source or group spacing", () => {
    const originals = [clip({ start_sample: 0 }), clip({ id: "b", start_sample: 144000, track_id: "voice" })];
    let counter = 0;
    const copies = duplicateClips(originals, () => `copy-${++counter}`);
    expect(copies.map(item => item.start_sample)).toEqual([240000, 384000]);
    expect(copies.map(item => item.id)).toEqual(["copy-1", "copy-2"]);
    expect(copies.every(item => item.asset_id === "source")).toBe(true);
    expect(originals[0].id).toBe("a");
  });
});
