# 验证计划（在 Ubuntu 设备上执行）

Windows 本机只写代码和文档，**不运行任何东西**。构建、测试、部署和下面所有场景都在 Ubuntu 设备上执行。结果记录在对应 PR 的描述里：写明场景编号、Codex 版本、平台、结论，以及日志摘要（不能包含用户内容）。

代码里凡是依赖"尚未在 Ubuntu 上验证过的 Codex 或平台行为"的地方，都标注 `# VERIFY(ubuntu): <场景编号> <假设>`。场景验证通过后，删掉对应标注。

## 测试设施（P0a 一起实现）

- **`fake` 平台适配器**：通过本地 HTTP 或 CLI 注入入站消息、查看出站消息，并且可以模拟"不支持编辑"和"投递失败"。自动化场景都基于它；真实平台只做手工冒烟测试。
- **故障注入点**：通过环境变量 `CODEX_IM_FAULTS=<point>[,<point>]` 开启，只在测试配置里生效：
  - `crash_after_inbound_insert`：入站消息持久化之后、向平台确认之前，进程直接退出；
  - `crash_after_turn_pending`：`turns` 写入 pending 之后、调用 `turn()` 之前退出；
  - `crash_after_turn_start`：拿到 `codex_turn_id` 之后退出；
  - `crash_before_outbox_send`：结果已写入 outbox、还没投递时退出；
  - `crash_after_steer_persist`：追加意图已持久化、steer RPC 发出之前退出；
  - `crash_after_steer_send`：steer RPC 已成功、本地还没标记为 steered 时退出；
  - `fail_delivery:<n>`：前 n 次投递返回临时失败；
  - `fail_delivery_permanent`：投递返回永久失败。
- **冒烟测试** `python -m codex_im.smoke`：依次做初始化、新建 thread、执行一个只读 turn、resume 这个 thread。

## 如何执行

**单元测试和端到端测试**（Codex 是模拟的，不需要网络和登录）：

```bash
make test
```

`tests/test_sessions.py` 和 `tests/test_recovery.py` 用模拟的 Codex 覆盖了下面各场景的**逻辑**：V1 的 async 提问、V2-1、V3 的 a/b/e、V4-1、V5-1、V6、V7。真实的 Codex 和平台行为仍然要按下面的场景在 Ubuntu 上实测。

**接真实 Codex 的 fake 平台**：

```bash
make up-fake                                    # bridge 只启用 fake 平台；控制 API 在 127.0.0.1:8787
curl -s -XPOST localhost:8787/inbound -H 'content-type: application/json'      -d '{"chat_id":"c1","user_id":"u1","text":"列出当前目录的文件"}'
curl -s localhost:8787/outbound | python3 -m json.tool
curl -s -XPOST localhost:8787/control -d '{"can_edit": false}'      # 模拟不能编辑消息的平台
curl -s -XPOST localhost:8787/control -d '{"fail_next": 3}'         # 后面 3 次投递失败
```

**故障注入**：启动时设置 `CODEX_IM_FAULTS`，例如：

```bash
CODEX_IM_FAULTS=crash_after_turn_start make up-fake
```

- bridge 在注入点以退出码 77 退出，compose 的 `restart: unless-stopped` 会把它重新拉起，然后走恢复流程。
- 每个注入点在同一个 data 卷里只触发一次，避免反复崩溃。要再次触发，先删掉标记：`docker compose -f deploy/compose.yaml exec bridge rm -rf /data/faults-fired`。

## 场景

### V0. 环境与中继（D4、D5）

| 检查项 | 期望 |
|---|---|
| bridge 通过 `socat` 中继完成 SDK 的初始化 | 成功，并能拿到 `metadata` |
| 两个会话同时运行 turn | 事件按 turn 正确路由，没有串台 |
| 重启 agent 容器 | bridge 自动重连；会话被 `thread_resume` 接上，或按 D14 分类处理 |
| 在容器里让 Codex 执行 shell 命令（`full_access`） | 命令能执行；不会因为 bubblewrap 失败而报错 |
| `agentd` 创建会话目录和 worktree | 目录的所有者是 agent 用户；bridge 容器里看不到工作区 |
| 观察出网 | 记录 ChatGPT 模式实际需要放行的域名，回填到 `deploy/egress/allowlist` |
| 关掉中继进程，确认 app-server 在 stdin 收到 EOF 时的行为 | 记录进行中的 turn 最终是什么状态（结果作为 V3 的输入） |

### V1. 澄清提问的往返（D13）

1. 发一条消息，要求 Codex"先问我用哪种语言，再继续"。
   - **期望**：问题以一条**单独的消息**出现（来自 async agentMessage，或者最终回复里的文字提问），**不会被后面的输出覆盖**。
2. 在 turn 还在运行时回答。**期望**：通过 `steer()` 追加到当前 turn，Codex 根据回答继续。
3. 等 turn 结束后再回答。**期望**：作为一个新的 turn 执行，上下文连续。
4. 有问题挂着时发 `/stop`。**期望**：turn 被中断，问题状态变为关闭。
5. 回归检查（**每次升级 Codex 都要做**）：开启 `RUST_LOG` 跑一组容易引发提问的任务，检查 app-server 日志，确认**没有出现** `item/tool/requestUserInput` 请求。同时确认生成的 `config.toml` 里有 `features.default_mode_request_user_input = false`。
6. 记录当前 model 是否声明了 `request_user_input_async`，作为能力说明。

### V2. 重复消息（D14）

1. 用 fake 适配器把同一个 `message_key` 注入两次。**期望**：只执行一次；第二次直接确认并丢弃。
2. Telegram：开启 `crash_after_inbound_insert`，发一条消息，进程退出后重启。**期望**：Telegram 重新投递这条 update，按 `update_id` 去重，只执行一次。
3. Email：同样的故障注入。**期望**：同一个 `Message-ID` 只执行一次。

### V3. 执行中重启（D14）

| 子场景 | 操作 | 期望 |
|---|---|---|
| a | `crash_after_turn_pending` | 重启后在 thread 里找不到对应的 turn，标记为 unknown，并提示可以 `/retry`；**不自动执行** |
| b | `crash_after_turn_start`（turn 正在运行） | 重启后按 `codex_turn_id` 查询：已完成就补发结果；被中断就提示 `/retry` |
| c | 在 turn 运行时 `docker kill` agent | 按"临时故障"重试；不新开 thread；恢复后按 b 的规则处理 |
| d | thread 只做了 `thread_start`，还没跑过 turn，然后重启 | resume 失败后静默新开 thread（没有工作丢失） |
| e | 删掉一个已经 materialized 的 thread 的 rollout 文件，然后发消息 | 识别为"确实不存在"，标记为 lost，**明确告诉用户**，不静默替换 |
| f | 记录 a–e 中每种情况实际返回的错误类型、错误码和错误消息 | 写进 `codex/errors.py` 的映射表和单元测试 |

### V4. 回复失败后补发（D14）

1. 开启 `fail_delivery:3`。**期望**：退避重试后投递成功；Codex 里这个 thread 的 turn 数**没有增加**。
2. 开启 `crash_before_outbox_send`，然后重启。**期望**：结果被补发，没有重新执行。
3. Telegram：投递时临时断开 bridge 的 `im` 网络（`docker network disconnect`），之后再接回。**期望**：结果被补发，没有重新执行。
4. 开启 `fail_delivery_permanent`。**期望**：记录标记为 dead，日志里有记录，没有重新执行。

### V5. `/stop` 与 `/new` 的竞态（D15）

1. 发一个耗时较长的任务，在 1 秒内依次发 `/stop`、`/new`，紧接着再发一条普通消息。**期望**：
   - 旧 turn 被中断；
   - 当前 thread 切换为新的；
   - 那条普通消息在新 thread 里执行；
   - 旧 turn 迟到的事件不会出现在新 thread 的输出里。
2. 连发两次 `/stop`。**期望**：幂等，不报错。
3. 在 running 状态下发 `/resume`。**期望**：被拒绝，并提示先发 `/stop`。
4. 用一个不响应中断的命令（比如 `sleep 600`）触发停止截止时间。**期望**：
   - 在 `CODEX_IM_STOP_TIMEOUT` 内给出"停止超时"的通知（即使 interrupt RPC 本身不返回）；turn 标为 abandoned；
   - `/status` 仍然立即响应，并显示"等待 Codex 确认结束的任务"；
   - 同一会话的新消息、`/new` 之后的新 thread、以及绑定同一 serial 项目的其他会话，都**不会开始执行**，并收到"已排队"的提示；
   - 远端 turn 真正结束后（或者管理员 `/release` 之后），排队的消息才开始执行；
   - 记录：在真实 Codex 上，对放弃的 turn 用 turn id 再发 interrupt 是否生效；中继断开后历史里这个 turn 的状态（V3b）。
5. 依次快速发送 `/stop`、`/new`、普通消息，并让旧 turn 在 `/stop` 等待期间才结束。**期望**：普通消息只在新 thread 中执行，旧 thread 没有收到它（评审 R2）。

### V6. 同一项目的并发（D6）

1. 两个会话（Telegram 群的两个 topic，或者 Telegram 和 Email 各一个）绑定同一个项目，`concurrency=serial`，同时发写文件的任务。**期望**：第二个 turn 排队，并收到"项目正被占用"的提示；两个任务前后执行。
2. 改成 `concurrency=worktree`。**期望**：两个任务并行，分别在各自的 worktree 和分支上工作，互不覆盖。
3. 同一个 Telegram 群的两个 topic，不绑定项目。**期望**：各自使用独立的默认目录 `/workspace/sessions/telegram/<chat>/<topic>`。

### V7. `/sessions` 和 `/resume` 的可见范围（D7）

1. 在会话 A 里 `/new` 两次，然后 `/sessions`。**期望**：列出 A 的 3 个 thread。
2. 在会话 B 里 `/resume <A 的 thread 短 ID>`。**期望**：回复"不存在"，B 的状态不变。
3. 在私聊里 `/sessions`。**期望**：看不到任何群会话的 thread。

### V8. Email 安全（D9）

1. 伪造白名单地址作为 `From`，并且没有通过 `Authentication-Results` 校验。**期望**：丢弃，记一条日志（不含邮件内容）。
2. 发一封带 `Auto-Submitted: auto-replied` 头的邮件。**期望**：丢弃。
3. 回复邮件。**期望**：带有正确的 `In-Reply-To` / `References`，落在同一个会话里。

### V10. 评审 2026-11-19-p0a 的回归（R1–R12）

`tests/test_review_p0a.py` 用模拟 Codex 覆盖了每一项的逻辑。在 Ubuntu 上还要用真实 Codex 确认下面几项依赖真实行为的地方：

| 项 | 在真实环境中确认 |
|---|---|
| R1 | 对刚结束的 turn 调用 steer，返回的是 JSON-RPC 错误（会被当成"拒绝"），而不是断线或超时；追加的输入在历史里以独立的 userMessage 出现。用 `crash_after_steer_persist` 和 `crash_after_steer_send` 各重启一次：两种情况都**不会**把那条消息当成新 turn 执行 |
| R3 | 见 V5-4 |
| R4 | 见 V5-4；另外断开 agent 网络让 steer 卡住，`/stop` 仍然在截止时间内返回 |
| R5 | 用 `{"fail_next": 1}` 让第一条分段失败，确认后面的分段不会先到 |
| R6 | 用 `crash_after_turn_pending` 制造一次"无 ID 恢复"，然后发送完全相同的内容并再次崩溃：第二次不会收到第一次的结果。另外确认 `startedAt` 存在且单位是秒 |
| R7 | 一个只提问、没有最终回答的 turn 在提问后崩溃：重启后问题被补发一次 |
| R8 | 用真实邮件客户端往返三次以上，并且只回复机器人的邮件：始终在同一个会话里 |
| R9 | 在没有任何 codex-im 容器的机器上，严格按 README 的首次安装顺序执行 `make login`、`make smoke` |
| R10 | 发一个会产生超长提问或超长通知的任务：Telegram 收到分段，没有 dead 记录 |
| R11 | 提问之后 `/stop`：`/status` 显示待回答的问题为 0 |
| R12 | 群里发裸 `/stop` 没有效果；`/stop@机器人名` 生效 |

### V9. 升级回归（D12）

升级 `CODEX_VERSION` 之后依次执行：冒烟测试、V1 第 5 步、V3 的 d 和 e、V0 中"重启 agent 容器"那一项。
