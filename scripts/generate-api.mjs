import { spawnSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const python = path.join(root, "backend", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const result = spawnSync(python, ["-c", "import json,tempfile;from pathlib import Path;from jdx_backend.app import create_app\nwith tempfile.TemporaryDirectory() as d:\n app=create_app(Path(d),'x'*64);print(json.dumps(app.openapi(),ensure_ascii=False));app.state.store.close()"], {
  cwd: path.join(root, "backend"), encoding: "utf8",
});
if (result.status !== 0) throw new Error(result.stderr);
await mkdir(path.join(root, "docs"), { recursive: true });
await writeFile(path.join(root, "docs", "openapi.json"), result.stdout);
const generate = spawnSync(process.execPath, ["node_modules/openapi-typescript/bin/cli.js", "docs/openapi.json", "-o", "src/generated/api.ts"], { cwd: root, stdio: "inherit" });
process.exitCode = generate.status || 0;
