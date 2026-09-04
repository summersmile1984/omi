# AUTH-1 Python HTTP / WebSocket 消费边界

依赖 SH-1 `df7302b752` 和 AUTH-1 core `a5b21e143b`。

原 `endpoints.verify_token` 把 shim 的 CertificateFetchError 转成 InvalidIdTokenError，
并且首消息 WebSocket 路由把泛型认证异常转为 1008。仅在 helper 返回 1013 也不足：
Uvicorn 在 upgrade 前收到 close 会返回 HTTP 403，真实客户端收不到 close code。

本包在 fork registry 中替换 token verifier，保留无效凭据 / 权威服务不可用分类，
包住两个 HTTP uid 依赖和首消息 async 依赖，并给 self_hosted 的 app 安装仅针对
1013 的 upgrade-then-close handler。正常路径继续执行上游账号删除/Cutover fences；
omi_cloud 继续原 Firebase / admin 行为。self_hosted 身份只来自 shim，不接受 ADMIN_KEY 前缀。

## 验证

日志 `/tmp/memweft-implementation/server/`，全部 synthetic 本地 fixture。

- `backend/test.sh`，选择 auth consumer、auth contract、auth shim、real seams 四文件：
  38 tests pass（7 + 23 + 4 + 4）。`auth-consumers-core-tests.log`。
- 已构建最新真实 Linux/amd64 backend+fork image 和 Auth image；
  `auth-base-build.log`、`auth-fork-build.log`、`auth-server-build.log`。
- Auth 缺席时：真实 `/v1/users/profile` 返回 503、
  `detail={code: auth_service_unavailable, retryable: true}`；
  `/v4/listen` header auth 与 `/v4/web/listen` first-message auth 的真实 socket
  都收到 1013，没有 application data。`live-auth-outage.log`。
- 启动单独 Auth+PG 后，真实注册→session→JWT→API uid dependency 返回 200；
  非法 token 返回 401。`live-auth-session.log`。
- 暂停本次专属 Auth 容器，同一枚缓存过公钥且尚未过期的 JWT 返回实际 HTTP 503，
  未用缓存冒充 active-session 成功。finally 恢复容器；
  `live-auth-session-outage.log`。
- 注销后之前有效的 JWT 返回实际 HTTP 401，随后删除 synthetic Auth user；
  `live-auth-session-logout.log`。
- 过期凭据的 ASGI 逻辑分类仍为 HTTP 401 / WS 4001；仅 1013 做了实际 upgrade
  传输修复，未声称其他 pre-upgrade close 已改变。

`Failure-Class: FC-typed-failure-collapsed-to-generic`：消费层把暂时不可达的认证权威误当成用户凭据失效。
已有 startup local/CI gate 扩展为覆盖实际 HTTP/WS consumers；没有新增上游文件编辑。

## 边界

Compose 的 Auth 服务固定 NODE_ENV=production，会拒绝 local profile 的 HTTP loopback。
该生产 guard 的拒绝已实测。上面的完整 Auth fixture 显式覆盖 NODE_ENV=development，
保持随机密钥、真实 PG、实际共享认证代码，属于本地验证，不能当生产 TLS/部署验收。
production/beta 安全 guard 未放宽。stage→Auth mode 的唯一映射另归启动补强。
