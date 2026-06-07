# Public Share 与访问控制代码实现分析

## 一、整体架构概览

Mealie 的公开访问体系包含三条独立路径：

| 访问模式 | 入口路径 | 认证方式 | 权限边界 |
|---------|---------|---------|---------|
| 链接分享（Share Token） | `/api/recipes/shared/{token_id}` | 无认证，凭 Token UUID 访问 | Token 有效期 + 所属 Recipe |
| 公开浏览（Explore） | `/api/explore/groups/{group_slug}/...` | 无认证 | Group 非私有 + Household 非私有 + Recipe public=true |
| 媒体资源 | `/api/media/recipes/{recipe_id}/...` | 无认证 | 仅通过路径可达性（存在即返回） |

---

## 二、Share Token 匿名入口的真实挂载层级

### 2.1 四级路由挂载链

Share Token 匿名访问入口经过四层挂载，最终路径为 **`/api/recipes/shared/{token_id}`**，挂载链路如下：

```
1. [app.py#L147-L150] app.include_router(router)
                         ↓  router = APIRouter(prefix="/api")
2. [routes/__init__.py#L27] router.include_router(recipe.router)
                               ↓
3. [routes/recipe/__init__.py#L13]
       router.include_router(shared_routes.router, prefix="/recipes")
                                  ↓
4. [routes/recipe/shared_routes.py#L22]
       @router.get("/shared/{token_id}", response_model=Recipe)
```

具体代码证据：

**第 1 层** — [app.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/app.py#L147-L150)：
```python
def api_routers():
    app.include_router(router)       # 主 API 路由，prefix="/api"
    app.include_router(media_router) # media_router 独立挂载，prefix="/api/media"
    app.include_router(utility_routes.router)
```

**第 2 层** — [routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/__init__.py#L20-L27)：
```python
router = APIRouter(prefix="/api")
...
router.include_router(recipe.router)    # recipe 子路由
```

**第 3 层** — [routes/recipe/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/__init__.py#L5-L14)：
```python
prefix = "/recipes"
router = APIRouter()
...
router.include_router(shared_routes.router, prefix=prefix, tags=["Recipe: Shared"])
```

**第 4 层** — [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L17-L22)：
```python
router = APIRouter()   # 普通 APIRouter，无认证依赖

@router.get("/shared/{token_id}", response_model=Recipe)
def get_shared_recipe(token_id: UUID4, ...):
```

### 2.2 两套完全独立的 `/shared/` 路由

存在两套命名相似但用途、认证、挂载位置完全不同的路由：

| 路由 | 前缀 | Router 类型 | 认证 | 用途 |
|-----|------|------------|------|------|
| 匿名消费 | `/api/recipes/shared/{token_id}` | `APIRouter` | 无 | 用 Token 获取 Recipe JSON / Zip |
| 用户管理 | `/api/shared/recipes` | `UserAPIRouter` | 必须登录 | CRUD 管理自己 group 下的 Share Token |

**用户管理路由** — [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py#L12-L16)：
```python
router = UserAPIRouter(prefix="/shared/recipes", tags=["Shared: Recipes"])
# 挂载位置：routes/__init__.py#L29 → router.include_router(shared.router)
# 最终路径：/api + /shared/recipes = /api/shared/recipes
```

前端 API 客户端也印证了路径定义 — [frontend/app/lib/api/public/shared.ts](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/frontend/app/lib/api/public/shared.ts#L6-L7)：
```typescript
const routes = {
  recipeShareToken: (token: string) => `${prefix}/recipes/shared/${token}`,
};
```

测试文件路由常量同样确认 — [tests/utils/api_routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/utils/api_routes/__init__.py#L495-L502)：
```python
def recipes_shared_token_id(token_id):
    """`/api/recipes/shared/{token_id}`"""
    return f"{prefix}/recipes/shared/{token_id}"

def recipes_shared_token_id_zip(token_id):
    """`/api/recipes/shared/{token_id}/zip`"""
```

---

## 三、Share Token 机制详解

### 3.1 数据模型

Token 模型定义于 [db/models/recipe/shared.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/recipe/shared.py#L21-L34)：

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    group_id: FilterableColumn[GUID]
    recipe_id: FilterableColumn[GUID]
    recipe: Mapped["RecipeModel"] = relationship("RecipeModel", back_populates="share_tokens")
    expires_at: FilterableColumn[datetime]
```

关键设计：
- **UUID 作为主键**：Token 值本身就是数据库主键，无需额外签名/加密，安全性依赖于 UUID 的不可猜测性
- **级联删除**：`RecipeModel.share_tokens` 配置了 `cascade="all, delete, delete-orphan"`，删除 Recipe 时关联 Token 自动清除
- **过期时间默认 30 天**：由 `defaut_expires_at_time()` 函数统一设置

### 3.2 Token 创建（用户侧）

创建逻辑在 [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py#L33-L42)：

```python
@router.post("", response_model=RecipeShareToken, status_code=201)
def create_one(self, data: RecipeShareTokenCreate) -> RecipeShareToken:
    group_repos = get_repositories(self.repos.session, group_id=self.group_id, household_id=None)
    recipe = group_repos.recipes.get_one(data.recipe_id, "id")
    if recipe is None or recipe.group_id != self.group_id:
        raise HTTPException(status_code=404, detail="Recipe not found in your group")
```

越权防护点：
1. 路由使用 `UserAPIRouter`，必须登录认证
2. 创建前校验 Recipe 的 `group_id` 必须与当前用户的 `group_id` 一致
3. 跨 group 创建直接返回 404（测试验证见 [test_recipe_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py#L116-L122)）
4. 同 group 不同 household 允许创建

### 3.3 Token 消费（匿名侧）

消费逻辑在 [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L22-L39)：

```python
@router.get("/shared/{token_id}", response_model=Recipe)
def get_shared_recipe(token_id: UUID4, session: Session = Depends(generate_session)) -> Recipe:
    db = get_repositories(session, group_id=None, household_id=None)

    token_summary = db.recipe_share_tokens.get_one(token_id)
    if token_summary and token_summary.is_expired:
        try:
            db.recipe_share_tokens.delete(token_id)
            session.commit()
        except Exception:
            session.rollback()
        token_summary = None

    if token_summary is None:
        raise HTTPException(status_code=404, detail=ErrorResponse.respond("Token Not Found"))

    return token_summary.recipe
```

**关键安全设计**：
1. 路由使用普通 `APIRouter()`，无任何认证依赖
2. Repository 初始化时 `group_id=None, household_id=None`，不做组过滤
3. 访问时**惰性清理过期 Token**：如果 Token 存在但已过期，先删除再返回 404
4. `is_expired` 是 Pydantic Schema 的 `@property`（不是 DB 字段），定义于 [schema/recipe/recipe_share_token.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_share_token.py#L21-L23)：
   ```python
   @property
   def is_expired(self) -> bool:
       return self.expires_at < datetime.now(UTC)
   ```

### 3.4 过期 Token 定期清理

后台定时任务在 [services/scheduler/tasks/purge_expired_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/services/scheduler/tasks/purge_expired_share_tokens.py)：
- 由 `SchedulerRegistry.register_daily` 每天触发一次
- 遍历所有 `expiresAt < current_time` 的 Token 批量删除

双重过期防护：访问时惰性删除 + 定时任务批量清理。

---

## 四、完整 Recipe 响应中的身份归属字段暴露

### 4.1 MealieModel 的全局序列化规则

所有 Recipe Schema 均继承自 `MealieModel`，其全局配置在 [schema/_mealie/mealie_model.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/_mealie/mealie_model.py#L45-L53)：

```python
class MealieModel(BaseModel):
    model_config = ConfigDict(alias_generator=camelize, populate_by_name=True)
```

`alias_generator=camelize`（来自 `humps.main.camelize`）意味着**所有字段在序列化输出时自动转为 camelCase 别名**。

### 4.2 RecipeSummary 中明确定义了身份归属字段

[schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe.py#L116-L136)：

```python
class RecipeSummary(MealieModel):
    id: UUID4 | None = None

    user_id: Annotated[UUID4, Field(default_factory=uuid4, validate_default=True)]
    household_id: Annotated[UUID4, Field(default_factory=uuid4, validate_default=True)]
    group_id: Annotated[UUID4, Field(default_factory=uuid4, validate_default=True)]
    ...
```

由于 `Recipe` 继承自 `RecipeSummary`，这些字段在 Share Token 返回的完整 Recipe JSON 中**会被暴露**，输出形式为 camelCase：

| Python 字段名 | JSON 输出字段名 | 含义 |
|--------------|----------------|------|
| `user_id` | `userId` | Recipe 创建者的 User UUID |
| `household_id` | `householdId` | Recipe 所属的 Household UUID |
| `group_id` | `groupId` | Recipe 所属的 Group UUID |

### 4.3 暴露风险评估

**这三个身份归属字段在公开访问接口中确实会泄露**：
- `/api/recipes/shared/{token_id}`（Share Token 匿名访问）
- `/api/explore/groups/{group_slug}/recipes/{slug}`（Explore 公开浏览）

泄露内容：Recipe 的创建者 UUID、所在 Household UUID、所在 Group UUID。由于 UUID 本身不可枚举，攻击者无法通过这些 UUID 反推出更多信息，但结合其他信息（如泄露的用户列表）可能用于关联分析。

### 4.4 其他隐式保护的内部字段

以下字段**未在 Schema 中声明**，因此不会出现在输出中：
- `name_normalized`、`description_normalized`（SQL 索引辅助字段）
- ORM 内部属性（`_sa_instance_state` 等由 `from_attributes=True` 自动忽略）

---

## 五、SPA Meta 注入与过期 Token 边界差异

### 5.1 SPA Share Token 页面的路由注册

SPA 页面路由在 [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L246-L256) 中注册：

```python
def mount_spa(app: FastAPI):
    ...
    app.get("/g/{group_slug}/r/{recipe_slug}", include_in_schema=False)(serve_recipe_with_meta)
    app.get("/g/{group_slug}/shared/r/{token_id}", include_in_schema=False)(serve_shared_recipe_with_meta)
```

### 5.2 API 路由 vs SPA Meta 的过期检查差异

**匿名 API 路由**（[routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L26-L38)）：
```python
token_summary = db.recipe_share_tokens.get_one(token_id)
if token_summary and token_summary.is_expired:    # ✅ 检查过期
    try:
        db.recipe_share_tokens.delete(token_id)    # ✅ 惰性删除
        session.commit()
    except Exception:
        session.rollback()
    token_summary = None

if token_summary is None:
    raise HTTPException(status_code=404, ...)      # ✅ 过期返回 404
```

**SPA Meta 注入**（[routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L233-L243)）：
```python
async def serve_shared_recipe_with_meta(group_slug: str, token_id: str, session: Session = Depends(generate_session)):
    try:
        public_repos = AllRepositories(session, group_id=None)
        token_summary = public_repos.recipe_share_tokens.get_one(token_id)
        if token_summary is None:                    # ❌ 只检查 Token 是否存在
            raise Exception("Token Not Found")
                                                       # ❌ 未检查 is_expired
        return Response(content_with_meta(group_slug, token_summary.recipe), media_type="text/html")
    except Exception:
        return response_404()
```

### 5.3 差异导致的边界行为

| Token 状态 | `/api/recipes/shared/{token_id}` | `/g/{group_slug}/shared/r/{token_id}` |
|-----------|---------------------------------|--------------------------------------|
| 不存在 | 返回 404 JSON | 返回 404 HTML |
| 存在且未过期 | 返回完整 Recipe JSON | 返回带 OG Meta 的 HTML |
| 存在但已过期（未清理） | 删除 Token + 返回 404 JSON | **仍返回带 OG Meta 的 HTML** |
| 存在但已过期（已清理） | 返回 404 JSON | 返回 404 HTML |

**边界结论**：过期 Token 在被惰性删除或定时清理之前，仍可通过 SPA 页面获取到 Recipe 的 OG Meta 信息（title、description、image、JSON-LD），但无法通过 API 获取完整的 Recipe JSON 数据。

---

## 六、媒体资源访问边界

### 6.1 Media Router 独立挂载

媒体路由完全独立于主 API Router 挂载，见 [app.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/app.py#L147-L150)：
```python
def api_routers():
    app.include_router(router)        # 主 API
    app.include_router(media_router)  # 媒体路由独立注册
    ...
```

`media_router` 定义于 [routes/media/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/__init__.py#L8-L11)：
```python
media_router = APIRouter(prefix="/api/media", tags=["Recipe: Images and Assets"])
media_router.include_router(media_recipe.router)
media_router.include_router(media_user.router)
```

### 6.2 Recipe 媒体（图片/附件/时间线图片）

[routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py) 定义了三个端点：

| 端点 | 用途 | 认证 | Recipe 公开性校验 |
|-----|------|------|------------------|
| `GET /api/media/recipes/{recipe_id}/images/{file_name}` | Recipe 图片 | ❌ 无 | ❌ 无 |
| `GET /api/media/recipes/{recipe_id}/images/timeline/{timeline_event_id}/{file_name}` | 时间线事件图片 | ❌ 无 | ❌ 无 |
| `GET /api/media/recipes/{recipe_id}/assets/{file_name}` | Recipe 附件 | ❌ 无 | ❌ 无 |

**安全措施仅有**：
- 附件下载的路径穿越防护（`is_relative_to` 校验）
- 附件响应头：`X-Content-Type-Options: nosniff` + `content_disposition_type="attachment"`

**完全没有的校验**：
- Recipe 是否属于 public group / public household / settings.public
- Share Token 是否有效
- 用户是否登录

### 6.3 User 头像

[routes/media/media_user.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_user.py#L10-L23)：

```python
@router.get("/{user_id}/{file_name}", response_class=FileResponse)
async def get_user_image(user_id: UUID4, file_name: str):
    user_dir = PrivateUser.get_directory(user_id)
    recipe_image = (user_dir / file_name).resolve()
    if not recipe_image.is_relative_to(user_dir.resolve()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    ...
```

端点：`GET /api/media/users/{user_id}/{file_name}`
- 完全无认证，只有路径穿越防护
- 只要知道 User UUID 和文件名，即可获取用户头像

### 6.4 Docker 生产环境的特殊说明

代码注释提到：
> This route is proxied in the docker image and should not hit the API in production

即 Docker 部署时，Nginx 会直接代理 `/api/media/*` 请求，从静态文件目录直接返回，不经过 Python/FastAPI。但在开发、测试及非 Docker 部署环境下，这些路由由 FastAPI 处理，上述无认证情况真实存在。

---

## 七、匿名入口汇总

所有完全无认证的公开路由（使用默认 `APIRouter`，无全局依赖）：

| 路由 | 文件 | 说明 |
|-----|------|------|
| `GET /api/recipes/shared/{token_id}` | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L22) | Share Token 获取 Recipe JSON |
| `GET /api/recipes/shared/{token_id}/zip` | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L42) | Share Token 下载 Recipe Zip |
| `GET /api/explore/groups/{group_slug}/...` | [routes/explore/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/__init__.py) | 公开浏览 |
| `GET /api/media/recipes/{recipe_id}/images/...` | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py#L19) | Recipe 图片（含时间线图片） |
| `GET /api/media/recipes/{recipe_id}/assets/...` | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py#L51) | Recipe 附件 |
| `GET /api/media/users/{user_id}/{file_name}` | [routes/media/media_user.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_user.py#L10) | 用户头像 |
| `GET /g/{group_slug}/r/{recipe_slug}` | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L253) | 公开 Recipe SPA 页面（Meta 注入） |
| `GET /g/{group_slug}/shared/r/{token_id}` | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L254) | Share Token SPA 页面（Meta 注入，不检查过期） |
| `POST /api/auth/token`、`/api/auth/oauth` 等 | [routes/auth/auth.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/auth/auth.py) | 认证相关 |

三种 Router 认证级别定义于 [routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py)：
- `AdminAPIRouter`：全局依赖 `get_admin_user`
- `UserAPIRouter`：全局依赖 `get_current_user`
- 默认 `APIRouter`：完全公开，无认证依赖

---

## 八、Recipe Scope 权限控制

### 8.1 Repository 层 Scope 过滤

`RepositoryGeneric._filter_builder()` 在 [repos/repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_generic.py#L94-L102) 自动附加过滤：

```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

每个 Repository 实例绑定固定的 `group_id` / `household_id`，所有查询自动附加过滤。

### 8.2 Explore 公开 Recipe 的四层防护链

```
匿名请求 GET /api/explore/groups/{group_slug}/recipes/{slug}
    ↓
1. get_public_group 依赖 → group.preferences.private_group=False
    ↓
2. Repository 级自动过滤 group_id（来自已校验的 group）
    ↓
3. 业务层校验 recipe.settings.public=True
    ↓
4. 业务层校验 household.preferences.private_household=False
```

代码见 [routes/explore/controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/controller_public_recipes.py#L114-L125)。

---

## 九、只读字段裁剪机制

### 9.1 Pydantic Schema 分级

| Schema | 字段范围 | 使用场景 |
|--------|---------|---------|
| `RecipeSummary` | 基础元数据 + userId/groupId/householdId + 关联标签/categories 等摘要 | 列表分页、搜索结果 |
| `Recipe` | 完整 Recipe（含 ingredients/instructions/settings/assets/notes/comments/extras） | 详情页、Share Token 返回 |
| `RecipeShareTokenSummary` | id/recipeId/groupId/expiresAt/createdAt | 管理端 Token 列表（不含 recipe 对象） |
| `RecipeShareToken` | Token 基础字段 + 完整 Recipe 对象 | 管理端 Token 详情 |

裁剪机制：FastAPI 的 `response_model` 通过 Pydantic 序列化，不在 Schema 定义中的字段自动丢弃。

### 9.2 身份归属字段的裁剪现状

如第五节所述，`userId`/`groupId`/`householdId` 在 Schema 中明确定义，因此**不会被裁剪**，会出现在所有 Recipe 相关公开响应中。

---

## 十、撤销分享后的缓存边界

### 10.1 应用层（DB 查询）：无缓存

Share Token 查询不经过任何内存/Redis 缓存，每次请求直查数据库。Token 被删除后下一次请求立即返回 404。

### 10.2 MealieCrudRoute 的缓存头：有条件触发

`MealieCrudRoute` 是自定义 `APIRoute` 子类，定义于 [routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py#L27-L52)，其缓存头设置逻辑为：

```python
async def custom_route_handler(request: Request) -> Response:
    response = await original_route_handler(request)
    response_body = json.loads(response.body)

    if isinstance(response_body, dict):               # 条件 1：响应体必须是 dict
        if last_modified := response_body.get("updatedAt"):  # 条件 2：必须有 updatedAt 字段
            response.headers["last-modified"] = last_modified
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

    return response
```

**两个条件必须同时满足**才会设置 `Cache-Control` 头：
1. 响应 JSON body 必须是 `dict` 类型（list 类型不触发，如分页列表）
2. 该 dict 中必须包含 `"updatedAt"` 键（camelCase）

### 10.3 各路由缓存头现状全景

显式使用 `route_class=MealieCrudRoute` 的路由（grep 结果）：

| 路由 | 文件 | route_class | 响应类型 | 是否触发 Cache-Control |
|-----|------|------------|---------|----------------------|
| `/api/units` | [unit_and_foods/units.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/unit_and_foods/units.py#L21) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |
| `/api/foods` | [unit_and_foods/foods.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/unit_and_foods/foods.py#L21) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |
| `/api/recipes`（用户 CRUD） | [recipe/recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/recipe_crud_routes.py#L85) | MealieCrudRoute | dict/list | 单条 Recipe dict ✅，列表 ❌ |
| `/api/recipes/timeline/events` | [recipe/timeline_events.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/timeline_events.py#L26) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |
| `/api/households/events/notifications` | [controller_group_notifications.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/households/controller_group_notifications.py#L32) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |
| `/api/households/cookbooks` | [controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/households/controller_cookbooks.py#L24) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |
| `/api/groups/labels` | [controller_labels.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/groups/controller_labels.py#L21) | MealieCrudRoute | dict/list | 单条 dict ✅，列表 ❌ |

**公开/匿名路由均未使用 MealieCrudRoute**，因此全部没有 `Cache-Control` 头：

| 公开/匿名路由 | Router 类型 | route_class | Cache-Control |
|-------------|------------|-------------|---------------|
| `/api/recipes/shared/{token_id}` | 默认 `APIRouter` | 默认 | ❌ 无 |
| `/api/recipes/shared/{token_id}/zip` | 默认 `APIRouter` | 默认 | ❌ 无（StreamingResponse 无 body） |
| `/api/explore/groups/{group_slug}/...` | 默认 `APIRouter` | 默认 | ❌ 无 |
| `/api/shared/recipes`（用户管理 Token） | `UserAPIRouter` | 默认 | ❌ 无 |
| `/api/media/recipes/...`、`/api/media/users/...` | 默认 `APIRouter` | 默认 | ❌ 无（FileResponse 无 JSON body） |

### 10.4 动态分享页面（SPA Meta 注入）的缓存头

以下三条路由通过 `app.get()` 直接注册，返回 `Response(..., media_type="text/html")`：

| 路由 | 注册位置 | Cache-Control |
|-----|---------|---------------|
| `GET /g/{group_slug}/r/{recipe_slug}` | [spa/__init__.py#L253](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L253) | ❌ 未设置 |
| `GET /g/{group_slug}/shared/r/{token_id}` | [spa/__init__.py#L254](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L254) | ❌ 未设置 |
| `response_404()` 返回 | [spa/__init__.py#L183-L184](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L183-L184) | ❌ 未设置 |

代码证据：
```python
# 均返回纯 Response 对象，未设置任何缓存头
return Response(content_with_meta(group_slug, recipe), media_type="text/html")
return Response(__contents, media_type="text/html", status_code=404)
```

这些路由由 FastAPI 路由表优先匹配，**完全不走 `SPAStaticFiles`**。

### 10.5 静态 SPA fallback 的 no-cache 规则

`SPAStaticFiles`（继承自 `StaticFiles`）的缓存策略仅对走静态文件服务的请求生效，见 [spa/__init__.py#L34-L52](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L34-L52)：

```python
class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except HTTPException as ex:
            if ex.status_code == 404:
                response = await super().get_response("index.html", scope)  # fallback
            else:
                raise ex

        if path.startswith("_nuxt/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "." or response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"

        return response
```

**生效路径**（仅当无显式路由匹配时，请求落到 `app.mount("/", SPAStaticFiles(...))`）：

| 请求路径 | 返回内容 | Cache-Control |
|---------|---------|---------------|
| `/_nuxt/abc123.js` | 静态构建产物 | `public, max-age=31536000, immutable` |
| `/`（path == "."） | index.html | `no-cache` |
| `/foo/bar`（无路由匹配） | index.html fallback（media_type == "text/html"） | `no-cache` |

**不生效路径**：
- 显式注册的 `app.get("/g/{group_slug}/shared/r/{token_id}")`
- 显式注册的 `app.get("/g/{group_slug}/r/{recipe_slug}")`
- 所有 API 路由

### 10.6 图片版本化缓存

Recipe 图片 URL 格式：`/api/media/recipes/{recipe_id}/images/original.webp?version={recipe.image}`
- `recipe.image` 是 0-255 随机整数，每次调用 `update_image` 时重新生成（见 [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_recipes.py#L155-L159)）
- 通过 URL query 参数变化实现浏览器缓存自动失效

### 10.7 缓存边界总表

| 路由/资源 | 缓存策略 | 撤销分享后失效时机 |
|----------|---------|-----------------|
| DB 查询 | 无缓存 | 立即 |
| **Share Token 匿名 API** (`/api/recipes/shared/{id}`) | ❌ 无 Cache-Control | 取决于浏览器/CDN 默认策略 |
| **Explore 公开浏览** (`/api/explore/...`) | ❌ 无 Cache-Control | 取决于浏览器/CDN 默认策略 |
| **Share Token 用户管理** (`/api/shared/recipes`) | ❌ 无 Cache-Control（UserAPIRouter 无 MealieCrudRoute） | 取决于浏览器/CDN 默认策略 |
| **用户 Recipe CRUD 单条** (`/api/recipes/{slug}` GET) | ✅ `no-cache, no-store`（MealieCrudRoute + dict + updatedAt） | 立即 |
| **用户 Recipe CRUD 列表** (`/api/recipes` 分页) | ❌ 无 Cache-Control（list 类型不触发 MealieCrudRoute） | 取决于浏览器/CDN 默认策略 |
| **SPA Meta 注入页面**（`/g/.../shared/r/...` 等显式路由） | ❌ 无 Cache-Control | 取决于浏览器/CDN 默认策略 |
| **SPA 404 页面** (`response_404()`) | ❌ 无 Cache-Control | 取决于浏览器/CDN 默认策略 |
| **SPA fallback HTML**（无显式路由匹配时） | ✅ `no-cache`（SPAStaticFiles） | 立即 |
| **SPA _nuxt/* 静态资源** | ✅ 永久缓存（hash 文件名） | 重新发布构建后 |
| **Recipe 图片** | URL 带 `?version=` 参数 | 立即（新 URL 不走旧缓存） |
| **Recipe 附件、时间线图片** | ❌ 无特殊缓存头 | 取决于浏览器默认策略 |
| **User 头像** | ❌ 无特殊缓存头 | 取决于浏览器默认策略 |

### 10.8 差异对撤销分享后可见内容的实际影响

| 撤销/变更操作 | 受影响资源 | 缓存行为 | 用户可能看到旧内容的窗口 |
|-------------|-----------|---------|----------------------|
| **用户删除 Share Token** | `/api/recipes/shared/{id}` | 无缓存头 | 浏览器/CDN 默认缓存 TTL（通常几分钟到几小时） |
| **用户删除 Share Token** | `/g/{group_slug}/shared/r/{id}` Meta | 无缓存头 | 浏览器/CDN 默认缓存 TTL |
| **Token 过期被惰性删除** | `/api/recipes/shared/{id}` | 无缓存头 + 立即删 DB | 仅浏览器端缓存（若有） |
| **Recipe 由 public 改为 private** | `/api/explore/.../recipes/{slug}` | 无缓存头 | 浏览器/CDN 默认缓存 TTL |
| **Household 改为 private** | Explore 列表 | 无缓存头 | 浏览器/CDN 默认缓存 TTL |
| **Recipe 被删除** | 级联清理 Token → API 404 | 无缓存头 | 浏览器/CDN 默认缓存 TTL |
| **用户 CRUD 单条 Recipe** | `/api/recipes/{slug}` | `no-cache, no-store` | 立即 |
| **用户 CRUD 分页列表** | `/api/recipes` 列表 | 无缓存头（list 不触发） | 浏览器/CDN 默认缓存 TTL |
| **用户列出自己的 Share Token** | `/api/shared/recipes` | 无缓存头 | 浏览器/CDN 默认缓存 TTL |
| **Recipe 图片更新** | `/api/media/recipes/{id}/images/original.webp` | `?version=` 变更 | 立即（新 URL） |
| **Recipe 附件更新** | `/api/media/recipes/{id}/assets/{file}` | 无缓存头 + 同文件名 | 取决于浏览器默认策略（可能有 ETag/Last-Modified） |

**结论**：绝大多数公开/匿名接口以及用户侧列表接口都**没有**显式 `Cache-Control` 头，撤销分享后的可见内容受浏览器和中间 CDN 的默认缓存策略影响。只有通过 `MealieCrudRoute` 且满足条件（dict + updatedAt）的单条对象查询接口，以及 SPA fallback HTML，才保证不缓存。

---

## 十一、防越权综合分析与已知边界

### 11.1 Share Token 链路

```
匿名请求 GET /api/recipes/shared/{token_id}
    ↓
无认证依赖（默认 APIRouter）
    ↓
Repository: group_id=None, household_id=None
    ↓
按主键 UUID 查询 RecipeShareTokenModel
    ↓
校验 is_expired → 过期则删除 DB 记录 + 返回 404
    ↓
ORM relationship 加载关联 Recipe 对象
    ↓
Pydantic Recipe Schema 序列化（输出含 userId/groupId/householdId）
```

### 11.2 已知安全边界

1. **身份归属字段泄露**：公开 Recipe 响应包含 `userId`、`groupId`、`householdId`（camelCase 形式），Share Token 匿名访问和 Explore 公开浏览均会输出
2. **Media 路由完全无校验**：Recipe 图片/附件/时间线图片、用户头像均无认证，也不校验 Recipe 公开性或 Share Token 有效性，仅靠 UUID 不可猜测保护
3. **绝大多数公开接口无 Cache-Control**：不仅 Share Token 匿名 API，Explore 公开浏览、Share Token 用户管理列表（`/api/shared/recipes`）、SPA Meta 注入页面（`/g/.../shared/r/...`）、用户 Recipe 分页列表等均未设置显式缓存头，撤销分享后浏览器/CDN 可能返回旧内容；仅 MealieCrudRoute + dict 响应 + 含 updatedAt 的单条查询以及 SPA fallback HTML 保证不缓存
4. **SPA Meta 不检查 Token 过期**：过期 Token 在被惰性删除或定时清理前，仍可通过 `/g/{group_slug}/shared/r/{token_id}` 获取 OG Meta（title/description/image/JSON-LD）
5. **Share Token 无速率限制**：理论可暴力枚举（UUID 空间足够大，实际不可行）

### 11.3 有效防护措施

- ✅ Repository Scope 过滤：强制附加 group_id/household_id，无法通过参数绕过
- ✅ 创建 Token 时校验 recipe.group_id == user.group_id
- ✅ Explore 四层防护链（Group → Repository → Recipe public → Household non-private）
- ✅ 级联删除：删除 Recipe 自动清理关联 Token
- ✅ 双重过期清理：访问时惰性删除 + 每日定时任务

---

## 十二、代码位置索引

| 关注点 | 文件路径 |
|-------|---------|
| 主路由挂载（app.py） | [app.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/app.py) |
| 主 API 子路由注册 | [routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/__init__.py) |
| Recipe 子路由注册（含 shared_routes） | [routes/recipe/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/__init__.py) |
| Share Token 匿名消费 | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py) |
| Share Token 用户管理 | [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py) |
| Share Token 数据模型 | [db/models/recipe/shared.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/recipe/shared.py) |
| Share Token Schema（含 is_expired） | [schema/recipe/recipe_share_token.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_share_token.py) |
| MealieModel 全局配置（alias_generator=camelize） | [schema/_mealie/mealie_model.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/_mealie/mealie_model.py) |
| Recipe/RecipeSummary Schema（含 userId 等字段） | [schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe.py) |
| Router 认证级别 | [routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py) |
| Controller 基类与 Scope | [routes/_base/base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/base_controllers.py) |
| Repository Scope 过滤 | [repos/repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_generic.py) |
| Explore 公开 Recipes | [routes/explore/controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/controller_public_recipes.py) |
| Media Router 注册 | [routes/media/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/__init__.py) |
| Recipe 媒体路由（图片/附件） | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py) |
| User 头像路由 | [routes/media/media_user.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_user.py) |
| SPA Meta 注入（含 serve_shared_recipe_with_meta） | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py) |
| 过期 Token 定时清理 | [services/scheduler/tasks/purge_expired_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/services/scheduler/tasks/purge_expired_share_tokens.py) |
| 认证依赖函数 | [core/dependencies/dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/core/dependencies/dependencies.py) |
| 前端共享 API 客户端 | [frontend/app/lib/api/public/shared.ts](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/frontend/app/lib/api/public/shared.ts) |
| 测试路由常量 | [tests/utils/api_routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/utils/api_routes/__init__.py) |
| Share Token 集成测试 | [tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py) |
