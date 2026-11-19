# 路线图

> 开发方式：Windows 本机只写代码，不运行；每个阶段的验收都在 Ubuntu 设备上按 [verification.md](verification.md) 执行。

## P0a：核心 + 部署 + 一个可编辑平台 + 一个只发最终结果的平台

> 状态（2026-11-19）：下面的条目都**已编码，但还没有在 Ubuntu 上运行或验证**。勾选只在 verification.md 中对应的场景通过之后进行。

目标：先把会决定后续 27 个平台能否用好的**共性能力**跑通。

- [ ] 项目骨架：`pyproject.toml`（Python ≥3.10，与 `openai-codex` 的要求一致）、`src/codex_im/`、pytest、ruff。
- [ ] `codex/runtime`：封装 `AsyncCodex`，包括：
  - `thread_start` / `thread_resume`；
  - turn 流、`steer`、`interrupt`；
  - 显式设置 `sandbox=full_access` 和 `approval_mode=deny_all`；
  - 每个 thread 和 turn 都显式传 model；
  - 错误分类映射表 `codex/errors.py`（D14）。
- [ ] `core/store`：SQLite（WAL），包括 `inbound`、`threads`、`turns`、`outbox` 和会话表（D14）。
- [ ] `core/sessions`：会话状态机（D15）、steer 或排队策略、项目锁（D6）。
- [ ] `core/outbox` 和 `core/recovery`：投递重试和重启对账（D14）。
- [ ] `core/router` 和命令：`/new` `/stop` `/status` `/sessions` `/resume` `/retry` `/model` `/provider` `/compact` `/help`。
- [ ] `core/render`：节流流式、前缀稳定草稿、编辑落定、分段；`delivery=async` 的消息逐条投递（D8、D13）。
- [ ] 认证：ChatGPT device auth 和自定义 provider 两种模式；entrypoint 生成 `config.toml`，其中包含 `features.default_mode_request_user_input = false`。
- [ ] 部署：两个镜像、compose、egress-proxy 白名单、stdio-over-TCP 中继、`agentd`。
- [ ] 测试设施：`fake` 平台适配器、故障注入点、冒烟测试 `python -m codex_im.smoke`。
- [ ] 平台：**Telegram**（可编辑消息、流式输出）和 **Email**（只发最终结果）。
  - Email：校验 `Authentication-Results`，过滤自动邮件，按邮件线程建会话。
  - 如果 Ubuntu 设备访问不了 Telegram，把飞书换进 P0a，Telegram 移到 P0b。
- [ ] **验收**：verification.md 中的 V0–V9。重点是澄清往返、重复消息、执行中重启、回复失败补发、`/stop` 和 `/new` 的竞态、同项目并发。

## P0b：首批平台的剩余部分

- [ ] **飞书**：长连接、可编辑消息；`FEISHU_DOMAIN` 区分国内和国际。
- [ ] **微信（个人号）**：只发最终结果，生成中显示"正在输入"；`context_token` 需要持久化；扫码登录命令 `python -m codex_im.platforms.weixin.login`（在 bridge 容器里运行，终端显示二维码）。
- [ ] 验收：每个平台都满足 platforms.md 的验收清单，并重跑 V2、V4、V5 中涉及平台的部分。

## P1：其余出站长连接平台（A 类）

P0 未覆盖的全部 A 类平台：Discord、Slack、钉钉、企业微信（智能机器人）、QQ 机器人、Mattermost、Matrix、IRC、ntfy、Google Chat（Pub/Sub 拉取模式）。

另外：管理员 `/login` 命令（在 IM 里完成 ChatGPT device auth）；图片入站。

## P2：Webhook 平台（B 类）+ ingress

LINE、Microsoft Teams、WhatsApp Business（Cloud）、SMS（Twilio）、企业微信（自建应用回调）、Google Chat（HTTP 回调模式）。

另外：bridge 内置的 ingress 服务；Caddy 或隧道的部署文档。

## P3：Sidecar / 特殊平台（C 类）

Signal（signal-cli）、SimpleX、WhatsApp 个人号（Baileys）、iMessage（Photon）、iMessage（BlueBubbles）、Buzz、Raft（需要重新设计成由 bridge 收发）、A2A（只做入站）。

## P4：增强项（按需）

- 语音消息（STT 和 TTS）。
- 阻塞式澄清提问 `item/tool/requestUserInput` 和 Plan 模式（**依赖上游**：SDK 提供公开的服务端请求回调，D13）。
- IM 内人工审批（同样依赖上述上游能力；只在非容器沙箱的部署形态下才有意义）。
- 用 `clientUserMessageId` 精确关联 turn（**依赖上游**：SDK 的 `turn()` 暴露这个字段，D14）。
- 用 gVisor 运行 agent。
- 多实例 / 多租户。

## 已确定不做

- 往 IM 回传 Codex 生成的文件（包括 `send_file` 工具）。
- 群聊按用户拆分会话。
