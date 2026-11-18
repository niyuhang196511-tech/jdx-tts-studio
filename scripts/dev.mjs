import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createInterface } from "node:readline";
import { writeFile, readFile, unlink, mkdir, access } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const port = Number(process.env.JDX_DEV_PORT ?? 1420);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("开发端口须为 1024–65535 的整数");
const dataDir = path.resolve(root, process.env.JDX_DEV_DATA_DIR ?? ".data");
const sessionFile = path.resolve(root, process.env.JDX_DEV_SESSION_FILE ?? ".dev-session.json");
const alreadyRunning = await new Promise(resolve => {
  const socket = net.createConnection({ host: "127.0.0.1", port });
  socket.once("connect", () => { socket.destroy(); resolve(true); });
  socket.once("error", () => resolve(false));
});
if (alreadyRunning) throw new Error(`端口 ${port} 已有工作台运行，请直接打开 http://127.0.0.1:${port}`);
const python = path.join(root, "backend", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
await access(python).catch(() => { throw new Error("请先执行 uv sync --project backend --python 3.12 安装开发依赖"); });
await mkdir(dataDir, { recursive: true });
await mkdir(path.dirname(sessionFile), { recursive: true });
const token = randomBytes(32).toString("hex");
const backend = spawn(python, ["-m", "jdx_backend", "--data-dir", dataDir, "--parent-watch", "--browser-dev"], {
  cwd: path.join(root, "backend"),
  stdio: ["pipe", "pipe", "inherit"],
});
backend.stdin.write(JSON.stringify({ type: "init", session_token: token }) + "\n");
let frontend;
let stopping = false;
async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  frontend?.kill("SIGTERM");
  if (!backend.stdin.destroyed) backend.stdin.end(JSON.stringify({ type: "shutdown" }) + "\n");
  setTimeout(() => backend.kill("SIGTERM"), 4000).unref();
  try {
    const owned = JSON.parse(await readFile(sessionFile, "utf8"));
    if (owned.token === token) await unlink(sessionFile);
  } catch { /* Another session may already have removed its file. */ }
  process.exitCode = code;
}
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => void stop());
backend.on("exit", code => { if (!stopping) void stop(code || 1); });
const timeout = setTimeout(() => { console.error("后端启动超时，请检查 .data/logs/backend.log"); void stop(1); }, 30000);
for await (const line of createInterface({ input: backend.stdout })) {
  let message;
  try { message = JSON.parse(line); } catch { continue; }
  if (message.type !== "ready") continue;
  clearTimeout(timeout);
  await writeFile(sessionFile, JSON.stringify({ base_url: `http://127.0.0.1:${message.port}`, token }), { mode: 0o600 });
  frontend = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], {
    cwd: root, stdio: "inherit", env: { ...process.env, VITE_BACKEND_SESSION_FILE: sessionFile },
  });
  frontend.on("exit", code => void stop(code || 0));
  console.log(`Jdx 开发工作台：http://127.0.0.1:${port}（浏览器开发模式，密钥仅本次使用）`);
}
