# Mealie OIDC / Social Login 协作关系分析

## 1. 整体架构概览

Mealie 的认证系统采用 **Provider 设计模式**，将不同的认证方式（用户名密码、LDAP、OIDC）抽象为统一的 `AuthProvider` 接口。OIDC/Social Login 的完整流程横跨 **前端 (Nuxt/Vue)** 和 **后端 (FastAPI)** 两层，涉及 OAuth 重定向、外部身份断言、用户匹配、JWT 会话建立等多个阶段。

### 1.1 核心模块位置

| 模块 | 文件路径 | 职责 |
|------|----------|------|
| OIDC Provider | [openid_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py) | 解析 OIDC claims、用户匹配、权限判断 |
| Auth Provider 基类 | [auth_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py) | 定义认证接口、JWT token 生成、用户查询 |
| 认证路由 | [auth.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py) | OAuth 登录端点、回调处理、token 颁发 |
| Auth Cache | [auth_cache.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth_cache.py) | authlib OAuth 状态缓存 |
| 用户模型 | [users.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/db/models/users/users.py) | `AuthMethod` 枚举、User 表结构 |
| 配置项 | [settings.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/settings/settings.py#L364-L416) | OIDC_* 系列配置 |
| 当前用户依赖 | [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/dependencies/dependencies.py#L88-L123) | `get_current_user` 会话解析 |
| 前端登录页 | [login.vue](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/pages/login.vue) | 触发 OIDC 重定向、处理回调 |
| 前端认证状态 | [use-auth-backend.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-auth-backend.ts) | token 存储、会话获取、登出 |
| 前端 OIDC 封装 | [use-mealie-auth.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-mealie-auth.ts) | `oauthSignIn` 回调处理 |

---

## 2. 外部身份返回流程（OAuth 重定向 → Callback）

### 2.1 流程时序

```
用户浏览器                    Mealie 前端                  Mealie 后端                  OIDC IdP
    |                            |                           |                          |
    |  点击 "Login with OIDC"    |                           |                          |
    |--------------------------->|                           |                          |
    |                            |  oidcAuthenticate(false)  |                          |
    |                            |------                     |                          |
    |                            |      |                    |                          |
    |                            |  保存 pendingShareRedirect|                          |
    |                            |  到 sessionStorage        |                          |
    |                            |<-----                     |                          |
    |                            |  navigateTo(/api/auth/oauth, external=true)         |
    |<-------------------------------------------------------|                          |
    |                            |                           |                          |
    |  GET /api/auth/oauth                                                                     |
    |--------------------------------------------------------->|                          |
    |                            |                           |  oauth_login()           |
    |                            |                           |  oauth.authorize_redirect|
    |                            |                           |  (redirect_url=/login)   |
    |<---------------------------------------------------------|------------------------->|
    |                                                                                  |
    |  重定向到 IdP 登录页                                                                |
    |------------------------------------------------------------------------------------>|
    |                                                                                  |
    |  用户在 IdP 完成认证                                                                 |
    |<----------------------------------------------------------------------------------->|
    |                                                                                  |
    |  携带 code 重定向回 /login                                                           |
    |<-----------------------------------------------------------------------------------|
    |                            |                           |                          |
    |  onBeforeMount 检测到 code |                           |                          |
    |--------------------------->|                           |                          |
    |                            |  oidcAuthenticate(true)  |                          |
    |                            |  auth.oauthSignIn()      |                          |
    |                            |  GET /api/auth/oauth/callback?code=...                |
    |--------------------------------------------------------->|                          |
    |                            |                           |  oauth_callback()        |
    |                            |                           |  client.authorize_access_token()
    |                            |                           |------------------------->|
    |                            |                           |  取 token + userinfo     |
    |                            |                           |<-------------------------|
    |                            |                           |  OpenIDProvider.auth()   |
    |                            |                           |  返回 MealieAuthToken    |
    |<---------------------------------------------------------|                          |
    |                            |                           |                          |
    |                            |  setToken(access_token)  |                          |
    |                            |  getSession() → /users/self|                          |
    |                            |                           |                          |
```

### 2.2 前端触发重定向 — [login.vue](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/pages/login.vue#L325-L347)

用户点击 OIDC 登录按钮时调用 `oidcAuthenticate(callback=false)`：

```javascript
// login.vue L325-L347
async function oidcAuthenticate(callback = false) {
  if (callback) {
    // ... 处理回调场景
  } else {
    // 保存待跳转 URL（OIDC 重定向会刷新页面，query string 会丢失）
    const redirectTarget = route.query.redirect as string | undefined;
    if (redirectTarget && redirectTarget.startsWith("/")) {
      pendingShareRedirect.value = redirectTarget;
    }
    navigateTo("/api/auth/oauth", { external: true }); // 离开前端，进入后端 OAuth 端点
  }
}
```

关键点：
- 跳转前将 `redirect` 查询参数持久化到 **sessionStorage**（`pendingShareRedirect`），因为 IdP 回调会导致页面重载，原 URL 的 query 会丢失。
- 使用 `navigateTo(..., { external: true })` 触发浏览器级跳转。

### 2.3 后端 OAuth 登录端点 — [auth.py oauth_login()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L91-L112)

`GET /api/auth/oauth` 负责构建授权 URL 并跳转到 IdP：

```python
# auth.py L91-L112
@public_router.get("/oauth")
async def oauth_login(request: Request):
    client = oauth.create_client("oidc")
    redirect_url = None
    if not settings.PRODUCTION:
        redirect_url = "http://localhost:3000/login"  # 开发环境回退
    else:
        base = settings.BASE_URL or request.base_url   # 生产环境使用配置或请求头
        redirect_url = URLPath("/login").make_absolute_url(base)

    response: RedirectResponse = await client.authorize_redirect(request, redirect_url)
    return response
```

关键点：
- `redirect_url` 始终指向前端 `/login` 页面（而非后端 callback），这是 Mealie 的设计选择：IdP 先回调前端，前端再将 `code` 转发给后端 `/oauth/callback` 换取 token。
- 使用 **PKCE (S256)** 进行代码挑战（`code_challenge_method="S256"` 在 L52 注册时指定）。

### 2.4 OAuth 客户端初始化 — [auth.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L30-L52)

模块加载时通过 `authlib.integrations.starlette_client.OAuth` 注册 OIDC client：

```python
# auth.py L30-L52
if settings.OIDC_READY:
    oauth = OAuth(cache=AuthCache())  # 使用自定义内存缓存
    scope = None
    if settings.OIDC_SCOPES_OVERRIDE:
        scope = settings.OIDC_SCOPES_OVERRIDE
    else:
        groups_claim = settings.OIDC_GROUPS_CLAIM if settings.OIDC_REQUIRES_GROUP_CLAIM else ""
        scope = f"openid email profile {groups_claim}"
    # ... timeout, TLS 证书等参数
    oauth.register(
        "oidc",
        client_id=settings.OIDC_CLIENT_ID,
        client_secret=settings.OIDC_CLIENT_SECRET,
        server_metadata_url=settings.OIDC_CONFIGURATION_URL,  # .well-known/openid-configuration 自动发现
        client_kwargs={"scope": scope.rstrip()},
        code_challenge_method="S256",
    )
```

关键点：
- 缓存：使用自实现的 `AuthCache`（内存字典 + TTL）存储 OAuth 状态参数。
- Scope 构造：默认 `openid email profile`，如启用了 group claim 校验则附加 groups claim 名称。
- 通过 `server_metadata_url` 自动发现 IdP 的端点（授权、token、userinfo、jwks 等）。

### 2.5 后端 Callback 换 Token — [auth.py oauth_callback()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L115-L144)

前端拿到 `code` 后，通过 Axios 请求 `GET /api/auth/oauth/callback`：

```python
# auth.py L115-L144
@public_router.get("/oauth/callback")
async def oauth_callback(request: Request, session: Session = Depends(generate_session)):
    client = oauth.create_client("oidc")
    token = await client.authorize_access_token(request)  # 用 code 换 access_token + id_token

    auth = None
    try:
        # 第一优先：直接从 id_token 解析 userinfo
        auth_provider = OpenIDProvider(session, token["userinfo"])
        auth = auth_provider.authenticate()
    except MissingClaimException:
        try:
            # 第二优先：从 userinfo endpoint 拉取
            logger.debug("[OIDC] Claims not present in the ID token, pulling user info")
            userinfo = await client.userinfo(token=token)
            auth_provider = OpenIDProvider(session, userinfo, use_default_groups=True)
            auth = auth_provider.authenticate()
        except MissingClaimException:
            logger.error("[OIDC] Required claims not present in ID token or userinfo endpoint")
            auth = None

    if not auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    access_token, _ = auth
    return MealieAuthToken.respond(access_token)
```

外部身份获取具有 **fallback 两级策略**：
1. **Level 1**：直接使用 id_token 内的 claims（`token["userinfo"]`，由 authlib 自动解析 id_token）。
2. **Level 2**：若 Level 1 缺少必要 claims，则主动调用 IdP 的 `userinfo` endpoint 拉取完整身份。此时 `use_default_groups=True`，允许 groups claim 缺失（对 Keycloak 等不总是返回 groups 的 IdP 做兼容）。

---

## 3. 用户匹配逻辑

用户匹配和授权全部在 [OpenIDProvider.authenticate()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L23-L117) 中完成。

### 3.1 执行流程

```
authenticate()
    │
    ├─ Step 1: claims 存在性 & 非空校验
    │    (required_claims = {OIDC_NAME_CLAIM, email, OIDC_USER_CLAIM} [+ OIDC_GROUPS_CLAIM])
    │
    ├─ Step 2: （可选）组权限校验
    │    ├─ 读取 groups claim
    │    ├─ is_admin = (OIDC_ADMIN_GROUP in groups)
    │    ├─ is_valid_user = (OIDC_USER_GROUP in groups) 或未配置时为 True
    │    └─ 若两者都不满足 → 认证失败 (return None)
    │
    ├─ Step 3: 用户查找
    │    └─ try_get_user(claims[OIDC_USER_CLAIM])
    │         ├─ 先按 username 查找（大小写不敏感）
    │         └─ 再按 email 查找（大小写不敏感）
    │
    ├─ Step 4a: 找到用户 → 同步 admin 状态 → 发 token
    │
    └─ Step 4b: 未找到用户
         ├─ OIDC_SIGNUP_ENABLED=False → return None
         └─ OIDC_SIGNUP_ENABLED=True → 创建新用户 → 发 token
```

### 3.2 Claims 校验 — [openid_provider.py L26-L49](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L26-L49)

```python
claims = self.data
if not claims:
    raise MissingClaimException()

if not self.required_claims.issubset(claims.keys()):
    raise MissingClaimException()

# 校验非空
for claim in self.required_claims:
    if not claims.get(claim):
        raise MissingClaimException()
```

`required_claims` 动态计算（见 [openid_provider.py L119-L126](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L119-L126)）：
- 始终包含：`{OIDC_NAME_CLAIM, "email", OIDC_USER_CLAIM}`（默认即 `{name, email, email}`）
- 若启用了组校验 (`OIDC_REQUIRES_GROUP_CLAIM=True`) 且 **非** `use_default_groups`，附加 `OIDC_GROUPS_CLAIM`。

### 3.3 组（Group）权限判断 — [openid_provider.py L53-L75](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L53-L75)

```python
if settings.OIDC_REQUIRES_GROUP_CLAIM:
    if settings.OIDC_GROUPS_CLAIM not in claims:
        # 兼容 Keycloak 等不总是返回 groups 的 IdP
        self._logger.warning("[OIDC] claims did not include a %s claim ...", ...)

    group_claim = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []
    is_admin = settings.OIDC_ADMIN_GROUP in group_claim if settings.OIDC_ADMIN_GROUP else False
    is_valid_user = settings.OIDC_USER_GROUP in group_claim if settings.OIDC_USER_GROUP else True

    if not (is_valid_user or is_admin):
        return None  # 既不是用户组也不是管理员组 → 拒绝
```

### 3.4 用户查找（try_get_user）— [auth_provider.py L54-L66](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py#L54-L66)

基类实现的用户查找策略是 **两阶段回退**，且带结果缓存：

```python
def try_get_user(self, username: str) -> PrivateUser | None:
    if self.__has_tried_user:
        return self.user

    db = get_repositories(self.session, ...)
    user = db.users.get_one(username, "username", any_case=True)
    if not user:
        user = db.users.get_one(username, "email", any_case=True)

    self.user = user
    return user
```

匹配键是 `claims[OIDC_USER_CLAIM]`（默认 `email`），先按 `username` 字段匹配再按 `email` 匹配。

### 3.5 已有用户：同步管理员状态 — [openid_provider.py L109-L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L109-L114)

```python
if user:
    if settings.OIDC_ADMIN_GROUP and user.admin != is_admin:
        user.admin = is_admin
        repos.users.update(user.id, user)
    return self.get_access_token(user, settings.OIDC_REMEMBER_ME)
```

每次 OIDC 登录都会 **动态同步** `admin` 标志：若 IdP 中的组成员变化，Mealie 侧的用户权限也随之变化。

### 3.6 新用户：自动创建 — [openid_provider.py L77-L107](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L77-L107)

```python
if not user:
    if not settings.OIDC_SIGNUP_ENABLED:
        return None

    username = claims.get(
        "preferred_username", claims.get("username", claims.get(settings.OIDC_USER_CLAIM))
    )
    user = repos.users.create({
        "username": username,
        "password": "OIDC",            # 占位符，OIDC 用户无需本地密码
        "full_name": claims.get(settings.OIDC_NAME_CLAIM),
        "email": claims.get("email"),
        "admin": is_admin,
        "auth_method": AuthMethod.OIDC,  # 标记为 OIDC 用户
    })
    self.session.commit()
    return self.get_access_token(user, settings.OIDC_REMEMBER_ME)
```

新用户创建时的字段优先级：
- `username`: `preferred_username` → `username` → `OIDC_USER_CLAIM`（通常是 email）
- `password`: 固定字符串 `"OIDC"`，仅作占位，真实认证由 IdP 完成
- `auth_method`: 设为 `AuthMethod.OIDC`，后续密码登录会被拒绝

### 3.7 AuthMethod 隔离（单向） — [credentials_provider.py L34-L39](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L34-L39)

`CredentialsProvider`（账号密码登录）会校验 `user.auth_method` 必须是 `AuthMethod.MEALIE`，这防止了 OIDC/LDAP 用户被本地密码方式绕过：

```python
if user.auth_method != AuthMethod.MEALIE:
    self.verify_fake_password()  # 抗时序攻击：仍然跑一次密码校验
    self._logger.warning(
        "Found user but their auth method is not 'Mealie'. Unable to continue with credentials login"
    )
    return None
```

注意这是 **单向** 隔离：密码方式不能接管 OIDC/LDAP 用户，但反向不一定成立（见下一节）。

### 3.8 用户匹配边界：OpenIDProvider 的死代码与跨方式接管

#### 3.8.1 控制流分析 — [openid_provider.py L77-L117](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L77-L117)

```python
user = self.try_get_user(claims.get(settings.OIDC_USER_CLAIM))
if not user:
    # ... 创建新用户 ...
    return self.get_access_token(user, settings.OIDC_REMEMBER_ME)   # 分支 A：return

if user:                                                               # 分支 B
    if settings.OIDC_ADMIN_GROUP and user.admin != is_admin:
        user.admin = is_admin
        repos.users.update(user.id, user)
    return self.get_access_token(user, settings.OIDC_REMEMBER_ME)      # 分支 B：return

# L116-L117 —— 死代码（Unreachable Code）
self._logger.warning("[OIDC] Found user but their AuthMethod does not match OIDC")
return None
```

**关键发现**：L116-L117 是 **永远不会执行的死代码**。
- `if not user:` 分支 A 一定会 `return`
- `if user:` 分支 B 也一定会 `return`
- 两个互斥且穷尽的分支都 return，后续代码无法到达

这意味着 `OpenIDProvider` **完全不检查 `user.auth_method`**。只要 `try_get_user` 能通过 username/email 匹配到数据库中的任意一条 User 记录（无论其 auth_method 是 `MEALIE`、`LDAP` 还是 `OIDC`），OIDC 登录都会直接成功，并且可能修改该用户的 `admin` 标志。

#### 3.8.2 测试侧证 — [test_openid_provider.py L122-L150](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/tests/unit_tests/core/security/providers/test_openid_provider.py#L122-L150)

```python
# test_has_user_group_existing_user
def test_has_user_group_existing_user(monkeypatch: MonkeyPatch, unique_user: TestUser):
    # unique_user 的 auth_method = AuthMethod.MEALIE（见 fixture_schemas.py L19）
    data = {
        "preferred_username": "dude1",
        "email": unique_user.email,      # 使用 MEALIE 用户的 email 作为 claim
        "name": "Firstname Lastname",
        "groups": ["mealie_user"],
    }
    auth_provider = OpenIDProvider(unique_user.repos.session, data)
    assert auth_provider.authenticate() is not None   # ✅ MEALIE 用户被 OIDC 成功接管
```

测试中 `unique_user` fixture（[fixture_schemas.py L19](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/tests/utils/fixture_schemas.py#L19)）默认 `auth_method = AuthMethod.MEALIE`，测试断言 OIDC 方式可以登录该 MEALIE 用户，直接印证了 **OpenIDProvider 不校验 auth_method**。

此外，L116 的警告日志 `"Found user but their AuthMethod does not match OIDC"` 在任何测试中都未被断言覆盖，也侧面说明该分支从未执行。

#### 3.8.3 try_get_user 的跨方式匹配 — [auth_provider.py L54-L66](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py#L54-L66)

```python
def try_get_user(self, username: str) -> PrivateUser | None:
    if self.__has_tried_user:
        return self.user
    db = get_repositories(self.session, group_id=None, household_id=None)
    user = db.users.get_one(username, "username", any_case=True)   # 阶段 1：username 模糊匹配
    if not user:
        user = db.users.get_one(username, "email", any_case=True)  # 阶段 2：email 模糊匹配
    self.user = user
    return user
```

查找逻辑中 **完全没有 auth_method 过滤条件**。匹配键来自 `claims[OIDC_USER_CLAIM]`（默认 `email`），按以下顺序尝试：
1. 按 `users.username` 大小写不敏感匹配
2. 失败则按 `users.email` 大小写不敏感匹配

只要某条记录的 username 或 email 与 IdP 返回的 claim 相等，就会被命中——**不管这条用户原本以什么方式注册**。

### 3.9 三种 Provider 的登录方式隔离矩阵

综合 `OpenIDProvider`、`CredentialsProvider`、`LDAPProvider` 三者的 auth_method 检查逻辑，整理隔离矩阵如下：

| 用户 DB 中 auth_method | OIDC 登录 | 密码登录 (Credentials) | LDAP 登录 (LDAP 启用) |
|------------------------|-----------|------------------------|-----------------------|
| **MEALIE**             | ✅ 登录成功；若配置了 `OIDC_ADMIN_GROUP` 还会被同步 admin 权限 | ✅ 校验本地密码 | ✅ 回退到 CredentialsProvider，校验密码成功 |
| **LDAP**               | ✅ 登录成功；admin 可能被同步 | ❌ 被 CredentialsProvider 拒绝（auth_method != MEALIE），执行假密码哈希抗时序攻击 | ✅ 走 LDAP BIND，成功则登录（必要时创建/更新用户） |
| **OIDC**               | ✅ 登录成功；admin 可能被同步 | ❌ 被 CredentialsProvider 拒绝（auth_method != MEALIE） | ❌ LDAPProvider 分支 `not user or auth_method==LDAP` 不命中 → 回退 CredentialsProvider → 被拒绝 |

#### 3.9.1 LDAPProvider 的混合策略 — [ldap_provider.py L25-L37](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L25-L37)

```python
def authenticate(self) -> tuple[str, timedelta] | None:
    user = self.try_get_user(self.data.username)
    if not user or user.auth_method == AuthMethod.LDAP:
        # 用户不存在或已标记为 LDAP → 走 LDAP BIND + 自动建号
        user = self.get_user()
        if user:
            return self.get_access_token(user, self.data.remember_me)

    return super().authenticate()  # 否则回退到 CredentialsProvider（只接受 MEALIE 用户）
```

LDAP 登录的行为：
- 找不到用户 **或** 用户已被标记为 `AuthMethod.LDAP` → 向 LDAP 服务器 BIND，BIND 成功则登录（必要时在 Mealie 侧创建用户）
- 找到了用户且 `auth_method != LDAP`（即 MEALIE 或 OIDC）→ 回退到 `CredentialsProvider.authenticate()`
  - 对 MEALIE 用户：密码校验通过则登录 ✅
  - 对 OIDC 用户：`auth_method != MEALIE` 被拒绝 ❌

### 3.10 典型边界场景与实际行为

| 场景 | 实际行为 | 涉及代码 |
|------|---------|----------|
| **已有 MEALIE 用户，用相同 email 通过 OIDC 登录** | 直接登录成功；若 IdP 中该用户在 admin group，本地用户的 `admin` 会被提升 | [openid_provider.py L109-L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L109-L114) |
| **已有 LDAP 用户，用相同 email 通过 OIDC 登录** | 直接登录成功；admin 可能被同步修改 | [openid_provider.py L77-L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L77-L114) |
| **已有 OIDC 用户，尝试用密码登录** | 被 CredentialsProvider 拒绝（auth_method != MEALIE），执行假哈希抗时序 | [credentials_provider.py L34-L39](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L34-L39) |
| **已有 LDAP 用户，尝试用密码登录（LDAP 未启用）** | 同上，被拒绝 | 同上 |
| **已有 LDAP 用户，尝试用密码登录（LDAP 启用）** | 先走 LDAP 分支（auth_method==LDAP），向 LDAP BIND；BIND 成功则登录 | [ldap_provider.py L25-L35](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L25-L35) |
| **已有 MEALIE 用户，LDAP 启用，用 LDAP 凭证登录** | LDAP BIND 成功 → 不会覆盖原 MEALIE 用户（因为 auth_method != LDAP，回退到 CredentialsProvider，走本地密码校验） | [ldap_provider.py L32-L37](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L32-L37) |
| **IdP 的 groups claim 缺失（如 Keycloak）** | Level 1 id_token claims 校验抛 `MissingClaimException` → 回退 Level 2 userinfo endpoint，`use_default_groups=True`（允许 groups claim 缺失），再做校验 | [auth.py L126-L138](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L126-L138) |
| **OIDC_SIGNUP_ENABLED=False，IdP 返回一个从未见过的 email** | try_get_user 返回 None → `OIDC_SIGNUP_ENABLED=False` → 认证失败 return None | [openid_provider.py L78-L81](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L78-L81) |
| **OIDC_SIGNUP_ENABLED=True，但 DEFAULT_GROUP/DEFAULT_HOUSEHOLD 配置错误** | `repos.users.create()` 内部 User 构造抛 `ValueError` → 被 L103 `except Exception` 捕获 → return None，用户不会被创建 | [openid_provider.py L103-L105](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L103-L105) + [users.py L161-L167](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/db/models/users/users.py#L161-L167) |

### 3.11 设计风险提示

1. **OIDC 可接管任意 auth_method 用户**：`OpenIDProvider` 缺少 `user.auth_method == AuthMethod.OIDC` 的检查（死代码问题），意味着只要 IdP 返回的 email/username 命中数据库中任何现有账号（MEALIE/LDAP/OIDC），OIDC 登录都会成功。若管理员启用了 OIDC 但对 IdP 组配置不谨慎，可能出现外部身份提升本地 MEALIE 用户为管理员的情况。

2. **auth_method 迁移单向不可逆**：
   - OIDC 方式可以登录 MEALIE/LDAP 用户，但不会自动把 `auth_method` 改成 `OIDC`
   - 这些用户仍然保留原来的 `auth_method`，下次用原方式（密码/LDAP）能否登录取决于原 Provider 的校验规则

3. **try_get_user 两阶段匹配的冲突风险**：由于先按 username 再按 email 模糊匹配，若一个 IdP 用户的 claim 值恰好等于另一个 Mealie 用户的 username（而非 email），可能出现错配登录。

---

## 4. 会话建立流程

### 4.1 JWT Access Token 生成 — [auth_provider.py L29-L52](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py#L29-L52)

```python
def get_access_token(self, user: PrivateUser, remember_me=False) -> tuple[str, timedelta]:
    duration = timedelta(hours=settings.TOKEN_TIME)
    if remember_me:
        duration = max(remember_me_duration, duration)  # remember_me_duration = 14 天
    return AuthProvider.create_access_token({"sub": str(user.id)}, duration)

@staticmethod
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> tuple[str, timedelta]:
    to_encode = data.copy()
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    to_encode["iss"] = "mealie"
    return (jwt.encode(to_encode, settings.SECRET, algorithm="HS256"), expires_delta)
```

JWT 载荷：
- `sub`: 用户 ID（UUID）
- `exp`: 过期时间
- `iss`: `"mealie"`

**注意**：OIDC 的 remember_me 由 `OIDC_REMEMBER_ME` 配置项全局控制，而密码登录由前端表单的 `remember_me` 字段控制。

### 4.2 前端 Token 存储 — [use-auth-backend.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-auth-backend.ts#L27-L40)

```typescript
const tokenName = runtimeConfig.public.AUTH_TOKEN;
const tokenCookie = useCookie(tokenName, {
  maxAge: $appInfo.tokenTime * 60 * 60,
  secure: $appInfo.production && window?.location?.protocol === "https:",
});

function setToken(token: string | null) {
  tokenCookie.value = token;
}
```

使用 **HttpOnly=false** 的 Nuxt Cookie 存储（前端可访问），生产环境下 HTTPS 时启用 `secure` 标志。

### 4.3 前端会话建立 — [use-mealie-auth.ts oauthSignIn()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-mealie-auth.ts#L40-L45)

```typescript
async function oauthSignIn() {
  const params = new URLSearchParams(window.location.search);
  const { data: token } = await $axios.get<{ access_token: string; token_type: "bearer" }>(
    "/api/auth/oauth/callback", { params }
  );
  auth.setToken(token.access_token);  // 写入 cookie
  await auth.getSession();            // GET /api/users/self 拉取用户信息
}
```

`getSession()` ([use-auth-backend.ts L54-L72](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-auth-backend.ts#L54-L72)) 会：
1. 读 cookie，若无则标记 `unauthenticated`
2. 调用 `GET /api/users/self` 获取用户详情
3. 成功则标记 `authenticated`，失败则清除 token

### 4.4 插件启动时恢复会话 — [init-auth.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/plugins/init-auth.client.ts)

```typescript
export default defineNuxtPlugin({
  async setup() {
    const auth = useAuthBackend();
    await auth.getSession();  // 页面加载时从 cookie 恢复用户信息
  },
});
```

### 4.5 后端请求鉴权 — [dependencies.py get_current_user()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/dependencies/dependencies.py#L88-L123)

```python
async def get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme_soft_fail),
    session=Depends(generate_session),
) -> PrivateUser:
    # 双来源：Authorization header 优先，其次 cookie
    if token is None and "mealie.access_token" in request.cookies:
        token = request.cookies.get("mealie.access_token", "")

    payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
    user_id: str | None = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    repos = get_repositories(session, ...)
    user = repos.users.get_one(token_data.user_id, "id", any_case=False)
    session.commit()
    return user
```

鉴权支持两种 token 传输方式：
1. `Authorization: Bearer <token>` （由 `OAuth2PasswordBearer` 提取）
2. Cookie：`mealie.access_token`

### 4.6 Token 刷新 — [auth.py refresh_token()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L147-L151)

```python
@user_router.get("/refresh")
async def refresh_token(current_user: PrivateUser = Depends(get_current_user)):
    access_token = security.create_access_token(data={"sub": str(current_user.id)})
    return MealieAuthToken.respond(access_token)
```

刷新逻辑要求用户已经处于登录状态（`get_current_user` 依赖），即用旧 token 换新 token。

### 4.7 登出 — [auth.py logout()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L154-L162) + [use-auth-backend.ts signOut()](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-auth-backend.ts#L94-L115)

后端删除 cookie，前端额外清除本地存储、所有 store 缓存、useAsyncData 缓存，最后跳转登录页。

---

## 5. 配置项汇总

| 配置项 | 默认值 | 作用 |
|--------|--------|------|
| `OIDC_AUTH_ENABLED` | `False` | 总开关 |
| `OIDC_CLIENT_ID` | `None` | OAuth Client ID |
| `OIDC_CLIENT_SECRET` | `None` | OAuth Client Secret |
| `OIDC_CONFIGURATION_URL` | `None` | `.well-known/openid-configuration` URL |
| `OIDC_SIGNUP_ENABLED` | `True` | 未匹配到用户时是否自动创建账号 |
| `OIDC_USER_GROUP` | `None` | 允许登录的用户组（IdP groups claim 中需包含） |
| `OIDC_ADMIN_GROUP` | `None` | 管理员组（匹配则设为 admin） |
| `OIDC_AUTO_REDIRECT` | `False` | 前端登录页是否自动跳转 IdP |
| `OIDC_PROVIDER_NAME` | `"OAuth"` | 按钮显示名 |
| `OIDC_REMEMBER_ME` | `False` | OIDC 登录的 token 是否走 remember_me 长有效期 |
| `OIDC_USER_CLAIM` | `"email"` | 匹配本地用户的 claim 字段 |
| `OIDC_NAME_CLAIM` | `"name"` | 用户显示名字段 |
| `OIDC_GROUPS_CLAIM` | `"groups"` | 组列表字段 |
| `OIDC_SCOPES_OVERRIDE` | `None` | 覆盖默认 scope |
| `OIDC_TLS_CACERTFILE` | `None` | IdP TLS CA 证书路径 |
| `OIDC_CLIENT_TIMEOUT` | `"default"` | HTTP 超时 |

派生属性：
- `OIDC_REQUIRES_GROUP_CLAIM = OIDC_USER_GROUP is not None or OIDC_ADMIN_GROUP is not None`
- `OIDC_READY`：所有必填项非空且 group claim 配置合法

---

## 6. 关键文件清单

| 角色 | 文件 |
|------|------|
| OIDC 业务逻辑 | [openid_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py) |
| Provider 基类 & JWT 生成 | [auth_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py) |
| 认证路由 & OAuth 端点 | [auth.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py) |
| OAuth 缓存 | [auth_cache.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth_cache.py) |
| 用户模型 | [users.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/db/models/users/users.py) |
| 配置 | [settings.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/settings/settings.py) |
| 请求鉴权依赖 | [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/dependencies/dependencies.py) |
| 前端登录页 | [login.vue](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/pages/login.vue) |
| 前端认证状态管理 | [use-auth-backend.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-auth-backend.ts) |
| 前端 OIDC 封装 | [use-mealie-auth.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/composables/use-mealie-auth.ts) |
| 前端启动插件 | [init-auth.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/frontend/app/plugins/init-auth.client.ts) |
| 单元测试 | [test_openid_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/tests/unit_tests/core/security/providers/test_openid_provider.py) |
