import type { Project } from "./types";

const equal = (a: unknown, b: unknown): boolean => {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  const left = a as Record<string, unknown>, right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => equal(left[key], right[key]));
};

function mergeValue(base: unknown, local: unknown, remote: unknown, path: string): unknown {
  if (equal(local, base)) return remote;
  if (equal(remote, base) || equal(local, remote)) return local;
  if (base && local && remote && typeof base === "object" && typeof local === "object" && typeof remote === "object" &&
      !Array.isArray(base) && !Array.isArray(local) && !Array.isArray(remote)) {
    const b = base as Record<string, unknown>, l = local as Record<string, unknown>, r = remote as Record<string, unknown>;
    return Object.fromEntries([...new Set([...Object.keys(b), ...Object.keys(l), ...Object.keys(r)])]
      .map(key => [key, mergeValue(b[key], l[key], r[key], `${path}.${key}`)]));
  }
  throw new Error(`项目中的${path}同时发生变化，当前编辑已保留。请先复制修改内容，再重新打开项目核对。`);
}

function mergeRows<T extends { id: string }>(base: T[], local: T[], remote: T[], name: string, owned: string[] = []): T[] {
  const b = new Map(base.map(item => [item.id, item]));
  const r = new Map(remote.map(item => [item.id, item]));
  const l = new Map(local.map(item => [item.id, item]));
  const order = local.map(item => item.id);
  for (const item of remote) if (!b.has(item.id) && !l.has(item.id)) order.push(item.id);
  const rows: T[] = [];
  for (const id of order) {
    const before = b.get(id), current = l.get(id), latest = r.get(id);
    if (!before) { if (current || latest) rows.push((current ?? latest)!); continue; }
    if (!current) continue;
    if (!latest) {
      if (!equal(before, current)) throw new Error(`正在编辑的${name}已被移除，当前编辑已保留。`);
      continue;
    }
    const strip = (item: T) => Object.fromEntries(Object.entries(item).filter(([key]) => !owned.includes(key)));
    const merged = mergeValue(strip(before), strip(current), strip(latest), name) as T;
    for (const key of owned) (merged as Record<string, unknown>)[key] = (latest as Record<string, unknown>)[key];
    rows.push(merged);
  }
  return rows;
}

// A cloud result may update assets, selected versions and automatic clips while
// the user is typing. Merge independent edits without dropping either side.
export function mergeServerProject(base: Project, local: Project, remote: Project): Project {
  if (base.id !== local.id || local.id !== remote.id) throw new Error("无法合并不同项目的修改。");
  return {
    ...remote,
    name: mergeValue(base.name, local.name, remote.name, "项目名称") as string,
    video_asset_id: mergeValue(base.video_asset_id, local.video_asset_id, remote.video_asset_id, "视频") as Project["video_asset_id"],
    roles: mergeRows(base.roles, local.roles, remote.roles, "角色"),
    lines: mergeRows(base.lines, local.lines, remote.lines, "台词", ["revision", "versions", "selected_version_id"])
      .map((line, position) => ({ ...line, position })),
    clips: mergeRows(base.clips, local.clips, remote.clips, "时间轴片段"),
  };
}
