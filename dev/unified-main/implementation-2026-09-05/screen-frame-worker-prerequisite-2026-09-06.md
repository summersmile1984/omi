# Cloudflare 截图隐私：原版图像处理的运行时验证

本轮完成截图处理的构建前置工作。正常 API Core 构建已包含原版截图类型、
图像规范化、调色板、隐私提示词和保留顺序；固定版本的本地 Python Worker
实际执行了原版图片处理。**八条截图隐私路由仍未接入**，路由台账继续保持
619 条注册路由、586 条 staging-owned、33 条 blocked。

## 实现范围

`deploy/cloudflare/scripts/screen_frame_sources.py` 从上游源码生成六个临时
普通 Python 模块。类型和规范化实现逐字节复制；调色板及策略只替换类型
导入路径；隐私提示词和保留顺序提取原有语法树表达式。缺失表达式、类型
导入变化或目标模块冲突均拒绝构建，不覆盖工作区文件。没有修改上游源码。

正常 Python source stage 调用该编译器；部署候选的源文件身份包含这些
后端所有者，现有 Cloudflare 路由 CI 的触发范围同时覆盖它们。Core 自有
依赖锁固定 Pillow 11.3.0，使用 Pyodide 0.28.3 官方包索引的 wasm wheel
和 SHA-256。默认聊天、工具、记忆提取及截图隐私提示词没有修改。

## 验证证据

基线提交 `636cc9ee87`，分支 `codex/unified-delivery`。使用 Node 22、
Wrangler 4.127.0、workerd 1.20260826.1、workers-py 1.16.7、uv 0.12.3。

- `node scripts/python-worker.mjs api-core sync`：成功，依赖准备后锁文件
  保持不变。
- `node scripts/python-worker.mjs api-core deploy --dry-run --outdir <private>`：
  成功。产物包含六个普通 `screen_frames_*.py` 文件及 Pillow metadata；
  类型和规范化模块与原版逐字节一致。没有远端发布。
- `bash deploy/cloudflare/ci/routes.sh`：成功，路由清单 6 个测试、全部
  619 条注册路由对照、类型检查、Workers 112 files / 896 tests、
  Core 549 tests、AI 150 tests 通过。此时已包含新截图源码行为测试。
- 新测试实际运行生成后的 codec、类型校验、提示词表达式和截图保留算法；
  与独立执行的原版表达式比较提示词，检查损坏图片拒绝和阶段冲突。
  发布源身份测试另覆盖上游类型/策略变化使候选失效。
- 最终运行 `vitest run tests/screen-frame-source.test.mjs
tests/python-worker.test.mjs tests/release-files.test.mjs`：3 files / 16 passed，
  包含补充的发布源身份测试；日志 `screen-frame-source-tests-20260906-d.log`。

在私有独立探针内，通过固定版本的真实 workerd/Pyodide HTTP 请求执行原版
canonicalizer 和 palette，结果如下。输入均为合成图片，没有用户屏幕内容。

| 输入 | 真实结果 | 本地耗时 |
| --- | --- | --- |
| 1920×1080 PNG 幻灯片，带元数据 | 200，1600×900 JPEG，17,650 bytes，缩略图 3,851 bytes，元数据已去除 | 0.236 秒 |
| 带 EXIF 旋转的 JPEG | 200，900×1600 JPEG，18,752 bytes，缩略图 4,020 bytes，EXIF/ICC 已去除 | 0.104 秒 |
| APNG 动画 | 422，`animated` | 0.004 秒 |
| 损坏图片 | 422，`decode_failed` | 0.111 秒 |

此探针只证明固定本地 Worker 运行时能执行图片处理；它不是公开截图 API、
真实视觉模型、D1/R2 持久化或 Cloudflare 托管环境的验收。高分辨率输入及
托管 Worker 内存上限仍需验证，不能由这些 1920×1080 用例推定。

私有证据根目录为 `/Users/macstudio/.codex/eddy-production/`：
`screen-frame-core-sync-20260906-a.log`、`screen-frame-core-build-20260906-a.log`、
`screen-frame-cloudflare-suite-20260906-a.log`、
`screen-frame-worker-probe-20260906-a/results.json`。未把原始业务数据、
凭据或私有生成配置放入仓库。

## 尚未完成的业务边界

后续截图路由必须保留上游的默认设置和判断规则：账户截图设置缺省为启用、
会话分享设置缺省为启用；全局 egress 不可用时在模型调用之前拒绝；请求
先校验全部候选的传输摘要；重复 attempt 保留原结果。不能把这些设置改成
另一套产品策略，或通过修改默认提示词让模型通过验收。

同一流程还需要独立的 R2 写入身份、一次性审批消费、并发追加和保留顺序、
当前隐私状态约束的下载、分享撤销、会话/账户删除及晚到写入清理。当前的
共享 ASSETS 绑定没有证明该独立写入边界，不能直接充当已完成的截图实现。
帧请求、记忆账本、完整双目标业务资格及生产发布仍属于当前交付范围。

本轮也只读核对了现有 Server 本地容器：LLM `mimo-v2.5`、ASR
`mimo-v2.5-asr`、TTS `mimo-v2.5-tts` 使用 China Token Plan，embedding 为
本机 BGE-M3/Ollama。真实业务结果及 ASR 质量限制仍以
[MiMo 实测记录](server-mimo-verification-2026-09-06.md)为准。

固定版本 Python/Prettier 格式检查、`git diff --check`、本轮文件上游交集为零、
变更文件无 MiMo 密钥模式检查均通过。四个关键提示词/工具所有者相对
`9b7e48dca2` 逐字节一致。
整个分支已有的 `desktop/macos/docs/desktop-updates.mdx` 上游触碰超预算
没有在此变更中放宽；不宣称整个分支或生产发布门禁已通过。

包来源：[Pyodide 0.28.3 官方索引](https://cdn.jsdelivr.net/pyodide/v0.28.3/full/pyodide-lock.json)、
[Cloudflare Python packages](https://developers.cloudflare.com/workers/languages/python/packages/)。
