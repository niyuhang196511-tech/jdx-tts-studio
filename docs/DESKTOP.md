# 桌面构建与运行

## 用户安装

Windows 10/11 x64 使用 NSIS 安装包。安装包内置 WebView2 离线安装程序、Python 后端完整运行目录及 FFmpeg；用户无需安装 Python、Node.js、Rust 或 CUDA。

Linux x64 提供 deb 和 AppImage。正式发布应在最低支持的 Ubuntu 环境构建，并分别在 Ubuntu 22.04/24.04 桌面验证；当前机器的构建结果不能代替这些安装测试。AppImage 使用系统内核和部分桌面组件，不能承诺支持所有发行版。

应用数据使用 Tauri 的用户本地数据目录，卸载程序不主动删除项目、历史音频或用户偏好。日志保存在该目录的 `logs` 子目录。

## 开发前置条件

- Node.js 22、pnpm、uv、Python 3.12、Rust 1.90 或更高版本。
- Windows：Visual Studio C++ Build Tools、Windows SDK 和可再分发的 x64 FFmpeg/FFprobe。
- Ubuntu：`libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev librsvg2-dev libayatana-appindicator3-dev patchelf libgtk-3-dev`；打包视频代理支持还需 GStreamer good/bad/ugly/libav 插件。
- FFmpeg 和 FFprobe 必须来自同一目标平台的可再分发构建。分发者需核对构建许可证与相应义务；脚本保留 `ffmpeg -L` 输出。不能仅复制依赖宿主共享库的可执行文件并假定另一台机器能够运行。

在项目根目录完成 `uv sync --project backend --group dev --locked` 和 `pnpm install --frozen-lockfile` 后，运行 `pnpm exec tauri icon src-tauri/icons/app-icon.svg` 生成原生图标，再运行 `pnpm exec tauri dev`。Tauri 启动前端开发服务器，并自动启动 `backend/.venv` 中的后端，不需要另开终端运行 FastAPI。

## 构建

Linux 执行 `bash scripts/build-linux.sh`；Windows 执行 `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1`。

可通过 `JDX_FFMPEG` 和 `JDX_FFPROBE` 指定 FFmpeg 与 FFprobe 的绝对路径。脚本在各自平台运行 PyInstaller `onedir`，将入口、`_internal`、音频工具和摘要清单整体复制进 Tauri 资源目录。禁止只分发 `jdx-backend` 入口文件，也不能将 Linux 冻结产物用于 Windows。

`backend/.venv/bin/python scripts/build-backend.py --check-only` 可检查 Linux 后端打包前置条件；Windows 使用对应的 `Scripts/python.exe`。

## 桌面与后端协议

1. Rust 生成每次启动独立的 256-bit 会话令牌，启动后端并通过 stdin 发送 `{"type":"init","session_token":"..."}`。令牌不进入命令行或环境变量。
2. 后端绑定 `127.0.0.1:0`，stdout 仅输出一行 `{"type":"ready","protocol":1,"port":12345,"pid":123}`；日志写 stderr。
3. Rust 验证协议和进程 ID，并携带令牌检查 `/api/v1/health`，成功后发布 `backend-status` 事件。
4. `backend_info` 返回 `{base_url, token}`；`backend_status` 返回启动状态；`retry_backend` 回收旧进程后重启。
5. `set_credential(connectionId, apiKey)` 尝试保存系统凭据库，再通过 stdin 的 `secret` 消息交给后端。`sync_credentials(connectionIds)` 在打开连接配置后恢复已有凭据。参数名称在 JavaScript invoke 中使用 camelCase。
6. `credential_status(connectionId)` 返回 `configured`、`storage` 和中文提示，不返回密钥。`storage` 为 `system`、`session`、`none` 或 `unavailable`。
7. `open_logs` 和 `open_data_dir` 只打开由应用确定的目录，不接受任意路径。
8. `delete_credential(connectionId)` 在连接删除前清除系统凭据、本次会话密钥和后端内存密钥。系统凭据删除失败时保留连接配置；仅本次使用的密钥可在无钥匙环环境清除。
9. `save_export(url, suggestedName)` 校验当前本地服务地址和素材路径后打开原生保存对话框；只创建新文件，不覆盖已有文件。`open_export_folder` 打开最近成功保存的位置。

Windows 使用带 `KILL_ON_JOB_CLOSE` 的 Job Object 管理进程树；Linux 后端使用独立进程组，并通过 stdin 断开检测配合父进程退出回收。后端应在初始化消息到达前禁止启动子进程。

Windows 使用 Credential Manager；Linux 使用 Secret Service。钥匙环不存在或锁定时保留“仅本次使用”，不回退到明文配置文件。项目导出和诊断包必须排除凭据。

## 验证记录要求

本文件描述实现与构建方式，不代表对应安装测试已经通过。发布报告必须分别列出 Rust 检查、原生构建、安装启动、进程清理、凭据保存恢复和 Linux 无钥匙环回退的真实结果。无 Windows 环境时，Windows 安装和凭据管理器测试标为未验证。

### 当前主机验证（2026-11-17）

在 Ubuntu 26.04.1 LTS x86_64 上完成 Rust 原生测试、release 编译、deb/AppImage 构建和桌面自动后端启动检查。开发包通过项目内解包方式提供，未使用 sudo 安装系统依赖。这些结果不能替代 Ubuntu 22.04/24.04 的兼容性或干净系统安装测试。

冻结后端通过独立环境、中文路径、动态端口、认证、音频导入、FFmpeg WAV 导出和父进程管道断开退出检查。可复现命令：

```sh
uv run --project backend python scripts/smoke-runtime.py src-tauri/resources/backend/jdx-backend
```

Windows Credential Manager 与 Job Object 模块已在 Linux 上针对 `x86_64-pc-windows-gnu` 执行编译检查；这只验证平台代码类型和 API，不代表 Windows 完整应用构建、安装或运行已通过。

### 0.1.1 代理和项目管理修复交付

Linux 安装包在 `release/`，版本为 0.1.1；旧版 0.1.0 保留。最终 AppImage 默认启动及 `--appimage-extract-and-run`、桌面窗口正常关闭、后端回收均已在当前主机验证。deb 只完成解包布局检查，未执行系统安装。

运行目录包含 HTTPX SOCKS 和 WebSocket SOCKS 依赖。增加以下独立检查，使用本地拒绝代理验证协议与错误处理，不请求真实云端：

```sh
uv run --project backend python scripts/smoke-proxy-runtime.py src-tauri/resources/backend/jdx-backend
```

应用代理设置由操作系统环境继承；当前用户使用的 `ALL_PROXY=socks://127.0.0.1:7897` 与 HTTP/HTTPS 代理并存配置已验证不会阻止应用启动。检查结果见 `docs/verification/*0.1.1.json`。

### 0.1.2 声音库和连接体验交付

Linux deb、AppImage 和源码 ZIP 位于 `release/`。应用在 Ubuntu 26.04.1 x86_64 构建；当前主机使用项目内解包的开发依赖，没有安装系统开发包。AppImage 默认启动与 `--appimage-extract-and-run` 均通过中文临时数据目录、后端自动启动、动态 loopback 端口认证和关闭回收检查。deb 经解包后执行同一检查，没有安装到系统。

0.1.2 的冻结后端、代理和桌面运行证据保存在 `docs/verification/`。Windows 安装包、Ubuntu 22.04/24.04 兼容性及干净系统安装仍待目标环境验证；真实百炼付费接口未在构建或 UI 测试中调用。

## 官方实现依据

- Tauri 配置与资源：`https://v2.tauri.app/reference/config/`、`https://v2.tauri.app/develop/resources/`
- 单实例插件：`https://github.com/tauri-apps/plugins-workspace/tree/v2/plugins/single-instance`
- 凭据 API：`https://docs.rs/keyring-core/1.0.1/keyring_core/`
- Windows 存储：`https://docs.rs/windows-native-keyring-store/1.1.0/`
- Linux 存储：`https://docs.rs/zbus-secret-service-keyring-store/1.0.1/`
- PyInstaller 运行目录：`https://pyinstaller.org/en/stable/operating-mode.html`
