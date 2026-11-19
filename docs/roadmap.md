# 路线图

## P0：核心 + 部署 + 首批参考平台

- [ ] 项目骨架：`pyproject.toml`（Python ≥3.10，与 `openai-codex` 的要求一致）、`src/codex_im/`、测试框架、lint。
- [ ] `codex/runtime`：封装 `AsyncCodex`，包括：
  - `thread_start` / `thread_resume`；
  - turn 流、`steer`、`interrupt`；
  - 显式设置 `sandbox=full_access` 和 `approval_mode=deny_all`；
  - 每个 thread 和 turn 都显式传 model。
- [ ] `core/sessions`：SQLite 中的映射、每会话的锁、steer 或排队策略、resume 失败时的降级。
- [ ] `core/router` 和命令：`/new` `/stop` `/status` `/model` `/provider` `/compact` `/help`；运行中可用的命令绕过排队。
- [ ] `core/render`：节流流式、前缀稳定草稿、最终结果用编辑落定、分段、超长输出转文件。
- [ ] 认证：ChatGPT device auth 和自定义 provider 两种模式；entrypoint 生成 `config.toml`。
- [ ] 部署：两个镜像、compose、egress-proxy 白名单、stdio-over-TCP 中继。
- [ ] **验证项**：
  - 中继下 SDK 的初始化、并发 turn、断线重连；
  - Docker 里 Codex `full_access` 能正常执行命令；
  - 实测 ChatGPT 模式需要放行的出网域名。
- [ ] 冒烟测试 `python -m codex_im.smoke`。
- [ ] 首批参考平台：**待确认**（建议飞书和 Telegram）。

## P1：其余出站长连接平台（A 类）

P0 未覆盖的全部 A 类平台：Discord、Slack、钉钉、企业微信（智能机器人）、QQ 机器人、微信（个人号）、Mattermost、Matrix、IRC、Email、ntfy、Google Chat（Pub/Sub 拉取模式）。

另外：管理员 `/login` 命令（在 IM 里完成 ChatGPT device auth）；图片入站。

## P2：Webhook 平台（B 类）+ ingress

LINE、Microsoft Teams、WhatsApp Business（Cloud）、SMS（Twilio）、企业微信（自建应用回调）、Google Chat（HTTP 回调模式）。

另外：bridge 内置的 ingress 服务；Caddy 或隧道的部署文档。

## P3：Sidecar / 特殊平台（C 类）

Signal（signal-cli）、SimpleX、WhatsApp 个人号（Baileys）、iMessage（Photon）、iMessage（BlueBubbles）、Buzz、Raft（需要重新设计成由 bridge 收发）、A2A（只做入站）。

## P4：增强项（按需）

- 出站文件投递（opt-in 的 `send_file` MCP 工具，需要权衡是否算引入了 harness）。
- 语音消息（STT 和 TTS）。
- IM 内人工审批（需要解决 SDK approval handler 同步阻塞的问题；只在非容器沙箱的部署形态下才有意义）。
- 用 gVisor 运行 agent。
- 多实例 / 多租户。
