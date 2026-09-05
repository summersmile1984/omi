# Cloudflare 生命周期邮件退订验证

本轮实现公开的 `GET /email/unsubscribe` 与 `POST /email/unsubscribe`，并接入
Edge → API Core → Auth / App D1。账户存在性继续归 Auth，Core 持有退订偏好，
Jobs 使用现有账号删除流程清理数据。没有引入第二份账户，也没有发送邮件。

## 行为与密钥

签名格式沿用 `backend/utils/email/lifecycle.py`：无 padding 的 base64url UID、
点号和 `HMAC-SHA256(secret, uid + ':lifecycle')`。整个 canonical token 常量时间
比较，故意不设置过期时间；其他用途、不同 key、非 canonical 编码和畸形输入拒绝。

GET 只确认签名、账户和删除状态，显示按部署品牌转义后的 POST 表单。POST
不读取正文；邮件客户端的 `List-Unsubscribe=One-Click` 和浏览器表单都走同一个
写入函数。签名 token 是唯一身份来源，不借用 app 登录态。响应不重定向，页面
禁止缓存和 referrer 外传。协议依据为 [RFC 8058](https://www.rfc-editor.org/rfc/rfc8058.html)
以及上游 `backend/routers/email_preferences.py`。

无效 token、已删除账户、身份不匹配、服务故障和删除保护都返回相同 400 HTML；
失败响应及 POST 成功响应不回显 token。有效 GET 的表单 action 包含持有者原有
的转义 token。独立的 `LIFECYCLE_EMAIL_SIGNING_SECRET` 纳入 Core 资源契约及示例
清单；生产私有输入已补齐引用，独立 key 保存在仓库外，没有上传到 Cloudflare。
部署时应保留此 key，直接替换会使历史退订链接失效。

迁移 `0160_email_preferences.sql` 建立 `cf_user_email_preferences`，安装 INSERT
及 UPDATE 的账号删除保护。导出读同一行，Jobs 残留注册表清理同一行；不能在
短期删除 tombstone 消失后用旧 token 重建账户偏好，因为还须通过 Auth 的账户
存在检查。

## 已执行的证据

- API Core `uvx uv==0.12.3 run pytest -q`：**463 passed**；新增退订测试 **18** 项。
  使用实际 ASGI 路由、全量迁移后的 SQLite 和验证请求签名的 Auth IO seam，覆盖
  已有账号无偏好记录、GET 无写入、多种 POST 正文、重复退订、账户隔离、导出、
  签名/用途/编码错误、依赖故障和授权检查后才开始删除的竞争窗口。
- Cloudflare `npm run typecheck` 通过；`npm test`：**111 文件 / 884 测试通过**。
  App D1 身份面覆盖、删除触发器和资源计划契约均处于普通套件中。曾误用
  `node --test` 单跑 Vitest 资源计划文件，因缺少 Vitest 上下文退出；正式结果来自
  正常 `npm test`，没有修改测试来绕过该错误。
- 正常源码构建的本地 workerd：`local-target.mjs --run-core --run-recording`。
  共用 HTTP 套件 **12/12**；实际录音、Queue、导出与隐私套件 **14/14**。
  合成账户的退订 token 由独立夹具 key 签发；HTTP 经过真实 Edge/Core/Auth/D1。
  两次 GET 后偏好仍为空，重复 POST 后只有一行，导出可见，另一用户不可见。
  正常账号删除队列后该行从 **1 → 0**，旧链接 POST 返回中性 400，存活账户保留。
  保留正式代码的 60 秒 quiescence 和 30 秒 settling，未通过直接运行删除函数代替队列。
  此后为新表补齐 UID 的 NOT NULL 约束，新增断言并重跑相应组件套件；有效 UID 的业务流程未改。
- 原有 Server Docker 镜像 `memweft-contract-a44e046d403a-api` 运行同一
  `contracts/deployment/core.py`：**12/12**。新增共用用例只证明公开无效退订
  token 的 HTML/400 一致性，不声称 Server 的有效退订本轮也做了完整验收。
- 实际 FastAPI 注册清单仍为 **619** 槽位，现 **583 owned / 36 blocked**。
  Python 固定格式检查、diff 检查、本轮上游零修改检查通过，默认提示词未改。

私有原始证据位于 `/Users/macstudio/.codex/eddy-production/`：
`email-cf-core-final-20260906.log`、`email-cf-vitest-final-20260906.log`、
`email-cf-typecheck-20260906.log`、`email-routes-20260906.log`、
`email-cf-20260906-a/trace/{core-results,recording-results,privacy-storage-results}.json`
以及 `email-server-20260906-a/core.log`。契约追踪会移除退订 query，不保存 token 或 key。

## 边界

本轮没有实现或验证生命周期营销邮件发送、邮件客户端投递、DKIM 或外部邮件
服务集成。录音的模型 IO 仍受控，此结果不覆盖托管模型质量；本地真实 MiMo
推理结果另见其验证日志。Eddy 生产 Worker、远端 SQL 应用、CF-4 / CI-1 完整发布
执行器和 macOS 生产联通仍需完成，不能据此宣称上线。
