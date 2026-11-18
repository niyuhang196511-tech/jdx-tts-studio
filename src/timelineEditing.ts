import type { TimelineClip } from "./types";
import { clamp, SAMPLE_RATE } from "./lib";

export const MAX_TIMELINE_SAMPLE = SAMPLE_RATE * 86400;
export const clipSamples = (clip: TimelineClip) => Math.round((clip.source_end_sample - clip.source_start_sample) / clip.stretch);
export const clipEnd = (clip: TimelineClip) => clip.start_sample + clipSamples(clip);

export function moveClips(clips: TimelineClip[], delta: number, tracks: string[], trackDelta = 0): TimelineClip[] {
  if (!clips.length) return [];
  const safeDelta = clamp(Math.round(delta), -Math.min(...clips.map(clip => clip.start_sample)),
    MAX_TIMELINE_SAMPLE - Math.max(...clips.map(clip => clip.start_sample)));
  const indexes = clips.map(clip => tracks.indexOf(clip.track_id));
  const offset = clamp(Math.round(trackDelta), -Math.min(...indexes), tracks.length - 1 - Math.max(...indexes));
  return clips.map((clip, index) => ({ ...clip, start_sample: clip.start_sample + safeDelta,
    track_id: tracks[indexes[index] + offset] ?? clip.track_id }));
}

export function snapMove(clips: TimelineClip[], delta: number, targets: number[], tolerance: number) {
  if (!clips.length) return { delta: 0, guide: null };
  let best = tolerance + 1;
  let correction = 0;
  let guide: number | null = null;
  const min = Math.min(...clips.map(clip => clip.start_sample));
  const max = Math.max(...clips.map(clip => clip.start_sample));
  const safe = clamp(Math.round(delta), -min, MAX_TIMELINE_SAMPLE - max);
  for (const clip of clips) for (const edge of [clip.start_sample, clipEnd(clip)]) {
    for (const target of targets) {
      const difference = target - edge - safe;
      if (Math.abs(difference) <= tolerance && Math.abs(difference) < best && min + safe + difference >= 0 && max + safe + difference <= MAX_TIMELINE_SAMPLE) {
        best = Math.abs(difference);
        correction = difference;
        guide = target;
      }
    }
  }
  return { delta: safe + correction, guide };
}

export function snapEdge(position: number, targets: number[], tolerance: number) {
  let snapped = Math.round(position), guide: number | null = null, best = tolerance + 1;
  for (const target of targets) if (Math.abs(target - position) <= tolerance && Math.abs(target - position) < best) {
    best = Math.abs(target - position); snapped = target; guide = target;
  }
  return { position: snapped, guide };
}

function constrainFades(clip: TimelineClip): TimelineClip {
  const duration = Math.max(0, Math.floor(clipSamples(clip) / SAMPLE_RATE * 1000));
  return { ...clip, fade_in_ms: Math.min(clip.fade_in_ms, duration), fade_out_ms: Math.min(clip.fade_out_ms, duration) };
}

export function trimClip(clip: TimelineClip, edge: "start" | "end", delta: number, maxSource: number): TimelineClip {
  // A short imported clip must remain editable, even below the usual 20 ms minimum.
  const minimum = Math.min(Math.max(1, Math.round(SAMPLE_RATE * 0.02 * clip.stretch)), clip.source_end_sample - clip.source_start_sample);
  if (edge === "start") {
    const change = clamp(Math.round(delta * clip.stretch),
      Math.max(-clip.source_start_sample, -Math.floor(clip.start_sample * clip.stretch)),
      Math.min(clip.source_end_sample - clip.source_start_sample - minimum,
        Math.floor((MAX_TIMELINE_SAMPLE - clip.start_sample) * clip.stretch)));
    return constrainFades({ ...clip, source_start_sample: clip.source_start_sample + change,
      start_sample: clip.start_sample + Math.round(change / clip.stretch) });
  }
  return constrainFades({ ...clip, source_end_sample: clamp(clip.source_end_sample + Math.round(delta * clip.stretch),
    clip.source_start_sample + minimum, Math.max(clip.source_end_sample, maxSource)) });
}

export function fadeClip(clip: TimelineClip, edge: "in" | "out", delta: number): TimelineClip {
  const field = edge === "in" ? "fade_in_ms" : "fade_out_ms";
  return { ...clip, [field]: clamp(Math.round(clip[field] + delta / SAMPLE_RATE * 1000 * (edge === "in" ? 1 : -1)),
    0, Math.min(30000, Math.floor(clipSamples(clip) / SAMPLE_RATE * 1000))) };
}

export function splitClip(clip: TimelineClip, position: number, nextId: string): TimelineClip[] {
  const source = clip.source_start_sample + Math.round((position - clip.start_sample) * clip.stretch);
  if (source <= clip.source_start_sample || source >= clip.source_end_sample) return [clip];
  return [constrainFades({ ...clip, source_end_sample: source, fade_out_ms: 0 }),
    constrainFades({ ...clip, id: nextId, source_start_sample: source, start_sample: position, fade_in_ms: 0 })];
}

export function duplicateClips(clips: TimelineClip[], nextId: () => string): TimelineClip[] {
  if (!clips.length) return [];
  const start = Math.min(...clips.map(clip => clip.start_sample));
  const end = Math.max(...clips.map(clipEnd));
  const offset = end - start;
  // Preserve relative spacing. Reject as a group rather than partially duplicating at the project limit.
  if (clips.some(clip => clip.start_sample + offset > MAX_TIMELINE_SAMPLE)) return [];
  return clips.map(clip => ({ ...clip, id: nextId(), start_sample: clip.start_sample + offset }));
}
