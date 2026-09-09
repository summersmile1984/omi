# Eddy 邀请试用：两端实现和验证

本轮完成邀请链接、品牌登录后的领取及 30 天 Operator 权益，并把三个
Cloudflare 路由从 blocked 更新为 staging-owned。619 个上游接口中，
目前 586 个有 Worker 实现，33 个仍待迁移。这个状态不代表已经部署生产。

## 业务和数据所有权

- 保留上游 `ref1` HMAC、HttpOnly/Secure/SameSite cookie、15 分钟新账号
  窗口、一次性领取和已有付费账号不可覆盖的规则。API/Web 地址来自部署
  配置，邀请 URL 的 `environment` 参数不能选择认证或业务服务。
- CF API Core 通过 D1 batch 和触发器一起提交不可变领取凭证、Operator
  权益及邀请关系。并发只有一个赢家，重复请求不延长权益。
- 所有权益读取使用 `cf_effective_user_subscriptions`，到期立即按 basic
  计算；后续 Stripe 购买保留自己的期限。账单写入仍归原订阅表。
- 邀请关系与领取凭证分开保存。删除邀请者移除关系，接受者保留一次性
  领取记录；删除接受者通过现有删除工作流清除自己的记录。导出包含
  自己的凭证和邀请关系。新表纳入既有删除写入屏障及残留检查。
- CF 需要独立 `REFERRAL_SIGNING_SECRET`。资源配置和示例已声明该输入，
  生产资源清单及密钥还需在正式候选发布时配置。

Server 的共享验收最初失败：领取接口直接调用 Firebase SDK，返回
`ValueError: A project ID is required to access the auth service` 和 HTTP 500。
`fork/referral_transport.py` 现在使用既有 Better Auth 身份所有者提供的
创建时间，然后调用原上游资格策略和数据库事务。原请求解析、认证依赖、
响应模型及上游模式保留。缺少创建时间的旧账号不可领取，身份服务故障
返回可重试 503。正常构建的新镜像已用于原隔离环境，保留现有测试数据。

## 真实 HTTP 和浏览器证据

- 同一 `contracts/deployment/core.py` 在 Server OS 和 CF 各 **15/15**
  通过：公开注册、登录、用户隔离、记忆、任务、邀请、权益读取和注销。
  新增邀请用例验证当前 API 地址、配置的 Web 地址、安全 cookie、无认证
  401、无效邀请码 404、拒绝自邀、首次成功、重试拒绝及准确 2,592,000 秒
  权益。直接使用公开接口，没有写测试 SQL 来制造领取成功。
- CF 独立本地 Worker 运行：基础 **14/14**、录音/隐私 **18/18**、聊天
  **11/11**。录音套件包含四个并发领取请求、一次成功、导出和真实队列
  删除。后续扩展后的 15 项共享套件另行通过。D1/Worker/Queues 实际执行，
  AI 和 Vectorize 使用受控 IO，不将其记为线上推理验证。
- 用浏览器打开真实邀请码，跳到品牌登录页，创建新账号并领取，公开接口
  读回 Operator 和唯一凭证。首次测试发现下载 404 时页面一直显示
  “Applying your referral”。先用旧构建复现相应回归失败，再修复成功状态。
- 最终 CF Web 构建中，用另一个新账号重复完整流程，页面明确显示
  “Your 30-day Operator trial is ready.”，保留下载入口和账户入口。
  点击 Continue 后实际进入 `/home`。本地未发布安装包，不能报告下载通过。

私有记录位于 `/Users/macstudio/.codex/eddy-production/`：

- `referral-server-20260906-a/`：14 项通过、邀请 500 的原始失败和诊断。
- `referral-server-build-20260906-b/`：正常 Docker 构建、源码哈希和本地
  服务重建；镜像 `memweft-contract-728cc660c04f-api`。
- `referral-server-20260906-b/`、`referral-cf-core-final-20260906-b/`：
  最终共享 HTTP 报告，均 15 项通过。
- `referral-cf-20260906-a/`：首次公共 Web 地址缺失导致 capture 503；
  修复本地配置投影后，`referral-cf-20260906-b/` 三套运行全部通过。
- `referral-browser-20260906/`：隔离 API、品牌 Web 构建、合成账户及
  权益核对。账户、token、业务响应不进入仓库。

## 自动化与构建

- Core：`uvx uv==0.12.3 run pytest -q`，**549 passed**；AI 相同命令，
  **150 passed**。重点 41 项已包含在上述范围中，不重复计算。
- Workers：`npm test`，**111 files / 895 passed**；`npm run typecheck`
  通过。资源投影修复后的重点 **29 passed**。
- Web：Bun 会话测试 **14 passed**，Vitest **4 files / 23 passed**。
  Server 和 CF 的最终品牌 Web 构建各生成 **28 个路由**，生产类型检查通过。
  最终路径为 `referral-web-server-20260906-b/` 和
  `referral-browser-20260906/web-final/`，均非生产发布产物资格证明。
- Server：在普通后端镜像中禁用网络，通过 `backend/test.sh` 的
  `BACKEND_UNIT_TEST_FILE_LIST` 跑 `test_auth_identity.py`、
  `test_auth_consumers.py`、`test_startup_contract.py`，合计 **74 passed**。
  新增 HTTP 测试运行原资格策略并控制持久化 seam；真实事务另由上述
  Server PostgreSQL 与 CF D1 HTTP 套件验证。只读测试挂载产生的 pytest
  cache 警告不影响结果。
- Python 使用固定 Black 26.5.1；Web 使用 `web/app/bun.lock` 对应的
  Prettier 2.8.8 和 tailwind 插件 0.3.0。上游 pre-commit 要求不存在的
  package-lock，因此提交时使用已公开的 `OMI_SKIP_WEB_FORMAT=1`，并手动
  完成锁定版本格式检查，没有修改上游锁文件。
- 最终 `npm run validate:backend-routes` 匹配全部 **619** 个上游接口；
  `npm run validate:manifest` 通过，包含 638 个 CF 路由声明及 33 个待迁移
  上游接口。manifest/resource/local-target 三个文件 **44 passed**。
  四个关键提示词文件相对 `9b7e48dca2` 字节一致；48 个变更/新增文件与
  `upstream/main` 文件集合交集为零，密钥模式检查及 `git diff --check` 通过。
  整个分支仍有先前记录的 `desktop/macos/docs/desktop-updates.mdx` 上游
  修改超预算（4 行 / 1 行）；本轮没有修改该文件，也没有放宽门禁。

## 仍待完成

浏览器进入首页后仍看到 Omi 页面标题、图标替代文字、目标引导文案及
`macos.omi.me` 链接，属于后续白牌修复范围。受控 API 夹具还显示 Mira
问候语；应检查夹具品牌投影，不能为此改默认提示词。

本轮没有改默认提示词或模型选择。本地 Server 仍使用 MiMo v2.5 的
LLM/ASR/TTS，embedding 仍使用本机 BGE-M3。生产 Cloudflare 部署、其余
33 个接口、完整资格执行器、安装包发布/公证及生产桌面业务验收仍需完成。
