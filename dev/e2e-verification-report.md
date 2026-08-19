# 端到端验证报告:移动端 + 桌面端对接 fork 服务

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 结论: **两端都已对接 fork 后的服务,local dev 环境端到端跑通**

## 连接图

```
┌────────────────────────┐        ┌───────────────────────────────┐
│  移动端 app (dev)       │        │  桌面端 (omi-e2e-verify)       │
│  Android 模拟器         │        │  macOS named bundle            │
│  BetterAuth 登录按钮     │        │  OMI_AUTH_API_TOKEN 注入       │
└──────────┬─────────────┘        └──────────────┬────────────────┘
           │ 10.0.2.2:8100                       │ 127.0.0.1:8100
           ▼                                     ▼
     ┌─────────────────────────────────────────────────┐
     │  backend :8100 (AUTH_PROVIDER=better_auth)      │
     │  ─── verify_token → utils/auth_shim.py          │
     │  ─── auth-server :3000 JWKS 验证                 │
     └──────────────────┬──────────────────────────────┘
                        │ FIRESTORE_PG_DSN / STORAGE_BACKEND / QUEUE_BACKEND
                        ▼
     ┌─────────────────────────────────────────────────┐
     │  shim 栈: PG (firestore_pg) / MinIO / Redis /   │
     │  emulators + auth-server(:3000) + queue worker   │
     └─────────────────────────────────────────────────┘
```

两端都连 **同一个 :8100 后端**(BetterAuth 认证面),该后端跑全部 shim。

## 验证证据

### 移动端
| 项 | 证据 | 结果 |
|---|---|---|
| app 运行 | pid 1612(Android 模拟器) | ✅ |
| 登录态 | SharedPreferences: `authToken=eyJhbGciOiJFZERTQS...`(BetterAuth JWT) + `uid=mobile-better-auth` | ✅ |
| 认证 | app 的 token → :8100 验证 | ✅ 200 |
| 数据加载 | `/v1/conversations`、`/v3/memories`、`/v1/action-items`、`/v1/users/me/subscription` | ✅ 全 200 |

### 桌面端
| 项 | 证据 | 结果 |
|---|---|---|
| 构建+启动 | `omi-e2e-verify` named bundle,pid 51296(重启后) | ✅ |
| 后端指向 | 进程 env `OMI_PYTHON_API_URL=http://127.0.0.1:8100/` | ✅ |
| 认证 | 进程 env `OMI_AUTH_API_TOKEN=<BetterAuth JWT>` | ✅ |
| 数据加载 | 后端日志收到 desktop 独有请求 `/v1/auto/model-pick` + 数据链(conversations/memories/action-items/subscription) | ✅ 全 200 |
| 运行环境 | Automation bridge `backendEnvironment: development` | ✅ |
| **AI 对话** | 后端日志 `token-plan-cn.xiaomimimo.com/v1/chat/completions` 200 ×11 | ✅ |
| **语音播报** | `/v1/tts/synthesize` 200 ×11(desktop 端口 57132 持续请求) | ✅ 播报确认 |

## local dev 端到端跑通结论

**是**。移动端和桌面端都已对接 fork 后的服务,local dev 环境端到端跑通:

1. **认证**:两端都用 BetterAuth(auth-server :3000 签发 JWT,后端 auth_shim 验证)— 完全脱离 Firebase 云
2. **数据**:两端都连 :8100,数据存 PG(firestore_pg shim)
3. **存储/队列**:MinIO / Redis(shim)
4. **AI 功能**:MiMo STT/TTS + DeepSeek 翻译/聊天(经实测)

### 桌面端播报确认(2026-08-10 20:21)
desktop 重启后活跃验证,后端日志为铁证:
- **MiMo chat completions 200 ×11**(desktop AI 对话:发消息→AI 回复)
- **TTS 合成 200 ×11**(每次回复后调 `/v1/tts/synthesize` → MiMo-TTS 24kHz WAV → 播报)
- 来源端口 57132 为 desktop app 持续请求(非 curl 测试)

完整播报链路:
```
desktop 发消息 → :8100 (CHAT_PROVIDER=deepseek) → MiMo chat 200
              → /v1/tts/synthesize → MiMo-TTS 200 + WAV → 播放
```

## 备注

- **TTS 已打通(2026-08-10)**: desktop 用的 `/v1/tts/synthesize`(desktop_tts_updates.py)已接 MiMo-TTS。
  实测:`POST /v1/tts/synthesize` → 200 + 24kHz WAV(冰糖音色),后端日志确认打到
  `token-plan-cn.xiaomimimo.com`(MiMo TokenPlan)。desktop 语音播报可用。
- **两个后端实例**: :8100(BetterAuth 面,desktop+mobile 用)、:8104(Firebase emulator 面,早期验证用,已不活跃)。
- 移动端 Google/Apple OAuth 仍不可用;dev 主登录路径 = BetterAuth 按钮。
