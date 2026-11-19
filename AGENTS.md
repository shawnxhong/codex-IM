# AGENTS.md：给在本仓库工作的人和 coding agent

动手前先读 `docs/design.md`。下面这些规则来自其中的设计决策，违反任何一条都需要先修改设计文档并说明理由。

## 开发方式（必须遵守）

- **Windows 本机只写设计和代码，不运行项目**：不跑测试、lint、docker，也不执行项目里的代码和脚本。构建、测试、部署全部在 Ubuntu 设备上进行。
- 没有在 Ubuntu 上实际跑过的东西，不能说"已通过"或"可用"。PR 描述里要写明哪些内容已在 Ubuntu 上验证、哪些还没有。
- 代码里凡是依赖"尚未在 Ubuntu 上验证过的 Codex 或平台行为"的地方，标注 `# VERIFY(ubuntu): <verification.md 场景编号> <假设>`。
- 所有文件使用 LF 换行（见 `.gitattributes`），shell 脚本要能在 Ubuntu 上直接执行。
- 评审意见和处理结果记录在 `docs/reviews/`。

## 不可违反的原则

1. **Codex 只通过 `openai-codex` SDK 的公开 API 访问**（D2）。不手写 JSON-RPC，不调用 SDK 的私有模块（以 `_` 开头的那些），也不用 `codex exec`。
2. **不往 Codex 注入 harness**（D3）。不传 `base_instructions`，不改 `personality`，不注册额外的 MCP 工具；唯一的例外是可选的 `CODEX_IM_DEVELOPER_INSTRUCTIONS`。
3. **不导入、不 vendor hermes-agent 的代码**（D1）。可以阅读 Hermes 的实现作为参考；如果借鉴代码片段，在文件头注明出处并保留 MIT 版权声明。
4. **显式设置沙箱和审批**（D4）：`sandbox=Sandbox.full_access`、`approval_mode=ApprovalMode.deny_all`。不要依赖 SDK 的默认值。
5. **IM 凭证只存在于 bridge 进程和容器**（D5）。不要把它们传给 agent 容器，也不要写进 Codex 的配置。
6. **日志里不出现用户内容**（D9）。INFO 及以上级别只记录长度、哈希过的 ID 和事件类型。
7. **每个 thread 和 turn 都显式传 model**；provider 在 thread 上固定（D6）。
8. **不自动重跑可能已经产生副作用的 turn**（D14）。回复失败时只重新投递结果；重新执行只能由用户的 `/retry` 触发。
9. **不能只看 `final_response`**：`delivery=async` 的 agentMessage 必须逐条投递（D8、D13）。
10. **bridge 不在工作区里执行 git，也不挂载工作区**；工作区操作一律通过 `agentd` 完成（D5、D6）。

## 适配器约定

- 适配器只做协议转换：实现 `PlatformAdapter` 接口，并声明 `Capabilities`。
- 准入、命令、会话、渲染都由 core 负责，适配器不要自己实现。
- 凭证环境变量名和 Hermes 保持一致（D11），并在 `docs/platforms.md` 中登记。
- 新增或修改平台时，逐条满足 `docs/platforms.md` 末尾的验收清单。

## 目录约定（计划）

```
src/codex_im/
  core/        router / sessions / commands / render / store
  codex/       runtime（AsyncCodex 封装）
  platforms/   <name>/adapter.py
  ingress/     webhook 平台共享的 HTTP 服务
deploy/        Dockerfile、compose、egress 白名单、secrets 样例
tests/
docs/
```
