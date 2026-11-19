# 部署：Ubuntu + Docker

> 状态：实际文件在 `deploy/` 目录（`compose.yaml`、`compose.fake.yaml`、`docker/Dockerfile`、`egress/`、`secrets/*.example`），常用命令见根目录的 `Makefile`。下面的片段用于说明设计，以 `deploy/` 中的文件为准。这些文件都还没有在 Ubuntu 上运行过。

## 1. 宿主机要求

- Ubuntu 22.04 或 24.04 LTS。
- Docker Engine：按 Docker 官方 apt 源安装（不用 snap 版），外加 compose v2 插件。
- **宿主机上不需要安装 codex、python 或 hermes**，codex-IM 全部运行在容器里。

## 2. 与宿主机已有 codex / hermes 的隔离

| 隔离项 | 做法 |
|---|---|
| Codex 状态 | agent 容器使用独立的 `codex-home` volume；**不挂载** `~/.codex` |
| Hermes 状态 | 不挂载 `~/.hermes`；不和 Hermes 共用任何文件 |
| IM 凭证 | **使用独立的 bot 应用和 token**。和宿主机上的 Hermes 共用同一个 bot 会抢消息或冲突 |
| 进程和网络 | 独立的 compose 项目名 `codex-im`、独立的网络；不用 `network_mode: host`；**不挂载 `/var/run/docker.sock`** |
| 端口 | 只有 ingress（webhook 平台才需要）对外发布端口，其余服务都不发布 |

## 3. 服务拓扑

| 服务 | 镜像 | 网络 | 说明 |
|---|---|---|---|
| `bridge` | `codex-im-bridge:<CODEX_VERSION>` | `im`、`agent_internal` | IM SDK、会话库、`openai-codex` SDK；持有 IM 凭证 |
| `agent` | `codex-im-agent:<CODEX_VERSION>` | 只接 `agent_internal`（internal） | `socat` 后面接 `codex app-server`（端口 4500），加上 `agentd`（端口 4501，负责创建目录和 worktree）；持有工作区和模型凭证 |
| `egress-proxy` | squid（或同类） | `agent_internal`、`egress` | agent 唯一的出网通道，按域名白名单放行 |
| `ingress`（可选） | caddy | `im`，发布 443 端口 | webhook 平台的 HTTPS 入口，反向代理到 bridge |
| sidecars（可选） | signal-cli、simplex-chat、wa-bridge 等 | `im` | 通过 compose `profiles` 按需启用 |

```yaml
# 示意，不是最终文件
name: codex-im

networks:
  im: {}
  agent_internal: { internal: true }   # 这个网络没有直连外网的出口
  egress: {}

volumes:
  codex-home: {}
  workspace: {}
  bridge-data: {}

services:
  agent:
    image: codex-im-agent:${CODEX_VERSION}
    user: "10001:10001"
    networks: [agent_internal]
    volumes: [codex-home:/home/agent/.codex, workspace:/workspace]
    environment:
      CODEX_HOME: /home/agent/.codex
      HTTPS_PROXY: http://egress-proxy:3128
      HTTP_PROXY: http://egress-proxy:3128
      NO_PROXY: localhost,127.0.0.1
    env_file: [./secrets/agent.env]      # 例如 CODEX_IM_PROVIDER_API_KEY，权限 600
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 512
    mem_limit: 4g
    cpus: 2
    # entrypoint（tini 作为 PID 1）：根据环境变量生成 config.toml，然后启动两个进程：
    #   socat TCP-LISTEN:4500,reuseaddr,fork EXEC:"codex app-server --listen stdio://"
    #   python -m codex_im.agentd --listen 0.0.0.0:4501
    # 生成的 config.toml 里固定写入：features.default_mode_request_user_input = false（D13）

  bridge:
    image: codex-im-bridge:${CODEX_VERSION}
    user: "10002:10002"
    networks: [im, agent_internal]
    volumes: [bridge-data:/data]
    env_file: [./secrets/bridge.env]     # 各平台 token、白名单
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    depends_on: [agent]

  egress-proxy:
    image: ubuntu/squid
    networks: [agent_internal, egress]
    volumes: [./egress/squid.conf:/etc/squid/squid.conf:ro, ./egress/allowlist:/etc/squid/allowlist:ro]
```

## 4. 关键实现点

### 4.1 agent 容器：容器本身就是沙箱（设计决策 D4）

- 在 Docker 里，Codex 的 bubblewrap 沙箱通常无法创建 user namespace。所以 Codex 使用 `full_access` 并且不发起审批，隔离完全靠容器：
  - 非 root 运行；
  - `cap_drop: ALL` 加 `no-new-privileges`；
  - 只挂载工作区；
  - 限制 pids、内存、CPU；
  - 只能通过 egress-proxy 出网。
- **不要**为了让 bubblewrap 能运行而加 `SYS_ADMIN`、使用 `seccomp=unconfined` 或 `privileged`。
- 如果需要更强的隔离，可选改用 gVisor（`runtime: runsc`）运行 agent。这是可选加固项，P4 再评估。

### 4.2 bridge ↔ agent 的 stdio-over-TCP 中继（设计决策 D5）

- **agent 端**：
  ```
  socat TCP-LISTEN:4500,reuseaddr,fork EXEC:/usr/local/bin/codex-app-server
  ```
  `codex-app-server` 是一个包装脚本，内容是 `exec codex app-server --listen stdio://`；这样 socat 的 EXEC 参数里就不会出现冒号和空格。每个连接启动一个 app-server。bridge 必须保证同一时间只有一条连接，避免多个 app-server 同时写同一个 `CODEX_HOME`。
- **bridge 端**：
  ```python
  CodexConfig(launch_args_override=("socat", "-", "TCP:agent:4500"))
  ```
- 这条中继没有鉴权，只依赖 internal 网络和 `range` 限制。可选加固：改成 Unix socket 加共享 volume，或者用 mTLS 的 `socat OPENSSL`。
### 4.2.1 agentd（D5、D6）

- 提供几个固定的操作：`ensure_dir(session_path)`、`worktree_add(project, session)`、`worktree_remove(project, session)`。不接受任意命令。
- 以 agent 用户身份运行，权限和 Codex 一样，不构成新的提权面。只监听 internal 网络。
- 参数里的路径必须在 `/workspace` 下，并做规范化，拒绝 `..` 和符号链接逃逸。

- **P0 验证项**（verification.md V0）：
  - 初始化握手；
  - 多个并发 turn；
  - agent 重启后 bridge 能自动重连并 `thread_resume`；
  - 中继下拿不到 app-server 的 stderr，诊断信息改看 agent 容器日志。

### 4.3 出网白名单

- **为什么放在 agent 一侧**：`agent_internal` 网络是 internal 的，agent 没有直连外网的出口。即使 shell 命令绕过 `HTTPS_PROXY`，也连不上外网。
- **白名单内容**（`deploy/egress/allowlist`）：
  - 模型和认证域名（ChatGPT 模式，具体列表 P0 实测后确定）；
  - 自定义 provider 的 host；
  - 按需开放的 pypi、npm、github 等。
- **bridge 不经过代理**：bridge 不运行不可信代码，因此各平台 SDK 不需要支持代理，减少兼容问题。

### 4.4 Webhook 入口（仅 B 类平台需要）

- bridge 内置一个 ingress HTTP 服务，单端口，路径形如 `/hooks/<platform>`；只在 `im` 网络上监听。
- 宿主机侧有两种做法：
  - Caddy 自动申请 HTTPS 证书，反向代理到 `bridge:8080`；
  - 用隧道（例如 cloudflared），这样不需要公网 IP。
- **ufw 注意**：Docker 发布的端口会绕过 ufw（规则在 `DOCKER-USER` 链里）。只发布 ingress 的端口；用隧道时绑定到 `127.0.0.1`。

### 4.5 公司网络

- **代理**：egress-proxy 配置上游代理；bridge 容器配置 `HTTPS_PROXY` 和 `NO_PROXY`。
- **根证书**：打进两个镜像（`update-ca-certificates`）。Codex 也支持用 `CODEX_CA_CERTIFICATE` 或 `SSL_CERT_FILE` 指定。

## 5. 运维流程（草案）

```bash
# 首次部署
for f in deploy/secrets/*.env.example; do cp "$f" "${f%.example}"; done
chmod 600 deploy/secrets/*.env                                # 然后填写
docker compose build --build-arg CODEX_VERSION=0.167.2
docker compose run --rm agent codex login --device-auth      # 仅 ChatGPT 模式需要
docker compose up -d
docker compose logs -f bridge

# 升级 Codex（设计决策 D12）
docker run --rm -v codex-im_codex-home:/v -v $PWD/backup:/b alpine tar czf /b/codex-home-$(date +%F).tgz -C /v .
docker compose build --build-arg CODEX_VERSION=<新版本>
docker compose run --rm bridge python -m codex_im.smoke      # 冒烟测试：新建 thread / 只读 turn / resume
docker compose up -d

# 回滚：切回上一个镜像 tag；必要时用升级前的备份恢复 codex-home
```

两个镜像共用同一个 `CODEX_VERSION`：bridge 安装 `openai-codex==$CODEX_VERSION`；agent 使用同一个包自带的 `openai-codex-cli-bin` 内核二进制，保证 SDK 和内核版本一致。
