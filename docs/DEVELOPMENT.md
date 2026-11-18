# 开发记录

## 2026-11-17：从空目录建立远程配音工作站

需求中的 V1/V2 是误写。项目没有既有代码或 Git 历史。用户确认首版仅使用远程模型，并专注百炼服务；原来的本地模型、CUDA 和 GPU 方案已取消。

### 阶段一：工程和契约

建立 React/TypeScript、Tauri 2、Python 3.12/FastAPI 工程，锁定前后端依赖，定义项目、台词、版本、片段、声音、连接与任务契约，并生成 OpenAPI 类型。实现应用库和项目库分离、SQLite 迁移备份及安全路径检查。

### 阶段二：桌面集成

实现独立后端运行目录、stdin 初始化令牌、动态 loopback 端口握手、健康检查、单实例、异常退出状态和整棵进程树回收。Windows/Linux 凭据存储分平台实现；钥匙环不可用时只保留内存密钥。添加原生导出保存对话框。

当前 Linux 环境缺少系统开发包且无免密 sudo，通过项目内解包开发依赖完成原生编译验证，未修改系统包。干净目标发行版仍需单独测试。

### 阶段三：核心配音

实现百炼 HTTP 与 WebSocket 合成、连接只读验证、音色克隆/设计/查询、逐句与批量队列、去重、取消状态、历史版本、不可覆盖音频落盘及服务端用量保存。云端协议测试通过本地 HTTP/WS 测试服务执行，真实付费账户未提供。

### 阶段四：专业编辑

实现规则导演计划、批量参数、片段移动/裁剪/分割、增益、淡入淡出、静音、速度、时间轴缩放吸附、波形以及历史选择。试听与导出共享 FFmpeg 渲染。

并发测试发现后句先完成时自动排布可能重叠，已通过台词顺序和自动位置记录修复；用户手动移动与混音参数保留。取消与结果发布在同一项目锁内协调，避免取消后迟到结果进入当前时间轴。

### 阶段五：视频与数据恢复

实现字幕关联、视频代理转码、音视频位置同步、WAV/MP3/逐句/CSV/完整项目导出。实现项目备份、恢复为新项目、缺失素材检查及恢复校验。

备份测试发现 SQLite WAL 未落入打包快照的问题，已在归档前显式 checkpoint 并关闭快照连接，恢复测试通过。

### 阶段六：产品体验

实现中文深浅主题、可调整面板、持久化布局、快捷键、右键操作、明确空状态、加载进度和错误反馈。前端调用真实本地 API；不注入演示项目或模拟生成结果作为产品功能。

### 阶段七：验证与构建

执行 Python 协议/存储/音频/队列测试、TypeScript 和生产构建、浏览器工作流验证、Rust 编译检查以及 PyInstaller 冻结后端启动检查。每类验证的最终结果及尚未验证项统一记录在 `TEST_REPORT.md`。

最终记录：后端 73 项、前端单元测试 7 项、浏览器端到端测试 2 项、Rust 原生测试 8 项全部通过。Linux deb 与 AppImage 已构建；AppImage 默认启动及 deb 解包布局启动通过。500 句队列验证使用明确测试替身，未调用真实付费模型。声音设计预览、首次启动向导、A/B 试听、按词停顿/发音修正、参考单价与请求限流均已接入。项目规则写入根目录 `AGENTS.md`。

## 2026-11-17：0.1.1 日常工作流修复

- 修复 HTTPX 初始化时把 `ALL_PROXY=socks://…` 作为不支持协议解析而报错的问题。增加按目标地址选择的代理解析、SOCKS 依赖、WebSocket 代理兼容和中文错误信息；保留 NO_PROXY 与本地直连，不修改全局环境。
- 增加项目重命名、逻辑回收站和恢复。应用数据库迁移为 v3 并备份；项目数据库仍为 v2。删除保留整个项目和音频，旧媒体访问链接在回收站状态下拒绝访问。
- 阻止生成/导出/导入期间删除项目；检查过期修订号；项目切换时不再接收旧项目异步返回的内容。
- 修复 TXT/SRT/VTT 导入的新台词没有继承已配置连接的问题。
- 连接配置或 API Key 尚未保存时，禁用只读连接检测并提示先保存，避免检查旧配置导致误判。
- 增加真实本地 SOCKS HTTP 握手、WebSocket 隧道以及项目删除/恢复的回归测试。真实百炼付费效果验证仍未进行。

本轮源码验证：后端 111 项、前端单元测试 7 项、浏览器端到端测试 4 项、Rust 原生测试 8 项通过；类型生成、类型检查、生产构建及 Python 语法检查通过。冻结运行目录额外通过 7 项基础检查和 3 项本地代理协议检查。0.1.1 Linux deb 和 AppImage 已构建；deb 解包布局、AppDir 内后端、AppImage 默认启动与解包启动均通过检查，正常关闭后后端已回收。旧版文件保留，详细边界见 `TEST_REPORT.md`。

## 2026-11-17：0.1.2 声音库和连接易用性

- 直接从阿里云官方音色列表表格整理 157 个模型/音色条目及官方静态样音 URL，严格保留模型兼容范围与官方语言、性别、声音特质、场景和地域说明；共 153 个不同音色 ID。
- 系统音色试听按点击下载官方样音，固定官方域名白名单、HTTPS、不跟随跳转、文件格式/大小/时长检查、FFmpeg 音频规范化及原子落盘，使用本地短时签名媒体链接。试听不发起付费合成，也无需 API Key。
- 用户克隆参考录音可试听，但标记为参考录音；声音设计使用百炼返回的设计样音。不会把参考录音伪装成生成效果，也不在没有样音时自动提交收费请求。
- 持久化默认连接和官方音色收藏。首个连接成为默认，新建及导入台词使用已保存默认，已有显式配置不会被改写。
- 新连接允许仅填写 HTTPS Base URL、API Key 和名称；内网/本机地址仍拒绝。旧 workspace + region 配置展开为原服务地址。自定义服务域名不套用百炼地域规则，切换域名后清空已过时的 workspace/region 推断值；专属音色仍通过只读云端校验。
- 全量后端 125 项、`pnpm api:types`、TypeScript 检查及生产构建通过。公开官方样音跨 Plus 中文、Flash 中文、英文及小语种四例下载/解码/规范化通过；没有真实账户合成或付费请求。细节见 `VOICE_LIBRARY.md` 与测试报告。

### 时间轴操作改进与竞品参考

- 片段支持多选和跨轨拖动；拖动时显示预览，松开后只提交一次编辑。片段边缘与播放头支持吸附，Alt 可临时绕过吸附，并显示对齐参考线。
- 补充拖拽裁剪、淡入淡出手柄、适配全部/选中片段、右键操作，以及复制、分割、静音、删除、方向键微调等常用操作。编辑状态仍使用固定 48 kHz 采样基准，试听和导出共用渲染描述，源音频不可变。
- 交互研究参考 [Premiere Pro 的片段吸附](https://helpx.adobe.com/premiere/desktop/edit-projects/change-clip-sequence/snap-clips.html)、[Audition 的多轨片段排列与编辑](https://helpx.adobe.com/audition/desktop/mixing-multitrack-sessions/arranging-editing-multitrack-clips.html)及[淡化控制](https://helpx.adobe.com/audition/desktop/mixing-multitrack-sessions/clip-volume-matching-fading-mixing.html)：强调对齐提示、直接操作手柄、非破坏性编辑和上下文操作。首版没有实现交叉淡化、可编辑淡化曲线或商业软件完整混音器，不能将其描述为已有能力。
- 声音库参考官方音色名称、模型兼容范围、元数据和公开样音；增加试听、搜索、按模型/语言/声线筛选、收藏及应用到台词的直接流程。重复模型条目按唯一音色分组，在卡片中选择目标模型，避免同一声音重复占据列表。
- 官方音色卡片直接展示音色 ID，并提供一键复制和成功/失败提示；连接和台词流程仍优先让用户按可读名称选择声音。

0.1.2 最终回归：后端 125 项、Vitest 20 项、Playwright 7 项通过；`pnpm api:types`、类型检查、生产构建及 Python 打包脚本语法检查通过。Linux deb、AppImage 与源码包已生成；AppImage 两种启动方式及 deb 解包后的桌面生命周期检查均通过。无真实百炼账户调用，Windows 与其他 Ubuntu 版本未验证，详见 `TEST_REPORT.md`。

## 官方依据（接口）

核验时间：2026-11-17。直接读取官方正文及示例，记录实际接口而非根据截图猜测：

- 合成 HTTP：`https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api`
- 流式协议：`https://help.aliyun.com/zh/model-studio/qwen-audio-tts-websocket-api`
- 客户端取消：`https://help.aliyun.com/zh/model-studio/qwen-audio-tts-client-events`
- 服务端事件及用量：`https://help.aliyun.com/zh/model-studio/qwen-audio-tts-server-events`
- 声音克隆：`https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide`
- 音色管理：`https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api`
- 声音设计：`https://help.aliyun.com/zh/model-studio/voice-design-api-references`
- SSML 支持范围：`https://help.aliyun.com/zh/model-studio/ssml-latex-user-guide`

未把旧 Qwen3-TTS 的请求结构套用到 Qwen-Audio 3.1。模型支持、音色支持、地域和协议模式分别判断。
