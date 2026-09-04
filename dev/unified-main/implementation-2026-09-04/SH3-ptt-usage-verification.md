# SH3 PTT 终止路径与用量修复

根代理在审查 `be567c33308b2d77491d8fe4b3d0d2037fde876a` 时发现：本地 PTT
只在 `finalize` 写入用量，断线、空闲和限额退出虽然 drain 却不记录。上游
`routers/chat.py` 的 `record_stt_usage_once` 与 finally 明确要求健康尾段处理后
按已接受字节记录一次；这次修复恢复该合同，不修改上游文件或 quota owner。

`fork/speech_transport.py` 以同一个任务拥有 terminal drain 与一次用量写入。
所有退出都等待它；有效已接受音频只在健康 drain 后记账。限额/奇数字节拒绝的
触发帧不计入 accepted bytes；provider send 拒绝或 drain 失败不产生收费。
`utils/sensevoice/socket.py` 保留 pump 任务引用并 shield 等待者取消，避免取消
传播至健康尾段或后续 drain 跳过尚在运行的推理。pump 自身取消按失败处理。
PTT 外层取消仍然传播，但在收尾期间再次取消也不能跳过该结算 owner。

## 验证

- 正式 `BACKEND_UNIT_TEST_FILE_LIST` → `backend/test.sh`：18 个实际 transport
  seam 测试与 15 个 speech 工件/socket 测试通过。新增覆盖 disconnect event/
  exception、idle、limit、malformed、finalize 一次、native failure、cancelled
  pump、provider rejection、未接受音频、PTT cleanup 二次取消与 drain waiter
  取消；使用生产 PTT/socket 和可控推理故障，不检查源码字符串。
- 真实 Uvicorn / Auth JWT / 本地 SenseVoice / Redis quota：对四个人造用户分别
  运行断线、finalize、限额和实际 30 秒空闲超时。官方中文 PCM 每次 5592ms；
  退出后 Redis 均新增一次且仅 5592ms。限额场景先通过原 quota owner 写入该
  人造用户的已用额度，余量不足的后续帧被拒绝且没有计费。
- 实测仅新建隔离容器 `memweft-speech-usage-proof`，沿已验证 amd64 标准镜像
  `908d727…`、原只读模型与独立本地依赖；本补丁两个生产文件通过只读 mount
  注入。因此这是当前源码的真实 wire 回归，**不是新不可变镜像资格证明**。
  四个测试账户与其 Redis 用量键已清理，新容器已停止，原服务未变。
- 完整日志：`/tmp/memweft-implementation/server/speech-usage-focused-final.log`
  与 `speech-usage-live.log`；真实探针为同目录 `speech-usage-live.py`。
  精确候选 gate 与 failure-class 结果写入提交说明。现有 local/CI startup lane
  已选择上述测试，无需新建 runner 或扩大 selector。

本修复不改变原 Redis quota helper 的 fail-open 策略，不将它称为持久计费系统；
完整 LLM/会话/记忆闭环仍是后续包。没有 push、PR、merge 或外部部署。
