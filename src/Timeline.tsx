import { AudioLines, Copy, Magnet, Maximize2, Minus, Plus, Scissors, Scan, Trash2, Volume2, VolumeX } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { createPortal } from "react-dom";
import type { Project, TimelineClip } from "./types";
import { SAMPLE_RATE, clamp, clipDuration, secondsToSamples, timecode, uid } from "./lib";
import { clipEnd, duplicateClips, fadeClip, moveClips, snapEdge, snapMove, splitClip, trimClip } from "./timelineEditing";
import { IconButton } from "./ui";
import { Waveform } from "./Waveform";
import "./timeline.css";

type EditMode = "move" | "start" | "end" | "fade-in" | "fade-out";
const EDIT_FIELDS = ["start_sample", "track_id", "source_start_sample", "source_end_sample", "fade_in_ms", "fade_out_ms"] as const;

export function Timeline({ project, selectedId, onSelect, onEdit, currentTime, onSeek }: {
  project: Project | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onEdit: (updater: (project: Project) => Project) => void;
  currentTime: number;
  onSeek: (time: number) => void;
}) {
  const [zoom, setZoom] = useState(45);
  const [snap, setSnap] = useState(true);
  const [selectedIds, setSelectedIds] = useState<string[]>(selectedId ? [selectedId] : []);
  const [draft, setDraft] = useState<Record<string, TimelineClip>>({});
  const [guide, setGuide] = useState<number | null>(null);
  const [hint, setHint] = useState("");
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const section = useRef<HTMLElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const labels = useRef<HTMLDivElement>(null);
  const gesture = useRef<(() => void) | null>(null);
  const latest = useRef({ project, onEdit, onSeek });
  latest.current = { project, onEdit, onSeek };
  const selectionProject = useRef(project?.id);

  useEffect(() => {
    if (selectionProject.current !== project?.id) {
      selectionProject.current = project?.id;
      setSelectedIds(selectedId ? [selectedId] : []);
      setMenu(null);
    } else setSelectedIds(ids => {
      const available = ids.filter(id => project?.clips.some(clip => clip.id === id));
      const next = selectedId ? (available.includes(selectedId) ? available : [selectedId]) : [];
      return next.join() === ids.join() ? ids : next;
    });
  }, [selectedId, project?.id, project?.clips]);
  useEffect(() => () => { gesture.current?.(); gesture.current = null; }, [project?.id]);
  useEffect(() => {
    if (!menu) return;
    const close = (event: globalThis.PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".timeline-context-menu")) setMenu(null);
    };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [menu]);

  const tracks = useMemo(() => {
    const map = new Map<string, { name: string; color: string }>();
    map.set("main", { name: "主音轨", color: "#70b4a8" });
    project?.roles.forEach(role => map.set(role.id, { name: role.name, color: role.color }));
    project?.clips.forEach(clip => {
      if (!map.has(clip.track_id)) map.set(clip.track_id, { name: clip.track_id === "narrator" ? "旁白" : clip.track_id, color: "#8999bd" });
    });
    return [...map.entries()];
  }, [project?.roles, project?.clips]);
  const clips = project?.clips.map(clip => draft[clip.id] ?? clip) ?? [];
  const selected = clips.filter(clip => selectedIds.includes(clip.id));
  const endTime = Math.max(0, ...clips.map(clip => clipEnd(clip) / SAMPLE_RATE));
  const duration = Math.max(10, endTime + 3, currentTime + 2);
  const width = Math.max(scroll.current?.clientWidth ?? 600, duration * zoom);
  const allMuted = selected.length > 0 && selected.every(clip => clip.muted);
  const canSplit = selected.some(clip => secondsToSamples(currentTime) > clip.start_sample && secondsToSamples(currentTime) < clipEnd(clip));
  const select = (ids: string[], primary = ids[ids.length - 1] ?? null) => {
    setSelectedIds(ids);
    onSelect(primary);
  };
  const focus = () => section.current?.focus({ preventScroll: true });
  const updateSelection = (operation: (value: Project, ids: Set<string>) => Project) => {
    if (!project || !selectedIds.length) return;
    const projectId = project.id, ids = new Set(selectedIds);
    onEdit(value => value.id === projectId ? operation(value, ids) : value);
    setMenu(null);
  };
  const remove = () => {
    updateSelection((value, ids) => ({ ...value, clips: value.clips.filter(clip => !ids.has(clip.id)) }));
    select([]);
    setHint("片段已移除，原始音频保留；Ctrl+Z 撤销");
  };
  const mute = () => updateSelection((value, ids) => ({ ...value,
    clips: value.clips.map(clip => ids.has(clip.id) ? { ...clip, muted: !allMuted } : clip) }));
  const duplicate = () => {
    const copies = duplicateClips(selected, uid);
    if (!copies.length) return;
    updateSelection(value => ({ ...value, clips: [...value.clips, ...copies] }));
    select(copies.map(clip => clip.id));
    setHint(`已在选中片段之后复制 ${copies.length} 个片段`);
  };
  const split = () => {
    if (!canSplit) return;
    const position = secondsToSamples(currentTime);
    updateSelection((value, ids) => ({ ...value, clips: value.clips.flatMap(clip => ids.has(clip.id) ? splitClip(clip, position, uid()) : [clip]) }));
    setHint("已在播放指针处分割，原始音频保留");
  };
  const nudge = (delta: number) => updateSelection((value, ids) => {
    const moved = new Map(moveClips(value.clips.filter(clip => ids.has(clip.id)), delta, tracks.map(([id]) => id)).map(clip => [clip.id, clip]));
    return { ...value, clips: value.clips.map(clip => moved.get(clip.id) ?? clip) };
  });
  const fit = (onlySelected: boolean) => {
    const values = onlySelected ? selected : clips;
    if (!values.length || !scroll.current) return;
    const start = onlySelected ? Math.min(...values.map(clip => clip.start_sample / SAMPLE_RATE)) : 0;
    const end = Math.max(...values.map(clip => clipEnd(clip) / SAMPLE_RATE));
    const nextZoom = clamp((scroll.current.clientWidth - 48) / Math.max(0.25, end - start + 0.5), 0.1, 600);
    setZoom(nextZoom);
    requestAnimationFrame(() => { if (scroll.current) scroll.current.scrollLeft = Math.max(0, start * nextZoom - 24); });
    setMenu(null);
  };
  const zoomAt = (nextZoom: number) => {
    if (!scroll.current) return;
    const node = scroll.current;
    const anchor = (node.scrollLeft + node.clientWidth / 2) / zoom;
    const next = clamp(nextZoom, 0.1, 600);
    setZoom(next);
    requestAnimationFrame(() => { node.scrollLeft = Math.max(0, anchor * next - node.clientWidth / 2); });
  };
  useEffect(() => {
    const node = scroll.current;
    if (!node) return;
    const wheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        zoomAt(zoom * (event.deltaY > 0 ? 1 / 1.15 : 1.15));
      } else if (event.shiftKey) {
        event.preventDefault(); node.scrollLeft += event.deltaY;
      }
    };
    node.addEventListener("wheel", wheel, { passive: false });
    return () => node.removeEventListener("wheel", wheel);
  }, [zoom]);
  const pointerEdit = (event: PointerEvent, clip: TimelineClip, mode: EditMode) => {
    if (event.button !== 0 || !project) return;
    event.stopPropagation(); event.preventDefault(); focus(); setMenu(null);
    if ((event.shiftKey || event.ctrlKey || event.metaKey) && mode === "move") {
      const ids = selectedIds.includes(clip.id) ? selectedIds.filter(id => id !== clip.id) : [...selectedIds, clip.id];
      select(ids); return;
    }
    const ids = mode === "move" && selectedIds.includes(clip.id) ? selectedIds : [clip.id];
    select(ids, clip.id);
    gesture.current?.();
    const originals = project.clips.filter(item => ids.includes(item.id));
    const projectId = project.id;
    const startX = event.clientX, startY = event.clientY;
    const initialScroll = scroll.current?.scrollLeft ?? 0;
    const trackIds = tracks.map(([id]) => id);
    const originTrack = trackIds.indexOf(clip.track_id);
    const targetPoints = [0, secondsToSamples(currentTime), ...project.clips.filter(item => !ids.includes(item.id)).flatMap(item => [item.start_sample, clipEnd(item)])];
    const tolerance = SAMPLE_RATE * 8 / zoom;
    const maxSource = Math.round((project.assets.find(asset => asset.id === clip.asset_id)?.duration_ms ?? 0) * SAMPLE_RATE / 1000);
    let pending = originals, changed = false;
    const pointerId = event.pointerId;
    const cleanup = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("blur", cancel);
      window.removeEventListener("keydown", escape);
      gesture.current = null; setDraft({}); setGuide(null);
    };
    const cancel = () => { cleanup(); setHint("已取消拖动"); };
    const escape = (key: globalThis.KeyboardEvent) => {
      if (key.key === "Escape") { key.preventDefault(); key.stopPropagation(); cancel(); }
    };
    const move = (next: globalThis.PointerEvent) => {
      if (next.pointerId !== pointerId) return;
      if (Math.abs(next.clientX - startX) < 3 && Math.abs(next.clientY - startY) < 3 && !changed) return;
      const node = scroll.current;
      if (node) {
        const bounds = node.getBoundingClientRect();
        if (next.clientX < bounds.left + 20) node.scrollLeft -= 12;
        else if (next.clientX > bounds.right - 20) node.scrollLeft += 12;
      }
      let delta = Math.round((next.clientX - startX + (node?.scrollLeft ?? 0) - initialScroll) / zoom * SAMPLE_RATE);
      let snapGuide: number | null = null;
      if (mode === "move") {
        if (snap && !next.altKey) { const result = snapMove(originals, delta, targetPoints, tolerance); delta = result.delta; snapGuide = result.guide; }
        const lane = document.elementFromPoint(next.clientX, next.clientY)?.closest<HTMLElement>("[data-track-id]");
        const destination = lane && section.current?.contains(lane) ? trackIds.indexOf(lane.dataset.trackId ?? "") : originTrack;
        pending = moveClips(originals, delta, trackIds, destination < 0 ? 0 : destination - originTrack);
      } else if (mode === "start" || mode === "end") {
        const origin = mode === "start" ? clip.start_sample : clipEnd(clip);
        if (snap && !next.altKey) { const result = snapEdge(origin + delta, targetPoints, tolerance); delta = result.position - origin; snapGuide = result.guide; }
        pending = [trimClip(clip, mode, delta, maxSource)];
        const actual = mode === "start" ? pending[0].start_sample : clipEnd(pending[0]);
        if (snapGuide !== null && Math.abs(actual - snapGuide) > 1) snapGuide = null;
      } else pending = [fadeClip(clip, mode === "fade-in" ? "in" : "out", delta)];
      changed = pending.some(nextClip => {
        const previous = originals.find(item => item.id === nextClip.id)!;
        return EDIT_FIELDS.some(field => nextClip[field] !== previous[field]);
      });
      setDraft(Object.fromEntries(pending.map(item => [item.id, item]))); setGuide(snapGuide);
      const current = pending.find(item => item.id === clip.id) ?? pending[0];
      setHint(mode.startsWith("fade") ? `${mode === "fade-in" ? "淡入" : "淡出"} ${mode === "fade-in" ? current.fade_in_ms : current.fade_out_ms} 毫秒`
        : `${timecode(current.start_sample / SAMPLE_RATE)} · ${clipDuration(current).toFixed(3)} 秒${snapGuide === null ? "" : " · 已吸附"}`);
    };
    const up = (next: globalThis.PointerEvent) => {
      if (next.pointerId !== pointerId) return;
      if (changed && latest.current.project?.id === projectId) {
        const changes = new Map(pending.map(nextClip => {
          const previous = originals.find(item => item.id === nextClip.id)!;
          return [nextClip.id, Object.fromEntries(EDIT_FIELDS.filter(field => nextClip[field] !== previous[field]).map(field => [field, nextClip[field]]))];
        }));
        latest.current.onEdit(value => value.id === projectId ? { ...value,
          clips: value.clips.map(item => changes.has(item.id) ? { ...item, ...changes.get(item.id) } : item) } : value);
      }
      cleanup();
    };
    gesture.current = cancel;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", cancel);
    window.addEventListener("keydown", escape);
  };
  const keyboard = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable) return;
    const modifier = event.ctrlKey || event.metaKey;
    let handled = true;
    if (event.key === "Escape") { gesture.current?.(); setMenu(null); }
    else if (gesture.current) {
      if (modifier && event.key.toLowerCase() === "z") gesture.current();
      event.preventDefault(); event.stopPropagation(); return;
    }
    else if (modifier && event.key.toLowerCase() === "a") select(clips.map(clip => clip.id));
    else if (modifier && event.key.toLowerCase() === "d") duplicate();
    else if (event.key === "Delete" || event.key === "Backspace") { if (selected.length) remove(); }
    else if (!modifier && event.key.toLowerCase() === "s") setSnap(value => !value);
    else if (!modifier && event.key.toLowerCase() === "b") split();
    else if (!modifier && event.key.toLowerCase() === "m") mute();
    else if (!modifier && event.key === "ArrowLeft") nudge(-SAMPLE_RATE * (event.shiftKey ? 0.1 : 0.01));
    else if (!modifier && event.key === "ArrowRight") nudge(SAMPLE_RATE * (event.shiftKey ? 0.1 : 0.01));
    else if (event.key === "Home") { onSeek(0); if (scroll.current) scroll.current.scrollLeft = 0; }
    else if (event.key === "End") onSeek(endTime);
    else if (event.key === "=" || event.key === "+") zoomAt(zoom * 1.3);
    else if (event.key === "-") zoomAt(zoom / 1.3);
    else handled = false;
    if (handled) { event.preventDefault(); event.stopPropagation(); }
  };
  const rulerStep = zoom < 2 ? 60 : zoom < 10 ? 30 : zoom < 30 ? 10 : zoom < 70 ? 5 : zoom < 200 ? 2 : 0.5;
  const seek = (event: PointerEvent<HTMLDivElement>) => {
    focus(); setMenu(null);
    const bounds = event.currentTarget.getBoundingClientRect();
    let position = secondsToSamples((event.clientX - bounds.left) / zoom);
    if (snap && !event.altKey) position = snapEdge(position, [0, ...clips.flatMap(clip => [clip.start_sample, clipEnd(clip)])], SAMPLE_RATE * 8 / zoom).position;
    onSeek(position / SAMPLE_RATE);
  };
  return <section className="timeline timeline-enhanced" ref={section} tabIndex={0} aria-label="时间轴编辑区" onKeyDown={keyboard}>
    <div className="timeline-toolbar">
      <div className="panel-title-inline"><AudioLines size={15} />时间轴<span className="muted small">{selected.length ? `已选 ${selected.length} / ` : ""}{clips.length} 个片段</span></div>
      <div className="timeline-tools">
        <IconButton label="在播放指针处分割" disabled={!canSplit} onClick={split}><Scissors size={15} /></IconButton>
        <IconButton label="复制选中片段" disabled={!selected.length} onClick={duplicate}><Copy size={15} /></IconButton>
        <IconButton label={allMuted ? "取消静音" : "静音片段"} disabled={!selected.length} onClick={mute}>{allMuted ? <VolumeX size={15} /> : <Volume2 size={15} />}</IconButton>
        <IconButton label="移除时间轴片段" disabled={!selected.length} onClick={remove}><Trash2 size={15} /></IconButton>
        <div className="separator" />
        <IconButton label={snap ? "关闭吸附" : "开启吸附"} className={snap ? "active-icon" : ""} onClick={() => setSnap(value => !value)}><Magnet size={15} /></IconButton>
        <IconButton label="适应全部片段" disabled={!clips.length} onClick={() => fit(false)}><Maximize2 size={15} /></IconButton>
        <IconButton label="放大选中片段" disabled={!selected.length} onClick={() => fit(true)}><Scan size={15} /></IconButton>
        <div className="separator" />
        <IconButton label="缩小时间轴" disabled={zoom <= 0.1} onClick={() => zoomAt(zoom / 1.3)}><Minus size={14} /></IconButton>
        <input aria-label="时间轴缩放" className="zoom-range" type="range" min={0} max={100} step={1} value={Math.log(zoom / 0.1) / Math.log(6000) * 100} onChange={event => zoomAt(0.1 * 6000 ** (Number(event.target.value) / 100))} />
        <IconButton label="放大时间轴" disabled={zoom >= 600} onClick={() => zoomAt(zoom * 1.3)}><Plus size={14} /></IconButton>
      </div>
    </div>
    <div className="timeline-body">
      <div className="track-labels" ref={labels}><div className="ruler-label">48,000 Hz</div>{tracks.map(([id, track]) => <div className="track-label" key={id}><i style={{ background: track.color }} /><span title={track.name}>{track.name}</span><small>{clips.filter(clip => clip.track_id === id).length}</small></div>)}</div>
      <div className="timeline-scroll" ref={scroll} onScroll={event => { if (labels.current) labels.current.scrollTop = event.currentTarget.scrollTop; }}>
        <div className="timeline-content" style={{ width }}>
          <div className="timeline-ruler" onPointerDown={event => {
            if (event.button === 0) { event.currentTarget.setPointerCapture(event.pointerId); seek(event); }
          }} onPointerMove={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) seek(event); }}
            onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}>
            {Array.from({ length: Math.min(3000, Math.ceil(duration / rulerStep) + 1) }, (_, index) => <span key={index} style={{ left: index * rulerStep * zoom }}>{timecode(index * rulerStep).slice(0, rulerStep < 1 ? 9 : 5)}</span>)}
          </div>
          {tracks.map(([id, track]) => <div className="track-lane" key={id} data-track-id={id} style={{ backgroundSize: `${rulerStep * zoom}px 100%` }} onPointerDown={event => {
            if (event.button !== 0) return;
            select([]); seek(event);
          }}>
            {clips.filter(clip => clip.track_id === id).map(clip => {
              const asset = project?.assets.find(value => value.id === clip.asset_id);
              const clipWidth = Math.max(8, clipDuration(clip) * zoom);
              return <div key={clip.id} data-clip-id={clip.id} data-start-sample={clip.start_sample} data-source-start={clip.source_start_sample} data-source-end={clip.source_end_sample}
                className={`timeline-clip ${selectedIds.includes(clip.id) ? "selected" : ""} ${draft[clip.id] ? "drag-preview" : ""} ${clip.muted ? "muted-clip" : ""}`}
                style={{ left: clip.start_sample / SAMPLE_RATE * zoom, width: clipWidth, "--clip-color": track.color } as React.CSSProperties}
                onPointerDown={event => pointerEdit(event, clip, "move")}
                onContextMenu={event => { event.preventDefault(); event.stopPropagation(); focus(); if (!selectedIds.includes(clip.id)) select([clip.id]); setMenu({ x: Math.min(event.clientX, window.innerWidth - 224), y: Math.min(event.clientY, window.innerHeight - 260) }); }}
                title={`${clip.label} · ${timecode(clip.start_sample / SAMPLE_RATE)} · ${clipDuration(clip).toFixed(3)} 秒`}>
                <span className="trim-handle trim-start" title="拖动裁剪入点" onPointerDown={event => pointerEdit(event, clip, "start")} />
                <div className="clip-label">{clip.muted && <VolumeX size={11} />}{clip.label}</div>
                {asset?.media_url && <Waveform url={asset.media_url} height={29} color={track.color} start={clip.source_start_sample / SAMPLE_RATE} end={clip.source_end_sample / SAMPLE_RATE} compact />}
                <svg className="clip-fade-shape" width="100%" height="100%" preserveAspectRatio="none" aria-hidden="true">
                  {clip.fade_in_ms > 0 && <path d={`M0 57 L${Math.min(clipWidth, clip.fade_in_ms / 1000 * zoom)} 0 L0 0 Z`} />}
                  {clip.fade_out_ms > 0 && <path d={`M${clipWidth} 57 L${Math.max(0, clipWidth - clip.fade_out_ms / 1000 * zoom)} 0 L${clipWidth} 0 Z`} />}
                </svg>
                {clipWidth >= 28 && <><span className="fade-handle fade-in" style={{ left: clamp(clip.fade_in_ms / 1000 * zoom, 8, clipWidth - 16) }} title={`拖动调整淡入 · ${clip.fade_in_ms} 毫秒`} onPointerDown={event => pointerEdit(event, clip, "fade-in")} />
                  <span className="fade-handle fade-out" style={{ right: clamp(clip.fade_out_ms / 1000 * zoom, 8, clipWidth - 16) }} title={`拖动调整淡出 · ${clip.fade_out_ms} 毫秒`} onPointerDown={event => pointerEdit(event, clip, "fade-out")} /></>}
                <span className="trim-handle trim-end" title="拖动裁剪出点" onPointerDown={event => pointerEdit(event, clip, "end")} />
              </div>;
            })}
          </div>)}
          {guide !== null && <div className="snap-guide" style={{ left: guide / SAMPLE_RATE * zoom }}><span>吸附</span></div>}
          <div className="playhead" style={{ left: currentTime * zoom }}><i /></div>
        </div>
      </div>
    </div>
    <div className="timeline-status"><span>{hint || "拖动移动 · 边缘裁剪 · 顶角淡入淡出"}</span><span title="Shift / Ctrl 多选；左右方向键微调 10 毫秒，Shift 加速至 100 毫秒；B 分割；Ctrl+D 复制；Delete 移除；S 吸附">Shift 多选 · Alt 暂停吸附 · 右键更多</span></div>
    {menu && createPortal(<div className="timeline-context-menu" role="menu" aria-label="时间轴片段操作" style={{ left: Math.max(8, menu.x), top: Math.max(8, menu.y) }} onKeyDown={keyboard}>
      <div className="timeline-menu-title">已选 {selected.length} 个片段</div>
      <button role="menuitem" disabled={!canSplit} onClick={split}><Scissors size={14} />在播放指针处分割<kbd>B</kbd></button>
      <button role="menuitem" onClick={duplicate}><Copy size={14} />复制到选区之后<kbd>Ctrl+D</kbd></button>
      <button role="menuitem" onClick={mute}><VolumeX size={14} />{allMuted ? "取消静音" : "静音片段"}<kbd>M</kbd></button>
      <button role="menuitem" onClick={() => fit(true)}><Scan size={14} />放大选中片段</button>
      <button role="menuitem" onClick={() => { nudge(-480); setMenu(null); }}>向左微调 10 毫秒<kbd>←</kbd></button>
      <button role="menuitem" onClick={() => { nudge(480); setMenu(null); }}>向右微调 10 毫秒<kbd>→</kbd></button>
      <button role="menuitem" className="danger" onClick={remove}><Trash2 size={14} />移除片段，保留原音频<kbd>Del</kbd></button>
    </div>, document.body)}
  </section>;
}
