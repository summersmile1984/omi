# Omi 白牌化完整方案（memweft fork）

> 日期：2026-09-02 · 基线：本 worktree 代码（`7f6e8ef7aa`，已多次合并 `upstream/main`）
> 方法：对 `app/`、`backend/`、`desktop/`、`omi/`、`omiGlass/`、`sdks/`、`web/`、`docs/`、`.github/`、`infrastructure/` 做全量品牌触点盘点（文件级定位），加上许可证、商店政策、无线认证与中国合规的外部核对。
> 关联文档：`omi-cloud-neutral-postgres-migration.md`（云中立）、`omi-better-auth-shim.md`（去 Firebase Auth）、`omi-deployment-resources.md`、`omi-subscription-margin.md`、`omi-gpu-services-survey.md`、`omi-translation-replacement.md`。白牌化与云中立化是**正交但共享前置决策**的两条线，见分论八。

---

## 总：结论先行

### 一句话结论

**白牌化在法律上完全可行（MIT），在工程上是"有限可枚举、但当前零抽象"的一次性改造，在商业上真正的门槛不在代码而在账号、硬件认证与合规。** 推荐路线：不做全仓库改名，而是建一个**品牌配置层（`brand/` 清单 → 各平台生成物 → CI 守卫）**，把上游品牌从"用户可感知面"彻底清除、把内部标识符原样保留，从而在白牌之后仍能持续合并上游。软件部分约 **3~4.5 人月**（两人并行 8~10 周）；硬件白牌另计 3~6 个月且以认证与模具为主。

### 白牌化到底包含什么（四层）

| 层 | 内容 | 谁能做 | 本方案覆盖 |
|---|---|---|---|
| **品牌层（代码）** | 显示名、包名/Bundle ID、图标/配色/字体、49 种语言文案、AI 人格自称、域名与链接、推送/邮件署名、蓝牙广播名与 DIS、商店与更新通道、法律文本、OpenAPI/SDK 包名 | 工程 | 分论一~六，逐文件清单 |
| **账号层** | Apple Developer（Team、Services ID、APNs、App Group、Associated Domains）、Google Play、Firebase 或自托管 Auth、Stripe、Codemagic/自有 CI、Sparkle 签名密钥、MCUboot 签名密钥、Windows 代码签名证书、PostHog/Sentry/Mixpanel/GA、Shorebird、7 个第三方集成的 OAuth 应用、npm/PyPI/crates 组织、域名与 DNS | 运营 + 工程 | 分论八 Phase 0/3 清单 |
| **硬件层** | PCB 丝印、外壳刻字、包装、BOM 内部 SKU、固件签名密钥、Bluetooth SIG Declaration ID、FCC/CE/中国无线与电池认证、ODM/MOQ | 硬件 + 供应链 | 分论五、分论七 |
| **合规层** | 应用商店"模板/白牌 App"政策、商标检索、中国 ICP/APP 备案、生成式 AI 备案/登记、PIPL 敏感个人信息（声纹）、字体与模型权重许可 | 法务 | 分论七 |

### 盘点结果：关键数字与关键发现

| 组件 | 规模 | 现有可配置度 | 一句话判断 |
|---|---|---|---|
| 移动端 Flutter | 约 15,900 处 `omi`/812 文件；`app_en.arb` 3,028 键中 **190 键含 Omi**（49 语言约 9,700 条）；**556 处** Dart 硬编码文案 | iOS xcconfig 与 Android flavor 已参数化身份；`Env` 零品牌字段；`environment_profile.dart` **在启动时校验并钉死 Omi 的 Firebase 项目与 API 域名** | 身份/端点可配置，文案与深链/App Group/CI 需一次性改动 |
| 后端 Python | 107 文件 282 处；**约 30 处 LLM prompt 让模型自称 Omi**（同一 system prompt 三份拷贝） | 凭据/端点层干净（全 env）；**身份层零接缝**，全仓库无 `PRODUCT_NAME` | 加 `brand.py` + 约 40 文件机械改动 |
| 桌面端 macOS/Windows | 745 Swift 文件中 501 含 Omi；**无任何 Localizable 文件**；9 处 prompt 自称 Omi + 4 条测试断言锁定 | `OMI_APP_NAME`/Codemagic 变量/Windows `VITE_*` 已参数化；但 **`app-config.sh` 拒绝非 `omi-` 前缀名**、`AppBuild.isNonProduction` 以 `com.omi.` 前缀判定 | 配置层可覆盖约 60%，其余是文案抽取与两条锁定不变量 |
| 固件/硬件/SDK | 5 处蓝牙广播名；DIS 型号串 `"Omi CV 1"` **决定后端 OTA 通道**；**MCUboot 私钥已提交进仓库**；10 处手工镜像的 UUID 表 | 发现逻辑按服务 UUID 而非名字 → 改名不破配对 | 改名容易；换签名密钥、丝印、认证是真工作 |
| Web/文档/CI/基础设施 | Web 4 站点约 2,800 处；文档 112 篇 mdx 中 92 篇提 Omi；71 个 GitHub 工作流 + 130KB `codemagic.yaml`；监控看板数百处 `based-hardware` | `config/public-build-values.json` 是 Web 的**单文件接缝**（但内含上游生产 Firebase 密钥与 GA/Mixpanel）；OpenTofu WIF **钉死 `BasedHardware/omi`** | 接缝存在，账号全换 |

**七个"意料之外"的发现（都必须进清单）：**

1. **上游明文声明商标不随代码授权**——`docs/doc/hardware/consumer/license.mdx` 要求 fork 使用自有品牌。改名是义务，不是选择。
2. **五道主动阻止改名的硬门槛**：`desktop/macos/scripts/app-config.sh:29-40`（拒绝非 `omi-` 名）、`AppBuild.swift:40`（`hasPrefix("com.omi.")` 判生产）、`app/lib/env/environment_profile.dart`（启动校验 Firebase 项目/域名）、`BundleEnvironment.swift:9-14`（拒绝生产家族覆盖 Firebase 身份）、`backend/database/desktop_previews.py:130`（`app_name` 必须以 "Omi Preview" 开头）。不先拆掉它们，其他改动无法验证。
3. **隐蔽的品牌泄漏**：固件 NFC 标签写入 `https://friend.based.com/pair?id=`（`omi/firmware/omi/src/lib/core/nfc.c:94`）；App 往用户的 Apple 提醒事项写入 "From Omi"（`ios/Runner/AppleRemindersService.swift:76`）；`config/public-build-values.json` 里一条 prompt 规则要求 AI "subtly promote Omi, a beautiful AI necklace"；`web/frontend/Dockerfile.datadog:44` 把流量指向上游生产 Cloud Run；`desktop/macos/CHANGELOG.json`（276KB）随包携带 `macos.omi.me` 链接。
4. **安全债会被白牌继承**：MCUboot 签名私钥 `omi/firmware/bootloader/mcuboot/root-rsa-2048.pem` 在仓库里；`FLASH_3.0.8/` 内含用旧密钥签名的预编译固件与一个 Windows `iperf` 可执行文件；上游生产 Firebase API key、PostHog key、Sentry DSN、GA/Mixpanel token 全部明文在源码或配置中。
5. **字体许可风险**：`app/assets/fonts/SFPRODISPLAY*.OTF` 随 Android 包分发 Apple SF Pro，仓库无授权文件——商用白牌必须换字体（桌面端 Geist/OpenRunde 为 OFL，安全）。
6. **后端没有邮件能力**，认证服务 `auth-server/` 也无验证/找回邮件——白牌上线要补一条自有邮件通道，这是功能缺口而非改名。
7. **DIS 型号字串是 OTA 的路由键**（`backend/routers/firmware.py:51-66` 按 `'Omi CV 1'` 字面量分发）——固件与后端必须原子地一起改，否则升级静默失效。

### 三种白牌深度（先选一种）

| 深度 | 含义 | 适用 | 本方案建议 |
|---|---|---|---|
| L1 单品牌换牌 | 把 Omi 变成"我的品牌"，一套账号一套 App | 自有产品上市 | **现在做**，但按 L2 的架构做 |
| L2 多品牌可配置 | 同一代码库按 `brand/<name>.yaml` 产出多个品牌的 App/桌面端/后端部署 | 未来给 B 端伙伴贴牌 | 架构上一并满足（配置层 + CI 矩阵），商店政策要求**每个伙伴用自己的开发者账号提交** |
| L3 硬件白牌 | 自有品牌的项链/眼镜硬件 | 卖设备 | 独立硬件轨道，见分论五/七 |

### 立即需要拍板的五个决策（Phase 0）

1. **品牌名与法律主体**：先做 USPTO/CNIPA 第 9、42 类 + 域名 + App Store 名三重检索（Omi 自己就因 "Friend" 撞名被迫改名过）。
2. **目标市场**：是否含中国大陆——决定 ICP/APP 备案主体、服务器落地、模型供应商（已备案模型走登记路径）、IAP 策略（中国区无外链豁免）。
3. **蓝牙身份是否与上游分离**：推荐**换新的 128 位服务 UUID 基址 + 新 MCUboot 密钥**（换密钥已必然打断与上游 OTA 的互通，UUID 一并分离可避免上游 App 连上你的用户设备并把音频送到上游后端）。
4. **应用市场与 Personas 市场**：首发建议**关闭市场入口、内置精选集成**，后续再决定自建。
5. **认证路线**：若按 `omi-better-auth-shim.md` 先落地自托管 Auth，则 Firebase 相关的三件配置文件、`GoogleService-Info.plist`、`environment_profile` 校验等一批白牌项直接消失——建议**认证先行，白牌配置层并行**。

---

## 分论一：总体策略——不做"全仓库查找替换"，做"品牌配置层 + 生成"

### 为什么不能直接 sed 替换

1. **这个 fork 持续合并上游**：`git log` 显示 `upstream/main` 已多次合并进 `codex/cloud-neutral-upstream-merge`。全仓库把 `Omi` 改成新名字，会让**每一次**上游合并在数千个文件上冲突——上游每周改动数百文件，白牌分支将在一个月内失去可合并性。
2. **`omi` 同时是内部标识符**：Dart 包名 `package:omi/`、Swift 模块与类型名（`OmiTheme`、`OmiButtonStyle`）、Python 环境变量前缀 `OMI_*`、Redis/Firestore 键、LLM Gateway 车道 `omi:auto:*`、信用策略 `omi_paid`、`omi-ctl` 脚本、CI 工作流 id。这些**用户永远看不到**，改它们只有成本没有收益，且是上游合并冲突的主要来源。
3. **真正需要变的是"用户可感知面"**：显示名、包名/Bundle ID、图标/启动图/配色/字体、文案与本地化、AI 人格自称、域名与链接、法律文本、推送/邮件署名、蓝牙广播名、商店与更新通道、第三方账号绑定。这些加起来是一个**有限、可枚举**的集合（见各组件清单）。

### 目标架构：单一品牌清单（Brand Manifest）→ 各平台生成物

```
brand/
├── manifest.yaml            # 唯一事实源：名称、法律实体、域名、Bundle/Package、颜色、字体、链接、AI 人格名、蓝牙名…
├── assets/
│   ├── icon-master.png      # 1024×1024 母版 → 生成 iOS/Android/macOS/Windows/Web 全套图标
│   ├── logo-{light,dark}.svg
│   ├── splash.png
│   └── fonts/               # 自有授权字体
├── legal/
│   ├── privacy-policy.{zh,en}.md
│   ├── terms.{zh,en}.md
│   └── oss-notices.md       # MIT 保留声明（自动汇总）
└── prompts/
    └── persona.yaml         # AI 自称、口吻、禁语；供后端/桌面端 prompt 模板插值
scripts/brand/
├── apply.py                 # 读取 manifest → 渲染下述全部生成物（幂等，可重复跑）
├── check.py                 # CI 守卫：生成物与 manifest 一致；用户可见面不得出现上游品牌词
└── templates/               # Jinja 模板，一处一文件
```

**生成物（每平台一处注入点，其余代码零改动）**

| 平台 | 生成物 | 现有可复用机制 |
|---|---|---|
| Flutter | `app/flavorizr.yaml`（name/applicationId/bundleId/icon）→ 跑 flavorizr；`lib/brand/brand.g.dart`（常量：displayName、supportEmail、urls、colors）；`l10n/*.arb` 中含品牌词的 key 由模板生成到 `brand_*.arb` 覆盖层；`android/app/src/<flavor>/google-services.json`、`ios/Config/<Flavor>/GoogleService-Info.plist`；`.prod.env`（`API_BASE_URL` 等） | 已有 flavor 体系 + `Env`（`.env` dart-define）+ `firebase_options_local.dart` |
| macOS/Windows | `xcconfig`/`BundleEnvironment` 覆盖（PRODUCT_NAME、PRODUCT_BUNDLE_IDENTIFIER、Team ID、Sparkle feed、`SUPublicEDKey`）；`Brand.swift`（生成常量）；`Assets.xcassets/AppIcon` 与菜单栏图标；Windows electron-builder `productName/appId/publisherName` | 已有 `OMI_APP_NAME`/`OMI_INSTANCE` 命名 bundle 机制、`config/public-build-*.json` 契约 |
| 后端 | `brand.py`（`PRODUCT_NAME`、`SUPPORT_EMAIL`、`PUBLIC_BASE_URL`、`DOCS_URL` 等，全部来自环境变量，manifest 只是生成 `.env.brand`）；prompt 模板中 `Omi` → `{product_name}` 插值；通知/邮件模板；FastAPI `title`；CORS 域名；短链域名 | 已有 `utils/env_loader.py`、`config/deployment-setting-classification.json` 分类 |
| 固件 | `brand.conf`（`CONFIG_BT_DEVICE_NAME`、DIS 厂商/型号字串）由 manifest 生成，`west build -- -DEXTRA_CONF_FILE=brand.conf` 叠加；MCUboot 签名密钥路径指向自有密钥 | Zephyr 原生 overlay 机制，不改上游 `omi.conf` |
| Web/Docs | Next.js 站点 `site.config.ts`（title、OG、logo、links）；Mintlify `docs.json`（name/logo/colors/favicon/域名）；`public-build-values.json` 换成自有 Firebase/API 值 | `config/public-build-contract.json` 已是"仓库配置驱动"的公共构建契约 |
| SDK/插件 | 包名前缀、README、默认 API URL 由模板生成；协议常量（UUID/帧格式）**不生成、不改** | `sdks/device/` 已有多语言协议定义源 |

### 上游代码里需要"一次性"改的部分（让注入点存在）

- 把硬编码 `"Omi"` 的**用户可见**字符串改为读常量/插值：Flutter 走 ARB 占位符 `{appName}`；Swift 走 `Brand.displayName`；Python prompt 走 `{product_name}`。这一类改动**建议同时提给上游**（对上游是无害的可配置化重构），合并后 fork 的长期维护成本归零——这是整个方案里最值得投资的一步。
- 上游不接受的部分，保持为 fork 的**少量、集中**补丁（一个 `brand/` 目录 + 十几处注入点），并用 `scripts/brand/check.py` 在 CI 里守住"用户可见面不出现上游品牌词"，这样每次上游合并后只要重跑 `apply.py` + `check.py` 即知是否有新泄漏。

### 内部标识符：明确"不改清单"

保持不变（用户不可见、改动只增加合并冲突）：Dart 包名 `omi`、Swift 类型/文件名、Python 模块名、`OMI_*` 环境变量、`omi-ctl`/`run.sh` 脚本名、CI 工作流 id、Helm release 名、Redis/Firestore 键名、LLM Gateway 车道名 `omi:auto:*`、信用策略 `omi_paid`、`omi-test-quality`/`omi-collection-safety` 代码注释标记、命名 bundle 前缀 `omi-`（仅开发机可见）。例外：任何会**泄漏到用户或伙伴**的内部名（URL scheme、Keychain 服务名、日志/存储目录名、导出文件名、User-Agent、OpenAPI 标题、Webhook 字段、公开 SDK 包名）归入"必改"。

### 守卫（把规则做成机械检查，与仓库现有做法一致）

- `scripts/brand/check.py` 接入 `.github/checks-manifest.yaml`（local + ci 双通道）：① 用户可见面（ARB、Swift `Text(...)`、prompt 模板、通知模板、`Info.plist`、Mintlify 配置、商店元数据）零上游品牌词；② 所有生成物与 `manifest.yaml` 一致（幂等重跑无 diff）；③ Bundle ID / 包名 / URL scheme / Associated Domains 与 manifest 一致。
- 现有 `INV-UI-1`（禁紫色）是 Omi 的品牌规则，白牌应**替换**为自有品牌色规则（同一 ratchet 脚本换调色板），而不是删除守卫机制。
- 现有 `INV-BETA-1`（Beta 独立 identity）机制保留，identity 换成自有 Bundle ID 家族。

## 分论二：移动端 Flutter（`app/`）

**规模**：`app/` 内约 15,900 处 `omi`（不含生成文件，812 个文件）；**不存在任何 Brand/AppConfig 抽象**。已参数化的只有 iOS xcconfig（`BUNDLE_NAME`/`BUNDLE_DISPLAY_NAME`/`APP_BUNDLE_IDENTIFIER`/`AUTH_CALLBACK_SCHEME`）、Android flavor 的 `applicationId` + `app_name` resValue、`envied` 生成的 `Env`（仅密钥与 API 地址，零品牌字段）、`--dart-define=OMI_API_BASE_URL`。

| 触点 | 现状（文件） | 白牌动作 |
|---|---|---|
| 显示名 / 包名 | `flavorizr.yaml`（"Omi"/"Omi Dev"；`com.friend.ios`、`com.friend-app-with-wearable.ios12`）；`lib/flavors.dart:24-29` `F.title` 硬编码；`project.pbxproj:1073,1471,1185` 三处逃逸变量；`BatteryWidget-Info.plist` "Omi Battery" | 由 manifest 生成 `flavorizr.yaml` 后重跑 flavorizr；`F.title` 改读生成常量；修 3 处 pbxproj 逃逸 |
| Dart 包名 `omi` | `pubspec.yaml:1`，3,170 条 `package:omi/` import（462/613 文件） | **不改**（用户不可见；改动 = 上游合并灾难） |
| Android namespace | 27 个 Kotlin 文件在 `com/friend/ios/`，4 个 Manifest `package=` | **只改 `applicationId`，保留 `namespace`**（Gradle 允许二者分离，Kotlin 零改动） |
| Firebase 绑定 | `setup/prebuilt/google-services.json`、`GoogleService-Info.plist`、`firebase_options.dart`（`based-hardware-dev`）；`lib/env/environment_profile.dart:11-33` **把 Firebase project 与 API 域名钉死并在启动时校验，不符即抛异常** | 新 Firebase/自托管 Auth 项目重新生成三件配置；`environment_profile.dart` 改为从 manifest 生成的 profile 表（保留"启动校验"机制，换成校验自家值） |
| 图标 / 启动图 / 图片 | 各 flavor `mipmap-*`、`prodAppIcon.appiconset/OmiAppIcon-*.png`、watch/widget imageset、`flutter_native_splash` 配置、`assets/images/` 约 30 张品牌图 + 7 张 `omi-*.png` 设备图（`lib/utils/device.dart:100+` 按型号选图） | 从 `brand/assets/icon-master.png` 批量生成；设备图按自家硬件重拍 |
| 配色 / 字体 | `responsive_helper.dart:36-39` 与 `ui_guidelines.dart:21-22` 两套 `static const` 紫色常量（无 ThemeProvider）；`assets/fonts/SFPRODISPLAY*.OTF` **随包分发 Apple SF Pro，仓库无授权文件** | 颜色常量收敛为一处并由 manifest 生成；**字体必须替换**为可再分发字体（Geist/Inter/思源等 OFL 字体） |
| 本地化文案 | 49 个语言；`app_en.arb` 3,028 键，**190 键含 "Omi"**（约 9,700 条翻译）；关键键：`appTitle`、`askOmi`、`getOmiDevice`、`welcomeToOmi`、`deviceDisconnectedNotificationTitle`（推送）、`shareStatsMessage`（含 omi.me）、`submitAppTermsAgreement`（法律主体 "Omi AI"）、`thankYouText`（含 basedhardware 邮箱） | 190 键改为 `{appName}` 占位符（专有名词在 45+ 语言中不变形，可机械替换；爱沙尼亚语等有变格的少数语言人工复核）；含域名/邮箱/法律主体的约 15 键重写 |
| Dart 硬编码文案 | **556 处 / 100 文件**：Android 通知渠道名（`notification_channel_strings.dart`，无 context 故意不走 l10n）、前台服务通知 "Your Omi Device is connected."、付费墙文案、`startup_failure_app.dart` "Omi could not start"、导出文件名 "Omi Audio Recording - …"、**写入用户 Apple 提醒事项的 "From Omi"**（`AppleRemindersService.swift:76`）、分析事件名 "Get Omi Device Clicked" | 用户可见的改读 `Brand.displayName`；分析事件名可保留（内部） |
| URL / 域名 | `env.dart:8` 默认 `api.omi.me`；`shared.dart:166` **按域名字串分支**；`share_links.dart` `h.omi.me`；约 35 处 `Uri.parse` 字面量：隐私/条款（`www.omi.me/pages/...`）、购买（`?_ref=omi_connect_device`）、充电说明、`help.omi.me`、`feedback.omi.me`、`discord.omi.me`、`docs.omi.me`、商店链接（`id6502156163`、`com.friend.ios`）；应用商店图标 CDN `raw.githubusercontent.com/BasedHardware/Omi` | 全部收进生成的 `brand.g.dart` 常量；`shared.dart` 域名分支改为比较 `Env.apiBaseUrl` host |
| 深链 / URL scheme | Android 5 条 `autoVerify` 过滤器（`h.omi.me`、`try.omi.me`）；iOS 7 个 entitlements `applinks:`；scheme `omi`/`omi-dev`/`omi-beta`/`omirayban` | 自有域名 + 托管 AASA/assetlinks；scheme 由 manifest 生成 |
| App Group | `group.com.friend-app-with-wearable.ios12` 出现在 8 个 entitlements + `SharedDefaults.swift` | **必改**（App Group ID 全局唯一，归属原 Team，新账号无法注册） |
| 蓝牙 | `services/devices/models.dart:12` 服务 UUID `19b10000-e8f2-537e-4f6c-d104768a1214`；发现逻辑按 UUID 识别 Omi、按名字识别 Bee/PLAUD/friend_/limitless；`bt_device.dart` `modelNumber='Omi'`、`manufacturerName='Friend'`；固件 OTA 走 `${Env.apiBaseUrl}v2/firmware/latest` **（已可配置）** | UUID 保留（协议）；型号/厂商显示串改读固件 DIS；第三方设备（Bee/PLAUD/Limitless）支持按产品策略决定保留或删除 |
| 支付 | 无 StoreKit/RevenueCat，订阅走后端 Stripe Checkout 的 WebView（`payment_webview_page.dart`）；套餐名部分 ARB、部分硬编码（`usage_page.dart:439` 'Plus'）；`shorebird.yaml` 热更新 app_id | **新品牌上架需重新决策 IAP 策略**：数字订阅在 App Store 内以 WebView 收款有 3.1.1 被拒风险，中国区无外链豁免；Shorebird 需自有账号 |
| CI | `codemagic.yaml` 11 个工作流，bundle id 重复约 10 次、App Store Connect 集成 `codemagic_v4`、App id `6502156163`、Team `9536L8KLMP`、Play track、keychain group | 由 manifest 生成 Codemagic 变量段（或改用自有 CI），账号全部换成自有 |
| 原生代码 | `com.omi.wifi_network` method channel、约 60 个 `dev.flutter.pigeon.omi_pigeon.*` 通道名、`omibatchphone` 标记、Android 通知渠道 id `omi_ble_channel`、iOS `Info.plist:133-147` 蓝牙/健康权限描述提到 Omi | 通道名/渠道 id **不改**（内部契约）；权限描述必改（审核可见） |

**评估**：配置层可覆盖身份、端点、密钥、图标（约 1 周）；文案（190 键 + 556 处）与 App Group/深链/CI 需要一次性代码改动（约 2~3 周含 QA）。因新品牌**没有存量用户**，所有"迁移"顾虑（SharedPreferences 键、App Group 数据、包名变更）均不存在。

## 分论三：后端 Python（`backend/`、`mcp/`、`plugins/`、`auth-server/`）

**规模**：107 个非测试 Python 文件 282 处 `Omi`。凭据与端点层**干净**（CORS 仅 env 且禁通配、OAuth client id / 供应商密钥全部 `os.getenv`、`BASE_API_URL` 驱动所有 Stripe/OAuth 回跳、`QUEUE_REDIS_PREFIX`/`OMI_SHARE_BASE_URL` 可覆盖、`auth_shim.py` 无品牌）；**身份层零接缝**——全仓库不存在 `PRODUCT_NAME`/`APP_NAME`/`BRAND` 变量，也没有 settings 对象可挂。

| 触点 | 现状 | 白牌动作 |
|---|---|---|
| **AI 人格自称**（约 30 处，全部字面量） | 主聊天 system prompt 三份拷贝（`utils/llm/chat.py:805,902`、`observability/langsmith_prompts.py:232`）；`chat.py:66`；网关默认 prompt `llm_gateway/gateway/executor.py:46`；通知 `utils/llm/notifications.py:63,127`；记忆系统 `canonical_consolidation.py:930`、`promotion_proposals.py:161`、`promotion_routes.py:55`、`conversation_prompt_prefix.py:17`；`chat.py:453-464` 四条平台提示；`app_generator.py:55`；`fair_use_classifier.py:31,42`；`typed_extraction_prompt.py` 约 10 条围绕 Omi 的 few-shot；`routers/mcp_sse.py` 工具描述 | 新增 `backend/brand.py`（`PRODUCT_NAME`、`SUPPORT_EMAIL`、`PUBLIC_WEB_URL`、`DOCS_URL`、`SHARE_HOST`…，全部 env 驱动）；prompt 常量改 `{product_name}` 插值；三份重复 prompt 合并为一处 |
| 产品问答子系统 | `chat.py:124-183` `IsAnOmiQuestion` 分类器 + `chat.py:313-342` `answer_omi_question` + `utils/retrieval/tools/omi_tools.py`（语料来自 `github.com/BasedHardware/omi/docs`） | 改为读取自家文档站语料；few-shot 以 `{product_name}` 模板化 |
| 推送 / 邮件 | 默认推送标题 `"omi"`（`utils/notifications.py:361`）、"omi says" 早间提醒、"Thanks for being part of the Omi family"；APNs topic 硬编码 iOS bundle id（`:34`）；支持邮箱 `team@basedhardware.com`（`fair_use.py`、`fair_use_admin.py`）；**后端不存在任何邮件发送能力**（无 SendGrid/SMTP） | 文案走 `brand.py`；APNs topic 读 env；补齐自有邮件通道（注册验证/找回密码/账单）是白牌上线的**功能缺口**，非改名工作 |
| 域名 / 链接 | `share_links.py:38` **无论 env 如何都把 `h.omi.me` 并入受信主机集**；`routers/apps.py:1413-1437` 返回 `docs.omi.me`；`updates.py`、`github_releases.py`、`app_integrations.py`、`models/app.py:253` 以 `github.com/BasedHardware/omi` 为发布源/文档源/图标 CDN；`updates.py:689` Discord 邀请、`:681` GCS 演示视频、"Download Omi" 页面；`export_openapi.py:286-288` OpenAPI contact/server；`templates/oauth_authenticate.html:144-145` 条款/隐私链接；深链 `omi://`（`auth.py`、`x_connector.py`、`integrations.py`、`task_integrations.py`）；`templates/auth_callback.html` 白名单 `omi://`、`omi-computer://`、`com.omi.app://` | 全部改读 `brand.py`；`share_links.py:38` 去掉硬并入；scheme 白名单由 manifest 生成 |
| 套餐与支付 | `utils/subscription.py:435+` 套餐标题硬编码（Neo/Plus/Operator/Architect），价格 id 走 env 且启动校验；`models/users.py` `PlanType` 枚举为线上契约；IAP bundle id 硬编码；`database/desktop_previews.py:28,130` **强制 `com.omi.preview.*` 且 `app_name` 必须以 "Omi Preview" 开头** | 套餐名进 `brand.py`（枚举值不改）；预览校验读配置 |
| 第三方 OAuth | Todoist/Asana/Google Tasks/ClickUp/Notion/Linear/X 的 client id 均 env，但回跳 URI 是 `omi://…` 与 `{BASE_API_URL}/…` | **每个集成在对方开发者后台以新品牌重新注册**（这是运营工作量，不是代码） |
| 应用市场 / 插件 | 27 个 `plugins/omi-*-app`、PyPI `omi-plugin-sdk`、`/.well-known/omi-tools.json`、图标 CDN 指向 BasedHardware GitHub、出站 UA `Omi-App-Store/1.0`、`community-plugin-stats.json`、personas 市场 | 三选一：① 自建市场（自有目录 + 审核）；② 白牌首发**关闭市场入口**只内置精选集成；③ 保留上游市场（品牌泄漏，不建议）。推荐 ②→① |
| MCP | PyPI `mcp-server-omi`、`Server("mcp-omi")`、`serverInfo.name="omi-mcp-server"`、9 条 OAuth scope 描述 "your Omi …"（授权页可见）、docs URL | 包名/服务器名/scope 文案由 manifest 生成 |
| **泄漏到用户的内部名**（必改） | API key 前缀 `omi_mcp_`/`omi_dev_`/`omi_code_`/`omi_oat_`/`omi_ort_`；响应头 `X-Omi-Rate-Limit-Reason`/`X-Omi-Request-Id`/`X-Omi-Provider`/…（7 个）+ 请求头 `X-Omi-Sync-Capture-Manifest`；UA `Omi-AI-Bot/1.0`；OpenAPI 标题 "Omi Developer API"/"Omi App Client API"；`FastAPI(title='Omi LLM Gateway')`；导出文件 `omi-export.json`；桌面制品名 `Omi.zip`/`omi.dmg`/`omi-setup.exe`；`templates/*.html` 内嵌 Omi 徽标 SVG 与品牌 CSS；错误文案 | 前缀/文件名/标题/模板走 `brand.py`；`X-Omi-*` 头与制品名是**桌面端共享契约**，在同一发布窗口两端同改 |
| 内部名（不改） | `omi:auto:*` 车道、`omi_paid`/`omi_eval`/`omi_managed` 策略、Prometheus 指标 `omi_*`、`OMI_*` env（约 40 个）、Helm/命名空间名；无任何 Firestore 集合以 omi 命名 | — |
| 法律 | `SECURITY.md` 指向 BasedHardware advisories；API 不提供隐私/条款端点 | 自有 SECURITY 联系人；条款/隐私由 Web 站承载 |
| auth-server | `package.json` 名 `omi-auth-server`、默认 DSN `omi:omi-dev-password@…/omi`；无 appName/issuer/邮件模板 | 基本无品牌工作；补邮件模板时一并品牌化 |

**评估**：`brand.py` + 约 40 个文件的机械改动（1~2 周）；市场/文档语料/更新源/OAuth 重注册属于**运营与产品决策**，不是改字符串能解决的。

## 分论四：桌面端 macOS + Windows（`desktop/`）

**规模**：745 个 Swift 文件中 501 个含 `Omi`；Windows 1,351 个 TS 文件中 292 个；**没有任何 `Localizable.strings`/`.xcstrings`**（全部文案是源码字面量）；265 个 `OMI_*` 环境变量（内部）。已参数化：`OMI_APP_NAME`/`OMI_BUNDLE_ID`/`OMI_URL_SCHEME`/`OMI_INSTANCE`、`OMI_LOCAL_PROFILE_STORAGE_NAME`、`.env` 的 API 地址、DMG 脚本（`create-desktop-dmgs.sh` 全参数化）、Codemagic 工作流级 `APP_NAME`/`BUNDLE_ID`/`BINARY_NAME`/`GITHUB_REPO`（**最接近品牌配置的现成机制**）、Windows 全套 `VITE_*` env、签名证书从 Keychain 解析。

| 触点 | 现状 | 白牌动作 |
|---|---|---|
| **两道硬门槛（必须最先改）** | ① `scripts/app-config.sh:29-40` **拒绝任何非 `omi-` 前缀的应用名**；② `AppBuild.swift:40` 以 `hasPrefix("com.omi.")` 判定非生产——换 bundle id 后会**静默变成生产态**，翻转沙箱/自动化/Sparkle 行为 | 二者改为读 manifest 的 bundle 前缀与生产 id 家族 |
| 身份常量 | `AppBuild.swift:6-18`（`com.omi.computer-macos`、`.beta`、`com.omi.desktop-dev`、`com.omi.preview.`）；`Info.plist` 显示名 `omi`、图标 `OmiIcon`、**7 条 TCC 权限说明 "Omi needs…"**、URL scheme；`create-omi-beta-variant.sh`；存储根 `~/Library/Application Support/Omi{, Beta}`（`DesktopLocalProfile.swift:61-70`）；日志 `/tmp/omi.log`、`~/Library/Logs/Omi`；`RewindOnlyView.swift:293` 把路径直接显示给用户；**31 个 `com.omi.*` 反域名字面量**（通知名、Keychain 服务 `com.omi.desktop.notion-mcp`、`com.omi.client-device-id`、自动化桥…）；`Package.swift` 可执行名 "Omi Computer"（= 包内二进制名）；`codemagic.yaml:1425-1429` Team `9536L8KLMP` + 沿用 iOS 旧 keychain group | 生成 `Brand.swift`；`run.sh` 已有 PlistBuddy 改写通道，扩展到 TCC 文案/feed/EdDSA key；`com.omi.*` 反域名字面量集中为 `Brand.reverseDomain + "…"` |
| 更新 / 分发 | `Info.plist:79` `SUFeedURL=api.omi.me/v2/desktop/appcast.xml`；`:81` **`SUPublicEDKey` 硬编码**；`AppBuild.swift:95,184` 下载页 host 与 `github.com/BasedHardware/omi/releases` 更新日志源；`CHANGELOG.json`（276KB）内嵌 `macos.omi.me` 链接随包发布；`omi_icon.icns`；`dmg-assets/` 品牌背景图；预览命名 `Omi Preview - $SLUG` | **生成新的 Sparkle EdDSA 密钥对**；feed/下载/日志源全部由 manifest 注入；CHANGELOG 从零开始 |
| 端点 / 第三方 | `DesktopBackendEnvironment.swift:4-9` 默认 `api.omi.me`/`api.omiapi.com`/Cloud Run URL/`h.omi.me`；**`BundleEnvironment.swift:9-14` 刻意拒绝生产家族 bundle 用 `.env` 覆盖 Firebase 身份**；`Sources/GoogleService-Info.plist` 整文件提交（`based-hardware`、API key、OAuth client）；`PostHogManager.swift:14-15` key 字面量；`OmiApp.swift:397-398` Sentry DSN 字面量；`pi-mono-extension` 注册 provider "omi"、`~/.omi/` 审计日志；Windows `.env.example` **全部可配置** | 换整文件 `GoogleService-Info.plist`（或自托管 Auth 后移除）；PostHog/Sentry 走 `Brand.swift`/env |
| 视觉 | 无 `.xcassets`，散文件：`omi_menu_bar_icon.png`、`omi_app_icon.png`、`herologo.png`（8+ 调用点，含默认参数）、`omi_text_logo.png`、`omi_notch_logo.svg`、`omi-demo.mp4`；字体 Geist/GeistMono/OpenRunde **均为 SIL OFL，可再分发，需随包附带 OFL.txt**；`Theme/Omi*.swift` 为标识符不可见 | 资源文件名保留、内容替换（最少改动）；菜单栏/notch 徽标重绘 |
| 文案 | 49 个 Swift 文件在 `Text(`/`Button(`/`.alert` 中含 Omi：登录页字标、"Preparing Omi…"、"Ask Omi"、"Omi Device"、"Work on this with Omi"×4、**`PermissionsPage.swift:492` "Find "Omi" and toggle it ON"（必须与系统设置里的实际显示名一致）**、"Update Omi"、"Get Omi Beta"、"Use Omi free forever" | 改读 `Brand.displayName`；顺手抽成 String Catalog 便于中文化 |
| AI 人格 | 9 处："You are Omi…"（`Chat/ChatPrompts.swift:21,165,396`、`RealtimeHubTools.swift:53,249`、`HomeSuggestionsStore.swift:315`、`ChatLabView.swift:424`、`agent/src/runtime/context-snapshot.ts:558`、Windows `desktopChatPrompt.ts:43`、`systemInstruction.ts:256`）；**4 条 Vitest 断言 `toContain('You are Omi')`** | 模板化 + 断言改为读品牌常量 |
| 产品不变量 | `INV-BETA-1` 锁定 `com.omi.computer-macos(.beta)`、"Omi Beta"、`Omi.Beta.zip`/`omi-beta.dmg`，5 套守卫测试，注明"退役需创始人签字"；`INV-UI-1` 禁紫色 ratchet 覆盖 desktop/app/web | 机制保留、**标识符参数化**：守卫测试改为断言 manifest 值；紫色 ratchet 换成自家禁色表 |
| Rust / 脚本 | crate `omi-desktop-core`（UniFFI 符号名）、14 个 `omi-*` 开发脚本、`OMI_*` env | **不改**（开发者可见、用户不可见） |
| Windows | `electron-builder.config.mjs:29-30,95,118,178`：appId `com.omiwindows.app`、productName "Omi for Windows"、可执行名 `omi-windows`、NSIS 制品名、publish 指向 `BasedHardware/omi`；**未配置代码签名**（SmartScreen "未知发布者"）；`package.json` author "Based Hardware"；托盘 tooltip 'Omi'（测试锁定） | 由 manifest 生成 electron-builder 配置；购买 EV/OV 代码签名证书是白牌 Windows 上线前提 |

**评估**：配置层约覆盖 60%（身份、分发、端点、Windows）且可一周内完成；剩余是 49 个文件文案抽取、9 处 prompt、两条不变量的守卫测试改写、Sparkle 密钥与 Firebase 身份间接层——约 3~4 周。仓库自身先例 `desktop/context-for-claude/` 是"另起代码库"而非"换皮"，说明上游从未为多品牌设计。

## 分论五：固件、硬件与 SDK（`omi/`、`omiGlass/`、`sdks/`）

**核心事实**：① 所有消费端（App、桌面端、Rust/Swift/RN SDK）都按**服务 UUID** 发现 Omi 设备，不按名字——`native_bluetooth_discoverer.dart:96-97`、`sdks/rust/omi-device/src/lib.rs:32-34`、`BLEScanner.swift:55`、`OmiConnection.ts:211`。**改广播名不会破坏配对**。② 固件不发送任何厂商自定义广播数据、不使用任何 Bluetooth SIG 公司 ID——没有"借用别人身份"的包袱。③ 自定义 UUID 有 5 个互不相关的随机基址，**不含品牌信息**，也没有"换前缀"的捷径。④ 协议常量在 **10 处手工镜像**（App、桌面 Swift、`sdks/device/{dart,go,typescript,rust,cpp}`、`sdks/{python,rust,react-native,swift}`），没有生成器，只有 `sdks/device/PARITY.md` 人工追踪。

| 触点 | 现状 | 判定 | 白牌动作 |
|---|---|---|---|
| 广播名 | `omi/firmware/omi/omi.conf:107` `CONFIG_BT_DEVICE_NAME="Omi"`；devkit `"Friend"`/`"Omi DevKit 2"`；test `"Omi EVT"`；`omiGlass/firmware/src/config.h:14` `"OMI Glass"` | 品牌 | 由 `brand.conf` 叠加覆盖（Zephyr `EXTRA_CONF_FILE`），不改上游 `omi.conf` |
| DIS 字串 | `omi.conf:108-113` 型号 `"Omi CV 1"`、厂商 `"Based Hardware"`、FW/HW 版本；glass `MANUFACTURER_NAME` | 品牌，**且型号串是 OTA 路由键** | 与 `backend/routers/firmware.py:51-66,96-108` 的型号→通道映射、`FIRMWARE_TAG_PATTERN`（`^(Omi_CV1\|Omi_DK2\|OmiGlass\|OpenGlass\|Friend)_v`）**同一 PR 原子修改** |
| 名字过滤（少数） | `device_connection.dart:102-106` 按 `glass` 子串路由眼镜；`omiGlass/sources/modules/useDevice.ts:42` **精确匹配 `'OMI Glass'`**（WebBluetooth）；`app/lib/utils/device.dart:54-93` `isOmiDevKit/isOmiCv1` 按 `DEVKIT/GLASS/NEO/FRIEND` 子串；`intercom.dart:38` | 品牌 | 改为读 DIS 型号或 manifest 的型号表 |
| 协议 UUID | `19b10000-…`（音频/主）、`19b10010-…`（设置，Glass 复用为 OTA）、`19b10020-…`（特性）、`19b10030-…`（时间同步）、`23ba7924-…`（按键）、`30295780-…`（存储）、`32403790-…`（加速度）、`cab1ab95-…`（扬声器/震动）；Nordic 传统 DFU `00001530-…`；SIG 标准 `180f/180a` | **协议** | 二选一：保留（与上游 App/SDK 互通）或**铸造新基址**（推荐，见下）；无论哪种，10 处镜像表必须字节一致 |
| 编码/帧格式/DFU | 编解码 ID 21/20、3 字节包头、16kHz 单声道；mcumgr SMP OTA（`mcumgr_flutter`） | **协议，保留** | — |
| 板级身份 | `boards/omi/board.yml` `name: omi, vendor: omi`（14 个文件）、`Kconfig.omi` `BOARD_OMI`、`CONFIG_OMI_*` 符号、`CMakeLists.txt` `project(omi)` | 内部 | **不改**（开发者可见，用户不可见；改动面大且无收益） |
| **签名密钥** | `omi/firmware/omi/sysbuild.conf:8` 指向 **已提交的私钥** `bootloader/mcuboot/root-rsa-2048.pem`（含 `enc-rsa2048-priv.pem`）；`scripts/ci/README.md:67` 明确承认 | **必改（安全）** | 生成新密钥对，私钥入 CI secret/HSM，仓库只留公钥指纹；`FLASH_3.0.8/` 内旧密钥签名的预编译 hex、`bootloader烧录.bat`、捆绑的 `iperf-2.2.1-win64.exe` 全部删除重建 |
| 发布流水线 | `.github/workflows/firmware_release.yml:14-15,98,140-144` 标签 `Omi_CV1_v<ver>`、资产 `Omi_CV1_OTA_v<ver>.zip`（后端要求文件名含 `ota` 且 `.zip`）、由 "Omi Bot" GitHub App 发布；`backend/utils/github_releases.py` 缓存键 `github_releases_omi` | 品牌 | 前缀由 manifest 生成；`ota`/`.zip` 约定保留；新建自有 GitHub App |
| **NFC 配对 URI** | `omi/firmware/omi/src/lib/core/nfc.c:94` 写入 `https://friend.based.com/pair?id=%s` | 品牌泄漏 | 改为自有域名（注意这是写进物理标签的载荷） |
| USB | 无 `CONFIG_USB_DEVICE_VID/PID/MANUFACTURER/PRODUCT` | — | 若需 USB-CDC 产品字串需从零添加，并需自有 USB VID（或用 pid.codes） |
| 文档 | `BUILD_AND_OTA_FLASH.md:191,334`（"scan for Omi"、`docs.omi.me`）、`FLASH_3.0.8/README.md:18`（上游 releases）、`omi/firmware/AGENTS.md` | 品牌 | 重写 |

**蓝牙身份是否与上游分离——推荐"分离"：**
保留帧格式、编解码 ID、mcumgr DFU（真实互操作价值、无品牌内容），但**铸造新的 128 位服务 UUID 基址 + 新 MCUboot 密钥**。理由：换密钥后已不可能接受上游 OTA 镜像（这是正确结果），此时若仍沿用上游 UUID，上游 App 会照常连上你的用户设备并把音频送到上游后端，你的设备也会出现在上游的支持队列里。成本：每个 SDK 一个常量 × 10 处镜像。

### 硬件（`omi/hardware/`）

| 触点 | 现状 | 白牌动作 |
|---|---|---|
| 许可证 | `omi/hardware/consumer/LICENSE` MIT（Based Hardware Contributors） | 保留版权声明 |
| 电气 | `electrical/{mainboard,charger-board,fpc-board}/{altium,gerbers}/omi2-*.zip`（Altium 源 + Gerber + 原理图 PDF）；**丝印/徽标图层在压缩包内，本次未能检视** | 需 Altium 打开修改丝印层并重新出 Gerber → 改板 |
| BOM | `bom/omi-bom.csv` 88 行，内部 SKU/装配名 `Oni`/`Oni2 Main Board`（内部代号）；MPN 为第三方器件 | 改内部 SKU；重新核对 nRF7002、TDK T5838 麦克风等器件的供货与 EOL |
| 结构 | `mechanical/` STEP + 2D PDF（CNC 铝盖、注塑壳、硅胶垫、SLA 件）——刻字/标识出现处需改 | 改刻字 = 改模具/CNC 程序 |
| 包装 | `packaging/{package-drawing.pdf,cad/*.step,photos/*}` 即盒型与包装图 | 全新包装设计 + 认证标识位置 |
| 电池 | README 注明 150mAh **定制** LiPo（D16×H6.1mm） | 自有 MOQ/交期；UN 38.3、IEC 62133；中国 GB 31241 + 电池 CCC |
| 认证 | **仓库内零处** FCC/CE/QDID/Declaration ID 记录 | 见分论七：主板是裸 nRF5340 + nRF7002 自制 PCB 而非预认证模组 → 各市场需完整发射机测试 |
| 眼镜 | `omiGlass/app.json` Expo 应用名 "Dude where's my car?"、slug `find-anything`、bundle `com.basedhardware.find`；`.env.template` 仅 Ollama 地址；Wi-Fi/OTA URL 由 App 经 BLE 运行时下发（协议保留）；`hardware/*_PCBWay Community.STL` 需核对第三方条款 | 眼镜若纳入产品线需单独立项 |

### SDK 与开发者面

| 包 | 已发布身份 | 白牌动作 |
|---|---|---|
| `sdks/python/pyproject.toml` | PyPI `omi-sdk`，作者 "Omi Community <support@omi.me>"，主页/文档指向 omi.me | 新包名；补 LICENSE 文件（`sdks/{python,rust,react-native,swift,device}` 元数据声明 MIT 但**未随包附带 LICENSE**） |
| `sdks/python-cli/pyproject.toml` + `publish_omi_cli.yml` | PyPI `omi-cli`，**命令名 `omi`**（用户可见），Trusted Publishing 绑定上游仓库 | 新包名与命令名；重建 Trusted Publishing |
| `sdks/react-native/package.json` / podspec | npm `@omiai/omi-react-native`，pod `omi-react-native`，源指向 BasedHardware | 新 npm 组织与作用域；类名 `OmiConnection` 为 API 破坏性改动 |
| `sdks/device/typescript` | npm `@basedhardware/omi-device` | 新作用域 |
| `sdks/device/go/go.mod` | `module github.com/BasedHardware/omi/sdks/device/go`（Go 模块路径即 URL） | 无法避免的硬改名 |
| `sdks/rust/omi-device`、`sdks/device/rust` | crate `omi-device`（**两处同名冲突**） | 新 crate 名，顺手消除冲突 |
| 根 `Package.swift` | SwiftPM `omi-lib`（`sdks/swift`），`Friend.swift` 类型名 | 新包名 |
| `sdks/omi-expo/` | 仅 `ios/`+`android/`，无 `package.json`，未发布 | 目录名而已 |
| 后端 URL | **所有 SDK 都不含 Omi 后端地址**，仅直连 Deepgram（`wss://api.deepgram.com`，密钥走 env） | 无需处理 |

**是否重发布 SDK 是产品决策**：若白牌产品不做开发者生态，SDK 可暂不发布，只需在仓库内改名以免品牌残留在开源代码中。若做，建议先写 `sdks/device/` 的**单一协议定义 + 生成器**（把 10 处手工镜像变成 1 处），再谈品牌——否则每次协议改动都是 10 倍工作量，这与白牌无关但会被白牌放大。

## 分论六：Web、文档、CI 与基础设施（`web/`、`docs/`、`.github/`、`codemagic.yaml`、`infrastructure/`）

### Web 四站点

| 站点 | Cloud Run 服务名 | 用途 / 域名 | 品牌触点 |
|---|---|---|---|
| `web/app` | `omi-web-app` | 消费者 Web 客户端 `app.omi.me`，功能与移动端接近，手机访问弹 `MobileBlockOverlay` 引导装 App | `layout.tsx:17` `metadataBase omi.me`/`siteName 'Omi'`；`robots.ts`/`sitemap.ts`/`JsonLd.tsx` 钉 `omi.me`；4 处 API 兜底 `\|\| 'https://api.omi.me'`（`api/proxy`、`api/apps/search`、`lib/api/public.ts`、`case/[ref]`）与 `transcriptionSocket.ts:32` `wss://api.omi.me`——**env 可覆盖但默认值错**；`Footer.tsx` 8 处（`team@basedhardware.com`、GitHub、Shopify 商品、`affiliate.basedhardware.com`）；`Sidebar.tsx`（`macos.omi.me`、`onelink.to`、feedback、discord）；`SettingsPage.tsx` 7 处文档/帮助/MCP SSE 地址；`CaseStatusView.tsx:12` 支持邮箱 |
| `web/frontend` | `frontend` | 应用市场 + 公开分享页 `h.omi.me`（**不是** omi.me 落地页——omi.me 是仓库外的 Shopify 商店） | `layout.tsx:17-40` 标题/描述、Elfsight 公告条 app id；`google-analytics.tsx` **GA `G-2WSLB4VPWF` 硬编码**；`public/sitemap.xml` 静态 `h.omi.me`；Shopify 商品与 `instagram.com/omi.me`；**`Dockerfile.datadog:44` `ENV API_URL=https://backend-hhibjajaja-uc.a.run.app`（上游生产后端）** |
| `web/personas-open-source` | `omi-web` | AI 人格聊天 `personas.omi.me` | 标题 "Omi by Based Hardware"、OG `omidevice.webp`；**PostHog key 与 GA `G-3JHGJR61HK` 硬编码**；`u/[username]/layout.tsx` 三处 `metadataBase personas.omi.me`；消费 `NEXT_PUBLIC_EXTRA_PROMPT_RULES`（"subtly promote Omi…"） |
| `web/admin` | `omi-admin-dashboard` | 内部运营（订单、分销、联盟、发布、Grafana/PostHog 看板） | "Omi Admin Dashboard"；`public/images/omi.png`；Shopify/ShipBob/GoAffPro 集成为上游电商运营专用 |

**接缝**：`config/public-build-contract.json` 已把四站点做成"仓库配置驱动的公共构建契约"，`config/public-build-values.json` 是**单文件替换点**——但该文件当前装着上游生产 Firebase 项目 `based-hardware` 的 API key、sender id、VAPID key、GA `G-CVV8BPC4DT`、Mixpanel token 与 `api.omi.me`。白牌第一步就是整文件重写并把 `NEXT_PUBLIC_EXTRA_PROMPT_RULES` 清空。徽标资源：`web/app/public/{logo.png,omi-white.webp,favicon.png}`、`web/frontend/public/{logo.webp,omi-*.webp,omi-app.png,df-*.png}`、`web/personas-open-source/public/{omilogo.png,omifavicon.*,basedlogo.png,basedfavicon.*,omidevice.webp,omiweb2.mp4}`、`web/admin/public/images/omi.png`——文件名保留、内容替换最省。

### 文档站（`docs/`，Mintlify）

- `docs/docs.json`：`name` 已是通用的 "Docs"，配色是 Mintlify 默认绿（非 Omi 色）；需改：`favicon`/`logo/{dark,light}.svg`、页脚（GitHub/Discord/X/LinkedIn/Instagram）、导航 Support→`help.omi.me`、主按钮 **"Buy Now" → Shopify 变体链接**。自定义域名在 Mintlify 后台而非仓库；`deploy_docs.yml` 另发 GitHub Pages。
- **112 篇 `.mdx` 中 92 篇（82%）提到 Omi**。不建议逐篇改：白牌首发只需"快速开始 + App 使用 + 隐私/条款 + 开发者 API"约 15~20 篇，其余（硬件组装、购买指南、社区插件）按产品线决定是否保留。
- OpenAPI 三份（`openapi.json`、`app-client-openapi.json`、`integration-public-openapi.json`）标题 "Omi Developer/App Client/Integration API"、contact/license/servers 全部由 `backend/scripts/export_openapi.py:278-319` 生成——改生成器即可。
- `docs/assets/readme/*-badge.png` 为 Apple/Google 商店徽章（需新商店链接）；`README.md:16` DeepWiki 链接。

### CI/CD（71 个 GitHub 工作流 + Codemagic 约 40 个）

身份是**集中**而非弥散的：多数 GCP 工作流读 `vars.GCP_PROJECT_ID`。必须重调的：

| 类别 | 工作流 / 位置 | 品牌或账号绑定 |
|---|---|---|
| Web 部署 | `gcp_admin.yml`、`gcp_app.yml`、`gcp_frontend.yml`、`gcp_personas.yml` | 并发组名 `omi-admin-dashboard`/`omi-web-app`/`omi-web`；服务名来自契约文件 |
| 后端/GPU 部署 | `gcp_backend*.yml`、`gcp_backend_pusher*.yml`、`gcp_diarizer.yml`、`gcp_parakeet.yml`、`gcp_llm_gateway.yml`、`gcp_nllb_translation.yml`、`gcp_plugins.yml`、`gcp_*_job*.yml`、`gcp_firestore_indexes.yml` | `PROJECT_ID: based-hardware(-dev)`；云中立化后整批替换为自托管部署 |
| 移动/桌面 | `mobile_internal_build.yml:48`、`desktop_publish_preview.yml:165` **`CODEMAGIC_APP_ID` 默认 `66c95e6ec76853c447b8bcbb`（上游 Codemagic 应用）**；`desktop_auto_release.yml` 标签 `v*-macos`；`desktop_promote_prod.yml` 写死 `api.omi.me/v2/desktop/appcast.xml?identity=stable` 与 `macos-beta/stable` 指针；`desktop_windows_release.yml` | 全部换自有账号；Sparkle 密钥在 secrets |
| 固件 | `firmware_release.yml` | 标签/资产前缀、"Omi Bot" App |
| 发布 | `publish_omi_cli.yml` | PyPI `omi-cli` Trusted Publishing 绑定上游仓库 |
| 基础设施 | `opentofu-development-wif-pilot*.yml` + `infrastructure/opentofu/pilots/development-wif-plan/variables.tf:4-70` | WIF 信任主体钉死 `BasedHardware/omi`（仓库 id `776121034`、组织 id `162546372`、项目号 `1031333818730`、池 `omi-opentofu-9842-dev`）——**fork 在重新引导前无法认证** |
| 协作 | `sync-docs.yml`（Omi Bot 自动合并）、`main.yml`（同步到上游 org 项目看板）、`entelligence-*.yml`（第三方 PR 评审供应商）、`.github/CODEOWNERS`（三位上游成员个人账号）、`ISSUE_TEMPLATE/*`（链接上游 PRODUCT.md） | 删除或换成自有 |
| Codemagic | `codemagic.yaml`：`APP_ID: 6502156163`、`PACKAGE_NAME com.friend.ios`（15 处）、bundle id（21 处）、`app_store_connect: codemagic_v4`、Firebase 项目、Team `9536L8KLMP`、GCS 桶 `gs://omi_macos_updates` | 由 manifest 生成变量段；或迁到自有 CI |

**`.github/checks-manifest.yaml` 的 140 项检查**：几乎全部是工程守卫，**保留**；只需重调 `brand-ui`/`brand-ui-ratchet-tests`（INV-UI-1 禁紫色是上游创始人的品味规则，白牌换成自家色表）以及名字绑定到 `omi_icon.icns`、`create-omi-beta-variant.sh`、`com.omi.*` 路径的少数检查。`check_agents_md_lean.py` 与 16 条非品牌不变量原样保留。

### 基础设施与监控

- `firebase.json`、`firestore.rules`（deny-all）、`firestore.indexes.json`：**无项目绑定，干净**。
- `dev/docker-compose.dev.yml`：容器名 `omi-emulators`/`omi-postgres`/`omi-redis`/`omi-minio`，仅本地，可不改。
- `backend/charts/monitoring/**`：`dashboards/omi-services/omi-services-overview.json` 一个文件 **92 处** `based-hardware`，Cloud Run 看板各约 39 处，`backend/deploy/runtime_env.yaml` 23 处——机械但量大、易与上游冲突；云中立化时整套看板重建更划算。
- `infrastructure/opentofu/foundation/main.tf` 无硬编码项目；`environments/*.backend.hcl.example` 为占位符——干净。

### 仓库级品牌规则（治理层）

- `INV-UI-1`（`docs/product/invariants/brand-ui.md`，锁定）：禁紫色 ratchet，覆盖 desktop/app/web，allowlist 含 `OmiColors.swift`。白牌应**改写为自家品牌色规则**并同步改 `check_brand_ui.py` + `test_check_brand_ui.py`（三者同 PR），而非删除守卫机制。
- `INV-BETA-1`（`desktop-beta-identity.md`，锁定）：写死 Bundle ID 与 "Omi Beta"，注明"退役需创始人签字"——这是上游的治理规则，fork 直接**接管**：机制保留、标识符改为 manifest 值、守卫测试改断言配置。
- `.impeccable.md`（品牌人格、"Omi mark 的使用"、禁紫色）、`PRODUCT.md`（"Taste floor: Stay on-brand"）、`design-qa.md`（一份过期的 QA 记录）：重写为自家版本；其余 16 条不变量（记忆/认证/数据面）与 7 份 `AGENTS.md` 原样保留。

## 分论七：法律、平台政策与合规边界（代码之外，但决定方案成败）

### 许可证与商标

| 事项 | 事实 | 对白牌的约束 |
|---|---|---|
| 代码许可证 | 根 `LICENSE` 为 MIT，版权方 "Based Hardware Contributors"；`mcp/LICENSE`、`web/personas-open-source/LICENSE` 同为 MIT | 可自由商用、闭源二次分发。**唯一硬性义务**：在分发件（App 的开源许可页、桌面端 About、后端镜像）中保留原版权声明与许可文本 |
| 商标 | MIT 不授予商标权。"Omi"、Omi 徽标、"Friend"（Omi 前身）、"Based Hardware" 均为 Based Hardware 的商业标识 | 名称、徽标、吉祥物、口号、`omi.me` 域名家族、App Store 上架名 "Friend AI Wearable" 全部不可沿用；"Omi 兼容"之类描述性使用需谨慎 |
| 命名前车之鉴 | Omi 原名 "Friend"，因另一家公司以 180 万美元购入 friend.com 并发布同名设备而被迫改名 | 定名前做 USPTO / CNIPA（第 9、42 类）与域名、App Store 名称三重检索，避免上市后改名（改名 = 重做全部本方案） |
| 第三方许可 | 见后文各组件清单中的"非 MIT 组件"（模型权重、字体、专有 STT SDK 等） | 商用前逐项核对：模型权重（CC-BY / gated）、字体（是否允许嵌入分发）、专有供应商合同（Modulate、Deepgram 自托管） |

### 应用商店政策（每个品牌 = 一个独立开发者账号）

- **Apple 4.2.6**：由"商业化模板/应用生成服务"产出的 App，必须由**内容提供方自己的开发者账号**提交，模板方不得代为提交；替代路径是"聚合/选择器"单一二进制。对白牌意味着：**自有品牌**必须用自己的 Apple Developer Program 账号（含 Sign in with Apple 的 Services ID、APNs 密钥、Associated Domains 全部重建）；若未来对 B 端伙伴做多品牌，每个伙伴各自建账号提交。
- **Apple 4.3（Spam）**：同一代码库在多个账号下以近似外观上架会被判"重复 App"。缓解：每个品牌有独立视觉、独立商店素材与描述、可感知的产品差异（功能集、默认语言市场、硬件配套）。
- **Google Play**：官方《白牌开发者最佳实践》要求每个 App 有独立且有说服力的商店页（描述、图标、图形、截图不得雷同），并建议**每个品牌独立开发者账号**（集中式单账号被标记为高风险，可能连带封禁）。"重复内容政策"禁止"由自动化工具/模板生成、内容与体验高度相似"的 App。
- **推论**：白牌不是"换皮"就能上架；商店层面需要真实的品牌差异化资产与独立账号体系。这一条应写进本方案的验收标准。

### 硬件与无线合规（设备白牌的真实门槛）

| 项目 | 规则 | 白牌动作 |
|---|---|---|
| Bluetooth SIG | 对**未修改的既有合格设计**做换牌（品牌、包装、颜色、外形、产品名、型号均可变），无需重测，但**必须以自己的 SIG 会员身份购买新的 Declaration ID** 并完成声明，才能使用 Bluetooth 商标和字标 | 加入 SIG（Adopter 免费）→ 引用 nRF52 模组/Omi 既有 QDID → 购买 Declaration ID（费用按会员级别，数千至上万美元量级，以 Launch Studio 当期报价为准） |
| FCC（美国） | 47 CFR §2.933 "Change in ID"：同一硬件换新 FCC ID 时，若设计/电路/结构未变，无需重新测试与送样，但需附原认证标识声明，且**需原 Grantee 同意/协议** | 若沿用 Omi 整机方案需 Based Hardware 出具同意；若自找 ODM 改板则走全新认证（预算 1~2 万美元级） |
| CE / UKCA（欧英） | RED 指令需 DoC + 技术文件；换品牌换型号即新的经济运营者责任 | 以自有法律实体重新出具 DoC，复用测试报告需实验室同意 |
| 中国 无线 | 工信部 2019 年第 52 号公告：2.4GHz 微功率短距离设备（含 BLE，发射功率 ≤10mW/10dBm e.i.r.p.）**免型号核准（SRRC）** | 核对固件 TX power 配置不超 10dBm，否则需 SRRC |
| 中国 电池 | GB 31241 为强制标准；锂离子电池/电池组自 2024-08-01 起纳入 CCC 强制认证目录 | 电芯/电池包需具 CCC 证书，整机需符合 GB 31241 |
| 中国 整机 | 蓝牙耳机/项链类消费电子本身不在 CCC 目录（电池除外）；需 GB 4943.1 安全（自愿/市场抽检依据） | 走第三方检测报告 |

### 中国大陆运营合规（如目标市场包含国内）

1. **ICP 备案 + APP 备案**：工信部 2023-08-04《关于开展移动互联网应用程序备案工作的通知》要求 APP 主办者履行 ICP 备案义务，未备案不得提供 APP 互联网信息服务，应用商店不得分发；2024-04 起进入核查处置阶段。Apple 中国区 App Store 同步要求提交 ICP 备案号。**主体必须是中国境内实体**，域名需在境内备案 → 与已有的"4C8G 自托管"路线天然一致。
2. **生成式 AI 备案**：《生成式人工智能服务管理暂行办法》（2023-08-15 施行）——面向公众、具有舆论属性或社会动员能力的生成式 AI 服务需**算法备案 + 安全评估**；**通过 API 调用已备案模型**的应用/功能需在属地网信办**登记**，并在产品页公示所用模型名称与备案号。白牌产品的"AI 助手 / 记忆聊天"属于此范围：选用已备案模型（阿里百炼 Qwen、火山方舟、DeepSeek 等）走登记路径成本最低；自托管开源模型则需自行备案。
3. **个人信息保护法（PIPL）**：录音、声纹（用于说话人识别）属于**生物识别 = 敏感个人信息**，需单独同意 + 个人信息保护影响评估（PIA）；录制第三方对话需在产品交互与隐私政策中明确告知义务；境内用户数据默认境内存储，跨境需评估/标准合同——再次指向自托管。
4. **未成年人保护、数据出境、算法推荐备案**（如做内容推荐/主动通知）按需评估。

> 这些不是"改代码"能解决的项，但决定了品牌名、开发者账号、服务器所在地、模型供应商四个上游决策，必须放在 Phase 0。

## 分论八：实施路线图、验收标准与风险

### 分期（软件两人并行；硬件独立轨道）

```
Phase 0  决策与账号（1~2 周，非工程为主，可与 P1 并行）
  命名 + 商标/域名/商店名三重检索 → 法律主体 → 目标市场（含中国？）
  Apple Developer / Google Play / 自有 CI / Sparkle 密钥 / MCUboot 密钥 / Windows 签名证书
  Firebase 新项目 或 自托管 Auth（推荐后者，见 omi-better-auth-shim.md）
  PostHog/Sentry/GA 自有账号；Stripe 自有账号与套餐；7 个集成的 OAuth 应用重注册
  模型供应商（国内：选已备案模型走登记）；市场/Personas 开关；UUID 分离决策

Phase 1  品牌配置层 + 拆硬门槛（2 周）
  brand/manifest.yaml + assets + legal + prompts/persona.yaml
  scripts/brand/apply.py（渲染全部生成物）+ check.py（接入 checks-manifest 双通道）
  拆 5 道硬门槛：app-config.sh 前缀校验、AppBuild.isNonProduction、environment_profile 校验表、
    BundleEnvironment Firebase 覆盖限制、desktop_previews app_name 校验
  注入点落地：flavorizr.yaml / xcconfig / Brand.swift / brand.py / brand.conf / site.config / docs.json
  一次性重写 config/public-build-values.json；清空 NEXT_PUBLIC_EXTRA_PROMPT_RULES
  验收：全平台以"占位品牌"构建通过；check.py 报告的泄漏清单 = P2 待办

Phase 2  用户可见面清洗（3~4 周）
  移动端：190 个 ARB 键 → {appName} 占位（45+ 语言机械替换，变格语言人工复核）；约 15 键重写；
          556 处 Dart 硬编码中用户可见部分改读 Brand；8 个 entitlements 的 App Group/Associated Domains；
          Info.plist 权限描述；字体替换（SF Pro → OFL 字体）；设备图/图标/启动图
  桌面端：49 个文件文案改读 Brand.displayName（顺手抽 String Catalog）；9 处 prompt + 4 条断言；
          7 条 TCC 文案；31 个 com.omi.* 反域名集中；资源内容替换；Windows electron-builder 配置
  后端：brand.py；约 30 处 prompt 模板化（三份重复合一）；通知/HTML 模板/OpenAPI/导出名/前缀；
        share_links 去硬并入；firmware.py 型号→通道映射与固件 DIS 同 PR
  Web：四站点元数据/页脚/侧栏/分析 key；Dockerfile.datadog；docs.json + 首发 15~20 篇文档
  固件：brand.conf（广播名/DIS）；nfc.c 配对 URI；新 MCUboot 密钥；删除 FLASH_3.0.8 预编译件
  验收：check.py 零泄漏；全新安装 E2E 无 "Omi"；AI 自我介绍评测通过

Phase 3  分发链路（2~3 周）
  App Store / Play 上架（独立素材与描述，见 4.2.6/4.3）；IAP 策略落地
  Codemagic（或自有 CI）变量段生成；Sparkle appcast 后端 + 新公钥；INV-BETA-1 守卫改配置
  Windows 代码签名；firmware_release 前缀与 GitHub App；SDK 按需重发布
  验收：三端自动更新在新通道走通；固件 OTA 用新密钥走通；上游合并演练（见下）

Phase 4  硬件白牌（并行，3~6 月，若做 L3）
  ODM/CM 选定 → 丝印/刻字/包装设计 → 试产 → 认证（Bluetooth SIG DID、FCC/CE 或中国微功率免核准 + 电池 CCC）→ 量产

Phase 5  生态与运营（持续）
  邮件通道（功能缺口）、帮助中心/反馈/状态页、隐私政策与条款、应用市场决策、第三方集成上线
```

**工作量汇总**：P1 约 2 人周；P2 约 8~10 人周（移动 3、桌面 3~4、后端 1.5、Web/文档 1.5）；P3 约 3~4 人周。合计 **13~16 人周 ≈ 3~4.5 人月**，两人并行 8~10 周。硬件轨道另计。

### 上游同步流程（白牌后的常态）

1. `git merge upstream/main` → 冲突只应出现在十几个注入点文件（`flavorizr.yaml`、`AppBuild.swift`、`brand.py` 调用处等），因为内部标识符未改。
2. `scripts/brand/apply.py` 重跑生成物 → `git diff` 应为空或仅限生成文件。
3. `scripts/brand/check.py` 扫描用户可见面 → 上游新增的 "Omi" 字面量在此暴露，逐条改为读常量，并把这类"可配置化"补丁**回推上游**（对上游无害，接受后 fork 负担归零）。
4. 每次合并演练记录耗时；若单次合并冲突文件超过 30 个，说明有内部名被误改，回滚该改名。

### 验收标准（白牌 Definition of Done）

- **机械检查**：`check.py` 在 ARB、Swift 文案、prompt 模板、通知模板、HTML 模板、`Info.plist`、`docs.json`、商店元数据、OpenAPI 输出上**零上游品牌词**（Omi/omi.me/Based Hardware/Friend/basedhardware.com/friend.based.com）；所有生成物幂等；Bundle ID/包名/URL scheme/Associated Domains/App Group 与 manifest 一致。
- **无上游凭据**：构建产物与镜像中不含 `based-hardware*` Firebase 项目、`AIzaSy…` 上游 key、上游 PostHog/Sentry/Mixpanel/GA/Shorebird/Codemagic id、`api.omi.me`/`api.omiapi.com`/`*.a.run.app` 上游地址（含 `\|\|` 兜底默认值）。
- **行为验收**：iOS/Android/macOS/Windows/Web 全新安装走完 onboarding、配对、录音、聊天、订阅、导出，界面/通知/系统权限弹窗/导出文件名/写入第三方（Apple 提醒事项）**全程不出现上游品牌**；对 AI 提问"你是谁/谁做的你/推荐我买什么设备"，回答只出现自家品牌（把这组问题做成后端 eval 固定用例）。
- **设备**：扫描到的广播名、DIS 厂商/型号为自家；NFC 标签 URI 为自家域名；OTA 用新密钥成功、用上游镜像被拒。
- **法律**：开源许可页含 MIT 原文与 Based Hardware Contributors 版权行（另含 Nordic 5-Clause、Opus BSD-3、Parakeet CC-BY-4.0 归属、OFL 字体许可）；隐私政策/条款为自家法律主体；商店页素材与描述独有。
- **可持续**：一次 `upstream/main` 合并演练在半天内完成且 `check.py` 通过。

### 风险与对策

| 风险 | 说明 | 对策 |
|---|---|---|
| 全局改名导致上游不可合并 | fork 每周合并上游数百文件 | 只改用户可感知面；内部标识符"不改清单"写进 `AGENTS.md`；`check.py` 守住边界 |
| 两端共享契约改名不同步 | `X-Omi-*` 头（7 个）、`X-Omi-Sync-Capture-Manifest`、制品名 `Omi.zip`/`omi.dmg`、API key 前缀、DIS 型号→OTA 通道 | 归为"契约组"，同一发布窗口两端同改；新品牌无存量用户，无需兼容层 |
| 锁定不变量与守卫测试阻塞 | `INV-UI-1` 紫色 ratchet、`INV-BETA-1` 五套守卫、4 条 `'You are Omi'` 断言 | 机制保留、断言改读 manifest；同 PR 更新不变量文档 |
| 商店审核 | Apple 4.2.6/4.3 与 Play 重复内容政策；WebView 内 Stripe 收款的 3.1.1 风险 | 独立开发者账号 + 独有素材与差异化；按市场决定 IAP 路线（中国区必须 IAP 或不在 App 内售卖数字订阅） |
| 继承的安全债 | 仓库内 MCUboot 私钥、上游明文 key、预编译固件与 `iperf.exe` | P1 即换密钥、删预编译件、轮换所有 key；`.secrets.baseline` 重建 |
| 隐蔽品牌泄漏 | NFC URI、Apple 提醒事项 "From Omi"、prompt 促销规则、`Dockerfile.datadog` 上游地址、`CHANGELOG.json` 链接、导出文件名、`omi-export.json` | 全部列入 `check.py` 的扫描面（不仅扫 UI 字串，也扫模板、配置与固件源） |
| 第三方许可 | SF Pro 字体（必换）；Deepgram 自托管需付费许可与 license-proxy；Modulate 为商业 API；Parakeet 权重 CC-BY-4.0 需归属；`pydub`→FFmpeg 需确认 LGPL/GPL 构建；Nordic 5-Clause 限 Nordic 芯片；Lottie 素材条款；`PCBWay Community` STL 条款 | 商用前逐项核对并写入开源声明页 |
| 硬件认证被低估 | 主板是裸 nRF5340+nRF7002 自制 PCB，无模组预认证；仓库内无任何认证记录 | 分论七清单进入 P4 预算；中国市场核对 e.i.r.p. ≤10dBm（当前 `CONFIG_BT_CTLR_TX_PWR_ANTENNA=8`）以享微功率免核准 |
| 中国合规 | APP 备案主体、生成式 AI 登记、声纹属敏感个人信息 | 在 P0 定主体与模型供应商；隐私交互加单独同意与 PIA |
| 功能缺口误当改名 | 邮件通道缺失、帮助中心/状态页/反馈入口全在 omi.me | P5 列为独立功能项，不计入白牌工程量 |

### 与云中立化路线的关系

- **共享前置决策**：法律主体、服务器落地、认证方案、模型供应商。
- **相互抵消的工作**：自托管 Auth 落地后，移动端三件 Firebase 配置、桌面 `GoogleService-Info.plist`、`environment_profile` 的 Firebase 校验、`BundleEnvironment` 的覆盖限制、`config/public-build-values.json` 的 Firebase 段全部消失；自托管部署后 `gcp_*.yml`、监控看板、OpenTofu WIF 的 `based-hardware` 引用整批作废。
- **推荐顺序**：P0 同时启动两条线 → 白牌 P1（配置层）与认证 shim 并行 → 白牌 P2 在自托管 Auth 可用后进行（少做一遍 Firebase 相关改动）→ P3 与部署面云中立化合并进行（CI 反正要重写）。

---

## 总：收束

### 白牌化的本质

Omi 的代码把"品牌"当成事实而不是输入：没有 `PRODUCT_NAME`，AI 在约 40 处 prompt 里自称 Omi，五道校验主动阻止改名，两条锁定不变量把 Bundle ID 写进治理文档。但**用户可感知面是有限、可枚举、已逐文件定位的**（本方案六个分论即清单），而内部标识符（Dart 包名、Swift 类型、`OMI_*` 环境变量、车道名、脚本名）**一个都不必改**。这两点决定了正确做法：**一个 `brand/` 清单 + 各平台一处生成物注入点 + 一条 CI 守卫**，而不是全仓库查找替换——后者会让这个每周合并上游的 fork 在一个月内失去可合并性。

### 三件事最值得先做

1. **拆五道硬门槛并落地配置层**（P1，2 周）：在这之前任何白牌改动都无法构建验证。
2. **换掉继承的安全与许可债**：MCUboot 私钥、上游明文 key、预编译固件、SF Pro 字体——与改名无关，但白牌上线即暴露。
3. **把"可配置化"补丁回推上游**：`{product_name}` 插值、`Brand.displayName`、ARB `{appName}` 占位符对上游无害；上游接受多少，fork 的长期维护成本就减少多少。

### 代码之外决定成败的四件事

品牌名的商标清关（Omi 自己付过 "Friend" 的学费）、每个品牌独立的商店开发者账号（Apple 4.2.6 / Play 白牌指引）、硬件的无线与电池认证（仓库内零记录、主板非预认证模组）、以及若进入中国市场的 ICP/APP 备案 + 生成式 AI 登记 + 声纹作为敏感个人信息的合规——这些在 Phase 0 就要有人负责，工程排期不等它们。

### 最终交付形态

软件白牌 ≈ 3~4.5 人月（两人 8~10 周）后：一套按 `brand/manifest.yaml` 生成的 iOS/Android/macOS/Windows/Web/后端/固件，全新安装全程不见上游品牌，AI 只以自家品牌自称，设备以自家名字与 UUID 广播、以自家密钥 OTA，`check.py` 在 CI 里把品牌边界守成机械规则，`upstream/main` 仍能半天合完。硬件白牌是另一条 3~6 个月的轨道，以认证与模具为主，代码只占很小一部分。
