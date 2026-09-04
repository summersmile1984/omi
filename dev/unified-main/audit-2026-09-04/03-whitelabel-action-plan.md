# 第三路：白牌终端与统一客户端行动方案

三路入口：[标准服务器](01-self-host-action-plan.md) · [Cloudflare](02-cloudflare-action-plan.md) · [统一主线索引](../README.md)。

审计日期：2026-09-04（Asia/Shanghai）。代码基线：`origin/main = d238a85af9d999992d9f0352db682cd9f11fc951`。工作分支：`codex/audit-whitelabel-20260904`；隔离工作树：`/Users/macstudio/Documents/memweft-worktrees/audit-whitelabel-20260904`。本轮只做代码检查、可控实验和方案，没有修改产品代码、推送、发布、启动或停止生产 Omi 应用。

**结论：白牌仍处于工具骨架和 Flutter 标题接线阶段，尚无完整自有品牌客户端。下一步先修品牌/profile 的输入和验收边界，再按完整安装身份、认证和升级契约推进；不能把“生成器通过”“上游品牌零 diff”或“前缀可改”当成交付完成。**

本路覆盖 iOS/Android Flutter、原生 macOS、Windows/Linux Electron、Web，以及终端必须共同消费的后端人格/分享/通知和固件 BLE/OTA。标准 Server OS 对应仓库已有 target `self_hosted`，另一个 target 为 `cloudflare`；不新增第三个名称。品牌、部署 target、发布 stage 和客户端平台是独立维度，继续使用一条 `main`。

## 1. 当前状态与证据范围

| 层/平台 | 已在 main 的实现 | 未完成的边界 | 本轮结论 |
|---|---|---|---|
| 品牌配置 | `brand/_schema/manifest.schema.json`、`brand/omi-upstream/manifest.yaml`、apply/check、28 项 Python 测试 | 只有上游品牌；资产路径和若干密钥/字体/发布字段仍为占位；没有正式自有品牌 manifest | 配置骨架可运行，完整身份未生成 |
| Flutter 运行时标题 | `F.title` 消费生成的 `kBrandDisplayName`；prod/dev 标题有 2 个 Flutter 测试 | 桌面图标下的应用名、包 ID、扩展、图标、文案、原生回调注册均未随品牌生成 | 仅一个 Dart 文件的能力已完成 |
| iOS/Android 安装包身份 | 上游 flavor 和原生工程仍可供继续开发 | 包 ID、App Group、entitlements、Widget/Watch/扩展、scheme、AASA/assetlinks、签名仍为上游配置 | 尚无白牌安装包证据 |
| 原生 macOS | 上游 named bundle 构建、身份判断、签名、更新路径仍在 | main 没有 desktop generator；自有命名被 shell 拒绝，运行时身份判断也不识别自有 namespace | 必须整体接通身份判断与打包，不能只放宽前缀 |
| Windows / Linux | Electron 原有构建和独立平台 seam | appId、productName、安装产物、release feed、桌面/托盘/协议/存储、UI 仍未品牌化 | 未接线；Linux 自动更新原本也未实现，不能声称 Windows 打包通过即覆盖 Linux |
| Web | 当前已是 Moonshine/Bun 源码；有生成的 profile TS 表 | 品牌生成器缺失；Firebase 登录、fallback API/WS、链接/logo/persona 未消费品牌/profile；扫描器漏掉整个 `web/app` | 未接线；不得按旧 Next/vinext 计划验收 |
| 四端 deployment profile | 五份输出文件（四端 + backend）及单一配置源 | 搜索生成符号仅命中定义；移动端生产选择器直接拒绝两 fork target 的新 profile 名 | 生成表存在，客户端未消费 |
| 后端品牌输出 | schema 已描述 persona、domain、API prefix 等 | prompt、通知/邮件、分享、OpenAPI、升级链接还没接品牌层；需两个服务端共同实现 | 终端白牌必须覆盖这些可见输出 |
| 固件/设备 | 上游 BLE、DIS、NFC、OTA 发布链 | 未生成品牌 conf/header；终端 UUID、型号映射和固件 tag/签名未共用品牌契约 | 未完成，改广播名不足以交付设备白牌 |
| CI/发布 | `.github/workflows/fork-checks.yml`，品牌/profile 基础检查 | 没有 `deploy/matrix.json`、品牌构建矩阵、真实品牌产物扫描与安装 smoke | 基础 gate 与发行验收仍有明显距离 |

### 1.1 已合并与仍打开的工作

- main 的 [`apply.py:50–52`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/scripts/brand/apply.py#L50) 只注册 `flutter`；其他 7 类输出打印“no generator registered yet”后正常返回。`mobile.py:46–59` 只写 `app/lib/flavors.brand.dart`。
- [`flavors.dart:24–30`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/app/lib/flavors.dart#L24) 已读品牌标题，[PR #12](https://github.com/summersmile1984/omi/pull/12) 的边界是运行时标题，不是完整移动白牌。`04-brand-layer.md:156–166` 已明确承认其余 native 身份项未落地。
- [PR #10](https://github.com/summersmile1984/omi/pull/10) 当前仍 `OPEN`、`CONFLICTING` / `DIRTY`，head 为 `60a62d49bb43029b42357df265da5969bee91a8a`。范围是 `app-config.sh` 的 named 前缀加生成器/测试；尚未处理完整 macOS 运行时身份。`Desktop Swift Release Compile` 成功；`Desktop Swift Static & Test Contracts` 与汇总 `Desktop Swift Build & Tests` 失败。
- PR #10 的实际失败日志指向 `ProactiveListenEventTests.testHandlerRecognisesProactiveMessageType` → `NotificationService.swift:171` → `UNUserNotificationCenter.current()` 的 `bundleProxyForCurrentProcess is nil`，进程 signal 6。**这是当前 CI 阻塞证据，尚无证据证明由该品牌改动引入。** 不把“两个红项”解读为两个独立品牌缺陷。需要在基线和更新后的 head 对比同一 suite，按现有 hermetic 通道修复 fixture/生产 seam，不能直接删检查。

## 2. 会决定下一步的实测发现

### E1：合法测试品牌只改变标题，原生身份完全不动

本轮在 `/tmp/memweft-audit-20260904/whitelabel/fixture-repo` 建立临时 Git fixture，复制生产生成器、schema、profile 源和必要原生配置，使用虚构的 `audit-neutral / Weft Audit / *.audit.example`，没有往正式 `brand/` 新增品牌。

`schema_validate.validate()` 返回空错误列表。运行 `python3 scripts/brand/apply.py --brand audit-neutral` 返回 0，`git diff --name-only` **只出现 `app/lib/flavors.brand.dart`**，内容为 `const String kBrandDisplayName = 'Weft Audit';`。`flavorizr.yaml`、iOS Base.xcconfig、Info.plist、Android applicationId 和桌面配置均未变。这直接证明现阶段“执行 apply”不等于“得到可安装白牌”。

权威代码：[mobile generator](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/scripts/brand/generators/mobile.py#L1)、[flavorizr](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/app/flavorizr.yaml#L1)、[iOS Base](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/app/ios/Flutter/Base.xcconfig#L1)、[Android build](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/app/android/app/build.gradle#L54)。

### E2：品牌 schema 与部署 profile 的域名契约冲突

[`render.py:173–178`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/scripts/profiles/render.py#L173) 读取 `domains.auth_base`、`mcp_base`、`objects_base`；[`manifest.schema.json:53–67`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/brand/_schema/manifest.schema.json#L53) 没有这些属性且 `additionalProperties:false`。

合法 fixture 分别运行两个 target 的 `render.py --emit-json` 都返回 0，但生产配置为：

| 字段 | self_hosted.production | cloudflare.production |
|---|---|---|
| `api_base_url` | `https://api-base.audit.example/` | `https://api-base.audit.example/` |
| `auth_base_url` | 自动回落到 API origin | 自动回落到 API origin |
| `objects_base_url` | 空字符串 | 空字符串 |
| `auth_callback_scheme` | `audit-weft` | `audit-weft` |

分别添加 `auth_base`、`mcp_base`、`objects_base` 后，schema 全部报 `unknown key(s)`。同 origin 认证可以是合法拓扑，空 objects 地址也可能表示经 API 代理；问题是当前模型无法明确表达和验证两种部署需要的拓扑，并且将“未填”静默生成“可通过”的值。`check_tables.py:105–145` 只验证少量结构及当前选中的生成 target，没有验证可用地址或消费者接线。

行动上应确定 endpoint 的唯一权威：品牌提供稳定展示身份，target + stage deployment 配置提供实际服务地址（或正式支持 schema 中明确的可选/必选 domain 字段），用显式代理能力解释无直连对象地址的情况。不能继续一边拒绝字段、一边暗中读取字段。相同品牌同时部署两个 target 的 staging 必须可以使用不同 origins。

### E3：品牌检查存在可复现假绿与覆盖缺口

- [`apply.py:116–129`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/scripts/brand/apply.py#L116) 只比较前后的 `git status --porcelain`。fixture 已有 ` M app/lib/flavors.brand.dart` 时，故意写入 `CORRUPTED` 后执行 `--check-clean`，文件被重写，但前后仍是同一个 ` M`，**返回 0**。检查应比较预期字节与实际字节，并在 check 模式不写文件；单靠状态字符串不足以证明一致性。
- `check.py --brand omi-upstream --json` 返回 0、词表匹配 **9,581**。这是上游自检设计行为：上游叫 Omi 不算泄漏，服务标识扫描在该模式直接跳过。它不能证明其他品牌零泄漏。
- 对未改动的完整工作树运行真实扫描函数，非上游品牌模式得到 **9,581 次词表匹配 + 18 次服务标识匹配**。其中 ARB 9,164 次、macOS literal 103 次、后端 prompt 180 次等。它们是源文件匹配次数，可能重复或包含注释，不是 9,599 个独立缺陷，也不是白牌构建后的实测泄漏数。
- [`check.py:57–75`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/scripts/brand/check.py#L57) 完全没有 `web/app` glob；`windows_source` 使用 Swift literal 正则（`:160–161`）。实际注入 `<h1>Omi</h1>` 与 JS URL 字符串的 TSX fixture 后，候选字符串列表为空。当前 scanner 也不证明图标/屏幕、安装元数据、构建产物、运行时导出的 prompt 和 OpenAPI 安全。

原始统计和复现器见 `fixture-summary.json`、`fixture_probe.py`。这些实测故障是 WL-1 回归测试的依据；保持上游许可/历史记录等明确豁免，不做整库盲目字符串替换。

### E4：客户端未消费 profile，且 macOS 新品牌身份会落入错误类别

**移动端真实选择器：** `/tmp/profile_probe.dart` 直接 import 主线 `app/lib/env/environment_profile.dart` 并执行 `AppEnvironmentProfile.forFlavor(productionFlavor:true)`：

- `dart -DOMI_APP_PROFILE=self_hosted.production ...`：exit 255，`Unknown OMI_APP_PROFILE`。
- `dart -DOMI_APP_PROFILE=cloudflare.production ...`：同样 exit 255。
- `dart -DOMI_APP_PROFILE=production ...`：exit 0，输出 `production https://api.omi.me/ omi`。

这不是源码字串断言；执行了真正的生产选择函数。四端 `forkDeploymentProfiles` / `ForkDeploymentProfiles` 的全路径搜索只发现生成表内定义。移动端已有 `AuthProvider` Better Auth 路径，但 [`auth_provider.dart:65–74,162–200`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/app/lib/providers/auth_provider.dart#L65) 是带 dev issuer secret 的非 release 桥，不是发行登录实现。macOS/Windows/Web 仍走各自 Firebase 路径。

**macOS 真实身份函数：** 用 `xcrun swiftc` 直接编译主线 `AppBuild.swift` 与临时 harness，未启动应用。`AppBuild.configuration()` 实际输出：

| bundle ID | nonProduction | named | automation | sparkle |
|---|---:|---:|---:|---:|
| `com.omi.omi-audit` | true | true | true | false |
| `org.audit.weft-dev` | false | false | false | true |
| `org.audit.weft` | false | false | false | true |
| `com.omi.computer-macos` | false | false | false | true |

新 namespace 没有被判为明确的生产族，却也不再被识别为 named dev，因此自动化被禁、更新被允许。原因在 [`AppBuild.swift:6–54`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/desktop/macos/Desktop/Sources/AppBuild.swift#L6)。同一 harness 调用 `manualDownloadURL` 仍返回上游 API。main 的 `derive_omi_app_config audit-weft` 还会直接 exit 1 拒绝非 `omi-` 名称。

因此必须一次贯通 shell 身份派生 → Info.plist → runtime 生产族/preview/named 分类 → 数据目录/keychain → OAuth callback → 更新 feed/签名；PR #10 即使恢复可合并也只解决其中一环。

### E5：身份之外的真实用户触点尚未接线

- 移动端 `Runner*.entitlements` 仍含上游 App Group、`applinks:h.omi.me` / `try.omi.me`；`Info.plist` 使用 `$(AUTH_CALLBACK_SCHEME)`，而 `environment_profile.dart` 仍硬编码 `omi/omi-dev/omi-beta`。改其中一侧会造成 OAuth 回不到应用；Widget/Watch 等共享容器不能遗漏。
- macOS [`Info.plist:36–71`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/desktop/macos/Desktop/Info.plist#L36) 仍有 Omi TCC 文案、callback、上游 Sparkle feed/key。`DesktopKeychainStore.swift:45–55` 已用 team + bundle 做作用域，不应为了品牌更名破坏已有安全边界；品牌 prefix、target/stage 会话隔离和迁移语义需要一起定义。
- Windows [`electron-builder.config.mjs:30–31,119,190`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/desktop/windows/electron-builder.config.mjs#L30) 固定 `com.omiwindows.app`、`Omi for Windows`、安装文件名及 `BasedHardware/omi` feed。Linux 同配置含 Based Hardware maintainer，且组件 guide 明确 Linux 尚无发行/自动更新闭环。
- Web [`package.json:6–15,27–33`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/web/app/package.json#L6) 是 Moonshine/Bun。`LoginClient.tsx:396–414`、Sidebar、Settings 仍含上游链接；`transcriptionSocket.ts:32` 有上游 WS fallback，`geminiLive.ts:173` 仍自称 Omi。旧 `04/05/07` 中 Next/vinext 和 `next.config.js` allowlist 不能直接作为当前实现依据。
- 后端 [`utils/llm/chat.py:825,922`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/backend/utils/llm/chat.py#L825) 仍 `You are Omi`。桌面/移动文案覆盖后，用户仍可能从 AI 输出、邮件、分享页、下载链接看到上游身份。
- 固件 [`omi.conf:107–109`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/omi/firmware/omi/omi.conf#L107) 固定 BLE/DIS；`nfc.c:94` 为 `friend.based.com/pair`；`app/lib/services/devices/models.dart:12–29` 固定服务/characteristic UUID；后端 [`firmware.py:38–63,102–106`](https://github.com/summersmile1984/omi/blob/d238a85af9d999992d9f0352db682cd9f11fc951/backend/routers/firmware.py#L38) 固定型号与 release tag。新品牌固件不能只改广告名，也不能在未同步客户端解析器时换 UUID。

## 3. 实际执行与 preflight 解释

全部原始日志：`/tmp/memweft-audit-20260904/whitelabel/`。`baseline-results.json`、`fixture-summary.json`、`production-seam-results.json`、`normalized-preflight-results.json` 记录命令、退出码及对应文件。

| 执行项 | 实际结果 | 能证明什么 / 不能证明什么 |
|---|---|---|
| `python3 scripts/brand/test_brand_tooling.py` | exit 0，28 tests | 当前工具测试通过；没有覆盖新发现的 dirty 假绿与完整 native 身份 |
| `python3 scripts/profiles/check_tables.py` | exit 0 | 当前表等价/结构检查通过；未证明四端消费和 endpoint 可用 |
| `python3 scripts/brand/apply.py --brand omi-upstream --check-clean` | exit 0，只写同内容标题文件，其他 7 类跳过 | 上游标题 no diff；不是完整品牌生成验证 |
| `python3 scripts/brand/check.py --brand omi-upstream --json` | exit 0，9,581 自检匹配，服务标识 0 | 服务标识 0 由上游自检跳过产生，不是扫描零泄漏 |
| `make preflight`（默认环境） | exit 2 | 系统 Python 3.9 的 `str \| None` TypeError；基础解释器未满足 gate，非品牌缺陷 |
| `scripts/pr-preflight --suggest`（默认环境） | exit 1 | 同一解释器问题 |
| `scripts/fork/preflight` | exit 0 | 自动选新 Python；base=head、files=0，11 个上游通用检查 + 1 个 fork touch 检查；品牌/profile diff 项未被选择 |
| `make preflight`（下述隔离 Python 环境） | exit 0 | 11 个通用检查通过，files=0；仍不是全产品验收 |
| `scripts/pr-preflight --suggest`（同环境） | exit 0 | 当前无产品 diff，invariants 为 none、无 fix commit；不代表未来身份 PR 无须引用 invariant |
| 临时品牌 apply + 两 target profile render | exit 0 | 重现 E1/E2；只改标题且 objects URL 为空 |
| 临时脏生成物 `--check-clean` | exit 0 | 重现 E3 假绿；输出确实被改写 |
| `bash desktop/macos/tests/test-app-config.sh` | exit 0 | 上游 named 配置契约通过 |
| `derive_omi_app_config audit-weft` | exit 1 | main 拒绝新品牌命名前缀 |
| `xcrun swiftc AppBuild.swift /tmp/.../main.swift -o /tmp/.../identity-probe` + 执行 | 两步均 exit 0 | 实际生产身份函数的分类/更新权限输出；不是 .app 打包或 UI smoke |
| `dart -DOMI_APP_PROFILE=<target>.production /tmp/.../profile_probe.dart` | 两 fork target exit 255；上游 production exit 0 | 实际选择函数拒绝 fork profile |
| `flutter test test/unit/flavors_brand_test.dart` | exit 1，依赖解析失败 | 本机 Flutter 3.38.9/Dart 3.10.8；`flutter_contacts 2.3.1` 要求 Dart `^3.12.0`。未执行到那 2 个 Flutter 测试，不能算通过 |
| `gh pr view 10 -R summersmile1984/omi ...`、`gh run view ... --log-failed` | exit 0，元数据/失败日志已存 | 当前 PR/CI 外部状态；因果边界见 §1.1 |

可复现的 Python 运行环境（仅本轮进程，未改机器默认 Python）：

```bash
export PYTHON=/Users/macstudio/.local/bin/python3.12
export PATH=/tmp/memweft-audit-20260904/whitelabel/python-bin:$PATH
make preflight
scripts/pr-preflight --suggest
```

其中 `python-bin/python3` 是指向上述 Python 的临时链接，让子检查也使用相同版本。`scripts/fork/preflight` 已内置相同思路，后续优先使用它。

共享累计检查由主协调路保存于 `/tmp/memweft-audit-20260904/common/`：`--base fd01c27267` 选择 50 项/898 文件，4 项通过后第 5 项源码行数 ratchet 失败，后续 45 项和 fork 阶段在该组合运行中未执行。随后从这 45 项独立补跑 6 项均通过，仍有 39 项没有本次累计范围的执行结果。补跑包括 `production-data-plane-routing`（12 tests）和 `desktop-auth-session-ratchet`，说明现有上游 guard 通过也未覆盖 E4 的自有 namespace 分类问题。这是累计差异审计、缺少原 PR 元数据，不可称作全量 preflight 通过，也不可把停止处误计成多个产品故障。

**本轮覆盖限制：** 没有构建并安装自有品牌 IPA/APK/.app/EXE/AppImage，没有跑四端登录/录音 UI E2E、真实 OAuth 回跳、AASA/assetlinks、签名升级、BLE 实机或 OTA。尚无完整品牌生成器，Flutter 工具链也与 CI pin `3.44.5` 不匹配；macOS 函数探针用本机 Swift 6.3.3，不能替代 CI Xcode 16.4 打包链。Windows 必须在真实 Windows runner 上验证安装和协议唤起；固件需 NCS 2.9.0 构建环境与硬件。以上均列入下面具体行动包，不能因为功能未实现而宣布验收通过。

## 4. 下一步行动包：按完整契约拆 PR

共同原则：保持 fork-owned 新目录/生成 overlay/包别名为默认；生成变更发生在隔离构建树，不能把上游 generated output、lockfile 或测试改动提交进 main。`upstream-touch-allowlist.yaml` 当前 3 行预算约束的是上游接缝，不应被用来把一个安装身份切成一连串无法独立验收的标题/前缀 PR。若确实无法用既有接缝实现完整契约，应先提供可审查的最小技术证据和上游提案，按仓库规则处理例外，不能绕过 ratchet 或偷偷扩大 allowlist。

| 顺序 / 包 | 主责 | 完成结果 | 依赖 |
|---|---|---|---|
| P0 · WL-1 品牌/profile 输入与验收修复 | 白牌路；两服务端共同定义 endpoint 拓扑 | 任一合法品牌 × target × stage 都能得到明确配置；check 真正只读且按字节判定；缺失实现不可当发布通过 | 可立即开工 |
| P0 · AUTH-1 共享认证权威 | Server 路，CF adapter 配合 | 两 target 的发行登录/JWT/JWKS/refresh/revoke 契约及共同夹具 | 可与 WL-1 并行 |
| P0 · CLIENT-1 四端 profile + 认证/实时消费 | 白牌路；服务端提供契约 | 四端用同一选择和身份语义，真实发行登录取代仅 dev bridge；同步 capability gating | WL-1，AUTH-1 契约；Web 接 WEB-1 |
| P0 · WL-2 macOS 完整身份 | 白牌路 | 新品牌 stable/beta/dev/preview/named 安装、会话/存储/更新行为可验证 | WL-1；认证验收接 CLIENT-1 |
| P0 · WL-3 iOS/Android 完整身份 | 白牌路 | 原生身份、扩展、回调、签名输入和可安装产物统一 | WL-1；认证验收接 CLIENT-1 |
| P1 · WL-4 Electron 双平台身份 | 白牌路 | Windows/Linux 包元数据、协议、存储、显示与 feed 配置一致 | WL-1；认证验收接 CLIENT-1 |
| P1 · WL-5 用户输出、人格和 Web 品牌 | 白牌路；两服务端对称实现输出 | 客户端 UI/链接/资产 + 两后端 runtime 输出可检查 | WL-1/CLIENT-1；Web 打包接 WEB-1 |
| P1 · WL-6 固件/设备/OTA 契约 | 白牌路；Server/CF 固件 API 配合 | BLE/DIS/NFC、UUID策略、型号/version/tag/签名从同一配置贯通 | WL-1；可先用 fixture keys |
| P1 · WL-7 品牌构建与发行验收矩阵 | 白牌路扩展 CI-1 | 品牌 × 两 target × 平台的产物/安装/更新/零泄漏证据 | CI-1、WL-2～6、CLIENT-1、WEB-1 |

这些包可以跨文件、跨原生工程/生成器，判断边界是可独立证明的用户行为，不按单个文件或 3 行额度拆分。**CLIENT-1 的 Web 消费者与 CF-2、WEB-1 组成联合批次 INTEGRATION-1，在同一短期集成 PR 候选树验收。** 前置为 WL-1 schema、AUTH-1 契约/测试服务和可运行的 Server 参考后端，不要求三个互相依赖的部分先各自合并。三部分共用夹具并一起完成双 target Web 登录/实时闭环；原生 CLIENT-1 再按平台独立交付，每个平台仍须对两个 target 完整可用，不能再交付“先生成表、以后接线”。四端整体完成状态要等所有平台均通过。

### WL-1：先让输入与检查值得信任

- **范围/产物：** 明确品牌展示域与 deployment endpoints 的归属，修 schema/renderer 冲突、空地址和 stage/target 表达；实现内存渲染/逐文件字节比较；将 `supported/rendered/skipped` 输出覆盖写入结构化 manifest，发行 gate 对必要生成器缺失失败。上游回归 lane 仍可保留明示的骨架自检模式。
- **错误路径测试：** 不存在品牌、目录 ID 与 manifest ID 不一致、非法/缺失 endpoint、跨品牌/跨 target 回调混用、脏输出、未跟踪输出、缺失文件、连续执行幂等；TSX 文本/属性/URL 和 Web 路径 scanner 的真实负例。使用本轮 E2/E3 复现器作为回归起点。
- **门禁：** `python3 scripts/brand/test_brand_tooling.py`、profile 检查、fixture A/B × 两 target/stage 的表与所有声明输出一致；check 模式运行前后内容 hash 不变；相关检查登记在现有 `checks-manifest.fork.yaml` 两 lane，由 Fork Checks 执行。
- **依赖/限制：** 不需要真实品牌名/证书。不得通过“全源代码无 Omi 字串”的规则误伤 LICENSE、历史记录、第三方兼容协议名；零泄漏目标针对明确列出的发行可见面和网络目的地。

### CLIENT-1：四端共同消费 profile，闭合真实登录和实时链路

- **范围/产物：** Flutter Env/认证、Swift endpoint/auth owner、Electron main/preload/renderer、Web browser/server proxy 读同一 target/stage 结果；AUTH-1 统一 owner identity、access/refresh、JWKS、注销/撤销；原生 OAuth scheme、Web origin/cookie、实时首帧认证一起接线。发行构建不依赖 dev issuer secret，也不能把 legacy Firebase fallback 暗中当发行兜底。
- **错误路径测试：** 未知 profile、missing config、错误 issuer/audience、过期/撤销 session、刷新失败、错误 nonce/state/回调 origin、不同品牌或 target 的 token 混用、实时重连和过期首帧；保留上游默认模式与 legacy principal 的预期行为。capability 禁用项必须通过实际 provider/UI 路径验证，不能只检查生成表布尔值。
- **门禁：** 四个平台分别针对同一 auth/realtime 契约 fixture 测核心成功和主错误路径；每个平台在两个 staging 上完成登录→录音/实时→对话→记忆→导出→退出→重登，验证数据 owner 一致和会话隔离。使用网络证据证明白牌发行 profile 不再发送到上游 Firebase/API/遥测默认值。
- **依赖：** AUTH-1 由 Server 路定义权威，CF 提供同契约 adapter；Web 构建方式归 CF 主责 WEB-1。Web相关实现按 INTEGRATION-1 联合候选验收，依赖稳定输入而非对方已完成合并；本路拥有消费者，不能复制一份 CF 专属登录协议。

### WL-2：macOS 打包和运行时身份作为一个交付单元

- **范围/产物：** 统一 shell/Swift/Info.plist 的品牌身份模型，覆盖 stable/beta/dev/preview/named、显示名/二进制名/TCC 文案/URL scheme；生成 feed/public key/artifact names；数据目录、single-instance、team+bundle keychain、迁移来源明确。`create-omi-beta-variant.sh` 的 API 地址读 deployment profile，Sparkle 读 distribution；不另造一套 URL 配置。
- **错误路径测试：** 新品牌 named 必须被判 dev 且禁 shared updater；新品牌 stable/beta 必须准确归生产族；preview 禁 automation；错误/缺失身份、错误 feed/key、跨品牌/跨 stage 存储、原有 stable/beta/legacy principal 保持预期。直接扩展本轮 E4 的真实函数 seam，保留 fork 模式测试与上游模式测试分离。
- **门禁：** 先修/明确 PR #10 的冲突和独立 suite 红灯，再集成完整身份实现；运行 app-config、Swift 身份/keychain/update tests、组件 debug compile；用**唯一且符合当时已实现规则的 named bundle**完成安装、启动、显示、回跳、登录、退出 smoke；发行候选另跑现有 signed artifact smoke，核验 `codesign`、plist、feed/public key 和副本并存。
- **依赖/外部输入：** 通用实现与测试使用 fixture identity，真实签名/公证需要自有 Team/证书；不能碰 `/Applications/Omi.app` 或 Omi Beta。没有真实证书时如实标记 signed release gate 未验，不能冒充发行完成。

### WL-3：移动原生身份、扩展与 OAuth 原子迁移

- **范围/产物：** 在隔离 build overlay 生成 flavor 配置、bundle/application IDs、开发/发行名称、图标/splash、权限文案、App Group/associated domains、Widget/Watch/其他 native 扩展引用、Android namespace/manifest resources、scheme 与 Dart profile 同源。检查 `flavorizr` 的生成闭包，不提交大批上游工程生成 diff。
- **错误路径测试：** 不同品牌/阶段不能共用错误 App Group 或回调；扩展与主应用 ID/entitlement 一致；回调 scheme 大小写/后缀冲突、错误 AASA/assetlinks、错误签名身份失败；legacy 上游构建默认值不变。
- **门禁：** 使用 CI pin Flutter 3.44.5（先确认其 Dart 满足锁定依赖）并安装必要平台 SDK；`app/test.sh` 和 analyzer；对生成后的 Xcode/Android 真实工程分别 build。simulator/debug 可以先验证图标、显示名、权限与假 OAuth callback；最终在设备验证发行 callback、Universal/App Links、共享容器、安装/覆盖升级与双品牌共存。
- **依赖/外部输入：** WL-1 + CLIENT-1；真实 AASA/assetlinks 域名、App Store/Play 身份和签名是发行门，fixture 的本地回调/构建检查现在即可做。

### WL-4：Windows/Linux 安装身份和升级边界

- **范围/产物：** fork-owned builder 配置/入口消费品牌；appId/productName/exe/installer/icon、协议注册、UserData 路径、安全存储、托盘/应用菜单、卸载标识和 feed 同源；Linux `.desktop`/AppImage/deb 元数据明确。保留现有 `--publish never` 安全约束，发布由专属 workflow 明确执行。
- **错误路径测试：** 两品牌并存、错误 callback scheme、错误 package/feed 组合、跨品牌缓存/会话污染、更新签名或 metadata 不匹配；Linux capability 缺失（如全局快捷键/portal）按既有产品规则显式处理，不能伪称与 Windows 同等支持。
- **门禁：** Node 使用 package 声明的 22.x、pnpm 10；typecheck/lint/test，Windows runner 真实打包后安装、协议唤起、退出/更新 smoke；Linux runner 构建+Xvfb/实际会话验证。Linux 若本期发行，须有对应发布与更新/手动更新的完整用户路径和产品说明，不能仅产出 .deb 便标完成。
- **依赖/外部输入：** CLIENT-1；Windows 签名身份用于发行阶段，unsigned 测试产物和协议隔离先做。

### WL-5：品牌可见输出、AI 人格和当前 Web 运行时

- **范围/产物：** Flutter l10n delegate/覆盖层、Swift/Electron/Web UI/图标/链接、权限/通知标题、法务支持入口；两后端相同 persona/邮件/分享/OpenAPI/下载地址输出；manifest 中 analytics/provider capabilities 关闭时真实停止发送。Web adapter 跟随当前 Moonshine/Bun，由 WEB-1 完成双 target 打包；本包提供品牌输入和页面输出验证。
- **错误路径测试：** 本地化 fallback 不回落到上游产品文案/域名；社区/市场/遥测禁用后不渲染入口或调用 provider；AI 自称和支持链接来自品牌，缺失模板/资产给出明确失败；保留 MIT/上游作者及第三方许可归属。
- **门禁：** 对构建产物和 runtime 导出 prompt/HTML/OpenAPI/通知 payload 扫描；关键 UI 新安装 smoke 截图/语义树（iOS/Android/macOS/Windows/Web）+ 支持/分享跳转检查；两后端跑相同 persona eval 与用户输出契约。源码 scanner 只作补充，不能替代图标、运行时和打包输出。
- **依赖/外部输入：** 功能可用测试 assets/文案实现；正式 logo、字体授权、法务文案、域名最后作为 overlay 输入。`INV-UI-1` 的禁紫规则继续适用。

### WL-6：设备广告、解析与 OTA 发布身份

- **范围/产物：** 固件 brand.conf/header、BLE/DIS/NFC、客户端发现/型号映射、Server/CF 固件 API、release tag/asset 约定及签名输入同源。UUID 可以明确沿用上游互通值；若更换，客户端和固件同一交付契约同步，不创建隐蔽兼容层。
- **错误路径测试：** 未知型号、品牌/tag/asset 不匹配、低于最低 app version、错误 OTA 签名/损坏包、NFC URL/设备 ID 编码异常；不得使用上游仓库中的签名材料作为自有发行密钥。
- **门禁：** 先以临时测试 key 构建 NCS/MCUboot 产物并跑型号/tag/manifest 合同；最后真实设备验证广播→配对→录音→NFC→OTA→升级后继续配对，错误签名被拒绝。两 target 返回同一品牌 OTA metadata 形状与授权边界。
- **依赖/外部输入：** 硬件和自有发行 key 用于最终 gate；通用生成器、解析器和测试 key 构建可先做。

### WL-7：品牌 × target × 平台验收与发行矩阵

- **范围/产物：** 在 CI-1 的两 target 共用 contracts 之上增加品牌/平台构建矩阵、产物清单和 provenance（代码 SHA、brand/profile digest、target/stage、工具链、签名状态）；先固定 `omi-upstream` 回归 + 至少一个无 Omi 默认值的 fixture 品牌，正式品牌加入时只新增配置。必要时用第二 fixture 品牌证明隔离。
- **错误路径测试：** 缺品牌 overlay、缺必要生成器、缺 artifact、错误 profile、串用 signing/feed/包 ID、扫描到上游网络目的地、缺硬件/签名/账号时，矩阵报告明确 `not run/blocked` 并阻止相应发行，不能靠 skip 汇总成全绿。
- **门禁：** 每个计划发行单元都有可安装件、安装/启动/登录/录音/对话/记忆/导出证据，协议回跳与升级验证；无密钥 PR 跑全部 hermetic fixture/unsigned build，受保护发行环境补 signed smoke 和两 staging 运行记录；合一次 upstream 后完整复跑。
- **依赖/发布边界：** CI-1/CLIENT-1/WEB-1 和对应平台包。新构建/发行工作流属于 release/CI 高影响改动，按仓库规则评审并取得发布操作授权；本轮只制定方案，不创建生产资源或发布版本。

## 5. 三路接口与推进顺序

| 共享包 | 唯一主责 | 本路交付/消费关系 |
|---|---|---|
| AUTH-1 | Server 路 | 共同身份语义和 fixture 为客户端唯一契约；CF 实现 adapter，本路实现四端消费者 |
| CLIENT-1 | 白牌路 | 两 target profile/auth/realtime/capabilities 客户端接线，服务端不得要求独立客户端分叉 |
| WEB-1 | Cloudflare 路 | 当前 Moonshine/Bun Web 源码的双 target 打包；白牌路提供品牌/域名/链接输入与页面验收 |
| CI-1 | 两服务端共同 contracts，各自 runner | 本路扩展品牌 × 平台产物矩阵，四端连接两 staging；不复制不同接口预期 |

推荐先并行 WL-1、AUTH-1、WEB-1 的基础工作；WL-2/3/4 的身份生成可用 fixture 同时开发。共同 auth/profile 夹具稳定后，INTEGRATION-1 联合完成 Web 消费者、实时服务和双 target Web工件，原生 CLIENT-1 按平台跟进；WL-5/6 贯通用户输出/设备；最后 WL-7 把前面已有测试和产物接成发行门。不要等待商标/账号才开始通用工程，也不要在两个服务端契约未一致前各写一份客户端 shim。

每个实际实现 PR：先重读对应组件指南与 `PRODUCT.md`，用 `scripts/pr-preflight --suggest` 从真实 diff 取得全部 invariant 引用；若是 fix，声明并验证 `Failure-Class`。验证命令、实际结果和未跑范围写进 PR body；本轮无产品 diff 的 `none` 不能复制过去。

## 6. 外部输入与可立即完成的工程任务

| 输入 | 真正阻塞的最终步骤 | 现在可先完成 |
|---|---|---|
| 正式品牌名、法人、logo/字体和文案 | 商店/正式 UI 物料及法务页面定稿 | fixture 品牌、schema、生成器、负例测试与隔离检查 |
| 自有域名、DNS、证书、AASA/assetlinks 托管 | 公网 OAuth/深链/分享入口验收 | endpoint 模型、回调一致性、模拟失败路径、localhost 验证 |
| Apple Developer Team、App IDs/扩展 IDs、发行证书、公证与商店账号 | iOS/macOS 正式签名和上架 | simulator/debug 配置生成、身份分类/权限/存储测试 |
| Android 签名与 Play 应用身份；Windows 代码签名 | 签名安装包和发行升级 | unsigned/debug 打包、协议与数据隔离测试 |
| 自有 OAuth provider apps 与允许回调列表 | Google/Apple 等真实第三方账号登录 | AUTH-1 共同 mock/provider seam、token/refresh/revoke 及安全负例 |
| Sparkle/Windows update 发行 endpoint、签名材料 | 真实签名升级和 feed 校验 | fixture feed/key、错误品牌/渠道/签名拒绝测试 |
| 固件硬件、MCUboot 自有发行 key、产品型号 | 实机 BLE/OTA 与正式固件发布 | 测试 key、NCS build、版本/型号/资产契约与仿真夹具 |
| 正式遥测项目、服务支持渠道或明确关闭选择 | 正式 telemetry/support 链接 | 默认关闭/空入口行为、无上游请求验证 |

生产密钥只以 `env:`/`secret:` 引用进入配置，原始日志不记录密钥或用户数据。保留上游开源许可与作者归属不属于品牌泄漏。

## 7. 本路完成判据与当前勾选

- [x] 以统一 main 的明确 SHA 读取代码并核对 PR #10 外部状态。
- [x] 执行 brand/profile/preflight；记录退出码、实际选择范围与覆盖限制。
- [x] 用隔离新品牌验证真正生成范围，复现 schema/guard 缺口。
- [x] 直接运行移动 profile 与 macOS 身份生产函数，证明关键接线问题。
- [x] 给出完整身份/认证/输出/设备/CI 行动包、责任边界和可先做的工程工作。
- [ ] 自有品牌客户端产品完成：等待 CLIENT-1、WL-1～7 和服务端 AUTH-1/WEB-1/CI-1 对应验收。
- [ ] 四端 × 两 target 的发行闭环、signed 安装/升级、设备 BLE/OTA：本轮没有满足，必须以真实产物和运行证据关闭。

本文件是可执行的审计与下一步方案，不是白牌发行通过证明。
