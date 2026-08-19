# Omi 云中立化:Firestore SDK Shim + 三 Emulator 本地开发方案

> 目标:在**不修改业务代码**的前提下,将 memweft fork 的存储层从 Google Cloud Firestore 迁移到自托管 PostgreSQL,并通过 Firestore/Auth/Storage 三 emulator 支撑本地开发与影子对拍。
> 方法:**实现 `google.cloud.firestore` 的 drop-in 兼容层(shim)**,让 `database/*.py` 88 个业务模块以为自己在访问 Firestore。

---

## 1. 结论摘要

| 问题 | 答案 |
|---|---|
| 业务代码要不要改? | **0 行**(88 个 `database/*.py` 模块、routers、utils 全部不动) |
| 需要新写什么? | 一个 `firestore-pg` 兼容包(~2000-4000 行)+ 部署编排 + 迁移脚本 |
| 本地开发怎么跑? | Firestore/Auth/Storage **三 emulator**,后端已有原生支持,纯 env 切换 |
| 迁移路径 | 三 emulator 先行 → 影子对拍验证 → shim 生产切换 |
| 总工期 | 核心 shim ~1.5-2 月;emulator 本地跑通 1-2 天 |

**决定性前提(全部代码验证过):**

1. 仓库已有 `db = _LazyFirestoreClient()` 单点注入(`database/_client.py:159`)
2. **无任何实时监听依赖**(无 `on_snapshot`/watch)——Firestore 最难 shim 的 watch 流完全不需要
3. 调用面是**可枚举的 SDK 子集**:8 个核心方法 + 5 个字段操作
4. **后端已原生支持 emulator**:`main.py:111-120`(Auth emulator 分支)、`utils/env_loader.py:117,179`(自动剥离真实凭据)、测试已用 `FIRESTORE_EMULATOR_HOST`

---

## 2. Shim 架构

### 2.1 总览

```
┌─────────────────────────────────────────────┐
│ database/*.py 88 模块 (零改动)               │
│   from ._client import db                    │
│   from google.cloud import firestore         │
├─────────────────────────────────────────────┤
│ ① 注入点: database/_client.py                │
│    get_firestore_client() → 返回 shim client │
│    (切换真实/模拟:env 或模块开关)             │
├─────────────────────────────────────────────┤
│ ② firestore-pg 包 (drop-in 同名)             │
│    sys.modules['google.cloud.firestore']     │
│    = firestore_pg.compat  (import 零改动)     │
│    实现: Client / CollectionReference /      │
│    DocumentReference / Query / Transaction / │
│    FieldFilter / ArrayUnion / ArrayRemove /  │
│    Increment / DELETE_FIELD / SERVER_        │
│    TIMESTAMP / transactional 装饰器          │
├─────────────────────────────────────────────┤
│ ③ SQLAlchemy 2.0 → PostgreSQL                │
│    JSONB + 查询列 + pgvector + tsvector      │
└─────────────────────────────────────────────┘
```

### 2.2 关键机制:import 零改动

业务模块的 import 形态(已统计):

```
32 个模块: from google.cloud import firestore
27 个模块: from google.cloud.firestore import FieldFilter / ArrayUnion / ...
 1 个模块: from google.cloud.firestore_v1.base_query import FieldFilter
```

**方案**:shim 包 `firestore_pg` 在加载时把自己注册为 `google.cloud.firestore` 与 `google.cloud.firestore_v1` 的替代模块(`sys.modules` 别名)。所有 `from google.cloud import firestore` 无需改动,`db.collection(...)`, `firestore.transactional`, `firestore.FieldFilter`, `firestore.ArrayUnion` 全部解析到 shim 实现。

> 备选(更保守):只让 `_LazyFirestoreClient` 返回 shim client,import 行不动、但 `google.cloud.firestore` 仍走官方包——仅当官方包的类型/常量(shim 用不到)在业务代码里没直接使用时的降级方案。已验证业务代码用到 `firestore.transactional/FieldFilter/ArrayUnion/ArrayRemove/Increment/DELETE_FIELD/SERVER_TIMESTAMP`,全部需要 sys.modules 方案覆盖。

### 2.3 调用面全集(代码验证的完整清单)

| SDK 元素 | 用法 | 映射 | 备注 |
|---|---|---|---|
| `db.collection(path)` | 854 处 | 表解析 | 顶层集合 = 表 |
| `db.collection('users').document(uid).collection(...)` | 43 处嵌套 | 表名 + `uid` 列 | `users/{uid}/<coll>` → 表 `<coll>`,带 uid 列 |
| `.document(id)` | 832 处 | 主键行 | doc id 作主键 |
| `.get()` / `.stream()` | 247 stream | SELECT | |
| `.set(data, merge=)` | 362 处 | INSERT ON CONFLICT / UPSERT | merge=True → 部分字段 |
| `.update(dict)` | 309 处 | UPDATE | **支持点路径 key**如 `feature.model.input_tokens` |
| `.delete()` | 184 处 | DELETE | |
| `.where(filter=FieldFilter)` | 240/256 | WHERE | `==, !=, >, >=, <, <=, in, not-in, array-contains, array-contains-any` |
| `.order_by()` / `.limit()` | 62/114 | ORDER BY / LIMIT | |
| `.transaction()` + `@firestore.transactional` | 87 装饰器 / 246 `get(transaction=` | BEGIN/COMMIT + SELECT FOR UPDATE | 核心难点,见 2.5 |
| `collection_group(name)` | 6 个模块 | 全表查询(uid 列) | llm_usage / fcm_tokens / fair_use_state / conversations |
| `ArrayUnion` / `ArrayRemove` | 12/2 | jsonb `\|\|` / `-` 元素 | |
| `firestore.Increment(n)` | 18 处 | `col = col + n` | llm_usage 原子计数 |
| `firestore.DELETE_FIELD` | 26 处 | jsonb 删键 / 列置 NULL | |
| `firestore.SERVER_TIMESTAMP` | 5 处 | `now()` | notifications / conversations |
| `DocumentSnapshot.exists/to_dict/id` | 普遍 | shim 数据类 | |
| `create()`(不存在才写) | 少量 | INSERT ... ON CONFLICT DO NOTHING | |

**不需要实现**(验证无使用):watch 流、on_snapshot、offline 持久化、安全规则、分布式配额、get_all。

### 2.4 数据模型:集合 → 表

```
顶层集合(users 等单层使用)          → users 表(JSONB data 列 + 查询列)
users/{uid}/<coll> 嵌套集合          → <coll> 表(uid 列 + doc_id 主键 + JSONB data)
   实际集合清单(45+): action_items, memories, conversations, goals, workstreams,
   tasks, messages, chat_sessions, staged_tasks, fcm_tokens, llm_usage,
   fair_use_state, screen_activity, memory_items, folders, files, events, ...

表结构(以 action_items 为例):
  CREATE TABLE action_items (
    uid      TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    data     JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    -- 查询列(由 firestore_index_registry 推断):
    status   TEXT,
    created_at_ts TIMESTAMPTZ,
    ...
    PRIMARY KEY (uid, doc_id)
  );
```

**列提升策略**:
- 查询列:凡在 `where`/`order_by` 出现的字段,由注册表提升为真实列(或生成表达式索引 `data->>'status'` + 索引)——零迁移成本
- 现有 `database/firestore_index_registry.py`(含 collection_group 索引定义)直接复用,机械翻译为 `CREATE INDEX`

### 2.5 事务翻译(最难点)

现状:87 个 `@firestore.transactional` 装饰器 + 246 处 `get(transaction=)`,典型模式:

```python
@firestore.transactional
def apply(write_transaction):
    _validate_generation(_control_ref(uid).get(transaction=write_transaction), ...)
    # ... 读多个文档 → 校验 → 写
    write_transaction.set(...); write_transaction.update(...)
```

Firestore 语义:文档级版本号冲突检测 + 冲突自动重试(整个函数体重跑)。

**PG 翻译策略**:

```
@transactional 装饰器 shim:
  ① BEGIN ISOLATION LEVEL REPEATABLE READ
  ② 函数体内 transaction.get() → SELECT ... FOR UPDATE(对每个读到的 doc id)
  ③ commit 冲突(serialization_failure) → 捕获 → 重跑函数体(复用现有
     firestore_transaction_retry.py:48 的 run_with_transaction_contention_retry)
  ④ 事务内 get 的文档,业务代码只依赖 exists/to_dict(读后写一致性)
```

- **读后写一致性**:`get(transaction=)` 全量 `FOR UPDATE`(或 `REPEATABLE READ` 快照),与 Firestore 的"事务内读反映最新已提交"对齐
- 嵌套事务:已验证 92 处装饰器内部会调用 `get(transaction=write_transaction)`——同事务对象传递,shim 保证 `get` 走到同一条 PG 连接
- `run_transactional()`(现有 _client.py:132,attempts=3)与 retry 封装**原样复用**

### 2.6 点路径 update 与原子操作

```python
# llm_usage.py 模式:嵌套 key 原子累加
session_ref.update({f"{feature}.{model}.input_tokens": firestore.Increment(n)})
# → UPDATE ... SET data = jsonb_set(data, '{feature,model,input_tokens}',
#                                COALESCE(data#>>'{...}', '0')::int + n)
# → 或列提升:llm_usage 表加 input_tokens/output_tokens 真实列
```

- `Increment` → `jsonb_set` + 表达式(18 处,集中在 llm_usage)
- `ArrayUnion/ArrayRemove` → `jsonb ||` 去重合并 / `-` 数组元素
- `DELETE_FIELD` → `data - 'key'`(jsonb 删键)
- `SERVER_TIMESTAMP` → `now()`

### 2.7 向量检索桥接

- Pinecone 已 fail-open(`vector_db.py:100` 无 env 不初始化,全部操作 warning 跳过)
- 生产切 shim 时,Pinecone 可作为**独立外部服务保留**(它本就不是 Firestore 的一部分),或后续换 pgvector
- **决策:shim 阶段不碰向量**——减少改动面,向量是独立可替换组件

### 2.8 需要修的现有代码(极小,非业务逻辑)

| 位置 | 改动 |
|---|---|
| `database/_client.py` | `get_firestore_client()` 内:emulator/shim 开关,返回 shim client |
| 无其他 | 其余 88 模块 0 改动 |

---

## 3. 三 Emulator 本地开发方案

**后端已原生支持(代码证据)**,无需任何代码改动:

### 3.1 证据清单

| 能力 | 代码位置 |
|---|---|
| Auth emulator 分支(剥真实凭据、demo 项目) | `main.py:111-120` |
| env_loader 自动跳过真实凭据 | `utils/env_loader.py:117,179` |
| 桌面后端 Auth emulator | `desktop_backend.py:34` |
| 测试已用 FIRESTORE_EMULATOR_HOST | `testing/contracts/test_desktop_backend_parity.py:12` |
| firebase-admin 6.5.0(≥6.0.0 原生支持 Auth emulator) | `requirements.txt:48` |

### 3.2 docker-compose.dev.yml

```yaml
services:
  firestore-emulator:
    image: mtlynch/firestore-emulator:latest   # 或 gcr.io/google.com/cloudsdktool/cloud-sdk
    ports: ["8080:8080"]
    environment:
      FIRESTORE_EMULATOR_PERSISTENCE_ENABLED: "1"
    volumes: [firestore-data:/opt/firestore]

  auth-emulator:     # 与 Firestore 同套件,实际可用单容器 firebase emulators:start
    image: node:20   # 或官方 firebase-tools 镜像
    command: ["npx", "firebase-tools", "emulators:start", "--only", "auth,firestore,storage", "--project", "demo-omi-local"]
    ports: ["9099:9099", "9199:9199"]

  redis:
    image: redis:7
    ports: ["6379:6379"]

  parakeet-stt:      # STT 自托管
    image: ghcr.io/omi/parakeet:latest
    ports: ["8883:8883"]

volumes:
  firestore-data:
```

> 注意:Firestore/Auth/Storage 三个 emulator 由官方 `firebase emulators:start --only firestore,auth,storage` 一个进程统一提供(默认端口 8080/9099/9199),单容器编排最简单。

### 3.3 环境变量切换(.env.dev)

```bash
# 存储
FIRESTORE_EMULATOR_HOST=localhost:8080
FIRESTORE_DATABASE_ID=(default)
FIREBASE_PROJECT_ID=demo-omi-local        # emulator 不校验项目名
# 认证
FIREBASE_AUTH_EMULATOR_HOST=localhost:9099
# 存储(GCS)
STORAGE_EMULATOR_HOST=localhost:9199
# 向量:不设 → Pinecone 自动降级跳过
# PINECONE_API_KEY=  (不设)
# 其余
REDIS_HOST=localhost
```

`main.py:111` 检测到 `FIREBASE_AUTH_EMULATOR_HOST` 后自动:`pop` 掉 ADC 凭据 → `initialize_app(projectId=demo-omi-local)` → `verify_id_token` 验证 emulator 签发的 token(Google 官方 6.x SDK 原生行为)。

### 3.4 启动流程

```bash
# 1. 起三 emulator
firebase emulators:start --only firestore,auth,storage --project demo-omi-local

# 2. 造测试用户(emulator 自带 REST,免真 Firebase)
curl -X POST localhost:9099/identitytoolkit.googleapis.com/v1/accounts:signUp \
  -H 'Content-Type: application/json' \
  -d '{"email":"dev@omi.local","password":"devpass","returnSecureToken":true}'
# → 返回 idToken,直接当 Bearer 用

# 3. 起后端
FIREBASE_AUTH_EMULATOR_HOST=localhost:9099 \
FIRESTORE_EMULATOR_HOST=localhost:8080 \
FIREBASE_PROJECT_ID=demo-omi-local \
uvicorn main:app --reload
```

### 3.5 移动端/桌面端连本地

| 端 | 配置 |
|---|---|
| iOS 模拟器 | Firebase SDK 设 `FirebaseConfiguration.shared.setEmulatorSettings(host:port)`;localhost 直达 |
| Android 模拟器 | emulator host 用 `10.0.2.2` |
| 桌面端 | 已走 REST + API key,天然不依赖 Firestore |

---

## 4. 迁移路线(三阶段)

```
Phase 0(1-2 天):三 emulator 本地跑通
   后端 + 三 emulator + Redis → 全链路可开发
   验证:登录、记忆写入、对话、任务创建

Phase 1(1.5-2 月):实现 firestore-pg shim
   ① sys.modules 别名 + Client/Collection/Document/Query 骨架
   ② where/order_by/limit/stream + 表映射 + 索引翻译
   ③ @transactional + SELECT FOR UPDATE + 重试
   ④ Increment/ArrayUnion/DELETE_FIELD/SERVER_TIMESTAMP
   ⑤ collection_group + 点路径 update
   ⑥ 影子对拍(见 5)

Phase 2(1-1.5 月):生产切换 + 周边替换
   数据迁移工具(存量 Firestore 导出 → PG)
   认证换 OIDC(Keycloak/Authelia/logto)— 与 shim 并行、独立
   推送 FCM → ntfy/UnifiedPush;Crashlytics → Sentry
   Cloud Tasks → Redis 队列;Cloud Run → compose/K8s
```

## 5. 影子对拍(验证正确性的核心)

```
┌─────────────┐   双写   ┌──────────────┐
│ 业务调用     │────────→│ Firestore     │(旧,对拍基准)
│             │────────→│ PostgreSQL    │(shim)
└─────┬───────┘         └──────────────┘
      │ 读:主读 Firestore,shim 读 PG
      └→ 定时对拍器:比较两侧文档(collection/dock_id/data)
         差异 → 上报日志 → 修复 shim 翻译
```

- 由于**同一个调用面**同时打到两边,对拍器可以直接用业务自身的 `to_dict()` 结果做字段级 diff
- emulator 持久化数据即对拍基准;跑满 1-2 周的业务路径 + 全量单元测试(现有测试直接跑在 shim 上,天然适配)

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 事务语义不精确(冲突重试次数、FOR UPDATE 粒度) | 现有 `run_with_transaction_contention_retry` 复用;对拍阶段重点压测并发写 |
| sys.modules 别名影响第三方库 | 别名作用域仅包加载期;测试隔离验证(项目已有 import 副作用扫描脚本可扩展) |
| collection_group 索引翻译遗漏 | 复用 firestore_index_registry,机械翻译 + 对拍覆盖 |
| 点路径 update 语义歧义 | 枚举 18+ 处,llm_usage 走列提升,其余走 jsonb_set |
| emulator 性能不代表生产 | emulator 只用于开发/对拍;生产走 PG |
| 存量数据迁移中断 | 导出 gcloud 格式 → 分批导入,断点续传 |

## 7. 附件:验证过的代码证据

| 事实 | 证据 |
|---|---|
| 单点注入存在 | `database/_client.py:159` `db = _LazyFirestoreClient()` |
| 无实时监听 | rg 全库无 on_snapshot/watch |
| import 面 | 32 模块 `from google.cloud import firestore`;27 模块 `from google.cloud.firestore import ...`;1 模块 `firestore_v1.base_query` |
| 事务面 | 87 `@firestore.transactional`;246 `get(transaction=`;18 `Increment`;12 `ArrayUnion`;2 `ArrayRemove`;26 `DELETE_FIELD`;5 `SERVER_TIMESTAMP` |
| collection_group | 6 模块(llm_usage/fcm_tokens/fair_use_state/conversations 等) |
| 嵌套集合 | 43 处 `users/{uid}/` 模式 |
| Auth emulator 原生支持 | `main.py:111-120`,`env_loader.py:117,179`,`desktop_backend.py:34` |
| firebase-admin 版本 | 6.5.0(≥6.0.0 支持 Auth emulator) |
| Pinecone fail-open | `vector_db.py:100-102` 无 env 不初始化,全操作跳过 |
| 现有重试封装 | `firestore_transaction_retry.py:48` `run_with_transaction_contention_retry` |
| 复合索引注册表 | `database/firestore_index_registry.py`(可机械翻译为 PG 索引) |
| 测试 fake 先例 | `StrictFirestore`(已有 SDK 层 fake,证明替换层可行) |

## 8. 与之前文档的关系

- 本文是 `omi-cloud-neutral-postgres-migration.md` 的**演进**:原方案按"88 模块逐域重写"评估 4-6 人月;本文改为 SDK 兼容层(shim)方案,**业务代码 0 改动**,核心成本收敛到单个包
- emulator 方案替代了原"自建 OIDC 才能本地开发"的假设——**本地开发不需要 OIDC**,Auth emulator 零代码接入
- 生产认证替换(OIDC)、推送、队列等周边方案沿用原文档,与 shim 并行推进

## 9. 实现状态回填(2026-08-09)

`backend/firestore_pg/` 已实现并落地(`feature/cloud-neutral-shim` b6911dcba3),本节记录实际实现与验证证据,作为对 §2-§5 设计的实现确认。

### 9.1 已实现的机制

| 设计点 | 实现 | 验证 |
|---|---|---|
| import 零改动 | `compat.py`: `types.ModuleType` facade,启动时硬赋值 `sys.modules['google.cloud.firestore']`/`'google.cloud.firestore_v1'`,保存 `_real/_real_v1/_real_base_query` 转发未知属性(`from ...base_query import BaseCompositeFilter`、`LastUpdateOption` 等) | 88 模块全量 import 无错误 |
| 注入点 | `database/_client.py` + `database/__init__.py`:存在 `FIRESTORE_PG_DSN` 时提前安装 shim,`get_firestore_client()` 返回 shim Client | 3.11 venv + emulators 启动成功 |
| 集合→表 | `resolve_collection`: `users`→表 `users`(uid=''),`users/{uid}/<coll>`→表 `<coll>` + uid 列,嵌套级联(表名 `_` 拼接) | 嵌套 `users/{uid}/conversations` 读写通过 |
| 建表 | `ensure_table` 运行时自动建表(进程缓存),schema 18 表 | 启动日志确认 |
| 查询翻译 | `_build_query_sql`: 10 个操作符 → JSONB `->>`/`@>`/`?|`,named 参数(`:pN`);字符串 API `where('f','==',v)` 兼容;typed CAST(`CAST(:p AS timestamptz/boolean/double precision)`)解决 text vs 类型比较 | 启动期 `reconcile_after_at`/`wipe_status` 查询干净执行 |
| 事务 | `@firestore.transactional` + Transaction 同连接语义;真 SDK `_Transactional` 桥(`_begin/_commit/_rollback/_clean_up` 等);冲突抛 `Aborted`;`transaction=` 参数桥接 `get/stream` | 真实业务 wipe 事务函数、冒烟 count=3 通过 |
| 字段操作 | Increment/ArrayUnion/ArrayRemove/DELETE_FIELD(sentinel 保留后删),`set(merge=True)+transforms` 合并,ISO-8601→datetime 读回转换 | `record_user_platform` 去重、byok set/clear 通过 |

### 9.2 踩坑记录(后续维护者必读)

1. **psycopg pyformat 陷阱**: `:data::jsonb` 会把 `:jsonb` 当参数名 → 一律写 `CAST(:data AS jsonb)`
2. **SQLAlchemy 2.0**: `engine.connect()` 上下文结束不提交(静默回滚)→ 非事务路径必须 `engine.begin()`;参数必须 named dict
3. **merge 方向**: `EXCLUDED.data || {table}.data` 是旧的赢(Firestore 语义是新字段覆盖)→ 必须 `{table}.data || EXCLUDED.data`
4. **facade 必须是真模块**: `types.ModuleType` 子类 + 复制 `__spec__/__loader__/__file__`,否则 importlib 报 `(unknown location)`
5. **子模块属性**: `BaseCompositeFilter` 在 `firestore_v1.base_query` 子模块、`LastUpdateOption` 在 `firestore_v1` 包级——facade 的 `__getattr__` 转发链要含 `_real_v1` 和 `_real_base_query`
6. **`update` 对不存在文档抛 `ValueError`**(对齐真 SDK NotFound):业务必须先建文档(onboarding 流程)再 update

### 9.3 验证证据(真机执行)

```
uvicorn main:app (3.11 venv + FIRESTORE_PG_DSN + 三 emulator) → /health 200,启动无错误
GET  /v1/users/onboarding                       → 200 (读 PG)
PATCH /v1/users/onboarding                      → 200 (创建 users 文档,PG 落库)
POST /v1/users/store-recording-permission       → 200 (update 路径,PG 确认 perm=true)
POST /v1/users/private-cloud-sync               → 200 (PG 确认 pcs=true)
GET  /v1/users/store-recording-permission       → 200 读回 true
嵌套 users/{uid}/conversations set/get/where    → 通过
```

### 9.4 未完成项

- 影子对拍器(§5)未实现——当前以服务器端真实端点 + 冒烟测试作为验证基准
- `firestore_index_registry` 复合索引翻译未做(shim 按单字段 JSONB 查询,暂无复合索引需求)
- 文档 `README`(安装/运行/迁移说明)未落地,优先在迁移路线文档中补齐
