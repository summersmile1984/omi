# Desktop E2E 验证工具:不需要 computer use 插件

日期: 2026-08-09 · 结论: desktop/macos 已有原生 macOS app 的完整 E2E 自动化机制,computer use 插件/浏览器自动化(ego-browser)不适用也不必要。

## 一、直接答案

**不需要装 computer use 插件/skill。** desktop 是原生 macOS SwiftUI app,ego-browser(浏览器自动化)不适用;而项目内置的 `DesktopAutomationBridge` + `omi-ctl` 已是比鼠标点击更可靠的 E2E 机制。

## 二、desktop 自带的 E2E 工具栈(代码确认)

| 工具 | 作用 | 是否需装 |
|---|---|---|
| **`DesktopAutomationBridge.swift`** | 本地 HTTP 控制桥,**自动在非 production bundle 启用** | 内置 |
| **`scripts/omi-ctl`** | 驱动桥:导航/读状态/执行语义动作 | 内置 |
| **`omi-ctl action <name>`** | **语义动作**,直接调 app 真实代码路径(如 `refresh_all_data`、`toggle_transcription`),cursor-free 不抢鼠标 | 内置 |
| **`omi-auth-dump/seed.sh`** | 注入认证,跳过 web 登录 | 内置 |
| **`desktop-core-harness.sh`** | 分层 E2E:T1(agent-local)/T2(**hermetic 验证层**)/T3(gauntlet) | 内置 |
| **`omi-harness run <flow.yaml>`** | 类型化 v2 流程,走 bridge lane | 内置 |
| agent-swift | 原生 app 探索/点击(补充) | **未装,可选** |
| cliclick | 坐标点击 | **未装,可选** |

## 三、为什么不需要 computer use

1. **语义动作优于鼠标点击**: `omi-ctl action` 直接调用 app 真实代码路径(`preferSemantic` 优先),比模拟鼠标稳定、不抢光标、可断言
2. **认证注入**: `omi-auth-seed` 预置 token,无需人工 web 登录(和 BetterAuth shim 可对接——seed 的 token 换成 BetterAuth/本地 token)
3. **T2 hermetic 层**: 用 `make dev-up`(offline)+ 桥 + 语义动作,完全本地、无真实 LLM/STT——**正好对接我们改造的 shim 后端**
4. **ego-browser 不适用**: 它是浏览器工具,desktop 原生 app 不在其内

## 四、接入我们改造后端的路径

desktop E2E 的 harness 用 `make dev-up`(offline 后端)。要让它跑在我们改造的 cloud-neutral 后端上:

```
1. dev/deploy-local.sh 起 shim 后端(:8100,PG/MinIO/Redis/BetterAuth)
2. desktop named bundle + OMI_PYTHON_API_URL=http://127.0.0.1:8100/
3. 认证: omi-auth-seed 注入 BetterAuth token(或 omi-auth-dump 从 emulator 拿)
4. desktop-core-harness.sh --tier 1/2 --bundle omi-core-e2e
   → 桥 + 语义动作验证 app 在 shim 后端上工作
```

## 五、结论

- **不需要 computer use 插件**
- desktop 内置 `omi-ctl` 桥 + 语义动作 + T2 hermetic harness 是原生 app E2E 的正解
- agent-swift/cliclick 是可选补充(坐标/无障碍),非必需
- 接入路径清晰:shim 后端 + named bundle + auth seed + harness
