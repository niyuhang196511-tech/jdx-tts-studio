import { expect, test } from "@playwright/test";

function sampleWav() {
  const buffer = Buffer.alloc(44 + 48000 * 2);
  buffer.write("RIFF", 0); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(48000, 24); buffer.writeUInt32LE(96000, 28); buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36); buffer.writeUInt32LE(buffer.length - 44, 40);
  for (let index = 0; index < 48000; index++) buffer.writeInt16LE(Math.round(Math.sin(index / 48000 * 440 * Math.PI * 2) * 1200), 44 + index * 2);
  return buffer;
}

test("voice catalog previews, filters, favorites and applies compatible named voices", async ({ page, request }) => {
  const session = await (await request.get("/__dev/session")).json();
  const url = session.base_url + "/api/v1";
  const headers = { Authorization: `Bearer ${session.token}` };
  const original = await (await request.get(url + "/bootstrap", { headers })).json();
  const connection = await (await request.post(url + "/connections", { headers, data: {
    name: "声音库专用验收连接", base_url: "https://voice-ui.cn-beijing.maas.aliyuncs.com",
  } })).json();
  const project = await (await request.post(url + "/projects", { headers, data: { name: `声音库验收 ${Date.now()}` } })).json();
  let cloudSubmissions = 0;
  page.on("request", req => { if (req.method() === "POST" && /\/(tasks|voices\/clone|voices\/design)$/.test(req.url())) cloudSubmissions++; });
  try {
    await request.patch(url + "/preferences", { headers, data: { default_connection_id: connection.id, favorite_voice_ids: [] } });
    const imported = await (await request.post(`${url}/projects/${project.id}/import`, { headers, multipart: {
      file: { name: "声音试听界面测试.wav", mimeType: "audio/wav", buffer: sampleWav() },
    } })).json();
    await page.goto("/");
    const skip = page.getByRole("button", { name: "稍后配置，进入工作台", exact: true });
    if (await skip.waitFor({ state: "visible", timeout: 1500 }).then(() => true).catch(() => false)) await skip.click();
    await page.getByRole("button", { name: "项目", exact: true }).click();
    await page.getByRole("button", { name: `打开项目：${project.name}`, exact: true }).click();
    await page.getByRole("button", { name: "添加台词", exact: true }).first().click();
    await page.getByLabel("第 1 句台词", { exact: true }).fill("先试听，再选择适合故事的声音。");
    await page.getByRole("button", { name: "保存项目", exact: true }).click();
    await expect(page.locator(".save-state")).toContainText("已保存");
    let saved = await (await request.get(`${url}/projects/${project.id}`, { headers })).json();
    expect(saved.lines[0].settings.connection_id).toBe(connection.id);
    await page.getByRole("button", { name: "声音", exact: true }).click();
    expect(await page.locator(".voice-card").count()).toBeGreaterThan(100);
    // A single official voice shared by Plus and Flash is displayed once.
    await expect(page.locator('.voice-card[data-voice-id="longanhuan_v3.1"]')).toHaveCount(1);
    await page.getByLabel("筛选音色模型", { exact: true }).selectOption("qwen-audio-3.1-tts-plus");
    await expect(page.locator(".voice-card")).toHaveCount(4);
    await page.getByLabel("筛选音色模型", { exact: true }).selectOption("qwen-audio-3.1-tts-flash");
    expect(await page.locator(".voice-card").count()).toBeGreaterThan(100);
    await page.getByLabel("搜索声音", { exact: true }).fill("longanhuan_v3.1");
    await expect(page.locator(".voice-card")).toHaveCount(1);
    const card = page.locator(".voice-card");
    const bootstrap = await (await request.get(url + "/bootstrap", { headers })).json();
    const voice = bootstrap.voices.find((item: { voice_id: string; target_model: string }) => item.voice_id === "longanhuan_v3.1" && item.target_model === "qwen-audio-3.1-tts-flash");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await card.getByRole("button", { name: `复制音色 ID：${voice.name}`, exact: true }).click();
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(voice.voice_id);
    let previews = 0;
    // UI playback uses a local fixture; actual official-CDN download is a separate backend check.
    await page.route("**/api/v1/voices/*/preview", async route => {
      previews++;
      await route.fulfill({ json: { ...voice, preview_url: imported.assets[0].media_url, preview_source: "official" } });
    });
    await card.getByRole("button", { name: /^试听：/ }).click();
    await expect(page.getByLabel("音色试听进度", { exact: true })).toBeEnabled();
    expect(previews).toBe(1);
    await card.getByRole("button", { name: /^收藏：/ }).click();
    await expect(card.getByRole("button", { name: /^取消收藏：/ })).toBeVisible();
    await card.getByRole("button", { name: "使用", exact: true }).click();
    await page.getByRole("button", { name: "保存项目", exact: true }).click();
    await expect(page.locator(".save-state")).toContainText("已保存");
    saved = await (await request.get(`${url}/projects/${project.id}`, { headers })).json();
    expect(saved.lines[0].settings).toMatchObject({ voice_id: voice.voice_id, model_id: voice.target_model, connection_id: connection.id });
    await page.reload();
    await page.getByRole("button", { name: "声音", exact: true }).click();
    await page.getByRole("button", { name: "我的收藏", exact: true }).click();
    await expect(page.locator('.voice-card[data-voice-id="longanhuan_v3.1"]')).toBeVisible();
    await page.getByRole("button", { name: "全部声音", exact: true }).click();
    await page.getByRole("button", { name: "展开声音库", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText("挑选配音声音");
    await page.screenshot({ path: "test-results/voice-library-dark.png", fullPage: true });
    await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("button", { name: "切换浅色主题", exact: true }).click();
    await page.getByRole("button", { name: "展开声音库", exact: true }).click();
    await page.screenshot({ path: "test-results/voice-library-light.png", fullPage: true });
    expect(cloudSubmissions).toBe(0);
  } finally {
    await request.patch(url + "/preferences", { headers, data: {
      default_connection_id: original.preferences.default_connection_id ?? null,
      favorite_voice_ids: original.preferences.favorite_voice_ids ?? [],
    } });
    const latest = await (await request.get(`${url}/projects/${project.id}`, { headers })).json();
    if (latest.revision) await request.delete(`${url}/projects/${project.id}`, { headers, data: { revision: latest.revision } });
    await request.delete(`${url}/connections/${connection.id}`, { headers });
  }
});
