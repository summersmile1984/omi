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

## 四、codex / claude code 的 project + conversation 概念(2026-08-10 补充)

### Claude Code(本机已装 2.1.202)
- **project 目录**: `~/.claude/projects/-Users-<path>-<project>/`(每个项目一个目录)
- **conversation 文件**: 每个目录下 `*.jsonl`(文件名为会话 UUID)
- **jsonl 结构**: `summary`(会话摘要)+ `message`(user/assistant 消息)
- **恢复**: `claude --resume <UUID>` 或 `-c`(继续最近)
- **选择能力**: 可解析所有 project 目录 + 每个 conversation 的 summary,列出"哪个项目里有什么对话"

### Codex(本机已装 0.145.0)
- **session 存储**: `~/.codex/sessions/YYYY/MM/DD/*.jsonl`
- **session_meta**: 含 `cwd`(工作目录)+ `id`(UUID)+ `originator`
- **project 配置**: `~/.codex/config.toml` 的 `[projects."<path>"]`(per-project 权限/模型)
- **恢复**: `codex exec resume <SESSION_ID>`(按 UUID 或 thread 名)
- **选择能力**: 可扫描 sessions + config.toml projects,列出 project → session

### 能否用语音选择 project + conversation 对话操作?

**能,机制已具备**:
1. **枚举**: 读 `~/.claude/projects/*/` + `~/.codex/sessions/**/*.jsonl` + config.toml projects → 得到 project 列表
2. **选择**: 语音说"在 memweft 项目的那个对话里...",LLM 解析 → 匹配 project 路径 + conversation(UUID/summary)
3. **对话操作**: `claude --resume <UUID> -p "<prompt>"` 或 `codex exec resume <SESSION_ID> "<prompt>"`(非交互发送消息)
4. **接 Omi**: 语音 → STT → LLM 解析意图 → 调用上述 CLI → 结果回传 TTS

**唯一要注意**: claude 的 conversation 按"目录路径"归属 project,codex 按 cwd 归属——语音选择需要把"项目名"映射到路径(可用 config.toml projects 或项目名匹配)。
