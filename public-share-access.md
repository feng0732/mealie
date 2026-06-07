# Public Share 与访问控制代码实现分析

## 一、整体架构概览

Mealie 的公开访问体系包含两条独立路径：

| 访问模式 | 入口路径 | 认证方式 | 权限边界 |
|---------|---------|---------|---------|
| 链接分享（Share Token） | `/api/shared/{token_id}` | 无认证，凭 Token UUID 访问 | Token 有效期 + 所属 Recipe |
| 公开浏览（Explore） | `/api/explore/groups/{group_slug}/...` | 无认证 | Group 非私有 + Household 非私有 + Recipe public=true |
| 媒体资源 | `/api/media/recipes/{recipe_id}/...` | 无认证 | 仅通过路径可达性（存在即返回） |

路由注册总入口在 [routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/__init__.py)，所有子路由统一挂载到 `/api` 前缀下。

---

## 二、Share Token 机制详解

### 2.1 数据模型

Token 模型定义于 [db/models/recipe/shared.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/recipe/shared.py#L21-L34)：

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    group_id: FilterableColumn[GUID]  # 外键关联 groups
    recipe_id: FilterableColumn[GUID]  # 外键关联 recipes
    recipe: Mapped["RecipeModel"] = relationship("RecipeModel", back_populates="share_tokens")
    expires_at: FilterableColumn[datetime]  # 默认当前时间 + 30 天
```

关键设计：
- **UUID 作为主键**：Token 值本身就是数据库主键，无需额外签名/加密，安全性依赖于 UUID 的不可猜测性
- **级联删除**：`RecipeModel.share_tokens` 配置了 `cascade="all, delete, delete-orphan"`，删除 Recipe 时关联 Token 自动清除
- **过期时间默认 30 天**：由 `defaut_expires_at_time()` 函数统一设置

### 2.2 Token 创建（用户侧）

创建逻辑在 [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py#L33-L42)：

```python
@router.post("", response_model=RecipeShareToken, status_code=201)
def create_one(self, data: RecipeShareTokenCreate) -> RecipeShareToken:
    group_repos = get_repositories(self.repos.session, group_id=self.group_id, household_id=None)
    recipe = group_repos.recipes.get_one(data.recipe_id, "id")
    if recipe is None or recipe.group_id != self.group_id:
        raise HTTPException(status_code=404, detail="Recipe not found in your group")

    save_data = RecipeShareTokenSave(**data.model_dump(), group_id=self.group_id)
    return self.mixins.create_one(save_data)
```

**越权防护点**：
1. 路由使用 `UserAPIRouter`，必须登录认证
2. 创建前校验 Recipe 的 `group_id` 必须与当前用户的 `group_id` 一致
3. 跨 group 创建直接返回 404（测试验证见 [test_recipe_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py#L116-L122)）
4. 同 group 不同 household 允许创建（同一 group 内共享）

### 2.3 Token 消费（匿名侧）

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
1. 路由使用普通 `APIRouter()`，**无任何认证依赖**，完全匿名可访问
2. Repository 初始化时 `group_id=None, household_id=None`，不做组过滤
3. 访问时**惰性清理过期 Token**：如果 Token 存在但已过期，先删除再返回 404
4. Token 本身通过 `group_id` 与 Recipe 绑定，Repository 的 `_filter_builder` 对 `recipe_share_tokens` 虽然会附加 group_id 过滤，但这里 `group_id=None`，所以直接按主键查即可
5. 返回值是完整的 `Recipe` Schema（Pydantic 负责序列化）

### 2.4 Token 管理（列表/删除）

Token 管理接口在 [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py)：
- `GET /api/shared/recipes`：列出当前用户 group 下所有 Share Token（可按 recipe_id 过滤）
- `GET /api/shared/recipes/{item_id}`：获取单个 Token 详情
- `DELETE /api/shared/recipes/{item_id}`：撤销分享

这些接口均使用 `UserAPIRouter`，且 Repository 会附带 `group_id=self.user.group_id`，天然保证用户只能操作自己 group 内的 Token。

### 2.5 过期 Token 定期清理

后台定时任务在 [purge_expired_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/services/scheduler/tasks/purge_expired_share_tokens.py)：

```python
def purge_expired_tokens() -> None:
    with session_context() as session:
        db = get_repositories(session, group_id=None)
        tokens_response = db.recipe_share_tokens.page_all(
            PaginationQuery(page=1, per_page=-1, query_filter=f"expiresAt < {current_time}")
        )
        db.recipe_share_tokens.delete_many([token.id for token in tokens])
```

双重过期防护：访问时惰性删除 + 定时任务批量清理。

---

## 三、匿名访问入口与路由层级设计

### 3.1 三种 Router 的认证级别

定义于 [routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py)：

```python
class AdminAPIRouter(APIRouter):
    def __init__(self, ...):
        super().__init__(..., dependencies=[Depends(get_admin_user)])

class UserAPIRouter(APIRouter):
    def __init__(self, ...):
        super().__init__(..., dependencies=[Depends(get_current_user)])

# 普通 APIRouter = 完全公开，无认证依赖
```

| Router 类型 | 全局依赖 | 使用场景 |
|------------|---------|---------|
| `AdminAPIRouter` | `get_admin_user` | 系统管理接口 |
| `UserAPIRouter` | `get_current_user` | 登录用户操作 |
| `APIRouter`（默认） | 无 | 公开访问、登录/注册、share token 访问、explore |

### 3.2 匿名入口清单

完全无认证的公开路由（使用默认 `APIRouter`）：

| 路由 | 文件 | 用途 |
|-----|------|------|
| `/api/shared/{token_id}` | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L22) | Share Token 获取 Recipe |
| `/api/shared/{token_id}/zip` | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py#L42) | Share Token 下载 Recipe Zip |
| `/api/explore/groups/{group_slug}/...` | [routes/explore/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/__init__.py) | 公开浏览 recipes/cookbooks/tags 等 |
| `/api/media/recipes/{recipe_id}/images/...` | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py#L19) | Recipe 图片 |
| `/api/media/recipes/{recipe_id}/assets/...` | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py#L51) | Recipe 附件 |
| `/api/auth/token`、`/api/auth/oauth` 等 | [routes/auth/auth.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/auth/auth.py#L25) | 认证相关 |
| `/g/{group_slug}/r/{recipe_slug}` (SPA) | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L253) | 公开 Recipe 页面 Meta 注入 |
| `/g/{group_slug}/shared/r/{token_id}` (SPA) | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L254) | Share Token 页面 Meta 注入 |

### 3.3 认证依赖详解

核心认证函数在 [core/dependencies/dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/core/dependencies/dependencies.py)：

```python
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
oauth2_scheme_soft_fail = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)

async def get_current_user(request: Request, token: str | None = Depends(oauth2_scheme_soft_fail), ...) -> PrivateUser:
    if token is None and "mealie.access_token" in request.cookies:
        token = request.cookies.get("mealie.access_token", "")
    # JWT 解码 → 校验 sub(user_id) → 查询用户
    payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
    ...
```

注意：Share Token 路由完全不经过 `get_current_user`，无论请求带不带 Authorization header，都不会触发认证检查。

---

## 四、Recipe Scope 权限控制（数据访问层隔离）

### 4.1 Repository 层的 Scope 过滤

所有数据库访问通过 `AllRepositories` 工厂统一创建，定义于 [repos/repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_factory.py#L113-L122)：

```python
class AllRepositories:
    def __init__(self, session, *, group_id=NOT_SET, household_id=NOT_SET):
        self.group_id = group_id
        self.household_id = household_id
```

过滤逻辑在 `RepositoryGeneric._filter_builder()`，见 [repos/repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_generic.py#L94-L102)：

```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

**核心机制**：每个 Repository 实例绑定固定的 `group_id` / `household_id`，所有查询（`get_one`、`multi_query`、`page_all` 等）都会自动附加这些过滤条件。攻击者即便能调用接口，也无法通过构造参数绕过这一层。

### 4.2 Controller 基类决定 Scope

不同 Controller 基类决定了 Repository 的 Scope：

| Controller 基类 | group_id | household_id | 说明 |
|----------------|----------|-------------|------|
| `BaseUserController` | `self.user.group_id` | `self.user.household_id` | 普通用户，仅自己组+家庭 |
| `BaseAdminController` | `None` | `None` | 管理员，无过滤 |
| `BasePublicGroupExploreController` | `self.group.id`（经 `get_public_group` 校验） | `NOT_SET` | 公开浏览，先校验 group 非私有 |
| `BasePublicHouseholdExploreController` | `self.group.id` | `None` | 公开浏览 Household 级别数据，需**额外**在业务层过滤 |

定义见 [routes/_base/base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/base_controllers.py)。

### 4.3 Public Group 的前置校验

`get_public_group` 依赖确保了只有非私有 group 才能被 explore 访问，见 [core/dependencies/dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/core/dependencies/dependencies.py#L67-L74)：

```python
async def get_public_group(group_slug: str = fastapi.Path(...), session=Depends(generate_session)) -> GroupInDB:
    repos = get_repositories(session)
    group = repos.groups.get_by_slug_or_id(group_slug)
    if not group or group.preferences.private_group:
        raise HTTPException(404, "group not found")
    return group
```

### 4.4 Explore 公开 Recipe 的多层过滤

公开 Recipe 列表在 [routes/explore/controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/controller_public_recipes.py#L30-L92) 中叠加了三层过滤：

```python
# 第 1 层：Repository 自动附加 group_id（来自 get_public_group）
# 第 2 层：业务层强制追加 public_filter
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
if q.query_filter:
    q.query_filter = f"({q.query_filter}) AND {public_filter}"
else:
    q.query_filter = public_filter

# 第 3 层（Cookbook 场景）：校验 Cookbook 本身 public 且所在 Household 非私有
if cookbook_data is None or not cookbook_data.public:
    raise COOKBOOK_NOT_FOUND_EXCEPTION
household = self.repos.households.get_one(cookbook_data.household_id)
if not household or household.preferences.private_household:
    raise COOKBOOK_NOT_FOUND_EXCEPTION
```

单条 Recipe 获取同样的检查（[controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/controller_public_recipes.py#L114-L125)）：

```python
@router.get("/{recipe_slug}", response_model=Recipe)
def get_recipe(self, recipe_slug: str) -> Recipe:
    recipe = self.cross_household_recipes.get_one(recipe_slug)
    if not recipe or not recipe.settings.public:
        raise RECIPE_NOT_FOUND_EXCEPTION
    household = self.repos.households.get_one(recipe.household_id)
    if not household or household.preferences.private_household:
        raise RECIPE_NOT_FOUND_EXCEPTION
    return recipe
```

**四层防护链**：
1. Router 级：`get_public_group` 校验 group 非私有
2. Repository 级：自动过滤 `group_id`
3. 业务级：强制 `settings.public = TRUE`
4. 业务级：强制 `household.preferences.private_household = FALSE`

### 4.5 私有标志数据模型

- Group 私有标志：[GroupPreferencesModel.private_group](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/group/preferences.py#L22)，默认 `True`
- Household 私有标志：[HouseholdPreferencesModel.private_household](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/household/preferences.py#L26)，默认 `True`
- Recipe 公开标志：[RecipeSettings.public](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/recipe/settings.py#L12)，默认由 Household 偏好决定

---

## 五、只读字段裁剪与响应序列化

### 5.1 Pydantic Schema 分级裁剪

Mealie 通过定义不同粒度的 Pydantic Schema 来控制字段暴露：

| Schema | 定义位置 | 字段范围 | 典型使用场景 |
|--------|---------|---------|-------------|
| `RecipeSummary` | [schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe.py#L116-L175) | 基础元数据（不含 ingredients/instructions/notes/assets/comments 等） | 列表分页、搜索结果 |
| `Recipe` | [schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe.py#L182-L320) | 完整 Recipe（含 ingredients/instructions/settings/assets/notes/comments/extras） | 详情页、Share Token 返回 |
| `RecipeShareTokenSummary` | [schema/recipe/recipe_share_token.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_share_token.py#L30-L33) | Token 基础信息（不含 recipe 对象） | 管理端 Token 列表 |
| `RecipeShareToken` | [schema/recipe/recipe_share_token.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_share_token.py#L36-L61) | Token + 完整 Recipe | 管理端 Token 详情 |

**裁剪机制**：FastAPI 的 `response_model` 参数会在返回时通过 Pydantic 的 `model_validate` 进行序列化，不在 Schema 定义中的字段会被自动丢弃。

例如 Share Token 列表接口使用 `response_model=list[RecipeShareTokenSummary]`，即便 Repository 返回了完整的 Token 关联数据，Pydantic 也只会输出 `id`/`recipe_id`/`group_id`/`expires_at`/`created_at` 这些摘要字段。

### 5.2 敏感字段隐式保护

当前 `Recipe` Schema 中**不包含**以下敏感内部字段：
- `user_id`、`household_id`、`group_id`（虽然在 `RecipeSummary` 中定义了，但这是设计上的选择，用于前端逻辑判断）
- 数据库内部字段（`_sa_instance_state` 等由 `from_attributes=True` 自动忽略）
- `name_normalized`、`description_normalized`（SQL 索引辅助字段，未在 Schema 中声明）

### 5.3 Media 路由的特殊处理

图片和附件路由在 [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py)：

```python
# 附件下载：防止路径穿越
file = asset_dir.joinpath(file_name).resolve()
if not file.is_relative_to(asset_dir.resolve()):
    raise HTTPException(status.HTTP_400_BAD_REQUEST)

# 强制下载 + MIME Sniffing 防护
return FileResponse(
    file,
    filename=file.name,
    content_disposition_type="attachment",
    headers={"X-Content-Type-Options": "nosniff"},
)
```

**Media 路由的安全风险点**：该路由完全无认证，也不校验 Recipe 是否 public/是否属于有效 Share Token。只要知道 `recipe_id`（UUID）和文件名，即可下载图片和附件。这是一个"不可知性安全"设计，依赖于 UUID 的不可猜测性。

---

## 六、撤销分享后的缓存边界

### 6.1 Share Token 无应用层缓存

Share Token 的查询**不经过任何内存/Redis 缓存**。每次访问 `/api/shared/{token_id}` 都直接查询数据库。这意味着：

- Token 被删除（用户主动撤销或过期清理）后，**下一次请求立即生效**，返回 404
- 不存在"撤销分享后旧缓存仍然可读"的时间窗口

这与 AuthCache（仅用于 OAuth/OIDC 登录流程的 state/token 缓存）完全分离，见 [routes/auth/auth_cache.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/auth/auth_cache.py)。

### 6.2 HTTP 响应级缓存控制

在 `MealieCrudRoute` 自定义路由类中（[routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py#L48-L49)），所有通过该类的 API 响应都会被强制追加：

```python
response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
```

这保证了浏览器和中间代理不会缓存用户 API 的响应。

**但注意**：Share Token 路由 `routes/recipe/shared_routes.py` 使用的是默认 `APIRouter`，**没有**应用 `MealieCrudRoute`，因此响应不带 `Cache-Control` 头，存在被浏览器/CDN 缓存的可能性。

### 6.3 SPA 静态资源缓存

在 [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L44-L50)：

```python
if path.startswith("_nuxt/"):
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
elif path == "." or response.media_type == "text/html":
    response.headers["Cache-Control"] = "no-cache"
```

前端构建产物（`_nuxt/*`）永久缓存（靠文件名 hash 失效），HTML 页面禁止缓存。

### 6.4 图片资源版本化缓存

Recipe 图片 URL 格式为：
```
/api/media/recipes/{recipe_id}/images/original.webp?version={recipe.image}
```

`recipe.image` 是一个 `0-255` 的随机整数，每次调用 `update_image` 都会重新生成（见 [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_recipes.py#L155-L159)）。这通过 URL query 参数变化实现了前端图片缓存的自动失效。

### 6.5 撤销分享后的边界总结

| 层面 | 是否缓存 | 撤销后失效时机 |
|-----|---------|--------------|
| 应用层（DB 查询） | 无缓存 | 立即 |
| Share Token API 响应头 | **未设置** `Cache-Control` | 取决于浏览器/CDN 默认策略（存在风险） |
| Explore/User API 响应头 | `no-cache, no-store` | 立即 |
| Recipe 图片 | URL 带 version 参数 | 立即（新 URL 不走旧缓存） |
| Recipe 附件 | 无特殊缓存头 | 取决于浏览器默认策略 |
| SPA HTML | `no-cache` | 立即 |
| SPA 静态资源 | 永久缓存（hash 文件名） | 重新发布构建后 |

---

## 七、防越权综合分析

### 7.1 Share Token 链路的防越权

```
匿名请求 GET /api/shared/{token_id}
    ↓
无认证依赖（APIRouter）
    ↓
Repository: group_id=None, household_id=None（无 scope 过滤）
    ↓
按主键 UUID 查询 RecipeShareTokenModel
    ↓
校验 is_expired → 过期则删除并返回 404
    ↓
通过 ORM relationship 返回关联的 Recipe 对象
    ↓
Pydantic Recipe Schema 序列化（字段裁剪）
```

**越权面**：
- ✅ Token UUID 不可猜测（122 位熵）
- ✅ 过期 Token 自动失效
- ✅ 级联删除：删除 Recipe 时 Token 自动清除
- ⚠️ **Media 路由独立无校验**：如果攻击者通过其他渠道获取了 recipe_id（UUID），可直接访问 `/api/media/recipes/{id}/images/original.webp`，无需 Token。这意味着 Share Token 只保护 Recipe JSON 数据，不保护图片/附件

### 7.2 Explore 公开浏览链路的防越权

```
匿名请求 GET /api/explore/groups/{group_slug}/recipes/{slug}
    ↓
get_public_group 依赖 → group.preferences.private_group=False
    ↓
BasePublicHouseholdExploreController: cross_household_repos (group_id=xxx, household_id=None)
    ↓
业务层校验 recipe.settings.public=True
    ↓
业务层校验 household.preferences.private_household=False
    ↓
Pydantic Recipe Schema 序列化
```

**越权面**：
- ✅ 四层防护（group 非私有 + Repository group 过滤 + recipe public + household 非私有）
- ✅ 用户自定义查询条件会与 public_filter 做 AND 合并，无法绕过

### 7.3 跨 Group/Household 越权

通过 Repository 层的 `_filter_builder` 强制附加 `group_id`/`household_id`，结合 `UserAPIRouter` 的登录认证，用户在调用任何受保护接口时：
- 无法通过修改请求参数查询其他 group 的数据
- 无法通过修改请求参数删除/修改其他 group 的数据
- 创建 Share Token 时校验 recipe.group_id == user.group_id

### 7.4 已知安全边界

1. **Media 路由无认证也无 Recipe 公开性校验**：图片/附件靠 UUID 不可猜测保护
2. **Share Token 路由无 Cache-Control**：可能被中间层缓存
3. **Recipe Share Token 不限制访问次数/速率**：理论上可被暴力枚举（但 UUID 空间足够大，实际不可行）
4. **SPA Meta 注入（`serve_shared_recipe_with_meta`）未校验 Token 过期**：见 [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py#L233-L243)，该函数只检查 Token 是否存在，不检查 `is_expired`，过期 Token 仍能获取 OG Meta（title/description/image），但无法获取完整 Recipe JSON

---

## 八、代码位置索引

| 关注点 | 文件路径 |
|-------|---------|
| Share Token 数据模型 | [db/models/recipe/shared.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/recipe/shared.py) |
| Share Token Schema | [schema/recipe/recipe_share_token.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_share_token.py) |
| Share Token 创建/管理（用户侧） | [routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/shared/__init__.py) |
| Share Token 消费（匿名侧） | [routes/recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/recipe/shared_routes.py) |
| 过期 Token 定时清理 | [services/scheduler/tasks/purge_expired_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/services/scheduler/tasks/purge_expired_share_tokens.py) |
| Router 认证级别定义 | [routes/_base/routers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/routers.py) |
| Controller 基类与 Scope | [routes/_base/base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/_base/base_controllers.py) |
| 认证依赖函数 | [core/dependencies/dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/core/dependencies/dependencies.py) |
| Repository Scope 过滤 | [repos/repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_generic.py) |
| Repository 工厂 | [repos/repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/repos/repository_factory.py) |
| Explore 公开 Recipes | [routes/explore/controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/explore/controller_public_recipes.py) |
| Recipe Schema（字段裁剪） | [schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe.py) |
| Recipe Settings（public 标志） | [schema/recipe/recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/schema/recipe/recipe_settings.py) |
| Group 私有标志 | [db/models/group/preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/group/preferences.py) |
| Household 私有标志 | [db/models/household/preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/db/models/household/preferences.py) |
| Media 路由 | [routes/media/media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/media/media_recipe.py) |
| SPA Meta 注入 | [routes/spa/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/spa/__init__.py) |
| AuthCache（与 Share Token 无关） | [routes/auth/auth_cache.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/mealie/routes/auth/auth_cache.py) |
| 集成测试 | [tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py](file:///d:/fz/0601/solo-dogfeeding/code/79-mealie/tests/integration_tests/user_recipe_tests/test_recipe_share_tokens.py) |
