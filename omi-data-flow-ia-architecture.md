# Omi 业务数据全链路流程与 IA 架构分析

> 全景式、全流程、全链条分析:从设备音频采集到记忆/任务/目标沉淀,再到检索消费与桌面 Agent 执行回写。
> 基于代码勘察(memweft 仓库,backend/ + desktop/macos/ + app/)整理,所有路径均可回溯到源文件。

---

## 0. 总览:三条主链 + 一个数据平面

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ ① 采集面     │   │ ② 处理链     │   │ ③ 沉淀面     │   │ ④ 消费/执行面 │
│ device/watch │──▶│ 服务端 pipeline│──▶│ Firestore   │──▶│ chat/RAG     │
│ phone 录音   │   │ STT+提取     │   │ Redis/Pinecone│   │ 桌面 Agent   │
│ desktop 截屏 │   │ finalization │   │ GCS/Neo4j    │   │ 通知/同步     │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
       ▲                                                  │
       └──────────── ⑤ 写回闭环(执行结果/任务状态) ──────────┘
```

**核心设计原则(IA 架构特征)**:

1. **执行分散、数据集中**:采集与 Agent 执行发生在端侧(用户设备),所有结构化数据(记忆/任务/目标/对话)集中存储在服务端 `users/{uid}/` 数据平面,跨端共享同一池子。
2. **端侧提取 vs 服务端提取并存**:
   - 手机转录链:提取在**服务端**(转录本身在服务端,LLM 提取也在服务端)。
   - 桌面链:转录/截屏在**本地**,记忆/任务提取在**桌面本地**(Gemini),经审批后落服务端。
3. **任务=执行器,目标=度量衡**:action_items 是短期动作(桌面 Agent 执行),goals 是长期量化目标(对话文本提取进度),两者通过任务侧 `goal_id`/`workstream_id` 外键挂靠,互不自动驱动。
4. **Agent 执行永不发生在服务端**:后端 Python 只做数据提取与编排;真正的代码仓库操作在用户 Mac 本地(pi-mono/ACP/Claude CLI),经 Unix socket 桥回 Swift 执行。

---

## 1. 采集面(Ingest Surfaces)

### 1.1 硬件设备(Omi Device / Apple Watch)

| 路径 | 链路 | 关键实现 |
|---|---|---|
| Omi Device | BLE GATT `19b10000` 服务 → 3 字节头 + Opus 16kHz 音频 | `sdks/device/PROTOCOL.md` |
| Apple Watch | watchOS `AVAudioEngine` → WCSession → 1.5s chunk → iPhone App | `app/ios/omiWatchApp/WatchAudioRecorderViewModel.swift` |
| 手机(BLE 伪装) | `AppDelegate.swift:382-413` 前置 3 个 dummy 字节 `[0,0,0]` 模仿 BLE header | `app/ios/.../AppDelegate.swift`;`app/lib/services/devices/transports/watch_transport.dart`(伪 GATT 服务) |

**结论**:采集协议统一收敛为 BLE 音频流语义(3 字节头 + Opus),Flutter 侧 `DeviceTransport` 抽象屏蔽设备差异;WatchConnectivity 依赖使手表仅支持 Apple Watch。

### 1.2 手机端

- **实时流**:`/v4/listen` WebSocket(backend `routers/transcribe.py`,2900 LOC)承载音频流;transcribe 后经 pusher 分发。
- **录音同步**:`/v2/sync-local-files`(backend-sync Cloud Run):录音 ≤6h 进 `sync-jobs`(fresh),更老/不可信进 `sync-backfill`(scale-to-zero worker,30 天回溯,每日 4 语音小时/用户配额)。
- **音频回放**:`audio-merge` 队列构建 30 天 MP3 工件,指纹缺失触发重建。
- 手机端**不承担任何提取计算**,只有录音端 + 展示端(记忆页/任务页/对话页)。

### 1.3 桌面端(macOS)

- **截屏采集**:`ScreenCaptureService.swift` — 只同步元数据 + OCR + embedding,图片永不出设备(Windows 用 getUserMedia 刻意避开 desktopCapturer)。
- **对话采集**:`ConversationFinalizationService.swift` — 本地时间戳匹配/云端对话完成事件。
- **观察数据**:`RewindIndexer.swift:247-354` 窗口捕获 → 本地 SQLite → 服务端 `users/{uid}/screen_activity`(Firestore)+ 桌面 `usage_page` 同步。
- **音频**:`BleAudioProcessor.swift` 本地 BLE 处理。

### 1.4 服务端子服务(音频侧)

```
backend(main.py)
  ├─ ws ──► pusher(二进制 WS 协议:1s 转录批、4s 音频流、60s 音频上传、LLM 会话分析)
  ├─ ────► diarizer(GPU,pyannote 说话人边界/embedding)
  ├─ ────► vad(Modal GPU,pyannote VAD + 说话人识别)
  ├─ ────► parakeet / modulate(GPU STT,策略由 config/stt_provider_policy.py 独占)
  ├─ ────► nllb-translation(GPU,NLLB-200,降级 Gemini Flash-Lite)
  └─ ────► llm-gateway(内部 LLM auto 车道)
```

---

## 2. 处理链(Processing Chain)

### 2.1 音频 → 转录(全服务端)

```
设备/手机音频 → /v4/listen WS(backend)
  → pusher 二进制协议 → Parakeet/Modulate STT(默认)/ Deepgram 自托管(显式策略)
  → VAD 门控 + 说话人识别(diarizer/vad)
  → transcript segments(加密)存 Firestore users/{uid}/conversations
  → 会话结束 → conversation-finalization Cloud Tasks 队列
  → POST /v1/conversation-finalization-jobs/run → process_conversation
```

### 2.2 会话后处理(核心提取:process_conversation.py,1971 行)

`backend/utils/conversations/process_conversation.py` 是手机链路核心,顺序执行:

```
process_in_progress_conversation(conversations.py:175)
  └─ process_conversation()
       ├─ should_discard_conversation()          # 废话过滤
       ├─ get_transcript_structure()             # 标题/摘要/emoji
       ├─ extract_action_items()                 # ★ 任务提取(LLM)
       │    └─ _fetch_dedup_candidates()         #   语义查重(threshold 0.6 + 近一周)
       ├─ extract_memories_from_text() /         # ★ 记忆提取(LLM)
       │  extract_canonical_l1_memory_candidates() #  L1 记忆 + 证据引用 + 敏感标记
       │    └─ MemoryService 冲突解决 → Firestore memories
       ├─ extract_and_update_goal_progress()     # ★ 目标进度提取(LLM,单次查所有 active goals)
       ├─ _save_action_items()(:1293)            # ★ 批量写 Firestore action_items(重处理时 retire 旧)
       └─ 后处理 executor 链(llm_executor/postprocess_executor)
```

提取执行的三个线程池:转录阶段用 `llm_executor`(6w),后处理协调用 `postprocess_executor`(24w),DB 用 `db_executor`(24w)。

### 2.3 提取产物明细

| 产物 | 提取位置 | 落点 | 幂等/防重 |
|---|---|---|---|
| 记忆 memory | 服务端 LLM(`extract_memories_from_text`/L1 candidates) | Firestore `users/{uid}/memories` | 语义去重 + `canonical_l1` 证据绑定 |
| 任务 action_item | 服务端 LLM(`extract_action_items`) | Firestore `users/{uid}/action_items` | `find_similar_action_items` 语义查重;`conversation_n.extract.shadow` shadow feature |
| 目标进度 goal progress | 服务端 LLM(`extract_and_update_goal_progress`) | Firestore `users/{uid}/goals` | Redis `try_acquire_conversation_goal_lock` 每会话幂等 |
| 主题/趋势 | 服务端 | `topics`/`trends` | — |

### 2.4 桌面端提取链(本地 Gemini)

```
截屏 → MemoryAssistant(20 条 previousMemories 去重 + 置信度门槛)
     → 本地 SQLite(MemoryStorage:FTS+向量)
     → AgentSyncService → 服务端 memories
截屏 → TaskAssistant → staged_tasks 候选(本地)
     → 后端审批(workflowMode 控制)→ TaskPromotionService(60s 保底 + debounce 30s,每次一条)
     → Firestore action_items(共享池)
```

---

## 3. 沉淀面:统一数据平面

### 3.1 Firestore `users/{uid}/` 主集合(勘察全量)

```
account_deletions, action_items, analytics_markers, analytics, api_keys,
artifact_refs, candidates, chat_quota_events, chat_sessions, client_devices,
conversations(加密 segments), events(workstream), fair_use_events, fair_use_state,
fcm_tokens, files, folders, goals, hourly_usage, import_jobs, integrations,
llm_usage, meetings, memories, memory_items, memory_state, messages,
pending_verifications, people, photos, reviews, screen_activity(桌面观察),
soniox_streaming, speechmatics_streaming, staged_tasks, sync_content_ledger,
task_integrations, task_intelligence_control, tasks, topics, trends,
usage_history, workstreams, goals/progress_events, ...
```

### 3.2 辅助存储

| 存储 | 用途 |
|---|---|
| Redis | 速率限制(Lua)、公平使用分钟桶、锁(listen/goal)、pub/sub、缓存 |
| Pinecone | 向量语义搜索(`database/vector_db.py`) |
| Neo4j | 知识图谱(`database/knowledge_graph.py`) |
| GCS | 私有云音频(`private_cloud_queue` deque(maxlen=20)) |
| 桌面本地 SQLite | 任务/记忆/观察/agent 状态(后端权威,本地缓存) |

### 3.3 数据共享语义

- **任务池共享**:手机转录任务(来源 `transcription:omi`)与桌面截屏任务(`screenshot`)写同一 `action_items` 集合,桌面 TasksStore 按 uid 拉取全量。
- **加密**:Firestore segments/敏感字段 AES-256-GCM(每用户 HKDF-SHA256 派生密钥)。

---

## 4. 状态机(IA 核心:显式状态模型)

### 4.1 Goal 状态机(强约束,事务 + 幂等)

```
                focus(独占槽位,上限 focus_cap)
   background ────────────────────────────► focused
       ▲            unfocus                     │
       └────────────────────────────────────────┘
       │
       ├─────────────► paused(可逆)
       └─────────────► achieved / abandoned(终态,写 ended_at,is_active=False)
```

- **focus 唯一性**:`database/goals.py:511` 事务内查所有 focused;满额必须带 `replacement_goal_id` 踢一个;rank 冲突 → 409。
- **幂等防重放**:`_begin_goal_mutation` → 事务 receipt(请求 hash)→ 同 key 重放返回原结果。
- **lifecycle 终态入口**(`:648`):只接受 paused/achieved/abandoned;`relationship_disposition=detach` 时事务内把该 goal 下所有 action_items/workstreams 的 `goal_id` 置 None(上限 450)。
- 进度提取(`extract_and_update_goal_progress`):每次会话后单次 LLM 调用检查所有 active goals,只接受**绝对进度值**,拒绝负值/NaN;`is_active=False` 后自动跳过。

### 4.2 Task/action 状态机(弱约束)

```
   active ──► completed(completed_at 时间戳)
     │
     ├──► cancelled
     └──► superseded(被新任务取代:去重合并/重处理)
```

- 唯一硬约束:`completed` bool 与 `status` 必须同步(`models/action_item.py:95` reconcile)。
- 无强制转移矩阵、无事务状态机、无幂等 receipt——通用 `update_action_item`/`batch_update` 自由改。
- 挂靠校验:`validate_task_relationship_in_transaction`(goal 必须存在;workstream.goal_id 必须与任务一致;ended goal 下任务不能改挂)。

### 4.3 候选/工作流状态机

| 实体 | 状态 | 说明 |
|---|---|---|
| Candidate(staged) | pending / accepted / rejected / expired | 桌面提取候选 → 后端审批 → promote 为 action_item |
| Workstream | open / paused / completed / archived | 目标下的工作流,带事件日志(10 种事件类型)+ artifacts + checkpoints |
| 挂靠层级 | task → workstream → goal | 三级,关系存任务侧 |

---

## 5. 消费面与执行链

### 5.1 检索/RAG(`utils/retrieval/`)

```
rag.py         — topic/memory/conversation 上下文检索
hybrid.py      — 混合检索(向量 + 关键词)
agentic.py     — Claude agentic RAG:18 类工具(5000 LOC 级)
tools/         — 19 个工具域:action_item/calendar/gmail/apple_health/
                 conversation/memory/screen_activity/file/graph/perplexity/web/...
graph.py       — 知识图谱检索
safety.py      — 检索边界/安全
```

### 5.2 聊天消费

- `/v2/messages`(backend chat.py):工具调用(含任务/记忆/目标工具)+ 语音消息 + 文件上传。
- 桌面 agent daemon:pi-mono 推理经 `api.omi.me` 网关(omi-sonnet/omi-opus);BYOK 走 X-BYOK 头。
- 桌面语音助手工具:`create_action_item`/`update_action_item`/`get_tasks`/`search_tasks`/`get_action_items`(agent 直接写共享任务池,`omi-tool-manifest.ts:438`)。

### 5.3 桌面 Agent 执行(核心执行面)

```
任务池(action_items,服务端)
  ├─ 手动/自动触发(TaskAgentManager / TaskChatCoordinator / RecurringTaskScheduler 60s)
  ├─ TaskAgentManager:tmux + claude --dangerously-skip-permissions(feature/bug/code,autoLaunch 默认 false)
  ├─ TaskChatCoordinator:Omi agent daemon(ACP 桥,任意类别,4h 去重闸)
  └─ agent daemon 架构:
       Swift Process() ──node(--max-old-space-size=256)──► agent/dist/index.js
         ├─ kernel(runtime/kernel.ts + kernel-{core,sessions,runs,coordinator,artifacts} + sqlite-store)
         ├─ 适配器:pi-mono / acp(Claude) / hermes / openclaw / local-subprocess / one-shot-cli
         ├─ 工具执行经 Unix socket OMI_BRIDGE_PIPE 回 Swift 实际执行
         └─ 安全:denylist(sudo/rm 系统路径/管道到 shell)+ ~/.omi/pi-mono-audit.log 审计
```

### 5.4 通知与其他消费

- `notifications-job`(Modal cron):推送 + X connector sync。
- `memory-maintenance-job`(Cloud Run Job):canonical 记忆维护(标准化 → TTL 审计 → 终态整合/提升)。
- `agent-proxy`(GKE):手机 app ↔ 用户 agent VM 的 WS 桥(Firebase auth → Firestore VM lookup → GCE 生命周期)。

---

## 6. 写回闭环(⑤)

```
桌面 Agent 执行
  → 结果回本地 SQLite(动作日志/会话 journal)
  → TasksStore.updateTask / completeActionItem(batch_update,PATCH)
  → 服务端 Firestore action_items(status/completed/completed_at)
  → 手机 app 刷新可见完成状态
  → (可选)任务 goal_id 挂靠的 goal:不自动推进——靠对话文本提取
```

---

## 7. 端到端数据流图(全景)

```
【手机链】
Phone 录音 → /v4/listen WS → pusher → STT(parakeet/modulate)→ segments(加密)
  → conversation-finalization 队列 → process_conversation()
       ├─ extract_action_items ──► action_items(Firestore 池)
       ├─ extract_memories ──────► memories(Firestore + Pinecone)
       ├─ goal progress ─────────► goals/metric(Firestore)
       └─ structured(标题/摘要)──► conversations(Firestore)
【桌面链】
截屏 → OCR+embedding(本地)→ screen_activity(Firestore)
截图 → MemoryAssistant(Gemini,本地)→ 本地 SQLite → 服务端 memories
截图 → TaskAssistant → staged_tasks → 后端审批 → promote → action_items(同一池)
【手表链】
watch 音频 → WCSession → iPhone App → 3-byte header → 与手机同管线
【执行链】
action_items 池 → 桌面 TasksStore 拉取 → TaskAgentManager/daemon(本地)
  → pi-mono/ACP/Claude CLI 执行 → 结果写回服务端任务状态
【消费链】
chat /v2/messages + agentic RAG(18 工具)→ 记忆+任务+对话上下文
notifications-job → 推送;sync → 移动端;task_integrations → Todoist/MS To Do
```

---

## 8. 关键代码索引

| 环节 | 文件 |
|---|---|
| 音频入口 | `backend/routers/transcribe.py`(2900 LOC) |
| 会话后处理 | `backend/utils/conversations/process_conversation.py:508,1293` |
| 任务提取 | `backend/utils/llm/conversation_processing.py`(extract_n,shadow feature) |
| 记忆提取 | `backend/utils/memory/memory_service.py` |
| 目标提取 | `backend/utils/llm/goals.py:248` |
| Goal 状态机 | `backend/database/goals.py:511,602,648`;`backend/models/goal.py:19` |
| Task 状态 | `backend/models/action_item.py:12`;`backend/database/action_items.py:703` |
| 工作流 | `backend/database/workstreams.py`;`backend/models/workstream.py` |
| 桌面拉取 | `desktop/macos/Desktop/Sources/Stores/APIClient+Tasks.swift` |
| 桌面提取 | `desktop/macos/Desktop/Sources/ProactiveAssistants/`(7 助手) |
| Agent daemon | `desktop/macos/agent/src/index.ts`,`ARCHITECTURE.md`,`omi-tool-manifest.ts:438` |
| 手表 | `app/ios/omiWatchApp/WatchAudioRecorderViewModel.swift`,`app/lib/services/devices/transports/watch_transport.dart` |
| 设备协议 | `sdks/device/PROTOCOL.md` |

---

## 9. 结论

1. **数据流形态**:"端侧采集 → 服务端提取 → 共享数据平面 → 端侧执行 → 写回"的星型模型;手机转录的提取 100% 在服务端,桌面截屏的提取 100% 在本地。
2. **任务与目标分工**:action_items 由 Agent 执行并回写(执行器);goals 由对话文本驱动进度更新(度量衡),任务侧 `goal_id`/`workstream_id` 提供三级挂靠,但无自动级联。
3. **状态机设计分层**:goal 有强状态机(事务 + 幂等 + 独占槽位 + 关联清理),task 有弱状态机(状态+completed 一致性),candidate/workstream 有各自生命周期——职责边界清晰。
4. **IA 架构特征**:执行分散、数据集中、服务端绝不执行 Agent;所有端共享 `users/{uid}/` 池;安全边界(加密、denylist、审计日志、PII 脱敏日志)贯穿全链。
