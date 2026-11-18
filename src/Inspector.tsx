import {
  ChevronDown,
  Headphones,
  Play,
  Copy,
  SlidersHorizontal,
  Sparkles,
  UserRound,
} from "lucide-react";
import { useEffect, useState } from "react";
import type {
  Bootstrap,
  Line,
  LineSettings,
  Project,
  TimelineClip,
  VoiceProfile,
} from "./types";
import { SAMPLE_RATE, clipDuration } from "./lib";
import { Button, Field } from "./ui";
import { defaultConnection } from "./voiceLibrary";

export function Inspector({
  project,
  line,
  clip,
  bootstrap,
  selectedCount,
  onSettings,
  onLine,
  onClip,
  onCopyPrevious,
  onDirector,
  onBrowseVoices,
  onPreviewVoice,
}: {
  project: Project | null;
  line?: Line;
  clip?: TimelineClip;
  bootstrap?: Bootstrap;
  selectedCount: number;
  onSettings: (value: Partial<LineSettings>) => void;
  onLine: (value: Partial<Line>) => void;
  onClip: (value: Partial<TimelineClip>) => void;
  onCopyPrevious: () => void;
  onDirector: () => void;
  onBrowseVoices: () => void;
  onPreviewVoice: (voice: VoiceProfile) => Promise<void>;
}) {
  const [advanced, setAdvanced] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const preferred = defaultConnection(bootstrap?.connections ?? [], bootstrap?.preferences.default_connection_id);
  const [pauseText, setPauseText] = useState("");
  const [pronunciationText, setPronunciationText] = useState("");
  const [ssmlError, setSsmlError] = useState("");
  useEffect(() => {
    setPauseText(Object.entries(line?.settings.pause_after_words ?? {}).map(([word, delay]) => `${word}=${delay}`).join("\n"));
    setPronunciationText(Object.entries(line?.settings.pronunciation_overrides ?? {}).map(([word, pronunciation]) => `${word}=${pronunciation}`).join("\n"));
    setSsmlError("");
  }, [line?.id, line?.settings.pause_after_words, line?.settings.pronunciation_overrides]);
  const applySsmlMap = (text: string, pauses: boolean) => {
    try {
      const values: Record<string, string | number> = {};
      for (const entry of text.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
        const split = entry.indexOf("=");
        if (split < 1 || !entry.slice(split + 1).trim()) throw new Error("请每行填写“词语=设置值”。");
        const word = entry.slice(0, split).trim(), value = entry.slice(split + 1).trim();
        if (pauses) {
          const delay = Number(value);
          if (!Number.isInteger(delay) || delay < 50 || delay > 10000) throw new Error("停顿须为 50–10000 的整数毫秒。");
          values[word] = delay;
        } else values[word] = value;
      }
      onSettings(pauses ? { pause_after_words: values as Record<string, number> } : { pronunciation_overrides: values as Record<string, string> });
      setSsmlError("");
    } catch (error) { setSsmlError(error instanceof Error ? error.message : "SSML 设置格式错误"); }
  };
  const targetMs = clip ? project?.lines.find(item => item.id === clip.line_id)?.settings.target_duration_ms : null;
  const fitRate = clip && targetMs ? (clip.source_end_sample - clip.source_start_sample) / SAMPLE_RATE / (targetMs / 1000) : null;
  const model = bootstrap?.models.find(
    (value) => value.id === line?.settings.model_id,
  );
  const voice = bootstrap?.voices.find(
    (value) =>
      value.voice_id === line?.settings.voice_id &&
      value.target_model === line.settings.model_id,
  );
  const number = (
    name: keyof LineSettings,
    label: string,
    min: number,
    max: number,
    step: number,
    suffix = "",
  ) => (
    <Field label={label}>
      <div className="range-value">
        <input
          type="range"
          disabled={!model?.verified}
          min={min}
          max={max}
          step={step}
          value={Number(line?.settings[name] ?? min)}
          onChange={(event) =>
            onSettings({ [name]: Number(event.target.value) })
          }
        />
        <div className="number-unit">
          <input
            type="number"
            disabled={!model?.verified}
            min={min}
            max={max}
            step={step}
            value={Number(line?.settings[name] ?? min)}
            onChange={(event) => {
              if (event.target.value)
                onSettings({ [name]: Number(event.target.value) });
            }}
          />
          <span>{suffix}</span>
        </div>
      </div>
    </Field>
  );
  return (
    <aside className="inspector">
      <div className="panel-title">
        <span>
          <SlidersHorizontal size={15} />
          {clip ? "片段属性" : "配音属性"}
        </span>
        {selectedCount > 1 && <span className="badge">{selectedCount} 句</span>}
      </div>
      {clip ? (
        <div className="inspector-scroll">
          <section className="property-section">
            <h3>{clip.label || "音频片段"}</h3>
            <Field label="轨道">
              <select
                value={clip.track_id}
                onChange={(event) => onClip({ track_id: event.target.value })}
              >
                <option value="main">主音轨</option>
                {project?.roles.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}
                  </option>
                ))}
              </select>
            </Field>
            <div className="form-row">
              <Field label="开始位置（秒）">
                <input
                  type="number"
                  min={0}
                  step={0.01}
                  value={Number((clip.start_sample / SAMPLE_RATE).toFixed(3))}
                  onChange={(event) =>
                    onClip({
                      start_sample: Math.round(
                        Math.max(0, Number(event.target.value)) * SAMPLE_RATE,
                      ),
                    })
                  }
                />
              </Field>
              <Field label="时长（秒）">
                <input value={clipDuration(clip).toFixed(3)} readOnly />
              </Field>
            </div>
            <div className="form-row">
              <Field label="源入点（秒）">
                <input
                  type="number"
                  min={0}
                  step={0.01}
                  value={Number(
                    (clip.source_start_sample / SAMPLE_RATE).toFixed(3),
                  )}
                  onChange={(event) =>
                    onClip({
                      source_start_sample: Math.min(
                        clip.source_end_sample - 1,
                        Math.round(Number(event.target.value) * SAMPLE_RATE),
                      ),
                    })
                  }
                />
              </Field>
              <Field label="源出点（秒）">
                <input
                  type="number"
                  min={clip.source_start_sample / SAMPLE_RATE + 0.001}
                  step={0.01}
                  value={Number(
                    (clip.source_end_sample / SAMPLE_RATE).toFixed(3),
                  )}
                  onChange={(event) =>
                    onClip({
                      source_end_sample: Math.max(
                        clip.source_start_sample + 1,
                        Math.round(Number(event.target.value) * SAMPLE_RATE),
                      ),
                    })
                  }
                />
              </Field>
            </div>
            <Field label="片段增益">
              <div className="range-value">
                <input
                  type="range"
                  min={-60}
                  max={12}
                  step={0.5}
                  value={clip.gain_db}
                  onChange={(event) =>
                    onClip({ gain_db: Number(event.target.value) })
                  }
                />
                <span className="numeric-value">{clip.gain_db} dB</span>
              </div>
            </Field>
            <div className="form-row">
              <Field label="淡入（毫秒）">
                <input
                  type="number"
                  min={0}
                  value={clip.fade_in_ms}
                  onChange={(event) =>
                    onClip({ fade_in_ms: Number(event.target.value) })
                  }
                />
              </Field>
              <Field label="淡出（毫秒）">
                <input
                  type="number"
                  min={0}
                  value={clip.fade_out_ms}
                  onChange={(event) =>
                    onClip({ fade_out_ms: Number(event.target.value) })
                  }
                />
              </Field>
            </div>
            <Field label="播放速度">
              <input
                type="number"
                min={0.5}
                max={2}
                step={0.01}
                value={clip.stretch}
                onChange={(event) =>
                  onClip({
                    stretch: Math.max(
                      0.5,
                      Math.min(2, Number(event.target.value)),
                    ),
                  })
                }
              />
            </Field>
            {clip && targetMs && fitRate !== null && <div className="duration-fit">
              <p className="muted small">台词目标时长：{(targetMs / 1000).toFixed(2)} 秒</p>
              <Button disabled={fitRate < 0.5 || fitRate > 2} onClick={() => onClip({ stretch: fitRate })}>适配目标时长</Button>
              <p className="muted small">{fitRate < 0.5 || fitRate > 2 ? "所需速度超出 0.5–2 倍范围，请调整文本或重新生成。" : "保留音调调整片段速度；应用后可播放时间轴试听。"}</p>
            </div>}
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={clip.muted}
                onChange={(event) => onClip({ muted: event.target.checked })}
              />
              静音片段
            </label>
          </section>
        </div>
      ) : !line ? (
        <div className="inspector-empty">
          <UserRound size={26} />
          <span>选择一句台词</span>
        </div>
      ) : (
        <div className="inspector-scroll">
          <section className="property-section">
            <h3>声音与角色</h3>
            <Field label="角色">
              <select
                value={line.role_id ?? ""}
                onChange={(event) => {
                  const role = project?.roles.find(
                    (value) => value.id === event.target.value,
                  );
                  onLine({ role_id: event.target.value || null });
                  if (role)
                    onSettings({
                      ...(role.voice_id ? { voice_id: role.voice_id } : {}),
                      ...(role.model_id ? { model_id: role.model_id } : {}),
                      ...(role.connection_id
                        ? { connection_id: role.connection_id }
                        : {}),
                    });
                }}
              >
                <option value="">未分配角色</option>
                {project?.roles.map((role) => (
                  <option value={role.id} key={role.id}>
                    {role.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="生成连接" hint="保存一次后自动沿用；留空时使用默认连接。">
              <select value={line.settings.connection_id ?? ""}
                onChange={event => onSettings({ connection_id: event.target.value || null })}>
                <option value="">{preferred ? `默认 · ${preferred.name}` : "请先配置连接"}</option>
                {bootstrap?.connections.map(connection => <option key={connection.id} value={connection.id}>{connection.name}</option>)}
              </select>
            </Field>
            <Field label="配音声音">
              <select value={voice?.id ?? ""} onChange={event => {
                const chosen = bootstrap?.voices.find(item => item.id === event.target.value);
                if (chosen) onSettings({ voice_id: chosen.voice_id, model_id: chosen.target_model,
                  ...(chosen.connection_id ? { connection_id: chosen.connection_id } : {}) });
              }}>
                {!voice && <option value="">当前自定义音色</option>}
                {bootstrap?.voices.filter(item => item.target_model === line.settings.model_id && (!item.connection_id || item.connection_id === (line.settings.connection_id ?? preferred?.id)))
                  .map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
              </select>
            </Field>
            {voice && (
              <div className="voice-current">
                <div className="avatar">{voice.name.slice(0, 1)}</div>
                <span>
                  {voice.name}
                  <small>
                    {voice.kind === "clone"
                      ? "克隆音色"
                      : voice.kind === "design"
                        ? "设计音色"
                        : "系统音色"}
                  </small>
                </span>
                <span
                  className={`badge ${voice.status === "OK" ? "badge-teal" : ""}`}
                >
                  {voice.status === "OK" ? "可用" : voice.status}
                </span>
              </div>
            )}
            <div className="inspector-voice-actions">
              <Button onClick={onBrowseVoices}><Headphones size={13} />挑选声音</Button>
              <Button disabled={!voice} busy={previewing} onClick={async () => {
                if (!voice) return;
                setPreviewing(true); try { await onPreviewVoice(voice); } finally { setPreviewing(false); }
              }}><Play size={13} />试听</Button>
            </div>
            <Field label="语言">
              <select
                value={line.settings.language}
                onChange={(event) =>
                  onSettings({ language: event.target.value })
                }
              >
                {[
                  ["zh", "中文"],
                  ["en", "英语"],
                  ["ja", "日语"],
                  ["ko", "韩语"],
                  ["fr", "法语"],
                  ["de", "德语"],
                  ["es", "西班牙语"],
                  ["ru", "俄语"],
                  ["pt", "葡萄牙语"],
                  ["it", "意大利语"],
                  ["ar", "阿拉伯语"],
                ].map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </select>
            </Field>
          </section>
          <section className="property-section">
            <h3>声音表现</h3>
            <Field label="情绪">
              <select
                value={line.settings.emotion}
                disabled={!model?.instruction}
                onChange={(event) =>
                  onSettings({ emotion: event.target.value })
                }
              >
                {[
                  "自然",
                  "开心",
                  "悲伤",
                  "愤怒",
                  "紧张",
                  "惊讶",
                  "严肃",
                  "温柔",
                  "坚定",
                  "兴奋",
                ].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            {number("rate", "语速", 0.5, 2, 0.05, "×")}
            {number("pitch", "音调", 0.5, 2, 0.05, "×")}
            {number("volume", "合成音量", 0, 100, 1, "%")}
            <div className="form-row">
              <Field label="句前停顿（毫秒）">
                <input
                  type="number"
                  min={0}
                  max={10000}
                  step={50}
                  value={line.settings.pause_before_ms}
                  onChange={(event) =>
                    onSettings({
                      pause_before_ms: Math.max(0, Number(event.target.value)),
                    })
                  }
                />
              </Field>
              <Field label="句后停顿（毫秒）">
                <input
                  type="number"
                  min={0}
                  max={10000}
                  step={50}
                  value={line.settings.pause_after_ms}
                  onChange={(event) =>
                    onSettings({
                      pause_after_ms: Math.max(0, Number(event.target.value)),
                    })
                  }
                />
              </Field>
            </div>
            <Field label="目标时长（秒）">
              <input
                type="number"
                min={0}
                step={0.1}
                value={
                  line.settings.target_duration_ms
                    ? line.settings.target_duration_ms / 1000
                    : ""
                }
                placeholder="不限"
                onChange={(event) =>
                  onSettings({
                    target_duration_ms: event.target.value
                      ? Number(event.target.value) * 1000
                      : null,
                  })
                }
              />
            </Field>
          </section>
          <section className="property-section">
            <h3>
              导演要求 <Sparkles size={13} />
            </h3>
            <textarea
              rows={4}
              value={line.settings.instruction}
              disabled={!model?.instruction}
              onChange={(event) =>
                onSettings({ instruction: event.target.value })
              }
              placeholder="例如：开头温柔，结尾坚定有力"
            />
            <Button className="full-width" onClick={onDirector} disabled={!model?.instruction}>
              <Sparkles size={14} />
              编辑导演计划
            </Button>
          </section>
          <section className="property-section">
            <button
              className="section-disclosure"
              onClick={() => setAdvanced((value) => !value)}
            >
              高级参数
              <ChevronDown size={14} className={advanced ? "rotate" : ""} />
            </button>
            {advanced && (
              <>
                <Field label="模型 ID">
                  <input
                    list="inspector-models"
                    value={line.settings.model_id}
                    onChange={(event) => {
                      const id = event.target.value;
                      const verified = bootstrap?.models.find(value => value.id === id)?.verified;
                      const defaultVoice = bootstrap?.voices.find(value => value.target_model === id && value.kind === "system");
                      onSettings({ model_id: id, ...(defaultVoice ? { voice_id: defaultVoice.voice_id } : {}),
                        ...(!verified ? { rate: 1, pitch: 1, volume: 50, emotion: "自然", instruction: "", emphasis_words: [], ssml: false, seed: 0 } : {}) });
                    }}
                  />
                  <datalist id="inspector-models">
                    {bootstrap?.models.map((value) => (
                      <option key={value.id} value={value.id}>
                        {value.name}
                      </option>
                    ))}
                  </datalist>
                </Field>
                {!model?.verified && (
                  <div className="inline-warning">此模型尚未验证接口能力，高级生成参数已恢复默认；可先验证基础合成。</div>
                )}
                <Field label="重点词">
                  <input
                    value={line.settings.emphasis_words.join("，")}
                    disabled={!model?.instruction}
                    onChange={(event) =>
                      onSettings({
                        emphasis_words: event.target.value
                          .split(/[,，]/)
                          .map((value) => value.trim())
                          .filter(Boolean),
                      })
                    }
                    placeholder="用逗号分隔"
                  />
                </Field>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={line.settings.ssml}
                    disabled={!model?.ssml || !voice?.ssml}
                    onChange={(event) =>
                      onSettings({ ssml: event.target.checked })
                    }
                  />
                  启用 SSML
                </label>
                {(!model?.ssml || !voice?.ssml) && (
                  <small className="muted">当前模型或音色未确认支持 SSML</small>
                )}
                <Field label="词后停顿" hint="每行：词语=毫秒，例如“欢迎=300”。同词每次出现均生效。">
                  <textarea rows={3} value={pauseText} disabled={!line.settings.ssml || !model?.ssml || !voice?.ssml}
                    onChange={event => setPauseText(event.target.value)} onBlur={event => applySsmlMap(event.target.value, true)} placeholder="欢迎=300" />
                </Field>
                <Field label="读音纠正" hint="每行：汉字词语=带调拼音。拼音数量须与字数一致。">
                  <textarea rows={3} value={pronunciationText} disabled={!line.settings.ssml || !model?.ssml || !voice?.ssml}
                    onChange={event => setPronunciationText(event.target.value)} onBlur={event => applySsmlMap(event.target.value, false)} placeholder="重庆=chong2 qing4" />
                </Field>
                {ssmlError && <p className="error-text small">{ssmlError} 本次修改尚未保存。</p>}
                <Field label="随机种子">
                  <input
                    type="number"
                    min={0}
                    max={65535}
                    disabled={!model?.verified}
                    value={line.settings.seed}
                    onChange={(event) =>
                      onSettings({ seed: Number(event.target.value) })
                    }
                  />
                </Field>
              </>
            )}
            <Button
              className="full-width"
              onClick={onCopyPrevious}
              disabled={
                !project ||
                project.lines.findIndex((value) => value.id === line.id) < 1
              }
            >
              <Copy size={14} />
              沿用上一句参数
            </Button>
          </section>
        </div>
      )}
    </aside>
  );
}
