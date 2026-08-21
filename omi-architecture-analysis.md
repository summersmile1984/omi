# Omi (BasedHardware) 系统架构分析

> 基于 2026-08-07 shallow clone (`git clone --depth 1 https://github.com/BasedHardware/omi.git`) 代码分析。
> 代码规模:63.5 万行(Flutter 67.8w / Swift 30.7w / Backend 54w / 固件 C 12.2w)

---

## 一、云端一体架构(端到端)

```mermaid
flowchart TB
    subgraph DEVICES["设备端 (Client)"]
        NECK["Omi 项链<br/>nRF52 Zephyr C<br/>麦克风+BLE"]
        GLASS["Omi Glass<br/>ESP32-S3 C<br/>相机+麦克风"]
        PHONE["手机 App<br/>Flutter (iOS/Android)<br/>BLE 音频中继"]
        DESKTOP["桌面端<br/>SwiftUI + Python<br/>屏幕捕获+麦克风"]
        NECK -- "BLE音频" --> PHONE
        GLASS -- "BLE音频" --> PHONE
    end

    subgraph EDGE["接入层"]
        WS["backend-listen<br/>WebSocket 实时音频<br/>Helm Chart"]
        API["backend (FastAPI)<br/>60+ routers<br/>REST + SSE"]
        DESKWS["desktop_realtime<br/>桌面实时通道"]
    end

    subgraph AI_PIPE["音频智能管线 (GPU)"]
        VAD["VAD<br/>语音活动检测"]
        ASR["Parakeet ASR<br/>GPU Worker (NIM)"]
        DIA["Diarizer<br/>说话人分离"]
        NLLB["NLLB 翻译<br/>多语言"]
    end

    subgraph LLM["LLM 网关 (llm-gateway)"]
        LANES["Lane 路由<br/>quality/latency/cost 目标"]
        PROVIDERS["Provider:<br/>Anthropic · OpenAI · Gemini"]
        BYOK["BYOK 自携密钥<br/>+ omi_paid 兜底"]
        ACCT["计费会计<br/>output_budget 限流"]
    end

    subgraph MEMORY["记忆系统 (canonical)"]
        EXTRACT["LLM 结构化抽取<br/>facts/evidence"]
        LEDGER["memory_ledger<br/>原子账本事务"]
        OUTBOX["outbox worker<br/>异步重试"]
        LT["Long-term 记忆<br/>lineage 血缘"]
        VEC["Pinecone<br/>向量检索"]
    end

    subgraph AGENTS["Agent 层"]
        VM["Agent VM<br/>云端虚拟机"]
        MCP["MCP Server<br/>SSE 协议"]
        PROXY["agent-proxy<br/>网络隔离"]
    end

    subgraph DATA["数据层 (GCP)"]
        FS[("Firestore<br/>主数据库<br/>文档型 NoSQL")]
        GCS[("GCS 对象存储<br/>音频文件")]
        REDIS[("Redis<br/>缓存/队列")]
        TYPESENSE[("Typesense<br/>全文搜索")]
        TASKS[("Cloud Tasks<br/>异步任务")]
    end

    subgraph PUSH["触达层"]
        PUSHER["pusher<br/>FCM/APNs 推送"]
        NOTIF["通知智能<br/>proactive_notification"]
    end

    PHONE --> WS
    DESKTOP --> DESKWS
    DESKTOP --> API
    WS --> API
    DESKWS --> API

    API --> VAD --> ASR --> DIA --> NLLB --> API
    API --> LANES --> PROVIDERS
    API --> BYOK
    LANES --> ACCT

    API --> EXTRACT --> LEDGER --> OUTBOX --> LT
    LT --> VEC
    VEC --> API

    API --> VM --> PROXY
    API --> MCP

    API --> FS
    API --> GCS
    API --> REDIS
    API --> TYPESENSE
    API --> TASKS
    LT --> FS
    ASR --> GCS

    API --> PUSHER --> NOTIF
    NOTIF --> PHONE
```

### 部署形态(GCP 原生,Kubernetes)
- `infrastructure/opentofu/`:foundation 基建 + environments 环境 + pilots
- `backend/charts/`(Helm):`backend-listen`、`llm-gateway`、`agent-proxy`、`agent-vm-firewall/reaper`、`vad`、`diarizer`、`parakeet`(GPU)、`nllb-translation`、`pusher`、`deepgram-self-hosted`、`monitoring`
- `backend/modal/`:Modal serverless 跑定时任务(memory_maintenance、notifications、vad、speech_profile)
- `backend/jobs/`:agent_vm_reconciler、short_term_lifecycle_worker(云调度)

### 同步一致性设计
- 设备→云端:**WAL(Write-Ahead Log)** 双端同步,`sync_ledger` 保证幂等
- 记忆写入:memory_ledger 原子事务 + outbox 异步重试 + 向量 repair worker 自愈

---

## 二、AI 架构

```mermaid
flowchart LR
    subgraph INPUT["多模态输入"]
        A1["对话音频<br/>(项链/眼镜/手机)"]
        A2["屏幕内容<br/>(桌面捕获)"]
        A3["通话音频<br/>(手机电话)"]
    end

    subgraph STAGE1["感知层 (实时)"]
        S1["VAD 端点检测"]
        S2["Parakeet ASR<br/>NVIDIA GPU"]
        S3["说话人分离<br/>diarizer"]
        S4["NLLB 翻译"]
    end

    subgraph STAGE2["理解层 (LLM 编排)"]
        U1["transcript_chunks<br/>切片"]
        U2["结构化抽取<br/>LLM(JSON schema)"]
        U3["冲突消解<br/>resolve_memory_conflict"]
        U4["主体推断<br/>infer_subject"]
    end

    subgraph STAGE3["记忆层 (canonical)"]
        M1["Short-term 记忆"]
        M2["晋升判定<br/>required_promotion"]
        M3["Long-term 记忆<br/>(graph+ledger)"]
        M4["向量化<br/>Pinecone"]
    end

    subgraph STAGE4["应用层 (生成)"]
        G1["Action Items<br/>待办抽取"]
        G2["摘要/通知<br/>headline"]
        G3["Goal/Workstream<br/>目标追踪"]
        G4["KG 知识图谱<br/>实体关系"]
        G5["Proactive 主动提醒<br/>时机预算"]
    end

    subgraph STAGE5["交互层 (检索)"]
        R1["Chat RAG<br/>记忆检索+对话"]
        R2["MCP 工具调用"]
        R3["Agent VM<br/>多步执行"]
        R4["Apps/Personas<br/>插件生态"]
    end

    A1 --> S1 --> S2 --> S3 --> S4
    A2 --> U1
    A3 --> S2
    S4 --> U1 --> U2 --> U3 --> U4
    U3 --> M1 --> M2 --> M3 --> M4
    M3 --> G1 & G2 & G3 & G4 & G5
    M4 --> R1
    M3 --> R1
    R1 --> R2 --> R3
    R1 --> R4
```

### LLM 网关设计(核心创新)
- **Lane 路由**:每个业务面(surface)一个 lane,声明质量/延迟/成本权重目标,由 resolver 动态选路(如 `chat-structured` 质量 0.6 / 延迟 0.2 / 成本 0.2)
- **Credential Policy**:`omi_paid`(公司付费)vs BYOK(用户自带密钥);BYOK 失败分类(`byok_auth`/`byok_quota`/`byok_rate_limit`…)决定可否降级到公司付费
- **路由版本化**:`active_route` / `last_known_good` 双指针,新路由灰度后可回滚
- **记账**:accounting 按 token 计量 + output_budget 防滥用 + cost_rate_cards 成本卡
- 提供商:Anthropic / OpenAI(兼容面)/ Gemini,统一 OpenAI-compatible surface

### 记忆抽取数据结构(事实三元组)
```
Memory = proposition(主语, 谓语, 宾语)
       + category + tags + headline
       + Evidence[] { source_id(对话), extractor_id, capture_confidence, independence_group }
       + subject_entity_id → KG 实体
       + veracity(置信度) + durability(持久期)
```

---

## 三、业务对象与关系

```mermaid
erDiagram
    USER ||--o{ DEVICE : "拥有"
    USER ||--o{ CONVERSATION : "产生"
    USER ||--o{ MEMORY : "拥有"
    USER ||--o{ TASK : "拥有"
    USER ||--o{ FOCUS_SESSION : "记录"
    USER ||--o{ GOAL : "设定"
    USER ||--o{ APP : "安装使用"
    USER ||--|| SUBSCRIPTION : "订阅"
    USER ||--o{ CHAT_SESSION : "对话"
    USER ||--o{ NOTIFICATION : "接收"
    USER ||--o{ CALENDAR_MEETING : "关联"

    DEVICE ||--o{ CONVERSATION : "采集"
    DEVICE ||--o{ RECORDING_SESSION : "产出"

    CONVERSATION ||--|{ TRANSCRIPT_SEGMENT : "包含"
    CONVERSATION ||--o{ MEMORY : "提取证据"
    CONVERSATION ||--o{ ACTION_ITEM : "抽取"
    CONVERSATION ||--o{ SUMMARY : "生成"

    MEMORY ||--o{ EVIDENCE : "引用"
    MEMORY ||--o{ MEMORY : "lineage 父子"
    MEMORY ||--|| KNOWLEDGE_GRAPH_ENTITY : "关联主体"
    MEMORY }o--o{ TASK : "驱动"

    TASK ||--o{ GOAL : "服务目标"
    TASK ||--o{ WORKSTREAM : "归属工作流"
    WORKSTREAM }o--|| GOAL : "推进"

    FOCUS_SESSION }o--o{ DISTRACTION : "记录分心"
    FOCUS_SESSION }o--o{ GOAL : "对齐目标"

    APP ||--o{ APP_USAGE : "使用记录"
    APP ||--o{ PROACTIVE_NOTIFICATION : "产生主动提醒"

    CHAT_SESSION ||--|{ MESSAGE : "包含"
    CHAT_SESSION }o--o{ MEMORY : "RAG 检索"
```

### 核心对象字段与生命周期

| 对象 | 关键字段 | 生命周期 |
|---|---|---|
| **User** | uid, plan(Free/Plus/Architect), subscription, 位置同意书 | 订阅驱动配额 |
| **Device** | 设备类型(项链/眼镜/手机/桌面), BLE 状态 | 配对→采集→同步 |
| **Conversation** | segments[], source(设备/桌面/电话/导入), status(post-processing 状态机) | 采集→转录→后处理→终态 |
| **Memory** | proposition, category, tags, evidence[], veracity, durability | short-term → **唯一晋升通道** → long-term;每级:archived/reviewed/rejected |
| **Task / ActionItem** | title, status, evidence_ref, provider(Asana/Linear…), priority | 对话抽取 → 用户确认 → 外部执行 → 完成 |
| **Goal** | type, status, metrics, relationship, source(work-intent) | 创建→对齐→进度事件→达成 |
| **Workstream** | status, sensitivity, events[], artifacts[] | 工作意图编排(2026 新增层) |
| **FocusSession** | distractions[], goals[], stats | 专注周期(对抗分心) |
| **App (Plugin)** | catalog, oauth, usage_history, personas | 应用商店分发 + 用量计量 |
| **KG Entity** | 实体类型(人/公司/概念), 关系边 | 图增强,随记忆晋升 |
| **ChatSession** | messages[], 记忆上下文窗口 | 检索增强对话,配额计费 |

### 关键关系语义
1. **证据链**:Memory → Evidence → Conversation(每段记忆可溯源到原始对话片段,支持红action)
2. **记忆晋升闭环**:short-term 只能通过 promotion 进入 long-term,且要求 ledger 事务原子记录晋升回执 + 图断言
3. **任务-目标-工作流**:Task 附 evidence 关联 conversation,归入 Workstream 或服务 Goal(双层归属)
4. **RAG 检索**:ChatSession 检索 Memory(向量)+ KG 遍历,默认折叠 lineage 血缘让同一逻辑记忆只出现一次

---

## 四、工程特质总结

1. **云原生重度**:Firestore + Pinecone + GCS + Cloud Tasks + Modal + Helm + Opentofu,「可自托管」是宣称,实际是 GCP 绑定
2. **双轨兼容**:legacy 记忆系统 ↔ canonical 系统并存,契约层隔离 + 灰度 rollout
3. **AI 可观测**:LLM gateway 有完整计费/降级/回滚/影子(shadow)机制
4. **治理即代码**:invariant 注册表(INV-*)+ 守卫测试 + 契约测试 + pylock 依赖锁
5. **业务演进方向**:2026 重心 = Agent(VM/MCP)→ 主动式 AI(proactive)→ 工作流(Workstream/Goal)→ 桌面屏幕理解
