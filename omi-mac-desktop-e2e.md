# Mac Desktop App 纳入端到端验证:现状 + 改动清单

日期: 2026-08-09 · 目标: 让改造版 Omi(cloud-neutral 后端,本地 8100)跑通 Mac desktop app

## 一、现状(代码确认)

| 维度 | 现状 | 对本地的意义 |
|---|---|---|
| **Python API base URL** | `DesktopBackendEnvironment.swift`: prod `api.omi.me/`,dev `api.omiapi.com/`;`pythonBaseURL()` 支持 **`OMI_PYTHON_API_URL` env 覆盖** | ✅ **无需改代码**,设 env 指向 `http://127.0.0.1:8100/` |
| **Rust backend URL** | `rustBackendURL()` 读 **`OMI_DESKTOP_API_URL`**(agent VM/Crisp/订阅) | 本地可用同一后端 |
| **认证** | `AuthService` → Firebase Auth 拿 ID token → `Authorization: Bearer` | ⚠️ **需替换为 BetterAuth/本地 token** |
| **测试认证头** | `APIClient.testAuthHeader`("test-only"):设置后 `buildHeaders` 用它,不调 AuthService | ✅ **可直接注入本地 token**,无需改认证代码 |
| **数据边界** | 所有 CRUD/chat/title 走 Python 后端;本地 SQLite 仅桌面 agent 上下文 | ✅ 后端 shim 覆盖 |
| **运行方式** | `run.sh`(named bundle)、`scripts/`(omi-ctl)、`e2e/` | ✅ 已有本地运行基建 |

**结论:Mac app 已具备指向本地后端的全部机制,主要改动在"认证 token 注入",非结构性改动。**

## 二、需改什么(按影响排序)

### 1. 认证 token 注入(必改,最小)
desktop 用 Firebase Auth 签 ID token;本地后端用 BetterAuth/emulator token。
- **方案 A(零代码改动,推荐)**: `APIClient.testAuthHeader` 已支持注入——用 e2e harness 或 env 把 BetterAuth JWT 塞进去
- **方案 B**: 保留 Firebase Auth emulator 的 token(BetterAuth shim 前的老路径)——desktop 连 emulator 拿 token,后端走 emulator 验证
- **实际需要**: 确认 `testAuthHeader` 的注入入口(当前是 test-only 属性,可能需要小改暴露给启动流程)

### 2. 后端 URL env(零代码改动)
```bash
# 启动 desktop named bundle 时:
export OMI_PYTHON_API_URL="http://127.0.0.1:8100/"
export OMI_DESKTOP_API_URL="http://127.0.0.1:8100/"
```
→ desktop 所有 CRUD 指向本地后端

### 3. named bundle 路由(零代码改动)
- 非 productionFamily bundle 自动走 dev 后端(`shouldUseDevelopmentBackends`)
- `run.sh` 已支持 named bundle 本地运行
- **注意**: 生产 bundle 拒绝 env 覆盖(设计如此),必须用 named/dev bundle 验证

### 4. 潜在差异点(需验证)
| 面 | 差异 | 影响 |
|---|---|---|
| WebSocket(transcribe/listen) | 本地 WS 端口 | 需确认 desktop 的 listen 走哪 |
| WebSocket(live STT) | SenseVoice CPU | 本地 8100 已支持 |
| MCP/agent | 需 rust backend URL | `OMI_DESKTOP_API_URL` 指向本地 |
| 推送/通知 | FCM 未替换 | 桌面不依赖,可跳过 |

## 三、验证路径(纳入端到端)

1. **准备**: `dev/deploy-local.sh` 起本地后端(8100,全 shim)
2. **启动 desktop**: named bundle + `OMI_PYTHON_API_URL=http://127.0.0.1:8100/`
3. **认证**: 注入 BetterAuth JWT(testAuthHeader)或 emulator token
4. **验证**: desktop 登录 → 首页仪表盘(记忆/会话/任务)→ 对话 chat → 设置
5. **对照**: 后端日志确认请求打到本地 shim(PG/MinIO/Redis)

## 四、结论

- **Mac app 已为本地部署预留了全部机制**(`OMI_PYTHON_API_URL` env + `testAuthHeader` + named bundle 路由)
- **真正的改动最小**:主要是把 BetterAuth/emulator token 通过 `testAuthHeader` 注入(可能需要把 test-only 属性提升为正式注入点)
- **推荐先跑通**: 用 named bundle + env 指向本地后端 + emulator token(最快),再升级到 BetterAuth token
- **结构性改动**: 无——数据边界(本地 SQLite vs 后端 REST)已清晰,所有 CRUD 走 Python 后端,正好是 shim 覆盖面
