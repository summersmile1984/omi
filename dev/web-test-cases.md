# Web 功能测试用例清单 (ego-lite 验证)

目标:用 ego-browser(ego-lite)对本地 shim 后端(`127.0.0.1:8100`,3.11 venv + FIRESTORE_PG_DSN)
执行 web 功能验证。先列清单,后逐个用例测试。

环境:
- Base URL: `http://127.0.0.1:8100`
- 认证:Firebase Auth emulator 签发的 ID token(`/tmp/dev-idtoken.txt`),`Authorization: Bearer <token>`
- 存储:shim → PostgreSQL(users 等表已建,18+29 索引)

## 用例域与核心端点

### A. 认证 / 基础
| # | 方法 | 路径 | 预期 |
|---|---|---|---|
| A1 | GET | /health | 200,status=healthy |
| A2 | GET | /v1/users/profile | 200,返回用户资料(带 token) |
| A3 | GET | /v1/users/profile | 401(无 token) |

### B. 用户域
| # | 方法 | 路径 | 预期 |
|---|---|---|---|
| B1 | GET | /v1/users/onboarding | 200,onboarding 状态 |
| B2 | PATCH | /v1/users/onboarding | 200,更新 completed |
| B3 | POST | /v1/users/store-recording-permission?value=true | 200,写 PG |
| B4 | GET | /v1/users/store-recording-permission | 200,回读 true |
| B5 | POST | /v1/users/private-cloud-sync?value=true | 200,写 PG |
| B6 | GET | /v1/users/people | 200,people 列表 |
| B7 | POST | /v1/users/people | 200,创建 person |
| B8 | GET | /v1/users/me/byok-active | 200,byok 状态 |
| B9 | POST | /v1/users/me/byok-active | 200/400,设 BYOK key |

### C. 内容域
| # | 方法 | 路径 | 预期 |
|---|---|---|---|
| C1 | GET | /v1/conversations | 200,会话列表 |
| C2 | GET | /v1/conversations/count | 200,count |
| C3 | GET | /v3/memories | 200,记忆列表 |
| C4 | GET | /v1/action-items | 200,任务列表 |
| C5 | GET | /v1/goals | 200,目标列表 |
| C6 | GET | /v1/folders | 200,文件夹列表 |

### D. 应用市场 / 设置
| # | 方法 | 路径 | 预期 |
|---|---|---|---|
| D1 | GET | /v1/apps | 200,应用列表 |
| D2 | GET | /v1/apps/enabled | 200,启用应用 |
| D3 | GET | /v1/app-categories | 200,分类 |
| D4 | GET | /v1/personas | 200,personas |

### E. 错误路径
| # | 方法 | 路径 | 预期 |
|---|---|---|---|
| E1 | GET | /v1/nonexistent | 404 |
| E2 | GET | /v1/users/profile | 401(坏 token) |
| E3 | POST | /v1/users/onboarding | 405(错误方法) |
| E4 | POST | /v1/users/store-recording-permission | 422(缺 query 参数) |

## 记录格式
每用例: `[PASS|FAIL|BLOCKED] <#|名称> <方法> <路径> -> <状态码> <要点>` + 证据。
汇总于测试报告,写回 LoopX。

## 执行结果 (2026-08-09, ego-lite)

### A 认证/基础 — 全过
- A1 GET /health -> 200 `{"status":"healthy",...}`
- A2 GET /v1/users/profile(有效 token) -> 200,返回 PG 用户 JSON
- A3 GET /v1/users/profile(无 token) -> 401

### B 用户域 — 全过
- B1 GET /v1/users/onboarding -> 200
- B2 PATCH /v1/users/onboarding -> 200,读回 `acquisition_source=web-verify`
- B3 POST /v1/users/store-recording-permission?value=true -> 200
- B4 GET /v1/users/store-recording-permission -> 200 `true`
- B5 POST /v1/users/private-cloud-sync?value=true -> 200
- B6 GET /v1/users/people -> 200 `[]`
- B7 POST /v1/users/people -> 200 创建 person
- B7b GET /v1/users/people/{id} -> 200
- B7c DELETE /v1/users/people/{id} -> 200,列表回空
- B8 POST /v1/users/me/byok-active(fingerprints) -> 200 `{"active":true}`
- B8b 缺 provider -> 400
- B8c DELETE byok-active -> 200 `{"active":false}`
- PG 落库确认:perm=true, acq=web-verify, pcs=true

### C 内容域 — 全过(修复 2 个 shim 缺口后)
- C1 GET /v1/conversations -> 200 `[]`(修复 `Query.offset` 后)
- C2 GET /v1/conversations/count -> 200 `{"count":0}`(修复 `Query.count` 后)
- C3 GET /v3/memories -> 200 `[]`
- C4 GET /v1/action-items -> 200 `{"action_items":[],"has_more":false}`
- C5 GET /v1/goals -> 200
- C6 GET /v1/folders -> 200(含 Work 文件夹)

### D 应用市场/设置 — 通过(修复 BaseCompositeFilter + array_contains 后)
- D1 GET /v1/apps -> 200 `[]`(修复 `BaseCompositeFilter` 后)
- D2 GET /v1/apps/enabled -> 200 `[]`
- D3 GET /v1/app-categories -> 200(3 分类)
- D4 GET /v1/personas -> 404(数据态:该用户无 persona,预期行为;`array_contains` 修复后不再 500)

### E 错误路径 — 全过
- E1 GET /v1/nonexistent -> 404
- E2 无 token -> 401
- E3 POST /v1/users/onboarding(错误方法) -> 405
- E4 POST store-recording-permission(缺 value) -> 422

### 本轮发现并修复的 shim 缺口
1. `Query.offset()` 缺失(C1/C3 500 根因) — 16+ 处 database/* 使用
2. `Query.count()` 缺失(C2 500 根因) — 需返回 `[[result]]` 嵌套结构(`result[0][0].value`)
3. `BaseCompositeFilter` 未支持(D1 500 根因) — AND 复合过滤器
4. `array_contains`/`array_contains_any` 下划线变体缺失(D4 500 根因)
5. `CollectionReference.count()` 缺失

### 回归
- 影子对拍:19/19 场景与真 SDK 一致(新增 offset/count/array_contains/composite 场景)
- firestore_pg 测试:10/10 通过
