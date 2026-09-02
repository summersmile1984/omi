# 04 · 白牌层实施（brand/ 清单 → 生成物 → 守卫）

> 依据：`omi-white-label-strategy.md`（逐文件触点清单）。本文只讲"怎么落地"：清单 schema、生成器与守卫的规范、注入点改动、PR 拆分与验收。品牌维度与部署目标维度正交：一个品牌可以同时构建 self-host 与 Cloudflare 两套服务端；CI 矩阵见 `05-ci-matrix.md`。

## 1. 目录与文件

```
brand/
├── README.md
├── _schema/manifest.schema.json        # JSON Schema，apply/check 共用
├── omi-upstream/manifest.yaml          # 上游品牌的等价清单（用于回归：apply 后与上游零 diff）
└── <brand>/                            # 每品牌一个目录（可放私有 overlay repo，见 §7）
    ├── manifest.yaml
    ├── assets/{icon-master.png, logo-light.svg, logo-dark.svg, splash.png, menu-bar-icon.svg, notch-logo.svg, device/*.png}
    ├── fonts/{*.ttf|otf, LICENSE.txt}   # 仅 OFL/自有授权字体
    ├── legal/{privacy.zh.md, privacy.en.md, terms.zh.md, terms.en.md}
    ├── prompts/persona.yaml            # AI 自称、口吻、禁语、平台提示句
    └── l10n/brand_*.arb                # 含品牌词的 ARB 覆盖层（由模板生成，可人工微调）
scripts/brand/
├── apply.py        # 渲染全部生成物；幂等；--brand <id> [--only flutter|desktop|backend|firmware|web|docs|ci]
├── check.py        # 守卫：泄漏扫描 + 生成物一致性 + 标识符一致性；--brand <id> --base upstream/main
├── templates/      # Jinja2，一处一文件（下表"生成物"列一一对应）
└── lexicon.yaml    # 上游品牌词表：Omi, omi.me, Based Hardware, basedhardware.com, Friend(设备语境), friend.based.com, omiapi.com, discord.omi.me …
```

## 2. `manifest.yaml` schema（v1，字段即注入点）

```yaml
schema_version: 1
brand:
  id: memweft                         # [a-z0-9-]，目录名、生成文件后缀
  display_name: "Memweft"             # 应用显示名、菜单栏、TCC 文案、通知标题
  short_name: "Memweft"               # 空间受限处（Watch、Widget）
  legal_entity: "某某科技有限公司"
  support_email: support@example.com
  ai_persona_name: "Memweft"          # prompt 中的 {product_name}
  copyright_holder: "某某科技有限公司"   # 与 MIT 原声明并列，不替换
domains:
  api_base: https://api.example.com/           # Env.apiBaseUrl / BASE_API_URL / DesktopBackendEnvironment
  web_app: https://app.example.com
  share_base: https://s.example.com            # OMI_SHARE_BASE_URL、深链 applinks
  docs: https://docs.example.com
  help: https://help.example.com
  feedback: https://feedback.example.com
  privacy: https://www.example.com/privacy
  terms: https://www.example.com/terms
  status: https://status.example.com
  community: https://…                          # 替代 discord.omi.me；可为空 → 隐藏入口
identifiers:
  ios_bundle_id: com.example.memweft
  ios_bundle_id_dev: com.example.memweft.dev
  android_application_id: com.example.memweft
  android_application_id_dev: com.example.memweft.dev
  app_group: group.com.example.memweft
  associated_domains: ["applinks:s.example.com"]
  url_scheme: memweft                          # prod；dev/beta 由 apply 派生 memweft-dev / memweft-beta
  macos_bundle_id: com.example.memweft.desktop
  macos_bundle_id_beta: com.example.memweft.desktop.beta
  macos_bundle_id_dev: com.example.memweft.desktop-dev
  macos_named_bundle_prefix: com.example.memweft.   # 替代 com.omi.；命名 bundle 名前缀改为 "<id>-"
  macos_binary_name: "Memweft"
  windows_app_id: com.example.memweft.windows
  windows_product_name: "Memweft for Windows"
  apple_team_id: ABCDE12345
  app_store_id: "0000000000"
  keychain_service_prefix: com.example.memweft
theme:
  accent: "#1F6FEB"
  forbidden_hue_families: ["purple"]            # 生成 check_brand_ui 的 ratchet 配置；可为空
  fonts: { sans: "Geist", mono: "Geist Mono", license: "OFL-1.1" }
assets: { icon_master: assets/icon-master.png, logo_light: assets/logo-light.svg, logo_dark: assets/logo-dark.svg, splash: assets/splash.png }
device:
  ble_name: "Memweft"
  ble_name_devkit: "Memweft DevKit"
  dis_manufacturer: "某某科技"
  dis_model_cv1: "Memweft CV 1"                 # 同时生成 backend firmware.py 的型号→通道映射
  firmware_release_prefix: "Memweft_CV1"        # 标签 <prefix>_v<ver>，资产 <prefix>_OTA_v<ver>.zip
  service_uuid_base: "5f2c0000-9e21-4b7a-8c3d-3a1f0d6e7b90"   # 留空 = 沿用上游 19b10000-…（互通）
  nfc_pair_url: https://s.example.com/pair
  mcuboot_signing_key: env:MCUBOOT_SIGNING_KEY_PEM        # 只允许 env:/secret: 引用，禁止路径
distribution:
  sparkle_feed_url: https://api.example.com/v2/desktop/appcast.xml
  sparkle_public_ed_key: "BASE64…"
  github_releases_repo: example/memweft
  macos_artifacts: { zip: "Memweft.zip", dmg: "memweft.dmg", beta_zip: "Memweft.Beta.zip", beta_dmg: "memweft-beta.dmg" }
  windows_artifact: "Memweft-Setup-${version}.exe"
analytics:
  posthog: { host: https://us.i.posthog.com, key: env:POSTHOG_API_KEY }   # 可为空 → 关闭
  sentry_dsn: env:SENTRY_DSN
plans:
  display_names: { basic: "免费版", plus: "Plus", unlimited: "无限版", operator: "团队版", architect: "专业版" }  # 枚举值不变
marketplace: { enabled: false }                # false → 隐藏 Apps/Personas 入口，仅内置集成
mcp: { package_name: memweft-mcp, server_name: memweft-mcp-server }
api: { key_prefix: "mw_", header_prefix: "X-Mw-" }   # header 属于两端契约组，见 §5
```

校验规则（schema + check.py）：所有 URL 为 https；`*_key`/`dsn`/`signing_key` 只允许 `env:`/`secret:` 引用；`service_uuid_base` 为空或合法 UUID 且末段与上游不同；`identifiers.*` 反域名合法；`display_name` 不含上游词表。

## 3. 生成物一览（每平台一处注入点）

| 平台 | 模板 → 生成物 | 注入方式 | 备注 |
|---|---|---|---|
| Flutter | `flavorizr.yaml`；`app/lib/brand/brand.g.dart`（displayName/urls/supportEmail/planNames/colors）；`app/lib/l10n/brand_<locale>.arb` 覆盖层；`app/ios/Flutter/Brand.xcconfig`（`BUNDLE_*`、`AUTH_CALLBACK_SCHEME`、`APP_GROUP`、TEAM）；`app/ios/Runner/Runner*.entitlements`（App Group、applinks）；`android/app/src/<flavor>/res/values/brand.xml`；图标全套；splash 配置 | `gen-l10n` 读取 ARB 覆盖层；`Env` 不变；`environment_profile.dart` 的 profile 表由 `02-deployment-profile.md` 生成 | `pubspec.yaml name: omi` 不变 |
| macOS | `desktop/macos/Desktop/Sources/Brand.generated.swift`（`Brand.displayName/bundleIds/reverseDomain/urls/sparkle`）；`Info.plist` 由模板渲染（显示名、图标名、7 条 TCC 文案、URL scheme、`SUFeedURL`、`SUPublicEDKey`）；`scripts/app-config.brand.sh`（供 `app-config.sh` source）；资源文件替换（保留文件名） | `AppBuild.swift` 改读 `Brand.*`；`app-config.sh` 前缀校验改读 `macos_named_bundle_prefix` | `Package.swift` 可执行名改读环境变量 `BINARY_NAME`（Codemagic 已有该变量） |
| Windows | `desktop/windows/brand.generated.json` → `electron-builder.config.mjs` 读取；`resources/icon.*` 替换 | 现有 `VITE_*` env 不变 | 代码签名证书为运营项 |
| 后端 | `backend/.env.brand`（`PRODUCT_NAME`、`SUPPORT_EMAIL`、`PUBLIC_WEB_URL`、`DOCS_URL`、`SHARE_BASE_URL`、`API_KEY_PREFIX`、`PLAN_NAMES_JSON`、`FIRMWARE_RELEASE_PREFIX`、`FIRMWARE_MODEL_MAP_JSON`、`ARTIFACT_NAMES_JSON`、`GITHUB_RELEASES_REPO`）；`backend/brand.py` 读 env 并提供常量与 `{product_name}` 模板变量 | `env_loader.py` 追加加载 `.env.brand`（一行） | prompt/模板/OpenAPI/通知改读 `brand.*`（一次性代码改动，见 §4） |
| 固件 | `omi/firmware/brand.conf`（`CONFIG_BT_DEVICE_NAME`、`CONFIG_BT_DIS_MODEL/MANUF`）；`omi/firmware/omi/src/lib/core/brand_generated.h`（NFC URL、可选 UUID 基址） | `west build … -DEXTRA_CONF_FILE=brand.conf`；`transport.c` 的 UUID 常量改为 include 生成头 | 签名密钥路径由 CI 从 secret 写入临时文件 |
| Web | `web/app/src/brand.generated.ts`、`web/frontend/...`、`web/personas-open-source/...`（title/OG/logo/links/analytics）；`config/public-build-values.json` 整文件生成（Firebase 段按部署 profile 可为空） | 站点 `layout.tsx`/Footer/Sidebar 改读 `brand` | `NEXT_PUBLIC_EXTRA_PROMPT_RULES` 由 `prompts/persona.yaml` 生成或为空 |
| 文档 | `docs/docs.json`（name/logo/favicon/colors/footer/navbar）；OpenAPI 三份由 `export_openapi.py` 读 `brand.py` 生成 | — | 页面正文的品牌词由 `check.py` 报告，人工按首发页面集处理 |
| CI | `codemagic.brand.yaml` 片段（`APP_NAME`/`BUNDLE_ID`/`BINARY_NAME`/`GITHUB_REPO`/`APP_ID`/`PACKAGE_NAME`）或 `fork-*.yml` 的 env 段 | 见 `05-ci-matrix.md` | — |

`apply.py` 幂等：第二次运行零 diff；`--check-clean` 模式在 CI 里断言生成物与 manifest 一致。

## 4. 一次性代码改动（让注入点存在），按 PR 拆分

先说约束（`00-upstream-touch-policy.md`）：下表里凡涉及上游文件的改动，优先走 T0（生成文件、包/模块别名、运行时委托、导入时补丁），无法 T0 的进 T1 白名单并同时提上游 PR。特别是：后端 prompt 里的品牌词首选向上游提 `{product_name}` 参数化 PR，在被接受之前由 `backend/fork/patches/brand.py` 在导入时替换上游 prompt 模块中的常量，`check.py` 以运行时导出的 prompt 文本为扫描对象；Flutter 的 190 个 ARB 键同理（运行时 `LocalizationsDelegate` 委托 + 上游 `{appName}` PR），不直接改 ARB。

| PR | 范围 | 关键文件 | 验收 |
|---|---|---|---|
| B0 | `brand/` 骨架 + schema + `omi-upstream/manifest.yaml` + `apply.py`/`check.py` 空实现 + `lexicon.yaml` + `checks-manifest.fork.yaml` 登记 | 新增路径 | `apply.py --brand omi-upstream` 后 `git diff` 为空；`check.py --brand omi-upstream` 报告当前泄漏数作为基线 |
| B1 | 桌面硬门槛与身份常量：`app-config.sh` 前缀校验改读配置；`AppBuild.swift` 生产家族/`isNonProduction` 改读 `Brand`；`Info.plist` 模板化；`create-omi-beta-variant.sh` 参数化；`BundleEnvironment.swift` Firebase 覆盖限制改为按 profile；`desktop_previews.py` 校验读配置 | `desktop/macos/scripts/app-config.sh:29-40`、`Desktop/Sources/AppBuild.swift:6-40,95,184`、`Desktop/Info.plist`、`BundleEnvironment.swift:9-14`、`backend/database/desktop_previews.py:28,130` | 用 `brand/test-brand` 构建命名 bundle 成功且被判定为非生产；`AppBuildBetaIdentityTests` 改为读 manifest 后通过 |
| B2 | 移动端身份：`flavorizr.yaml` 生成 + 重跑；`flavors.dart` 读常量；3 处 pbxproj 逃逸；`BatteryWidget-Info.plist`；entitlements App Group/applinks 模板化；`Info.plist` 权限描述；`environment_profile.dart` 校验表改读生成表 | `app/flavorizr.yaml`、`lib/flavors.dart:24-29`、`ios/Runner.xcodeproj/project.pbxproj:1073,1185,1471`、8 个 entitlements、`lib/env/environment_profile.dart` | test-brand 的 iOS/Android 构建通过；深链 AASA/assetlinks 校验脚本通过 |
| B3 | 移动端文案：190 个 ARB 键改 `{appName}` 占位（脚本机械替换 + 变格语言人工复核清单）；约 15 键重写；556 处 Dart 硬编码中用户可见部分改读 `Brand.displayName`（通知渠道名、前台通知、付费墙、启动失败页、导出文件名、Apple 提醒事项 "From …"） | `app/lib/l10n/*.arb`、`notification_channel_strings.dart`、`foreground.dart:156`、`device_provider.dart`、`message_provider.dart`、`startup_failure_app.dart:34`、`audio_player_utils.dart:209`、`AppleRemindersService.swift:76` | `check.py` 在 `app/` 用户可见面零泄漏；49 语言 `gen-l10n` 通过 |
| B4 | 后端身份：`brand.py`；约 30 处 prompt `{product_name}` 插值并合并三份重复 system prompt；通知/HTML 模板/OpenAPI/导出名/API key 前缀/UA；`share_links.py:38` 去硬并入；`firmware.py` 型号映射与标签前缀读配置；`updates.py` 制品名读配置 | `utils/llm/chat.py:66,124-183,313-342,453-464,805,902`、`observability/langsmith_prompts.py:232`、`llm_gateway/gateway/executor.py:46`、`utils/llm/notifications.py`、`utils/notifications.py:34,361`、`utils/other/notifications.py:223`、`templates/*.html`、`scripts/export_openapi.py:278-319`、`routers/firmware.py:39-108`、`routers/updates.py`、`utils/share_links.py`、`database/mcp_api_key.py:300`、`dev_api_key.py:202` | prompt eval 固定用例（"你是谁/谁做的你"）只出现品牌名；`check.py` 后端零泄漏；OpenAPI 输出标题为品牌名 |
| B5 | 桌面文案与 prompt：49 个 Swift 文件改读 `Brand.displayName`（顺手抽 String Catalog）；9 处 prompt + 4 条 Vitest 断言；31 个 `com.omi.*` 反域名集中到 `Brand.reverseDomain`；资源内容替换；Windows `electron-builder` 读 `brand.generated.json` | `Desktop/Sources/**`、`Chat/ChatPrompts.swift:21,165,396`、`RealtimeHubTools.swift:53,249`、`agent/src/runtime/context-snapshot.ts:558`、`windows/src/main/agentKernel/desktopChatPrompt.ts:43`、`windows/electron-builder.config.mjs` | `check.py` 桌面零泄漏；`PermissionsPage` 文案与 `CFBundleDisplayName` 一致的单测 |
| B6 | Web 与文档：四站点 `brand.generated.ts` 接入；`Dockerfile.datadog` 上游地址移除；`public-build-values.json` 生成；`docs.json` 生成；首发文档页面集重写 | `web/*/src/app/layout.tsx`、`Footer.tsx`、`Sidebar.tsx`、`google-analytics.tsx`、`web/frontend/Dockerfile.datadog:44`、`docs/docs.json` | `check.py` Web/文档零泄漏；`public-build-config-preflight` 通过 |
| B7 | 固件：`brand.conf` + 生成头；`nfc.c` URL；可选 UUID 基址；新 MCUboot 密钥（CI secret）；删除 `FLASH_3.0.8/` 预编译件；`firmware_release.yml` → `fork-firmware-release.yml` | `omi/firmware/omi/omi.conf:107-109`、`src/lib/core/nfc.c:94`、`transport.c:136-236`、`sysbuild.conf:8`、`bootloader/mcuboot/*.pem`（删除） | 新密钥签名 OTA 成功、上游镜像被拒；App 扫描到新广播名 |
| B8 | 治理：`INV-UI-1` 改为读 `theme.forbidden_hue_families` 的 ratchet；`INV-BETA-1` 守卫改读 manifest；`.impeccable.md`/`design-qa.md` fork 版本；`AGENTS.fork.md` 写入"内部标识符不改清单" | `.github/scripts/check_brand_ui.py`、`docs/product/invariants/{brand-ui,desktop-beta-identity}.md`、5 套守卫测试 | 全部守卫在 test-brand 下通过 |

顺序：B0 → B1/B2（并行）→ B3/B4/B5（并行）→ B6/B7 → B8。每个 PR 都以 `check.py` 泄漏数下降为验收证据；B0 之后每个 PR 描述里贴前后数字。

## 5. 契约组（两端同一发布窗口同改）

以下项属于客户端与后端共享契约，不能单侧改：`X-Omi-*` 7 个响应头 + `X-Omi-Sync-Capture-Manifest` 请求头（→ `api.header_prefix`）；API key 前缀（→ `api.key_prefix`）；桌面制品名与 appcast（→ `distribution.*`）；DIS 型号串 ↔ `firmware.py` 映射（→ `device.dis_model_cv1`）；固件标签前缀 ↔ `FIRMWARE_TAG_PATTERN`。新品牌没有存量用户，不需要兼容层；但**上游品牌回归清单**（`brand/omi-upstream`）必须仍能生成与上游一致的值，保证 `apply.py --brand omi-upstream` 零 diff。

## 6. `check.py` 规范

- **输入**：`--brand <id>`、`--base <ref>`（默认 `upstream/main`，用于识别"上游文件"）、`--surfaces` 默认全部。
- **面 1 泄漏扫描**：对用户可见面按词表扫描：ARB 值、Swift `Text(`/`Button(`/`Label(`/`.alert`/`.help` 字面量、`Info.plist` 字符串、prompt 模板（`backend/utils/llm/**`、`llm_gateway/**`、`desktop/**/Chat*`、`*Prompt*.ts`）、通知模板、`templates/*.html`、OpenAPI 输出、`docs.json`、商店元数据目录、固件 `brand.conf`/`nfc.c`、`config/public-build-values.json`、`Dockerfile*` 中的 URL。允许列表：`brand/_allow.yaml`（如历史 changelog、第三方设备名 "Friend Pendant" 作为竞品识别）。
- **面 2 一致性**：`apply.py --check-clean` 零 diff；`identifiers.*` 与 `flavorizr.yaml`/xcconfig/entitlements/`Info.plist`/`electron-builder`/`Brand.generated.swift` 一致。
- **面 3 凭据**：构建输出与源码中不得含上游 Firebase 项目 id、上游 API key 前缀、`api.omi.me`/`api.omiapi.com`/`*.a.run.app` 上游地址（含 `||` 兜底默认值）、上游 PostHog/Sentry/Mixpanel/GA/Shorebird/Codemagic id。
- **输出**：按面统计的泄漏数 + 文件行；非零即失败（B0 阶段用 `--baseline` 记录当前数并只禁止增加，即 ratchet）。

## 7. 私有 overlay（可选）

若品牌资产/法律文本/密钥引用不宜进公开 fork：把 `brand/<brand>/` 放到私有仓库，CI 以 `actions/checkout` 第二仓库或 git submodule 方式挂载到 `brand/<brand>/`；`brand/.gitignore` 忽略除 `_schema/`、`omi-upstream/`、`README.md` 外的目录。`apply.py` 行为不变。

## 8. 验收（白牌层 Definition of Done）

1. `apply.py --brand omi-upstream` 零 diff（上游等价性）；`apply.py --brand <brand>` 后三端 + Web + 后端 + 固件全部构建通过。
2. `check.py --brand <brand>` 三个面全部为零。
3. 全新安装 E2E（iOS/Android/macOS/Windows/Web）流程中无上游品牌词；AI 自我介绍 eval 通过；BLE 广播名/DIS/NFC 为品牌值；OTA 新密钥通过。
4. 开源许可页含 MIT 原文与 Based Hardware Contributors 版权行、Nordic 5-Clause、Opus BSD-3、Parakeet CC-BY-4.0、OFL 字体许可。
5. 一次 `upstream/main` 合并后重跑 `apply.py` + `check.py` 仍为零（可持续性）。
