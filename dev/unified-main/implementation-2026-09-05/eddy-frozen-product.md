# Eddy 冻结 Cloudflare 工件验收

2026-09-05。基线 `bd184e1e06` 加本次 fork-only 改动。没有发布 Worker、
上传应用密钥或执行远端业务迁移。

## 已执行

`local-target.mjs --candidate` 现在复用已验证候选中的全部 8 个 Worker、
Python 依赖、Web 静态文件和 Auth/App SQL。应用文件直接复制，不重新编译。
每次运行拥有自己的本地 D1、R2、DO、Queue、端口、密钥和进程组。
品牌名、persona、联系邮箱、固件策略来自候选配置。模型与 ASR 是受控 IO；
服务、认证、队列和数据操作执行实际应用代码。启动及验收结束再次校验原候选
和复制的模块、资产、SQL；无本地对应契约的已启用迁移/混合策略不会被静默关闭。

两次 canonical `release.mjs prepare` 均成功：111 个 Cloudflare 测试文件、
872 个测试，Core 415、AI 131，以及 Web 检查、两目标构建、SQL 和所有 Worker
的源码/冻结上传 dry-run。这里的 dry-run 不代表已部署。

- 候选 i：`f6c9bf203e3ef7b55c2c2ce652afd3fe690c07f371ca986287bc9469d234d6bb`。
  冻结运行 a 通过 core 8、recording 6、chat 11。
- 候选 j：`6a4420dac66d66bb8c0718f2fceefeb0bc1fe87f418cf68e2501a18cfafc0790`。
  冻结运行 b 通过 core 8、recording 6、chat 11、share 5，共 30 项。
  Web `/login` 为 200；任务/聊天匿名预览经实际 Web→Edge service binding
  返回 200，缺失 token 返回 404，预览保持 `private, no-store`、删减字段。
  `/favicon.png`、`/logo.png` 返回的字节与冻结文件一致。

命令使用 Node 22 和显式已有 Pyodide cache：

```sh
node deploy/cloudflare/contracts/local-target.mjs \
  --output /Users/macstudio/.codex/eddy-production/frozen-product-20260905-b \
  --candidate /Users/macstudio/.codex/eddy-production/candidate-20260905-j \
  --run-core --run-recording --run-chat --run-share
```

执行环境的 `CLOUDFLARE_PYODIDE_CACHE_DIR` 为
`/tmp/memweft-implementation/cloudflare/runtime-cache`。私有目录中的 `fixture.json`
记录命令、工具、配置和文件哈希，`trace/*-results.json` 记录各用例结果。
全部报告 `release_qualified: false`。运行前后的 source/artifact 核验均成功，
进程正常退出并清理自己的两个 Wrangler 会话。后续文档、用例命名及提交改变
source identity；j 不能代替下次发布的重新 prepare。

复制和策略边界的 11 项本地测试通过，已在现有 Cloudflare Vitest lane 执行。
`check-upstream-touch.py --base upstream/main` 通过，仅保留原有 2 个允许钩子。

## 发现的白牌遗漏

实际打开候选的 `workers/web/assets/logo.png`，它仍是上游 Omi 的环状圆点图案，
并非 Eddy 的图形。Web builder 的 `profile_input.py` 目前只投射品牌文字和
profile，`build.ts` 复制 upstream public 文件，没有应用 manifest 的图片资产。
因此静态文件哈希检查仅证明交付完整性。运行 b 的旧用例名包含 `brand-assets`；
本次提交将其改成 `static-assets`，避免误解为品牌身份验证。

后续工作由当前 Eddy 生产目标继续承担：在共享 Web 构建阶段按 manifest 应用
品牌资产并验证两目标产物；补齐 CF-4 的导出、删除、MCP、对象、持久化和错误链路；
完成 CI-1 双目标验收，再通过 canonical release 发布并验证实际线上模型、
Vectorize 和 Eddy macOS 客户端闭环。Web 浏览器 API/Auth 地址仍是生产配置的
烘焙值，本地 SSR/分享测试不宣称浏览器生产登录已成功。
