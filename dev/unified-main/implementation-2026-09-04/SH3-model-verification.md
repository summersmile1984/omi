# SH3：真实本地 embedding 与禁用能力边界

本包基线 `822b9d4163`，独立 `codex/implement-server-models` worktree。
它完成可运行的本地 embedding → Qdrant 业务路径，以及未实现语音/推送的明确禁用边界。
它没有完成可用 STT/TTS 录音回放链路，也不代表整个 self-host/full-cutover 已通过。

## 同一配置与状态所有者

- `deploy/profiles/self_hosted.yaml` 固定模型身份；`fork/model_contract.py` 是 renderer、runtime 与模型仓库校验共用的纯类型校验器。
- `capabilities.embedding_dims` 从模型 contract 派生。Qdrant Config 保存同一不可变 typed contract，dimension 是派生属性。旧的独立环境默认值已从 Compose/示例移除；显式注入冲突值会失败。
- `fork/model_store.py` 在服务启动前核对 manifest 与全部引用 blob；Compose 的只读模型挂载、固定 Ollama image digest、CPU 限额可直接执行。
- Qdrant `migrate` 原子创建完整模型 metadata + vector schema；`check` 拒绝旧无绑定集合、不同模型身份、不同维度。即使维度相同，也不能修改 metadata 来承认旧向量；需要新 prefix、源文本 backfill 和明确切换。
- `fork/embedding.py` 使用 native `/api/tags`、`/api/show`、`/api/embed`，绑定 canonical `utils.llm.clients.embeddings` 及 captured `database.vector_db.embeddings`；无 OpenAI/BYOK fallback。每次请求核对真实 manifest、GGUF 来源、维度和 context，`truncate:false`、4 CPU threads、128 token evaluation batch、mmap。
- Ollama wire digest 为裸 64hex；公开配置统一保存 `sha256:<hex>`，只有协议比较处移除固定前缀。首次真实运行捕获了这个差异，修复后的测试以实际 wire 为依据。

## 工件与模型

实际 Ollama 工件是 0.33.3 Linux **amd64**，本机通过 Docker Desktop 执行；GPU 没有绑定，runtime 日志识别 `library=cpu`。

- image digest：`sha256:57a73f11f75b32b97b59b003f351445c9c2a8af4b9d586ecdc928dee6150ef26`
- BGE-M3 GGUF F16，1024 维，8192 context；模型 blob 1,157,671,200 bytes。
- manifest：`sha256:7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`
- model：`sha256:daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c`
- config：`sha256:0c4c9c2a325fb1cdafec606e6809cb745f1cb26a6d919994400d27372303e276`
- license：`sha256:a406579cd136771c705c521db86ca7d60a6f3de7c9b5460e6193a2df27861bde`

仅复制该模型及引用文件到独立 `/tmp/.../sh3-model-store`，没有修改用户 Ollama 服务或其仓库。实际内容 SHA-256 全部重算。
模型 API/上下文语义依据 [Ollama native embed](https://docs.ollama.com/api/embed)，模型元数据与 [BAAI BGE-M3](https://huggingface.co/BAAI/bge-m3) 相符；具体可复现身份以本次内容摘要为准。

第一次默认加载在 8GiB Docker VM 中被 OOM kill，API 非0退出，未变成 /ready 成功。采用 mmap、4线程和128-token批量，且主代理释放其已经完成的独立夹具后，实际推理成功；加载后观测模型约806MiB、API约476MiB。该数字是本次测量，不是任意并发/长文本的内存承诺。没有调整 Docker 全局资源、停止生产应用或旧 SH2 fixture。

## 实际验证

日志统一 `/tmp/memweft-implementation/server/sh3-*`；fixture env/Compose 含合成凭据，仅保存在 mode0600 临时文件，文档不包含秘密。

| 命令/路径 | exit / 结果 | 覆盖边界 |
|---|---|---|
| `make setup` | 0 | 锁定 backend 环境、worktree hooks |
| 标准 `docker build -f backend/Dockerfile` → `-f deploy/self-host/Dockerfile` | 均 0 | 没有修改上游 Dockerfile；实际镜像包含生成 local profile |
| `docker compose -f /tmp/.../sh3-compose.json up -d embedding` | 0 | 前置 artifact-check 0；只读全部 blob；实际固定 CPU 服务 |
| `run --rm --no-deps qdrant-migrate` | 0 | 新 `sh3_bge` prefix，7 个真实 Qdrant 集合 |
| `up -d --no-deps sh3-api` | 0；实际 `/ready` 200 | 当前 fork bootstrap + upstream ASGI，包含真实启动 inference |
| `exec -T -e PYTHONPATH=/app sh3-api python /tmp/sh3-live.py` | 0 | 下列实际业务闭环，无 embedding/vector stub |
| `sh3-negative.py` 6 个独立真实进程 | 每个 exit 1 | 旧无绑定集合、冲突模型 env、坏 manifest、同维不同身份、模型 endpoint 拒绝连接、超8192上下文 |
| 正式 `backend/test.sh`，5 files | 76 tests通过 | 23 embedding +14 vector +12 disabled owner +23 startup +4 real seams；独立 hermetic runner |
| `scripts/profiles/test_profiles.py` | 8 tests通过 | 两target三stage、五输出、共享model维度、disabled capabilities |
| `web/app/fork/test.sh` | 13 Bun +6 Vitest通过 | Server接受disabled push，CF仍webhook；现有登录/实时消费不回归 |
| `deploy/self-host/check-config.py --self-check` | 0 | source/entrypoint闭包；不是全provider验收 |

真实业务闭环包括：

1. 两条合成 memory 调用生产 `upsert_memory_vector`，真实 BGE-M3 CPU推理后写 Qdrant；中文“法国的首都是哪里”调用生产语义查询，Paris 排第一。另一个 UID 的同义高分记录不能进入结果。
2. 新建合成 Better Auth 用户→实际 JWT→over-wire Tasks `due_at` POST→GET 持久化，`ns4` 有真实1024维向量；主写入成功没有因禁用提醒变500。
3. 同一个标准工件、真实 PG/Redis/Auth/Qdrant 的生产 ASGI Tasks 路径，安装 Firebase `send_each` 测试陷阱，再创建含 due_at 的任务。请求200，Firebase调用0，第二条真实Qdrant向量存在。陷阱只阻止意外推送，不控制模型/存储/认证结果。
4. 实际 HTTP TTS/STT/FCM注册/推送返回503 `deployment_capability_disabled` + capability + `retryable:false`；真 WebSocket upgrade 后收到1008 / `stt_disabled`，没有接收音频。
5. 删除本次合成 principal/用户行/Tasks/全部向量；没有使用客户数据。旧SH2服务和`sh2_provider`集合保留。

## 禁用的明确语义与未完成事项

- STT/TTS/push 的 profile 声明、backend启动校验、真实入口和 Web validator 同包改变。没有用环境变量声称 sherpa/MOSS/webhook 已实现。
- background STT canonical/captured selector 拒绝，而非返回空转写。公开推送/FCM注册拒绝；内部 count型发送返回0，Apple bool为false，记录共享 fallback。这保留了 Tasks 在主数据已经写入后的成功语义，零投递不被解释成发送回执。
- 禁用能力 HTTP 拒绝发生在入口，不要求登录即可获知该静态部署能力；没有暴露用户信息。upstream/CF模式不安装这些 Python patches。
- 完整 STT/TTS启用与录音→转写→回放仍归SH3后续；即时multimodal relay、LLM、speaker等不由本次推理成功证明。
- 旧 `runtime-evidence.py` / `live-replacement-smoke.py` 仍描述完整generic/MOSS/TTS切换，不能为当前中间profile生成成功cutover证明，须在SH4与启用能力共同迁移。README顶部已明确历史描述边界。
- 本次 fixture 复用合成PG/Redis/Auth与Qdrant服务器，新服务名/new prefix与SH2隔离。它不是全新完整生产Compose栈启动。真实工件/代码摘要记录与正式提交范围检查另列；不声称外部部署、release provenance、性能SLO或全账户删除验收。

Failure-Class: FC-split-mutation-authority。PR #7及当前main审计曾显示Compose 1536、profile3072与硬编码OpenAI模型并存；本次由同一typed model/migration owner收束状态。新行为测试属于现有startup/profile lanes的生产契约，未新增独立脚本gate或编造registry已合并证据。

## 独立复核补丁：BYOK 直接推送旁路

主包 `84421de244` 后的独立 review 发现，`utils.llm.byok_errors` 的错误通知直接调用 FCM `send_each`，不经过中央通知 count owner。真实 `handle_llm_error` 根据请求中已验证的 BYOK key/UID 判断来源，不读取 profile 的 `allow_byok`；因此不能仅靠前端禁用开关声称后端不可达。

本补丁只在 fork capability patch registry 将该通知 owner 接到同一 disabled 策略，保留 void 返回。同步和异步生产错误入口均不能获取/清理 token、获取/释放冷却锁或调用 FCM，不记录已发送。共享 fallback 的 component 改为现有注册项 `pusher`，避免此前被归入 `other`。没有修改上游 BYOK 模块或引入第二套推送实现。

- 正式 `BACKEND_UNIT_TEST_FILE_LIST=/tmp/memweft-implementation/server/sh3-byok-files.txt bash backend/test.sh`：25 tests通过（13 disabled、4真实seams、8原上游BYOK通知行为）。回归先执行未patch的真实handler，证明已有principal可达，再安装生产patch并对FCM/token/cooldown设置调用陷阱；同步/异步均为0，void不被当投递成功。
- 标准 upstream Dockerfile→fork Dockerfile 新建 **amd64** 工件 `sha256:5131d4b9b5da9bd25c9f2ec66eb6031b44b7c5c8cd4774263dc21c4ac6e42ddb`。12个本包生产Python文件与工件内逐个SHA-256一致；这是本地工件内容证明，不是release provenance。
- `docker compose -f /tmp/memweft-implementation/server/sh3-compose.json exec -T -e PYTHONPATH=/app sh3-api python /tmp/sh3-byok-live.py`：exit0。实际 built bootstrap 完成真实模型readiness后，生产同步/异步错误入口在已有合成BYOK上下文中执行，FCM、token lookup/prune、cooldown acquire/release均0；共享日志为 `component=pusher to=disabled`。没有向外部LLM发送请求或使用客户数据。
- 日志：`/tmp/memweft-implementation/server/sh3-byok-tests.log`、`sh3-byok-live.log`、`sh3-byok-image-source-proof.json`及同前缀标准镜像构建日志。正式提交范围gate结果记在本独立提交消息中。

该补丁封闭本次review发现的BYOK错误通知旁路，不改变上文STT/TTS启用、完整cutover及生产部署尚未完成的限制。
