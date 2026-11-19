# 模型认证：ChatGPT 登录与自定义 API key

两种模式**可以同时启用**，按会话用 `/provider` 切换。原因：

- ChatGPT 登录的凭证存放在 `CODEX_HOME/auth.json`，供内置的 `openai` provider 使用；
- 自定义 provider 通过 `env_key` 从环境变量读 key，**不占用 `auth.json`**。

两者互不冲突。

所有 Codex 状态都放在 **agent 容器自己的 `CODEX_HOME`**（独立的 Docker volume）里。
**绝不挂载宿主机的 `~/.codex`**：同一份 refresh token 被两个实例使用时会因轮换而互相覆盖，导致掉线。Hermes 的文档也专门提到这一点。

## 模式一：ChatGPT 账号登录（provider = `openai`）

- **登录**：一次性操作，在容器里用 device code 流程完成，适合没有浏览器的服务器：
  ```bash
  docker compose run --rm agent codex login --device-auth
  # 终端会打印 URL 和验证码，在任意设备的浏览器里完成登录
  ```
  也可以在代码里调用 SDK 的 `Codex().login_chatgpt_device_code()`，bridge 可以借此提供一个仅限管理员的 `/login` 命令（P1）。
- 容器里没有系统 keyring，所以 `config.toml` 中设置：
  ```toml
  cli_auth_credentials_store = "file"   # 凭证写入 CODEX_HOME/auth.json（位于 volume 中）
  forced_login_method = "chatgpt"       # 可选：禁止在这个 CODEX_HOME 里改用 API key 登录
  ```
- token 由 Codex 自动刷新。出网白名单必须放行 ChatGPT / OpenAI 的认证和推理域名；具体域名列表在 P0 通过观察流量确定后写入 `deploy/egress/allowlist`。
- 额度计入你的 ChatGPT 套餐。

## 模式二：自定义 API key（自定义 provider）

在 `config.toml` 中声明（由 agent 容器的 entrypoint 根据环境变量生成）：

```toml
[model_providers.custom]            # id 不能和内置 provider 重名：openai / ollama / lmstudio / 云厂商内置项
name = "My Gateway"
base_url = "https://gateway.example.com/v1"
env_key = "CODEX_IM_PROVIDER_API_KEY"   # 只写环境变量名，key 本身不进配置文件
wire_api = "responses"
# 可选：
# http_headers     = { "X-Org" = "..." }               # 固定 header
# env_http_headers = { "X-Tenant" = "CODEX_IM_TENANT" } # 从环境变量取值的 header
# request_max_retries = 4
```

### 硬性约束：端点必须支持 Responses API

当前版本的 Codex 只支持 `wire_api = "responses"`（即 `/v1/responses`）；`wire_api = "chat"` 已经移除，配置了会直接报错（`codex-rs/model-provider-info/src/lib.rs`）。
如果你的端点只支持 Chat Completions，需要在前面加一层 Responses → Chat 的转换代理（例如 LiteLLM 之类，选型时确认它支持 `/v1/responses`），并且把这层代理也放进出网白名单。

### 用 OpenAI 官方 API key

推荐同样**配置成一个自定义 provider**（`base_url = "https://api.openai.com/v1"`，`env_key = "OPENAI_API_KEY"`，id 取 `openai-apikey` 之类），而不是执行 `codex login --with-api-key`。原因是后者会占用 `auth.json`，和 ChatGPT 登录互斥。

## bridge 侧配置（草案）

| 变量 | 含义 | 示例 |
|---|---|---|
| `CODEX_IM_DEFAULT_PROVIDER` | 新会话默认使用的 provider | `openai` 或 `custom` |
| `CODEX_IM_DEFAULT_MODEL` | 默认 model；bridge 在每个 thread 和 turn 上**显式传 model**，不依赖 Codex 的默认值 | `gpt-5.x` |
| `CODEX_IM_ALLOWED_PROVIDERS` | 允许 `/provider` 切换到的 provider 列表 | `openai,custom` |
| `CODEX_IM_PROVIDER_ADMINS` | 允许切换 provider 的用户（可选，默认所有白名单用户） | `telegram:123,feishu:ou_xxx` |
| `CODEX_IM_PROVIDER_API_KEY` | 自定义 provider 的 key，**只注入 agent 容器** | — |

几条规则：

- provider 在 thread 创建时就固定了：`/provider` 切换时会新开一个 thread，并提示用户上下文已重置。
- model 可以每个 turn 单独指定：`/model` 从下一个 turn 开始生效。

## 公司网络（代理和自签名根证书）

- **代理**：由 egress-proxy 统一配置上游代理（比如 squid 的 `cache_peer`）。agent 容器只看到 egress-proxy，不需要知道公司代理。
- **根证书**：在 agent 镜像里执行 `update-ca-certificates`，或通过 `CODEX_CA_CERTIFICATE` / `SSL_CERT_FILE` 指向公司根证书（Codex 的 http-client 支持这两个变量）。
- bridge 容器如果也在公司网络中，同样配置 `HTTPS_PROXY`、`NO_PROXY` 和根证书。

## 安全提示

- agent 拥有 full_access，理论上可以读到自己的模型凭证（`auth.json` 或 `env_key` 对应的环境变量）。这一点无法避免，缓解手段是：
  - 出网白名单，防止凭证被带出去；
  - 给自定义 key 设置额度上限，或使用专用 key；
  - 定期轮换。
- IM 凭证只存在于 bridge 容器，agent 一侧接触不到（设计决策 D5）。
