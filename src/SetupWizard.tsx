import { useState } from "react";
import { Check, Cloud, FileAudio, Loader2, Play, ShieldCheck } from "lucide-react";
import { api, post } from "./api";
import { newLine } from "./lib";
import type { Bootstrap, GenerationJob, Project } from "./types";
import { Button, Modal } from "./ui";

export function SetupWizard({ bootstrap, open, onConfigure, onDone, onProject, notify }: {
  bootstrap: Bootstrap;
  open: boolean;
  onConfigure: () => void;
  onDone: () => void;
  onProject: (project: Project) => void;
  notify: (message: string, error?: boolean) => void;
}) {
  const [step, setStep] = useState(0);
  const [testing, setTesting] = useState(false);
  const [testState, setTestState] = useState("");
  const [testAudio, setTestAudio] = useState("");
  const [testJob, setTestJob] = useState<{ project: string; job: string } | null>(null);
  const preferredId = typeof bootstrap.preferences.default_connection_id === "string" ? bootstrap.preferences.default_connection_id : null;
  const connection = bootstrap.connections.find(item => item.id === preferredId)
    ?? bootstrap.connections.find(item => item.has_credential) ?? bootstrap.connections[0];
  const mediaReady = bootstrap.environment.ffmpeg_available === true;
  const complete = async () => {
    if (testing) return;
    try {
      await api("/preferences", { method: "PATCH", body: JSON.stringify({ first_run_complete: true }) });
      onDone();
    } catch (error) { notify(error instanceof Error ? error.message : "无法保存初始化设置", true); }
  };
  const test = async () => {
    if (!connection?.has_credential) return;
    setTesting(true);
    setTestAudio("");
    setTestState("正在创建测试项目…");
    try {
      const project = await post<Project>("/projects", { name: "首次配音测试" });
      const line = newLine("你好，欢迎使用专业配音工作站。让每一个声音，都有自己的故事。", 0, connection);
      const saved = await api<Project>(`/projects/${project.id}`, { method: "PATCH", body: JSON.stringify({ revision: project.revision, lines: [line] }) });
      onProject(saved);
      const submitted = await post<{ jobs: GenerationJob[] }>(`/projects/${project.id}/tasks`, { line_ids: [line.id] });
      const jobId = submitted.jobs[0].id;
      setTestJob({ project: project.id, job: jobId });
      for (let attempt = 0; attempt < 300; attempt++) {
        const jobs = await api<GenerationJob[]>(`/projects/${project.id}/tasks`);
        const job = jobs.find(item => item.id === jobId);
        if (job?.status === "succeeded") {
          const result = await api<Project>(`/projects/${project.id}`);
          const version = result.lines[0]?.versions.find(item => item.id === job.version_id);
          setTestAudio(result.assets.find(item => item.id === version?.asset_id)?.media_url ?? "");
          setTestState("生成成功，测试音频已保存到项目。");
          onProject(result);
          return;
        }
        if (job && ["failed", "cancelled", "uncertain", "interrupted"].includes(job.status)) throw new Error(job.error ?? "测试已取消");
        setTestState(job?.status === "queued" ? "等待云端任务…" : "正在生成并保存测试音频…");
        await new Promise(resolve => window.setTimeout(resolve, 1000));
      }
      throw new Error("测试等待超时，可在工作台任务队列继续查看。");
    } catch (error) {
      setTestState(error instanceof Error ? error.message : "生成测试失败");
    } finally {
      setTesting(false);
      setTestJob(null);
    }
  };
  return <Modal open={open} onClose={() => { if (!testing) void complete(); }} title="欢迎使用 Jdx TTS Studio" description="首次启动设置" wide>
    <div className="setup-steps">{["准备工作台", "连接百炼", "生成与试听"].map((label, index) => <span key={label} className={step === index ? "active" : ""}>{index < step ? <Check size={13} /> : index + 1} · {label}</span>)}</div>
    {step === 0 && <div className="setup-content">
      <FileAudio size={36} />
      <h2>从一句台词开始，完成整个配音项目。</h2>
      <p>模型在百炼云端运行，项目和生成音频保存在这台电脑。无需下载模型或配置显卡。</p>
      <div className={`setup-check ${mediaReady ? "success" : "error-text"}`}><ShieldCheck size={17} />{mediaReady ? "本地音频处理组件已就绪" : "本地音频组件缺失，请安装完整桌面包后使用音频处理功能。"}</div>
      <p className="muted small">断网时仍可编辑项目、试听已有音频和导出。</p>
    </div>}
    {step === 1 && <div className="setup-content">
      <Cloud size={36} /><h2>连接你的阿里云百炼账户</h2>
      <p>从百炼控制台复制服务地址（Base URL）和 API Key，保存一次即可自动沿用。无需另外填写地域或业务空间 ID。</p>
      <p className="muted small">桌面版优先使用系统凭据库保存密钥；浏览器开发模式仅保存在本次服务会话。</p>
      {connection ? <div className={`setup-check ${connection.has_credential ? "success" : "muted"}`}><Check size={17} />默认连接：{connection.name}{!connection.has_credential && " · 请补充 API Key"}</div> : <p className="muted">尚未配置连接，也可以稍后在工作台设置。</p>}
      <Button onClick={onConfigure}><Cloud size={15} />{connection ? "管理百炼连接" : "配置百炼连接"}</Button>
    </div>}
    {step === 2 && <div className="setup-content">
      <Play size={36} /><h2>确认声音效果</h2>
      <p>进入工作台后，可先在声音库试听官方样音。下面的短句测试会创建真实项目并使用默认连接生成，按你的百炼账户计费。</p>
      {connection && <p className="muted small">使用连接：{connection.name}</p>}
      <Button disabled={!connection?.has_credential || !mediaReady || testing} busy={testing} onClick={() => void test()}><Play size={15} />生成测试配音（按云端计费）</Button>
      {!connection?.has_credential && <p className="muted small">默认连接尚未配置密钥，可以先进入工作台浏览音色或离线编辑。</p>}
      {testState && <p className="setup-check">{testing && <Loader2 size={15} className="spin" />}{testState}</p>}
      {testJob && <Button onClick={() => void post(`/projects/${testJob.project}/tasks/${testJob.job}/cancel`).catch(error => notify(String(error), true))}>请求取消测试</Button>}
      {testAudio && <audio controls src={testAudio} className="reference-player" />}
    </div>}
    <div className="modal-actions">
      <Button variant="ghost" disabled={testing} onClick={() => void complete()}>稍后配置，进入工作台</Button>
      {step > 0 && <Button disabled={testing} onClick={() => setStep(value => value - 1)}>上一步</Button>}
      {step < 2 ? <Button variant="primary" onClick={() => setStep(value => value + 1)}>下一步</Button> : <Button variant="primary" disabled={testing} onClick={() => void complete()}>进入配音工作台</Button>}
    </div>
  </Modal>;
}
