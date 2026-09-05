# Eddy Web 图片和页面元信息实测

2026-09-05，基线 `fe4dc1c335` 加本次 fork-owned Web 构建改动。
已修复上一轮观察到的 `logo.png` 上游图案，并在实际浏览器检查时进一步修复了
`Sign In to Omi` 标题与默认 Omi 描述。此记录不代表已部署生产 Worker。

## 实现

- 共享 Web 构建读取品牌清单的 `icon_master` 和 `logo_light`，复用已有
  `scripts/brand/raster` 的 PNG 校验与缩放，产出 64px favicon 与 512px Logo。
  两目标使用同一代码，manifest-relative 输入和输出哈希写入 build manifest。
- 原始上游 Web 文件保持不变。图片仅替换到临时 source stage；生成的 server
  仅改写已知静态品牌标题和描述。动态应用名称、用户描述与 API 域名保持其数据语义。
  HTML 转义仍由原 renderer 执行。无 tagline 的有效旧清单使用产品名作为描述。
- `omi-upstream` 回归身份保留原始图片和元信息。其他品牌缺失/损坏图片会失败。
- canonical prepare 对显式私有清单先冻结清单及消费的 PNG，再构建两个目标。
  PNG 依赖在现有 Web local/CI 检查入口安装，不成为 Cloudflare 模块加载的隐式前提。

## 证据

候选 k 完成准备及 Cloudflare share 5 项；两个目标实际返回的图片字节一致，
图形已是 Eddy 的开放 e。随后启动真实 Bun Web，在浏览器发现表单名称已是 Eddy，
但 HTML 标题仍是 Omi；这项失败触发了本次元信息改动。

候选 l 在完整测试中发现旧品牌 fixture 没有可选 tagline，准备失败；已修复，
原 fixture 不变，专门验证空标语仍可生成有效品牌标题/描述。

最终候选 m 通过 `release.mjs prepare --manifest .../brand/eddy/manifest.yaml`，
实际执行了私有清单快照入口，候选摘要为：

`65a66d0666fefe89c7a8bf81cb391c22abf9a58254f8971295a67ab04648e288`

- Cloudflare：111 文件 / 872 测试；Core 415、AI 131；Web 类型检查、6 个构建
  边界测试、双方完整构建、SQL fixture 和 8 个 Worker 的源码/冻结上传 dry-run。
- `/Users/macstudio/.codex/eddy-production/frozen-product-20260905-d` 从 m 的
  原始工件启动 8 个本地应用 Worker，share 5 项通过，正常退出并清理自有进程。
- 同时启动 m 的真实 Bun `artifact/start.js`。两个运行时均实际 GET `/login`：
  200、`<title>Sign In to Eddy</title>`、描述 `Eddy - 懂你的随身AI伴侣`。
  各自返回的 `/logo.png`、`/favicon.png` 均等于清单生成哈希，且跨目标字节一致。
  私有报告：`/Users/macstudio/.codex/eddy-production/web-brand-20260905-b/proof.json`。
- Codex 浏览器实际打开 Bun 登录页：标题与正文品牌名均为 Eddy；点击
  `Create an account` 后显示 Name/Email/Password 注册表单。未提交任何凭据。
  验证结束后关闭临时 tab 与自有 Bun 进程。

图片哈希：Logo `edceacd60ab165ea331a684d3e407b9548b2010203954f0cc47dfd032de4eda8`；
favicon `b33ac728135071242cd3b49c219b0bf37d4328f8738767408edeaa39a36c9347`。

## 剩余边界

这是两个本地运行时的 Web 图片/元信息证据，不是生产认证、远端模型或完整双目标
业务闭环。内联图形、其他产品字符串和所有客户端界面仍需各自的白牌验收。
CF-4、CI-1 完整业务发布门仍未完成，未发布生产 Worker，未上传应用密钥或迁移业务库。
后续文档、回归身份 receipt 表述和提交会改变 source identity；m 不能直接用于发布，
后续必须重新 prepare。所有运行报告继续保留 `release_qualified: false`。
