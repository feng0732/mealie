# Mealie API Key 与外部应用集成代码路径分析

本文档梳理 Mealie 项目中 API Key（长生命周期 Token）的生成、权限校验和外部请求识别的完整代码路径。

---

## 一、整体架构概览

```
外部应用请求
    │
    ▼
┌─────────────────────────────┐
│  请求携带 Authorization     │
│  Header (Bearer Token)      │
└───────────┬─────────────────┘
            │
            ▼
┌─────────────────────────────┐
│  FastAPI Dependencies       │
│  - oauth2_scheme            │
│  - get_current_user         │
│  - get_integration_id       │
└───────────┬─────────────────┘
            │
            ▼
┌─────────────────────────────┐
│  权限校验                   │
│  - JWT 解码验证             │
│  - Long-Lived Token DB 校验 │
│  - 权限字段检查             │
└───────────┬─────────────────┘
            │
            ▼
┌─────────────────────────────┐
│  业务逻辑执行               │
│  - 事件发布携带 integration_id
│  - 操作审计追踪             │
└─────────────────────────────┘
```

---

## 二、API Key（长生命周期 Token）生成流程

### 2.1 前端触发

用户在个人资料页面创建 API Token：

- **前端页面**: [api-tokens.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/user/profile/api-tokens.vue)
  - 用户输入 Token 名称，点击「Generate」按钮
  - 调用 `createToken(name)` → `api.users.createAPIToken({ name })`

- **前端 API 封装**: [users.ts](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/lib/api/user/users.ts#L82-L84)
  ```typescript
  async createAPIToken(tokenName: LongLiveTokenIn) {
    return await this.requests.post<LongLiveTokenOut>(routes.usersApiTokens, tokenName);
  }
  ```
  - 请求路径: `POST /api/users/api-tokens`

### 2.2 后端路由处理

- **路由定义**: [api_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/api_tokens.py)
- **核心处理函数**: [create_api_token](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/api_tokens.py#L21-L47)

关键逻辑：
```python
@router.post("/api-tokens", status_code=status.HTTP_201_CREATED)
def create_api_token(self, token_params: LongLiveTokenIn):
    token_data = {
        "long_token": True,              # 标记为长生命周期 Token
        "id": str(self.user.id),         # 关联用户 ID
        "name": token_params.name,       # Token 名称
        "integration_id": token_params.integration_id,  # 外部应用标识
    }

    five_years = timedelta(1825)          # 有效期：5 年
    token = create_access_token(token_data, five_years)  # 生成 JWT

    token_model = CreateToken(
        name=token_params.name,
        token=token,
        user_id=self.user.id,
    )
    new_token_in_db = self.repos.api_tokens.create(token_model)  # 存入数据库
    return new_token_in_db
```

### 2.3 Token 生成核心函数

- **位置**: [security.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/security/security.py#L31-L40)

```python
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    settings = get_app_settings()
    to_encode = data.copy()
    expires_delta = expires_delta or timedelta(hours=settings.TOKEN_TIME)
    expire = datetime.now(UTC) + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.SECRET, algorithm=ALGORITHM)  # HS256
```

**关键点**:
- 算法: HS256（对称加密）
- 密钥来源: `settings.SECRET`（环境变量配置）
- JWT Payload 包含:
  - `long_token`: 是否为长生命周期 Token（API Key）
  - `id` / `sub`: 用户 ID
  - `name`: Token 名称
  - `integration_id`: 外部应用标识（默认为 `"generic"`）
  - `exp`: 过期时间戳

### 2.4 Token 数据模型

**Schema 层** - [user.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/user/user.py):

| 类名 | 用途 | 关键字段 |
|------|------|----------|
| `LongLiveTokenIn` | 创建请求入参 | `name`, `integration_id` |
| `CreateToken` | 数据库写入模型 | `name`, `token`, `user_id` |
| `LongLiveTokenOut` | 列表查询响应 | `name`, `id`, `created_at` |
| `LongLiveTokenCreateResponse` | 创建响应（含明文 Token） | 上述字段 + `token` |
| `LongLiveTokenInDB` | 数据库完整记录 | 上述字段 + `user` 关联 |

**数据库层** - [users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L28-L43):

```python
class LongLiveToken(SqlAlchemyBase, BaseMixins):
    __tablename__ = "long_live_tokens"
    name: Mapped[str] = mapped_column(String, nullable=False)
    token: Mapped[str] = mapped_column(String, nullable=False, index=True)  # JWT 全文存储
    user_id: Mapped[GUID | None] = mapped_column(GUID, ForeignKey("users.id"), index=True)
    user: Mapped[Optional["User"]] = orm.relationship("User")
```

**Repository 层** - [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py#L190-L192):

```python
@cached_property
def api_tokens(self) -> GroupRepositoryGeneric[LongLiveTokenInDB, LongLiveToken]:
    return GroupRepositoryGeneric(self.session, PK_ID, LongLiveToken, LongLiveTokenInDB, group_id=self.group_id)
```

### 2.5 Token 删除

- **路由**: [api_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/api_tokens.py#L49-L61)
- 校验逻辑: 仅允许 Token 所属用户本人删除
  ```python
  if token.user.email == self.user.email:
      deleted_token = self.repos.api_tokens.delete(token_id)
  else:
      raise HTTPException(status.HTTP_403_FORBIDDEN)
  ```

---

## 三、权限校验流程

### 3.1 认证依赖注入入口

所有需要认证的路由通过 FastAPI 的 `Depends` 机制注入当前用户：

- **核心依赖定义**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py)

#### 3.1.1 OAuth2 Token 提取

```python
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
oauth2_scheme_soft_fail = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)
```

- `oauth2_scheme`: 严格模式，无 Token 时直接返回 401
- `oauth2_scheme_soft_fail`: 宽松模式，无 Token 时返回 `None`

#### 3.1.2 主认证函数 `get_current_user`

**位置**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py#L88-L123)

执行流程：
1. **Token 来源**: 优先从 `Authorization` Header 提取，若为空则尝试从 Cookie `mealie.access_token` 读取
2. **JWT 解码**: 使用 `jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])`
3. **分支判断**:
   - 若 Payload 含 `long_token` 字段 → 走长生命周期 Token 校验（见 3.2）
   - 否则 → 走普通短期 Token 校验（从 `sub` 字段提取 user_id 查询用户）
4. **用户查询**: 通过 `repos.users.get_one(token_data.user_id, "id")` 获取用户信息
5. **Session 提交**: 显式 `session.commit()` 避免 PostgreSQL 表锁问题

### 3.2 长生命周期 Token（API Key）校验

**位置**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py#L141-L149)

```python
def validate_long_live_token(session: Session, client_token: str, user_id: str) -> PrivateUser:
    repos = get_repositories(session, group_id=None, household_id=None)
    token = repos.api_tokens.multi_query({"token": client_token, "user_id": user_id})
    try:
        return token[0].user
    except IndexError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED) from e
```

**双重验证机制**:
1. JWT 本身的签名和过期时间验证（`jwt.decode` 已完成）
2. 数据库中查询匹配 `token` + `user_id` 的记录（防止 Token 被吊销后仍可使用）

### 3.3 登录态检测（宽松模式）

**位置**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py#L36-L64)

```python
async def is_logged_in(token: str = Depends(oauth2_scheme_soft_fail), session=...) -> bool:
    try:
        payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        long_token: str = payload.get("long_token")
        if long_token is not None:
            try:
                if validate_long_live_token(session, token, payload.get("id")):
                    return True
            except Exception:
                return False
        return user_id is not None
    except Exception:
        return False
```

用于无需强制认证但需区分登录/匿名用户的场景。

### 3.4 管理员权限校验

**位置**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py#L135-L138)

```python
async def get_admin_user(current_user: PrivateUser = Depends(get_current_user)) -> PrivateUser:
    if not current_user.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return current_user
```

### 3.5 控制器基类中的集成

**位置**: [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py)

| 基类 | 注入的依赖 | 适用场景 |
|------|------------|----------|
| `BaseUserController` | `get_current_user`, `get_integration_id` | 普通用户路由 |
| `BaseAdminController` | `get_admin_user` | 管理员路由（可跨 group/household） |
| `BaseCrudController` | 继承 `BaseUserController` + `EventBusService` | CRUD 操作（自动发布事件） |
| `BasePublicController` | 无认证依赖 | 公开路由 |

用户权限字段在 [users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L78-L82) 中定义：
- `admin`: 超级管理员
- `can_manage`: 组管理权限
- `can_manage_household`: 家庭管理权限
- `can_invite`: 邀请用户权限
- `can_organize`: 组织管理权限

---

## 四、外部请求识别（integration_id 机制）

### 4.1 integration_id 的来源

integration_id 是 Mealie 用于标识请求来源（哪个外部应用发起的操作）的关键标识。

**默认值定义**: [user.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/user/user.py#L24)
```python
DEFAULT_INTEGRATION_ID = "generic"
```

**内部系统标识**: [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_types.py#L10)
```python
INTERNAL_INTEGRATION_ID = "mealie_generic_user"
```

### 4.2 从 Token 中提取 integration_id

**位置**: [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py#L126-L132)

```python
async def get_integration_id(token: str = Depends(oauth2_scheme)) -> str:
    try:
        decoded_token = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        return decoded_token.get("integration_id", DEFAULT_INTEGRATION_ID)
    except PyJWTError as e:
        raise credentials_exception from e
```

### 4.3 控制器层的传递

**位置**: [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py#L139-L140)

```python
class BaseUserController(_BaseController):
    user: PrivateUser = Depends(get_current_user)
    integration_id: str = Depends(get_integration_id)  # 自动注入
```

所有继承 `BaseUserController` 的控制器均可通过 `self.integration_id` 获取请求来源标识。

### 4.4 在事件总线中的传播

当 CRUD 操作发生时，`integration_id` 会被携带到事件总线中，供 Webhook、通知等下游系统使用。

#### 4.4.1 事件发布（BaseCrudController）

**位置**: [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py#L199-L214)

```python
def publish_event(self, event_type, document_data, group_id, household_id, message=""):
    self.event_bus.dispatch(
        integration_id=self.integration_id,  # ← 传递来源标识
        group_id=group_id,
        household_id=household_id,
        event_type=event_type,
        document_data=document_data,
        message=message,
    )
```

#### 4.4.2 事件模型

**位置**: [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_types.py#L194-L207)

```python
class Event(MealieModel):
    message: EventBusMessage
    event_type: EventTypes
    integration_id: str           # ← 来源标识
    document_data: SerializeAsAny[EventDocumentDataBase]
    event_id: UUID4 | None = None
    timestamp: datetime | None = None
```

#### 4.4.3 事件总线分发

**位置**: [event_bus_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_service.py#L66-L96)

```python
def dispatch(self, integration_id, group_id, household_id, event_type, document_data, message=""):
    event = Event(
        message=EventBusMessage.from_type(event_type, body=message),
        event_type=event_type,
        integration_id=integration_id,  # ← 存入 Event 对象
        document_data=document_data,
    )
    # 向所有 household 广播，并通过 BackgroundTasks 异步处理
    for household_id in household_ids:
        self.bg.add_task(self._publish_event, event=event, group_id=group_id, household_id=household_id)
```

#### 4.4.4 事件监听器传递给 Webhook

**位置**: [event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L94)

```python
# Webhook 的 POST body 中包含 integration_id
"integration_id": event.integration_id,
```

### 4.5 典型的 integration_id 取值场景

| 场景 | integration_id | 代码位置 |
|------|----------------|----------|
| 普通 Web 用户登录 | `"generic"`（默认） | [user.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/user/user.py#L24) |
| 创建 API Token 时指定 | 用户自定义值 | [api_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/api_tokens.py#L32) |
| 用户注册流程 | `"registration"` | [registration.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/registration.py#L39) |
| 测试事件发送 | `"test_event"` | [controller_group_notifications.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_group_notifications.py#L99) |
| 定时任务（内部） | `"mealie_generic_user"` | [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_types.py#L10) |

---

## 五、完整调用链路总结

### 5.1 创建 API Token 链路

```
前端 [api-tokens.vue]
  → POST /api/users/api-tokens
    → [users.ts] createAPIToken()
      → [api_tokens.py] UserApiTokensController.create_api_token()
        → [security.py] create_access_token(data, 5年)  → 生成 JWT
        → [repository_factory.py] repos.api_tokens.create()
          → [users.py] LongLiveToken 模型写入 DB
    ← 返回 LongLiveTokenCreateResponse（含明文 token，仅此一次）
```

### 5.2 外部应用调用受保护接口链路

```
外部应用 (携带 Authorization: Bearer <api_token>)
  → FastAPI Route Handler
    → [dependencies.py] oauth2_scheme 提取 Token
    → [dependencies.py] get_current_user()
      → jwt.decode() 验证 JWT 签名和过期时间
      → 检测到 long_token=True
        → [dependencies.py] validate_long_live_token()
          → DB 查询 long_live_tokens 表中匹配 (token, user_id) 的记录
          → 返回关联的 User 对象
    → [dependencies.py] get_integration_id()
      → 从 JWT Payload 提取 integration_id
    → 执行业务逻辑
      → [base_controllers.py] publish_event()
        → [event_bus_service.py] EventBusService.dispatch(integration_id=...)
          → Webhook / Apprise 通知外部系统（携带 integration_id）
```

### 5.3 短期登录 Token vs 长生命周期 API Token

| 特性 | 短期 Token（登录） | 长生命周期 Token（API Key） |
|------|-------------------|---------------------------|
| JWT 标识字段 | `sub` (user_id) | `long_token=True`, `id` (user_id) |
| 有效期 | 由 `TOKEN_TIME` 配置（小时级） | 5 年（1825 天） |
| 生成位置 | `AuthProvider.get_access_token()` | `create_api_token()` 路由 |
| 数据库校验 | 否（仅 JWT 验证） | 是（需匹配 DB 中存储的 token 记录） |
| 吊销方式 | 等待自然过期 | 删除 DB 中的 `long_live_tokens` 记录 |
| integration_id | `"generic"` | 创建时可自定义 |

---

## 六、关键文件索引

| 文件 | 职责 |
|------|------|
| [security.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/security/security.py) | JWT 生成与编码 |
| [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/dependencies/dependencies.py) | 认证依赖注入、权限校验、integration_id 提取 |
| [api_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/users/api_tokens.py) | API Token 创建/删除路由 |
| [auth.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/auth/auth.py) | 登录、OAuth、Token 刷新路由 |
| [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py) | 控制器基类，统一注入认证和 integration_id |
| [user.py (schema)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/user/user.py) | Token 相关 Pydantic 模型、DEFAULT_INTEGRATION_ID |
| [users.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py) | `LongLiveToken` ORM 模型、User 权限字段 |
| [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py) | `api_tokens` Repository 注册 |
| [event_bus_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_service.py) | 事件分发，传播 integration_id |
| [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_types.py) | 事件模型定义、INTERNAL_INTEGRATION_ID |
| [auth_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/security/providers/auth_provider.py) | 认证提供者抽象基类、短期 Token 生成 |
| [credentials_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/core/security/providers/credentials_provider.py) | 用户名密码认证实现 |
| [api-tokens.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/user/profile/api-tokens.vue) | 前端 Token 管理页面 |
| [users.ts](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/lib/api/user/users.ts) | 前端用户/Token API 封装 |
