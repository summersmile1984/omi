# Codex 调用验证台账:desktop 让 codex 开发网页

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim

## 结论

本机 codex CLI(0.145.0,已登录)**可非交互执行开发任务**。用 `codex exec` 让 codex 开发了一个
完整的 MemWeft 产品落地页(index.html,323 行/14KB)。desktop 完全具备复用此机制的调用路径。

## 验证 1:codex exec 开发网页

### 命令
```bash
cd /tmp/codex-web-test && git init
codex exec --skip-git-repo-check --sandbox workspace-write \
  "Create a single self-contained HTML file called index.html that is a beautiful
   landing page for a product called 'MemWeft' (an AI memory wearable). Include:
   hero section with headline and CTA button, 3 feature cards, a footer. Use modern
   CSS with a clean dark theme, inline styles only. Make it polished and professional."
```

### 结果
| 项 | 值 |
|---|---|
| 产物 | `/tmp/codex-web-test/index.html`(323 行,14,052 字节) |
| 内容 | `<title>MemWeft — Your life, within reach</title>`,hero + 3 功能卡 + closing CTA + footer |
| tokens | 29,700 |
| 验证 | 浏览器打开渲染正常,截图 `/tmp/codex-page.png` |

## 验证 2:desktop 复用路径分析

`desktop/macos/Desktop/Sources/MemoryBankConnector.swift`:
- `executablePath(named: "codex")`: `command -v codex` 检测本机 codex
- `runProcess(executable:arguments:environment:)`: 通用进程执行器(经 `PipeProcessRunner`)
- 现有用途: `codex mcp add omi-memory`(注册 MCP)

**可复用**:把 arguments 换成 `["exec", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt]`
即可让 desktop 触发 codex 执行任意开发任务。

**关键注意**: `processTimeoutSeconds` 默认 20s,codex 开发任务(如本次)耗时 ~2 分钟,
需加长超时(或异步执行)。

## 下一步(实现方案 C)
1. desktop 新增 `codex exec` 入口(复用 runProcess,加长超时/异步)
2. 手机语音 → 后端 → desktop 通知 → 触发 codex exec
3. 产物/进度回传(经 desktop agent bridge 或后端)
