import { useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Cloud, KeyRound, Plus, Save, ShieldCheck, Star, Trash2, Wifi } from "lucide-react";
import { api, deleteCredential, isDesktop, patch, post, saveCredential } from "./api";
import type { CloudConnection, ModelProfile, VoiceProfile } from "./types";
import { Button, Field, IconButton, Modal, StatusDot } from "./ui";
import "./connection.css";

const emptyConnection: Partial<CloudConnection> = {
  name: "我的百炼连接", base_url: "", model_id: "qwen-audio-3.1-tts-plus",
  voice_id: "longanhuan_v3.1", max_concurrency: 2,
};

// Keep existing installations editable before their next server-side save.
function editableConnection(value: Partial<CloudConnection>): Partial<CloudConnection> {
  return { ...value, base_url: value.base_url || (value.workspace_id
    ? `https://${value.workspace_id}.${value.region || "cn-beijing"}.maas.aliyuncs.com` : "") };
}

function serviceAddress(value: string): string {
  let parsed: URL;
  try { parsed = new URL(value.trim()); } catch { throw new Error("请填写完整的 HTTPS 服务地址，例如百炼控制台提供的 Base URL。"); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash || !["", "/"].includes(parsed.pathname)) {
    throw new Error("请填写 HTTPS 服务根地址，不要附带 API Key、查询参数或合成接口路径。");
  }
  return parsed.origin;
}

function addressLabel(connection: Partial<CloudConnection>): string {
  try { return new URL(editableConnection(connection).base_url || "").hostname; }
  catch { return "尚未填写服务地址"; }
}

export function ConnectionModal({ open, onClose, connections, models, voices = [], defaultConnectionId,
  onSaved, notify }: {
  open: boolean;
  onClose: () => void;
  connections: CloudConnection[];
  models: ModelProfile[];
  voices?: VoiceProfile[];
  defaultConnectionId?: string | null;
  onSaved: () => void;
  notify: (message: string, error?: boolean) => void;
}) {
  const initial = editableConnection(connections.find(item => item.id === defaultConnectionId) ?? connections[0] ?? emptyConnection);
  const [draft, setDraft] = useState<Partial<CloudConnection>>(initial);
  const [savedDraft, setSavedDraft] = useState<Partial<CloudConnection>>(initial);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [customModel, setCustomModel] = useState(!models.some(item => item.id === initial.model_id));
  const [activeDefault, setActiveDefault] = useState(defaultConnectionId ?? null);
  const initialDefault = !connections.length || initial.id === defaultConnectionId;
  const [makeDefault, setMakeDefault] = useState(initialDefault);
  const [savedMakeDefault, setSavedMakeDefault] = useState(initialDefault);
  const [formError, setFormError] = useState("");
  const [modelHint, setModelHint] = useState("");
  const [testResult, setTestResult] = useState("");
  useEffect(() => { setActiveDefault(defaultConnectionId ?? null); }, [defaultConnectionId]);
  const unsaved = !!key || JSON.stringify(draft) !== JSON.stringify(savedDraft) || makeDefault !== savedMakeDefault;
  const isDefault = !!draft.id && draft.id === activeDefault;
  const compatibleVoices = useMemo(() => {
    const unique = new Map<string, VoiceProfile>();
    voices.filter(item => item.target_model === draft.model_id && (!item.connection_id || item.connection_id === draft.id))
      .forEach(item => { if (!unique.has(item.voice_id)) unique.set(item.voice_id, item); });
    return [...unique.values()];
  }, [voices, draft.id, draft.model_id]);
  const knownVoice = compatibleVoices.some(item => item.voice_id === draft.voice_id);
  const field = <K extends keyof CloudConnection>(name: K, value: CloudConnection[K]) => {
    setDraft(current => ({ ...current, [name]: value }));
    setFormError("");
    setTestResult("");
  };
  const canLeave = () => !busy && (!unsaved || confirm("连接配置尚未保存，放弃这些修改？"));
  const selectConnection = (connection: Partial<CloudConnection>) => {
    if (!canLeave()) return;
    const value = editableConnection(connection);
    setDraft(value); setSavedDraft(value); setKey(""); setFormError(""); setTestResult(""); setModelHint("");
    const preferred = !connections.length || !!value.id && value.id === activeDefault;
    setMakeDefault(preferred); setSavedMakeDefault(preferred);
    setCustomModel(!models.some(item => item.id === value.model_id));
  };
  const changeModel = (modelId: string) => {
    if (modelId === "__custom__") { setCustomModel(true); setAdvanced(true); return; }
    setCustomModel(false);
    const available = voices.filter(item => item.target_model === modelId && (!item.connection_id || item.connection_id === draft.id));
    const previousCompatible = available.some(item => item.voice_id === draft.voice_id);
    const nextVoice = previousCompatible ? draft.voice_id : available.find(item => item.status === "OK")?.voice_id;
    setDraft(current => ({ ...current, model_id: modelId, voice_id: nextVoice ?? current.voice_id }));
    setModelHint(previousCompatible ? "" : nextVoice
      ? `已切换为该模型的兼容音色：${available.find(item => item.voice_id === nextVoice)?.name}。保存后生效。`
      : "当前目录没有此模型的可用音色，请在高级设置核对音色 ID 后保存。");
    setFormError(""); setTestResult("");
  };
  const save = async () => {
    setFormError("");
    if (!draft.name?.trim() || !draft.base_url?.trim()) {
      setFormError("请填写连接名称和服务地址。"); return;
    }
    if (!draft.model_id?.trim() || !draft.voice_id?.trim()) {
      setFormError("请选择默认模型和音色。"); return;
    }
    let baseUrl: string;
    try { baseUrl = serviceAddress(draft.base_url); }
    catch (error) { setFormError(error instanceof Error ? error.message : "服务地址无效。"); return; }
    setBusy(true);
    try {
      // Workspace and region are derived from the address by the backend.
      const { workspace_id: _workspace, region: _region, ...configuration } = draft;
      void _workspace; void _region;
      const payload = { ...configuration, name: draft.name.trim(), base_url: baseUrl,
        model_id: draft.model_id.trim(), voice_id: draft.voice_id.trim(), ...(!isDesktop() && key ? { api_key: key } : {}) };
      let saved = draft.id
        ? await patch<CloudConnection>(`/connections/${draft.id}`, payload)
        : await post<CloudConnection>("/connections", payload);
      // Retain the saved ID even when a later credential-store operation fails.
      setDraft(saved); setSavedDraft(saved);
      if (isDesktop() && key) {
        const result = await saveCredential(saved.id, key);
        saved = { ...saved, has_credential: result?.configured ?? true };
        setDraft(saved); setSavedDraft(saved);
        if (result?.storage === "session") notify(result.message ?? "密钥仅在本次会话使用，退出后需重新输入。");
      }
      setKey("");
      if (makeDefault) {
        await patch("/preferences", { default_connection_id: saved.id });
        setActiveDefault(saved.id);
      }
      setSavedMakeDefault(makeDefault); setModelHint(""); setTestResult("");
      onSaved();
      notify(makeDefault ? "连接已保存，新台词将自动使用这个连接" : "连接已保存");
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "保存失败，请重试。");
      onSaved();
    } finally { setBusy(false); }
  };
  const test = async () => {
    if (!draft.id || unsaved) return;
    setBusy(true); setFormError(""); setTestResult("");
    try {
      const result = await post<{ message?: string }>(`/connections/${draft.id}/test`);
      setTestResult(result.message ?? "连接可用，生成时会进一步验证模型和音色权限。");
    } catch (error) { setFormError(error instanceof Error ? error.message : "连接失败，请检查服务地址和 API Key。"); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!draft.id || busy || !confirm("删除此连接配置？已生成的音频会保留。")) return;
    setBusy(true); setFormError("");
    try {
      await deleteCredential(draft.id);
      await api(`/connections/${draft.id}`, { method: "DELETE" });
      setDraft(emptyConnection); setSavedDraft(emptyConnection); setKey(""); setTestResult("");
      setMakeDefault(connections.length <= 1); setSavedMakeDefault(connections.length <= 1);
      setCustomModel(false); setModelHint(""); onSaved(); notify("连接已删除，已有音频保留");
    } catch (error) { setFormError(error instanceof Error ? error.message : "删除连接失败。"); }
    finally { setBusy(false); }
  };
  return <Modal title="云端模型连接" description="填写服务地址与密钥，保存后自动用于配音。" open={open}
    onClose={() => { if (canLeave()) onClose(); }} wide>
    <div className="connection-layout connection-settings">
      <div className="connection-list">
        <div className="section-label">我的连接<IconButton label="添加连接" disabled={busy} onClick={() => selectConnection(emptyConnection)}><Plus size={15} /></IconButton></div>
        {connections.map(connection => <button key={connection.id} disabled={busy}
          className={`connection-item ${draft.id === connection.id ? "active" : ""}`} onClick={() => selectConnection(connection)}>
          <Cloud size={18} /><span><strong>{connection.name}</strong><small title={addressLabel(connection)}>{addressLabel(connection)}</small>
            {connection.id === activeDefault && <em className="connection-default-badge"><Star size={10} fill="currentColor" />默认连接</em>}
          </span><StatusDot state={connection.has_credential ? "ok" : "warn"} />
        </button>)}
        {!connections.length && <div className="connection-empty"><Cloud size={25} /><p>添加第一个连接</p><small>配置一次，后续自动沿用。</small></div>}
        <p className="connection-list-tip">默认连接用于新台词。已有台词的独立设置会保留。</p>
      </div>
      <div className="connection-form">
        <div className="connection-section-heading"><Cloud size={17} /><div><strong>连接百炼服务</strong><small>从控制台复制服务地址，无需再填写地域或业务空间。</small></div></div>
        <fieldset disabled={busy} className="connection-fields">
          <Field label="连接名称"><input value={draft.name ?? ""} maxLength={100} onChange={event => field("name", event.target.value)} placeholder="例如：工作室配音" /></Field>
          <Field label="服务地址（Base URL）" hint="复制百炼控制台的 HTTPS 服务根地址，应用自动处理接口路径。">
            <input type="url" value={draft.base_url ?? ""} onChange={event => field("base_url", event.target.value)} placeholder="https://your-workspace.cn-beijing.maas.aliyuncs.com" autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="API Key" hint={isDesktop() ? "优先保存在系统凭据库；不可用时明确提示仅本次使用。" : "开发模式只在本次服务会话保存密钥，重启后需重新填写。"}>
            <div className="input-icon"><KeyRound size={16} /><input type="password" value={key}
              onChange={event => { setKey(event.target.value); setFormError(""); setTestResult(""); }}
              placeholder={draft.has_credential ? "已配置，留空保留原密钥" : "输入该服务对应的 API Key"} autoComplete="new-password" /></div>
          </Field>
          <div className="connection-section-heading voice-default-heading"><Star size={16} /><div><strong>默认配音设置</strong><small>新台词自动继承，也可为单句或角色单独调整。</small></div></div>
          <Field label="默认模型"><select value={customModel ? "__custom__" : draft.model_id ?? ""} onChange={event => changeModel(event.target.value)}>
            {models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}
            <option value="__custom__">自定义模型（高级）</option>
          </select></Field>
          <Field label="默认音色" hint="按当前模型筛选，直接选择声音名称即可。"><select value={draft.voice_id ?? ""} onChange={event => { field("voice_id", event.target.value); setModelHint(""); }}>
            {!knownVoice && <option value={draft.voice_id ?? ""}>{draft.voice_id ? "当前自定义音色（高级设置中查看）" : "请选择音色"}</option>}
            {compatibleVoices.map(voice => <option key={voice.voice_id} value={voice.voice_id} disabled={voice.status !== "OK"}>{voice.name}{voice.kind === "clone" ? " · 克隆" : voice.kind === "design" ? " · 设计" : ""}{voice.status !== "OK" ? " · 尚未就绪" : ""}</option>)}
          </select></Field>
          {modelHint && <p className="connection-inline-note">{modelHint}</p>}
          <label className="checkbox-row connection-default-option"><input type="checkbox" checked={isDefault || makeDefault} disabled={isDefault}
            onChange={event => setMakeDefault(event.target.checked)} /><span><strong>{isDefault ? "正在使用的默认连接" : "设为默认连接"}</strong><small>保存后自动沿用，无需每次生成重新选择。</small></span></label>
          <button className="text-btn advanced-toggle" onClick={() => setAdvanced(value => !value)} aria-expanded={advanced}>
            <ChevronDown size={14} className={advanced ? "rotate" : ""} />高级连接设置
          </button>
          {advanced && <div className="connection-advanced">
            {customModel && <Field label="自定义模型 ID" hint="使用与百炼语音协议兼容的模型；未验证的高级能力不会自动启用。"><input value={draft.model_id ?? ""} onChange={event => field("model_id", event.target.value)} /></Field>}
            <Field label="自定义音色 ID" hint="仅在声音未收录时手动填写，必须与当前模型兼容。"><input value={draft.voice_id ?? ""} onChange={event => field("voice_id", event.target.value)} /></Field>
            <div className="form-row">
              <Field label="最大并发任务"><input type="number" min={1} max={8} value={draft.max_concurrency ?? 2}
                onChange={event => field("max_concurrency", Math.max(1, Math.min(8, Number(event.target.value))))} /></Field>
              <Field label="每分钟请求上限"><input type="number" min={1} max={600} value={draft.requests_per_minute ?? 120}
                onChange={event => field("requests_per_minute", Math.max(1, Math.min(600, Number(event.target.value))))} /></Field>
            </div>
            <p className="muted small">以上限制在同一服务连接内共享，还需遵守账户实际配额。</p>
            <div className="section-label">参考单价 · 可留空</div>
            <p className="muted small">按账户实际价格填写用于估算，最终以百炼账单为准。</p>
            <Field label="字符计费单价（元 / 万字符）"><input type="number" min={0} step={0.01} value={draft.price_per_10k_chars ?? ""}
              onChange={event => field("price_per_10k_chars", event.target.value === "" ? null : Math.max(0, Number(event.target.value)))} placeholder="留空不估算" /></Field>
            <div className="form-row">
              <Field label="输入单价（元 / 百万 Token）"><input type="number" min={0} step={0.01} value={draft.input_price_per_million_tokens ?? ""}
                onChange={event => field("input_price_per_million_tokens", event.target.value === "" ? null : Math.max(0, Number(event.target.value)))} /></Field>
              <Field label="输出单价（元 / 百万 Token）"><input type="number" min={0} step={0.01} value={draft.output_price_per_million_tokens ?? ""}
                onChange={event => field("output_price_per_million_tokens", event.target.value === "" ? null : Math.max(0, Number(event.target.value)))} /></Field>
            </div>
          </div>}
        </fieldset>
        <div className="security-note"><ShieldCheck size={15} /><span>密钥不会进入项目、备份和生成历史。连接检测为只读请求。</span></div>
        {formError && <p className="connection-feedback error-text" role="alert">{formError}</p>}
        {testResult && <p className="connection-feedback success" role="status"><Check size={14} />{testResult}</p>}
        {unsaved && <p className="small muted">连接配置有未保存的修改，请先保存连接后再检测。</p>}
        <div className="modal-actions connection-actions">
          {draft.id && <IconButton label="删除连接" disabled={busy} onClick={() => void remove()}><Trash2 size={16} /></IconButton>}
          <Button onClick={() => void test()} busy={busy} disabled={!draft.id || unsaved || busy}><Wifi size={15} />检测连接</Button>
          <Button variant="primary" onClick={() => void save()} busy={busy}><Save size={15} />保存连接</Button>
        </div>
        {draft.has_credential && <div className="small success"><Check size={13} />已配置凭据</div>}
      </div>
    </div>
  </Modal>;
}
