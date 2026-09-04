# SH3 标准 Server 本地语音工件与接口验证

本包基于 `590fdbfda6378dbb991063562b03c000223d684e`，把标准 Server 的
STT/TTS 从显式禁用推进到固定本地 CPU 模型与真实认证接口。该基线包含
SH3 embedding 和 disabled-push 修复；根集成分支随后新增的 Auth/PG v5
包由各自 owner 验证，本包不会把旧基线镜像当作根分支最终交付证明。

## 唯一配置 owner 与具体工件

`deploy/profiles/self_hosted.yaml` → `fork/model_contract.py` → profile renderer
→ 镜像内生成的 profile → `fork.bootstrap` → 实际 STT/TTS consumer 共用
同一工件契约。没有独立模型版本别名或“文件存在即 ready”。

| 工件 | 固定身份 |
|---|---|
| Sherpa runtime | 已有锁定的 Linux amd64 `sherpa-onnx==1.13.4`，未改上游依赖清单 |
| SenseVoice | `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` |
| SenseVoice archive SHA-256 | `7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e` |
| Kokoro | `kokoro-multi-lang-v1_0`，选用 `af_heart` / `zf_xiaobei` |
| Kokoro archive SHA-256 | `c133d26353d776da730870dac7da07dbfc9a5e3bc80cc5e8e83ab6e823be7046` |
| 完整解压 inventory SHA-256 | `75734cf4e9c25611e1c4676ce37b5946eb12fa22c03a41a794371cfea0031b0a` |
| 上游 Silero VAD ONNX SHA-256 | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` |

完整 inventory 覆盖 387 个文件、641,292,524 字节，包括词典、声音、许可文件
及 phonemizer 数据。启动重算完整内容，校验 runtime/VAD，加载模型并实际执行
一次 TTS→ASR；模型不在服务启动或请求中下载。部署命令、来源与各组件许可见
[运行指南](../../../deploy/self-host/speech-runtime.md)。

## 实际修复的调用边界

- 既有 SenseVoice 适配器把 PCM16 整数直接交给接受归一化浮点的 Sherpa API。
  现在使用 little-endian 解码与 `/32768`；奇数字节、超限积压会在增长前拒绝。
- 实际 ambient receiver 构造的 socket 没启动 pump；selected socket 现在启动
  同一适配器。原 pump 吞掉异常后 drain 可以“成功”，现在失败会穿透最终化。
- 首轮实际 PTT 样本的静音尾窗产生了 `Yeah`。选定 socket 现在在每个窗口使用
  同一已校验的 Silero VAD；真实静音可无结果，VAD 失败仍然失败。修后真实镜像
  该尾词消失。Hermetic guard 使用生产 socket 和可控 VAD/native 故障。
- STT 选择、工厂、canonical/captured imports 共用 `patches/speech.py`。默认
  HTTP/multipart 的真实上传、MinIO 签名下载、VAD、结果 envelope 继续由上游
  owner 执行。新 wrapper 只补上选择器在上游 catch 之外抛出的 typed HTTP 错误。
- TTS 两个实际 FastAPI 路由与 PTT WS 保留原 dependency tree，使用本地 provider。
  错误分类、速率检查、输入/输出长度、executor、WS 结束状态由明确边界处理。
  已发布客户端默认 voice 请求可使用部署默认声线；未支持的显式 vendor 参数拒绝。

## 分层验证与原始日志

所有日志位于 `/tmp/memweft-implementation/server/`。

| 实测层次 | 命令/结果 | 边界 |
|---|---|---|
| 环境 | `make setup` exit 0 (`speech-setup.log`) | 独立 worktree，未切原分支 |
| Hermetic | `BACKEND_UNIT_TEST_FILE_LIST=… bash backend/test.sh`；具体最终计数见 commit | 生产 socket、HTTP/WS dependency、typed error、坏工件、disabled/旧用户；不冒充真实推理 |
| Profile | `python3.12 scripts/profiles/test_profiles.py`：8 tests，exit 0 | 两 target、三 stage、五生成文件及旧 omi_cloud |
| 真实原生 CPU | `speech-model-smoke.py`，Linux amd64、`--network none --cpus 2 --memory 2g`，exit 0 | 官方录音 en/zh 实际识别，真实 TTS 生成与回译；不是预置 transcript |
| 实际 provision | `prepare-speech.py` 的完整 production provision/extract/verify 在两个已下载官方 archive 上执行并重复校验，exit 0 | URL transport seam 复用已有原始 bytes；不声称第二次公网完整下载成功 |
| 实际下载负例 | 单独运行 provision CLI 的公网下载收到与固定 hash 不符的内容，exit 1 (`speech-provision.log`) | 正确拒绝，未发布坏模型；此前官方 archive 下载与 hash 已独立记录 |
| 标准镜像 | 原 `backend/Dockerfile` 的固定 Python base，再构建 `deploy/self-host/Dockerfile`，exit 0 | 最新 OCI index `sha256:908d72737e0e44c823cf5277c683501320bc064c09d16b0f01dba29d0e901901`，amd64 |
| 工件一致 | `speech-image-source-proof.log`，exit 0 | 16 个变更生产 Python 文件 SHA-256 全匹配；完整 selected profile 与 renderer 一致，无源码挂载 |
| 真实 HTTP/PTT | `speech-live.py` → `speech-live-vad.log`，exit 0 | 真 Auth 注册/换 JWT，PCM+multipart、两个 TTS、PTT WS；401/400/禁用 push503；合成用户清理 |
| 真实 CLI 故障 | `speech-native-faults.log`，三个进程各 exit 1 | 缺模型、错误 manifest、冲突独立线程配置均拒绝；未调用外部模型 |

真实录音识别结果：英文返回 “The tribal chieftain called for the boy and
presented him with 50 pieces of code.”，其中原句的 gold 被识别成 code；中文
整段 HTTP 返回“开放时间早上9点至下午5点。”。这是一组功能录音，不是准确率
benchmark。五秒分窗 PTT/ambient 返回“开饭时间早上9点至下午5点。”，保留
“开放→开饭”的质量缺陷，不能声称与整段识别等精度。最终 PTT 的验收检查时间
内容及静音尾词消失；首轮严格字面断言失败日志 `speech-live.log` 也保留。

真实合成：`/v1/tts/synthesize` 英文 MP3 回译得到 “Please record a reminder
for tomorrow morning.”；`/v2/tts/synthesize` 中文 MP3 回译得到“请记录一个明天
早上开会的提醒。”。这是实际 Kokoro 音频→FFmpeg PCM→真实 HTTP ASR，不是
把输入文本直接当作识别结果。

环境录音 `/v4/listen`：新 Auth 用户尚无 PG profile 时按既有规则关闭 1008
`Bad user`；通过实际公开语言设置 PATCH 创建资料后，真实认证、session ready、
PCM、ping/pong 和本地 segment 返回通过 (`speech-ambient-ping.log`)。
该 probe 没有启动完整 finalization worker，也没断言 LLM/conversation/memory
完成。Web 首消息认证入口的结果单独写入 `speech-web-ambient.log`。

## 环境事故与未完成范围

宿主 ENOSPC 导致 Docker VM 的 ext4 journal abort；日志显示引擎请求 shutdown。
根代理恢复 Docker，未重置/删卷。本路 PG/MinIO/Qdrant 原本是独立 tmpfs 夹具，
内容按设计丢失，不能描述为持久卷损坏或数据仍在。新镜像先对未迁移 Qdrant/PG
非零拒绝，再以正式 CLI 迁移 fresh v4 PG、Auth 与 Qdrant 后达到 ready。
原手工 Redis 夹具的 runtime 密码设置也丢失，已从既有私有配置恢复；这不是修改
生产 Compose。基础恢复、失败与重建日志均独立保留。

本包未证明完整产品录音到会话/记忆闭环、聊天 LLM、说话人识别、多说话人 diarization、
长录音、并发吞吐、生产网络隔离、持久卷灾备、移动端播放 UI 或发布。下一 SH3 包
须把固定本地 LLM 接到现有 chat/finalization/conversation/memory owner，并验证
真实队列及终端消费。现有 `/v2/messages` 缺 OpenAI credential 的 500 仍是待修入口。
Push 保持 disabled；没有通过加 vendor key 解除这些限制。

正式 preflight 的最终 candidate、check 数和 macOS compile 状态记录在 commit；
历史 failure-class guard 在 shallow clone 中显式 SKIP，不能计作历史审计成功。
没有 push、PR、merge、生产应用操作或外部部署。
