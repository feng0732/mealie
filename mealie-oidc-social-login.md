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
1. **Level 1**：直接使用 id_token 内的 claims（`token["userinfo"]`，由 authlib 自动解析 id_token）。`use_default_groups=False`（默认值）。
2. **Level 2**：若 Level 1 缺少必要 claims（抛出 `MissingClaimException`），则主动调用 IdP 的 `userinfo` endpoint 拉取完整身份。此时显式传入 `use_default_groups=True`。

### 2.5.1 `use_default_groups` 对 `required_claims` 的真实影响 — [openid_provider.py L119-L126](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L119-L126)

```python
@property
def required_claims(self):
    settings = get_app_settings()
    claims = {settings.OIDC_NAME_CLAIM, "email", settings.OIDC_USER_CLAIM}
    if settings.OIDC_REQUIRES_GROUP_CLAIM and not self.use_default_groups:
        claims.add(settings.OIDC_GROUPS_CLAIM)
    return claims
```

两级 fallback 中 `required_claims` 的差异：

| 条件 | Level 1 (`use_default_groups=False`) | Level 2 (`use_default_groups=True`) |
|------|-------------------------------------|-------------------------------------|
| `OIDC_REQUIRES_GROUP_CLAIM=False` | `{name, email, OIDC_USER_CLAIM}` — **不包含** groups claim | `{name, email, OIDC_USER_CLAIM}` — **不包含** groups claim |
| `OIDC_REQUIRES_GROUP_CLAIM=True` | `{name, email, OIDC_USER_CLAIM, OIDC_GROUPS_CLAIM}` — **包含** groups claim | `{name, email, OIDC_USER_CLAIM}` — **不包含** groups claim |

结论：`use_default_groups=True` 的作用是 **在启用了组校验的前提下，放宽对 groups claim 存在性的强制要求**。Level 1 要求 groups claim 必须存在于 claims 中，否则直接抛 `MissingClaimException`；Level 2 不再强制要求 groups claim 必须存在，允许其缺失。

### 2.5.2 `use_default_groups` 对组校验逻辑的真实影响 — [openid_provider.py L53-L75](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L53-L75)

```python
if settings.OIDC_REQUIRES_GROUP_CLAIM:                           # ← 进入条件：与 use_default_groups 无关
    if settings.OIDC_GROUPS_CLAIM not in claims:
        self._logger.warning(
            "[OIDC] claims did not include a %s claim%s",
            settings.OIDC_GROUPS_CLAIM,
            ", using an empty list as default" if self.use_default_groups else "",
        )
    group_claim = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []   # ← 无论如何都用 [] 兜底
    is_admin = settings.OIDC_ADMIN_GROUP in group_claim if settings.OIDC_ADMIN_GROUP else False
    is_valid_user = settings.OIDC_USER_GROUP in group_claim if settings.OIDC_USER_GROUP else True

    if not (is_valid_user or is_admin):
        return None                                                # ← 组判定失败
```

关键发现：
1. **组校验分支的进入条件完全由 `OIDC_REQUIRES_GROUP_CLAIM` 决定**，与 `use_default_groups` 无关。只要启用了组校验，后续逻辑就一定会执行。
2. `use_default_groups` 对组校验逻辑本身的影响仅体现在两处：
   - 日志消息：`use_default_groups=True` 时附加 `", using an empty list as default"` 提示
   - （间接）由于 Level 2 的 `required_claims` 不包含 groups claim，claims 中没有 groups 不会在前置校验阶段被拦截，而是能进入到组校验分支
3. **组匹配的判定逻辑完全相同**，无论 `use_default_groups` 为何值：
   - `group_claim = claims.get(..., []) or []` —— groups 缺失或为假值时统一按空列表处理
   - `is_admin`、`is_valid_user` 的计算方式相同
   - 最终 `if not (is_valid_user or is_admin): return None` 的判定相同

### 2.5.3 两级 fallback 的实际组合行为

| 场景 | Level 1 结果 | Level 2 结果 | 最终认证结果 |
|------|-------------|-------------|------------|
| `OIDC_REQUIRES_GROUP_CLAIM=False`（两个组都未配置）；id_token 有 name/email/user_claim | 通过 → 发 token | 不执行 | ✅ 成功 |
| `OIDC_REQUIRES_GROUP_CLAIM=True`；id_token 有 name/email/user_claim **且有 groups claim**；groups 满足 USER_GROUP 或 ADMIN_GROUP | 通过 → 发 token | 不执行 | ✅ 成功 |
| `OIDC_REQUIRES_GROUP_CLAIM=True`；id_token **缺少 groups claim**（详见 2.5.4 节的各种组配置组合） | `MissingClaimException` | 进入 Level 2：`use_default_groups=True` 使 groups 缺失不报错；`group_claim=[]`；最终结果取决于 **哪个组被配置** | ⚠️ 见 2.5.4 节组合表 |
| `OIDC_REQUIRES_GROUP_CLAIM=True`；id_token 缺少 name/email/user_claim | `MissingClaimException` | 进入 Level 2：若 userinfo 也缺这些基础字段 → 再抛 `MissingClaimException` → auth=None | ❌ 401 |
| `OIDC_REQUIRES_GROUP_CLAIM=True`；id_token 有 groups 但 groups 列表中既不含 USER_GROUP 也不含 ADMIN_GROUP | return None（组判定失败） | 不执行（MissingClaimException 才会 fallback） | ❌ 401 |

**重要**：fallback 只由 `MissingClaimException` 触发。如果 Level 1 的 claims 完整存在但组判定失败（return None），**不会**降级到 Level 2，直接返回 401。

### 2.5.4 admin-only 分支与组配置组合深度分析

组校验核心代码位于 [openid_provider.py L64-L68](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L64-L68)：

```python
group_claim = claims.get(settings.OIDC_GROUPS_CLAIM, []) or []
is_admin = settings.OIDC_ADMIN_GROUP in group_claim if settings.OIDC_ADMIN_GROUP else False
is_valid_user = settings.OIDC_USER_GROUP in group_claim if settings.OIDC_USER_GROUP else True

if not (is_valid_user or is_admin):
    return None
```

两个关键三元表达式的语义：

| 变量 | 计算规则 | 含义 |
|------|---------|------|
| `is_admin` | `OIDC_ADMIN_GROUP` 已配置 → `OIDC_ADMIN_GROUP in group_claim`；未配置 → **固定 `False`** | 用户是否在管理员组；未配置管理员组时所有人都不是 admin |
| `is_valid_user` | `OIDC_USER_GROUP` 已配置 → `OIDC_USER_GROUP in group_claim`；未配置 → **固定 `True`** | 用户是否在普通用户组；未配置普通用户组时所有人默认是有效用户 |

最终判定：`not (is_valid_user or is_admin)` → 只有当两者都为 `False` 时才拒绝登录。

`OIDC_REQUIRES_GROUP_CLAIM` 的派生规则在 [settings.py L384-L385](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/settings/settings.py#L384-L385)：
```python
@property
def OIDC_REQUIRES_GROUP_CLAIM(self) -> bool:
    return self.OIDC_USER_GROUP is not None or self.OIDC_ADMIN_GROUP is not None
```
即只要配置了其中任何一个组，组校验分支就会被启用。

#### 组配置 × groups claim 缺失的完整组合矩阵

以下场景假设 `OIDC_REQUIRES_GROUP_CLAIM=True` 且 claims 中缺少 groups 字段（如 Keycloak 在某些场景下不返回 groups），此时 `group_claim = []`（空列表），分析 Level 2 fallback 后的实际行为：

| `OIDC_USER_GROUP` | `OIDC_ADMIN_GROUP` | `is_valid_user` 计算 | `is_admin` 计算 | `is_valid_user or is_admin` | 最终结果 |
|---|---|---|---|---|---|
| **未配置** (None) | **未配置** (None) | `True`（USER_GROUP 未配置） | `False`（ADMIN_GROUP 未配置） | `True` | ✅ 认证通过；`admin=False`（注意：两组都未配置时 `OIDC_REQUIRES_GROUP_CLAIM=False`，组校验分支根本不会进入，此处仅为逻辑完整性） |
| **未配置** (None) | **已配置** `"admin_group"`（admin-only 场景） | `True`（USER_GROUP 未配置） | `"admin_group" in []` → `False` | `True` | ✅ **认证通过**；`admin=False`。由于 `is_valid_user=True` 兜底，即使 groups 为空也不会被拒绝，用户以普通用户身份登录 |
| **已配置** `"user_group"` | **未配置** (None)（user-only 场景） | `"user_group" in []` → `False` | `False`（ADMIN_GROUP 未配置） | `False` | ❌ 认证被拒绝（return None）。`is_valid_user` 和 `is_admin` 均为 False |
| **已配置** `"user_group"` | **已配置** `"admin_group"`（双组都配） | `"user_group" in []` → `False` | `"admin_group" in []` → `False` | `False` | ❌ 认证被拒绝（return None）。空 groups 列表中两个组都匹配不到 |

#### admin-only + groups 缺失的完整控制流（重点场景）

前提：`OIDC_USER_GROUP=None`、`OIDC_ADMIN_GROUP="admin_group"`、claims 中没有 groups 字段。

**Level 1（`use_default_groups=False`）**：
1. `OIDC_REQUIRES_GROUP_CLAIM = True`（ADMIN_GROUP 已配置）
2. `required_claims` 计算：`OIDC_REQUIRES_GROUP_CLAIM=True and not False=True` → **groups claim 被加入 required_claims**
3. claims 中缺少 groups → `required_claims.issubset(claims.keys())` 为 False → 抛 `MissingClaimException`
4. 进入 Level 2 fallback

**Level 2（`use_default_groups=True`）**：
1. `required_claims` 计算：`OIDC_REQUIRES_GROUP_CLAIM=True and not True=False` → **groups claim 不被加入 required_claims**
2. claims 中 name/email/user_claim 存在 → required_claims 校验通过
3. 进入组校验分支（`OIDC_REQUIRES_GROUP_CLAIM=True` 仍成立）
4. `OIDC_GROUPS_CLAIM not in claims` → 打 warning：`"claims did not include a groups claim, using an empty list as default"`
5. `group_claim = claims.get("groups", []) or [] = []`
6. `is_admin = "admin_group" in [] = False`（ADMIN_GROUP 已配置，空列表中匹配不到）
7. `is_valid_user = ... if None else True = True`（USER_GROUP 未配置，**默认为 True**）
8. `if not (True or False)` → `if not True` → 条件为 False，**不进入 return None 分支**
9. 继续执行用户查找/创建逻辑，以 `admin=False` 完成登录

**结论**：仅配置 `OIDC_ADMIN_GROUP`、不配置 `OIDC_USER_GROUP` 时，即使 IdP 完全不返回 groups claim（如 Keycloak），Level 2 fallback 后认证仍能通过，用户以普通身份（`admin=False`）登录。这是因为 `is_valid_user` 在 `OIDC_USER_GROUP` 未配置时默认 `True`，对空 groups 列表兜底放行。

#### user-only + groups 缺失的对比

若只配置 `OIDC_USER_GROUP` 而未配置 `OIDC_ADMIN_GROUP`，则：
- `is_valid_user = "user_group" in [] = False`
- `is_admin = False`（ADMIN_GROUP 未配置）
- `not (False or False) = True` → 认证被拒绝

这种场景下 groups 缺失会导致所有用户无法登录，必须确保 IdP 稳定返回 groups claim。

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

### 3.9 Provider 分发路由（入口级选择）

不同登录方式从 HTTP 路由层就走不同的代码路径，不是在同一个 Provider 内部按 auth_method 分发：

| 登录方式 | 入口路由 | Provider 选择逻辑 | 涉及代码 |
|---------|---------|------------------|---------|
| 密码登录（账号密码表单） | `POST /api/auth/token` | `get_auth_provider()`：`LDAP_ENABLED=True` 时返回 `LDAPProvider`，否则返回 `CredentialsProvider` | [security.py L21-L28](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/security.py#L21-L28) + [auth.py L64-L88](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L64-L88) |
| OIDC 登录 | `GET /api/auth/oauth/callback` | **不经过** `get_auth_provider()`，直接 `OpenIDProvider(session, token["userinfo"])` | [auth.py L115-L144](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L115-L144) |

**关键点**：LDAP 启用后，**所有**密码形式的登录请求（`/api/auth/token`）都会被路由到 `LDAPProvider.authenticate()`，再由其内部决定是否真正发起 LDAP BIND。

```python
# security.py L21-L28
def get_auth_provider(session: Session, data: CredentialsRequestForm) -> AuthProvider:
    credentials_request = CredentialsRequest(**data.__dict__)
    if settings.LDAP_ENABLED:
        return LDAPProvider(session, credentials_request)   # LDAP 启用时，所有密码登录都走这里
    return CredentialsProvider(session, credentials_request)
```

### 3.10 三种 Provider 的登录方式隔离矩阵（修正版）

综合路由分发 + 各 Provider 内部 auth_method 检查，完整隔离矩阵如下：

| 用户 DB 中 auth_method | OIDC 登录 | 密码登录（LDAP 未启用） | 密码登录（LDAP 已启用） |
|------------------------|-----------|------------------------|------------------------|
| **MEALIE**             | ✅ 登录成功；若配置 `OIDC_ADMIN_GROUP` 还会同步 admin 权限 | ✅ `CredentialsProvider` 校验本地密码 | ✅ **LDAP BIND 完全不执行**；因 `auth_method != LDAP`，直接回退到 `CredentialsProvider` 校验本地密码（输入的密码按本地哈希校验，LDAP 服务器凭证无关） |
| **LDAP**               | ✅ 登录成功；admin 可能被同步 | ❌ `CredentialsProvider` 拒绝（auth_method != MEALIE），执行假哈希抗时序 | ✅ 因 `auth_method == LDAP`，走 `get_user()` 发起 LDAP BIND；BIND 成功则登录（可能同步 admin） |
| **OIDC**               | ✅ 登录成功；admin 可能被同步 | ❌ `CredentialsProvider` 拒绝（auth_method != MEALIE） | ❌ 因 `auth_method != LDAP`，回退到 `CredentialsProvider` → 再被拒绝（auth_method != MEALIE），执行假哈希抗时序 |

### 3.10.1 LDAPProvider.authenticate 控制流深度分析 — [ldap_provider.py L25-L37](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L25-L37)

```python
def authenticate(self) -> tuple[str, timedelta] | None:
    user = self.try_get_user(self.data.username)
    if not user or user.auth_method == AuthMethod.LDAP:    # ← 关键条件
        # 分支 A：本地不存在 或 已标记为 LDAP → 走 LDAP BIND
        user = self.get_user()
        if user:
            return self.get_access_token(user, self.data.remember_me)
    # 分支 B：已存在且 auth_method != LDAP → 回退本地密码
    return super().authenticate()
```

按 `try_get_user` 结果和用户 auth_method 展开三种情况：

#### 情况 1：本地完全没有该用户（`not user == True`）
- 条件满足 → 进入分支 A → 调用 `get_user()`：
  1. 服务账号 `LDAP_QUERY_BIND` 连接 LDAP
  2. 按 `LDAP_ID_ATTRIBUTE` / `LDAP_MAIL_ATTRIBUTE` 搜索用户 DN
  3. 用搜索到的 DN + 用户输入密码执行 `simple_bind_s`（真正的用户认证）
  4. BIND 成功 → `try_get_user` 再查一次（还是 None）→ 创建本地新用户 `auth_method=AuthMethod.LDAP`
  5. 若配置了 `LDAP_ADMIN_FILTER`，同步 admin 标志
  6. 返回 token

#### 情况 2：本地有该用户且 `auth_method == AuthMethod.LDAP`
- 条件满足 → 进入分支 A → 调用 `get_user()`：
  1. 同情况 1，完整执行 LDAP query bind + 用户 DN BIND（**每次登录都重新校验 LDAP 凭证**，不信任本地）
  2. BIND 成功 → `try_get_user` 再查一次（能找到）→ **不创建新用户，不修改 auth_method**
  3. 仅根据 `LDAP_ADMIN_FILTER` 同步 admin 标志
  4. 返回 token

#### 情况 3：本地有该用户且 `auth_method != AuthMethod.LDAP`（即 MEALIE 或 OIDC）
- 条件 **不满足** → **完全跳过 LDAP BIND**，不建立 LDAP 连接 → 进入分支 B
- `return super().authenticate()` 即 `CredentialsProvider.authenticate()`
  - 对 **MEALIE 用户**：`auth_method == AuthMethod.MEALIE` 校验通过 → 按本地密码哈希校验（用户输入的密码直接与本地 bcrypt 比对，**LDAP 服务器凭证完全不参与**）
  - 对 **OIDC 用户**：`auth_method != AuthMethod.MEALIE` → 被拒绝，执行 `verify_fake_password()` 假哈希抗时序攻击

### 3.10.2 LDAPProvider.get_user() 对已有用户的处理 — [ldap_provider.py L96-L195](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L96-L195)

```python
def get_user(self) -> PrivateUser | None:
    # ... LDAP query bind、搜索用户、用户 DN BIND（已成功）...

    user = self.try_get_user(data.username)   # L146：二次查询本地

    if user is None:
        # L148-L176：仅当本地完全没有该用户时创建新账号
        user = db.users.create({
            "auth_method": AuthMethod.LDAP,   # 新建用户 auth_method 固定为 LDAP
            # ...
        })

    # L178-L192：无论新老用户，仅可能同步 admin 标志，绝不修改 auth_method
    if settings.LDAP_ADMIN_FILTER:
        should_be_admin = len(conn.search_s(user_dn, ...)) > 0
        if user.admin != should_be_admin:
            user.admin = should_be_admin
            db.users.update(user.id, user)

    return user
```

关键结论：
- **`get_user()` 不会修改已有用户的 `auth_method`**。即使本地已有一个 auth_method=MEALIE 的用户由于某种原因（例如 if 条件被改写）进入了 `get_user()`，也只会同步 admin，不会将其改成 LDAP。
- 只有本地完全不存在用户时才会创建 `auth_method=LDAP` 的新用户。

测试侧证：[test_security.py L314-L346](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/tests/unit_tests/test_security.py#L314-L346) `test_user_login_ldap_auth_method` 直接调用 `provider.get_user()` 对一个已存在的 `auth_method=LDAP` 用户操作，断言返回结果 `auth_method == AuthMethod.LDAP`。

### 3.11 典型边界场景与实际行为（修正版）

| 场景 | 实际行为 | 涉及代码 |
|------|---------|----------|
| **已有 MEALIE 用户，用相同 email 通过 OIDC 登录** | 直接登录成功；若 IdP 中该用户在 admin group，本地 `admin` 会被提升（auth_method 仍保持 MEALIE） | [openid_provider.py L109-L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L109-L114) |
| **已有 LDAP 用户，用相同 email 通过 OIDC 登录** | 直接登录成功；admin 可能被同步（auth_method 仍保持 LDAP） | [openid_provider.py L77-L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L77-L114) |
| **已有 OIDC 用户，尝试用密码登录（LDAP 未启用）** | `CredentialsProvider` 检测 `auth_method != MEALIE` → 拒绝，执行假哈希抗时序 | [credentials_provider.py L34-L39](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L34-L39) |
| **已有 LDAP 用户，尝试用密码登录（LDAP 未启用）** | 同上，被拒绝 | 同上 |
| **已有 LDAP 用户，尝试用密码登录（LDAP 启用）** | 路由到 `LDAPProvider` → `auth_method == LDAP` → 进入 `get_user()` 发起 LDAP BIND；BIND 成功则登录（同步 admin） | [ldap_provider.py L32-L35](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L32-L35) + [ldap_provider.py L96-L195](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L96-L195) |
| **已有 MEALIE 用户，LDAP 启用，输入 LDAP 服务器的密码登录** | 路由到 `LDAPProvider` → `user` 存在且 `auth_method != LDAP` → **不执行 LDAP BIND** → 回退 `CredentialsProvider` → 按本地 MEALIE 密码哈希校验。若输入的是 LDAP 密码（与本地不同）则校验失败 401；若恰好两个系统密码一致则登录成功。**LDAP 服务器全程不参与认证。** | [ldap_provider.py L32-L37](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L32-L37) + [credentials_provider.py L44-L56](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L44-L56) |
| **已有 MEALIE 用户，LDAP 启用，输入本地 Mealie 密码登录** | 同上路径，本地密码哈希校验成功 → 正常登录 | 同上 |
| **已有 OIDC 用户，LDAP 启用，尝试用密码登录** | 路由到 `LDAPProvider` → `auth_method != LDAP` → 回退 `CredentialsProvider` → `auth_method != MEALIE` → 拒绝，假哈希抗时序 | [ldap_provider.py L37](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L37) + [credentials_provider.py L34-L39](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L34-L39) |
| **本地无用户，LDAP 启用，用合法 LDAP 凭证登录** | `not user` → 进入 `get_user()` → LDAP BIND 成功 → 自动创建 `auth_method=LDAP` 的本地用户 → 登录成功 | [ldap_provider.py L32-L35](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L32-L35) + [ldap_provider.py L148-L176](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L148-L176) |
| **IdP 的 groups claim 缺失（如 Keycloak）** | Level 1 id_token claims 校验抛 `MissingClaimException` → 回退 Level 2 userinfo endpoint，`use_default_groups=True`（允许 groups claim 缺失），再做校验 | [auth.py L126-L138](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L126-L138) |
| **OIDC_SIGNUP_ENABLED=False，IdP 返回一个从未见过的 email** | try_get_user 返回 None → `OIDC_SIGNUP_ENABLED=False` → 认证失败 return None | [openid_provider.py L78-L81](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L78-L81) |
| **OIDC_SIGNUP_ENABLED=True / LDAP 启用，但 DEFAULT_GROUP/DEFAULT_HOUSEHOLD 配置错误** | `repos.users.create()` 内部 User 构造抛 `ValueError`（找不到 group/household）→ OIDC 侧被 `except Exception` 捕获 → return None；LDAP 侧未捕获异常会冒泡 | [openid_provider.py L103-L105](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L103-L105) + [users.py L161-L167](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/db/models/users/users.py#L161-L167) |

### 3.12 设计风险提示（修正版）

1. **OIDC 可接管任意 auth_method 用户**：`OpenIDProvider` 中 L116-L117 的 `auth_method` 检查是死代码（`if not user` 和 `if user` 两分支穷尽 return），只要 IdP 返回的 email/username 命中数据库中任何现有账号（MEALIE/LDAP/OIDC），OIDC 登录都会成功并可能同步 admin。若 IdP 组配置不谨慎，外部身份可提升本地 MEALIE/LDAP 用户为管理员。

2. **LDAP 启用不影响本地 MEALIE 用户的密码校验路径**：LDAP 启用后，MEALIE 用户的密码登录不经过 LDAP 服务器，仍然走本地 bcrypt 校验。若企业期望 LDAP 启用后所有账号都通过 LDAP 认证，需要手动将存量 MEALIE 用户的 `auth_method` 迁移为 LDAP（数据库层面修改），否则这些用户的密码不受 LDAP 策略管控。

3. **auth_method 迁移单向不可逆**：
   - OIDC 方式登录 MEALIE/LDAP 用户成功，但不会自动把 `auth_method` 改成 `OIDC`
   - LDAP 方式 `get_user()` 对已有用户只同步 admin，不修改 auth_method
   - 这些用户保留原有 `auth_method`，下次用原方式能否登录完全取决于原 Provider 的校验规则

4. **try_get_user 两阶段匹配的冲突风险**：所有 Provider 共用基类 `try_get_user`（先 username 后 email，大小写不敏感，无 auth_method 过滤）。若 IdP 或 LDAP 返回的身份字段恰好匹配另一个用户的 username（而非 email），可能发生跨用户错配登录。

5. **Provider 入口级路由不感知用户 auth_method**：`get_auth_provider()` 只看全局 `LDAP_ENABLED` 开关，不看用户本身是什么 auth_method。这意味着 OIDC 用户在 LDAP 启用时输入密码会先经过 LDAPProvider（虽然后续会被回退拒绝），但 MEALIE 用户在 LDAP 启用时完全绕开了 LDAP 认证。

---

## 4. 会话建立流程

### 4.1 初始登录 Token 与刷新 Token 的载荷差异

Mealie 存在 **两个独立的 JWT 生成函数**，分别服务于初始登录和 token 刷新，两者的载荷存在显著差异。

#### 4.1.1 初始登录 Token：`AuthProvider.create_access_token` — [auth_provider.py L29-L52](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/auth_provider.py#L29-L52)

所有登录方式（OIDC / 密码 / LDAP）在首次认证成功后，通过 Provider 基类的 `get_access_token()` 调用此静态方法：

```python
# auth_provider.py L12
ISS = "mealie"
remember_me_duration = timedelta(days=14)

# auth_provider.py L29-L36
def get_access_token(self, user: PrivateUser, remember_me=False) -> tuple[str, timedelta]:
    settings = get_app_settings()
    duration = timedelta(hours=settings.TOKEN_TIME)
    if remember_me:
        duration = max(remember_me_duration, duration)  # remember_me 时取 max(14天, TOKEN_TIME)
    return AuthProvider.create_access_token({"sub": str(user.id)}, duration)

# auth_provider.py L38-L52
@staticmethod
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> tuple[str, timedelta]:
    settings = get_app_settings()
    to_encode = data.copy()
    expires_delta = expires_delta or timedelta(hours=settings.TOKEN_TIME)
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    to_encode["iss"] = ISS    # ← 始终设置 iss="mealie"
    return (jwt.encode(to_encode, settings.SECRET, algorithm=ALGORITHM), expires_delta)
```

调用方：
- OIDC：[openid_provider.py L107](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L107)、[L114](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/openid_provider.py#L114) — `self.get_access_token(user, settings.OIDC_REMEMBER_ME)`
- 密码：[credentials_provider.py L56](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/credentials_provider.py#L56) — `self.get_access_token(user, data.remember_me)`
- LDAP：[ldap_provider.py L35](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/providers/ldap_provider.py#L35) — `self.get_access_token(user, data.remember_me)`

初始登录 Token 的载荷：
- `sub`: 用户 ID（UUID 字符串）
- `exp`: 过期时间戳（remember_me 时为 14 天，否则为 `TOKEN_TIME` 小时）
- `iss`: `"mealie"`（**始终存在**）

#### 4.1.2 刷新 Token：`security.create_access_token` — [security.py L31-L40](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/security/security.py#L31-L40) + [auth.py L147-L151](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/routes/auth/auth.py#L147-L151)

`GET /api/auth/refresh` 端点直接调用 **独立的** `security.create_access_token()`（注意不是基类的静态方法）：

```python
# auth.py L147-L151
@user_router.get("/refresh")
async def refresh_token(current_user: PrivateUser = Depends(get_current_user)):
    """Use a valid token to get another token"""
    access_token = security.create_access_token(data={"sub": str(current_user.id)})
    return MealieAuthToken.respond(access_token)

# security.py L31-L40
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    settings = get_app_settings()
    to_encode = data.copy()
    expires_delta = expires_delta or timedelta(hours=settings.TOKEN_TIME)
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    # ← 没有 iss 字段！
    return jwt.encode(to_encode, settings.SECRET, algorithm=ALGORITHM)
```

刷新 Token 的载荷：
- `sub`: 用户 ID（UUID 字符串）
- `exp`: 过期时间戳（固定为 `TOKEN_TIME` 小时，不支持 remember_me 长有效期）
- `iss`: **不存在**（`security.create_access_token` 未设置此字段）

#### 4.1.3 两种 Token 的载荷对比表

| 字段 / 特性 | 初始登录 Token（`AuthProvider.create_access_token`） | 刷新 Token（`security.create_access_token`） |
|------------|--------------------------------------------------|------------------------------------------|
| `sub` | ✅ 用户 ID（UUID） | ✅ 用户 ID（UUID） |
| `exp` | ✅ 过期时间；remember_me 时取 `max(14天, TOKEN_TIME)` | ✅ 过期时间；固定为 `TOKEN_TIME` 小时 |
| `iss` | ✅ 固定值 `"mealie"` | ❌ **不存在**（函数未写入） |
| `algorithm` | HS256 | HS256 |
| 返回值 | `tuple[str, timedelta]` (token, duration) | `str` (仅 token) |
| 调用方 | OIDC / 密码 / LDAP Provider 首次认证成功后 | `GET /api/auth/refresh` 端点 |

#### 4.1.4 设计差异的影响

1. **刷新 Token 没有 `iss` 字段**：两个函数重复实现了 JWT 生成逻辑，但 `security.create_access_token` 遗漏了 `iss`。由于后端鉴权 `get_current_user()` 中 `jwt.decode()` 未指定 `issuer="mealie"` 参数验证 issuer，因此缺少 `iss` 在当前代码中不会导致功能问题，但违反了 JWT 最佳实践，且两个实现不一致。

2. **刷新 Token 不支持 remember_me 长有效期**：`security.create_access_token` 的 `expires_delta` 默认 `TOKEN_TIME` 小时，且 `refresh_token` 端点调用时未传 remember_me 相关参数，因此刷新后的 token 始终是短有效期，即使用户首次登录时勾选了 remember_me，每次刷新都会重置为短时效。

3. **remember_me 的触发方式不同**：
   - OIDC：由后端配置 `OIDC_REMEMBER_ME` 全局控制
   - 密码 / LDAP：由前端登录表单的 `remember_me` 字段控制
   - 刷新：无 remember_me，固定短时效

4. **`get_current_user` 不校验 iss**：[dependencies.py L88-L123](file:///d:/fz/0601/solo-dogfeeding/code/118-mealie/mealie/core/dependencies/dependencies.py#L88-L123) 中 `jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])` 未指定 `issuer` 参数，意味着即使未来某版本刷新 token 补上了 `iss` 字段，只要签名正确就能通过鉴权。

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
