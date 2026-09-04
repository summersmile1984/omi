# Windows/Linux fork 开发入口

先读同目录 `AGENTS.md` 与根 `AGENTS.fork.md`。当前双 target 本地身份构建入口是
`fork/prepare.py`，包边界、命令与限制见 `fork/README.md`。不要编辑上游 package、
lock、tests、CI 或源码来接入 fork；在 `fork/source-owners.json` 审阅新上游输入后，
只在新建 staging 中应用迁移。它不是运行时猴子补丁或 Firebase custom-token 桥。

`bash fork/test.sh` 为正式 fork lane，必须使用 Node 22/pnpm 10 与 frozen 安装。
`pnpm test` 为独立上游 lane。实际 UI 登录证据与 hermetic tests 分开记录；本机
macOS 上启动 Electron 不能证明 Windows DPAPI、Linux libsecret/kwallet 或安装包资格。
禁止以 plaintext/basic_text、旧上游 profile/session、假 OAuth 或手写 JWT 绕过检查。
本地工件 updater 必须保持 disabled，发布与签名必须另有用户授权与真实资格。
