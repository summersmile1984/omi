# 白牌宪法：apply 替换 + shim 注入（fork-owned 唯一白牌模型）

> 状态：宪法性文件。任何 fork-owned PR 必须遵循本文。任何与本文冲突的旧计划（`04-brand-layer.md` §4 表格中"改源码"路径、`PR #10` 的 `app-config.sh` 改源码路径）作废，由本宪法指定的"prepare 脚本 + brand-stage 脚本 + source-owners + patches shim"四种模式替代。

## 一句话

白牌 = **已有一份固定的上游代码 + 一个 apply 脚本**：前端用脚本就地替换字符串/常量/资源；后端用 shim 在启动时注入替换符号。**fork 永远不动 main 任何源文件**，除了 6 个声明性生成物。

---

## 1. 模型图

```
                  brand/<id>/manifest.yaml
                            │
       ┌────────────────────┼────────────────────┐
       │                    │                    │
   FRONTEND              BACKEND              FIRMWARE
   (apply 脚本，           (shim，启动时           (stage 脚本，
    就地替换)              注入替换)              隔离树替换)
       │                    │                    │
  ┌────┴────┐          ┌────┴────┐          ┌────┴────┐
  │ Flutter │          │ self-   │          │ omi/fw/ │
  │ Windows │          │ hosted  │          │ fork/   │
  │ macOS   │          │ omi-    │          │ stage.py│
  │ Web     │          │ cloud   │          │         │
  └─────────┘          └─────────┘          └─────────┘
  一次跑完              启动 import-time        一次跑完
  生成新包              patch 替换上游符号      生成隔离
  不动 main             不动 main              构建树
```

---

## 2. fork 与上游的 6 个接缝（**唯一允许写入 main 的生成物**）

任何 PR 写入 `main` 的生成文件**必须**只出现在以下 6 个路径之一。其它任何 main 路径上的改动 = 白牌违规，CI 红。

| 接缝 | 文件 | 生成器 | 何时写入 |
|---|---|---|---|
| 1 | `app/lib/flavors.brand.dart` | `scripts/brand/generators/mobile.py` 或 `app/fork/prepare.py` | `apply --brand <id>` 或 Flutter prepare |
| 2 | `app/lib/env/fork/deployment_profiles.g.dart` | `scripts/profiles/render.py` | profile 渲染 |
| 3 | `backend/fork/firmware_brand.generated.json` | `scripts/brand/generators/firmware.py` | `apply --brand <id>` |
| 4 | `backend/fork/deployment_profiles.generated.json` | `scripts/profiles/render.py` | profile 渲染 |
| 5 | `desktop/macos/Desktop/Sources/Generated/DeploymentProfiles.generated.swift` | `scripts/profiles/render.py` | profile 渲染 |
| 6 | `web/app/src/lib/fork/deploymentProfile.generated.ts` | `scripts/profiles/render.py` | profile 渲染 |

**任何其他路径上的 fork-owned 改动** = 走 prepare 脚本（前端）或 shim patch（后端）或 stage 脚本（固件），**产出在隔离 build tree，不进 main**。

---

## 3. 四种合规模式

### 模式 A：前端 prepare 脚本（隔离 build tree）

**适用**：Flutter（Android/iOS）、Windows、macOS、Web、任何前端。

**做法**：
1. `git ls-files <component>` 全量复制到 `--output` 隔离路径
2. `source-owners.json` 用 SHA256 验证每个上游文件字节级一致（上游 sync 后任何人必须跑）
3. 用 catalogue（白名单）做 AST-aware 字面量替换：`brand-stage.mjs` / `prepare.py` 内置
4. raster/icon 替换走 `scripts/brand/raster/png.mjs`
5. 产出 `build-manifest.json`（含 source_owners digest + 漂移路径 + 拒绝条件）
6. 唯一允许写入 main 的就是模式 A 接缝 1

**已在使用**：`app/fork/prepare.py`（Android 已通；iOS 待补）、`desktop/windows/fork/prepare.py` + `brand-stage.mjs`。

**待补**：macOS（新建 `desktop/macos/fork/prepare.py`）、iOS（扩 `app/fork/prepare.py`）、Web（新建 `web/app/fork/prepare.py` + `brand-stage.mjs`）。

### 模式 B：后端 shim patch（启动时 import-time 替换）

**适用**：backend 任何行为变更，包括 prompts/persona/notifications/share/firmware mapping。

**做法**：
1. 在 `backend/fork/patches/<name>.py` 写一个 `@wraps(original)` 装饰器
2. 在 `backend/fork/registry.py` 注册 `Patch(name, module, attribute, build, applies_to, reason)`
3. `applies_to(profile) -> bool` 决定这个 patch 在哪个 deployment target 生效
4. `backend/fork/bootstrap.py` 在 `import main` 之前 `setattr(upstream_module, attribute, replacement)`
5. patch 失败 = `PatchError` 在启动时 fatal（绝不许静默回退到上游行为）

**已在使用**：`patches/auth.py`、`patches/queue.py`、`patches/storage.py`、`patches/llm.py` 等 16 个。

**待补**：prompts/persona（`utils/llm/chat.py:78/827/924` 等字面量替换）、notifications、share_links、firmware 模型映射。

### 模式 C：固件 stage 脚本（隔离 build tree）

**适用**：`omi/firmware/omi/` CV1 源码树。

**做法**：
1. `scripts/brand/generators/firmware.py` 渲染 `backend/fork/firmware_brand.generated.json`（BLE/DIS/NFC/release policy/signing-key 引用）
2. `omi/firmware/fork/stage.py --config <generated.json> --output <isolated>` 把 `omi/` 复制到隔离树，改 `omi.conf` + `nfc.c`
3. 拒绝 pre-existing output / unsafe Kconfig / 缺 `%s` 占位 / NFC URL > 64 字节
4. 隔离树走 NCS/MCUboot 标准构建链
5. 唯一允许写入 main 的就是模式 C 接缝 3

**已在使用**：✅ 已通（缺：UUID 基址、客户端 `models.dart` 同步、新签名 key 真接入）。

### 模式 D：CI/profile 渲染（声明性）

**适用**：deployment profile 的 4 份生成物。

**做法**：
1. `scripts/profiles/render.py --target <t> --stage <s> --emit-json` 渲染 profile
2. CI 在每个 fork lane 跑 `--check` 守生成物字节级一致

**已在使用**：✅ 已通。

---

## 4. 五条铁律（写进所有 fork 路径的 guard）

1. **6 个生成物之外不动 main**。任何 `git diff main` 出现非生成物的改动 → CI 红。
2. **`source-owners.json` SHA 是上游真值的唯一证明**。Flutter prepare、Windows prepare、固件 stage 各一份；上游 sync 后任何人必须跑一遍失败/通过。
3. **每个 patch 必须有 `applies_to(profile)`**。没有 = 在所有 profile 都生效 = 会破坏 omi_cloud 行为 = 不许合。
4. **Patch 失败必须 fatal，不能 fallback**。`PatchError` 在 `bootstrap()` 阶段抛，进程退出。绝不静默回退到上游行为。
5. **测试必须用真实隔离 build tree 跑**，不能在主树上跑（`tests/*.dart.txt` / `tests/*.dart.txt` 的存在就是防止主树 analyzer 误检查 stage-only 代码）。

---

## 5. 反模式（明令禁止）

| 反模式 | 后果 | 应改用 |
|---|---|---|
| 在 main 上加 `Brand.displayName` / `Brand.bundleId` 这种"白牌常量" | 把品牌轴硬编进代码，把白牌从 apply 模型退化成源码模型；上游每次 sync 都冲突 | 模式 A（prepare 脚本写入隔离树） |
| 给 macOS 改 `app-config.sh` / `AppBuild.swift` / `Info.plist` | 持续与上游冲突；PR #10 的现状 | 模式 A：新建 `desktop/macos/fork/prepare.py` |
| 把后端 prompt 中字面量 `"You are Omi"` 直接 grep 替换 | 静默改 main；下次上游 sync 必冲突 | 模式 B：新增 `backend/fork/patches/persona.py` |
| 把 Windows 的 `brand-text.json` catalogue 当全平台通用方案 | 不同平台 AST 规则不同；TS/Dart/Swift/TSX 不能共用 | 每平台各一份 catalogue |
| 用 `git status --porcelain` 做 apply 守门 | audit E3 已证：内容已篡改但 `M` 不变也能假绿 | 字节比对（`apply --check-clean` 已修） |
| 在 fork PR 里新增 fixture 品牌 | fixture 数量爆炸；audit E3 边界消失 | 锁定 3 个 fixture：`omi-upstream`、`audit-neutral`、`eddy` |

---

## 6. 与历史文档的关系

| 文档 | 关系 |
|---|---|
| `omi-white-label-strategy.md` | 上游触点清单（"为什么要做"的层面）；保留作背景 |
| `dev/unified-main/04-brand-layer.md` | 设计稿；其中 §4 表格的"改源码"路径作废，按本文四种模式重写 |
| `dev/unified-main/audit-2026-09-04/03-whitelabel-action-plan.md` | 2026-09-04 审计；其中 WL-1..WL-7 的工程包按本文四种模式重新分片 |
| `omi/firmware/fork/README.md`（本宪法待补） | 模式 C 的具体约束 |
| `app/fork/README.md` / `desktop/windows/fork/README.md` / `backend/fork/README.md` | 模式 A/B 的具体约束 |

---

## 7. 检查清单（每个白牌 PR 必跑）

```
[ ] git diff main -- ':!app/lib/flavors.brand.dart' ':!app/lib/env/fork/deployment_profiles.g.dart' \
     ':!backend/fork/firmware_brand.generated.json' ':!backend/fork/deployment_profiles.generated.json' \
     ':!desktop/macos/Desktop/Sources/Generated/DeploymentProfiles.generated.swift' \
     ':!web/app/src/lib/fork/deploymentProfile.generated.ts' 为空
[ ] python3 scripts/brand/apply.py --brand omi-upstream --check-clean   # B0 回归零 diff
[ ] python3 scripts/profiles/check_tables.py                            # 4 份 profile 生成物 current
[ ] python3 -m unittest discover -s scripts/brand -p 'test_*.py'        # 39+ 项工具测试
[ ] python3 scripts/brand/apply.py --brand eddy && python3 scripts/brand/apply.py --brand omi-upstream  # 切换恢复
[ ] scripts/fork/preflight --base origin/main                          # 完整守门
```

任何一项红 → PR 阻塞。
