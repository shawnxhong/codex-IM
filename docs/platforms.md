# 平台清单

来源：`hermes-agent/plugins/platforms/*`（@ `3371c03`）。Hermes 有 26 个平台插件目录；其中 `wecom` 注册了 `wecom` 和 `wecom_callback` 两种接入方式，所以一共是 **27 个平台**。本项目全部覆盖。

- 凭证的环境变量名和 Hermes 保持一致（设计决策 D11）。每个平台还有 `<PLATFORM>_ALLOWED_USERS` 白名单，以及只用于开发的 `<PLATFORM>_ALLOW_ALL_USERS`，下表不再逐一列出。
- 依赖版本是 Hermes 当前锁定的版本，仅作参考，实现时以本项目自己的锁定为准。
- 各平台的渲染能力（能否编辑消息、单条长度上限、Markdown 方言）在实现适配器时用 `Capabilities` 声明，并回填到本表。

## P0 平台实现要点

| 平台 | 流式输出 | 会话键 | 要点（参考 Hermes 的实现） |
|---|---|---|---|
| 飞书 | 编辑消息 | 私聊 / 群（话题群按话题） | `lark-oapi` 长连接；`FEISHU_DOMAIN` 区分飞书（国内）和 Lark（国际）；群聊必须 @ 机器人；Markdown 转成飞书富文本或卡片 |
| Telegram | 编辑消息（`editMessageText`） | 私聊 / 群（forum 群按 topic） | 长轮询；同一个 token 只能有一个进程在轮询，**不能和宿主机上的 Hermes 共用 bot**；Markdown 转成 Telegram HTML |
| 微信（个人号） | 不支持编辑，只发最终结果，生成中显示"正在输入"（`sendtyping`） | 私聊 / 群 | iLink 长轮询 `getupdates`；**每条回复都必须带上对方最新的 `context_token`**（需要持久化）；扫码登录（`get_bot_qrcode` / `get_qrcode_status`）；依赖 aiohttp、cryptography；纯文本输出 |
| Email | 不支持编辑，只发最终结果 | 邮件线程（`In-Reply-To` / `References`） | IMAP 轮询 + SMTP；**发件人必须通过 `Authentication-Results` 校验**（authserv-id 等于 `EMAIL_AUTHSERV_ID`）；丢弃 `Auto-Submitted`、`Precedence: bulk/list/junk`、`X-Auto-Response-Suppress`、`List-Unsubscribe` 等自动邮件；回复时带上线程头；正文长度设上限（Hermes 用的是 50k） |

## A. 出站长连接：不需要公网入口

| # | 平台 | 接入方式 | SDK / 依赖（Hermes 参考） | 主要凭证 | 阶段 |
|---|---|---|---|---|---|
| 1 | Telegram | Bot API 长轮询（也可选 webhook） | `python-telegram-bot` 22.8 | `TELEGRAM_BOT_TOKEN` | **P0** |
| 2 | 飞书 / Lark | 开放平台 WebSocket 长连接（也可选 webhook） | `lark-oapi` | `FEISHU_APP_ID` `FEISHU_APP_SECRET` `FEISHU_DOMAIN` | **P0** |
| 3 | Discord | Gateway WebSocket | `discord.py` 2.7.1 | `DISCORD_BOT_TOKEN` | P1 |
| 4 | Slack | Socket Mode | `slack-bolt` 1.30.0 / `slack-sdk` 3.45.0 | `SLACK_BOT_TOKEN` `SLACK_APP_TOKEN` | P1 |
| 5 | 钉钉 | Stream 模式 | `dingtalk-stream` ≥0.20 | `DINGTALK_CLIENT_ID` `DINGTALK_CLIENT_SECRET` | P1 |
| 6 | 企业微信（智能机器人） | WebSocket | 无专用 SDK（自实现） | `WECOM_BOT_ID` `WECOM_SECRET` `WECOM_WEBSOCKET_URL` | P1 |
| 7 | QQ 机器人 | 官方 Bot API v2：WebSocket 收事件、REST 发消息 | 无专用 SDK（自实现） | `QQ_APP_ID` `QQ_CLIENT_SECRET` | P1 |
| 8 | 微信（个人号） | 腾讯 iLink Bot API：长轮询 `getupdates`；首次需扫码登录；媒体走 AES-128-ECB 加密的 CDN | 无专用 SDK（自实现） | `WEIXIN_ACCOUNT_ID` `WEIXIN_TOKEN` | **P0** |
| 9 | Mattermost | REST v4 + WebSocket 事件流 | 无专用 SDK | `MATTERMOST_URL` `MATTERMOST_TOKEN` | P1 |
| 10 | Matrix | homeserver sync，可选 E2EE | `mautrix[encryption]` 0.21.1（`python-olm` 只能在 Linux 上构建） | `MATRIX_HOMESERVER` `MATRIX_ACCESS_TOKEN` | P1 |
| 11 | IRC | TCP/TLS | 标准库 asyncio | `IRC_SERVER` `IRC_CHANNEL` `IRC_NICKNAME` | P1 |
| 12 | Email | IMAP 轮询 + SMTP 发信 | 标准库 | `EMAIL_ADDRESS` `EMAIL_PASSWORD` `EMAIL_IMAP_HOST` `EMAIL_SMTP_HOST` `EMAIL_AUTHSERV_ID` | **P0** |
| 13 | ntfy | HTTP 流式订阅 + POST 发布 | httpx | `NTFY_TOPIC` `NTFY_SERVER_URL` `NTFY_TOKEN` | P1 |
| 14 | Google Chat（Pub/Sub 拉取模式） | Cloud Pub/Sub pull 收事件、REST 发消息 | `google-cloud-pubsub` 2.43.0 | `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` `GOOGLE_CHAT_PROJECT_ID` `GOOGLE_CHAT_SUBSCRIPTION_NAME` | P1 |

备注：
- **ntfy** 本身没有用户身份的概念：一个 topic 就是一个受信通道，必须使用私有 topic 加读 token。
- **微信个人号**走的是 iLink 接口，账号风控规则以腾讯为准。
- **Google Chat** 也可以用 HTTP 回调模式，那种模式需要公网入口，归入 B 类。

## B. Webhook：需要公网 HTTPS 入口（经 bridge 的共享 ingress）

| # | 平台 | 接入方式 | SDK / 依赖（Hermes 参考） | 主要凭证 | 阶段 |
|---|---|---|---|---|---|
| 15 | LINE | Messaging API webhook（HMAC-SHA256 验签） | aiohttp | `LINE_CHANNEL_ACCESS_TOKEN` `LINE_CHANNEL_SECRET` `LINE_PUBLIC_URL` | P2 |
| 16 | Microsoft Teams | Bot Framework | `microsoft-teams-apps` 2.1.1 | `TEAMS_CLIENT_ID` `TEAMS_CLIENT_SECRET` `TEAMS_TENANT_ID` | P2 |
| 17 | WhatsApp Business（Cloud API） | Graph API 发消息；webhook 收消息（X-Hub-Signature-256 验签） | aiohttp | `WHATSAPP_CLOUD_PHONE_NUMBER_ID` `WHATSAPP_CLOUD_ACCESS_TOKEN` `WHATSAPP_CLOUD_APP_SECRET` `WHATSAPP_CLOUD_VERIFY_TOKEN` | P2 |
| 18 | SMS（Twilio） | Twilio REST 发送；入站 webhook | aiohttp | `TWILIO_ACCOUNT_SID` `TWILIO_AUTH_TOKEN` `TWILIO_PHONE_NUMBER` | P2 |
| 19 | 企业微信（自建应用回调） | HTTP 回调，AES 加解密 | `defusedxml`（解析不可信 XML，防 XXE） | `WECOM_CALLBACK_CORP_ID` `WECOM_CALLBACK_CORP_SECRET` `WECOM_CALLBACK_AGENT_ID` `WECOM_CALLBACK_TOKEN` `WECOM_CALLBACK_ENCODING_AES_KEY` | P2 |
| 14' | Google Chat（HTTP 回调模式） | 带鉴权的 HTTP 回调 | — | `GOOGLE_CHAT_HTTP_EVENTS_URL` `GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE` | P2 |

备注：
- **LINE**：回复优先使用免费的 reply token；token 过期后会退回到计费的 Push API。Codex 回答慢时需要应对 token 过期，Hermes 的做法是先回一个"点击获取结果"的按钮。
- **WhatsApp Cloud**：有 24 小时会话窗口的限制。
- **SMS**：只发纯文本。

## C. Sidecar / 外部守护进程

| # | 平台 | 接入方式 | 额外组件 | 主要凭证 | 阶段 |
|---|---|---|---|---|---|
| 20 | Signal | signal-cli 守护进程的 HTTP 模式（SSE 收、JSON-RPC 2.0 发） | `signal-cli`（Java），独立的 compose 服务 | `SIGNAL_HTTP_URL` `SIGNAL_ACCOUNT` | P3 |
| 21 | SimpleX Chat | simplex-chat 守护进程的 WebSocket | `simplex-chat`，独立的 compose 服务 | `SIMPLEX_WS_URL` | P3 |
| 22 | WhatsApp（个人号） | Node.js 的 WhatsApp Web 桥接，HTTP API | `@whiskeysockets/baileys` 7.0.0-rc13（Node sidecar） | `WHATSAPP_ENABLED`（需扫码配对） | P3 |
| 23 | iMessage（Photon） | Photon Spectrum 托管服务；Node sidecar 维持 gRPC 长流，不需要公网 URL | `spectrum-ts` 12.10.1（Node sidecar） | `PHOTON_PROJECT_ID` `PHOTON_PROJECT_SECRET` | P3 |
| 24 | iMessage（BlueBubbles） | BlueBubbles macOS 服务端：REST 发、webhook 收 | **需要一台 Mac** 运行 BlueBubbles；Mac 到 bridge 的 webhook 走内网即可 | `BLUEBUBBLES_SERVER_URL` `BLUEBUBBLES_PASSWORD` | P3 |
| 25 | Buzz（Nostr） | 通过 `buzz` CLI（JSON 输入输出）连接 relay | `buzz` 二进制，打进 bridge 镜像 | `BUZZ_RELAY_URL` `BUZZ_PRIVATE_KEY` | P3 |
| 26 | Raft | Raft CLI 加本地 wake-channel（回环 HTTP） | `raft` CLI | `RAFT_PROFILE` | P3（需要重新设计，见下） |
| 27 | A2A（Agent-to-Agent v1.0） | 入站 HTTP：在 `/.well-known/agent-card.json` 提供 Agent Card，以 JSON-RPC 接收 task | 标准库 | `A2A_BEARER_TOKEN` `A2A_HOST` `A2A_PORT` | P3 |

备注：
- **WhatsApp 个人号**走的是非官方的 WhatsApp Web 协议，有封号风险。
- **Raft**：在 Hermes 里是 agent 自己调用 Raft CLI 收发消息，适配器只负责唤醒。这和本项目"bridge 负责收发、Codex 不感知 IM"（D3）的原则冲突，需要改成由 bridge 调用 CLI 收发。
- **A2A**：只实现入站部分，也就是把 codex-IM 暴露为一个 A2A agent。Hermes 的出站 A2A 工具（`a2a_call` 等）属于 harness 工具，不在本项目范围内。没有配置 bearer token 时只绑定 localhost，沿用 Hermes 的默认值。

## 不纳入的 Hermes 内置入口

以下入口不在 `plugins/platforms` 中，也不是 IM，首期不做：

| 入口 | 说明 |
|---|---|
| `api_server` | OpenAI 兼容的 HTTP API |
| `webhook` | 通用 webhook 触发 |
| `msgraph_webhook` | Microsoft Graph 订阅回调 |
| `relay` | Nous 托管 connector 前置的中继，依赖 Nous 服务，实验性 |
| `local` | Hermes 本地 CLI/TUI 会话 |

## 适配器验收清单（每个平台都要满足）

- [ ] 白名单默认拒绝；群聊必须 @ 才响应。
- [ ] 文本收发、按长度分段、Markdown 方言转换。
- [ ] 支持编辑的平台做节流流式，最终结果用编辑落定，不重复发送。
- [ ] `/stop`、`/status` 在 turn 运行中可用。
- [ ] 断线自动重连，带退避；凭证错误时直接失败，不无限重试。
- [ ] 同一凭证只允许一个运行实例（锁）。
- [ ] INFO 及以上级别的日志里没有用户内容。
- [ ] 图片入站（如果平台支持）以原生图片输入交给 Codex。
- [ ] 有 mock 了平台 API 的单元测试，外加一次真实平台的手工冒烟测试（记录在 PR 里）。
