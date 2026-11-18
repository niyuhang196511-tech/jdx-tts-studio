import { useEffect, useState } from "react";
import { AudioLines, Mic, Palette, Upload } from "lucide-react";
import { post } from "./api";
import type { CloudConnection, ModelProfile, VoiceProfile } from "./types";
import { Button, Field, Modal } from "./ui";
import { defaultConnection } from "./voiceLibrary";

export function VoiceModal({
  open,
  onClose,
  connections,
  voices,
  models,
  defaultConnectionId,
  onSaved,
  notify,
}: {
  open: boolean;
  onClose: () => void;
  connections: CloudConnection[];
  voices: VoiceProfile[];
  models: ModelProfile[];
  defaultConnectionId?: string;
  onSaved: () => void;
  notify: (message: string, error?: boolean) => void;
}) {
  const preferred = defaultConnection(connections, defaultConnectionId);
  const [mode, setMode] = useState<"clone" | "design" | "system">("system");
  const [manual, setManual] = useState(false);
  const [name, setName] = useState("");
  const [connectionId, setConnectionId] = useState(preferred?.id ?? "");
  const [model, setModel] = useState(
    preferred?.model_id ?? "qwen-audio-3.1-tts-plus",
  );
  const [voiceId, setVoiceId] = useState(preferred?.voice_id ?? "longanhuan_v3.1");
  const availableVoices = voices.filter(voice => voice.kind === "system" && voice.target_model === model && voice.id.startsWith("system:"));
  const selectedVoice = availableVoices.find(voice => voice.voice_id === voiceId);
  const [prompt, setPrompt] = useState("");
  const [preview, setPreview] = useState(
    "欢迎来到我们的故事，一起听见每一个精彩瞬间。",
  );
  const [file, setFile] = useState<File | null>(null);
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [referenceUrl, setReferenceUrl] = useState("");
  useEffect(() => {
    if (!file) {
      setReferenceUrl("");
      return;
    }
    const url = URL.createObjectURL(file);
    setReferenceUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const save = async () => {
    const displayName = name.trim() || (mode === "system" ? selectedVoice?.name : "");
    if (!displayName) {
      notify("请填写音色名称。", true);
      return;
    }
    if (mode !== "system" && !connectionId) {
      notify("请先配置百炼连接。", true);
      return;
    }
    setBusy(true);
    try {
      if (mode === "clone") {
        if (!file || !authorized)
          throw new Error("请上传参考录音并确认已获得声音使用授权。");
        const form = new FormData();
        form.append("file", file);
        form.append("connection_id", connectionId);
        form.append("target_model", model);
        form.append("name", displayName);
        form.append("authorized", "true");
        await post<VoiceProfile>("/voices/clone", form);
      } else if (mode === "design") {
        if (!prompt.trim()) throw new Error("请填写音色描述。");
        await post<VoiceProfile>("/voices/design", {
          connection_id: connectionId,
          target_model: model,
          name: displayName,
          voice_prompt: prompt,
          preview_text: preview,
        });
      } else {
        if (!voiceId.trim()) throw new Error("请选择声音。");
        await post<VoiceProfile>("/voices", {
          name: displayName,
          kind: "system",
          connection_id: connectionId || null,
          target_model: model,
          voice_id: voiceId,
          status: "OK",
          notes: prompt,
          authorized: false,
          ssml: selectedVoice?.ssml ?? false,
        });
      }
      onSaved();
      onClose();
      notify(
        mode === "system"
          ? "音色配置已保存"
          : "音色已提交，可在声音库刷新处理状态",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "创建音色失败", true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open={open} onClose={() => { if (!busy) onClose(); }} title="创建声音" description="选用官方声音，或创建获得授权的专属音色">
      <div className="segmented voice-modes">
        {(
          [
            ["system", "官方声音", AudioLines],
            ["clone", "声音克隆", Mic],
            ["design", "音色设计", Palette],
          ] as const
        ).map(([value, label, Icon]) => (
          <button
            key={value}
            className={mode === value ? "active" : ""}
            disabled={busy}
            onClick={() => setMode(value)}
          >
            <Icon size={15} />
            {label}
          </button>
        ))}
      </div>
      <Field label="音色名称">
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={selectedVoice && mode === "system" ? selectedVoice.name : "例如：纪录片旁白"}
        />
      </Field>
      <div className="form-row">
        <Field label="百炼连接">
          <select
            value={connectionId}
            onChange={(event) => {
              setConnectionId(event.target.value);
              const selected = connections.find(item => item.id === event.target.value);
              setVoiceId(selected?.voice_id ?? "");
              setModel(
                connections.find((item) => item.id === event.target.value)
                  ?.model_id ?? model,
              );
            }}
          >
            <option value="">选择连接</option>
            {connections.map((connection) => (
              <option value={connection.id} key={connection.id}>
                {connection.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="目标模型">
          <select value={model} onChange={event => {
            setModel(event.target.value);
            setVoiceId(voices.find(voice => voice.id.startsWith("system:") && voice.target_model === event.target.value)?.voice_id ?? "");
          }}>
            {models.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            {!models.some(item => item.id === model) && <option value={model}>{model}</option>}
          </select>
        </Field>
      </div>
      {mode === "clone" ? (
        <>
          <Field label="参考录音" hint="5–60 秒、10 MB 以内，建议连续 10–20 秒清晰人声。上传前会检查并转换格式。">
            <label className="upload-zone">
              <Upload size={22} />
              <span>{file?.name ?? "选择参考音频"}</span>
              <input
                type="file"
                accept="audio/*"
                onChange={(event) => {
                  const selected = event.target.files?.[0] ?? null;
                  if (selected && selected.size > 10 * 1024 * 1024) {
                    notify("参考录音不能超过 10 MB。", true);
                    event.target.value = "";
                    return;
                  }
                  setFile(selected);
                }}
              />
            </label>
          </Field>
          {referenceUrl && (
            <audio controls src={referenceUrl} className="reference-player" />
          )}
          <p className="muted small">百炼声音复刻直接使用参考录音，无需填写参考文本。</p>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={authorized}
              onChange={(event) => setAuthorized(event.target.checked)}
            />
            我已获得此声音用于克隆和配音的有效授权
          </label>
        </>
      ) : mode === "design" ? (
        <>
          <Field label="音色描述">
            <textarea
              rows={4}
              maxLength={500}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="描述声音的性别、年龄、音色和表达特点"
            />
          </Field>
          <Field label="试听台词">
            <textarea
              rows={2}
              minLength={15}
              maxLength={200}
              value={preview}
              onChange={(event) => setPreview(event.target.value)}
            />
          </Field>
        </>
      ) : (
        <>
          <Field label="选择官方声音" hint="按名称选择即可，兼容的模型和音色 ID 由应用处理。">
            <select value={voiceId} onChange={event => setVoiceId(event.target.value)} disabled={manual}>
              {availableVoices.map(voice => <option key={voice.id} value={voice.voice_id}>{voice.name}</option>)}
              {!availableVoices.some(voice => voice.voice_id === voiceId) && <option value={voiceId}>{voiceId ? "自定义声音" : "此模型暂无内置声音"}</option>}
            </select>
          </Field>
          <button className="text-btn" onClick={() => setManual(value => !value)}>{manual ? "收起自定义音色设置" : "高级：添加目录外音色"}</button>
          {manual && <Field label="自定义音色 ID" hint="仅在官方目录未收录时使用，须与当前模型兼容。">
            <input value={voiceId} onChange={event => setVoiceId(event.target.value)} />
          </Field>}
          <Field label="备注">
            <textarea
              rows={2}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
            />
          </Field>
        </>
      )}
      <div className="modal-actions">
        <Button disabled={busy} onClick={onClose}>取消</Button>
        <Button busy={busy} variant="primary" onClick={() => void save()}>
          {mode === "system" ? "保存音色" : "创建音色"}
        </Button>
      </div>
    </Modal>
  );
}
