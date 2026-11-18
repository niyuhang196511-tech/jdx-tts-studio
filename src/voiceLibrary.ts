import type { CloudConnection, VoiceProfile } from "./types";

export type CatalogVoice = VoiceProfile & {
  gender?: string;
  languages?: string[];
  tags?: string[];
  preview_available?: boolean;
  preview_source?: string | null;
  source_url?: string | null;
};
export type VoiceGroup = { key: string; voices: CatalogVoice[] };

export function defaultConnection(connections: CloudConnection[], preferred: unknown) {
  return connections.find(item => item.id === preferred)
    ?? connections.find(item => item.has_credential)
    ?? connections[0];
}

export function groupVoices(voices: CatalogVoice[]): VoiceGroup[] {
  const groups = new Map<string, VoiceGroup>();
  for (const voice of voices) {
    // Built-in variants share a card; personal configurations keep their identity.
    const key = voice.id.startsWith("system:") ? `official:${voice.voice_id}` : voice.id;
    const group = groups.get(key) ?? { key, voices: [] };
    if (!group.voices.some(item => item.id === voice.id)) group.voices.push(voice);
    groups.set(key, group);
  }
  return [...groups.values()];
}

export function modelLabel(id: string) {
  return id.replace("qwen-audio-", "千问 ").replace("-tts-plus", " Plus").replace("-tts-flash", " Flash");
}

export function voiceMatches(voice: CatalogVoice, query: string) {
  return [voice.name, voice.voice_id, voice.target_model, voice.notes, voice.gender,
    ...(voice.languages ?? []), ...(voice.tags ?? [])].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
}
