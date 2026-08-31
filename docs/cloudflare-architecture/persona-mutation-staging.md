# Persona 创建 staging 边界

截至 2026-08-31，`POST /v1/personas` 已具备 Cloudflare staging owner 的最小闭环。Edge 使用 Better Auth 验证用户后，将请求绑定转发到 Jobs Worker；Jobs 不读取 Firebase/Firestore，也不依赖本机临时目录。

## 已闭合的行为

- 通过有界 multipart parser 接受 `persona_data` 和一个图片文件；图片大小、数量、字段、MIME magic bytes 和 JSON payload 均在 Worker 内校验。
- 图片先写入带 uid/app id 的 ASSETS R2 key；D1 `cf_app_catalog.owner_uid` 是 Persona metadata authority，写入失败会删除 staged R2 object。
- Persona id 根据 uid、规范化请求数据和图片摘要确定性生成；相同 multipart 重试返回相同 `app_id`，不会再次生成描述或留下重复对象。
- 生成描述使用 Workers AI；provider 缺失、异常或返回空内容时返回 503，并通过共享 fallback telemetry 记录，不写入半成品 catalog row。
- 用户名在 D1 catalog 中做冲突检查；响应保持 legacy `{"status":"ok","app_id":...,"username":...}` 形状。
- Account deletion 已覆盖 `cf_app_catalog.owner_uid` 和 `cf-app-logos/{uid}/` R2 前缀。

## 尚未宣称完成的部分

`PATCH /v1/personas/{persona_id}`、Twitter ownership verification，以及 `/v1/apps/mcp*` 仍由 legacy owner 处理。历史 Firestore Persona 回填、公开目录缓存的 legacy invalidation、完整的 Persona prompt/memory condensation、图片缩略图和生产 cutover 仍待单独闭合；因此本边界只迁移创建入口，不将其它 Persona/app mutation 路径改成 Cloudflare owner。

## 验证

`deploy/cloudflare/tests/persona-mutations.test.ts` 覆盖认证、图片校验、D1/R2 创建、Workers AI 描述调用和重复 multipart 幂等；manifest 与 Edge/Jobs route registration 同步更新。2026-08-31 staging 实测 `POST /v1/personas` 返回 200，完全重复 multipart 返回相同 `app_id`，账号随后通过公开删号清理。
