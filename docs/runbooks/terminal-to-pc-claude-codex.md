# 终端 → PC Claude / Codex 端到端操作手册

这份 runbook 描述从终端准备 Omi，到让 PC 上的 Claude Code、Codex CLI、Claude Desktop
或 ChatGPT/Codex 云端界面完成一次实际工作的完整链路。它以 self-hosted / neutral
部署为默认；managed 部署只需把 operator origin 替换为 Omi 的公开 origin。

先区分三个容易混淆的产品：

| 名称 | 运行位置 | 终端能否直接驱动 | MCP 配置方式 |
| --- | --- | --- | --- |
| Claude Code | PC 本地 CLI | 可以，`claude` / `claude -p` | `claude mcp add` 或 `~/.claude.json` |
| Codex CLI | PC 本地 CLI | 可以，`codex` / `codex exec` | `codex mcp add` 或 `~/.codex/config.toml` |
| Claude Desktop、ChatGPT/Codex 云端界面 | GUI / 云端产品 | 终端只能准备配置；实际对话在界面中确认 | Claude Desktop 本地配置，云端走 OAuth/辅助连接 |

“终端驱动 PC 上的 Claude/Codex”在本手册中优先指本地 CLI。GUI 自动化不是把命令
塞进窗口，而是由 Omi Desktop 的 ACP/辅助流程在权限边界内完成；需要自动化 GUI 时，
必须遵循观察 → 单步操作 → 重新观察 → 验证 → 有限恢复的闭环。

相关架构图：

![部署中立架构](../architecture/deployment-neutral-architecture.png)

![从采集到行动的数据流](../architecture/data-flow-capture-to-act.png)

## 1. 端到端链路

```text
终端
  │  配置检查、启动服务、创建一次性 MCP key
  ▼
operator-owned HTTPS origin
  │  POST /v1/mcp/keys（Better Auth token）
  │  POST /v1/mcp/sse（MCP bearer key）
  ▼
PC 上的 Claude Code / Codex CLI
  │  tools/list、tools/call
  ▼
Omi MCP server → canonical memory / conversation / task projections
  │
  └─ agent 修改当前工作树 → 人工审阅 diff → 测试 → 提交或交给 PR 流程
```

两种 bearer 不可混用：

- `Better Auth` 的访问 token 只用于创建、列出、撤销 MCP key。
- `omi_mcp_...` 在本 runbook 中只用于 `/v1/mcp/sse` 及其 MCP 工具调用，不能拿去访问
  普通 REST 路由或创建另一把 key。

## 2. 终端准备：配置、启动、确认 authority

### 2.1 self-host 配置

在 operator 主机上准备真实 env 文件；不要把运行时密钥写回仓库示例文件：

```bash
cp deploy/self-host/.env.production.example deploy/self-host/.env.production
# 填入真实值；不要把文件内容贴到聊天、日志或 issue
make self-host-config-check SELF_HOST_ENV=deploy/self-host/.env.production
deploy/self-host/compose-clean-env.sh \
  deploy/self-host/.env.production deploy/self-host/compose.production.yml \
  config --quiet
SELF_HOST_ENV="$PWD/deploy/self-host/.env.production" \
  deploy/self-host/operations.sh start
```

所有 Compose 运维命令都通过 `compose-clean-env.sh`，避免宿主机的旧环境变量覆盖
经过审阅的 env 文件。反向代理必须提供 operator-owned HTTPS origin：

```text
PUBLIC_BACKEND_URL  → backend
PUBLIC_AUTH_URL     → Better Auth
PUBLIC_MCP_URL      → backend MCP routes
PUBLIC_OBJECTS_URL  → object storage
OMI_SHARE_BASE_URL  → share service
```

MCP 资源的精确关系是：

```text
MCP_RESOURCE_URL = ${PUBLIC_MCP_URL}/v1/mcp/sse
```

neutral/self-hosted 缺少或误填 `PUBLIC_MCP_URL` 时，发现、authorize、token 会返回
typed `503 deployment_capability_unavailable`，不会退回 `api.omi.me`。生产客户端应只
使用 HTTPS、无路径/查询/凭据的 canonical origin；内部 Compose 服务的 HTTP 只允许在
明确的私网约束下使用。

### 2.2 不泄露密钥的 shell 环境

以下变量应来自密码管理器或受保护文件，不要从完整 `.env.production` 直接 `source`：

```bash
export OMI_MCP_ORIGIN='https://mcp.example.net'
export OMI_MCP_KEY_FILE='/secure/omi/terminal-pc-agent.mcp-key'
export OMI_AUTH_TOKEN_FILE='/secure/omi/better-auth-access-token'

umask 077
test "$(stat -f '%Lp' "$OMI_MCP_KEY_FILE" 2>/dev/null || stat -c '%a' "$OMI_MCP_KEY_FILE")" = 600
```

如果 key 尚未存在，使用当前已登录用户的 Better Auth token 创建它。响应中的 raw key
只出现一次，因此直接写入 mode `0600` 文件：

```bash
umask 077
OMI_AUTH_TOKEN="$(<"$OMI_AUTH_TOKEN_FILE")"
curl --fail-with-body --silent --show-error \
  -X POST "$OMI_MCP_ORIGIN/v1/mcp/keys" \
  -H "Authorization: Bearer $OMI_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"name":"terminal-pc-agent"}' \
  | jq -er '.key' > "$OMI_MCP_KEY_FILE"
chmod 600 "$OMI_MCP_KEY_FILE"
unset OMI_AUTH_TOKEN
```

如果命令返回 `401`，通常是把 MCP key 当成了 Better Auth token；如果返回 `503`，先
检查 Better Auth、反向代理和 self-host migration gate。撤销旧 key 使用当前 Better
Auth token 调用 `DELETE /v1/mcp/keys/{key_id}`；不要把 raw key 放进 git、shell history、
截图或 agent prompt。

### 2.3 MCP authority smoke

只打印元数据，不打印 token：

```bash
curl --fail-with-body --silent --show-error \
  "$OMI_MCP_ORIGIN/.well-known/oauth-authorization-server" | jq .
curl --fail-with-body --silent --show-error \
  "$OMI_MCP_ORIGIN/.well-known/oauth-protected-resource/v1/mcp/sse" | jq .
```

检查结果中的：

- `issuer` 等于 `$OMI_MCP_ORIGIN`；
- protected resource 等于 `$OMI_MCP_ORIGIN/v1/mcp/sse`；
- `authorization_servers` 只包含当前 operator origin；
- 没有任何 `api.omi.me` 或其他 managed host。

## 3. 把 Omi MCP 接到 PC 客户端

### 3.1 Claude Code（推荐：终端注册）

命令参数使用环境变量展开，shell history 中不会保存 raw key：

```bash
OMI_MCP_KEY="$(<"$OMI_MCP_KEY_FILE")"
claude mcp add --scope user --transport http omi-memory \
  "$OMI_MCP_ORIGIN/v1/mcp/sse" \
  --header "Authorization: Bearer $OMI_MCP_KEY"
unset OMI_MCP_KEY
claude mcp list
chmod 600 "$HOME/.claude.json" 2>/dev/null || true
```

等价的 Claude Code 用户配置位于 `~/.claude.json` 的
`mcpServers.omi-memory`，形状如下；保留其他 server，不要用整文件覆盖：

```json
{
  "mcpServers": {
    "omi-memory": {
      "type": "http",
      "url": "https://mcp.example.net/v1/mcp/sse",
      "headers": {
        "Authorization": "Bearer <key-from-0600-file>"
      }
    }
  }
}
```

Omi Desktop 的 **Use Omi memory anywhere → Claude / Claude Code** 会写同一配置，且在
修改前备份到 `~/.claude/backups/`；配置写入后会重新读取文件确认 URL 与当前 key 一致。

### 3.2 Codex CLI（推荐：`codex mcp add`）

`codex mcp add` 会写入 `CODEX_HOME` 下的 `config.toml`。明确设置 home，避免终端与
桌面进程使用两份配置：

```bash
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME"
OMI_MCP_KEY="$(<"$OMI_MCP_KEY_FILE")"
codex mcp add omi-memory -- \
  npx -y mcp-remote "$OMI_MCP_ORIGIN/v1/mcp/sse" \
  --header "Authorization: Bearer $OMI_MCP_KEY"
unset OMI_MCP_KEY
codex mcp list
chmod 600 "$CODEX_HOME/config.toml" 2>/dev/null || true
```

等价配置为 `~/.codex/config.toml`（Windows 为 `%USERPROFILE%\\.codex\\config.toml`）：

```toml
[mcp_servers.omi-memory]
command = "npx"
args = ["-y", "mcp-remote", "https://mcp.example.net/v1/mcp/sse", "--header", "Authorization: Bearer <key-from-0600-file>"]
```

若 Codex 报 `command not found`，先在同一个终端确认 `command -v codex`、`node --version`
和 `npx --version`；不要让 Omi Desktop 静默使用另一用户的 `CODEX_HOME`。

### 3.3 Claude Desktop GUI

Claude Desktop 与 Claude Code CLI 不是同一个配置面。手工 fallback 配置文件：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\\Claude\\claude_desktop_config.json`

在现有 JSON 中合并以下 server，然后完全退出并重新打开 Claude Desktop：

```json
{
  "mcpServers": {
    "omi-memory": {
      "url": "https://mcp.example.net/v1/mcp/sse",
      "headers": {
        "Authorization": "Bearer <key-from-0600-file>"
      }
    }
  }
}
```

如果 operator 配置了 MCP OAuth client，也可以在 Claude 的 **Customize → Connectors →
Add custom connector** 中填写 `$OMI_MCP_ORIGIN/v1/mcp/sse`，由浏览器完成授权。终端
不能代替用户批准 consent，也不应把 Better Auth cookie 注入 GUI。

### 3.4 ChatGPT / Codex 云端界面

这里的 “Codex” 是 ChatGPT/Codex 云端界面，不是 `codex` CLI。云端连接使用 OAuth/PKCE
或产品目录，不读取本地 `~/.codex/config.toml`。self-host operator 必须先配置并验证：

```text
Remote MCP server URL: https://mcp.example.net/v1/mcp/sse
Authorization URL:     https://mcp.example.net/authorize
Token URL:             https://mcp.example.net/token
```

在 GUI 中完成登录、consent 和连接测试后，再从该界面发任务。若 operator 没有注册
OAuth client，改用 Codex CLI + MCP key；不要把 MCP key 粘贴到云端 OAuth 表单。

## 4. 让 agent 真正做事

### 4.1 先做只读 MCP 验证

在 Claude Code 或 Codex 的新会话中输入一条明确的验证 prompt：

```text
先调用 omi-memory 的 get_memories（limit=5）确认 MCP 已连接。
只汇总返回的非敏感上下文，不修改文件、不执行 git push、不创建外部资源。
如果工具不可用，报告具体错误类别，不要假装已经读到记忆。
```

这一步验证的是：客户端看到 `omi-memory`、bearer key 通过、服务端授权成功、canonical
memory projection 可读。空结果不等于连接成功；要区分“没有匹配数据”和“工具未授权”。

### 4.2 交互式本地 CLI

```bash
cd /path/to/target/repository
claude
# 或
codex
```

建议第一条工作 prompt 固定包含边界：

```text
在当前仓库完成：<明确任务>。
开始前先用 omi-memory 搜索与该仓库/任务相关的上下文；不要读取或打印密钥。
先检查现状，再修改；每个行为改动补最小回归测试。
完成后运行相关测试，汇报改动文件、测试命令和未完成事项。
不要 push、不要操作生产服务，除非我另行明确要求。
```

### 4.3 非交互式一次性任务

适合 CI-like、本地可审阅的任务；默认保留 agent 的审批边界：

```bash
cd /path/to/target/repository
claude -p '检查当前测试失败原因，提出并实现最小修复；先搜索 omi-memory，完成后运行 focused tests。不要 push。'

codex exec --skip-git-repo-check --sandbox workspace-write \
  '检查当前测试失败原因，提出并实现最小修复；先搜索 omi-memory，完成后运行 focused tests。不要 push。'
```

不要在无人审阅的生产工作树上使用 `--dangerously-skip-permissions`、全局写权限或把
生产 secret 传给 prompt。一次任务完成后，从终端确认：

```bash
git status --short
git diff --check
# 按仓库组件指南运行 focused test / typecheck
```

### 4.4 通过 Omi Desktop 驱动 PC agent

在支持 ACP 的 Omi Desktop 中，可在聊天或 push-to-talk 中说：

```text
ask Codex to fix the failing test in /path/to/repo
use Claude Code to add a README for /path/to/repo
```

当前产品契约是：Claude Code bridge 内置；Codex 使用已安装的 CLI/ACP adapter。任务的
working directory 必须是明确路径，agent 使用最小 allowlist 环境，不自动获得永久权限。
可在 **Settings → Agents** 运行真实 ACP handshake；连接失败时应显示失败原因，不应悄悄
切换到另一个 provider 并声称成功。

## 5. 完成后的证据链

一项可交付任务至少留下以下证据：

1. agent 实际调用过 `omi-memory` 的只读工具（或明确记录工具不可用）；
2. `git diff --check` 通过，改动集中在目标工作树；
3. 运行了与改动对应的 focused test、typecheck 或构建；
4. 人工检查 diff，确认没有密钥、生产 endpoint、越权命令或未授权外部写入；
5. 需要落地时走 feature branch → PR → review → merge，不让 agent 直接 push `main`。

可把以下信息写入变更记录，但不要写 raw token：MCP origin、key id/prefix、客户端配置
路径、agent/CLI 版本、prompt 摘要、工作树 SHA、测试命令与结果、未完成的外部证据。

## 6. 故障排查

| 症状 | 先查什么 | 正确处理 |
| --- | --- | --- |
| metadata 返回 503 | `PUBLIC_MCP_URL`、TLS、`MCP_RESOURCE_URL` | 修复 operator origin；不要加 managed URL fallback |
| `401 Invalid API Key` | key 是否以 `omi_mcp_` 开头、是否已撤销 | 重新创建 MCP key；不要拿 Better Auth token 访问 MCP |
| 创建 key 返回 401 | `/v1/mcp/keys` 是否用了 Better Auth token | 用当前登录用户的 Better Auth access token |
| 客户端列出 server 但没有工具 | CLI 配置文件、`CODEX_HOME`、客户端是否重启 | `claude mcp list` / `codex mcp list`，修复后重启会话 |
| `npx mcp-remote` 启动失败 | Node、npx、网络/TLS、URL 是否完整 | 在同一终端运行 `node --version`、`npx --version`，再重试 |
| 读到空记忆 | 查询没有命中，或 projection 不可用 | 让 agent 报告工具返回的错误/计数；不要把空结果当成功 |
| GUI Claude/Codex 没有自动执行 | 误把 GUI 当 CLI | 在 GUI 中完成 consent/确认；或切换到 CLI/ACP 路径 |
| Omi Desktop 显示已连接但 agent 失败 | 连接状态是本地配置扫描，不是永久成功 latch | 重新运行实际 handshake 或只读 MCP probe |

## 7. 安全边界与已知限制

- MCP key 是高价值 bearer credential，使用 mode `0600`、最小生命周期、按设备/任务分
  名，轮换后重新检查客户端配置；不要记录 raw key。
- self-hosted/neutral 只允许 operator-owned authority；缺失能力应 typed-unavailable，
  不能偷偷访问 Firebase、`api.omi.me` 或其他 vendor。
- Omi MCP 工具的写操作（例如创建/删除 memory、更新 task）必须由 agent prompt 和产品
  权限共同约束；终端命令本身不等于用户批准。
- 云端 Claude/ChatGPT-Codex 的 OAuth consent、第三方连接器和生产 TLS/KMS/恢复演练仍
  需要 operator 的外部证据；本地命令成功不代表生产 cutover 已完成。
- GUI 端的自动化遵循 `desktop/macos/docs/integrations-philosophy.md`：优先 API，再用
  DOM/Accessibility；每个动作后重新观察并进行功能性 probe。坐标点击或“看到窗口就算
  成功”不满足验收。

## 8. 相关契约

- [MCP setup](../doc/developer/mcp/setup.mdx)
- [self-host deployment README](../../deploy/self-host/README.md)
- [deployment-neutral diagrams](../architecture/deployment-neutral-diagrams.md)
- [desktop integration philosophy](../../desktop/macos/docs/integrations-philosophy.md)
- [Windows connector ground truth](../../desktop/windows/docs/mac-parity-audit/track3-ground-truth/gt-connectors.md)
- [Codex invocation ledger](../../dev/codex-invocation-ledger.md)
