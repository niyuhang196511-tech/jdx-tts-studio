import { expect, test } from "@playwright/test";

function toneWav(): Buffer {
  const rate = 48000;
  const samples = rate * 2;
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write("RIFF", 0);
  buffer.writeUInt32LE(buffer.length - 8, 4);
  buffer.write("WAVEfmt ", 8);
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(rate, 24);
  buffer.writeUInt32LE(rate * 2, 28);
  buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36);
  buffer.writeUInt32LE(samples * 2, 40);
  for (let index = 0; index < samples; index++) {
    const envelope = Math.min(1, index / 2400, (samples - index) / 2400);
    buffer.writeInt16LE(Math.round(Math.sin(index * 440 * Math.PI * 2 / rate) * 6500 * envelope), 44 + index * 2);
  }
  return buffer;
}

test("offline editing, real media import, timeline rendering and WAV export", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  const skipSetup = page.getByRole("button", { name: "稍后配置，进入工作台", exact: true });
  if (await skipSetup.waitFor({ state: "visible", timeout: 2000 }).then(() => true).catch(() => false)) await skipSetup.click();
  await expect(page.getByRole("button", { name: "新建项目", exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "新建项目", exact: true }).first().click();
  const projectName = `界面验收 ${Date.now()}`;
  await page.getByLabel("项目名称", { exact: true }).fill(projectName);
  await page.getByRole("button", { name: "创建项目", exact: true }).click();
  await expect(page.locator(".project-heading-name")).toHaveText(projectName);

  await page.getByRole("button", { name: "粘贴文本", exact: true }).click();
  await page.getByPlaceholder("粘贴需要配音的文本").fill("清晨，城市慢慢苏醒。每一个声音，都有自己的故事！让我们从这里开始。");
  await page.getByRole("button", { name: "分句并添加", exact: true }).click();
  await expect(page.locator(".line-row")).toHaveCount(3);
  await page.getByLabel("第 1 句台词", { exact: true }).fill("清晨，城市在第一缕阳光里苏醒。");
  await page.getByRole("button", { name: "保存项目", exact: true }).click();
  await expect(page.locator(".save-state")).toContainText("已保存");
  await page.reload();
  await expect(page.getByLabel("第 1 句台词", { exact: true })).toHaveValue("清晨，城市在第一缕阳光里苏醒。");

  await page.locator('input[type="file"][multiple]').setInputFiles({
    name: "验收用测试音频.wav", mimeType: "audio/wav", buffer: toneWav(),
  });
  await page.getByRole("button", { name: "素材", exact: true }).click();
  await expect(page.locator(".asset-item").filter({ hasText: "验收用测试音频.wav" })).toBeVisible();
  await page.locator(".asset-item").filter({ hasText: "验收用测试音频.wav" }).getByRole("button", { name: "加入时间轴", exact: true }).click();
  await expect(page.locator(".timeline-clip")).toHaveCount(2);
  await page.locator(".timeline-clip").first().click();
  await page.getByLabel("开始位置（秒）", { exact: true }).fill("0.5");
  await page.getByLabel("淡入（毫秒）", { exact: true }).fill("100");
  await page.getByRole("button", { name: "保存项目", exact: true }).click();
  await expect(page.locator(".save-state")).toContainText("已保存");

  await page.getByRole("button", { name: "播放时间轴", exact: true }).click();
  await expect(page.getByRole("button", { name: "暂停", exact: true })).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await page.getByRole("button", { name: "导出", exact: true }).click();
  await page.getByLabel("导出内容", { exact: true }).selectOption("wav");
  await page.getByLabel("文件名", { exact: true }).fill("界面验收导出");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "开始导出", exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.wav$/);
  await download.saveAs("test-results/ui-export.wav");
  await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).last().click();
  await page.screenshot({ path: "test-results/ui-workbench.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("connection pricing and limits persist without a cloud synthesis call", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "云端模型设置", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "云端模型设置", exact: true }).click();
  await page.getByRole("button", { name: "添加连接", exact: true }).click();
  await page.getByLabel("连接名称", { exact: true }).fill("配置界面验收");
  await page.getByLabel("服务地址（Base URL）", { exact: true }).fill("https://test-workspace.cn-beijing.maas.aliyuncs.com");
  await page.getByRole("button", { name: "高级连接设置", exact: true }).click();
  await page.getByLabel("每分钟请求上限", { exact: true }).fill("60");
  await page.getByLabel("字符计费单价（元 / 万字符）", { exact: true }).fill("2.5");
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeDisabled();
  const response = page.waitForResponse(value => value.url().endsWith("/api/v1/connections") && value.request().method() === "POST");
  await page.getByRole("button", { name: "保存连接", exact: true }).click();
  const saved = await (await response).json();
  expect(saved.price_per_10k_chars).toBe(2.5);
  expect(saved.requests_per_minute).toBe(60);
  expect(saved.has_credential).toBe(false);
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeEnabled();
  await page.getByLabel("服务地址（Base URL）", { exact: true }).fill("https://unsaved-workspace.cn-beijing.maas.aliyuncs.com");
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeDisabled();
  await expect(page.getByText("连接配置有未保存的修改，请先保存连接后再检测。", { exact: true })).toBeVisible();
  await page.getByLabel("服务地址（Base URL）", { exact: true }).fill("https://test-workspace.cn-beijing.maas.aliyuncs.com");
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeEnabled();
  await page.getByLabel("API Key", { exact: true }).fill("unsaved-test-fixture-only");
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeDisabled();
  await page.getByLabel("API Key", { exact: true }).fill("");
  await expect(page.getByRole("button", { name: "检测连接", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: /配置界面验收/ })).toBeVisible();
  page.once("dialog", dialog => dialog.accept());
  const deleted = page.waitForResponse(value => value.url().endsWith(`/api/v1/connections/${saved.id}`) && value.request().method() === "DELETE");
  await page.getByRole("button", { name: "删除连接", exact: true }).click();
  expect((await deleted).ok()).toBe(true);
  await expect(page.getByRole("button", { name: /配置界面验收/ })).toHaveCount(0);
});
