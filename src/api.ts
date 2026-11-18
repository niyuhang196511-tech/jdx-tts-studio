import { invoke } from "@tauri-apps/api/core";
import type { BackendSession } from "./types";

let session: BackendSession | undefined;
export const isDesktop = () => "__TAURI_INTERNALS__" in window;
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string,
  ) {
    super(message);
  }
}
export async function connectBackend(): Promise<BackendSession> {
  if (isDesktop()) {
    // The webview is intentionally shown before the bundled service starts.
    // Wait on native lifecycle state rather than misreporting a cold start.
    for (let attempt = 0; attempt < 200; attempt++) {
      const status = await invoke<{ state: string; message: string }>("backend_status");
      if (status.state === "ready") {
        session = await invoke<BackendSession>("backend_info");
        break;
      }
      if (["failed", "stopped"].includes(status.state)) throw new Error(status.message);
      if (attempt === 199) throw new Error("本地服务启动超时，请查看日志并重试。");
      await new Promise(resolve => window.setTimeout(resolve, 300));
    }
  }
  else {
    const response = await fetch("/__dev/session", { cache: "no-store" });
    if (!response.ok)
      throw new Error("本地服务尚未就绪，请使用项目启动脚本打开工作站。");
    session = (await response.json()) as BackendSession;
  }
  if (!session?.base_url || !session.token)
    throw new Error("无法获取本地服务的安全会话。");
  await api("/health");
  return session;
}
export async function api<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  if (!session) throw new Error("本地服务尚未连接");
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${session.token}`);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(`${session.base_url}/api/v1${path}`, {
    ...options,
    headers,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.error?.message ?? body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map(
                (item: { loc?: string[]; msg?: string }) =>
                  `${item.loc?.slice(1).join(".") ?? "参数"}：${item.msg ?? "无效"}`,
              )
              .join("；")
          : `请求失败（${response.status}）`;
    throw new ApiError(message, response.status, body?.error?.code);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    body:
      body instanceof FormData
        ? body
        : body === undefined
          ? undefined
          : JSON.stringify(body),
  });
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export async function retryBackend() {
  if (isDesktop()) await invoke("retry_backend");
  return connectBackend();
}
export async function saveCredential(connectionId: string, key: string) {
  if (!isDesktop()) return { storage: "session" as const, configured: true };
  return invoke<{ storage: "system" | "session"; configured: boolean; message?: string }>("set_credential", {
    connectionId,
    apiKey: key,
  });
}
export async function syncCredentials(connectionIds: string[]) {
  if (isDesktop() && connectionIds.length)
    await invoke("sync_credentials", { connectionIds });
}
export async function deleteCredential(connectionId: string) {
  if (isDesktop()) await invoke("delete_credential", { connectionId });
}
export async function openNative(command: "open_logs" | "open_data_dir" | "open_export_folder") {
  if (isDesktop()) await invoke(command);
  else throw new Error("此操作需要在桌面应用中使用。");
}
export async function saveExport(url: string, suggestedName: string) {
  if (isDesktop()) {
    return invoke<{ saved: boolean; path?: string; message?: string }>("save_export", { url, suggestedName });
  }
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = suggestedName;
  anchor.click();
  return { saved: true };
}
export async function subscribeEvents(
  onEvent: (event: Record<string, unknown>) => void,
  signal: AbortSignal,
) {
  if (!session) throw new Error("本地服务尚未连接");
  const response = await fetch(`${session.base_url}/api/v1/events`, {
    headers: { Authorization: `Bearer ${session.token}` },
    signal,
  });
  if (!response.ok || !response.body) throw new Error("无法订阅生成事件");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const payload = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (payload) {
          try {
            onEvent(JSON.parse(payload) as Record<string, unknown>);
          } catch {
            /* Ignore malformed diagnostic events. */
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
