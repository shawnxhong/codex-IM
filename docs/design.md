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
- 首期不做 IM 内的人工审批交互（见 D4）；阻塞式澄清提问在上游 SDK 支持之前不做（见 D13）。

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
- agent 容器里还运行一个小服务 **`agentd`**（本项目代码，以 agent 用户身份运行，监听 internal 网络的 4501 端口），专门做工作区操作：创建会话目录、创建或清理 git worktree。原因有两个：
  - bridge 不挂载工作区；
  - 不能让 bridge 在 agent 可控的仓库里执行 git：仓库里的 hooks、`core.fsmonitor` 之类的配置可以执行任意命令，一旦在持有 IM 凭证的 bridge 里执行就会泄露凭证。

  `agentd` 和 Codex 的权限相同，不构成新的提权面。
- **P0 验证项**：中继在 SDK 下行为正常（初始化、并发 turn、断线重连）。如果不可行，退回方案是单容器 + bridge 和 codex 用不同 UID 运行。

### D6. 会话模型

- **会话键**：`(platform, chat_id, thread_or_topic_id)`。
- **群聊整个群共用一个会话**（已确定，不提供按用户拆分）。群里任何一个白名单用户都可以发消息、`/new`、`/stop`。为了让 Codex 分得清是谁在说话，群聊里每条输入前加上发送者标识（如 `[昵称]: …`）；私聊不加。
- **Email** 没有"群"的概念：一个邮件线程（按 `In-Reply-To`/`References` 串起来）对应一个会话，一封新邮件就是一个新会话。
- **会话和 thread 的关系**：一个会话同一时刻只有一个"当前 thread"。`/new` 之后，旧 thread 进入这个会话的历史列表，可以用 `/resume` 找回（D7）。
- **持久化与恢复**：thread 一创建就记录，不等 turn 完成。续接失败要区分"临时故障"和"thread 确实不存在"，详见 D14。**不从本地记录重建上下文**：Codex 的 thread 才是真正的上下文，本地只存映射、状态和投递记录。
- **工作目录**：
  - 默认目录包含**完整的会话标识**：`/workspace/sessions/<platform>/<chat>/<topic 或 _>`。每一段都做安全转义，过长时改用哈希。所以同一个 Telegram 群的两个 topic 是两个会话，各自有独立的目录。
  - cwd 记在 thread 上：`/resume` 回到旧 thread 时，使用它当时的 cwd。
  - **绑定项目**：配置 `projects.<name>.path`，并配置"会话 → 项目"的绑定关系。多个会话绑定同一个项目时，按项目的 `concurrency` 设置处理：
    - `serial`（默认）：所有绑定的会话共用项目路径。同一时刻这个项目里只运行一个 turn，其他会话的 turn 排队，并收到提示"工作目录正被另一个任务使用，已排队"。
    - `worktree`：每个会话一个独立的 git worktree，位于 `/workspace/worktrees/<project>/<session>`，分支名为 `codex-im/<session 短 ID>`。这种模式下可以并行，但只适用于 git 仓库；是否合并回主分支由用户决定。
  - 目录和 worktree 都由 `agentd` 创建（D5）。
- **工作目录占用**（评审 R3）：互斥的单位是 **cwd**，不是会话或项目。turn 在 `turn/start` 之前占用自己的 cwd，**直到它的远端结果被确认为终态才释放**：
  - 释放只发生在 Finalizer 里，也就是 turn 变成 completed / interrupted / failed / unknown / released 的那一刻；
  - "本地不再消费"（停止超时、断线、bridge 重启）不等于"远端已经结束"，这些情况下占用会一直保持，直到对账确认远端结束；
  - 一个 cwd 同时覆盖三种情况：`serial` 项目跨会话串行；同一会话在旧 turn 未确认结束时不能开新 turn（包括 `/new` 之后的新 thread，因为它们共用会话目录）；`worktree` 项目每个会话的路径不同，可以并行；
  - 重启时，先从数据库恢复所有未确认 turn 的占用，再派发排队的消息；
  - 逃生口：确认 Codex 端确实不再运行之后，管理员可以用 `/release` 强制把这些 turn 标记为 released 并释放占用（D7）。
- **并发**：每个会话同时最多一个活动 turn。turn 进行中收到的新消息默认用 `steer()` 追加到当前 turn（可配置为排队），追加的规则见 D14。不同会话之间并行，但受 cwd 占用的约束。状态机和命令竞态见 D15。
- **provider 和 model**：provider 在 thread 创建时就固定了，`/provider` 切换会新开一个 thread；model 可以每个 turn 单独指定，`/model` 从下一个 turn 开始生效。

### D7. 桥接命令（由 bridge 处理，不发给 Codex）

| 命令 | 作用 | 运行中可用 |
|---|---|---|
| `/new` | 开新 thread；旧 thread 进入历史 | ✓（先中断当前 turn，见 D15） |
| `/stop` | `interrupt()` 当前 turn | ✓ |
| `/status` | 当前 provider、model、cwd、thread、运行状态、排队情况 | ✓ |
| `/sessions` | 列出**本会话**的历史 thread（序号、标题、最后活动时间、turn 数） | ✓ |
| `/resume <序号或短 ID>` | 把历史 thread 切换为当前 thread | （需要先空闲） |
| `/retry` | 重新执行上一个被中断或状态未知的 turn（**只有用户明确要求时才会重新执行**，见 D14） | |
| `/model <slug>` | 切换 model（下一个 turn 生效） | ✓ |
| `/provider <id>` | 切换 provider（新开 thread） | |
| `/compact` | 让 Codex 压缩当前 thread | |
| `/release` | （管理员）把本会话中远端结果无法确认的 turn 标记为 released，释放 cwd 占用（D6） | （需要先空闲） |
| `/help` | 命令列表 | ✓ |

- **命令分两类**（评审 R2、R4）：
  - **只读命令**（`/status`、`/sessions`、`/help`）收到后立即执行，不进入命令队列，所以不会被一个正在等待的 `/stop` 卡住。
  - **改变执行状态的命令**（`/stop`、`/new`、`/resume`、`/provider`、`/retry`、`/compact`、`/model`、`/release`）在**收到时**就竖起该会话的"控制屏障"，然后按到达顺序在命令队列里逐个执行。屏障存在期间，worker 不开始新 turn，也不做 steer；最后一个控制命令执行完才放下屏障。因此排在它们之后的普通消息不会落到旧 thread 上。
- 这一点 Hermes 也踩过坑：停止命令被排在正在运行的 turn 后面，结果失效。
- **群聊里命令也必须明确指向本机器人**（评审 R12）：@ 机器人、回复机器人的消息，或者写成 `/命令@机器人名`。裸的 `/stop` 在群里会被忽略。
- **`/sessions` 和 `/resume` 的可见范围**：只能看到和恢复**当前会话键**下记录过的 thread，数据来自 bridge 自己的库。不使用 Codex 的 `thread_list`，因为它会返回所有会话的 thread。私聊只能看到自己的，群聊里是整个群共享的历史，和 D6 一致。给的 ID 不属于当前会话时，统一回复"不存在"，不泄露这个 ID 是否在别处存在。

### D8. 输出投递

- **流式**：在支持编辑消息的平台上，持续编辑同一条消息，节流为每 1–2 秒一次，避开平台限流。不支持编辑的平台只发最终结果。
- 沿用 Hermes 总结的规则：
  - 流式过程中的草稿必须"前缀稳定"：后一帧以前一帧为前缀，不在中途改写已发出的内容；
  - 最终结果**通过编辑原消息落定**，不另发一条，否则会出现重复的最终消息。
- **长输出**：按平台的单条长度上限分段发送。**不往 IM 发送文件附件**：Codex 生成的文件留在工作区，不回传（已确定）。
- **事件逐条投递，不能只看 `final_response`**：SDK 的 `final_response` 只取最后一条 final_answer。凡是 `delivery == async` 的 agentMessage（例如 D13 里的非阻塞澄清提问），收到 `item/completed` 后要**立即作为一条单独的消息发出去**，否则会被后面的回答覆盖掉。
- **工具进度**：可选择显示一行精简状态（例如"运行中：pytest …"），详细程度可配置，默认精简。
- Markdown 按平台转换（Slack mrkdwn、Telegram MarkdownV2/HTML、飞书富文本或卡片、纯文本平台去掉格式）。

### D9. 安全模型

- **准入**：每个平台一个白名单 `<PLATFORM>_ALLOWED_USERS`，**默认拒绝**。`<PLATFORM>_ALLOW_ALL_USERS` 只用于开发。群聊里必须 @ 机器人才会响应，命令也一样（D7）。
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

### D13. 澄清提问与权限审批分开设计

"不做人工审批"（D4）只解决了权限审批的问题，**不覆盖澄清提问**。两者在协议里是不同的通路，需要分开设计。

**Codex 可能向客户端发起的服务端请求**，以及在本项目配置下的处理结果（来源：`app-server-protocol/src/protocol/common.rs`，SDK 版本对应 `rust-v0.167.2`）：

| 服务端请求 | 什么时候出现 | SDK 公开 API 下的结果 | 本项目的处理 |
|---|---|---|---|
| `item/commandExecution/requestApproval`、`item/fileChange/requestApproval` | 沙箱或审批策略要求审批时 | SDK 默认处理器直接 `accept` | 在 `full_access` 加 `approval_policy=never` 下不应出现；即使出现，结果也和我们的策略一致 |
| `item/permissions/requestApproval` | 需要额外权限时 | 返回 `{}` | 不应出现（`full_access`） |
| `item/tool/requestUserInput`（**阻塞式澄清**，实验性） | 只在 **Plan 协作模式**下，或者开启还在开发中的 feature `default_mode_request_user_input` 之后 | 返回 `{}`，app-server 把它当成**空答案**交给模型（`bespoke_event_handling.rs`），**用户完全收不到这个问题** | **不支持**。固定使用 Default 协作模式，并在配置里显式写 `features.default_mode_request_user_input = false`。P0 不提供进入 Plan 模式的命令 |
| `mcpServer/elicitation/request` | 某个 MCP server 或连接器向用户要信息时 | 返回 `{}`，app-server 按 **Decline** 处理 | 本项目不注册 MCP server。ChatGPT 账号自带的连接器可能触发，此时一律拒绝，列为已知限制 |
| `item/tool/call`（dynamic tool） | 客户端注册了动态工具时 | — | 本项目不注册，不会出现 |
| `account/chatgptAuthTokens/refresh` | 认证由外部管理时 | — | 认证由 Codex 自己管理，不会出现 |

**能够支持的澄清通路：非阻塞提问加普通消息往返**

- **提问**：如果模型的目录里声明了 `request_user_input_async`，Codex 会发出一条 `delivery=async` 的 agentMessage。这条消息的文本里包含问题和选项，结构化字段 `questions` 里也有同样的内容。按 D8，bridge 收到后立即单独发出，并记录一条 `open_question`（thread、turn、item）。如果模型没有这个工具，它会直接在回复里用文字提问，效果相同。
- **回答**：用户回复的就是一条普通消息。如果 turn 还在运行，用 `steer()` 追加进去；如果 turn 已经结束，就作为下一个 turn 的输入。
- **超时**：这条通路是非阻塞的，Codex 不会停下来等，所以不存在超时。问题会一直挂着，直到用户回复，或者下一个 turn 开始。
- **取消**：用户发 `/stop`，或者干脆不回答。

**已知限制和上游依赖**

- 阻塞式的 `item/tool/requestUserInput` 依赖 SDK 提供公开的服务端请求回调。目前 `AsyncCodex(config)` 没有 handler 参数；底层 `CodexClient(approval_handler=...)` 没有从包里导出，而且这个回调是在读取线程里同步执行的。按 AGENTS.md 的规则，不使用这些未导出的接口。跟踪上游，等 SDK 提供公开回调后再立项（roadmap P4）。
- 用公开 API **检测不到**意外出现的阻塞请求：`serverRequest/resolved` 通知里只有 `threadId` 和 `requestId`，没有 method。所以只能靠配置保证它不出现，并且每次升级 Codex 都跑一遍回归测试（verification.md 场景 V1）。

### D14. 消息可靠性：去重、持久状态、投递与恢复

**原则**：

- 同一条入站消息**最多执行一次**；
- 执行结果**至少投递一次**；
- 回复失败时**重新投递结果，不重新执行任务**；
- 任何可能已经产生副作用的 turn，**都不会自动重跑**。

**持久化记录**（bridge 的 SQLite，WAL 模式；每一步的状态变化都在一个事务里完成）：

| 表 | 关键字段 | 作用 |
|---|---|---|
| `inbound` | `(platform, message_key)` 唯一、`session_key`、`state`（queued / dispatched / steering / steered / steer_unknown / done / ignored）、`turn_id` | 入站去重和排队。只有 `queued` 会被自动执行。`message_key` 取平台的消息 ID：Telegram 的 `update_id`、飞书的 `message_id`、微信的消息 ID、Email 的 `Message-ID` |
| `threads` | `thread_id`、`session_key`、`state`（created / materialized / lost）、`cwd`、`provider`、`title`、`turn_count` | `thread_start` 一返回就写入（state=created）；看到第一个 `turn/started` 后改为 materialized |
| `turns` | 本地 ID、`thread_id`、`codex_turn_id`（可空）、`cwd`、`state`、`final_text` | 调用 `turn()` **之前**写入 pending；拿到 turn handle 后记录 `codex_turn_id`；turn 完成时，在**同一个事务**里写入 `final_text` 和对应的 outbox 记录。**未确认**状态：pending / started / reconciling（结果待从历史对账）/ abandoned（停止超时，本地已放弃等待），这些 turn 占着 cwd。**终态**：completed / interrupted / failed / unknown / released / retried |
| `steers` | `turn_id`、`text`、`inbound_ids`、`state`（sending / unknown / accepted / rejected / absent / unconfirmed） | 追加操作的持久化意图（评审 R1），见下文"追加"一节 |
| `outbox` | `turn_id`、`kind`（final / async_question / notice）、`seq`、`text`、`state`（pending / sent / dead）、`attempts`、`platform_message_id`、`next_attempt_at` | 结果先持久化再投递；投递失败按退避策略重试。**所有种类**的文本入库前都按平台上限分段（评审 R10）；async 提问的 `seq` = 问题序号 × 1000 + 分段序号，实时路径和恢复路径生成相同的记录，靠唯一约束去重 |

**入站流程**：

- 适配器收到消息后，先做持久化插入：`INSERT OR IGNORE`，重复的消息直接确认并丢弃。
- 插入成功之后，才向平台确认（有确认机制的平台）：Telegram 推进 offset，IMAP 标记 `\Seen`，微信 iLink 推进游标，飞书长连接按 SDK 的确认机制。
- 处理器只做持久化，必须很快返回；真正的执行是异步的。

**投递流程**：

- 流式编辑是尽力而为的，不进 outbox。
- 最终结果和 async 提问走 outbox：
  - 有可编辑的流式消息时，优先编辑它来落定；
  - 编辑失败或没有这样的消息时，再发一条新消息。
- 遇到临时失败（网络错误、限流）时，按平台的 retry-after 退避后重试。
- 遇到永久失败（机器人被移出群、会话已不存在）时，标记为 dead 并记录日志，**不重新执行**。
- **顺序跨扫描保持**（评审 R5）：每个会话只有最早的一条 pending 记录可以投递。它在退避期间，同会话后面的记录一律等待；它变成 sent 或 dead 之后，下一条才能发。其他会话不受影响。

**追加（steer）**（评审 R1）：

1. 在会话短锁内把这批消息从 `queued` 改为 `steering`，并写入一条 `steers` 记录（state=sending）。**先持久化意图，再发 RPC**。
2. RPC 在锁外执行，有超时（`CODEX_IM_RPC_TIMEOUT`）。
3. 结果：
   - 成功：accepted，消息变为 `steered`；
   - 服务端返回 JSON-RPC 拒绝（例如 turn 已经结束）：这能证明输入没有被接收，rejected，消息回到 `queued`，作为下一个 turn 执行；
   - 超时、断线、或在 RPC 前后崩溃：结果未知，记为 unknown，消息变为 `steer_unknown`，**不会被自动执行**。
4. unknown 的追加在 turn 结束后对照 Codex 历史判定：历史里该 turn 有这条输入 → steered；没有 → absent（消息标为 ignored，并告诉用户"没有送达，需要的话请重新发送"）；历史一直读不到 → unconfirmed（同样不重放，提示用户自行判断）。

**重启恢复流程**（bridge 启动时，按这个顺序）：

1. **先恢复所有未确认 turn 的 cwd 占用**（D6），然后才派发任何排队消息（评审 R3）。
2. outbox 里还是 pending 的记录：重新投递。
3. 状态为 sending 的追加：改为 unknown，随所属 turn 一起对账。
4. 未确认的 turn：
   - 先 `thread_resume`，再用 `thread.read(include_turns=True)` 查这个 turn；abandoned 的 turn 每轮还会用 turn id 再发一次 interrupt。
   - 历史显示仍在运行（inProgress）：保持占用，按退避继续轮询（最长 60 秒一次，**没有截止时间**）。
   - 已完成：取出最终回答写入 outbox 并投递，不重新执行。
   - 失败：按失败投递。中断：用户主动停止的（abandoned）显示"已停止"；其他情况告诉用户"上次任务因为重启被中断，没有自动重跑；需要的话请发 `/retry`"。
   - 历史里的 **async 提问**会以和实时路径相同的身份补进 outbox，并恢复待回答状态（评审 R7）。
   - 没有 `codex_turn_id` 的记录（崩溃发生在 `turn/start` 请求发出之后、响应回来之前），只匹配同时满足以下条件的历史 turn（评审 R6）：首条输入和记录完全一致；`startedAt` 不早于记录创建时间（允许 5 秒时钟误差）；早于同一 thread 上下一条本地记录；没有被其他本地记录绑定。**恰好一个**候选才算匹配，匹配后立刻把 Codex turn id 写回记录，并修正 thread 的 materialized 状态（turn_count 只增加一次）。没有候选或有多个候选：标记为 unknown，提示用户可以 `/retry`。
5. inbound 里状态为 queued、还没有派发的消息：正常派发（它们还没有被执行过；如果 cwd 被占用，会排队等待）。

**续接失败要分类处理，不能一律新开 thread**：

| 失败类型 | 判断依据 | 处理 |
|---|---|---|
| 临时故障 | `TransportClosedError`、超时、`ServerBusyError` / `is_retryable_error()`、"thread 正在关闭"一类的错误 | 带退避重试，消息继续留在队列里；**不新开 thread**。超过阈值后告诉用户"Codex 暂时不可用，消息已保留" |
| thread 从来没有落盘 | 我们的记录是 `state=created` 且 `turn_count=0` | 安全：说明还没有执行过任何工作。静默新开 thread |
| thread 确实不存在 | `InvalidRequestError`，并且我们的记录显示它已经 materialized | **真正丢失了上下文**。标记为 lost，明确告诉用户"之前的上下文已经无法恢复"，由用户决定 `/new` 或 `/resume` 其他 thread，**不静默替换** |

具体的错误码和错误消息以 Ubuntu 上的实测为准，写进错误映射表和测试（verification.md 场景 V3）。

**上游依赖**：协议的 `turn/start` 支持 `clientUserMessageId`，但 SDK 高层的 `turn()` 没有暴露它。所以崩溃窗口里只能按时间和输入内容来对账。等 SDK 暴露这个字段后，改用它精确关联。

### D15. 会话状态机与命令竞态

每个会话一个状态机：

```
idle ──turn()──► running ──完成/失败──► idle
                   │
                   ├─/stop──► stopping ──turn 结束或超时──► idle
                   └─/new ──► stopping ──► 切换当前 thread ──► idle
```

- **状态变化通过每个会话的一把短锁串行化，这把锁里绝不等待网络**（评审 R4）。steer、interrupt、compact 等 RPC 都在锁外执行，并且都有超时。
- **控制屏障**（评审 R2）：改变执行状态的命令在收到时立即竖起屏障（D7）。例如依次收到 `/stop`、`/new`、普通消息：旧 turn 被中断后，worker 发现屏障仍在，不会把普通消息交给旧 thread；等 `/new` 执行完、屏障放下，普通消息才派发到新 thread。
- **`/stop` 和 `/new` 共用一个截止时间**（`CODEX_IM_STOP_TIMEOUT`，评审 R4），**interrupt RPC 本身也算在内**：
  1. 进入 stopping，在截止时间内调用 `interrupt()`；
  2. 在剩余时间内等这个 turn 结束；
  3. 到截止时间还没结束：**放弃**本地消费者，turn 标为 abandoned，**cwd 继续占用**（D6），通知用户，并由对账继续尝试中断、轮询历史；
  4. `/new` 接着切换当前 thread。新 thread 的第一个 turn 会等待旧 turn 释放占用。
- `/stop` 在 stopping 状态下不做任何操作（幂等）。在空闲状态下，如果本会话还有未确认的 turn，会立即再次触发对账和 interrupt，并提示可以用 `/release`。
- `/stop` 和 `/new` 都会关闭当前 thread 的待回答问题（评审 R11）。
- 迟到的事件按 `codex_turn_id` 归属到旧 turn，不会混入新 thread。
- **`/resume`、`/retry`、`/release` 只能在 idle 状态下执行**；在 running 状态下会提示先发 `/stop`。
- 等待 cwd 占用的 turn 处于 running 状态但还没有 handle，期间可以被 `/stop` 取消。

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
 │                          │ agentd:4501  CODEX_HOME  /workspace (volumes)  │ │
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
| `core/outbox` | 结果持久化之后再投递；重试、退避、dead 标记（D14） |
| `core/recovery` | 启动时对账未完成的 turn、续接失败的分类处理（D14） |
| `agentd`（agent 容器内） | 创建会话目录和 git worktree（D5、D6） |

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

1. 适配器收到平台事件，先把它写入 `inbound`（去重），再向平台确认。
2. Router 计算会话键，做准入检查和群聊 @ 判定；如果是命令，交给命令处理器，运行中可用的命令直接进入状态机（D15）。
3. 会话 worker 取出排队的消息：
   - 当前 thread 不存在时，先通过 `agentd` 确保 cwd 存在，再 `thread_start`，**立即写入 `threads`**；
   - 存在时用 `thread_resume`，失败按 D14 分类处理。
4. 有活动 turn 时按 D14 的规则追加；否则先占用 cwd（D6），写入 `turns`（pending），然后调用 `thread.turn(input)`，记录 `codex_turn_id`。
5. Renderer 消费 `stream()` 的事件：
   - 先发占位消息，之后节流编辑；
   - `delivery=async` 的消息立即写入 outbox 并单独投递。
6. turn 结束时，在一个事务里写入 `final_text` 和 outbox，然后投递。需要编辑流式消息落定的，优先编辑。

## 4. 从 Hermes 借鉴的经验（不借代码，只借教训）

1. Codex thread 才是真正的上下文。不要用本地记录"重建"上下文，也不要为了压缩而丢掉活跃的 thread。
2. resume 失败时，不能用新 thread 静默替换掉已有的上下文。Hermes 的做法是"失败就新开 thread，提示一次"，本项目改为按 D14 分类处理。
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
- **开发方式**：在 Windows 本机上只做设计和编码，不运行任何东西；构建、测试、部署全部在 Ubuntu 设备上进行（见 AGENTS.md 和 verification.md）。
- **P0 验收顺序**（采纳 astra 的建议，2026-11-19）：先用一个可编辑消息的平台（Telegram）和一个只能发最终结果的平台（Email）把完整流程跑通，优先验收几个共性场景；之后再推广到飞书和微信。

## 6. 待决问题

- 语音消息（STT 和 TTS）是否需要。
- 是否提供 `api_server` 之类的非 IM 入口（Hermes 内置，但不在 `plugins/platforms` 中）。
- 许可证。
