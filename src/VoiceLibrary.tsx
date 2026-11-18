import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, Headphones, Loader2, Maximize2, Mic, Pause, Play, Plus, RefreshCw, Search, Star, Trash2, Users, X } from "lucide-react";
import { api } from "./api";
import type { ModelProfile, VoiceProfile } from "./types";
import { groupVoices, modelLabel, voiceMatches, type CatalogVoice } from "./voiceLibrary";
import { Button, Empty, IconButton, Modal } from "./ui";
import "./voiceLibrary.css";

type Props = {
  voices: VoiceProfile[];
  models: ModelProfile[];
  preferredModel: string;
  currentVoice?: string;
  currentRegion?: string;
  favoriteIds: string[];
  selectedCount: number;
  hasProject: boolean;
  onUse: (voice: VoiceProfile) => void;
  onFavorite: (ids: string[], favorite: boolean) => Promise<void>;
  onCopy: (voice: VoiceProfile) => Promise<void>;
  onRefresh: (voice: VoiceProfile) => Promise<void>;
  onDelete: (voice: VoiceProfile) => Promise<void>;
  onCreate: () => void;
  onRoles: () => void;
  onPreviewStart: () => void;
  externalPlaybackActive: boolean;
  notify: (message: string, error?: boolean) => void;
};

export function VoiceLibrary(props: Props) {
  const [query, setQuery] = useState("");
  const [model, setModel] = useState("");
  const [gender, setGender] = useState("");
  const [language, setLanguage] = useState("");
  const [compatibleOnly, setCompatibleOnly] = useState(true);
  const [category, setCategory] = useState<"all" | "favorites" | "personal">("all");
  const [expanded, setExpanded] = useState(false);
  const [variants, setVariants] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<{ id: string; name: string; source?: string | null } | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [position, setPosition] = useState(0);
  const [duration, setDuration] = useState(0);
  const [savingFavorite, setSavingFavorite] = useState(false);
  const audio = useRef<HTMLAudioElement>(null);
  const request = useRef<AbortController | null>(null);
  const serial = useRef(0);
  const voices = props.voices as CatalogVoice[];
  const groups = useMemo(() => groupVoices(voices), [voices]);
  const genders = [...new Set(voices.map(voice => voice.gender).filter((value): value is string => !!value))];
  const languages = [...new Set(voices.flatMap(voice => voice.languages ?? []))];
  const compatible = (voice: CatalogVoice) => !props.currentRegion || !voice.regions?.length || voice.regions.includes(props.currentRegion);
  const favorite = (voice: CatalogVoice) => props.favoriteIds.includes(voice.id);
  const visible = groups.map(group => ({ ...group, voices: group.voices.filter(voice =>
    (!compatibleOnly || compatible(voice)) && (!model || voice.target_model === model) && (!gender || voice.gender === gender)
    && (!language || voice.languages?.includes(language)) && voiceMatches(voice, query)
    && (category !== "personal" || !voice.id.startsWith("system:"))
    && (category !== "favorites" || favorite(voice)),
  ) })).filter(group => group.voices.length).sort((a, b) =>
    Number(b.voices.some(favorite)) - Number(a.voices.some(favorite)),
  );
  useEffect(() => () => { serial.current++; request.current?.abort(); audio.current?.pause(); }, []);
  useEffect(() => {
    if (props.externalPlaybackActive) {
      serial.current++; request.current?.abort(); audio.current?.pause(); setLoading(null);
    }
  }, [props.externalPlaybackActive]);
  const audition = async (voice: CatalogVoice) => {
    if (preview?.id === voice.id && audio.current?.src && !loading) {
      if (playing) audio.current.pause();
      else {
        props.onPreviewStart();
        try { await audio.current.play(); } catch { props.notify("试听播放失败，请重新试听。", true); }
      }
      return;
    }
    const attempt = ++serial.current;
    request.current?.abort();
    const abort = new AbortController(); request.current = abort;
    audio.current?.pause(); setLoading(voice.id); setPosition(0); setDuration(0);
    setPreview({ id: voice.id, name: voice.name, source: voice.preview_source });
    props.onPreviewStart();
    try {
      const result = await api<CatalogVoice>(`/voices/${encodeURIComponent(voice.id)}/preview`, { method: "POST", signal: abort.signal });
      if (serial.current !== attempt || !audio.current) return;
      if (!result.preview_url) throw new Error("此声音尚无可用样音。");
      setPreview({ id: voice.id, name: voice.name, source: result.preview_source });
      audio.current.src = result.preview_url;
      await audio.current.play();
    } catch (error) {
      if (serial.current === attempt && !abort.signal.aborted) {
        setPreview(null);
        props.notify(error instanceof Error ? error.message : "试听加载失败，请重试。", true);
      }
    } finally { if (serial.current === attempt) setLoading(null); }
  };
  const copyVoiceId = async (voice: CatalogVoice) => {
    try {
      await navigator.clipboard.writeText(voice.voice_id);
      props.notify(`已复制音色 ID：${voice.voice_id}`);
    } catch {
      props.notify("无法访问剪贴板，请检查应用权限后重试。", true);
    }
  };
  const player = () => preview && <div className="voice-preview-dock">
    <button className="voice-preview-toggle" aria-label={playing ? "暂停音色试听" : "播放音色试听"} disabled={!!loading}
      onClick={() => { if (playing) audio.current?.pause(); else { props.onPreviewStart(); void audio.current?.play().catch(() => props.notify("请重新加载样音。", true)); } }}>
      {loading ? <Loader2 size={17} className="spin" /> : playing ? <Pause size={17} /> : <Play size={17} />}
    </button>
    <div><strong>{preview.name}</strong><small>{loading ? "正在加载样音…" : preview.source === "reference" ? "参考录音 · 非合成效果" : preview.source === "design" ? "设计试听" : "官方样音 · 不消耗生成额度"}</small>
      <input aria-label="音色试听进度" type="range" min={0} max={Math.max(duration, 0.1)} step={0.05} value={position}
        onChange={event => { if (audio.current) audio.current.currentTime = Number(event.target.value); setPosition(Number(event.target.value)); }} disabled={!duration} />
    </div>
    <IconButton label="停止音色试听" onClick={() => { serial.current++; request.current?.abort(); audio.current?.pause(); setPreview(null); setLoading(null); }}><X size={14} /></IconButton>
  </div>;
  const browser = (large: boolean) => <div className={`voice-browser ${large ? "voice-browser-expanded" : ""}`}>
    <div className="voice-browser-filters">
      <div className="voice-library-search"><Search size={15} /><input aria-label="搜索声音" placeholder="搜索声音、语言或风格" value={query} onChange={event => setQuery(event.target.value)} />
        {query && <button aria-label="清除声音搜索" onClick={() => setQuery("")}><X size={13} /></button>}</div>
      <div className="voice-library-tabs">{([["all", "全部声音"], ["favorites", "我的收藏"], ["personal", "我的声音"]] as const).map(([value, label]) =>
        <button key={value} className={category === value ? "active" : ""} onClick={() => setCategory(value)}>{label}</button>)}</div>
      <select aria-label="筛选音色模型" value={model} onChange={event => setModel(event.target.value)}><option value="">全部模型</option>
        {props.models.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <div className="voice-filter-row">
        <select aria-label="筛选声音性别" value={gender} onChange={event => setGender(event.target.value)}><option value="">全部声线</option>{genders.map(value => <option key={value}>{value}</option>)}</select>
        <select aria-label="筛选声音语言" value={language} onChange={event => setLanguage(event.target.value)}><option value="">全部语言</option>{languages.map(value => <option key={value}>{value}</option>)}</select>
      </div>
      {props.currentRegion && <label className="voice-compatible-toggle"><input type="checkbox" checked={compatibleOnly} onChange={event => setCompatibleOnly(event.target.checked)} />仅显示当前服务支持的声音</label>}
      <div className="voice-result-summary"><span>{visible.length} 个声音</span><span>先试听，再选用</span></div>
    </div>
    <div className="voice-catalog-grid">
      {visible.map(group => {
        const voice = group.voices.find(item => item.id === variants[group.key])
          ?? group.voices.find(item => item.target_model === props.preferredModel) ?? group.voices[0];
        const isPlaying = preview?.id === voice.id && playing;
        const isLoading = loading === voice.id;
        const isFavorite = group.voices.some(favorite);
        const selected = props.currentVoice === voice.voice_id && props.preferredModel === voice.target_model;
        const available = !!voice.preview_available || !!voice.preview_url;
        const switchModel = voice.target_model !== props.preferredModel;
        return <article className={`voice-card ${selected ? "voice-card-selected" : ""}`} key={group.key} data-voice-id={voice.voice_id}>
          <div className="voice-card-main">
            <button className={`voice-audition voice-avatar-${voice.kind}`} aria-label={`${isPlaying ? "暂停试听" : "试听"}：${voice.name}`}
              title={available ? "试听样音，不提交生成" : "此声音暂无样音"} disabled={!available} onClick={() => void audition(voice)}>
              {isLoading ? <Loader2 size={20} className="spin" /> : isPlaying ? <Pause size={20} /> : <Play size={20} />}</button>
            <div className="voice-card-title"><strong title={voice.name}>{voice.name}</strong><small>{voice.gender || (voice.kind === "system" ? "官方音色" : voice.kind === "clone" ? "克隆声音" : "设计音色")}<span> · </span>{voice.languages?.slice(0, 2).join(" / ") || "自定义声音"}</small></div>
            <IconButton label={`${isFavorite ? "取消收藏" : "收藏"}：${voice.name}`} disabled={savingFavorite} onClick={async () => {
              setSavingFavorite(true);
              try { await props.onFavorite(group.voices.map(item => item.id), !isFavorite); }
              catch (error) { props.notify(error instanceof Error ? error.message : "收藏失败", true); }
              finally { setSavingFavorite(false); }
            }}><Star size={15} className={isFavorite ? "star-filled" : ""} /></IconButton>
          </div>
          <div className="voice-card-id">
            <span>音色 ID</span><code title={voice.voice_id}>{voice.voice_id}</code>
            <IconButton label={`复制音色 ID：${voice.name}`} onClick={() => void copyVoiceId(voice)}><Copy size={13} /></IconButton>
          </div>
          <div className="voice-card-tags">{(voice.tags?.length ? voice.tags : [voice.kind === "system" ? "官方预设" : "我的声音"]).slice(0, 3).map(tag => <span key={tag}>{tag}</span>)}</div>
          <div className="voice-card-model">
            {group.voices.length > 1 ? <select aria-label={`合成模型：${voice.name}`} value={voice.id} onChange={event => setVariants(value => ({ ...value, [group.key]: event.target.value }))}>
              {group.voices.map(item => <option key={item.id} value={item.id}>{modelLabel(item.target_model)}</option>)}</select> : <span>{modelLabel(voice.target_model)}</span>}
            <span>{voice.status === "OK" ? available ? "可试听" : "暂无样音" : voice.status}</span>
          </div>
          <div className="voice-card-bottom">
            <span className="voice-apply-hint">{!compatible(voice) ? "当前服务地址不支持此声音" : switchModel ? `同时切换至 ${modelLabel(voice.target_model)}` : selected ? "当前台词使用中" : props.selectedCount > 1 ? `应用到 ${props.selectedCount} 句` : "应用到所选台词"}</span>
            <Button variant={selected ? "default" : "primary"} disabled={!props.selectedCount || voice.status !== "OK" || !compatible(voice)} onClick={() => props.onUse(voice)}>{selected ? <Check size={13} /> : null}使用</Button>
          </div>
          {!voice.id.startsWith("system:") && <div className="voice-personal-tools">
            <IconButton label={`复制声音配置：${voice.name}`} onClick={() => void props.onCopy(voice)}><Copy size={13} /></IconButton>
            {voice.kind !== "system" && <IconButton label={`刷新云端状态：${voice.name}`} onClick={() => void props.onRefresh(voice)}><RefreshCw size={13} /></IconButton>}
            <IconButton label={`移除本地音色配置：${voice.name}`} onClick={() => void props.onDelete(voice)}><Trash2 size={13} /></IconButton>
          </div>}
        </article>;
      })}
      {!visible.length && <Empty icon={<Mic size={26} />} title="没有符合条件的声音" action={<Button onClick={() => { setQuery(""); setModel(""); setGender(""); setLanguage(""); setCategory("all"); setCompatibleOnly(true); }}>清除筛选</Button>} />}
    </div>
    {player()}
  </div>;
  return <>
    <div className="resource-heading"><span>声音库 <b>{groups.length}</b></span><div className="voice-heading-actions">
      <IconButton label="展开声音库" onClick={() => setExpanded(true)}><Maximize2 size={14} /></IconButton>
      <IconButton label="创建声音" onClick={props.onCreate}><Plus size={15} /></IconButton></div></div>
    {browser(false)}
    <div className="resource-footer"><Button className="full-width" onClick={props.onRoles} disabled={!props.hasProject}><Users size={14} />管理项目角色</Button></div>
    <Modal title="挑选配音声音" description="按语言和声线筛选，试听后应用到台词。不同模型的兼容性分别标明。" open={expanded} onClose={() => setExpanded(false)} wide>
      <div className="voice-expanded-heading"><span><Headphones size={16} />官方样音试听不消耗生成额度</span><Button onClick={() => { setExpanded(false); props.onCreate(); }}><Plus size={14} />创建声音</Button></div>
      {browser(true)}
    </Modal>
    <audio ref={audio} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)}
      onLoadedMetadata={() => setDuration(Number.isFinite(audio.current?.duration) ? audio.current!.duration : 0)}
      onTimeUpdate={() => setPosition(audio.current?.currentTime ?? 0)} onError={() => { if (preview && !loading) props.notify("样音无法播放，请重新加载。", true); }} />
  </>;
}
