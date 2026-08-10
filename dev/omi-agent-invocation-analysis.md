# Omi Agent 调用机制分析:desktop 如何调用本机 Claude Code / Codex

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim

## 一、desktop 的 agent 架构(两层)

### 1. AI 聊天 agent(本地运行)
desktop 打包的 node agent bridge(`desktop/macos/agent/`)支持 4 种 adapter:

| Adapter | 依赖 | 如何运行 | 调用本机 CLI? |
|---|---|---|---|
| **acp**(Claude Code) | `@zed-industries/claude-agent-acp` | node 内置 SDK(patched-acp-entry.mjs) | ❌ 不调 `claude` CLI,用内置 SDK |
| **pi-mono**(Omi AI) | `@earendil-works/pi-coding-agent` | node 内置 SDK(dist/cli.js) | ❌ 纯 SDK |
| **hermes** | 本机 Hermes | `spawn(OMI_HERMES_ADAPTER_COMMAND)` | ✅ 本机 `hermes acp` 命令 |
| **openclaw** | 本机 OpenClaw | `spawn(OMI_OPENCLAW_ADAPTER_COMMAND)` | ✅ 本机 openclaw 命令 |

关键代码:
- `agent/src/adapters/acp.ts`: `spawn(command, {shell:true})` 或 `spawn(node, [acpEntry])`
- `agent/src/runtime/adapter-selection.ts`: 4 个 adapter 注册
- `AgentRuntimeProcess.swift`: node 启动 bridge,`applyLocalAgentEnvironment` 注入 PATH/HOME

### 2. Memory Bank 连接(真调本机 CLI)
`desktop/macos/Desktop/Sources/MemoryBankConnector.swift` **真的执行本机 CLI**:

```swift
// connectCodex: 检测本机 codex → 执行注册 MCP
let cliPath = executablePath(named: "codex")   // command -v codex
runProcess(executable: cliPath, arguments: [
  "mcp", "add", "omi-memory", "--",
  "npx", "-y", "mcp-remote", mcpURL,
  "--header", "Authorization: Bearer \(key)",
])
```

- 检测:`command -v codex`(MemoryExportConnectionDetector 还看 `~/.codex/config.toml`)
- 动作:`codex mcp add omi-memory` → 把 Omi 记忆注册为 Codex 的 MCP server
- 同样处理 Claude Code(`~/.claude.json mcpServers`)、OpenClaw、Hermes

**结论**:desktop 调用本机 Claude Code/Codex 的真实动作是 **Memory Export(记忆注入)**,不是"让 codex 执行任务"。AI 聊天本身用内置 SDK。

## 二、手机说话能否让 codex 干活?

### 当前架构(两条隔离链路)
```
手机语音 → 转录(STT) → memory/chat
                │
                ├── chat → agent-proxy(WSS) → VM agent(GCE, 远程)
                │         ↑ 移动端 agent 走远程 VM,与 desktop 无关
                │
                └── (desktop 侧) 记忆 → MemoryBankConnector → 本机 codex MCP 注册
```

### 结论:当前**不能**直接用手机语音触发本机 codex 干活
1. **移动端 agent** 走 `agent-proxy` → **VM agent**(GCE 远程),完全不经过 desktop
2. **desktop 本地 agent**(ACP/pi-mono/hermes/openclaw)只有 desktop app 本地触发
3. 手机语音 → 记忆 → 只是**注入**到本机 codex 的 MCP(被动供查询),不是主动让 codex 干活

### 可行的接入点(若要打通"手机说话 → codex 干活")
| 方案 | 改动 | 复杂度 |
|---|---|---|
| **A. desktop 桥接** | desktop app 暴露本地 agent 为 WebSocket 服务,移动端连它 | 中:desktop 加 WS server + 认证 |
| **B. 后端转发** | 后端新增"desktop 中继"端点,把移动端 agent 请求转发到 desktop 的 agent bridge | 中:desktop 主动连后端长连接 |
| **C. 本机 codex 触发** | MemoryBankConnector 扩展:`codex exec "..."` 执行一次性任务 | 低:复用现有 `runProcess(codex)` 机制 |
| **D. agent-proxy 指向本地** | 移动端 agent proxy 改连 desktop 的本地 agent 而非 VM | 高:重构 agent-proxy 查找逻辑 |

**推荐 C 起步**:desktop 已有 `executablePath(named: "codex")` + `runProcess`,只需加一个"执行 codex 命令"的入口。配合手机语音 → 后端 → desktop 通知,即可实现"手机说话让 codex 干活"。

## 三、关键文件索引
- `desktop/macos/agent/src/adapters/acp.ts` — ACP(Claude Code)子进程
- `desktop/macos/agent/src/adapters/pi-mono.ts` — pi-mono SDK
- `desktop/macos/agent/src/runtime/adapter-selection.ts` — adapter 注册
- `desktop/macos/Desktop/Sources/Chat/AgentRuntimeProcess.swift` — node bridge 启动
- `desktop/macos/Desktop/Sources/MemoryBankConnector.swift` — 本机 codex/claude CLI 调用
- `desktop/macos/Desktop/Sources/MemoryExportConnectionDetector.swift` — 本机配置探测
- `backend/agent-proxy/main.py` — 移动端 → VM agent
- `app/lib/services/agent_chat_service.dart` — 移动端 agent chat(WSS 到 agent-proxy)
