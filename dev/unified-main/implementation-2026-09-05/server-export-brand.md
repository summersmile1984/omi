# Server 导出文件名跟随镜像品牌

2026-09-06。v8 修复完整导出后，实际响应仍返回 `omi-export.json`。
本次在 fork ASGI 入口安装 `brand_transport.py`，从已经验证的生成配置读取
品牌 ID，并通过上游实际 `export_all_user_data` 注册定位 GET 路由。
仅在该路由响应 200 时改写 Content-Disposition；认证、完整导出准备、
内容和流式发送均保留原 owner。请求头不能改变品牌，omi-cloud 模式不安装。
品牌允许字符与 `brand/_schema/manifest.schema.json` 的 ID 契约相同。

## 证据

- 现有 owner/startup/source-closure lane：45 项通过。新增用例调用实际
  上游路由，覆盖 Eddy、合法单字符/数字品牌、未认证拒绝、内容完整性和
  omi-cloud 原有文件名；未修改上游测试。
- 正常基础/fork/LLM 镜像重新构建，所有已跟踪 backend Python 的镜像内
  hash 与源码一致；新增模块进入现有 backend COPY 集，启动清单要求它存在。
  `fork-checks.yml` 无目录级事件过滤，现有 manifest 的 `backend/fork/**`
  和 `deploy/self-host/**` 触发项覆盖本包，没有新增游离检查。
- d 轮实际夹具换用 `memweft-contract-184595147daa-api`，readiness 通过。
  未认证 GET 返回 401 且没有下载头；认证 GET 返回 200，下载名
  **`eddy-export.json`**，37,257 bytes 与 v8 导出正文逐字节一致。
- 正文 SHA-256 保持
  `bcf25b89e9ede3d87ab95998834a6f424fb6a3a1e5c0afb3055aa9e035599349`。

私有日志：`server-export-brand-unit-20260906-c.log`、
`server-export-brand-restart-20260906-b.log`、
`server-export-brand-http-20260906-b.log`，以及
`server-export-brand-build-20260906-b/` 中的构建和 source-hash 记录。

这是 Server 白牌导出的真实 HTTP 证据；夹具其他展示名称仍是 Product Fixture。
Cloudflare 发布资格、真实模型的个人历史检索和 Eddy 原生生产闭环尚未通过。
