import { expect, test } from "@playwright/test";

function fixtureWav(): Buffer {
  const samples = 48000 * 4;
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write("RIFF", 0); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(48000, 24); buffer.writeUInt32LE(96000, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34); buffer.write("data", 36); buffer.writeUInt32LE(samples * 2, 40);
  for (let index = 0; index < samples; index++) buffer.writeInt16LE(Math.round(Math.sin(index / 48000 * 440 * Math.PI * 2) * 2000), 44 + index * 2);
  return buffer;
}

test("timeline gestures commit once, support multiselect and undo, and preserve source audio", async ({ page, request }) => {
  const session = await (await request.get("/__dev/session")).json();
  const base = session.base_url + "/api/v1";
  const headers = { Authorization: `Bearer ${session.token}` };
  const created = await (await request.post(`${base}/projects`, { headers, data: { name: `时间轴手势验收 ${Date.now()}` } })).json();
  const id = created.id;
  const read = async () => (await request.get(`${base}/projects/${id}`, { headers })).json();
  try {
    const importedResponse = await request.post(`${base}/projects/${id}/import`, { headers, multipart: { file: { name: "时间轴验收音频.wav", mimeType: "audio/wav", buffer: fixtureWav() } } });
    expect(importedResponse.ok()).toBe(true);
    const imported = await importedResponse.json();
    const asset = imported.assets[0];
    const sourceBefore = await (await request.get(asset.media_url)).body();
    const a = { ...imported.clips[0], id: crypto.randomUUID(), label: "验收片段 A", track_id: "main", start_sample: 0, source_start_sample: 0, source_end_sample: 96000, stretch: 1 };
    const b = { ...a, id: crypto.randomUUID(), label: "验收片段 B", start_sample: 144000 };
    expect((await request.patch(`${base}/projects/${id}`, { headers, data: { revision: imported.revision, clips: [a, b], roles: [{ id: "voice-track", name: "旁白验收轨", color: "#93a3cc" }] } })).ok()).toBe(true);
    await page.addInitScript(projectId => localStorage.setItem("jdx-project", projectId), id);
    await page.goto("/");
    const skip = page.getByRole("button", { name: "稍后配置，进入工作台", exact: true });
    if (await skip.waitFor({ state: "visible", timeout: 1500 }).then(() => true).catch(() => false)) await skip.click();
    await expect(page.locator(".timeline-clip")).toHaveCount(2);
    const section = page.getByLabel("时间轴编辑区", { exact: true });
    const first = page.locator(`[data-clip-id="${a.id}"]`);
    const second = page.locator(`[data-clip-id="${b.id}"]`);
    const patches: string[] = [];
    page.on("request", value => { if (value.method() === "PATCH" && value.url() === `${base}/projects/${id}`) patches.push(value.postData() ?? ""); });
    await page.getByRole("button", { name: "关闭吸附", exact: true }).click();
    const bounds = (await first.boundingBox())!;
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + 30);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2 + 45, bounds.y + 30, { steps: 6 });
    await expect(first).toHaveAttribute("data-start-sample", "48000");
    // Wait beyond the autosave interval: pointer preview must remain local.
    await page.waitForTimeout(850);
    expect(patches).toHaveLength(0);
    expect((await read()).clips.find((clip: { id: string }) => clip.id === a.id).start_sample).toBe(0);
    await page.mouse.up();
    await expect.poll(() => patches.length).toBe(1);
    await expect.poll(async () => (await read()).clips.find((clip: { id: string }) => clip.id === a.id).start_sample).toBe(48000);
    await section.press("Control+z");
    await expect(first).toHaveAttribute("data-start-sample", "0");

    await first.click({ position: { x: 25, y: 30 } });
    await second.click({ modifiers: ["Shift"], position: { x: 25, y: 30 } });
    await expect(page.locator(".timeline-clip.selected")).toHaveCount(2);
    await section.press("ArrowRight");
    await expect(first).toHaveAttribute("data-start-sample", "480");
    await expect(second).toHaveAttribute("data-start-sample", "144480");
    await section.press("Control+d");
    await expect(page.locator(".timeline-clip")).toHaveCount(4);
    await expect(page.locator(".timeline-clip.selected")).toHaveCount(2);
    await section.press("Delete");
    await expect(page.locator(".timeline-clip")).toHaveCount(2);
    await page.getByRole("button", { name: "保存项目", exact: true }).click();
    await expect(page.locator(".save-state")).toContainText("已保存");

    const originalPosition = await first.getAttribute("data-start-sample");
    const beforeCancelled = patches.length;
    const beforeBounds = (await first.boundingBox())!;
    await page.mouse.move(beforeBounds.x + 30, beforeBounds.y + 30); await page.mouse.down();
    await page.mouse.move(beforeBounds.x + 95, beforeBounds.y + 30);
    await page.keyboard.press("Escape"); await page.mouse.up();
    await expect(first).toHaveAttribute("data-start-sample", originalPosition!);
    await page.waitForTimeout(850);
    expect(patches).toHaveLength(beforeCancelled);

    const trackBounds = (await page.locator('[data-track-id="voice-track"]').boundingBox())!;
    const currentBounds = (await first.boundingBox())!;
    await page.mouse.move(currentBounds.x + 25, currentBounds.y + 30); await page.mouse.down();
    await page.mouse.move(currentBounds.x + 25, trackBounds.y + 30, { steps: 5 }); await page.mouse.up();
    await expect(page.locator(`[data-track-id="voice-track"] [data-clip-id="${a.id}"]`)).toHaveCount(1);
    await first.click({ button: "right", position: { x: 25, y: 30 } });
    await expect(page.getByRole("menu", { name: "时间轴片段操作" })).toBeVisible();
    await page.getByRole("menuitem", { name: "静音片段" }).click();
    await expect(first).toHaveClass(/muted-clip/);
    await page.getByRole("button", { name: "适应全部片段", exact: true }).click();
    const fade = (await first.locator(".fade-in").boundingBox())!;
    await page.mouse.move(fade.x + 4, fade.y + 4); await page.mouse.down();
    await page.mouse.move(fade.x + 34, fade.y + 4, { steps: 3 }); await page.mouse.up();
    await expect(page.getByLabel("淡入（毫秒）", { exact: true })).not.toHaveValue("0");
    const trim = (await first.locator(".trim-end").boundingBox())!;
    await page.mouse.move(trim.x + 3, trim.y + 30); await page.mouse.down();
    await page.mouse.move(trim.x - 27, trim.y + 30, { steps: 3 }); await page.mouse.up();
    await expect.poll(async () => Number(await first.getAttribute("data-source-end"))).toBeLessThan(96000);
    await page.getByRole("button", { name: "保存项目", exact: true }).click();
    await expect(page.locator(".save-state")).toContainText("已保存");
    const rulerBounds = (await page.locator(".timeline-ruler").boundingBox())!;
    const splitBounds = (await first.boundingBox())!;
    await page.mouse.move(splitBounds.x + splitBounds.width * 0.2, rulerBounds.y + 10); await page.mouse.down();
    await page.mouse.move(splitBounds.x + splitBounds.width * 0.5, rulerBounds.y + 10, { steps: 3 }); await page.mouse.up();
    await section.press("b");
    await expect(page.locator(".timeline-clip")).toHaveCount(3);
    await page.getByRole("button", { name: "保存项目", exact: true }).click();
    await expect(page.locator(".save-state")).toContainText("已保存");
    await section.press("Control+z");
    await expect(page.locator(".timeline-clip")).toHaveCount(2);
    const saved = await read();
    expect(saved.clips.find((clip: { id: string }) => clip.id === a.id).track_id).toBe("voice-track");
    expect(saved.clips.find((clip: { id: string }) => clip.id === a.id).fade_in_ms).toBeGreaterThan(0);
    expect(await (await request.get(saved.assets[0].media_url)).body()).toEqual(sourceBefore);
    await page.screenshot({ path: "test-results/timeline-ux.png", fullPage: true });
  } finally {
    // Only this test's exact project is moved to the reversible recycle bin.
    const current = await read();
    if (current.revision) await request.delete(`${base}/projects/${id}`, { headers, data: { revision: current.revision } });
  }
});
