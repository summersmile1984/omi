# SH4 启动补强子包：Auth stage owner 与镜像依赖缓存

基线 `425648f947`，本地 feature worktree。此包只完成 SH4 的两条已复现启动问题，不代表 source-freeze/import/reconcile/restore 全链已完成。

## 变更

`deploy/self-host/auth-runtime.mjs` 是 self-host Auth 镜像中唯一 stage→NODE_ENV owner。`SELF_HOST_STAGE=local` 为 development，beta/production 为 production，缺省 production；在动态导入原有 Auth module 之前设置，环境中的 NODE_ENV 不能降低 beta/production 安全检查。serve 与 migrate（含 --check）使用同一入口，未知 stage/command 在 import 前非零退出。

Docker 将该文件复制到 `/app/auth-server/self-host-runtime.mjs`。Compose 的 Auth server/migrate、迁移 cutover gate 都调用此入口，移除独立 NODE_ENV 配置。现有 startup source/config gate检查这两个命令与 stage wiring；`auth-server/test/self-host-runtime.test.js` 已由现有 Auth contracts local/CI lane 执行。

Auth Dockerfile 的源码 ARG/LABEL 移至依赖安装和源码 COPY 之后。保持原有 pinned Node、package-lock、npm ci flags；没有通过改依赖或取消校验获得缓存成功。

## 验证

日志目录 `/tmp/memweft-implementation/server/`，所有服务与数据为本任务隔离 fixture。

- Node 22.23.2 本地锁定依赖 `npm ci --ignore-scripts`：Auth 和 CF 成功。`scripts/fork/auth-contracts.sh`：Node 39、CF 31（含 typecheck）、Python 27 全通过（sh4-auth-contracts.log）。backend/test.sh config 6 项通过。
- Node 行为测试将真实 launcher 复制到临时镜像布局，实际子进程导入受控 entry module：三 stage × serve/migrate/migrate --check 共九种组合，先绑定模式再导入；默认、坏 stage/command 与 ambient NODE_ENV 负例。初版测试发现 macOS /var→/private/var symlink 导致 main 检测静默跳过，已使用 realpath 修复，测试现通过。
- 实际 `auth-server/Dockerfile` 构建首次 npm ci 最终成功（网络较慢，sh4-auth-build-initial.log）；随后完全相同源码、仅 `cache-proof-a/b` 测试标签不同的两次构建都显示 npm ci CACHED（sh4-auth-build-a/b.log）。docker image inspect 验证两镜像 RootFS.Layers 完全相同、标签不同。测试标签不作生产源码归属证明。
- sh4-live-stage.py 使用从本次 production Compose 实际渲染的 Auth command/environment，外接已有隔离 PG，local migrate 和 migrate --check exit 0，Auth server healthy，无手工 NODE_ENV override。beta/production 配置传入 NODE_ENV=development 后，仍由原 Auth HTTPS 校验拒绝（exit 1）；typo stage 在 launcher 处拒绝（exit 1）。
- 新 Auth 镜像签发实际 JWT，既有 API 调用与 PG 删除 gate 验证通过（sh4-live-api.log），合成 Auth/PG 主体随后删除。

完整 cutover、生产 TLS、第三方 OAuth 和外部部署没有在此 fixture 中验收。共享 runtime/fallback 的 COPY 后续由根集成者随其 Auth 实现加入，不在本包提前复制无消费者目录。

Failure-Class: FC-split-mutation-authority。复用已有类；本次 stage 冲突在实际 local Auth migration 重现，永久 launcher/行为测试将选择权收束为一处。

正式提交范围 staged candidate：24 upstream + 4 fork checks 全通过；failure-class 历史 guard 因 shallow clone 明确 SKIP。OpenAPI runner 使用现有锁定 uv cache（UV_OFFLINE=1），检查未跳过。

后续补证：SH2 production Dockerfile 的重试在网络恢复后成功，当前 upstream base + 标准 fork profile/dependency layer 均已构建（sh2-base-build-retry.log、sh2-fork-build-retry.log）；将真实 built image `memweft-server:sh2` 启动后 healthy，实际 JWT/API 删除 gate 再次通过（sh2-built-image-api.log）。这清除了 SH2 首次构建的网络阻塞；SH2 外部 provider/完整删除验收限制仍存在。
