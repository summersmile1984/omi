# 回推上游的 PR 队列

> 目的：每被上游接受一个可配置化 PR，就从 `upstream-touch-allowlist.yaml` 删一条 T1 白名单、fork 的长期冲突面减少一块。
> 原则：这些 PR 对上游是**无害的可配置化重构**，不带 fork 的品牌、部署或商业意图；默认值必须与上游现状**逐字节等价**，并附等价性测试。

| # | 主题 | 上游文件 | 提议形态 | 消除的白名单条目 | 状态 |
|---|---|---|---|---|---|
| 1 | Info.plist 驱动的生产族标识 | `desktop/macos/Desktop/Sources/AppBuild.swift` | 生产族 bundle id 从 Info.plist 键读取，缺省回落到现有常量 | 1 | 待提 |
| 2 | 后端端点来自 bundle 配置 | `DesktopBackendEnvironment.swift` | 四个 URL 常量改为可被 bundle 配置覆盖（生产族仍拒绝进程 env 覆盖） | 2 | 待提 |
| 3 | 身份提供方接缝 | `AuthService.swift`、`backend/utils/other/endpoints.py` | 认证实现改为可注册的 provider，默认 Firebase | 3（及后端未来可能的注入） | 待提 |
| 4 | 命名 bundle 前缀可配置 | `desktop/macos/scripts/app-config.sh` | 前缀从配置读取，默认 `omi-` | 4 | 待提 |
| 5 | ARB `{appName}` 参数化 | `app/lib/l10n/app_*.arb`（190 键） | 品牌词改为占位符，默认值 `Omi`；对上游是纯文案重构 | 5 | 待提（接受后删除运行时委托） |
| 6 | 可插拔认证提供方 / Workers 构建 | `web/app/next.config.js`、`src/lib/firebase.ts` | 认证提供方开关 + 条件别名钩子 | 6、7 | 待提 |
| 7 | Kconfig 驱动的 NFC 配对 URL | `omi/firmware/omi/src/lib/core/nfc.c` | URL 从 Kconfig 读取，默认值为现有字面量 | 8 | 待提 |
| 8 | prompt `{product_name}` 参数化 | `backend/utils/llm/**`、`llm_gateway/gateway/executor.py` | prompt 中的产品名改为模板变量，默认 `Omi`；顺带合并三份重复的 chat system prompt | 无（fork 侧用导入时补丁，接受后删补丁） | 待提 |
| 9 | STT/TTS/翻译 provider 注册表 | `backend/config/stt_provider_policy.py`、`utils/stt/streaming.py`、`utils/tts_provider.py` | provider 改为注册表 + 入口点，第三方可注册而不改上游文件 | 无（同上） | 待提 |
| 10 | 检查清单支持多文件 | `.github/scripts/run_checks.py`、`pr_preflight.py` | 支持 `include:` 或多 `--manifest`，让下游追加检查而不改上游清单 | 无（C7） | 待提 |
| 11 | 文档引用检查支持额外文件 | `.github/scripts/check_agent_doc_references.py` | `--extra` 参数 | 无（C7） | 待提 |
| 12 | 部署设置分类支持额外文件 | `.github/scripts/check_deployment_secret_boundary.py` | `--extra` 参数 | 无（C7） | 待提 |

## 提交约定

- 一个 PR 一件事，附：动机（可配置化，不提 fork）、默认值等价性测试、`make preflight` 结果。
- 被拒绝的记录理由，白名单条目保留并在 `reason` 里注明"上游已拒绝 + 日期"。
- 每季度复审本表；`00-upstream-touch-policy.md` §6 的度量应随接受数单调下降。
