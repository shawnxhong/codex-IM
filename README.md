# codex-IM

把 IM（Telegram、飞书、Slack、钉钉、企业微信、微信、Discord……共 27 个平台）接到 **OpenAI Codex** 上的一层薄桥接。

- **Codex 保持原样**：桥接只通过官方 Python SDK [`openai-codex`](https://github.com/openai/codex/tree/main/sdk/python) 驱动 `codex app-server`，不注入自定义的 harness。SDK 每个版本都精确锁定对应的 Codex 内核，升级只需要改一个版本号。
- **IM 覆盖面对齐 Hermes Agent**：平台清单取自 [hermes-agent](https://github.com/NousResearch/hermes-agent) 已实现的平台。只把 Hermes 当参考，不依赖、不 fork 它的代码。
- **两种模型认证**：ChatGPT 账号登录（device code），以及自定义 API key（任意兼容 Responses API 的端点）。两者可以同时启用，按会话切换。
- **隔离部署**：在 Ubuntu 上用 Docker 运行，与宿主机上已有的 codex / hermes 完全隔离。IM 凭证和执行代码的 agent 放在不同的容器里。

> **状态：P0a 代码已写完，尚未在 Ubuntu 上运行过。** 包括核心模块、Docker 部署、Telegram、Email，以及用于自动化验收的 fake 平台。
> 开发机（Windows）上不运行任何东西，所以目前所有测试和验收场景**都还没有执行**。依赖未验证行为的代码处标有 `VERIFY(ubuntu)`，在 Ubuntu 上按 [docs/verification.md](docs/verification.md) 逐项确认。
> 首批平台：P0a 是 Telegram 和 Email；P0b 是飞书和微信（个人号）。

## 在 Ubuntu 上运行

前提：Docker Engine（官方 apt 源）和 compose v2 插件。宿主机上不需要 Python，也不需要 codex。

```bash
git clone https://github.com/shawnxhong/codex-IM.git && cd codex-IM
cp deploy/.env.example deploy/.env                     # CODEX_VERSION
for f in deploy/secrets/*.env.example; do cp "$f" "${f%.example}"; done
chmod 600 deploy/secrets/*.env                         # 然后按注释填写 shared / agent / bridge 三个文件

make test                                              # 在测试镜像里跑单元测试
make build
make login                                             # 仅 ChatGPT 模式需要：device code 登录，只做一次
make smoke                                             # 冒烟测试：经过真实中继走一遍 Codex
make up && make logs
```

用 fake 平台跑验收场景：`make up-fake`，然后通过 `http://127.0.0.1:8787` 注入消息、查看输出（详见 docs/verification.md）。

## 代码结构

```
src/codex_im/
  app.py              bridge 进程的组装和启动（python -m codex_im）
  config.py           CODEX_IM_* 配置
  codex/              runtime（AsyncCodex 封装）、events（版本无关的事件解析）、errors（错误分类）
  core/               store（SQLite）、router、sessions（状态机）、commands、render、outbox、finalize、recovery、workspace、admission
  platforms/          base（适配器接口）、telegram、mail（email 平台）、fake
  agentd/             agent 容器里的工作区服务（标准库实现）
  agent_config.py     agent 容器启动时生成 config.toml
  smoke.py            冒烟测试
deploy/               Dockerfile（bridge / agent / test 三个 target）、compose、egress 白名单、secrets 和配置样例
tests/                单元测试和端到端测试（fake 平台 + 模拟的 Codex）
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/design.md](docs/design.md) | 背景、目标、15 条关键设计决策（含澄清提问、可靠性、状态机）、架构、适配器接口、从 Hermes 借鉴的经验 |
| [docs/platforms.md](docs/platforms.md) | 27 个平台的接入方式、依赖、凭证、所属阶段，以及适配器验收清单 |
| [docs/auth.md](docs/auth.md) | ChatGPT 登录和自定义 API key 两种模式；Responses API 约束；公司网络 |
| [docs/deployment.md](docs/deployment.md) | Ubuntu + Docker 拓扑、容器即沙箱、出网白名单、升级与回滚 |
| [docs/roadmap.md](docs/roadmap.md) | P0a/P0b–P4 分阶段计划 |
| [docs/verification.md](docs/verification.md) | 在 Ubuntu 上执行的验收场景 V0–V9、测试设施 |
| [docs/reviews/](docs/reviews/) | 评审意见和处理记录 |

## 架构一览

```
IM 平台 ⇄ bridge 容器（适配器 → 路由/准入/命令 → 会话管理 → openai-codex SDK）
                    ⇅ stdio-over-TCP（internal 网络）
          agent 容器（codex app-server，full_access，非 root，工作区）
                    ⇣ 仅经过 egress-proxy（域名白名单）
          模型 API / ChatGPT 认证
```

## 许可

待定。如果借鉴 Hermes Agent（MIT）的代码片段，需要保留其版权声明。
