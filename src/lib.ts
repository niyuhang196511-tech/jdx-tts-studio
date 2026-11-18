import type {
  CloudConnection,
  Line,
  LineSettings,
  TimelineClip,
} from "./types";

export const SAMPLE_RATE = 48000;
export const uid = () => crypto.randomUUID();
export const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value));
export const secondsToSamples = (seconds: number) =>
  Math.max(0, Math.round(seconds * SAMPLE_RATE));
export const clipDuration = (clip: TimelineClip) =>
  Math.max(
    0,
    (clip.source_end_sample - clip.source_start_sample) /
      SAMPLE_RATE /
      clip.stretch,
  );
export const timecode = (seconds: number) => {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  return `${Math.floor(safe / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor(safe % 60)
    .toString()
    .padStart(2, "0")}.${Math.floor((safe % 1) * 1000)
    .toString()
    .padStart(3, "0")}`;
};
export const formatDate = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
export const defaultSettings = (
  connection?: CloudConnection,
): LineSettings => ({
  connection_id: connection?.id,
  model_id: connection?.model_id ?? "qwen-audio-3.1-tts-plus",
  voice_id: connection?.voice_id ?? "longanhuan_v3.1",
  language: "zh",
  emotion: "自然",
  instruction: "",
  rate: 1,
  pitch: 1,
  volume: 50,
  pause_before_ms: 0,
  pause_after_ms: 200,
  target_duration_ms: null,
  emphasis_words: [],
  ssml: false,
  pause_after_words: {},
  pronunciation_overrides: {},
  seed: 0,
});
export const newLine = (
  text: string,
  position: number,
  connection?: CloudConnection,
): Line => ({
  id: uid(),
  text,
  position,
  revision: 1,
  settings: defaultSettings(connection),
  versions: [],
});
export const splitText = (text: string) =>
  (text.match(/[^。！？!?；;\n]+[。！？!?；;]?/g) ?? [])
    .flatMap(value => value.split(/(?<=\.)\s+(?=\S)/))
    .map((value) => value.trim())
    .filter(Boolean);
export const reindexLines = (lines: Line[]) =>
  lines.map((line, position) => ({ ...line, position }));
export const JOB_LABELS: Record<string, string> = {
  queued: "等待中",
  running: "生成中",
  cancel_requested: "正在取消",
  cancelled: "已取消",
  succeeded: "已完成",
  failed: "生成失败",
  uncertain: "结果待确认",
  interrupted: "已中断",
};

export const tokenPricedModel = (modelId: string) => modelId === "qwen-audio-3.1-tts-flash";
const characterPricedModels = new Set(["qwen-audio-3.1-tts-plus", "qwen-audio-3.0-tts-plus", "qwen-audio-3.0-tts-flash"]);
export function estimateGeneration(lines: Line[], connections: CloudConnection[]) {
  let amount = 0;
  let unknown = 0;
  let characters = 0;
  for (const line of lines) {
    if (!line.text.trim()) continue;
    const count = Array.from(line.text).length;
    characters += count;
    const connection = connections.find(item => item.id === line.settings.connection_id);
    const price = connection?.price_per_10k_chars;
    if (!characterPricedModels.has(line.settings.model_id) || price == null) unknown++;
    else amount += count / 10000 * price;
  }
  return { characters, amount, unknown };
}
export function referenceUsageCost(modelId: string, usage: Record<string, unknown>, connection?: CloudConnection): number | null {
  if (!connection) return null;
  if (tokenPricedModel(modelId)) {
    if (typeof usage.input_tokens !== "number" || typeof usage.output_tokens !== "number" ||
        connection.input_price_per_million_tokens == null || connection.output_price_per_million_tokens == null) return null;
    return (usage.input_tokens * connection.input_price_per_million_tokens + usage.output_tokens * connection.output_price_per_million_tokens) / 1000000;
  }
  if (!characterPricedModels.has(modelId) || typeof usage.characters !== "number" || connection.price_per_10k_chars == null) return null;
  return usage.characters / 10000 * connection.price_per_10k_chars;
}
