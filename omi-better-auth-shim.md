# Firebase Auth → Better Auth shim 方案

目标: 4C8G 自托管部署时,用 **Better Auth** 替代 Firebase Auth,业务代码零/极少改动。

## 一、认证现状(代码确认)

```
移动端 (Flutter firebase_auth SDK)
  └─ 登录 → 拿 Firebase ID Token (标准 JWT, ES256, Google 公钥验证)
        ↓ Authorization: Bearer <token>
后端 (Python)
  └─ utils/other/endpoints.py:127  auth.verify_id_token(token)  ← 唯一核心验证入口
       → decoded_token['uid']
```

- 核心验证入口**只有一处**: `utils/other/endpoints.py:91 verify_token()` → `auth.verify_id_token()`
- 所有 HTTP 请求(248+ 端点)经 `get_current_user_uid` 依赖它
- 其他 `firebase_admin.auth.*` 用法(create_user/import_users/get_user)是管理面,低频
- `ADMIN_KEY` 已有自建认证通道(4C8G 可用它,无需 Firebase)

## 二、方案: Better Auth 签发 JWT,Python 侧 shim 验证

Better Auth(TS 服务,与 PG 共存)做真正实现;Python 后端把 `verify_id_token` 换成验证 Better Auth JWT。

```
移动端 (Better Auth SDK 或任意 OIDC client)
  └─ 登录 → Better Auth 签发 JWT (ES256/EdDSA, 含 uid claim)
        ↓ Authorization: Bearer <token>
后端 shim (Python, utils/auth_shim.py)
  └─ verify_id_token(token)  ← 替代 firebase_admin.auth.verify_id_token
       → pyjwt.decode(token, public_key, algorithms=[ES256/EdDSA])
       → decoded['uid']
```

### Better Auth 侧(TS, 独立小服务或同机)
- `betterAuth({ emailAndPassword, jwt({ jwks: { keyPairConfig: { alg: 'ES256' } } }) })`
- 提供 `/jwks` 端点(公钥),签发带 `uid` claim 的 JWT
- 用户数据存 **PostgreSQL**(和 shim 同一库,加 auth schema)

### Python shim 侧(核心: 一个函数)
```python
# utils/auth_shim.py
import jwt, os, requests

JWKS_URL = os.getenv("AUTH_JWKS_URL", "http://localhost:3000/jwks")

def verify_id_token(token: str) -> dict:
    """Drop-in for firebase_admin.auth.verify_id_token: verify Better Auth JWT."""
    jwks = _get_jwks()  # 缓存, 定期刷新
    unverified = jwt.get_unverified_header(token)
    key = next(k for k in jwks["keys"] if k["kid"] == unverified["kid"])
    claims = jwt.decode(
        token,
        key,
        algorithms=[unverified["alg"]],
        options={"verify_aud": False},  # Better Auth 默认不带 aud
    )
    # 返回形状与 Firebase 一致: {'uid': ..., 'sub': ...}
    return {"uid": claims.get("uid") or claims.get("sub"), **claims}
```

### 接入点(零改动业务)
- `utils/other/endpoints.py:127`:`auth.verify_id_token(token)` 换成 `auth_shim.verify_id_token(token)`
- 或更干净:在 `verify_token()` 里加一个分支——`AUTH_PROVIDER=better_auth` 时走 shim,否则走 Firebase(兼容切换)

## 三、为何可行

| 维度 | 现状 | Better Auth shim |
|---|---|---|
| 核心验证 | `firebase_admin.auth.verify_id_token`(1 处) | 替换为 pyjwt 验证(1 处) |
| token 格式 | 标准 JWT(ES256) | 标准 JWT(ES256/EdDSA,pyjwt 支持) |
| 返回形状 | `{'uid': ...}` | shim 保证 `{'uid': ...}` 一致 |
| 用户存储 | Firebase Auth(云) | PostgreSQL(自托管) |
| 数据库 | — | 复用已有 PG |
| 部署 | 云服务 | 4C8G 上 TS 小服务 + Python shim |
| 依赖 | firebase_admin | pyjwt(已装 2.13.0) |

## 四、需要处理的边界

1. **移动端 SDK**: Flutter 端 `firebase_auth` 换 Better Auth client(或 OIDC)。这是前端改动,影响面大于后端。
2. **管理面 API**: `auth.create_user` / `import_users` / `get_user`(低频)——shim 需映射到 Better Auth 管理 API 或 PG 直读。
3. **FCM 推送**: `firebase_admin.messaging`(47 处)与 Auth 解耦,可单独保留或换自建推送——不在本方案范围。
4. **token 生命周期**: Better Auth 默认 session + JWT 插件;需确认 JWT 过期/刷新与移动端语义匹配。
5. **JWKS 刷新**: shim 需缓存公钥并按 kid 刷新(标准做法)。

## 五、验证标准

1. `AUTH_PROVIDER=better_auth` 时,`get_current_user_uid` 用 Better Auth JWT 认证成功
2. 返回 `uid` 与 Better Auth 用户一致
3. `AUTH_PROVIDER=firebase` 时,行为与现在完全一致(回归)
4. 移动端用 Better Auth 登录 → 调受保护端点 → 200
5. 4C8G 上: Better Auth(TS, ~100MB)+ shim(无额外内存)可行

## 六、工作量评估

| 项 | 工作量 |
|---|---|
| Better Auth 服务(TS, PG 存储, JWT+JWKS) | 小(标准配置) |
| Python shim `verify_id_token` | 极小(1 函数 + pyjwt) |
| 接入点切换 | 极小(1 分支) |
| 移动端 SDK 更换 | 中(Flutter auth 流程) |
| 管理面 API 映射 | 中(低频,可后置) |

**核心结论**: Better Auth shim 可行且改动集中——后端只需 1 个 shim 函数 + 1 个切换分支;主要工作量在移动端 SDK 和 Better Auth 服务本身。4C8G 部署完全支持。
