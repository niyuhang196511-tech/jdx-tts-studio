import { describe, expect, it } from "vitest";
import { defaultConnection, groupVoices, voiceMatches, type CatalogVoice } from "./voiceLibrary";
import type { CloudConnection } from "./types";

describe("voice library choices", () => {
  it("groups official model variants without merging personal voices or inventing compatibility", () => {
    const voices = [
      { id: "system:plus:a", voice_id: "a", target_model: "plus" },
      { id: "system:flash:a", voice_id: "a", target_model: "flash" },
      { id: "saved-a", voice_id: "a", target_model: "plus" },
      { id: "system:flash:b", voice_id: "b", target_model: "flash" },
    ] as CatalogVoice[];
    const groups = groupVoices(voices);
    expect(groups).toHaveLength(3);
    expect(groups[0].voices.map(voice => voice.target_model)).toEqual(["plus", "flash"]);
    expect(groups[2].voices.map(voice => voice.target_model)).toEqual(["flash"]);
  });
  it("keeps an explicit default even when another connection has credentials", () => {
    const connections = [{ id: "chosen", has_credential: false }, { id: "ready", has_credential: true }] as CloudConnection[];
    expect(defaultConnection(connections, "chosen")?.id).toBe("chosen");
    expect(defaultConnection(connections, "removed")?.id).toBe("ready");
    expect(defaultConnection([], null)).toBeUndefined();
  });
  it("searches descriptive metadata as well as model and IDs", () => {
    const voice = { name: "晴天", voice_id: "sun", target_model: "flash", tags: ["有声书"], languages: ["中文"] } as CatalogVoice;
    expect(voiceMatches(voice, "有声书")).toBe(true);
    expect(voiceMatches(voice, "SUN")).toBe(true);
    expect(voiceMatches(voice, "广告")).toBe(false);
  });
});
