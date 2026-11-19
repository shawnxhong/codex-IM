# codex-IM 设计文档

> 状态：设计阶段（尚无实现代码）。
> 调研基线：`openai/codex` @ `c0cc9fc`（2026-11-12，最新稳定 tag `rust-v0.167.2`）、
> `NousResearch/hermes-agent` @ `3371c03`（2026-11-18）。

## 1. 背景与目标

- **Hermes Agent**：IM 接入做得好（27 个平台），但我们不想用它的 agent harness。
- **Codex**：harness（工具、沙箱、上下文压缩、计划）足够好，但没有 IM 接入。

codex-IM 只做一件事：**把 IM 消息接到 Codex 上，自身只是一层"传输"。**

### 目标

1. 支持 Hermes 已实现的全部 IM 平台（清单见 [platforms.md](platforms.md)）。
2. 模型支持两种模式：**ChatGPT 账号登录**、**自定义 API key**（见 [auth.md](auth.md)）。
3. **Codex 能跟着上游版本升级**，桥接层不需要改动或只需极少改动。
4. 在 Ubuntu 上用 Docker 部署，与宿主机上已有的 codex / hermes **完全隔离**（见 [deployment.md](deployment.md)）。

### 非目标

- 不复刻 Hermes 的记忆、技能、人格、子代理、kanban、cron 等 harness 能力。
- 不往 Codex 注入自定义的 system prompt 或工具体系（只有一个可选的最小 IM 提示，见 D3）。
- 首期不做 IM 内的人工审批交互（见 D4）。

## 2. 关键设计决策

### D1. 不复用 Hermes 代码，自研薄桥接；Hermes 只作参考实现

- Hermes 的平台适配器和它的网关深度耦合：
  - `gateway/platforms/base.py` 有 4653 行，依赖 `gateway.session*`、`agent.*`、`hermes_cli.*`、profile/secret scope 等内部模块；
  - 单个适配器也很重：Telegram 约 15k 行，Discord 约 9.6k 行，Slack 约 8.5k 行，飞书约 6k 行。
- 抽取这些代码等于 fork，从此无法跟随 Hermes 上游更新，失去复用的意义。
- 以库的形式依赖 Hermes、只借用适配器也不可行：`set_message_handler` 这个接缝存在，但 `base.py` 会通过 `handler.__self__` 反向访问 runner 内部；而且 Hermes 的内部 API 没有稳定性承诺，主干每天在变。
- Hermes 采用 MIT 许可。可以把它当"踩坑手册"阅读（消息格式、分段、限流、重连、各平台的怪癖）。如果借鉴代码片段，必须保留其版权声明。

### D2. 只通过官方 Python SDK `openai-codex` 访问 Codex

- SDK 底层通过 stdio JSON-RPC 驱动 `codex app-server`。app-server 也是 Codex 官方 IDE 扩展、桌面端和 SDK 共用的集成面。
- **版本锁定**：每个 Codex 稳定版都会同步发布同版本号的 SDK，并精确锁定对应的内核二进制 `openai-codex-cli-bin`（见 `codex/sdk/python/docs/faq.md`）。升级时只改一个版本号，不会出现协议漂移。
- 不手写 JSON-RPC 客户端，也不用 `codex exec`。Hermes 是手写客户端，只能靠人工跟踪协议变化（它的源码注释里写着"verified against codex 0.147"）。
- 一个进程只开一个 `AsyncCodex` 实例。它支持同时推进多个 turn（按 turn ID 路由），一个 IM 会话对应一个 Codex thread。
- 要用到的 SDK 能力：
  - `thread_start` / `thread_resume`：新建会话 / 重启后续接；
  - `turn()` 配合 `stream()`、`steer()`、`interrupt()`：流式输出、运行中追加输入、`/stop`；
  - 图片输入（data URL 或本地路径）；
  - `ExternalMessage`：标记不可信的外部内容。

### D3. 不注入 harness，Codex 保持原样

- 默认**不传** `base_instructions`，不改 `personality`，不注册额外的 MCP 工具。上下文压缩、工具、计划全部由 Codex 自己处理。
- 唯一可选的注入是 `CODEX_IM_DEVELOPER_INSTRUCTIONS`（默认为空）：用来告诉模型"你正在通过 IM 被访问，输出会被渲染为某种格式"之类的最小信息。
- 对比 Hermes 的 codex runtime 模式：它会把 Hermes 完整的系统提示词作为 `developerInstructions` 注入，强制 `personality: "none"`，在 `~/.codex/config.toml` 里写入受管配置块，并在后台额外调用模型做记忆/技能复盘。这些正是本项目要避开的。

### D4. Docker 隔离，容器本身就是沙箱；IM 内不做人工审批

- Codex 在 Linux 上的沙箱依赖 bubblewrap 创建 user namespace（`codex-rs/linux-sandbox/README.md`）。Docker 默认的 seccomp 规则通常不允许这样做；Codex 在这种情况下会在启动时报警告，沙箱化的命令也无法执行。
- 放宽 seccomp 或给容器加 `SYS_ADMIN` 都会削弱容器隔离，不采用。
- **决策**：agent 容器内 Codex 使用 `sandbox = full_access`，并显式设置不发起审批（SDK 中 `approval_mode=ApprovalMode.deny_all`，对应 `approval_policy = "never"`）。隔离边界由容器负责：
  - 用非 root 用户运行；
  - 只挂载工作区；
  - 出网走白名单（D9）；
  - 容器里不放任何宿主机凭证；
  - 设置资源限制。
- 显式设置审批模式，不依赖 SDK 默认值。SDK 默认是 `auto_review`；底层 `CodexClient` 的默认 approval handler 会**直接 accept** 所有审批请求。
- 这样一来，"在 IM 里点按钮审批"这个最复杂的部分不需要做。附带的一个坑：SDK 的 approval handler 是在读取线程里同步执行的，一旦阻塞等待 IM 回复，会卡住所有事件。将来如果确实需要人工审批，单独立项（roadmap P4）。

### D5. 双容器：IM 凭证与 agent 分离

- agent 拥有 full_access，一旦被提示词注入，就能读取同一容器、同一 UID 下其他进程的环境变量和文件，包括 IM bot token。bot token 泄露意味着别人可以冒充机器人、读取所有会话。
- **决策**：拆成两个容器：
  - `bridge`：IM SDK 和平台凭证、会话库。不挂载工作区，不运行模型产生的代码。
  - `agent`：`codex app-server`、工作区、模型凭证。
- 两者之间是 **stdio-over-TCP 中继**：
  - agent 端执行 `socat TCP-LISTEN:4500,reuseaddr EXEC:"codex app-server --listen stdio://"`；
  - bridge 端用 SDK 的 `CodexConfig(launch_args_override=("socat", "-", "TCP:agent:4500"))`。
  - SDK 启动的"子进程"就是这条中继，协议是原样透传的 newline JSON-RPC。
- 两个镜像共用同一个 `CODEX_VERSION` 构建参数：bridge 装 `openai-codex==X`，agent 用同一个包自带的内核二进制，保证版本一致。
- 模型凭证必须放在 agent 一侧，这是 Codex 本身的需要，无法避免。缓解手段是出网白名单，以及使用有额度上限的 key。
- 中继只在内部网络上，不对宿主机或公网暴露端口。
- **P0 验证项**：中继在 SDK 下行为正常（初始化、并发 turn、断线重连）。如果不可行，退回方案是单容器 + bridge 和 codex 用不同 UID 运行。

### D6. 会话模型

- **会话键**：`(platform, chat_id, thread_or_topic_id)`。
- **群聊整个群共用一个会话**（已确定，不提供按用户拆分）。群里任何一个白名单用户都可以发消息、`/new`、`/stop`。为了让 Codex 分得清是谁在说话，群聊里每条输入前加上发送者标识（如 `[昵称]: …`）；私聊不加。
- **Email** 没有"群"的概念：一个邮件线程（按 `In-Reply-To`/`References` 串起来）对应一个会话，一封新邮件就是一个新会话。
- **持久化**：`会话键 → codex thread_id`，存在 bridge 的 SQLite 里。thread_id 在 turn 完成后才写入。
- **重启续接**：用 `thread_resume`。续接失败时新开 thread，并只在该会话里提示一次（沿用 Hermes 的做法）。**不从本地记录重建上下文**：Codex 的 thread 才是真正的上下文，本地只存映射和少量元数据。
- **工作目录**：默认每个会话一个独立目录 `/workspace/<platform>/<chat>`；可以在配置里把某个会话绑定到某个项目目录。
- **并发**：每个会话同时最多一个活动 turn。turn 进行中收到新消息时，默认用 `steer()` 追加到当前 turn（可配置为排队）；不同会话之间并行。
- **provider 和 model**：provider 在 thread 创建时就固定了，`/provider` 切换会新开一个 thread；model 可以每个 turn 单独指定，`/model` 从下一个 turn 开始生效。

### D7. 桥接命令（由 bridge 处理，不发给 Codex）

| 命令 | 作用 | 运行中可用 |
|---|---|---|
| `/new` | 开新 thread | ✓（会先中断当前 turn） |
| `/stop` | `interrupt()` 当前 turn | ✓ |
| `/status` | 当前 provider、model、cwd、thread、是否在运行 | ✓ |
| `/model <slug>` | 切换 model（下一个 turn 生效） | ✓ |
| `/provider <id>` | 切换 provider（新开 thread） | |
| `/compact` | 让 Codex 压缩当前 thread | |
| `/help` | 命令列表 | ✓ |

标"运行中可用"的命令**必须绕过会话排队，直接执行**。这一点 Hermes 踩过坑：审批或停止命令被排在正在运行的 turn 后面，结果失效。

### D8. 输出投递

- **流式**：在支持编辑消息的平台上，持续编辑同一条消息，节流为每 1–2 秒一次，避开平台限流。不支持编辑的平台只发最终结果。
- 沿用 Hermes 总结的规则：
  - 流式过程中的草稿必须"前缀稳定"：后一帧以前一帧为前缀，不在中途改写已发出的内容；
  - 最终结果**通过编辑原消息落定**，不另发一条，否则会出现重复的最终消息。
- **长输出**：按平台的单条长度上限分段发送。**不往 IM 发送文件附件**：Codex 生成的文件留在工作区，不回传（已确定）。
- **工具进度**：可选择显示一行精简状态（例如"运行中：pytest …"），详细程度可配置，默认精简。
- Markdown 按平台转换（Slack mrkdwn、Telegram MarkdownV2/HTML、飞书富文本或卡片、纯文本平台去掉格式）。

### D9. 安全模型

- **准入**：每个平台一个白名单 `<PLATFORM>_ALLOWED_USERS`，**默认拒绝**。`<PLATFORM>_ALLOW_ALL_USERS` 只用于开发。群聊里必须 @ 机器人才会响应。
- **Email 的发件人可以伪造**：白名单不能只看 `From`，必须校验收件服务器加上的 `Authentication-Results` 头（DMARC/DKIM/SPF 为 pass，且 authserv-id 等于 `EMAIL_AUTHSERV_ID`），校验不过一律丢弃（沿用 Hermes 的做法）。自动邮件（`Auto-Submitted`、`Precedence: bulk/list/junk`、`List-Unsubscribe` 等）也要丢弃，防止和自动回复互相触发形成循环。
- **非白名单内容**：默认丢弃；可选把群聊上下文以 `ExternalMessage` 的形式提供给模型（低权限，不代表用户授权）。
- **出网白名单**：
  - agent 容器只接入 `internal` 网络，所有出网都经过一个域名白名单代理；
  - 白名单包括模型 API、ChatGPT 认证域名，以及按需开放的 pypi、github 等；
  - 因为没有直连出口，agent 里的 shell 命令就算忽略代理环境变量，也连不上外网；
  - bridge 容器不运行不可信代码，正常出网，所以各平台 SDK 不需要支持代理。
- **凭证**：只通过环境变量或 Docker secrets 提供；不写进 `config.toml`（自定义 provider 用 `env_key` 引用）；`.env` 文件权限设为 600。
- **日志**：INFO 及以上级别不记录消息正文、用户名、手机号；只记长度、哈希过的 ID（沿用 Hermes 的规则 HX017）。
- **凭证独占**：codex-IM 必须使用**独立的 bot 应用和凭证**，不能和宿主机上的 Hermes 共用。同一个 Telegram token 被两个进程轮询会冲突；Slack Socket Mode 或飞书长连接被两个连接共用时，事件会被分摊到两边。
- **限流**：按用户或会话限制 turn 频率，防止被刷爆额度。

### D10. 平台接入分为三类（详见 platforms.md）

1. **出站长连接**（WebSocket、长轮询、Stream）：不需要公网 IP，也不需要映射端口。优先实现。
2. **Webhook**：需要公网 HTTPS 入口。bridge 内置一个共享的 HTTP ingress（单端口，每个平台一个路径），由宿主机上的反向代理（如 Caddy）或隧道转发进来。只有这一个入口对外。
3. **Sidecar**：需要额外的守护进程（signal-cli、simplex-chat、WhatsApp Baileys 桥、Photon 的 Node sidecar 等），各自作为 compose 中的独立服务，只和 bridge 在同一网络。

### D11. 配置约定

- 平台凭证的**环境变量名和 Hermes 保持一致**（如 `TELEGRAM_BOT_TOKEN`、`FEISHU_APP_ID`、`<PLATFORM>_ALLOWED_USERS`），方便参照 Hermes 文档，也方便迁移已有配置。
- 桥接层自身的配置统一用 `CODEX_IM_` 前缀。
- Codex 的配置（`config.toml`）放在 agent 容器的 `CODEX_HOME` 里，由 entrypoint 根据环境变量生成。这个 `CODEX_HOME` 是独立的 volume，**绝不挂载宿主机的 `~/.codex`**。

### D12. 升级策略

- 唯一的版本旋钮是构建参数 `CODEX_VERSION`，桥接层只依赖 SDK 的公开 API。
- 升级流程：
  1. 备份 `codex-home` volume；
  2. 用新的 `CODEX_VERSION` 重建两个镜像；
  3. 跑冒烟测试（启动、新建 thread、执行一个只读 turn、resume 一个已有 thread）；
  4. 滚动重启。
- 回滚：切回上一个镜像 tag。新版 Codex 可能会迁移状态库，跨版本回滚不一定安全，所以第 1 步的备份是必需的。
- 平台 SDK 版本单独锁定，单独升级。

## 3. 架构

```
                         Ubuntu 宿主机（Docker Engine + compose）
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │  [反向代理/隧道] ──443──► ┌───────────────────────── bridge ─────────────────┐ │
 │   (仅 webhook 平台需要)   │ ingress(HTTP) ─┐                                 │ │
 │                          │ 长连接适配器 ───┼─► Router ─► 准入/命令 ─► Session │ │
 │  IM 平台 ◄──出站连接──────┤ sidecar 客户端 ─┘    ▲         Manager(SQLite)  │ │
 │                          │                     └── Renderer ◄──┐    │       │ │
 │                          │                                     │    ▼       │ │
 │                          │                   AsyncCodex（openai-codex SDK） │ │
 │                          └───────────────────────────────┬────────────────┘ │
 │   sidecars (signal-cli /                                 │ stdio-over-TCP    │
 │   simplex / wa-bridge…) ◄── im 网络                       │ (internal 网络)   │
 │                          ┌───────────────── agent ───────▼────────────────┐ │
 │                          │ socat ─► codex app-server (full_access, 非root) │ │
 │                          │ CODEX_HOME(volume)   /workspace(volume)        │ │
 │                          └────────────────────────┬───────────────────────┘ │
 │                                                   │ HTTPS_PROXY             │
 │                          ┌──────── egress-proxy (域名白名单) ──────────────┐ │
 │                          └────────────────────────┬───────────────────────┘ │
 └───────────────────────────────────────────────────┼─────────────────────────┘
                                                     ▼
                                    模型 API / ChatGPT 认证 / 白名单域名
```

### 组件职责

| 组件 | 职责 |
|---|---|
| `platforms/<name>` | 只做协议转换：平台事件 → `InboundMessage`；`send`/`edit`/`typing`；声明自身能力 |
| `core/router` | 计算会话键、准入检查、群聊 @ 判定、命令分流 |
| `core/sessions` | 会话 ↔ thread 映射、每会话的锁和队列、steer/interrupt、持久化 |
| `codex/runtime` | 封装 `AsyncCodex`：thread 生命周期、turn 流、provider/model 参数、错误分类 |
| `core/render` | 流式节流、前缀稳定草稿、分段、Markdown 方言转换 |
| `ingress` | webhook 平台共用的 HTTP 服务（验签由各平台实现） |

### 适配器接口（草案）

```python
@dataclass(frozen=True)
class Capabilities:
    can_edit: bool
    max_text_len: int
    markup: Literal["markdown", "slack_mrkdwn", "telegram_html", "feishu_post", "plain", ...]
    supports_threads: bool
    supports_images_in: bool

@dataclass(frozen=True)
class InboundMessage:
    platform: str
    chat_id: str
    thread_id: str | None
    user_id: str
    message_id: str
    text: str
    attachments: list[Attachment]
    is_group: bool
    mentions_bot: bool

class PlatformAdapter(Protocol):
    name: str
    capabilities: Capabilities
    async def start(self, on_message: Callable[[InboundMessage], Awaitable[None]]) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, chat: ChatRef, text: str, *, reply_to: str | None = None) -> MessageRef: ...
    async def edit(self, ref: MessageRef, text: str) -> None: ...          # 仅 can_edit
    async def typing(self, chat: ChatRef) -> None: ...
```

### 一轮消息的数据流

1. 适配器收到平台事件，转换成 `InboundMessage`，交给 Router。
2. Router 计算会话键，做准入检查和群聊 @ 判定；如果是命令，交给命令处理器（运行中可用的命令直接执行）。
3. SessionManager 取出或创建 thread：没有映射就 `thread_start`，有映射就 `thread_resume`。
4. 如果会话里有活动 turn，就 `steer()` 追加；否则 `thread.turn(input)` 开一个新 turn。
5. Renderer 消费 `stream()` 事件：先发一条占位消息，之后节流编辑，turn 结束时用最终文本编辑落定。
6. turn 完成后持久化 thread_id 和元数据。

## 4. 从 Hermes 借鉴的经验（不借代码，只借教训）

1. Codex thread 才是真正的上下文。不要用本地记录"重建"上下文，也不要为了压缩而丢掉活跃的 thread。
2. thread_id 要在 turn 写入完成后再持久化；resume 失败时安全地新开 thread，并提示一次。
3. provider 在 thread 上固定，model 可以每个 turn 指定。
4. 图片以原生的 turn 输入传递，不要退化成一段文本标记。
5. 流式草稿保持前缀稳定，最终结果用编辑落定，避免重复发送。
6. 停止类命令必须绕过会话排队。
7. 同一份凭证只允许一个运行实例（Hermes 用 scoped lock 保证）。
8. 日志里不出现用户内容。
9. 两个实例不要共用同一份 Codex `auth.json`：refresh token 会轮换，两边互相覆盖后会掉线。

## 5. 已确定事项（2026-11-19）

- **P0 平台**：飞书、Telegram、微信（个人号）、Email。
- **群聊**：整个群共用一个会话。
- **出站文件**：不往 IM 回传 Codex 生成的文件，也不做 `send_file` 工具。
- **代码托管**：GitHub `shawnxhong/codex-IM`。

## 6. 待决问题

- 语音消息（STT 和 TTS）是否需要。
- 是否提供 `api_server` 之类的非 IM 入口（Hermes 内置，但不在 `plugins/platforms` 中）。
- 许可证。
