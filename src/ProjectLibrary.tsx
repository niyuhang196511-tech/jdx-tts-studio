import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileAudio, FolderOpen, Pencil, Plus, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { api, patch, post } from "./api";
import { formatDate } from "./lib";
import type { Bootstrap, Project, ProjectSummary, TrashedProject } from "./types";
import { Button, Empty, Field, IconButton, Modal } from "./ui";

type ProjectAction = { kind: "rename" | "delete"; project: ProjectSummary };

export function ProjectLibrary({ projects, currentId, disabled, hasProject, onOpen, onCreate,
  onBackup, onRestoreBackup, beforeChange, onUpdated, onDeleted, onRestored, onBusy, notify }: {
  projects: ProjectSummary[];
  currentId: string | null;
  disabled: boolean;
  hasProject: boolean;
  onOpen: (id: string) => Promise<void>;
  onCreate: () => void;
  onBackup: () => Promise<void>;
  onRestoreBackup: () => void;
  beforeChange: () => Promise<void>;
  onUpdated: (project: Project) => void;
  onDeleted: (id: string) => void;
  onRestored: (project: Project) => void;
  onBusy: (busy: boolean) => void;
  notify: (message: string, error?: boolean) => void;
}) {
  const client = useQueryClient();
  const [action, setAction] = useState<ProjectAction | null>(null);
  const [name, setName] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [trashOpen, setTrashOpen] = useState(false);
  const trash = useQuery({
    queryKey: ["project-trash"],
    queryFn: ({ signal }) => api<TrashedProject[]>("/projects/trash", { signal }),
    enabled: trashOpen,
    retry: false,
  });
  const startAction = (kind: ProjectAction["kind"], project: ProjectSummary) => {
    setAction({ kind, project });
    setName(project.name);
    setError("");
  };
  const refresh = async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["bootstrap"] }),
      client.invalidateQueries({ queryKey: ["project-trash"] }),
    ]);
  };
  const apply = async () => {
    if (!action || working) return;
    if (action.kind === "rename" && !name.trim()) {
      setError("请输入项目名称。");
      return;
    }
    setWorking(true);
    setError("");
    onBusy(true);
    try {
      if (action.project.id === currentId) await beforeChange();
      const latest = await api<Project>(`/projects/${action.project.id}`);
      if (action.kind === "rename") {
        const updated = await patch<Project>(`/projects/${latest.id}`, { revision: latest.revision, name: name.trim() });
        client.setQueryData(["project", updated.id], updated);
        client.setQueryData<Bootstrap>(["bootstrap"], value => value && ({ ...value,
          projects: value.projects.map(item => item.id === updated.id ? { ...item, name: updated.name, updated_at: updated.updated_at } : item),
        }));
        onUpdated(updated);
        notify("项目已重命名");
      } else {
        await api(`/projects/${latest.id}`, { method: "DELETE", body: JSON.stringify({ revision: latest.revision }) });
        await Promise.all([
          client.cancelQueries({ queryKey: ["project", latest.id] }),
          client.cancelQueries({ queryKey: ["jobs", latest.id] }),
        ]);
        onDeleted(latest.id);
        client.removeQueries({ queryKey: ["project", latest.id] });
        client.removeQueries({ queryKey: ["jobs", latest.id] });
        client.setQueryData<Bootstrap>(["bootstrap"], value => value && ({ ...value,
          projects: value.projects.filter(item => item.id !== latest.id),
        }));
        notify("项目已移入回收站，音频和历史版本完整保留");
      }
      setAction(null);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "项目操作失败，请重试。");
    } finally {
      setWorking(false);
      onBusy(false);
    }
  };
  const restore = async (id: string) => {
    if (working) return;
    setWorking(true);
    setError("");
    onBusy(true);
    try {
      await beforeChange();
      const restored = await post<Project>(`/projects/${id}/restore-trash`);
      client.setQueryData(["project", id], restored);
      client.setQueryData<TrashedProject[]>(["project-trash"], value => value?.filter(item => item.id !== id));
      onRestored(restored);
      notify("项目已恢复，台词、素材和历史版本均已保留");
      setTrashOpen(false);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "恢复项目失败，请重试。");
    } finally {
      setWorking(false);
      onBusy(false);
    }
  };
  return <>
    <div className="resource-heading">
      <span>最近项目</span>
      <IconButton label="新建项目" onClick={onCreate} disabled={disabled}><Plus size={15} /></IconButton>
    </div>
    <div className="resource-scroll">
      {projects.length ? projects.map(item => <div key={item.id} data-project-id={item.id}
        className={`project-item ${currentId === item.id ? "active" : ""}`}>
        <button className="project-open" aria-label={`打开项目：${item.name}`} disabled={disabled}
          onClick={() => void onOpen(item.id)}>
          <div className="project-icon"><FileAudio size={17} /></div>
          <span><strong title={item.name}>{item.name}</strong><small>{formatDate(item.updated_at)}</small></span>
        </button>
        <div className="project-actions">
          <IconButton label={`重命名项目：${item.name}`} disabled={disabled} onClick={() => startAction("rename", item)}><Pencil size={13} /></IconButton>
          <IconButton label={`删除项目：${item.name}`} disabled={disabled} onClick={() => startAction("delete", item)}><Trash2 size={13} /></IconButton>
        </div>
      </div>) : <div className="small muted resource-empty">还没有项目</div>}
    </div>
    <div className="resource-footer">
      <Button variant="ghost" className="full-width" disabled={disabled} onClick={() => { setError(""); setTrashOpen(true); }}>
        <Trash2 size={14} />项目回收站
      </Button>
      <Button variant="ghost" className="full-width" disabled={disabled} onClick={onRestoreBackup}>
        <FolderOpen size={14} />恢复项目备份
      </Button>
      <Button variant="ghost" className="full-width" disabled={!hasProject || disabled} onClick={() => void onBackup()}>
        <ShieldCheck size={14} />备份当前项目
      </Button>
    </div>
    <Modal title={action?.kind === "rename" ? "重命名项目" : "删除项目"} open={!!action}
      onClose={() => { if (!working) setAction(null); }}>
      {action?.kind === "rename" ? <Field label="新的项目名称">
        <input autoFocus maxLength={200} value={name} onChange={event => setName(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !working) void apply(); }} />
      </Field> : <div className="project-delete-message">
        <p>将“<strong>{action?.project.name}</strong>”移入项目回收站？</p>
        <p className="muted">台词、音频素材、生成历史和时间轴都会保留，可从“项目回收站”恢复。</p>
        <p className="small muted">正在生成或导出的项目需要等待任务结束，再移入回收站。</p>
      </div>}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="modal-actions">
        <Button disabled={working} onClick={() => setAction(null)}>取消</Button>
        <Button variant={action?.kind === "rename" ? "primary" : "danger"} busy={working}
          disabled={working || (action?.kind === "rename" && !name.trim())} onClick={() => void apply()}>
          {action?.kind === "rename" ? "保存名称" : "移入回收站"}
        </Button>
      </div>
    </Modal>
    <Modal title="项目回收站" description="项目中的原始素材和生成历史均保留，恢复后可继续编辑。"
      open={trashOpen} onClose={() => { if (!working) setTrashOpen(false); }}>
      {trash.isPending ? <p className="muted">正在读取回收站…</p> : trash.error ? <div>
        <p className="error-text" role="alert">{trash.error instanceof Error ? trash.error.message : "读取回收站失败"}</p>
        <Button onClick={() => void trash.refetch()}>重新加载</Button>
      </div> : trash.data?.length ? <div className="trash-projects">{trash.data.map(item => <div className="trash-project" key={item.id} data-project-id={item.id}>
        <div><strong>{item.name}</strong><small>移入时间：{formatDate(item.deleted_at)}</small></div>
        <Button disabled={working} onClick={() => void restore(item.id)} aria-label={`恢复项目：${item.name}`}><RotateCcw size={14} />恢复项目</Button>
      </div>)}</div> : <Empty icon={<Trash2 size={28} />} title="回收站为空" />}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="modal-actions"><Button disabled={working} onClick={() => setTrashOpen(false)}>关闭</Button></div>
    </Modal>
  </>;
}
