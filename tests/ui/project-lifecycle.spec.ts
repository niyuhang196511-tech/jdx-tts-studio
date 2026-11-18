import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

async function session(request: APIRequestContext) {
  const response = await request.get("/__dev/session");
  const value = await response.json() as { base_url: string; token: string };
  return { url: value.base_url + "/api/v1", headers: { Authorization: `Bearer ${value.token}` } };
}

async function openWorkbench(page: Page) {
  await page.goto("/");
  const skip = page.getByRole("button", { name: "稍后配置，进入工作台", exact: true });
  if (await skip.waitFor({ state: "visible", timeout: 1500 }).then(() => true).catch(() => false)) await skip.click();
  await expect(page.getByRole("button", { name: "新建项目", exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "项目", exact: true }).click();
}

function fixtureWav(): Buffer {
  const samples = 4800;
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write("RIFF", 0); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(48000, 24); buffer.writeUInt32LE(96000, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34); buffer.write("data", 36); buffer.writeUInt32LE(samples * 2, 40);
  for (let index = 0; index < samples; index++) buffer.writeInt16LE(Math.round(Math.sin(index / 48000 * 880 * Math.PI * 2) * 3000), 44 + index * 2);
  return buffer;
}

test("rename, cancel deletion, trash and restore preserve current project media and text", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const connection = await session(request);
  await openWorkbench(page);
  const initialName = `回收站验收 ${Date.now()}`;
  const renamed = initialName + " 已改名";
  await page.getByRole("button", { name: "新建项目", exact: true }).first().click();
  await page.getByLabel("项目名称", { exact: true }).fill(initialName);
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(initialName);
  const id = await page.evaluate(() => localStorage.getItem("jdx-project"));
  expect(id).toBeTruthy();
  await page.getByRole("button", { name: "粘贴文本", exact: true }).click();
  await page.getByPlaceholder("粘贴需要配音的文本").fill("删除到回收站以后，台词和原始音频仍然保留。");
  await page.getByRole("button", { name: "分句并添加", exact: true }).click();
  await page.locator('input[type="file"][multiple]').setInputFiles({ name: "回收站验收音频.wav", mimeType: "audio/wav", buffer: fixtureWav() });
  await expect(page.locator(".timeline-clip")).toHaveCount(1);
  await page.getByRole("button", { name: "项目", exact: true }).click();
  await page.getByRole("button", { name: `重命名项目：${initialName}`, exact: true }).click();
  await page.getByLabel("新的项目名称", { exact: true }).fill(renamed);
  await page.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(renamed);
  const before = await (await request.get(`${connection.url}/projects/${id}`, { headers: connection.headers })).json();
  const mediaBefore = await (await request.get(before.assets[0].media_url)).body();

  await page.getByRole("button", { name: `删除项目：${renamed}`, exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("生成历史和时间轴都会保留");
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(renamed);
  expect((await request.get(`${connection.url}/projects/${id}`, { headers: connection.headers })).status()).toBe(200);

  // Type immediately before deletion; the delete flow must flush the edit.
  await page.getByLabel("第 1 句台词", { exact: true }).fill("删除前尚未自动保存的最后一次修改。");
  await page.getByRole("button", { name: `删除项目：${renamed}`, exact: true }).click();
  await page.getByRole("button", { name: "移入回收站", exact: true }).click();
  await expect(page.getByRole("button", { name: `打开项目：${renamed}`, exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem("jdx-project"))).toBeNull();
  expect((await request.get(`${connection.url}/projects/${id}`, { headers: connection.headers })).status()).toBe(410);
  expect((await request.get(before.assets[0].media_url)).status()).toBe(410);
  await page.reload();
  await expect(page.getByRole("button", { name: "项目回收站", exact: true })).toBeVisible();
  await expect(page.locator(".line-row")).toHaveCount(0);
  await page.getByRole("button", { name: "项目回收站", exact: true }).click();
  await page.getByRole("button", { name: `恢复项目：${renamed}`, exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(renamed);
  await expect(page.getByLabel("第 1 句台词", { exact: true })).toHaveValue("删除前尚未自动保存的最后一次修改。");
  await expect(page.locator(".timeline-clip")).toHaveCount(1);
  const restored = await (await request.get(`${connection.url}/projects/${id}`, { headers: connection.headers })).json();
  expect(restored.assets[0].id).toBe(before.assets[0].id);
  expect(await (await request.get(restored.assets[0].media_url)).body()).toEqual(mediaBefore);
  expect(errors).toEqual([]);
});

test("managing another project keeps the current editor and stale restored selection is cleared", async ({ page, request }) => {
  const connection = await session(request);
  const create = async (name: string) => (await request.post(`${connection.url}/projects`, { headers: connection.headers, data: { name } })).json();
  const current = await create(`当前项目验收 ${Date.now()}`);
  const other = await create(`另一个项目验收 ${Date.now()}`);
  await openWorkbench(page);
  await page.getByRole("button", { name: `打开项目：${current.name}`, exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(current.name);
  await page.getByRole("button", { name: `重命名项目：${other.name}`, exact: true }).click();
  await page.getByLabel("新的项目名称", { exact: true }).fill(other.name + " 修改");
  await page.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(current.name);
  await page.getByRole("button", { name: `删除项目：${other.name} 修改`, exact: true }).click();
  await page.getByRole("button", { name: "移入回收站", exact: true }).click();
  await expect(page.getByRole("button", { name: `打开项目：${other.name} 修改`, exact: true })).toHaveCount(0);
  await expect(page.locator(".project-heading-name")).toHaveText(current.name);
  expect(await page.evaluate(() => localStorage.getItem("jdx-project"))).toBe(current.id);
  // Simulate another app window deleting the last-open project while this window is closed.
  const latest = await (await request.get(`${connection.url}/projects/${current.id}`, { headers: connection.headers })).json();
  expect((await request.delete(`${connection.url}/projects/${current.id}`, { headers: connection.headers, data: { revision: latest.revision } })).ok()).toBe(true);
  await page.reload();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("jdx-project"))).toBeNull();
  await expect(page.locator(".line-row")).toHaveCount(0);
});
