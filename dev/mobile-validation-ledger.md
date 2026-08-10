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
| **UI 登录**(BetterAuth) | app 点 Better Auth 按钮 → auth-server /auth-issue → 后端 :8100 | ✅ 登录后数据加载全 200 |

## BetterAuth UI 登录(2026-08-10 新增)

dev flavor 登录页新增 **Better Auth (self-hosted)** 按钮(`betterAuthSignInButton`),绕过
Firebase Google OAuth,直接连自托管 auth-server:

- `AuthenticationProvider.onBetterAuthSignIn`: 调 `auth-server /auth-issue`(uid 自动生成)
  → 解析 JWT → 存 `SharedPreferencesUtil.authToken`/`uid` → 标记 `_betterAuthSession` → onSignIn
- `isSignedIn()` 识别 BetterAuth 会话;登出时重置
- 按钮: `app/lib/pages/onboarding/auth.dart`(Apple 与 Google 之间)
- 测试: `parseBetterAuthToken` 纯函数 + 4 单测

**验证**: app 点 Better Auth 按钮 → 后端 :8100 收到登录链
(`/v1/conversations`、`/v3/memories`、`/v1/action-items`、`/v1/users/me/subscription`、jwks)全 200。

## 遇到的问题与解决

1. **Gradle StackOverflowError**(NUL 字符): 清理 `.gradle`/`build` 状态后解决
2. **iOS watch companion**: dev scheme 含 omiWatchApp(watchOS 26.5),模拟器构建受签名/嵌入限制;
   已安装 watchOS 26.5 runtime 但签名仍阻塞 → 改用 Android 模拟器
3. **auth emulator token 空**: 重复 signUp 同邮箱返回空 token → 用新随机邮箱
4. **Gradle SSL 下载失败**: 本机有 gradle-8.14.2 缓存,清理状态后离线可用

## 遗留

- app 的 WebSocket 录音链路(到 pusher)未验证(登录后界面停留在 onboarding 下一步)
- Google/Apple OAuth 仍不可用(emulator 无 Google 账号);BetterAuth 登录为 dev 主路径
