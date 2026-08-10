# 移动端验证台账

日期: 2026-08-10 · 分支: feature/cloud-neutral-shim · 目标: 移动端 app 运行并连到自托管后端

## 结论

移动端 Omi app(dev flavor)已在 Android 模拟器(emulator-5554, Android 16)成功构建并运行,
Flutter engine 加载、UI 渲染(进入录音界面)。Firebase auth emulator 正确映射
(`127.0.0.1 → 10.0.2.2`)。后端(:8104, Firebase-auth 面)数据链路用 emulator 用户 token
验证:核心端点全 200。

## 构建与运行

```bash
cd app
bash setup.sh ios              # pub get + build_runner + gen-l10n
flutter pub run build_runner build --delete-conflicting-outputs  # 生成 dev_env.g.dart 等
# Android 模拟器(推荐,无 watch companion 问题)
flutter emulators --launch Medium_Phone_API_36.1
flutter run --flavor dev --dart-define=OMI_API_BASE_URL=http://10.0.2.2:8104/ -d emulator-5554
```

**注意**: 
- 后端 :8104 用 Firebase-auth 面(移动端走 FirebaseAuth SDK),desktop 走 BetterAuth 面(:8100)
- Android 模拟器访问宿主机用 `10.0.2.2`(非 127.0.0.1),flutter 自动映射 auth emulator
- iOS 模拟器构建受 watch companion(omiWatchApp)签名/嵌入限制,推荐 Android 模拟器验证

## 验证结果

| 项 | 验证 | 结果 |
|---|---|---|
| **构建** | `flutter build apk` / assembleDevDebug | ✅ BUILD SUCCESSFUL |
| **运行** | app 进程 26452 存活,UI 渲染录音界面 | ✅ |
| **Auth emulator 映射** | `Mapping Auth Emulator host "127.0.0.1" to "10.0.2.2"` | ✅ |
| **认证** | emulator token → 后端 :8104 | ✅ 200 |
| **数据链路** | `/v1/conversations`、`/v3/memories`、`/v1/action-items` | ✅ 全 200 |

## 遇到的问题与解决

1. **Gradle StackOverflowError**(NUL 字符): 清理 `.gradle`/`build` 状态后解决
2. **iOS watch companion**: dev scheme 含 omiWatchApp(watchOS 26.5),模拟器构建受签名/嵌入限制;
   已安装 watchOS 26.5 runtime 但签名仍阻塞 → 改用 Android 模拟器
3. **auth emulator token 空**: 重复 signUp 同邮箱返回空 token → 用新随机邮箱
4. **Gradle SSL 下载失败**: 本机有 gradle-8.14.2 缓存,清理状态后离线可用

## 遗留

- app 本体未在 UI 上完成登录(Google/Apple OAuth 在 emulator 不可用);数据链路已用 emulator
  token 验证 200。完整 UI 登录流需真实 Firebase 或手机号登录接入
- app 的 WebSocket 录音链路(到 pusher)未验证(未登录)
