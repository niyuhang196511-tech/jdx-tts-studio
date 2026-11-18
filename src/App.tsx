import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { listen } from "@tauri-apps/api/event";
import {
  ArrowDown,
  ArrowUp,
  AudioLines,
  Check,
  ChevronDown,
  CircleHelp,
  Cloud,
  Copy,
  Download,
  FileAudio,
  FilePlus2,
  FileText,
  Film,
  Folder,
  FolderOpen,
  History,
  Layers,
  ListMusic,
  Loader2,
  Merge,
  Mic,
  Moon,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  Redo2,
  RefreshCw,
  Save,
  Scissors,
  Search,
  Settings2,
  SkipBack,
  Sparkles,
  Square,
  Star,
  Sun,
  Trash2,
  Undo2,
  Upload,
  Users,
  Video,
  Volume2,
  WandSparkles,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  connectBackend,
  isDesktop,
  openNative,
  post,
  patch,
  retryBackend,
  saveExport,
  subscribeEvents,
  syncCredentials,
} from "./api";
import type {
  Asset,
  AudioVersion,
  Bootstrap,
  DirectorSegment,
  ExportJob,
  GenerationJob,
  Line,
  LineSettings,
  Project,
  VoiceProfile,
} from "./types";
import {
  JOB_LABELS,
  SAMPLE_RATE,
  clipDuration,
  estimateGeneration,
  formatDate,
  newLine,
  reindexLines,
  referenceUsageCost,
  splitText,
  timecode,
  uid,
} from "./lib";
import { useUI } from "./store";
import { useProject } from "./useProject";
import { Button, Empty, Field, IconButton, Modal, StatusDot } from "./ui";
import { ConnectionModal } from "./ConnectionModal";
import { VoiceModal } from "./VoiceModal";
import { Inspector } from "./Inspector";
import { Timeline } from "./Timeline";
import { Waveform } from "./Waveform";
import { LiveAudioPlayer } from "./liveAudio";
import { SetupWizard } from "./SetupWizard";
import { ProjectLibrary } from "./ProjectLibrary";
import { VoiceLibraryPanel } from "./VoiceLibraryPanel";
import { defaultConnection } from "./voiceLibrary";

type Notice = { id: number; text: string; error: boolean };

export default function App() {
  const ui = useUI();
  const queryClient = useQueryClient();
  const [ready, setReady] = useState(false);
  const [startupError, setStartupError] = useState("");
  const [starting, setStarting] = useState(true);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [modal, setModal] = useState<
    | "new"
    | "connections"
    | "voice"
    | "text"
    | "replace"
    | "roles"
    | "director"
    | "export"
    | "about"
    | null
  >(null);
  const [busy, setBusy] = useState("");
  const [projectName, setProjectName] = useState("未命名配音项目");
  const [search, setSearch] = useState("");
  const [rawText, setRawText] = useState("");
  const [findText, setFindText] = useState("");
  const [replaceText, setReplaceText] = useState("");
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [transport, setTransport] = useState<"http" | "websocket">("http");
  const [generateMenu, setGenerateMenu] = useState(false);
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    lineId: string;
  } | null>(null);
  const [directorInstruction, setDirectorInstruction] = useState("");
  const [segments, setSegments] = useState<DirectorSegment[]>([]);
  const [directorWarnings, setDirectorWarnings] = useState<string[]>([]);
  const [exportFormat, setExportFormat] = useState("wav");
  const [exportName, setExportName] = useState("");
  const [sampleRate, setSampleRate] = useState(48000);
  const [normalize, setNormalize] = useState(true);
  const [exportJobs, setExportJobs] = useState<ExportJob[]>([]);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [livePlaying, setLivePlaying] = useState(false);
  const [playbackKind, setPlaybackKind] = useState<"timeline" | "asset">(
    "timeline",
  );
  const [playbackName, setPlaybackName] = useState("时间轴");
  const [currentUrl, setCurrentUrl] = useState("");
  const [rendering, setRendering] = useState(false);
  const [jobsOpen, setJobsOpen] = useState(false);
  const [compareA, setCompareA] = useState("");
  const [compareB, setCompareB] = useState("");
  const [hasNativeExport, setHasNativeExport] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  const setupChecked = useRef(false);
  const audio = useRef<HTMLAudioElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const restoreInput = useRef<HTMLInputElement>(null);
  const relinkInput = useRef<HTMLInputElement>(null);
  const relinkAssetId = useRef<string | null>(null);
  const renderCache = useRef<{
    revision: number;
    projectId: string;
    url: string;
  } | null>(null);
  const syncedConnections = useRef("");
  const liveAudio = useRef(new LiveAudioPlayer());
  const liveJob = useRef<string | null>(null);
  const notify = useCallback((text: string, error = false) => {
    const id = Date.now() + Math.random();
    setNotices((current) => [...current.slice(-3), { id, text, error }]);
    window.setTimeout(
      () => setNotices((current) => current.filter((value) => value.id !== id)),
      error ? 10000 : 5000,
    );
  }, []);
  const report = useCallback(
    (error: unknown) =>
      notify(error instanceof Error ? error.message : String(error), true),
    [notify],
  );
  const controller = useProject(ui.projectId, ready, report);
  const { project, edit, flush, replace, dirty, saving } = controller;
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: () => api<Bootstrap>("/bootstrap"),
    enabled: ready,
  });
  const bootstrap = bootstrapQuery.data;
  const preferredConnection = defaultConnection(bootstrap?.connections ?? [], bootstrap?.preferences.default_connection_id);
  const favoriteVoiceIds = Array.isArray(bootstrap?.preferences.favorite_voice_ids)
    ? bootstrap.preferences.favorite_voice_ids.filter((id): id is string => typeof id === "string")
    : (bootstrap?.voices.filter(voice => voice.favorite).map(voice => voice.id) ?? []);
  useEffect(() => {
    if (bootstrap && !setupChecked.current) {
      setupChecked.current = true;
      setSetupOpen(bootstrap.preferences.first_run_complete !== true);
    }
  }, [bootstrap]);
  const jobsQuery = useQuery({
    queryKey: ["jobs", ui.projectId],
    queryFn: ({ signal }) => api<GenerationJob[]>(`/projects/${ui.projectId}/tasks`, { signal }),
    enabled: ready && !!ui.projectId,
    refetchInterval: 1800,
    retry: (attempt, error) => !(error instanceof ApiError && [404, 410].includes(error.status)) && attempt < 2,
  });
  const jobs = jobsQuery.data ?? [];
  const activeJobs = jobs.filter((job) =>
    ["queued", "running", "cancel_requested"].includes(job.status),
  );
  const activeLine = project?.lines.find(
    (line) => line.id === ui.selectedIds[0],
  );
  const selectedClip = project?.clips.find(
    (clip) => clip.id === ui.selectedClip,
  );
  const selectedLines =
    project?.lines.filter((line) => ui.selectedIds.includes(line.id)) ?? [];
  const generationEstimate = estimateGeneration(
    ui.selectedIds.length ? selectedLines : (project?.lines ?? []),
    bootstrap?.connections ?? [],
  );
  const refreshBootstrap = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
  }, [queryClient]);
  const saveVoiceConfiguration = async (voice: VoiceProfile, duplicate: boolean) => {
    await run("voice-save", async () => {
      await post<VoiceProfile>("/voices", {
        id: duplicate ? uid() : voice.id,
        name: duplicate ? `${voice.name}（副本）` : voice.name,
        kind: voice.kind,
        connection_id: voice.connection_id ?? null,
        target_model: voice.target_model,
        voice_id: voice.voice_id,
        status: voice.status,
        notes: voice.notes,
        authorized: voice.authorized,
        workspace_id: voice.workspace_id ?? null,
        region: voice.region ?? null,
        ssml: voice.ssml ?? false,
        favorite: duplicate ? false : !voice.favorite,
      });
      refreshBootstrap();
      notify(duplicate ? "声音配置已复制到本地声音库" : "收藏状态已更新");
    });
  };
  const start = useCallback(async (retry = false) => {
    setStarting(true);
    setStartupError("");
    try {
      if (retry) await retryBackend();
      else await connectBackend();
      setReady(true);
    } catch (error) {
      setStartupError(
        error instanceof Error ? error.message : "本地服务启动失败",
      );
    } finally {
      setStarting(false);
    }
  }, []);
  useEffect(() => {
    void start();
  }, [start]);
  useEffect(() => {
    if (!isDesktop()) return;
    const listener = listen<{ state: string; message: string }>("backend-status", ({ payload }) => {
      if (payload.state === "failed" || payload.state === "stopped") {
        setReady(false);
        setStarting(false);
        setStartupError(payload.message);
        syncedConnections.current = "";
      }
    });
    return () => { void listener.then(unlisten => unlisten()); };
  }, []);
  useEffect(() => {
    setCompareA(activeLine?.selected_version_id ?? activeLine?.versions[0]?.id ?? "");
    setCompareB(activeLine?.versions.at(-1)?.id ?? "");
  }, [activeLine?.id]);
  useEffect(() => {
    document.documentElement.dataset.theme = ui.theme;
  }, [ui.theme]);
  useEffect(() => {
    const ids = bootstrap?.connections.map((connection) => connection.id) ?? [];
    const key = [...ids].sort().join(",");
    if (!isDesktop() || !key || syncedConnections.current === key) return;
    syncedConnections.current = key;
    void syncCredentials(ids).then(refreshBootstrap).catch(report);
  }, [bootstrap?.connections, report, refreshBootstrap]);
  useEffect(() => {
    if (controller.error instanceof ApiError && [404, 410].includes(controller.error.status)) {
      controller.clear();
      ui.setProject(null);
      refreshBootstrap();
      notify("上次打开的项目已删除或不存在，可从项目列表或回收站重新打开。");
    } else if (controller.error) report(controller.error);
  }, [controller.error, report]);
  useEffect(() => {
    const close = () => setContextMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, []);
  useEffect(() => {
    audio.current?.pause();
    video.current?.pause();
    liveAudio.current.stop();
    liveJob.current = null;
    setLivePlaying(false);
    setPlaying(false);
    setCurrentTime(0);
    setCurrentUrl("");
    renderCache.current = null;
  }, [ui.projectId]);
  useEffect(() => {
    if (!ready) return;
    const abort = new AbortController();
    const subscribe = async () => {
      while (!abort.signal.aborted) {
        try {
          await subscribeEvents((event) => {
            if (event.project_id !== ui.projectId) return;
            if (
              event.type === "audio_chunk" &&
              event.job_id === liveJob.current
            ) {
              liveAudio.current.append(
                String(event.audio),
                Number(event.sample_rate ?? 48000),
                Number(event.channels ?? 1),
              );
              setLivePlaying(true);
            } else if (event.type === "job") {
              void queryClient.invalidateQueries({
                queryKey: ["jobs", ui.projectId],
              });
              if (
                [
                  "succeeded",
                  "failed",
                  "cancelled",
                  "uncertain",
                  "interrupted",
                ].includes(String(event.status))
              ) {
                void queryClient.invalidateQueries({
                  queryKey: ["project", ui.projectId],
                });
                if (event.job_id === liveJob.current) {
                  if (event.status !== "succeeded") liveAudio.current.stop();
                  liveJob.current = null;
                  setLivePlaying(false);
                }
              }
            }
          }, abort.signal);
        } catch {
          if (liveJob.current && !abort.signal.aborted) {
            liveAudio.current.stop();
            liveJob.current = null;
            setLivePlaying(false);
            notify("实时试听连接中断，生成状态仍可在任务队列查看。", true);
          }
        }
        if (!abort.signal.aborted)
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
    };
    void subscribe();
    return () => abort.abort();
  }, [ready, ui.projectId, queryClient, notify]);
  useEffect(() => () => liveAudio.current.close(), []);

  const run = async (name: string, action: () => Promise<void>) => {
    setBusy(name);
    try {
      await action();
    } catch (error) {
      report(error);
    } finally {
      setBusy("");
    }
  };
  const switchProject = async (id: string) => {
    try {
      await flush();
      ui.setProject(id);
      setSearch("");
    } catch {
      /* Keep the current project when saving fails. */
    }
  };
  const createProject = () =>
    run("new", async () => {
      await flush();
      const created = await post<Project>("/projects", {
        name: projectName.trim() || "未命名配音项目",
      });
      ui.setProject(created.id);
      setModal(null);
      refreshBootstrap();
    });
  const importFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    if (!project) {
      notify("请先创建或打开项目。", true);
      return;
    }
    await run("import", async () => {
      await flush();
      for (const file of Array.from(files)) {
        const form = new FormData();
        form.append("file", file);
        const updated = await post<Project>(
          `/projects/${project.id}/import`,
          form,
        );
        replace(updated);
      }
      notify("素材已导入");
      refreshBootstrap();
    });
    if (fileInput.current) fileInput.current.value = "";
  };
  const addLines = (texts: string[]) => {
    if (!project) {
      setModal("new");
      return;
    }
    const lines = texts.map((text, index) =>
      newLine(text, project.lines.length + index, preferredConnection),
    );
    edit((value) => ({ ...value, lines: [...value.lines, ...lines] }));
    if (lines[0]) ui.select([lines[0].id]);
  };
  const updateLine = (value: Partial<Line>) =>
    edit((current) => ({
      ...current,
      lines: current.lines.map((line) =>
        ui.selectedIds.includes(line.id) ? { ...line, ...value } : line,
      ),
    }));
  const updateSettings = (settings: Partial<LineSettings>) =>
    edit((current) => ({
      ...current,
      lines: current.lines.map((line) =>
        ui.selectedIds.includes(line.id)
          ? { ...line, settings: { ...line.settings, ...settings } }
          : line,
      ),
    }));
  const deleteLines = (ids = ui.selectedIds) => {
    if (!ids.length) return;
    if (!confirm(`移除选中的 ${ids.length} 句台词？已生成的原始音频会保留。`))
      return;
    edit((current) => ({
      ...current,
      lines: reindexLines(
        current.lines.filter((line) => !ids.includes(line.id)),
      ),
    }));
    ui.select([]);
  };
  const splitLines = (ids = ui.selectedIds) =>
    edit((current) => ({
      ...current,
      lines: reindexLines(
        current.lines.flatMap((line) => {
          if (!ids.includes(line.id)) return [line];
          const parts = splitText(line.text);
          return parts.length < 2
            ? [line]
            : parts.map((text, index) =>
                index
                  ? {
                      ...newLine(text, line.position + index),
                      role_id: line.role_id,
                      settings: { ...line.settings },
                    }
                  : { ...line, text },
              );
        }),
      ),
    }));
  const mergeLines = () => {
    if (selectedLines.length < 2) return;
    const first = selectedLines[0];
    edit((current) => ({
      ...current,
      lines: reindexLines(
        current.lines.flatMap((line) =>
          line.id === first.id
            ? [
                {
                  ...line,
                  text: selectedLines.map((value) => value.text).join(""),
                },
              ]
            : ui.selectedIds.includes(line.id)
              ? []
              : [line],
        ),
      ),
    }));
    ui.select([first.id]);
  };
  const reorderLine = (id: string, direction: number) =>
    edit((current) => {
      const lines = [...current.lines];
      const index = lines.findIndex((line) => line.id === id);
      const other = index + direction;
      if (index < 0 || other < 0 || other >= lines.length) return current;
      [lines[index], lines[other]] = [lines[other], lines[index]];
      return { ...current, lines: reindexLines(lines) };
    });
  const duplicateLine = (id: string) =>
    edit((current) => ({
      ...current,
      lines: reindexLines(
        current.lines.flatMap((line) =>
          line.id === id
            ? [
                line,
                { ...line, id: uid(), versions: [], selected_version_id: null },
              ]
            : [line],
        ),
      ),
    }));
  const history = async (direction: "undo" | "redo") => {
    if (!project) return;
    await run(direction, async () => {
      await flush();
      replace(await post<Project>(`/projects/${project.id}/${direction}`));
    });
  };
  const generate = async (all = false) => {
    if (!project) {
      setModal("new");
      return;
    }
    if (
      !bootstrap?.connections.some((connection) => connection.has_credential)
    ) {
      setModal("connections");
      notify("请先配置百炼连接和 API Key。");
      return;
    }
    const ids = (all || !ui.selectedIds.length ? project.lines : selectedLines)
      .filter((line) => line.text.trim())
      .map((line) => line.id);
    if (!ids.length) {
      notify("请先添加台词。", true);
      return;
    }
    if (transport === "websocket") {
      try {
        audio.current?.pause();
        liveAudio.current.stop();
        await liveAudio.current.prepare();
      } catch {
        notify("实时试听设备不可用，生成结果仍会保存到项目。");
      }
    }
    setGenerateMenu(false);
    await run("generate", async () => {
      await flush();
      const result = await post<{ jobs: GenerationJob[] }>(
        `/projects/${project.id}/tasks`,
        { line_ids: ids, transport },
      );
      if (transport === "websocket") {
        liveJob.current =
          result.jobs.find((job) => job.line_id === (activeLine?.id ?? ids[0]))
            ?.id ??
          result.jobs[0]?.id ??
          null;
        setPlaybackName("实时生成试听");
      }
      await jobsQuery.refetch();
      notify(`已加入 ${ids.length} 句生成任务`);
      setJobsOpen(true);
    });
  };
  const selectVersion = async (
    line: Line,
    version: AudioVersion,
    best = false,
  ) => {
    if (!project) return;
    await run("version", async () => {
      await flush();
      replace(
        await post<Project>(
          `/projects/${project.id}/lines/${line.id}/versions/${version.id}/${best ? "best" : "select"}`,
        ),
      );
    });
  };
  const addAssetToTimeline = (asset: Asset, line?: Line) => {
    const end = Math.max(
      0,
      ...(project?.clips.map(
        (clip) => clip.start_sample / SAMPLE_RATE + clipDuration(clip),
      ) ?? []),
    );
    edit((current) => ({
      ...current,
      clips: [
        ...current.clips,
        {
          id: uid(),
          asset_id: asset.id,
          line_id: line?.id,
          track_id: line?.role_id ?? "main",
          label: line?.text.slice(0, 30) || asset.name,
          start_sample: Math.round(end * SAMPLE_RATE),
          source_start_sample: 0,
          source_end_sample: Math.round(asset.duration_ms * 48),
          gain_db: 0,
          fade_in_ms: 0,
          fade_out_ms: 0,
          muted: false,
          stretch: 1,
        },
      ],
    }));
    notify("已加入时间轴");
  };
  const playAsset = async (asset?: Asset, label?: string) => {
    if (!asset || asset.missing) {
      notify("音频素材不存在。", true);
      return;
    }
    await playUrl(asset.media_url, label ?? asset.name);
  };
  const playUrl = async (url: string, label: string) => {
    liveAudio.current.stop();
    liveJob.current = null;
    setLivePlaying(false);
    setPlaybackKind("asset");
    setPlaybackName(label);
    setCurrentUrl(url);
    setCurrentTime(0);
    if (audio.current) {
      audio.current.src = url;
      audio.current.currentTime = 0;
      try {
        await audio.current.play();
      } catch (error) {
        report(error);
      }
    }
  };
  const pollExport = async (
    projectId: string,
    id: string,
  ): Promise<ExportJob> => {
    for (let count = 0; count < 300; count++) {
      const result = await api<ExportJob>(
        `/projects/${projectId}/exports/${id}`,
      );
      setExportJobs((current) => [
        ...current.filter((value) => value.id !== id),
        result,
      ]);
      if (["succeeded", "completed", "done"].includes(result.status))
        return result;
      if (["failed", "cancelled"].includes(result.status))
        throw new Error(result.error ?? "音频处理失败");
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    throw new Error("任务仍在运行，请稍后重试查看结果。");
  };
  const playTimeline = async () => {
    if (livePlaying) {
      liveAudio.current.stop();
      liveJob.current = null;
      setLivePlaying(false);
      return;
    }
    if (playing) {
      audio.current?.pause();
      video.current?.pause();
      return;
    }
    if (!project?.clips.length) {
      notify("时间轴还没有音频片段。");
      return;
    }
    try {
      await flush();
      let url =
        renderCache.current?.projectId === project.id &&
        renderCache.current.revision === project.revision
          ? renderCache.current.url
          : "";
      if (!url) {
        setRendering(true);
        const response = await post<{ job_id: string }>(
          `/projects/${project.id}/render`,
        );
        const result = await pollExport(project.id, response.job_id);
        url =
          result.download_url ??
          project.assets.find((asset) => asset.id === result.asset_id)
            ?.media_url ??
          "";
        renderCache.current = {
          revision: project.revision,
          projectId: project.id,
          url,
        };
      }
      if (!url) throw new Error("试听文件尚未就绪。");
      setPlaybackKind("timeline");
      setPlaybackName("时间轴混音");
      setCurrentUrl(url);
      if (audio.current) {
        audio.current.src = url;
        audio.current.currentTime = currentTime;
        await audio.current.play();
      }
      if (video.current) {
        video.current.currentTime = currentTime;
        await video.current.play().catch(() => undefined);
      }
    } catch (error) {
      report(error);
    } finally {
      setRendering(false);
    }
  };
  const seek = (time: number) => {
    setCurrentTime(time);
    if (audio.current && playbackKind === "timeline" && currentUrl)
      audio.current.currentTime = time;
    if (video.current) video.current.currentTime = time;
  };
  const stop = () => {
    audio.current?.pause();
    video.current?.pause();
    liveAudio.current.stop();
    liveJob.current = null;
    setLivePlaying(false);
    seek(0);
  };
  const exportProject = () =>
    run("export", async () => {
      if (!project) return;
      await flush();
      const response = await post<{ job_id: string }>(
        `/projects/${project.id}/export`,
        {
          format: exportFormat,
          sample_rate: sampleRate,
          bit_depth: 24,
          normalize,
          filename: exportName || undefined,
        },
      );
      const result = await pollExport(project.id, response.job_id);
      if (!result.download_url)
        throw new Error("导出已完成，但未返回下载地址。");
      const extension = ["sentences", "project"].includes(exportFormat) ? "zip" : exportFormat;
      const saved = await saveExport(result.download_url, `${exportName || project.name}.${extension}`);
      if (saved.saved && isDesktop()) setHasNativeExport(true);
      notify(saved.saved ? "导出已保存" : "导出文件已生成，保存已取消");
    });
  const backupProject = () =>
    run("backup", async () => {
      if (!project) return;
      await flush();
      const response = await post<{ job_id: string }>(
        `/projects/${project.id}/backup`,
      );
      const result = await pollExport(project.id, response.job_id);
      if (result.download_url) {
        const saved = await saveExport(result.download_url, `${project.name}.zip`);
        if (saved.saved && isDesktop()) setHasNativeExport(true);
        notify(saved.saved ? "项目备份已保存" : "备份文件已生成，保存已取消");
      }
    });
  const analyzeDirector = () =>
    run("director", async () => {
      if (!project || !activeLine) return;
      const result = await post<{
        segments: DirectorSegment[];
        warnings: string[];
      }>(`/projects/${project.id}/director`, {
        text: activeLine.text,
        instruction: directorInstruction,
      });
      setSegments(result.segments);
      setDirectorWarnings(result.warnings);
    });
  const applyDirector = () => {
    if (!activeLine || !segments.length) return;
    const original = activeLine;
    edit((current) => ({
      ...current,
      lines: reindexLines(
        current.lines.flatMap((line) =>
          line.id !== original.id
            ? [line]
            : segments.map((segment, index) => ({
                ...(index
                  ? newLine(segment.text, original.position + index)
                  : original),
                text: segment.text,
                role_id: original.role_id,
                settings: {
                  ...original.settings,
                  instruction: segment.instruction,
                  pause_after_ms: segment.pause_after_ms,
                },
              })),
        ),
      ),
    }));
    setModal(null);
    notify("导演计划已应用");
  };
  const generatedCount =
    project?.lines.filter((line) => line.selected_version_id).length ?? 0;
  const totalDuration = Math.max(
    0,
    ...(project?.clips.map(
      (clip) => clip.start_sample / SAMPLE_RATE + clipDuration(clip),
    ) ?? []),
  );
  const filteredLines = useMemo(
    () =>
      project?.lines.filter(
        (line) =>
          !search ||
          line.text.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
      ) ?? [],
    [project?.lines, search],
  );
  const videoAsset = project?.assets.find(
    (asset) =>
      asset.id === project.video_asset_id ||
      (!project.video_asset_id && asset.kind === "video"),
  );
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      const typing =
        event.target instanceof HTMLElement &&
        (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) ||
          event.target.isContentEditable);
      const modifier = event.ctrlKey || event.metaKey;
      if (document.querySelector('[role="dialog"]')) return;
      if (modifier && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void flush()
          .then(() => notify("项目已保存"))
          .catch(() => undefined);
      } else if (modifier && event.key.toLowerCase() === "z" && !typing) {
        event.preventDefault();
        void history(event.shiftKey ? "redo" : "undo");
      } else if (modifier && event.key === "Enter") {
        event.preventDefault();
        void generate();
      } else if (event.code === "Space" && !typing && !modal) {
        event.preventDefault();
        void playTimeline();
      } else if (event.key === "Escape") {
        setContextMenu(null);
        setGenerateMenu(false);
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  });

  if (!ready)
    return (
      <div className="startup">
        <div className="startup-mark">
          <AudioLines size={34} />
        </div>
        <h1>Jdx TTS Studio</h1>
        <p>{starting ? "正在连接本地工作站…" : "本地服务未能启动"}</p>
        {starting ? (
          <Loader2 className="spin muted" size={22} />
        ) : (
          <>
            <div className="startup-error">{startupError}</div>
            <Button variant="primary" onClick={() => void start(true)}>
              <RefreshCw size={15} />
              重试启动
            </Button>
            {isDesktop() && (
              <Button
                variant="ghost"
                onClick={() => void openNative("open_logs").catch(report)}
              >
                查看日志
              </Button>
            )}
          </>
        )}
      </div>
    );

  return (
    <div className="studio-shell">
      <header className="app-header">
        <div className="brand">
          <div className="brand-mark">
            <AudioLines size={21} />
          </div>
          <div>
            <strong>Jdx</strong>
            <span>TTS STUDIO</span>
          </div>
        </div>
        <div className="header-divider" />
        <div className="project-heading">
          <span className="project-heading-name">
            {project?.name ?? "配音工作站"}
          </span>
          <span className="save-state">
            {saving ? (
              <Loader2 size={11} className="spin" />
            ) : dirty ? (
              <span className="unsaved-dot" />
            ) : (
              <Check size={11} />
            )}
            {saving ? "保存中" : dirty ? "待保存" : project ? "已保存" : "就绪"}
          </span>
        </div>
        <div className="header-spacer" />
        <div className="cloud-state">
          <StatusDot
            state={
              bootstrap?.connections.some((value) => value.has_credential)
                ? "ok"
                : "warn"
            }
          />
          <span>
            {bootstrap?.connections.find((value) => value.has_credential)
              ?.name ?? "未配置云端连接"}
          </span>
        </div>
        <IconButton
          label="云端模型设置"
          onClick={() => setModal("connections")}
        >
          <Settings2 size={18} />
        </IconButton>
        <div className="header-divider" />
        <div className="generate-group">
          <Button
            variant="primary"
            busy={busy === "generate"}
            onClick={() => void generate()}
            disabled={!project}
          >
            <Sparkles size={15} />
            生成配音
          </Button>
          <button
            className="generate-chevron"
            aria-label="生成选项"
            onClick={() => setGenerateMenu((value) => !value)}
          >
            <ChevronDown size={14} />
          </button>
          {generateMenu && (
            <div className="dropdown generate-dropdown">
              <div className="estimate-note">
                待生成约 {generationEstimate.characters.toLocaleString("zh-CN")} 字符
                <small>{generationEstimate.unknown ? "部分台词未配置字符单价，或按 Token 计费，暂不估算总额。" : `按参考单价估算 ¥${generationEstimate.amount.toFixed(4)}`}</small>
              </div>
              <button onClick={() => void generate(true)}>
                <ListMusic size={15} />
                生成全部台词
              </button>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={transport === "websocket"}
                  onChange={(event) =>
                    setTransport(event.target.checked ? "websocket" : "http")
                  }
                />
                流式生成
              </label>
            </div>
          )}
        </div>
        <Button
          onClick={() => {
            setExportName(project?.name ?? "");
            setModal("export");
          }}
          disabled={!project}
        >
          <Download size={15} />
          导出
        </Button>
      </header>
      <div className="main-toolbar">
        <div className="toolbar-left">
          <Button variant="ghost" onClick={() => setModal("new")}>
            <FilePlus2 size={14} />
            新建项目
          </Button>
          <Button
            variant="ghost"
            onClick={() => fileInput.current?.click()}
            disabled={!project}
          >
            <Upload size={14} />
            导入素材
          </Button>
          <div className="separator" />
          <IconButton
            label="保存项目"
            disabled={!project || saving}
            onClick={() =>
              void flush()
                .then(() => notify("项目已保存"))
                .catch(() => undefined)
            }
          >
            <Save size={15} />
          </IconButton>
          <IconButton
            label="撤销"
            disabled={!project?.can_undo || !!busy}
            onClick={() => void history("undo")}
          >
            <Undo2 size={15} />
          </IconButton>
          <IconButton
            label="重做"
            disabled={!project?.can_redo || !!busy}
            onClick={() => void history("redo")}
          >
            <Redo2 size={15} />
          </IconButton>
        </div>
        <div className="toolbar-right">
          <span className="small muted">
            {project
              ? `${project.lines.length} 句台词 · ${generatedCount} 句已生成`
              : "未打开项目"}
          </span>
          <div className="separator" />
          <IconButton
            label={ui.theme === "dark" ? "切换浅色主题" : "切换深色主题"}
            onClick={() => {
              const theme = ui.theme === "dark" ? "light" : "dark";
              ui.toggleTheme();
              void api("/preferences", { method: "PATCH", body: JSON.stringify({ theme }) }).catch(report);
            }}
          >
            {ui.theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </IconButton>
          <IconButton label="关于与日志" onClick={() => setModal("about")}>
            <CircleHelp size={15} />
          </IconButton>
        </div>
      </div>
      <div className="workspace">
        <PanelGroup direction="horizontal" autoSaveId="jdx-workspace-v1">
          <Panel defaultSize={18} minSize={13} maxSize={28}>
            <aside className="resource-panel">
              <div className="resource-tabs">
                {(
                  [
                    ["projects", Folder, "项目"],
                    ["voices", Mic, "声音"],
                    ["assets", Layers, "素材"],
                  ] as const
                ).map(([value, Icon, label]) => (
                  <button
                    key={value}
                    className={ui.resource === value ? "active" : ""}
                    onClick={() => ui.setResource(value)}
                  >
                    <Icon size={15} />
                    {label}
                  </button>
                ))}
              </div>
              {ui.resource === "projects" ? (
                <ProjectLibrary projects={bootstrap?.projects ?? []} currentId={ui.projectId}
                  disabled={!!busy || rendering} hasProject={!!project}
                  onOpen={switchProject} onCreate={() => setModal("new")} onBackup={backupProject}
                  onRestoreBackup={() => restoreInput.current?.click()} beforeChange={flush}
                  onUpdated={value => { if (value.id === ui.projectId) replace(value); }}
                  onDeleted={id => {
                    if (id !== ui.projectId) return;
                    stop();
                    controller.clear();
                    ui.setProject(null);
                    setSearch("");
                    setJobsOpen(false);
                    setExportJobs([]);
                  }}
                  onRestored={value => { ui.setProject(value.id); setSearch(""); }}
                  onBusy={value => setBusy(value ? "project-management" : "")} notify={notify} />
              ) : ui.resource === "voices" ? (
                <VoiceLibraryPanel voices={bootstrap?.voices ?? []} models={bootstrap?.models ?? []}
                  preferredModel={activeLine?.settings.model_id ?? preferredConnection?.model_id ?? "qwen-audio-3.1-tts-plus"}
                  currentRegion={(bootstrap?.connections.find(item => item.id === activeLine?.settings.connection_id) ?? preferredConnection)?.region}
                  currentVoice={activeLine?.settings.voice_id} favoriteIds={favoriteVoiceIds}
                  selectedCount={selectedLines.length} hasProject={!!project}
                  onUse={voice => {
                    edit(value => ({ ...value, lines: value.lines.map(line => ui.selectedIds.includes(line.id) ? {
                      ...line, settings: { ...line.settings, voice_id: voice.voice_id, model_id: voice.target_model,
                        connection_id: voice.connection_id ?? line.settings.connection_id ?? preferredConnection?.id ?? null },
                    } : line) }));
                    notify(`已为 ${selectedLines.length} 句应用“${voice.name}”`);
                  }}
                  onFavorite={async (ids, favorite) => {
                    const next = favorite ? [...new Set([...favoriteVoiceIds, ...ids])] : favoriteVoiceIds.filter(id => !ids.includes(id));
                    const preferences = await patch<Record<string, unknown>>("/preferences", { favorite_voice_ids: next });
                    queryClient.setQueryData<Bootstrap>(["bootstrap"], value => value && ({ ...value, preferences }));
                  }}
                  onCopy={voice => saveVoiceConfiguration(voice, true)}
                  onRefresh={voice => run("voice-refresh", async () => { await post(`/voices/${encodeURIComponent(voice.id)}/refresh`); refreshBootstrap(); })}
                  onDelete={async voice => {
                    if (!confirm(`移除“${voice.name}”的本地配置？已生成音频会保留。`)) return;
                    await run("voice-delete", async () => { await api(`/voices/${encodeURIComponent(voice.id)}`, { method: "DELETE" }); refreshBootstrap(); });
                  }}
                  onCreate={() => setModal("voice")} onRoles={() => setModal("roles")}
                  onPreviewStart={stop} externalPlaybackActive={playing || livePlaying || rendering} notify={notify} />
              ) : (
                <>
                  <div className="resource-heading">
                    <span>
                      项目素材{" "}
                      <b>
                        {project?.assets.filter(
                          (asset) => asset.kind !== "export",
                        ).length ?? 0}
                      </b>
                    </span>
                    <IconButton
                      label="导入音视频素材"
                      disabled={!project}
                      onClick={() => fileInput.current?.click()}
                    >
                      <Plus size={15} />
                    </IconButton>
                  </div>
                  <div className="resource-scroll">
                    {project?.assets
                      .filter((asset) => asset.kind !== "export")
                      .map((asset) => (
                        <div className="asset-item" key={asset.id}>
                          <div className="asset-symbol">
                            {asset.kind === "video" ? (
                              <Film size={17} />
                            ) : (
                              <FileAudio size={17} />
                            )}
                          </div>
                          <span>
                            <strong>{asset.name}</strong>
                            <small>
                              {asset.missing
                                ? "素材缺失"
                                : timecode(asset.duration_ms / 1000)}
                            </small>
                          </span>
                          {asset.missing ? (
                            <IconButton label="重新定位缺失素材" onClick={() => {
                              relinkAssetId.current = asset.id;
                              relinkInput.current?.click();
                            }}><FolderOpen size={14} /></IconButton>
                          ) : asset.kind === "audio" ? (
                            <>
                              <IconButton
                                label="试听素材"
                                disabled={asset.missing}
                                onClick={() => void playAsset(asset)}
                              >
                                <Play size={12} />
                              </IconButton>
                              <IconButton
                                label="加入时间轴"
                                disabled={asset.missing}
                                onClick={() => addAssetToTimeline(asset)}
                              >
                                <Plus size={12} />
                              </IconButton>
                            </>
                          ) : (
                            <IconButton
                              label="打开视频预览"
                              onClick={() => {
                                edit((value) => ({
                                  ...value,
                                  video_asset_id: asset.id,
                                }));
                                ui.setTab("video");
                              }}
                            >
                              <Video size={13} />
                            </IconButton>
                          )}
                        </div>
                      ))}
                    {!project?.assets.length && (
                      <div className="small muted resource-empty">
                        暂无音视频素材
                      </div>
                    )}
                  </div>
                </>
              )}
            </aside>
          </Panel>
          <PanelResizeHandle className="resize-handle" />
          <Panel defaultSize={61} minSize={35}>
            <PanelGroup
              direction="vertical"
              autoSaveId="jdx-editor-timeline-v1"
            >
              <Panel defaultSize={67} minSize={30}>
                <main className="editor-panel">
                  <div className="editor-tabs">
                    <div className="tab-list">
                      {(
                        [
                          ["lines", FileText, "台词编辑"],
                          ["video", Film, "视频预览"],
                          ["history", History, "生成历史"],
                        ] as const
                      ).map(([value, Icon, label]) => (
                        <button
                          key={value}
                          className={ui.tab === value ? "active" : ""}
                          onClick={() => ui.setTab(value)}
                        >
                          <Icon size={14} />
                          {label}
                          {value === "lines" && (
                            <span>{project?.lines.length ?? 0}</span>
                          )}
                        </button>
                      ))}
                    </div>
                    <IconButton
                      label="管理项目角色"
                      disabled={!project}
                      onClick={() => setModal("roles")}
                    >
                      <Users size={15} />
                    </IconButton>
                  </div>
                  {ui.tab === "lines" ? (
                    <>
                      <div className="line-toolbar">
                        <div className="search-input">
                          <Search size={14} />
                          <input
                            aria-label="搜索台词"
                            value={search}
                            onChange={(event) => setSearch(event.target.value)}
                            placeholder="搜索台词"
                          />
                          {search && (
                            <button
                              aria-label="清除搜索"
                              onClick={() => setSearch("")}
                            >
                              <X size={12} />
                            </button>
                          )}
                        </div>
                        <div className="line-tools">
                          <IconButton
                            label="搜索替换"
                            disabled={!project?.lines.length}
                            onClick={() => setModal("replace")}
                          >
                            <RefreshCw size={14} />
                          </IconButton>
                          <IconButton
                            label="自动分句"
                            disabled={!selectedLines.length}
                            onClick={() => splitLines()}
                          >
                            <Scissors size={14} />
                          </IconButton>
                          <IconButton
                            label="合并台词"
                            disabled={selectedLines.length < 2}
                            onClick={mergeLines}
                          >
                            <Merge size={14} />
                          </IconButton>
                          <IconButton
                            label="删除选中台词"
                            disabled={!selectedLines.length}
                            onClick={() => deleteLines()}
                          >
                            <Trash2 size={14} />
                          </IconButton>
                          <div className="separator" />
                          <Button
                            variant="ghost"
                            onClick={() => setModal("text")}
                            disabled={!project}
                          >
                            <FileText size={14} />
                            粘贴文本
                          </Button>
                          <Button
                            onClick={() => addLines([""])}
                            disabled={!project}
                          >
                            <Plus size={14} />
                            添加台词
                          </Button>
                        </div>
                      </div>
                      {project?.lines.length ? (
                        <div className="line-table">
                          <div className="line-table-header">
                            <label>
                              <input
                                type="checkbox"
                                aria-label="选择全部台词"
                                checked={
                                  !!filteredLines.length &&
                                  filteredLines.every((line) =>
                                    ui.selectedIds.includes(line.id),
                                  )
                                }
                                onChange={(event) =>
                                  ui.select(
                                    event.target.checked
                                      ? filteredLines.map((line) => line.id)
                                      : [],
                                  )
                                }
                              />
                            </label>
                            <span>序号</span>
                            <span>台词内容</span>
                            <span>声音 / 状态</span>
                            <span />
                          </div>
                          <div className="line-table-body">
                            {filteredLines.map((line) => {
                              const role = project.roles.find(
                                (value) => value.id === line.role_id,
                              );
                              const version = line.versions.find(
                                (value) =>
                                  value.id === line.selected_version_id,
                              );
                              const job = [...jobs]
                                .reverse()
                                .find((value) => value.line_id === line.id);
                              const running =
                                job &&
                                [
                                  "queued",
                                  "running",
                                  "cancel_requested",
                                ].includes(job.status);
                              return (
                                <div
                                  key={line.id}
                                  className={`line-row ${ui.selectedIds.includes(line.id) ? "selected" : ""}`}
                                  onClick={() => {
                                    ui.select([line.id]);
                                    ui.setClip(null);
                                  }}
                                  onContextMenu={(event) => {
                                    event.preventDefault();
                                    ui.select([line.id]);
                                    setContextMenu({
                                      x: Math.min(
                                        event.clientX,
                                        window.innerWidth - 210,
                                      ),
                                      y: Math.min(
                                        event.clientY,
                                        window.innerHeight - 260,
                                      ),
                                      lineId: line.id,
                                    });
                                  }}
                                >
                                  <label
                                    className="line-check"
                                    onClick={(event) => event.stopPropagation()}
                                  >
                                    <input
                                      type="checkbox"
                                      aria-label={`选择第 ${line.position + 1} 句`}
                                      checked={ui.selectedIds.includes(line.id)}
                                      onChange={(event) => {
                                        ui.setClip(null);
                                        ui.select(
                                          event.target.checked
                                            ? [...ui.selectedIds, line.id]
                                            : ui.selectedIds.filter(
                                                (id) => id !== line.id,
                                              ),
                                        );
                                      }}
                                    />
                                  </label>
                                  <span className="line-number">
                                    {String(line.position + 1).padStart(2, "0")}
                                  </span>
                                  <div className="line-text-cell">
                                    <textarea
                                      rows={Math.min(
                                        5,
                                        Math.max(
                                          2,
                                          Math.ceil(line.text.length / 40),
                                        ),
                                      )}
                                      aria-label={`第 ${line.position + 1} 句台词`}
                                      value={line.text}
                                      placeholder="输入台词…"
                                      onFocus={() => {
                                        if (!ui.selectedIds.includes(line.id))
                                          ui.select([line.id]);
                                        ui.setClip(null);
                                      }}
                                      onClick={(event) =>
                                        event.stopPropagation()
                                      }
                                      onChange={(event) =>
                                        edit((value) => ({
                                          ...value,
                                          lines: value.lines.map((item) =>
                                            item.id === line.id
                                              ? {
                                                  ...item,
                                                  text: event.target.value,
                                                }
                                              : item,
                                          ),
                                        }))
                                      }
                                    />
                                    <div className="line-meta">
                                      {line.subtitle_start_ms != null && (
                                        <span>
                                          {timecode(
                                            line.subtitle_start_ms / 1000,
                                          )}{" "}
                                          →{" "}
                                          {timecode(
                                            (line.subtitle_end_ms ??
                                              line.subtitle_start_ms) / 1000,
                                          )}
                                        </span>
                                      )}
                                      {line.settings.instruction && (
                                        <span>
                                          <Sparkles size={10} />
                                          {line.settings.instruction}
                                        </span>
                                      )}
                                      {line.settings.target_duration_ms &&
                                        version && (
                                          <span
                                            className={
                                              Math.abs(
                                                version.duration_ms -
                                                  line.settings
                                                    .target_duration_ms,
                                              ) > 300
                                                ? "warning-text"
                                                : ""
                                            }
                                          >
                                            目标{" "}
                                            {(
                                              line.settings.target_duration_ms /
                                              1000
                                            ).toFixed(1)}
                                            s · 实际{" "}
                                            {(
                                              version.duration_ms / 1000
                                            ).toFixed(1)}
                                            s
                                          </span>
                                        )}
                                    </div>
                                  </div>
                                  <div className="line-voice-status">
                                    <span className="role-label">
                                      <i
                                        style={{
                                          background: role?.color ?? "#7e8d9d",
                                        }}
                                      />
                                      {role?.name ?? "未分配角色"}
                                    </span>
                                    <small>
                                      {bootstrap?.voices.find(
                                        (value) =>
                                          value.voice_id ===
                                          line.settings.voice_id,
                                      )?.name ?? line.settings.voice_id}
                                    </small>
                                    <span
                                      className={`line-status ${running ? "pending" : version ? "complete" : "not-generated"}`}
                                    >
                                      {running ? (
                                        <>
                                          <Loader2 size={10} className="spin" />
                                          {JOB_LABELS[job.status]}
                                        </>
                                      ) : version ? (
                                        <>
                                          <Check size={10} />
                                          {version.text_revision !==
                                          line.revision
                                            ? "文本已修改"
                                            : `${(version.duration_ms / 1000).toFixed(1)}s · 已生成`}
                                        </>
                                      ) : (
                                        "待生成"
                                      )}
                                    </span>
                                    {job?.status === "failed" && (
                                      <span
                                        className="error-text small"
                                        title={job.error ?? ""}
                                      >
                                        生成失败
                                      </span>
                                    )}
                                  </div>
                                  <div className="row-actions">
                                    <IconButton
                                      label="试听当前版本"
                                      disabled={!version}
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        if (version)
                                          void playAsset(
                                            project.assets.find(
                                              (asset) =>
                                                asset.id === version.asset_id,
                                            ),
                                            line.text.slice(0, 25),
                                          );
                                      }}
                                    >
                                      <Play size={14} />
                                    </IconButton>
                                    <IconButton
                                      label="台词操作"
                                      onClick={(event) => {
                                        event.stopPropagation();
                                        ui.select([line.id]);
                                        const bounds =
                                          event.currentTarget.getBoundingClientRect();
                                        setContextMenu({
                                          x: Math.min(
                                            bounds.left,
                                            window.innerWidth - 210,
                                          ),
                                          y: Math.min(
                                            bounds.bottom,
                                            window.innerHeight - 260,
                                          ),
                                          lineId: line.id,
                                        });
                                      }}
                                    >
                                      <MoreHorizontal size={16} />
                                    </IconButton>
                                  </div>
                                </div>
                              );
                            })}
                            {!filteredLines.length && (
                              <div className="search-empty">没有匹配的台词</div>
                            )}
                            <button
                              className="add-line-footer"
                              onClick={() => addLines([""])}
                            >
                              <Plus size={14} />
                              添加一句台词
                            </button>
                          </div>
                        </div>
                      ) : (
                        <Empty
                          icon={<FileText size={34} />}
                          title={project ? "还没有台词" : "尚未打开项目"}
                          action={
                            project ? (
                              <div className="empty-actions">
                                <Button
                                  variant="primary"
                                  onClick={() => setModal("text")}
                                >
                                  <Plus size={15} />
                                  添加台词
                                </Button>
                                <Button
                                  onClick={() => fileInput.current?.click()}
                                >
                                  <Upload size={15} />
                                  导入文本或字幕
                                </Button>
                              </div>
                            ) : (
                              <Button
                                variant="primary"
                                onClick={() => setModal("new")}
                              >
                                <FilePlus2 size={15} />
                                创建配音项目
                              </Button>
                            )
                          }
                        />
                      )}
                    </>
                  ) : ui.tab === "video" ? (
                    <div className="video-workspace">
                      {videoAsset ? (
                        <>
                          <div className="video-stage">
                            <video
                              key={videoAsset.id}
                              ref={video}
                              src={videoAsset.media_url}
                              controls
                              onTimeUpdate={() => {
                                if (video.current && !playing)
                                  setCurrentTime(video.current.currentTime);
                              }}
                              onPlay={() => {
                                if (
                                  audio.current &&
                                  playbackKind === "timeline" &&
                                  currentUrl
                                ) {
                                  audio.current.currentTime =
                                    video.current?.currentTime ?? 0;
                                  void audio.current.play().catch(report);
                                }
                              }}
                              onPause={() => audio.current?.pause()}
                            />
                          </div>
                          <div className="video-caption">
                            <Film size={14} />
                            <span>{videoAsset.name}</span>
                            <b>{timecode(currentTime)}</b>
                          </div>
                          <div className="subtitle-strip">
                            {project?.lines
                              .filter((line) => line.subtitle_start_ms != null)
                              .map((line) => (
                                <button
                                  key={line.id}
                                  className={
                                    currentTime * 1000 >=
                                      (line.subtitle_start_ms ?? 0) &&
                                    currentTime * 1000 <
                                      (line.subtitle_end_ms ?? 0)
                                      ? "active"
                                      : ""
                                  }
                                  onClick={() => {
                                    seek((line.subtitle_start_ms ?? 0) / 1000);
                                    ui.select([line.id]);
                                  }}
                                >
                                  <span>
                                    {timecode(
                                      (line.subtitle_start_ms ?? 0) / 1000,
                                    )}
                                  </span>
                                  {line.text}
                                </button>
                              ))}
                          </div>
                        </>
                      ) : (
                        <Empty
                          icon={<Film size={34} />}
                          title="尚未导入视频"
                          action={
                            <Button
                              disabled={!project}
                              onClick={() => fileInput.current?.click()}
                            >
                              <Upload size={15} />
                              导入视频
                            </Button>
                          }
                        />
                      )}
                    </div>
                  ) : (
                    <div className="history-workspace">
                      {activeLine ? (
                        <>
                          <div className="history-heading">
                            <span className="eyebrow">
                              第 {activeLine.position + 1} 句
                            </span>
                            <p>{activeLine.text}</p>
                            <span className="muted small">
                              {activeLine.versions.length} 个音频版本
                            </span>
                          </div>
                          {activeLine.versions.length > 1 && (
                            <div className="comparison-row">
                              {[{ label: "A", value: compareA, set: setCompareA }, { label: "B", value: compareB, set: setCompareB }].map(item => (
                                <div className="comparison-choice" key={item.label}>
                                  <Field label={`对比版本 ${item.label}`}>
                                    <select value={item.value} onChange={event => item.set(event.target.value)}>
                                      {activeLine.versions.map((version, index) => <option key={version.id} value={version.id}>V{index + 1} · {(version.duration_ms / 1000).toFixed(2)} 秒</option>)}
                                    </select>
                                  </Field>
                                  <Button onClick={() => {
                                    const version = activeLine.versions.find(value => value.id === item.value);
                                    void playAsset(project?.assets.find(asset => asset.id === version?.asset_id), `对比 ${item.label}`);
                                  }}><Play size={14} />试听 {item.label}</Button>
                                </div>
                              ))}
                            </div>
                          )}
                          {activeLine.versions.length ? (
                            activeLine.versions.map((version, index) => (
                              <div
                                className={`version-row ${activeLine.selected_version_id === version.id ? "selected" : ""}`}
                                key={version.id}
                              >
                                <div className="version-mark">V{index + 1}</div>
                                <div className="version-content">
                                  <div className="version-title">
                                    <strong>
                                      {formatDate(version.created_at)}
                                    </strong>
                                    <span>
                                      {(version.duration_ms / 1000).toFixed(2)}{" "}
                                      秒
                                    </span>
                                    {version.best && (
                                      <Star size={12} className="star-filled" />
                                    )}
                                    {activeLine.selected_version_id ===
                                      version.id && (
                                      <span className="badge badge-teal">
                                        当前版本
                                      </span>
                                    )}
                                  </div>
                                  <small>
                                    {version.model_id} · 语速{" "}
                                    {version.settings.rate}× ·{" "}
                                    {version.settings.emotion}
                                  </small>
                                  <div className="version-wave">
                                    {project?.assets.find(
                                      (asset) => asset.id === version.asset_id,
                                    )?.media_url && (
                                      <Waveform
                                        url={
                                          project.assets.find(
                                            (asset) =>
                                              asset.id === version.asset_id,
                                          )!.media_url
                                        }
                                        height={36}
                                      />
                                    )}
                                  </div>
                                  <details>
                                    <summary>生成参数与用量</summary>
                                    {(() => {
                                      const amount = referenceUsageCost(version.model_id, version.usage,
                                        bootstrap?.connections.find(connection => connection.id === version.settings.connection_id));
                                      return amount == null ? null : <p className="muted small">按当前参考单价及返回用量估算 ¥{amount.toFixed(4)}；实际费用以账单为准。</p>;
                                    })()}
                                    <pre>
                                      {JSON.stringify(
                                        {
                                          ...version.settings,
                                          request_id: version.request_id,
                                          usage: version.usage,
                                          text_revision: version.text_revision,
                                        },
                                        null,
                                        2,
                                      )}
                                    </pre>
                                  </details>
                                </div>
                                <div className="version-actions">
                                  <IconButton
                                    label={`试听版本 ${index + 1}`}
                                    onClick={() =>
                                      void playAsset(
                                        project?.assets.find(
                                          (asset) =>
                                            asset.id === version.asset_id,
                                        ),
                                        `V${index + 1} · ${activeLine.text.slice(0, 15)}`,
                                      )
                                    }
                                  >
                                    <Play size={15} />
                                  </IconButton>
                                  <IconButton
                                    label="标记最佳版本"
                                    onClick={() =>
                                      void selectVersion(
                                        activeLine,
                                        version,
                                        true,
                                      )
                                    }
                                  >
                                    <Star
                                      size={15}
                                      className={
                                        version.best ? "star-filled" : ""
                                      }
                                    />
                                  </IconButton>
                                  <IconButton
                                    label="加入时间轴"
                                    onClick={() => {
                                      const asset = project?.assets.find(
                                        (value) =>
                                          value.id === version.asset_id,
                                      );
                                      if (asset)
                                        addAssetToTimeline(asset, activeLine);
                                    }}
                                  >
                                    <Plus size={15} />
                                  </IconButton>
                                  <Button
                                    disabled={
                                      activeLine.selected_version_id ===
                                        version.id || busy === "version"
                                    }
                                    onClick={() =>
                                      void selectVersion(activeLine, version)
                                    }
                                  >
                                    使用此版
                                  </Button>
                                </div>
                              </div>
                            ))
                          ) : (
                            <Empty
                              icon={<History size={28} />}
                              title="此句尚无生成版本"
                              action={
                                <Button onClick={() => void generate()}>
                                  <Sparkles size={14} />
                                  生成配音
                                </Button>
                              }
                            />
                          )}
                        </>
                      ) : (
                        <Empty
                          icon={<History size={30} />}
                          title="选择一句台词查看历史"
                        />
                      )}
                    </div>
                  )}
                </main>
              </Panel>
              <PanelResizeHandle className="resize-handle horizontal" />
              <Panel defaultSize={33} minSize={20}>
                <Timeline
                  project={project}
                  selectedId={ui.selectedClip}
                  onSelect={(id) => {
                    ui.setClip(id);
                  }}
                  onEdit={edit}
                  currentTime={currentTime}
                  onSeek={seek}
                />
              </Panel>
            </PanelGroup>
          </Panel>
          <PanelResizeHandle className="resize-handle" />
          <Panel defaultSize={21} minSize={18} maxSize={31}>
            <Inspector
              project={project}
              line={activeLine}
              clip={selectedClip}
              bootstrap={bootstrap}
              selectedCount={selectedLines.length}
              onSettings={updateSettings}
              onLine={updateLine}
              onClip={(value) =>
                edit((current) => ({
                  ...current,
                  clips: current.clips.map((clip) =>
                    clip.id === ui.selectedClip ? { ...clip, ...value } : clip,
                  ),
                }))
              }
              onCopyPrevious={() => {
                if (!activeLine || !project) return;
                const index = project.lines.findIndex(
                  (line) => line.id === activeLine.id,
                );
                if (index > 0)
                  updateSettings({ ...project.lines[index - 1].settings });
              }}
              onBrowseVoices={() => ui.setResource("voices")}
                onPreviewVoice={voice => run("voice-preview", async () => {
                  const result = await post<VoiceProfile>(`/voices/${encodeURIComponent(voice.id)}/preview`);
                  if (!result.preview_url) throw new Error("此声音暂无可用样音，请在声音库选择可试听的声音。");
                  await playUrl(result.preview_url, voice.name);
                })}
                onDirector={() => {
                setDirectorInstruction(activeLine?.settings.instruction ?? "");
                setSegments([]);
                setDirectorWarnings([]);
                setModal("director");
              }}
            />
          </Panel>
        </PanelGroup>
      </div>
      <div className="transport-bar">
        <div className="transport-left">
          <button
            className={`jobs-indicator ${activeJobs.length ? "has-jobs" : ""}`}
            onClick={() => setJobsOpen((value) => !value)}
          >
            {activeJobs.length ? (
              <Loader2 size={14} className="spin" />
            ) : (
              <ListMusic size={14} />
            )}
            <span>
              {activeJobs.length
                ? `${activeJobs.length} 个任务进行中`
                : "任务队列"}
            </span>
            {jobs.length > 0 && <b>{jobs.length}</b>}
          </button>
          {playbackName && (
            <span className="playback-name">{playbackName}</span>
          )}
        </div>
        <div className="transport-center">
          <IconButton label="回到起点" onClick={() => seek(0)}>
            <SkipBack size={16} />
          </IconButton>
          <button
            className="play-main"
            aria-label={playing || livePlaying ? "暂停" : "播放时间轴"}
            onClick={() => void playTimeline()}
            disabled={rendering}
          >
            {rendering ? (
              <Loader2 size={17} className="spin" />
            ) : playing || livePlaying ? (
              <Pause size={17} fill="currentColor" />
            ) : (
              <Play size={17} fill="currentColor" />
            )}
          </button>
          <IconButton label="停止" onClick={stop}>
            <Square size={13} />
          </IconButton>
          <div className="transport-time">
            <b>{timecode(currentTime)}</b>
            <span>/ {timecode(totalDuration)}</span>
          </div>
        </div>
        <div className="transport-right">
          <Volume2 size={14} />
          <input
            aria-label="试听音量"
            type="range"
            min={0}
            max={1}
            step={0.05}
            defaultValue={0.8}
            onChange={(event) => {
              liveAudio.current.setVolume(Number(event.target.value));
              if (audio.current)
                audio.current.volume = Number(event.target.value);
            }}
          />
          <span>48 kHz</span>
        </div>
      </div>
      <audio
        ref={audio}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false);
          video.current?.pause();
        }}
        onTimeUpdate={() => {
          if (audio.current) {
            setCurrentTime(audio.current.currentTime);
            if (
              playbackKind === "timeline" &&
              video.current &&
              Math.abs(video.current.currentTime - audio.current.currentTime) >
                0.12
            )
              video.current.currentTime = audio.current.currentTime;
          }
        }}
        onError={() => {
          if (currentUrl) notify("无法播放此音频，请检查素材是否完整。", true);
        }}
      />
      <input
        ref={fileInput}
        className="hidden-input"
        type="file"
        multiple
        accept=".txt,.srt,.vtt,audio/*,video/*"
        onChange={(event) => void importFiles(event.target.files)}
      />
      <input
        ref={relinkInput}
        className="hidden-input"
        type="file"
        accept="audio/*,video/*"
        onChange={event => {
          const file = event.target.files?.[0];
          const assetId = relinkAssetId.current;
          if (!file || !assetId || !project) return;
          void run("relink", async () => {
            await flush();
            const form = new FormData(); form.append("file", file);
            replace(await post<Project>(`/projects/${project.id}/assets/${assetId}/relink`, form));
            notify("缺失素材已重新关联");
          });
          event.target.value = "";
        }}
      />
      <input
        ref={restoreInput}
        className="hidden-input"
        type="file"
        accept=".zip,.jdx"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          void run("restore", async () => {
            await flush();
            const form = new FormData();
            form.append("file", file);
            const restored = await post<Project>("/projects/restore", form);
            ui.setProject(restored.id);
            refreshBootstrap();
            notify("项目已恢复");
          });
          event.target.value = "";
        }}
      />
      {jobsOpen && (
        <div className="jobs-popover">
          <div className="popover-heading">
            <strong>生成任务</strong>
            <IconButton label="关闭任务面板" onClick={() => setJobsOpen(false)}>
              <X size={15} />
            </IconButton>
          </div>
          <div className="jobs-scroll">
            {jobs.length ? (
              [...jobs].reverse().map((job) => (
                <div className="job-row" key={job.id}>
                  <div>
                    <strong>
                      {project?.lines
                        .find((line) => line.id === job.line_id)
                        ?.text.slice(0, 35) || "台词已移除"}
                    </strong>
                    <span
                      className={
                        ["failed", "uncertain"].includes(job.status)
                          ? "error-text"
                          : ""
                      }
                    >
                      {JOB_LABELS[job.status]}
                      {job.error ? ` · ${job.error}` : ""}
                    </span>
                    {job.status === "running" && (
                      <progress
                        max={100}
                        value={
                          job.progress <= 1 ? job.progress * 100 : job.progress
                        }
                      />
                    )}
                    {(job.request_id || Object.keys(job.usage ?? {}).length > 0) && (
                      <details className="job-details"><summary>请求与实际用量</summary>
                        {job.request_id && <small>请求 ID：{job.request_id}</small>}
                        <pre>{JSON.stringify(job.usage ?? {}, null, 2)}</pre>
                      </details>
                    )}
                  </div>
                  {["queued", "running"].includes(job.status) && (
                    <IconButton
                      label="取消任务"
                      onClick={() =>
                        void run("cancel", async () => {
                          await post(
                            `/projects/${project?.id}/tasks/${job.id}/cancel`,
                          );
                          await jobsQuery.refetch();
                        })
                      }
                    >
                      <X size={14} />
                    </IconButton>
                  )}
                  {["failed", "interrupted"].includes(job.status) && (
                    <IconButton
                      label="重新生成此句"
                      onClick={() => {
                        ui.select([job.line_id]);
                        void run("retry", async () => {
                          await flush();
                          await post(`/projects/${project?.id}/tasks`, {
                            line_ids: [job.line_id],
                            transport,
                          });
                          await jobsQuery.refetch();
                        });
                      }}
                    >
                      <RefreshCw size={14} />
                    </IconButton>
                  )}
                </div>
              ))
            ) : (
              <div className="resource-empty muted small">没有生成任务</div>
            )}
          </div>
        </div>
      )}
      {contextMenu && (
        <div
          className="dropdown context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onClick={(event) => event.stopPropagation()}
        >
          <button
            onClick={() => {
              duplicateLine(contextMenu.lineId);
              setContextMenu(null);
            }}
          >
            <Copy size={14} />
            复制台词
          </button>
          <button
            onClick={() => {
              splitLines([contextMenu.lineId]);
              setContextMenu(null);
            }}
          >
            <Scissors size={14} />
            自动分句
          </button>
          <button
            onClick={() => {
              reorderLine(contextMenu.lineId, -1);
              setContextMenu(null);
            }}
          >
            <ArrowUp size={14} />
            上移一句
          </button>
          <button
            onClick={() => {
              reorderLine(contextMenu.lineId, 1);
              setContextMenu(null);
            }}
          >
            <ArrowDown size={14} />
            下移一句
          </button>
          <button
            onClick={() => {
              ui.select([contextMenu.lineId]);
              ui.setTab("history");
              setContextMenu(null);
            }}
          >
            <History size={14} />
            查看历史版本
          </button>
          <hr />
          <button
            className="danger-text"
            onClick={() => {
              deleteLines([contextMenu.lineId]);
              setContextMenu(null);
            }}
          >
            <Trash2 size={14} />
            移除台词
          </button>
        </div>
      )}
      <Modal
        title="新建配音项目"
        open={modal === "new"}
        onClose={() => setModal(null)}
      >
        <Field label="项目名称">
          <input
            autoFocus
            value={projectName}
            onChange={(event) => setProjectName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void createProject();
            }}
          />
        </Field>
        <div className="modal-actions">
          <Button onClick={() => setModal(null)}>取消</Button>
          <Button
            variant="primary"
            busy={busy === "new"}
            onClick={() => void createProject()}
          >
            <Plus size={15} />
            创建项目
          </Button>
        </div>
      </Modal>
      {modal === "connections" && (
        <ConnectionModal
          open
          onClose={() => setModal(null)}
          connections={bootstrap?.connections ?? []}
          models={bootstrap?.models ?? []}
          voices={bootstrap?.voices ?? []}
          defaultConnectionId={preferredConnection?.id}
          onSaved={refreshBootstrap}
          notify={notify}
        />
      )}
      {bootstrap && <SetupWizard bootstrap={bootstrap} open={setupOpen && modal !== "connections"}
        onConfigure={() => setModal("connections")}
        onDone={() => { setSetupOpen(false); refreshBootstrap(); }}
        onProject={value => { ui.setProject(value.id); refreshBootstrap(); }} notify={notify} />}
      {modal === "voice" && (
        <VoiceModal
          open
          onClose={() => setModal(null)}
          connections={bootstrap?.connections ?? []}
          models={bootstrap?.models ?? []}
          voices={bootstrap?.voices ?? []}
          defaultConnectionId={preferredConnection?.id}
          onSaved={refreshBootstrap}
          notify={notify}
        />
      )}
      <Modal
        title="添加台词"
        description="文本导入"
        open={modal === "text"}
        onClose={() => setModal(null)}
        wide
      >
        <textarea
          className="paste-text"
          rows={12}
          value={rawText}
          onChange={(event) => setRawText(event.target.value)}
          placeholder="粘贴需要配音的文本"
        />
        <div className="modal-actions">
          <span className="muted small">{rawText.length} 字符</span>
          <Button
            onClick={() => {
              addLines(rawText.trim() ? [rawText.trim()] : []);
              setRawText("");
              setModal(null);
            }}
            disabled={!rawText.trim()}
          >
            作为一句添加
          </Button>
          <Button
            variant="primary"
            onClick={() => {
              addLines(splitText(rawText));
              setRawText("");
              setModal(null);
            }}
            disabled={!rawText.trim()}
          >
            <Scissors size={14} />
            分句并添加
          </Button>
        </div>
      </Modal>
      <Modal
        title="搜索和替换"
        open={modal === "replace"}
        onClose={() => setModal(null)}
      >
        <Field label="查找">
          <input
            value={findText}
            onChange={(event) => setFindText(event.target.value)}
          />
        </Field>
        <Field label="替换为">
          <input
            value={replaceText}
            onChange={(event) => setReplaceText(event.target.value)}
          />
        </Field>
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={selectedOnly}
            onChange={(event) => setSelectedOnly(event.target.checked)}
          />
          仅替换选中台词
        </label>
        <div className="modal-actions">
          <Button onClick={() => setModal(null)}>取消</Button>
          <Button
            variant="primary"
            disabled={!findText}
            onClick={() => {
              let count = 0;
              edit((current) => ({
                ...current,
                lines: current.lines.map((line) => {
                  if (
                    (selectedOnly && !ui.selectedIds.includes(line.id)) ||
                    !line.text.includes(findText)
                  )
                    return line;
                  count += line.text.split(findText).length - 1;
                  return {
                    ...line,
                    text: line.text.split(findText).join(replaceText),
                  };
                }),
              }));
              notify(`已替换 ${count} 处文本`);
              setModal(null);
            }}
          >
            全部替换
          </Button>
        </div>
      </Modal>
      <Modal
        title="项目角色"
        open={modal === "roles"}
        onClose={() => setModal(null)}
        wide
      >
        <div className="role-list">
          {project?.roles.map((role) => (
            <div className="role-editor" key={role.id}>
              <input
                aria-label="角色颜色"
                className="color-input"
                type="color"
                value={role.color}
                onChange={(event) =>
                  edit((current) => ({
                    ...current,
                    roles: current.roles.map((value) =>
                      value.id === role.id
                        ? { ...value, color: event.target.value }
                        : value,
                    ),
                  }))
                }
              />
              <input
                aria-label="角色名称"
                value={role.name}
                onChange={(event) =>
                  edit((current) => ({
                    ...current,
                    roles: current.roles.map((value) =>
                      value.id === role.id
                        ? { ...value, name: event.target.value }
                        : value,
                    ),
                  }))
                }
              />
              <select
                aria-label="角色音色"
                value={bootstrap?.voices.find(voice => voice.voice_id === role.voice_id && voice.target_model === role.model_id && (!voice.connection_id || voice.connection_id === role.connection_id))?.id ?? ""}
                onChange={(event) => {
                  const voice = bootstrap?.voices.find(
                    (value) => value.id === event.target.value,
                  );
                  edit((current) => ({
                    ...current,
                    roles: current.roles.map((value) =>
                      value.id === role.id
                        ? {
                            ...value,
                            voice_id: voice?.voice_id,
                            model_id: voice?.target_model,
                            connection_id: voice?.connection_id ?? value.connection_id,
                          }
                        : value,
                    ),
                  }));
                }}
              >
                <option value="">选择声音</option>
                {bootstrap?.voices.map((voice) => (
                  <option key={voice.id} value={voice.id}>
                    {voice.name} · {voice.target_model.includes("3.1") ? "3.1" : "3.0"} {voice.target_model.endsWith("flash") ? "Flash" : "Plus"}
                  </option>
                ))}
              </select>
              <IconButton
                label="移除角色"
                onClick={() => {
                  if (!confirm(`移除角色“${role.name}”？`)) return;
                  edit((current) => ({
                    ...current,
                    roles: current.roles.filter(
                      (value) => value.id !== role.id,
                    ),
                    lines: current.lines.map((line) =>
                      line.role_id === role.id
                        ? { ...line, role_id: null }
                        : line,
                    ),
                    clips: current.clips.map((clip) =>
                      clip.track_id === role.id
                        ? { ...clip, track_id: "main" }
                        : clip,
                    ),
                  }));
                }}
              >
                <Trash2 size={15} />
              </IconButton>
            </div>
          ))}
        </div>
        <div className="modal-actions">
          <Button
            onClick={() =>
              edit((current) => ({
                ...current,
                roles: [
                  ...current.roles,
                  {
                    id: uid(),
                    name: `角色 ${current.roles.length + 1}`,
                    color: ["#6eb3a5", "#bd9b6b", "#9393c6", "#c7838b"][
                      current.roles.length % 4
                    ],
                  },
                ],
              }))
            }
          >
            <Plus size={15} />
            添加角色
          </Button>
          <Button variant="primary" onClick={() => setModal(null)}>
            完成
          </Button>
        </div>
      </Modal>
      <Modal
        title="导演计划"
        description={activeLine ? `第 ${activeLine.position + 1} 句` : ""}
        open={modal === "director"}
        onClose={() => setModal(null)}
        wide
      >
        <Field label="导演要求">
          <textarea
            rows={3}
            value={directorInstruction}
            onChange={(event) => setDirectorInstruction(event.target.value)}
            placeholder="描述情绪、节奏和停顿要求"
          />
        </Field>
        <div className="director-templates">
          {[
            "温柔叙述，句尾稍作停顿",
            "情绪由平静转为坚定",
            "节奏轻快，充满期待",
            "严肃沉稳，突出重点",
          ].map((value) => (
            <button key={value} onClick={() => setDirectorInstruction(value)}>
              {value}
            </button>
          ))}
        </div>
        <Button
          busy={busy === "director"}
          onClick={() => void analyzeDirector()}
          disabled={!activeLine || !directorInstruction.trim()}
        >
          <WandSparkles size={15} />
          解析配音计划
        </Button>
        {segments.length > 0 && (
          <div className="director-segments">
            {segments.map((segment, index) => (
              <div className="director-segment" key={index}>
                <b>{String(index + 1).padStart(2, "0")}</b>
                <div>
                  <Field label="台词">
                    <textarea
                      rows={2}
                      value={segment.text}
                      onChange={(event) =>
                        setSegments((current) =>
                          current.map((value, i) =>
                            i === index
                              ? { ...value, text: event.target.value }
                              : value,
                          ),
                        )
                      }
                    />
                  </Field>
                  <div className="form-row">
                    <Field label="表达要求">
                      <input
                        value={segment.instruction}
                        onChange={(event) =>
                          setSegments((current) =>
                            current.map((value, i) =>
                              i === index
                                ? { ...value, instruction: event.target.value }
                                : value,
                            ),
                          )
                        }
                      />
                    </Field>
                    <Field label="句后停顿（毫秒）">
                      <input
                        type="number"
                        min={0}
                        value={segment.pause_after_ms}
                        onChange={(event) =>
                          setSegments((current) =>
                            current.map((value, i) =>
                              i === index
                                ? {
                                    ...value,
                                    pause_after_ms: Number(event.target.value),
                                  }
                                : value,
                            ),
                          )
                        }
                      />
                    </Field>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        {directorWarnings.map((warning, index) => (
          <div className="inline-warning" key={index}>
            {warning}
          </div>
        ))}
        <div className="modal-actions">
          <Button onClick={() => setModal(null)}>取消</Button>
          <Button
            variant="primary"
            onClick={applyDirector}
            disabled={!segments.length}
          >
            确认应用计划
          </Button>
        </div>
      </Modal>
      <Modal
        title="导出项目"
        open={modal === "export"}
        onClose={() => setModal(null)}
      >
        <Field label="导出内容">
          <select
            value={exportFormat}
            onChange={(event) => setExportFormat(event.target.value)}
          >
            <option value="wav">时间轴 WAV 音频</option>
            <option value="mp3">时间轴 MP3 音频</option>
            <option value="sentences">批量逐句 WAV</option>
            <option value="csv">CSV 时间码清单</option>
            <option value="project">完整项目备份</option>
          </select>
        </Field>
        <Field label="文件名">
          <input
            value={exportName}
            onChange={(event) => setExportName(event.target.value)}
            placeholder={project?.name}
          />
        </Field>
        {["wav", "mp3", "sentences"].includes(exportFormat) && (
          <>
            <div className="form-row">
              <Field label="采样率">
                <select
                  value={sampleRate}
                  onChange={(event) =>
                    setSampleRate(Number(event.target.value))
                  }
                >
                  <option value={48000}>48,000 Hz</option>
                  <option value={44100}>44,100 Hz</option>
                  <option value={24000}>24,000 Hz</option>
                </select>
              </Field>
              <Field label="位深">
                <input
                  readOnly
                  value={
                    exportFormat === "mp3" ? "MP3 · 192 kbps" : "24-bit PCM"
                  }
                />
              </Field>
            </div>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={normalize}
                onChange={(event) => setNormalize(event.target.checked)}
              />
              响度归一化与防削波
            </label>
          </>
        )}
        {exportJobs
          .filter((job) => job.status !== "succeeded")
          .slice(-3)
          .map((job) => (
            <div key={job.id} className="export-progress">
              <span>
                {job.status === "failed" ? job.error : "正在处理音频…"}
              </span>
              <progress
                max={100}
                value={job.progress <= 1 ? job.progress * 100 : job.progress}
              />
              {job.download_url && (
                <a href={job.download_url} download>
                  下载结果
                </a>
              )}
            </div>
          ))}
        <div className="modal-actions">
          {hasNativeExport && <Button onClick={() => void openNative("open_export_folder").catch(report)}><FolderOpen size={14} />打开导出文件夹</Button>}
          <Button onClick={() => setModal(null)}>关闭</Button>
          <Button
            variant="primary"
            busy={busy === "export"}
            onClick={() => void exportProject()}
            disabled={!project}
          >
            <Download size={15} />
            开始导出
          </Button>
        </div>
      </Modal>
      <Modal
        title="Jdx TTS Studio"
        description="百炼远程配音工作站 · 0.1.0"
        open={modal === "about"}
        onClose={() => setModal(null)}
      >
        <div className="about-info">
          <div>
            <StatusDot state="ok" />
            本地服务已连接
          </div>
          <div>
            <Cloud size={15} />
            {bootstrap?.connections.length ?? 0} 个百炼连接
          </div>
          <div>
            <Folder size={15} />
            {bootstrap?.projects.length ?? 0} 个本地项目
          </div>
        </div>
        <div className="modal-actions">
          <Button
            disabled={!isDesktop()}
            onClick={() => void openNative("open_data_dir").catch(report)}
          >
            <FolderOpen size={14} />
            打开数据目录
          </Button>
          <Button
            disabled={!isDesktop()}
            onClick={() => void openNative("open_logs").catch(report)}
          >
            <FileText size={14} />
            查看日志
          </Button>
        </div>
      </Modal>
      <div className="toast-stack">
        {notices.map((notice) => (
          <div
            key={notice.id}
            className={`toast ${notice.error ? "toast-error" : ""}`}
            role={notice.error ? "alert" : "status"}
          >
            {notice.error ? <CircleHelp size={16} /> : <Check size={16} />}
            <span>{notice.text}</span>
            <button
              aria-label="关闭通知"
              onClick={() =>
                setNotices((current) =>
                  current.filter((value) => value.id !== notice.id),
                )
              }
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
