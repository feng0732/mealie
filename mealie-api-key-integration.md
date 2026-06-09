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

---

## 七、普通用户权限模型与 OperationChecks

### 7.1 用户权限字段定义

用户权限存储在数据库 `users` 表中，ORM 模型定义在 [users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L78-L82)：

```python
# Permissions
can_manage_household: Mapped[bool | None] = mapped_column(Boolean, default=False)
can_manage:         Mapped[bool | None] = mapped_column(Boolean, default=False)
can_invite:         Mapped[bool | None] = mapped_column(Boolean, default=False)
can_organize:       Mapped[bool | None] = mapped_column(Boolean, default=False)
admin:              Mapped[bool | None] = mapped_column(Boolean, default=False)
```

**权限层级与关联设置逻辑**（[users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L207-L226)）：

```python
def _set_permissions(self, admin, can_manage_household=False, can_manage=False, can_invite=False, can_organize=False, **_):
    self.admin = admin
    if self.admin:
        # 管理员自动拥有所有权限
        self.can_manage_household = True
        self.can_manage = True
        self.can_invite = True
        self.can_organize = True
        self.advanced = True
    else:
        # 普通用户按字段分别设置
        self.can_manage_household = can_manage_household
        self.can_manage = can_manage
        self.can_invite = can_invite
        self.can_organize = can_organize
```

| 权限字段 | 说明 |
|----------|------|
| `admin` | 超级管理员，可跨 Group/Household 操作，自动拥有全部其他权限 |
| `can_manage` | 组级管理权限，可设置用户权限、管理 Group 级别资源 |
| `can_manage_household` | 家庭级管理权限，可修改家庭偏好设置 |
| `can_invite` | 可创建邀请链接、发送邀请邮件 |
| `can_organize` | 可创建/修改/删除标签、分类、食谱等组织类资源 |

### 7.2 OperationChecks 统一检查类

所有控制器通过 `self.checks` 访问权限检查方法，其实现位于 [checks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/checks.py)：

```python
class OperationChecks:
    user: PrivateUser
    ForbiddenException = HTTPException(status.HTTP_403_FORBIDDEN)

    def can_manage_household(self) -> bool:
        if not self.user.can_manage_household:
            raise self.ForbiddenException
        return True

    def can_manage(self) -> bool:
        if not self.user.can_manage:
            raise self.ForbiddenException
        return True

    def can_invite(self) -> bool:
        if not self.user.can_invite:
            raise self.ForbiddenException
        return True

    def can_organize(self) -> bool:
        if not self.user.can_organize:
            raise self.ForbiddenException
        return True
```

**设计特点**：
- 检查不通过时直接抛出 `HTTP 403 Forbidden`，调用方无需额外处理返回值
- 通过 `BaseUserController.checks` 懒加载属性注入（[base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py#L169-L172)）

### 7.3 权限检查的实际调用场景

#### 场景 1：组织类操作（can_organize）
以标签管理为例，[controller_tags.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/organizers/controller_tags.py) 中所有写操作均检查：

```python
@router.post("", status_code=201)
def create_one(self, tag: TagIn):
    self.checks.can_organize()  # ← 写操作必须检查
    ...

@router.put("/{item_id}")
def update_one(self, item_id: UUID4, new_tag: TagIn):
    self.checks.can_organize()
    ...

@router.delete("/{item_id}")
def delete_recipe_tag(self, item_id: UUID4):
    self.checks.can_organize()
    ...
```

#### 场景 2：家庭偏好设置（can_manage_household）
[controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_household_self_service.py#L58-L62)：

```python
@router.put("/preferences", response_model=ReadHouseholdPreferences)
def update_household_preferences(self, new_pref: UpdateHouseholdPreferences):
    self.checks.can_manage_household()
    return self.repos.household_preferences.update(self.household_id, new_pref)
```

#### 场景 3：用户权限管理（can_manage）
[controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_household_self_service.py#L64-L87) 中不仅检查 `can_manage`，还额外做了三层范围校验：

```python
@router.put("/permissions", response_model=UserOut)
def set_member_permissions(self, permissions: SetPermissions):
    self.checks.can_manage()  # ① 必须有组管理权限

    target_user = self.repos.users.get_one(permissions.user_id)
    if target_user.group_id != self.group_id:          # ② 目标用户必须同 Group
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is not a member of this group")
    if target_user.household_id != self.household_id:  # ③ 目标用户必须同 Household
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is not a member of this household")
    if target_user.id == self.user.id:                  # ④ 不能修改自己的权限
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is not allowed to change their own permissions")
    ...
```

#### 场景 4：邀请功能（can_invite + 特殊范围限制）
[controller_invitations.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_invitations.py#L33-L55)：

```python
@router.post("", response_model=ReadInviteToken, status_code=status.HTTP_201_CREATED)
def create_invite_token(self, body: CreateInviteToken):
    if not self.user.can_invite:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="User is not allowed to create invite tokens")

    body.group_id = body.group_id or self.group_id
    body.household_id = body.household_id or self.household_id

    # 非管理员只能为自己的 Group/Household 创建邀请
    if not self.user.admin and (body.group_id != self.group_id or body.household_id != self.household_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only admins can create invite tokens for other groups or households")
    ...
```

---

## 八、Group / Household 数据范围隔离机制

Mealie 采用「Group（组）→ Household（家庭）」两级组织架构，所有数据查询通过 Repository 层的 `_filter_builder` 自动注入范围过滤，实现 API Key 携带的用户只能访问其所属范围内的数据。

### 8.1 两级组织架构模型

**Group 模型**: [group.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/group/group.py)
- 一个 Group 可包含多个 Household
- Group 级资源：分类（Category）、用户、标签组、AI 配置、数据导出等

**Household 模型**: [household.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/household.py)
- Household 从属于一个 Group（`group_id` 外键）
- Household 级资源：食谱、购物清单、餐计划、Webhook、通知器、Cookbook 等

**User 与两级组织的关联**: 每个用户同时属于一个 Group 和一个 Household（[users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L63-L68)）

```python
group_id: FilterableColumn[GUID] = mapped_column(GUID, ForeignKey("groups.id"), nullable=False, index=True)
group: Mapped["Group"] = orm.relationship("Group", back_populates="users")

household_id: FilterableColumn[GUID | None] = mapped_column(GUID, ForeignKey("households.id"), nullable=True, index=True)
household: Mapped["Household"] = orm.relationship("Household", back_populates="users")
```

### 8.2 Repository 层的三类访问控制

Repository 层在 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_generic.py) 中定义了三个基类，通过构造函数强制传入范围参数：

```python
class RepositoryGeneric[Schema, Model]:
    """无范围过滤 — 仅由管理员或内部系统使用"""
    _group_id: UUID4 | None = None
    _household_id: UUID4 | None = None

class GroupRepositoryGeneric[Schema, Model](RepositoryGeneric[Schema, Model]):
    """Group 级范围 — 自动注入 group_id 过滤"""
    def __init__(self, session, primary_key, sql_model, schema, *, group_id: UUID4 | None | NotSet):
        super().__init__(...)
        if group_id is NOT_SET:
            raise ValueError("group_id must be set")  # ← 强制校验
        self._group_id = group_id if group_id else None

class HouseholdRepositoryGeneric[Schema, Model](RepositoryGeneric[Schema, Model]):
    """Household 级范围 — 自动注入 group_id + household_id 过滤"""
    def __init__(self, session, primary_key, sql_model, schema, *, group_id, household_id):
        super().__init__(...)
        if group_id is NOT_SET:
            raise ValueError("group_id must be set")
        if household_id is NOT_SET:
            raise ValueError("household_id must be set")  # ← 强制校验
        self._group_id = group_id if group_id else None
        self._household_id = household_id if household_id else None
```

### 8.3 自动范围注入的核心逻辑

所有查询方法（`get_one`、`multi_query`、`page_all` 等）都通过 `_filter_builder` 自动拼接过滤条件：

```python
# [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_generic.py#L94-L102)
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id      # ← 自动注入 Group 过滤
    if self.household_id:
        dct["household_id"] = self.household_id  # ← 自动注入 Household 过滤
    return {**dct, **kwargs}
```

**调用示例**（`_query_one`）：
```python
def _query_one(self, match_value, match_key=None):
    fltr = self._filter_builder(**{match_key: match_value})  # 自动加上范围条件
    return self.session.execute(self._query().filter_by(**fltr)).unique().scalars().one()
```

这意味着即使用户通过 API Key 直接传入一个其他 Group 的资源 ID，查询也会因 `filter_by(group_id=...)` 条件不匹配而返回空。

### 8.4 典型 Repository 的范围分类

| Repository 类型 | 示例资源 | 代码位置 |
|-----------------|----------|----------|
| `GroupRepositoryGeneric` | API Token、用户、分类、标签、通知器、Webhook | [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py#L190-L192) |
| `HouseholdRepositoryGeneric` | 食谱、购物清单、餐计划、Cookbook、家庭偏好 | [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py#L240-L243) |
| `RepositoryGeneric`（无范围） | 仅限 `get_current_user` 内部调用、管理员路由 | — |

### 8.5 控制器基类决定 Repository 的范围

控制器基类在 `@property repos` 中自动使用当前用户的 Group/Household ID 创建 Repository：

```python
# [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/base_controllers.py#L47-L50)
@property
def repos(self):
    if not self._repos:
        # 使用当前用户的 group_id 和 household_id
        self._repos = AllRepositories(self.session, group_id=self.group_id, household_id=self.household_id)
    return self._repos
```

- `BaseUserController`：`self.group_id = user.group_id`, `self.household_id = user.household_id` → **仅能访问本家庭数据**
- `BaseAdminController`：`self._repos = AllRepositories(session, group_id=None, household_id=None)` → **管理员无范围限制**

### 8.6 Router 级别的强制认证

在 [routers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/routers.py) 中定义的路由类通过 `dependencies` 参数强制所有子路由必须认证：

```python
class AdminAPIRouter(APIRouter):
    """管理员路由 — 所有子路由自动注入 get_admin_user"""
    def __init__(self, tags=None, prefix="", **kwargs):
        super().__init__(tags=tags, prefix=prefix, dependencies=[Depends(get_admin_user)], **kwargs)

class UserAPIRouter(APIRouter):
    """用户路由 — 所有子路由自动注入 get_current_user"""
    def __init__(self, tags=None, prefix="", **kwargs):
        super().__init__(tags=tags, prefix=prefix, dependencies=[Depends(get_current_user)], **kwargs)
```

这意味着外部应用使用 API Key 访问 `UserAPIRouter` 下的任何端点时，都会先经过 `get_current_user` 校验，确保 Token 有效且用户存在。

---

## 九、Webhook 与通知（Apprise）消费的权限差异

Mealie 的事件总线有两种下游消费者：**Webhook（定时触发的数据推送）** 和 **Apprise 通知（实时事件通知）**，两者在触发时机、数据内容、权限模型上有本质区别。

### 9.1 事件总线的两条消费链路

```
EventBusService.dispatch(event)
    │
    ├──→ AppriseEventListener  → ApprisePublisher  → 第三方通知渠道（Slack/邮件/Telegram 等）
    │     （实时事件触发，发送 title + body 文本消息）
    │
    └──→ WebhookEventListener  → WebhookPublisher  → 用户配置的 HTTP URL
          （仅定时任务触发 webhook_task 事件，POST 完整数据 JSON）
```

### 9.2 AppriseEventListener（通知）

**代码位置**: [event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L72-L132)

#### 9.2.1 通知数据模型

每个通知器（Notifier）按事件类型精细订阅，配置存储在 `group_events_notifiers` 表：

- **数据库模型**: [GroupEventNotifierModel](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/events.py#L60-L83)
- **订阅选项模型**: [GroupEventNotifierOptions](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/group_events.py#L13-L54)

每个事件类型（如 `recipe_created`、`tag_updated`）对应一个独立的布尔字段，用户可独立开关：

```python
class GroupEventNotifierOptions(MealieModel):
    recipe_created: bool = False
    recipe_updated: bool = False
    recipe_deleted: bool = False
    user_signup: bool = False
    mealplan_entry_created: bool = False
    shopping_list_created: bool = False
    # ... 其他事件类型
```

#### 9.2.2 订阅者筛选逻辑

```python
def get_subscribers(self, event: Event) -> list[str]:
    with self.ensure_repos(self.group_id, self.household_id) as repos:
        notifiers = repos.group_event_notifier.multi_query(
            {"enabled": True}, override_schema=GroupEventNotifierPrivate
        )
        # 仅返回对该事件类型订阅了的通知器 URL
        urls = [n.apprise_url for n in notifiers if getattr(n.options, event.event_type.name)]
        urls = AppriseEventListener.update_urls_with_event_data(urls, event)
    return urls
```

**范围约束**：`repos.group_event_notifier` 是 `HouseholdRepositoryGeneric`，自动按当前 Household 过滤，不会触发其他家庭的通知器。

#### 9.2.3 ApprisePublisher 实际发送

**位置**: [publisher.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/publisher.py#L14-L37)

```python
class ApprisePublisher:
    def publish(self, event: Event, notification_urls: list[str]):
        for dest in notification_urls:
            tag = str(event.event_id)
            self.apprise.add(dest, tag=tag)
        # 仅发送 title + body，不包含完整业务数据
        self.apprise.notify(title=event.message.title, body=event.message.body, tag=tags)
```

**关键特征**：
- **通知内容极简**：只包含 `event.message.title` 和 `event.message.body`（人类可读文本），不暴露食谱详情、用户信息等敏感数据
- **支持自定义参数扩展**：对 `form`、`json`、`xml` 等结构化 URL，通过 query params 注入 `event_type`、`integration_id`、`document_data`、`event_id`、`timestamp`（[event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L91-L109)）

### 9.3 WebhookEventListener（数据推送）

**代码位置**: [event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L134-L179)

#### 9.3.1 Webhook 数据模型

Webhook 配置存储在 `webhook_urls` 表：
- **数据库模型**: [GroupWebhooksModel](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/webhooks.py#L16-L40)
- **关键字段**：`url`、`enabled`、`webhook_type`（目前仅 `"mealplan"`）、`scheduled_time`（每日触发时间）

#### 9.3.2 与 Apprise 的核心差异

Webhook **不响应实时 CRUD 事件**，仅响应定时任务触发的 `webhook_task` 事件：

```python
def get_subscribers(self, event: Event) -> list[ReadWebhook]:
    # 仅处理 webhook_task 类型事件 + EventWebhookData 数据
    if not (event.event_type == EventTypes.webhook_task and isinstance(event.document_data, EventWebhookData)):
        return []  # ← 所有实时 CRUD 事件都不会触发 Webhook

    return self.get_scheduled_webhooks(
        event.document_data.webhook_start_dt, event.document_data.webhook_end_dt
    )
```

Webhook 的触发由 [post_webhooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/scheduler/tasks/post_webhooks.py) 定时任务每日在指定时间执行。

#### 9.3.3 WebhookPublisher 实际发送

**位置**: [publisher.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/publisher.py#L40-L49)

```python
class WebhookPublisher:
    def publish(self, event: Event, notification_urls: list[str]):
        event_payload = jsonable_encoder(event)  # ← 发送完整 Event 对象（含 document_data）
        for url in notification_urls:
            requests.post(url, json=event_payload, timeout=15)
```

**关键特征**：
- **发送完整 Event JSON**：包括 `integration_id`、`document_data`（完整业务数据）、`event_id`、`timestamp`
- **内容取决于 document_type**：当前仅支持 `mealplan`，会查询并发送指定时间范围内的全部餐计划数据（[event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L153-L159)）

### 9.4 Apprise vs Webhook 对比总结

| 维度 | Apprise 通知 | Webhook 数据推送 |
|------|-------------|----------------|
| **触发时机** | 实时 CRUD 事件发生时 | 每日定时任务（scheduled_time） |
| **事件范围** | 所有 EventTypes（按订阅开关） | 仅 `webhook_task` 事件 |
| **发送内容** | `title` + `body`（简短文本） | 完整 Event JSON（含 `document_data` 业务数据） |
| **数据敏感性** | 低（无用户/食谱详情） | 高（含完整餐计划/食谱数据） |
| **integration_id 暴露** | 通过 URL query params（仅限结构化协议） | 直接在 JSON body 中 |
| **配置位置** | Household → Notifiers | Household → Webhooks |
| **权限要求** | Household 成员可配置 | Household 成员可配置 |
| **数据库表** | `group_events_notifiers` + `group_events_notifier_options` | `webhook_urls` |
| **Publisher 实现** | `ApprisePublisher`（apprise 库） | `WebhookPublisher`（requests.post） |

---

## 十、API Key 权限边界全景图

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                          API Key 请求进入系统                                      │
│                  Authorization: Bearer <long_lived_jwt>                           │
└──────────────────────────────────┬───────────────────────────────────────────────┘
                                   │
          ┌────────────────────────▼────────────────────────┐
          │  Layer 1: 身份认证（get_current_user）            │
          │  • JWT 签名校验（HS256 + settings.SECRET）        │
          │  • JWT 过期时间校验（exp claim）                  │
          │  • long_token 标识 → 查 DB 确认 Token 未被吊销     │
          │  • 返回 PrivateUser 对象                          │
          └────────────────────────┬────────────────────────┘
                                   │
          ┌────────────────────────▼────────────────────────┐
          │  Layer 2: Router 级认证（UserAPIRouter）          │
          │  • dependencies=[Depends(get_current_user)]     │
          │  • 未通过直接返回 401                             │
          └────────────────────────┬────────────────────────┘
                                   │
          ┌────────────────────────▼────────────────────────┐
          │  Layer 3: 数据范围隔离（Repository 层）            │
          │  • AllRepositories(group_id=user.group_id,       │
          │                    household_id=user.household_id)│
          │  • _filter_builder 自动注入 group_id + household_id│
          │  • 跨范围 ID 查询返回空 → 404                      │
          └────────────────────────┬────────────────────────┘
                                   │
          ┌────────────────────────▼────────────────────────┐
          │  Layer 4: 功能权限检查（OperationChecks）         │
          │  • can_organize: 标签/分类/食谱 CRUD              │
          │  • can_manage_household: 家庭偏好设置             │
          │  • can_manage: 用户权限管理、组级配置              │
          │  • can_invite: 创建邀请链接/邮件                  │
          │  • admin: 跳过所有范围限制 + 拥有全部权限          │
          └────────────────────────┬────────────────────────┘
                                   │
          ┌────────────────────────▼────────────────────────┐
          │  Layer 5: 业务逻辑 + 事件发布                      │
          │  • publish_event(event_type, document_data)      │
          │  • 携带 integration_id 注入 Event 对象            │
          └────────────────────────┬────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
          ┌─────────▼──────────┐      ┌──────────▼───────────┐
          │ Apprise 通知        │      │ Webhook 数据推送     │
          │ • 实时触发          │      │ • 每日定时触发       │
          │ • title + body 文本 │      │ • 完整 Event JSON    │
          │ • 低敏感            │      │ • 含业务数据（高敏感）│
          └────────────────────┘      └──────────────────────┘
```

---

## 十一、补充文件索引

| 文件 | 职责 |
|------|------|
| [checks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/checks.py) | OperationChecks 权限检查类实现 |
| [routers.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/_base/routers.py) | AdminAPIRouter / UserAPIRouter 路由级强制认证 |
| [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_generic.py) | 三级 Repository 基类、_filter_builder 范围注入 |
| [household_permissions.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/household_permissions.py) | SetPermissions 设置用户权限请求模型 |
| [group_events.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/group_events.py) | GroupEventNotifierOptions 按事件类型订阅配置 |
| [events.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/events.py) | GroupEventNotifierModel / GroupEventNotifierOptionsModel ORM |
| [webhooks.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/webhooks.py) | GroupWebhooksModel Webhook 配置 ORM |
| [publisher.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/publisher.py) | ApprisePublisher / WebhookPublisher 两种发送实现 |
| [event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/services/event_bus_service/event_bus_listeners.py) | AppriseEventListener / WebhookEventListener 订阅筛选逻辑 |
| [controller_tags.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/organizers/controller_tags.py) | can_organize 检查示例（标签 CRUD） |
| [controller_invitations.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_invitations.py) | can_invite + 范围限制检查示例 |
| [controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_household_self_service.py) | can_manage_household / can_manage 检查示例 |
| [group.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/group/group.py) | Group ORM 模型（两级组织架构顶层） |
| [household.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/household.py) | Household ORM 模型（两级组织架构底层） |

---

## 十二、通知器与 Webhook 配置端点的 Repository 范围与权限边界深度分析

通知器（Event Notifiers / Apprise）和 Webhook 是 Mealie 向外暴露事件的两条核心通道。两者的 Repository 归属、权限检查模式存在容易混淆的细节，尤其是与其他 Household 级资源（如 Cookbook、标签分类）对比时，权限边界设计呈现出显著差异。

### 12.1 两个控制器的基本结构对比

| 维度 | 通知器（Event Notifiers） | Webhook |
|------|---------------------------|---------|
| 控制器 | [GroupEventsNotifierController](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_group_notifications.py#L36-L104) | [ReadWebhookController](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_webhooks.py#L18-L66) |
| 基类 | `BaseUserController` | `BaseUserController` |
| 路由前缀 | `/households/events/notifications` | `/households/webhooks` |
| 所属模块注册 | [households/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/__init__.py#L18-L24) | 同上 |
| Router 类型 | `APIRouter`（裸路由） | `APIRouter`（裸路由） |

两者均直接继承 `BaseUserController`、使用裸 `APIRouter`（而非 `UserAPIRouter`），但由于 `BaseUserController` 内部已通过 `@controller` 装饰器注入 `get_current_user`，因此所有端点仍强制要求 Bearer Token 认证。

### 12.2 Repository 范围归属：都是 HouseholdRepositoryGeneric

两个资源在 [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py#L382-L397) 中均注册为 **HouseholdRepositoryGeneric**，即同时按 `group_id` 和 `household_id` 双重过滤：

```python
@cached_property
def group_event_notifier(self) -> HouseholdRepositoryGeneric[GroupEventNotifierOut, GroupEventNotifierModel]:
    return HouseholdRepositoryGeneric(
        self.session,
        PK_ID,
        GroupEventNotifierModel,
        GroupEventNotifierOut,
        group_id=self.group_id,       # ← 控制器用户的 group_id
        household_id=self.household_id,  # ← 控制器用户的 household_id
    )

@cached_property
def webhooks(self) -> HouseholdRepositoryGeneric[ReadWebhook, GroupWebhooksModel]:
    return HouseholdRepositoryGeneric(
        self.session, PK_ID, GroupWebhooksModel, ReadWebhook,
        group_id=self.group_id, household_id=self.household_id
    )
```

结合 `RepositoryGeneric._filter_builder`（[repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_generic.py#L94-L102)），这意味着：

- **API Key 携带的用户属于 Household A** → 查询/修改只会命中 `household_id = A` 的记录
- 即使构造请求传入另一个 Household 的 Webhook/Notifier ID，`_filter_builder` 会自动追加范围条件，返回空 → 404
- 创建时通过 `mapper.cast(data, SaveModel, group_id=self.group_id, household_id=self.household_id)` 强制注入当前用户的范围（见下文）

### 12.3 创建时的范围注入机制

两个控制器的 `create_one` 方法均**不在请求体中接受 group_id/household_id**，而是在服务端强制注入当前用户的值：

**通知器创建** — [controller_group_notifications.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_group_notifications.py#L64-L67)：
```python
@router.post("", response_model=GroupEventNotifierOut, status_code=201)
def create_one(self, data: GroupEventNotifierCreate):
    # 强制用当前用户的 group_id / household_id 覆盖
    save_data = cast(data, GroupEventNotifierSave, group_id=self.group_id, household_id=self.household_id)
    return self.mixins.create_one(save_data)
```

**Webhook 创建** — [controller_webhooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_webhooks.py#L38-L41)：
```python
@router.post("", response_model=ReadWebhook, status_code=201)
def create_one(self, data: CreateWebhook):
    # 同样强制注入范围
    save = mapper.cast(data, SaveWebhook, group_id=self.group_id, household_id=self.household_id)
    return self.mixins.create_one(save)
```

对应的 Schema 定义确保了前端请求体不含范围字段：
- `GroupEventNotifierCreate`（[group_events.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/group_events.py#L70-L73)）：仅含 `name`, `apprise_url`
- `CreateWebhook`（[webhook.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/webhook.py#L16-L22)）：仅含 `enabled`, `name`, `url`, `webhook_type`, `scheduled_time`

### 12.4 关键发现：两个控制器均无 OperationChecks

对两个控制器全文搜索 `self.checks.` / `can_`，**结果为空**：

| 端点 | 方法 | OperationChecks 调用 | 实际权限要求 |
|------|------|---------------------|-------------|
| GET `/households/events/notifications` | get_all | ❌ 无 | 任意已认证 Household 成员 |
| POST `/households/events/notifications` | create_one | ❌ 无 | 任意已认证 Household 成员 |
| GET `/households/events/notifications/{id}` | get_one | ❌ 无 | 任意已认证 Household 成员 |
| PUT `/households/events/notifications/{id}` | update_one | ❌ 无 | 任意已认证 Household 成员 |
| DELETE `/households/events/notifications/{id}` | delete_one | ❌ 无 | 任意已认证 Household 成员 |
| POST `/households/events/notifications/{id}/test` | test_notification | ❌ 无 | 任意已认证 Household 成员 |
| GET `/households/webhooks` | get_all | ❌ 无 | 任意已认证 Household 成员 |
| POST `/households/webhooks` | create_one | ❌ 无 | 任意已认证 Household 成员 |
| POST `/households/webhooks/rerun` | rerun_webhooks | ❌ 无 | 任意已认证 Household 成员 |
| GET `/households/webhooks/{id}` | get_one | ❌ 无 | 任意已认证 Household 成员 |
| POST `/households/webhooks/{id}/test` | test_one | ❌ 无 | 任意已认证 Household 成员 |
| PUT `/households/webhooks/{id}` | update_one | ❌ 无 | 任意已认证 Household 成员 |
| DELETE `/households/webhooks/{id}` | delete_one | ❌ 无 | 任意已认证 Household 成员 |

这意味着：**只要 API Key 对应的用户是该 Household 的成员（无论权限等级），就可以完整 CRUD 通知器和 Webhook，包括删除其他成员创建的配置。**

### 12.5 前端「隐形」防线：`advanced-only` Middleware

虽然后端无 OperationChecks，但前端页面通过 Nuxt 路由中间件设置了访问门槛：

**Webhook 前端** — [webhooks.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/household/webhooks.vue#L76-L78)：
```typescript
definePageMeta({
  middleware: ["advanced-only"],
});
```

**通知器前端** — [notifiers.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/household/notifiers.vue#L201-L203)：
```typescript
definePageMeta({
  middleware: ["advanced-only"],
});
```

**中间件实现** — [advanced-only.ts](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/middleware/advanced-only.ts)：
```typescript
export default defineNuxtRouteMiddleware(() => {
  const { user } = useMealieAuth();
  if (!user.value?.advanced) {
    console.warn("User is not allowed to access advanced features");
    navigateTo("/");
  }
});
```

**`advanced` 字段的后端定义**：
- ORM：[users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L61) `advanced: Mapped[bool | None] = mapped_column(Boolean, default=False)`
- Schema：[user.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/user/user.py#L118) `advanced: bool = False`
- 管理员自动获得：[users.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/users/users.py#L225) `self.advanced = True`

**关键不对称**：后端 `routes/households/` 下所有控制器代码中**完全没有**对 `user.advanced` 的校验（对全项目 grep `self.user.advanced` 结果为空）。这意味着持有普通成员 API Key 的外部应用可以绕过前端，直接调用后端 API 创建/修改/删除通知器和 Webhook。

### 12.6 与其他 Household/Group 级资源的权限对比

将通知器、Webhook 与相邻资源的权限模式并置，可以看出 Mealie 在 Household 级资源上的权限设计并不统一：

| 资源 | Repository 类型 | 写操作是否需要 OperationChecks | 具体检查 | 控制器文件 |
|------|----------------|-------------------------------|----------|-----------|
| 通知器（Notifiers） | HouseholdRepositoryGeneric | ❌ 无 | — | [controller_group_notifications.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_group_notifications.py) |
| Webhook | HouseholdRepositoryGeneric | ❌ 无 | — | [controller_webhooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_webhooks.py) |
| Cookbook | HouseholdRepositoryGeneric | ❌ 无 | — | [controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_cookbooks.py) |
| 购物清单（Shopping List） | HouseholdRepositoryGeneric | ❌ 无 | — | [controller_shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_shopping_lists.py) |
| 家庭偏好设置 | HouseholdRepositoryGeneric | ✅ 有 | `can_manage_household` | [controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_household_self_service.py#L58-L62) |
| 用户权限分配 | HouseholdRepositoryGeneric | ✅ 有 | `can_manage` + 多层范围校验 | [controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_household_self_service.py#L64-L87) |
| 邀请链接/邮件 | GroupRepositoryGeneric | ✅ 有 | `can_invite` + 跨组限制 | [controller_invitations.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_invitations.py#L33-L55) |
| 标签（Tag） | GroupRepositoryGeneric | ✅ 有 | `can_organize` | [controller_tags.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/organizers/controller_tags.py#L51-L54) |
| 分类（Category） | GroupRepositoryGeneric | ✅ 有 | `can_organize` | [controller_categories.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/organizers/controller_categories.py) |

**规律总结**：
- **Group 级资源**（标签、分类、邀请）：统一使用 `can_organize` / `can_invite` 检查
- **Household 级高敏感配置**（用户权限、家庭偏好）：使用 `can_manage` / `can_manage_household` 检查
- **Household 级常规业务资源**（Cookbook、购物清单、通知器、Webhook）：**纯范围隔离，无功能权限检查**

### 12.7 API Key 持有者对通知器/Webhook 的实际可达能力

基于以上分析，一个持有 Household 普通成员（非 admin、无任何 can_* 权限）API Key 的外部应用，在通知器和 Webhook 上的实际权限边界为：

```
┌─────────────────────────────────────────────────────────────────────┐
│             API Key 用户（普通 Household 成员）                        │
│  admin=False, can_manage=False, can_invite=False, can_organize=False │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
        ┌──────────────────┴──────────────────┐
        │                                     │
┌───────▼─────────┐                 ┌─────────▼────────┐
│  通知器 CRUD     │                 │  Webhook CRUD     │
│  ✅ 全部允许     │                 │  ✅ 全部允许      │
│  - 创建/编辑/删除│                 │  - 创建/编辑/删除 │
│  - 发送测试通知  │                 │  - 触发 rerun     │
│                 │                 │  - 发送测试 Webhook│
└───────┬─────────┘                 └─────────┬────────┘
        │                                     │
        └──────────────────┬──────────────────┘
                           │
                  ┌────────▼────────┐
                  │  Repository 层   │
                  │  Household 过滤   │
                  │  - 不能跨家庭操作  │
                  │  - 不能跨组操作    │
                  └─────────────────┘
                           │
                  ┌────────▼────────┐
                  │  前端限制          │
                  │  advanced-only    │
                  │  ⚠️ 后端不校验      │
                  └─────────────────┘
```

### 12.8 通知器 vs Webhook 配置权限的细微差异

虽然两者都无 OperationChecks，但在**测试端点**和**关联事件的可见性**上存在细微区别：

| 特性 | 通知器测试 | Webhook 测试 |
|------|-----------|-------------|
| 路由 | `POST /households/events/notifications/{id}/test` | `POST /households/webhooks/{id}/test` |
| 实现 | 同步调用 `AppriseEventListener.publish_to_subscribers` | 通过 `BackgroundTasks` 异步执行 `post_test_webhook` |
| 发送内容 | 简单 Event（test_message 类型） | 实际构造包含 webhook body 的完整 Event |
| 测试 rerun | 无 | `POST /households/webhooks/rerun`（重跑今日全部定时 Webhook） |

---

## 十三、关键文件索引（补充：通知器与 Webhook 专项）

| 文件 | 职责 |
|------|------|
| [controller_group_notifications.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_group_notifications.py) | 通知器 CRUD + 测试端点（无 OperationChecks） |
| [controller_webhooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_webhooks.py) | Webhook CRUD + rerun + 测试端点（无 OperationChecks） |
| [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/repos/repository_factory.py#L382-L397) | 通知器与 Webhook 注册为 HouseholdRepositoryGeneric |
| [group_events.py (schema)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/group_events.py) | GroupEventNotifierCreate/Save/Out 等 Schema |
| [webhook.py (schema)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/schema/household/webhook.py) | CreateWebhook/SaveWebhook/ReadWebhook 等 Schema |
| [events.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/events.py) | GroupEventNotifierModel + OptionsModel ORM |
| [webhooks.py (db)](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/db/models/household/webhooks.py) | GroupWebhooksModel ORM |
| [controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_cookbooks.py) | Cookbook 控制器（同样无 OperationChecks，作为对照组） |
| [controller_shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/mealie/routes/households/controller_shopping_lists.py) | 购物清单控制器（同样无 OperationChecks，作为对照组） |
| [notifiers.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/household/notifiers.vue) | 通知器前端页面（使用 advanced-only middleware） |
| [webhooks.vue](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/pages/household/webhooks.vue) | Webhook 前端页面（使用 advanced-only middleware） |
| [advanced-only.ts](file:///d:/fz/0601/solo-dogfeeding/code/121-mealie/frontend/app/middleware/advanced-only.ts) | 前端 advanced-only 中间件实现 |
