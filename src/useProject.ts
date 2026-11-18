import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, patch } from "./api";
import { mergeServerProject } from "./mergeProject";
import type { Project } from "./types";

export function useProject(
  projectId: string | null,
  ready: boolean,
  onError: (error: unknown) => void,
) {
  const [project, setProject] = useState<Project | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const current = useRef<Project | null>(null);
  const baseline = useRef<Project | null>(null);
  const changed = useRef(0);
  const unsaved = useRef(false);
  const inFlight = useRef<Promise<void> | null>(null);
  const failedSerial = useRef<number | null>(null);
  const errorRef = useRef(onError);
  const activeId = useRef(projectId);
  activeId.current = projectId;
  errorRef.current = onError;
  const assign = useCallback((value: Project | null) => {
    current.current = value;
    setProject(value);
  }, []);
  useEffect(() => {
    assign(null);
    baseline.current = null;
    unsaved.current = false;
    changed.current = 0;
    failedSerial.current = null;
    setDirty(false);
  }, [projectId, assign]);
  const query = useQuery({
    queryKey: ["project", projectId],
    queryFn: ({ signal }) => api<Project>(`/projects/${projectId}`, { signal }),
    enabled: ready && !!projectId,
    refetchInterval: 3500,
    retry: (attempt, error) => !(error instanceof ApiError && [404, 410].includes(error.status)) && attempt < 2,
  });
  useEffect(() => {
    if (query.data?.id === projectId && !unsaved.current && !inFlight.current) {
      baseline.current = query.data;
      assign(query.data);
    }
  }, [query.data, projectId, assign]);
  const edit = useCallback(
    (updater: (value: Project) => Project) => {
      if (!current.current) return;
      changed.current += 1;
      failedSerial.current = null;
      unsaved.current = true;
      setDirty(true);
      assign(updater(current.current));
    },
    [assign],
  );
  const flush = useCallback(async (): Promise<void> => {
    if (inFlight.current) await inFlight.current;
    if (!current.current || !unsaved.current) return;
    setSaving(true);
    const request = (async () => {
      try {
        while (current.current && unsaved.current) {
          const snapshot = current.current;
          const serial = changed.current;
          let saved: Project;
          try {
          saved = await patch<Project>(`/projects/${snapshot.id}`, {
            revision: snapshot.revision,
            name: snapshot.name,
            lines: snapshot.lines,
            clips: snapshot.clips,
            roles: snapshot.roles,
            video_asset_id: snapshot.video_asset_id,
          });
          } catch (error) {
            if (!(error instanceof ApiError) || error.status !== 409 || !baseline.current || !current.current) throw error;
            const latest = await api<Project>(`/projects/${snapshot.id}`);
            if (!current.current || current.current.id !== snapshot.id) return;
            const merged = mergeServerProject(baseline.current, current.current, latest);
            baseline.current = latest;
            assign(merged);
            continue;
          }
          if (current.current?.id !== snapshot.id) return;
          baseline.current = saved;
          if (changed.current === serial) {
            unsaved.current = false;
            failedSerial.current = null;
            setDirty(false);
            assign(saved);
          } else {
            const live = current.current;
            assign({
              ...live,
              revision: saved.revision,
              assets: saved.assets,
              can_undo: saved.can_undo,
              can_redo: saved.can_redo,
              lines: live.lines.map((line) => ({
                ...line,
                versions:
                  saved.lines.find((item) => item.id === line.id)?.versions ??
                  line.versions,
              })),
            });
          }
        }
      } catch (error) {
        failedSerial.current = changed.current;
        errorRef.current(error);
        throw error;
      } finally {
        setSaving(false);
        inFlight.current = null;
      }
    })();
    inFlight.current = request;
    await request;
  }, [assign]);
  useEffect(() => {
    if (!dirty || saving || failedSerial.current === changed.current) return;
    const timeout = window.setTimeout(() => {
      void flush().catch(() => undefined);
    }, 700);
    return () => window.clearTimeout(timeout);
  }, [dirty, saving, project, flush]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (unsaved.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);
  const replace = useCallback(
    (value: Project) => {
      // An import, version selection or render may finish after the user has
      // opened another project. Its response must never replace that editor.
      if (value.id !== activeId.current) return;
      unsaved.current = false;
      baseline.current = value;
      changed.current += 1;
      setDirty(false);
      assign(value);
    },
    [assign],
  );
  const clear = useCallback(() => {
    unsaved.current = false;
    baseline.current = null;
    failedSerial.current = null;
    changed.current += 1;
    setDirty(false);
    assign(null);
  }, [assign]);
  return {
    project,
    edit,
    flush,
    replace,
    clear,
    dirty,
    saving,
    loading: query.isLoading,
    error: query.error,
    refresh: query.refetch,
  };
}
