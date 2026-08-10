# 语音选择本地 Codex/Claude Conversation 对话操作 —— 完整设计方案

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 状态: 方案(待评审)

## 一、功能定义

用户在**手机**上说话(或发文字),让**本机 desktop** 上的 Codex / Claude Code 在**指定的 project + conversation** 里执行对话操作,并把结果(文本/文件路径/状态)回传到手机。

```
手机语音 "在 memweft 项目那个对话里，帮我加一个导航栏"
  → STT → 意图解析 → 定位 project+conversation → 本机 codex/claude 执行 → 结果回传 → TTS 播报
```

## 二、核心难点与设计决策

### 难点
1. **移动端 ↔ desktop 隔离**: 移动端 agent 走 agent-proxy(远程 VM),desktop 本地 agent 无对外接口
2. **project/conversation 选择**: 需枚举本机 claude/codex 的会话,并让语音匹配
3. **上游 merge 冲突**: 这是跨三端大功能,改动面广,需最小化上游 diff

### 设计决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 通信通道 | **desktop 主动连后端 WS**(复用 omni_relay 模式),而非移动端直连 desktop | 移动端天然走后端;desktop 穿透防火墙只需出站 |
| agent 调用 | desktop 端封装 `codex exec / claude -p`(复用 MemoryBankConnector.runProcess) | 已验证可用,复用现有机制 |
| 会话枚举 | desktop 端读 `~/.claude/projects/` + `~/.codex/sessions/`,把元数据发后端 | 会话在本机,desktop 是唯一权威 |
| merge 策略 | 全部新功能放**独立目录**,上游只留 env-gated 开关 | 与既有 shim 模式一致,3 次 merge 零冲突验证 |

## 三、架构图

```
┌────────────┐ 语音   ┌────────────────────────┐
│ 移动端 app  │───────▶│ backend :8100          │
│ (手机)      │◀───────│ - /v1/agent-sessions   │  会话列表
└────────────┘  TTS   │ - /v1/agent-invoke     │  下发指令
                       └───────────┬────────────┘
                                   │ WS (outbound, 复用 omni_relay 通道)
                                   ▼
                       ┌────────────────────────┐
                       │ desktop agent-bridge   │  ← 新增独立目录
                       │ - session-enum         │  枚举本机会话
                       │ - session-invoke       │  codex exec resume / claude -r
                       │ - result-callback      │  结果回传
                       └───────────┬────────────┘
                                   │ runProcess (复用)
                                   ▼
                       ┌────────────────────────┐
                       │ 本机 Codex / Claude     │
                       │ ~/.codex/sessions      │
                       │ ~/.claude/projects     │
                       └────────────────────────┘
```

## 四、接口设计

### 后端新增(独立目录 `backend/utils/agent_remote/`)
| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/agent-remote/sessions` | GET | 列出可用的 project+conversation(desktop 上报的缓存) |
| `/v1/agent-remote/invoke` | POST | 下发指令 `{provider, sessionId, prompt}` → 经 WS 推给 desktop |
| `/v1/agent-remote/status` | GET | 查询某次 invoke 的状态/结果 |

### desktop 新增(独立目录 `desktop/macos/Desktop/AgentRemote/`)
| 组件 | 说明 |
|---|---|
| `AgentRemoteServer.swift` | WS 客户端连后端(复用 omni_relay 连接模式) |
| `SessionEnumerator.swift` | 扫描 `~/.claude/projects/` + `~/.codex/sessions/`,产出 {project, conversationId, summary} |
| `AgentInvoker.swift` | 封装 `codex exec resume <id> "<prompt>"` / `claude -r <uuid> -p "<prompt>"` |
| `AgentResultReporter.swift` | 结果(输出/产物路径/状态)回传后端 |

### 移动端新增
| 组件 | 说明 |
|---|---|
| `AgentRemoteService` | 调后端 3 个端点(复用 ApiClient) |
| 会话选择 UI | 语音或列表选择 project+conversation |

## 五、语音选择 project/conversation 流程

```
1. 手机: "在 memweft 项目里帮我做 X"
2. 后端 STT → LLM 意图解析 → 提取 {project名, 动作}
3. 后端 GET /sessions → 匹配 project(按 cwd 路径/名称)
4. 后端 POST /invoke {provider: codex, sessionId, prompt}
5. desktop AgentInvoker: codex exec resume <id> "<prompt>"
6. 结果回传 → 手机 TTS 播报 / 显示产物路径
```

**模糊选择处理**: 若 LLM 无法唯一匹配 project,返回候选列表让用户语音确认("是 memweft 还是 matrix?")。

## 六、上游 merge 冲突最小化策略(核心要求)

### 原则(与既有 shim 完全一致)
> **新功能 100% 独立目录;上游文件只加 env-gated 开关,不加业务逻辑。**

### 各端改动边界

#### Desktop(Swift,最容易冲突)
| 方案 | 上游改动 | 冲突风险 |
|---|---|---|
| 新增目录 `Desktop/AgentRemote/` | 零(新文件) | 无 |
| 注册入口(1 个文件) | `AppDelegate.swift` 或 `DesktopBackendEnvironment.swift` 加 **env-gated 启动分支** | 低(几行 env 判断) |

#### Backend(Python)
| 方案 | 上游改动 | 冲突风险 |
|---|---|---|
| `utils/agent_remote/` 独立目录 | 零 | 无 |
| 路由注册 | `main.py` 加 `if 环境变量: include_router(...)` | 低(env-gated) |
| WS 通道 | 复用现有 `omni_relay` 或新增独立 WS 路由(独立文件) | 低 |

#### Mobile(Flutter)
| 方案 | 上游改动 | 冲突风险 |
|---|---|---|
| `AgentRemoteService` 独立文件 | 零 | 无 |
| 入口 | 现有 agent chat 界面加 dev-only 入口(env-gated 或 flavor-gated) | 低 |

### 冲突最小化 checklist
1. **所有新文件**进独立目录(`Desktop/AgentRemote/`、`backend/utils/agent_remote/`、`app/lib/services/agent_remote/`)
2. **上游文件改动 ≤ 每端 2 个**,且都是:
   - env 判断分支(`if os.getenv("AGENT_REMOTE_ENABLED")` / `#if DEBUG`)
   - 或 `include_router` 一行
3. **不动上游核心**: 不碰 agent bridge 现有 adapter、不重构 omni_relay、不改 auth 流程
4. **merge 预演**: 每个 PR 前跑 `git merge-tree` 验证零冲突(已验证 3 次)
5. **回滚方案**: env 开关默认关闭,上游 merge 冲突时一键关闭即可恢复原状

### 上游 diff 预算
| 端 | 允许改的上游文件 | 预计 diff |
|---|---|---|
| desktop | `AppDelegate.swift`(启动分支) | ≤10 行 |
| backend | `main.py`(include_router) | ≤5 行 |
| mobile | agent chat 入口页 | ≤15 行 |
| **合计** | 3 个文件 | **≤30 行** |

## 七、实施顺序(里程碑)

| 阶段 | 内容 | 验证 |
|---|---|---|
| **M1 后端+desktop 直连** | backend 新增 agent-remote 路由 + desktop AgentRemoteServer(WS 连后端) | desktop 上报会话列表到后端 |
| **M2 会话枚举** | SessionEnumerator 扫描本机 claude/codex,产出列表 | 后端 /sessions 返回真实会话 |
| **M3 指令下发** | /invoke → WS → AgentInvoker → codex exec resume | 手机触发 desktop codex 执行 |
| **M4 移动端 UI** | 语音选择会话 + 结果展示/TTS | 完整语音流程跑通 |
| **M5 merge 预演** | 对上游 main 跑 merge-tree + 全测试 | 零冲突 + 无回归 |

## 八、风险与缓解

| 风险 | 缓解 |
|---|---|
| desktop 不在线 | 后端 /sessions 返回空+提示"desktop 未连接" |
| codex/claude 未登录 | /invoke 返回明确错误,提示先登录 |
| 语音匹配错会话 | 模糊匹配回退到候选列表确认 |
| 上游 merge 冲突 | env-gated + 独立目录,冲突面 ≤30 行 |
| codex 执行超时 | AgentInvoker 异步 + 结果回传状态(运行中/完成/失败) |

## 九、关键复用点(已验证)
- `codex exec resume <SESSION_ID> "<prompt>"`(实测:恢复对话继续操作成功)
- `claude --resume <UUID> -p "<prompt>"`
- `MemoryBankConnector.runProcess` + `PipeProcessRunner`(通用执行器)
- `omni_relay` WS 模式(desktop → 后端出站连接)
- 会话枚举路径:`~/.claude/projects/*/`(jsonl+summary)、`~/.codex/sessions/**/*.jsonl`(session_meta.cwd)

## 十、非目标
- 不做远程 VM agent 改造(移动端现有 agent 不动)
- 不做 desktop 反向穿透(移动端直连 desktop)——统一走后端中继
- 不做多用户多 desktop(单用户单 desktop 起步)
