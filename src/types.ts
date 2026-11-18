import type { components } from "./generated/api";

type Schema = components["schemas"];
type ServerFields<T, K extends keyof T> = T & Required<Pick<T, K>>;

// Pydantic supplies these factory defaults in every server response. The API
// schema also accepts their omission on input, so make only those defaults
// explicit in the editor's response types.
export type CloudConnection = ServerFields<Schema["CloudConnection"], "id">;
export type VoiceProfile = ServerFields<Schema["VoiceProfile"], "id">;
export type Role = ServerFields<Schema["Role"], "id">;
export type LineSettings = ServerFields<Schema["LineSettings"], "emphasis_words">;
export type Asset = Schema["Asset"];
export type TimelineClip = ServerFields<Schema["TimelineClip"], "id">;
export type AudioVersion = Omit<Schema["AudioVersion"], "settings" | "usage"> & {
  settings: LineSettings;
  usage: Record<string, unknown>;
};
export type Line = Omit<Schema["Line"], "id" | "settings" | "versions"> & {
  id: string;
  settings: LineSettings;
  versions: AudioVersion[];
};
export type Project = Omit<Schema["Project"], "lines" | "roles" | "clips"> & {
  lines: Line[];
  roles: Role[];
  clips: TimelineClip[];
};
export type GenerationJob = Schema["GenerationJob"];

export type ModelProfile = Schema["ModelProfile"];
export type TrashedProject = Schema["TrashedProject"];
export interface ProjectSummary {
  id: string;
  name: string;
  updated_at: string;
}
export interface ExportJob {
  id: string;
  status: string;
  progress: number;
  error?: string;
  asset_id?: string;
  download_url?: string;
}
export interface Bootstrap {
  projects: ProjectSummary[];
  connections: CloudConnection[];
  models: ModelProfile[];
  voices: VoiceProfile[];
  preferences: Record<string, unknown>;
  environment: Record<string, unknown>;
}
export interface DirectorSegment {
  text: string;
  instruction: string;
  pause_after_ms: number;
}
export interface BackendSession {
  base_url: string;
  token: string;
}
