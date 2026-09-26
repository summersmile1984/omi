# Firmware fork：CV1 isolation-tree stage

> 模式 C（见 [`dev/unified-main/WHITELABEL-MODEL.md`](../../../dev/unified-main/WHITELABEL-MODEL.md) §3）。
> 本目录是固件白牌模式的 fork-owned 实现：上游 `omi/firmware/omi/` 字节级不动，本脚本只生成 `backend/fork/firmware_brand.generated.json` 并把上游源复制到隔离 build tree，按生成的 config 改 `omi.conf` + `nfc.c`。

## 唯一入口

```
python3 scripts/brand/apply.py --brand <id> --only firmware
python3 omi/firmware/fork/stage.py --config <generated.json> --output <isolated>
```

`apply.py` 写 `backend/fork/firmware_brand.generated.json`（6 个接缝之一，唯一允许写入 main 的固件生成物）。`stage.py` 只读这个文件，不写 main 任何路径。

## stage.py 拒绝条件（fatal）

| 拒绝条件 | 原因 |
|---|---|
| `--output` 已存在或为 symlink | 防覆盖；stage 必须产出新目录 |
| `--output` 在仓库内 | 防污染 main |
| `config["build"]["nfc_pair_url"]` 缺 `%s` 占位 | 配对 URL 必须含设备 ID 占位 |
| NFC URL 含 fragment / 用户名 / 密码 | 安全约束 |
| NFC URL 替换 `%s` 为 `ABC123` 后 ≥ 64 字节 | CV1 NFC URI buffer 上限 63 字节 + NUL |
| Kconfig 值含控制字符 / 反斜杠 / 引号 | 注入防护 |
| `device.firmware_release_prefix` 不以 `_v` 结尾 | release tag/asset 前缀规则 |
| `distribution.github_releases_repo` 不是合法 `owner/repo` | OTA 元数据来源 |

## 模式 C 的 6 个接缝

| 接缝 | 文件 | 状态 |
|---|---|---|
| 1 | `app/lib/flavors.brand.dart` | 不适用固件 |
| 2 | `app/lib/env/fork/deployment_profiles.g.dart` | 不适用固件 |
| 3 | `backend/fork/firmware_brand.generated.json` | ✅ 已通 |
| 4 | `backend/fork/deployment_profiles.generated.json` | 模式 D |
| 5 | `desktop/macos/.../DeploymentProfiles.generated.swift` | 模式 D |
| 6 | `web/.../deploymentProfile.generated.ts` | 模式 D |

固件相关的 `models.dart`（客户端）和 `routers/firmware.py`（后端）的型号映射在模式 B（shim patch），不在 stage.py 的职责内。

## 未做的部分（已知）

- `transport.c` UUID 基址切换（保留上游互通值时跳过；切值需客户端 + 后端 + 固件三方同步走模式 B + 模式 C）
- 客户端 `app/lib/services/devices/models.dart` 同步 — 模式 B 待补
- 后端 `routers/firmware.py:38-63` 型号映射 — 模式 B 待补
- `bootloader/mcuboot/root-rsa-2048.pem` 删除 + 新 key 经 CI secret 写入 — 需要 NCS 2.9.0 真机验证（外部输入）

## 测试

```
python3 -m unittest discover -s scripts/brand -p 'test_*.py'  # 39+ 项，包含 firmware.py 的 5 个测试
```

`firmware.py` 测试覆盖：
- NFC URL 替换后超 64 字节 → 拒
- release prefix 不以 `_v` 结尾 → 拒
- 不安全 Kconfig 值 → 拒
- stage 不动上游树 → 验证目录 hash 一致
- stage 拒绝 pre-existing output

## 不允许的扩展

- ❌ 在 stage.py 里改 `transport.c` 或 `nfc.c` 字面量（必须通过生成的 config 走）
- ❌ 让 stage 直接调 NCS / MCUboot / west build（stage 只产出 source tree，build 由 release operator 在隔离环境下用 NCS 2.9.0 跑）
- ❌ 让 stage 读 `mcuboot_signing_key` 的真实值（只读 `env:XXX` 引用，不读 key 本身）
