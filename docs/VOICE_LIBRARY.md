# 声音库与连接体验改进

2026-11-17，0.1.2 开发记录。

## 官方目录

根据[百炼官方 Qwen-Audio-TTS 音色列表](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list)的实际表格提取名称、音色 ID、模型归属、性别、语言、标签、能力和公开样音地址。代码中的目录快照在 `backend/jdx_backend/voice_catalog.py`。

| 模型 | 音色条目 |
| --- | ---: |
| qwen-audio-3.1-tts-plus | 4 |
| qwen-audio-3.1-tts-flash | 139 |
| qwen-audio-3.0-tts-plus | 2 |
| qwen-audio-3.0-tts-flash | 12 |

共 157 个模型/音色条目，153 个不同音色 ID；同一 ID 在 Plus/Flash 的能力和试听素材可能不同。不能将 Flash 音色用于 Plus。3.1 Flash 的多语种/方言、精品英文、小语种支持北京和新加坡，其余组按官方说明仅开放北京。选择 Base URL 后内部解析地域，生成前检查已知地域限制。对自定义非百炼 Base URL，地区显示为“自定义”，不套用官方地区可用性判断；私有音色绑定也不因业务 ID/地区信息为空而误报错，但云端仍可拒绝实际服务权限。系统音色收藏使用 `PATCH /preferences` 的 `favorite_voice_ids: string[]`，不修改官方内置目录。

## 免费样音与本地缓存

`POST /api/v1/voices/{id}/preview` 使用本地会话认证，返回带 `preview_url` 的 `VoiceProfile`。

- 系统声音仅从预置的官方目录地址下载样音，不调用模型，不使用 API Key。复制或本地保存系统音色时，后端按 `(voice_id, target_model)` 补齐官方元数据及样音关联。
- 只允许两个官方静态资源域名，限制 HTTPS、WAV、大小和时长，不跟随重定向，不接受用户传入下载地址。
- 下载后校验音频，通过后台 FFmpeg 规范化，再以不可覆盖的原子链接保存；重复点击复用缓存。
- 播放使用短期签名链接及 Range，缓存后可离线试听；会话令牌不出现在素材 URL 中。
- 声音设计播放官方返回的设计样音。声音克隆保留已验证的参考录音并标记 `preview_source=reference`，不能把参考录音称为克隆合成效果。
- 原有无样音的声音明确报错，不偷偷生成收费短句。

`VoiceProfile` 新增 `gender`、`languages`、`tags`、`category`、`regions`、`source_url`、`preview_available`、`preview_source`；系统能力也提供 `instruction`、`word_timestamps`。样音下载地址不接受前端改写。

## 服务地址与默认连接

依据[官方 HTTP API](https://help.aliyun.com/zh/model-studio/qwen-audio-tts-http-api)，用户可直接填写包含业务空间域名的 HTTPS 服务根地址。内部识别业务空间和地域以保留专属音色绑定校验，界面无需再次输入这两个字段。旧版连接自动展开成服务根地址，仍可使用。

首个连接自动成为默认；通过 `PATCH /preferences` 的 `default_connection_id` 记忆选择。导入台词继承默认配置，无显式连接的旧台词生成时使用默认连接。已有台词显式指定的账户、模型和音色不会被覆盖。默认连接缺少密钥时提示补充密钥，不能自行换成另一个可能计费的账户。

## 本轮验证

- 后端完整回归 125 项通过；测试覆盖目录范围、连接默认值持久化、自定义服务地址变更、系统音色副本元数据关联、系统音色收藏持久化、专属音色校验、免费样音缓存、签名/Range、下载地址限制及不跟随重定向。
- 前端 Vitest 20 项通过，包含 3 项声音库去重、过滤/搜索相关测试；TypeScript 检查与生产构建通过。
- 4 条真实官方公开样音（Plus 中文、Flash 中文、英文、小语种）完成下载、解码和规范化。记录在 `build/official-preview-verification-0.1.2.json`，供交付时归档。
- 未进行真实账户合成、克隆或音色设计；测试中的生成和克隆响应使用明确测试替身。
