# Cookbook 与 Recipe 集合组织方式分析

## 一、核心架构：动态筛选 vs 静态关联

Cookbook 与 Recipe 的关系采用**动态筛选**而非**静态关联表**的设计。Cookbook 并不存储具体的 recipe ID 列表，而是通过 `query_filter_string` 定义筛选规则，每次查询时实时计算符合条件的 recipes。

### 1.1 Cookbook 数据模型

后端模型定义：[cookbook.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/household/cookbook.py#L18-L55)

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | GUID | 主键 |
| `name` | String | Cookbook 名称 |
| `slug` | String | URL 友好的唯一标识（group 内唯一） |
| `description` | String | 描述 |
| `position` | Integer | 排序位置 |
| `public` | Boolean | 是否公开 |
| `query_filter_string` | String | **核心字段**：定义 recipe 筛选规则的查询字符串 |
| `group_id` | GUID | 所属 Group |
| `household_id` | GUID | 所属 Household |

### 1.2 已弃用的旧版筛选字段

Cookbook 模型中仍保留以下字段，但已标记为 deprecated，不再使用：

- `categories` / `require_all_categories`
- `tags` / `require_all_tags`
- `tools` / `require_all_tools`

---

## 二、路由挂载全景

所有路由从 [app.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/app.py#L147-L156) 进入，通过 `api_routers()` 挂载，主路由前缀统一为 `/api`，由 [routes/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/__init__.py#L1-L35) 定义。

### 2.1 完整路由树

```
app.include_router(router)  # prefix = "/api"
├── households.router
│   └── controller_cookbooks.router
│       prefix = "/households/cookbooks"
│       ├── GET    /api/households/cookbooks          # 列表（跨 household）
│       ├── GET    /api/households/cookbooks/{slug}   # 单个
│       ├── POST   /api/households/cookbooks          # 创建
│       ├── PUT    /api/households/cookbooks          # 批量更新 position
│       ├── PUT    /api/households/cookbooks/{id}     # 更新单个
│       └── DELETE /api/households/cookbooks/{id}     # 删除
│
├── recipe.router
│   ├── recipe_crud_routes.router  (prefix="/recipes", 需要认证)
│   │   ├── GET    /api/recipes                    # 列表（支持 cookbook 参数）
│   │   ├── GET    /api/recipes/{slug}             # 单个
│   │   ├── POST   /api/recipes                    # 创建
│   │   ├── PUT    /api/recipes/{slug}             # 更新
│   │   ├── DELETE /api/recipes/{slug}             # 删除
│   │   └── GET    /api/recipes/{slug}/summary     # 摘要
│   │
│   ├── shared_routes.router  (NO prefix, 无认证)
│   │   ├── GET /api/recipes/shared/{token_id}      # 通过令牌获取 recipe
│   │   └── GET /api/recipes/shared/{token_id}/zip  # 通过令牌下载 recipe zip
│   │
│   ├── comments.router  (prefix="/recipes", 需要认证)
│   ├── bulk_actions.router  (prefix="/recipes", 需要认证)
│   ├── exports.router  (需要认证)
│   └── timeline_events.router  (prefix="/recipes", 需要认证)
│
├── shared.router  (prefix="/shared/recipes", 需要认证)
│   ├── GET    /api/shared/recipes                  # 令牌列表（支持 recipe_id 过滤）
│   ├── POST   /api/shared/recipes                  # 创建分享令牌
│   ├── GET    /api/shared/recipes/{item_id}        # 获取单个令牌详情
│   └── DELETE /api/shared/recipes/{item_id}        # 撤销分享令牌
│
└── explore.router
    ├── controller_public_cookbooks.router  (prefix="/cookbooks", 无认证)
    │   ├── GET /api/explore/groups/{group_slug}/cookbooks        # 公开 cookbooks 列表
    │   └── GET /api/explore/groups/{group_slug}/cookbooks/{slug} # 单个公开 cookbook
    │
    └── controller_public_recipes.router  (prefix="/recipes", 无认证)
        ├── GET /api/explore/groups/{group_slug}/recipes                    # 公开 recipes 列表
        ├── GET /api/explore/groups/{group_slug}/recipes/{slug}             # 单个公开 recipe
        └── GET /api/explore/groups/{group_slug}/recipes/{slug}/summary     # 公开 recipe 摘要
```

---

## 三、条件筛选机制：QueryFilterBuilder

### 3.1 query_filter_string 语法

`query_filter_string` 是一种类 SQL 的查询表达式，由 [QueryFilterBuilder](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/services/query_filter/builder.py#L162-L566) 解析并转换为 SQLAlchemy 查询。

**支持的关系运算符：**
- 比较：`=`, `<>`, `>`, `<`, `>=`, `<=`
- 关键字：`IS`, `IS NOT`, `IN`, `NOT IN`, `CONTAINS ALL`, `LIKE`, `NOT LIKE`

**支持的逻辑运算符：**
- `AND`, `OR`

**支持的括号分组：**
- `()`, 可嵌套

**示例：**
```
tags.id IN ["tag-uuid-1", "tag-uuid-2"] AND created_at >= "2024-01-01"
(recipe_category.id = "cat-uuid" AND rating >= 4) OR household_id = "hh-uuid"
```

### 3.2 Cookbook 编辑器可用的筛选字段

前端 [CookbookEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Cookbook/CookbookEditor.vue#L60-L101) 中定义了可筛选的字段：

| 字段路径 | 显示名称 | 类型 |
|----------|----------|------|
| `recipe_category.id` | 分类 (Categories) | Organizer |
| `tags.id` | 标签 (Tags) | Organizer |
| `recipe_ingredient.food.id` | 食材 (Ingredients) | Organizer |
| `tools.id` | 工具 (Tools) | Organizer |
| `household_id` | 家庭 (Households) | Organizer |
| `user_id` | 用户 (Users) | Organizer |
| `created_at` | 创建日期 | Date |
| `updated_at` | 更新日期 | Date |

### 3.3 查询执行流程

**后端查询入口**：[RepositoryRecipes.page_all()](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/repos/repository_recipes.py#L220-L293)

当传入 `cookbook` 参数时：
1. 如果用户已有 `query_filter`，则与 cookbook 的 `query_filter_string` 用 `AND` 合并
2. 如果没有用户 query_filter，则直接使用 cookbook 的 `query_filter_string`
3. 由 QueryFilterBuilder 将字符串转换为 SQLAlchemy 过滤条件

```python
# repository_recipes.py#L243-L249
if cookbook:
    if pagination_result.query_filter and cookbook.query_filter_string:
        pagination_result.query_filter = (
            f"({pagination_result.query_filter}) AND ({cookbook.query_filter_string})"
        )
    else:
        pagination_result.query_filter = cookbook.query_filter_string
```

**注意**：当使用 cookbook 筛选时，传入的 `categories`/`tags`/`tools` 等独立参数会被**忽略**，仅以 `query_filter_string` 为准。

---

## 四、"手工收录" 的实现本质

### 4.1 没有传统意义的"手工收录"

由于 Cookbook 采用动态筛选，不存在"将某个 recipe 添加到 Cookbook"的操作。要让特定 recipe 出现在 Cookbook 中，需要：

1. 为 recipe 设置合适的 tag/category/tool 等属性
2. 或者修改 Cookbook 的 `query_filter_string` 来扩大筛选范围

### 4.2 间接实现"精确收录"的方式

如果要实现类似收藏夹的功能（只包含特定 recipes），可以通过以下方式：

- 为这些 recipes 添加一个**专用 tag**（如 `收藏-我的中餐`）
- 在 Cookbook 的筛选条件中设置 `tags.id = "该tag的UUID"`

---

## 五、排序展示机制

### 5.1 前端排序选项

[RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue#L207-L214) 支持以下排序方式：

| 排序类型 | orderBy 参数 | 默认方向 | 说明 |
|----------|--------------|----------|------|
| 字母顺序 | `name` | asc | A-Z / Z-A |
| 评分 | `rating` | desc | 高到低 |
| 创建时间 | `created_at` | desc | 新到旧 |
| 更新时间 | `updated_at` | desc | 新到旧 |
| 最近制作 | `last_made` | desc | 新到旧 |
| 随机 | `random` | - | 每次刷新种子不同 |

### 5.2 排序偏好存储

排序偏好存储在用户设置中，由 [useUserSortPreferences](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/use-users/preferences.ts) 管理，包含：
- `orderBy`: 当前排序字段
- `orderDirection`: 排序方向 (asc/desc)
- `filterNull`: 是否过滤空值
- `sortIcon`: 排序图标

### 5.3 Cookbook 自身排序

Cookbook 列表的排序通过 `position` 字段控制，前端使用 VueDraggable 拖拽排序，更新时调用 [updateAll](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/store/use-cookbook-store.ts#L24-L32) 批量更新所有 cookbook 的 position 值。

### 5.4 分页加载

前端采用**无限滚动**分页：
- 默认每页 32 条 recipes
- 首次加载 2 页（避免大屏只渲染 1 页无法触发滚动）
- 由 [useLazyRecipes](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/recipes/use-recipes.ts#L39-L110) 管理状态

---

## 六、删除 Recipe 后的集合联动

### 6.1 Recipe 删除流程

后端删除实现在 [RepositoryRecipes._delete_recipe()](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/repos/repository_recipes.py#L110-L130)：

1. 先删除 `UserToRecipe` 关联记录（避免脏数据）
2. 再删除 recipe 本身（级联删除相关的 ingredients/instructions/assets/comments 等）
3. API 层发布 `recipe_deleted` 事件

### 6.2 Cookbook 集合自动更新

由于 Cookbook 采用动态筛选，**删除 recipe 后无需任何额外操作**来更新 Cookbook：
- Cookbook 不存储 recipe ID 列表
- 下次查询时已删除的 recipe 自然不会出现在结果中

### 6.3 前端状态更新

前端在 [useLazyRecipes.removeRecipe()](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/recipes/use-recipes.ts#L79-L86) 中从本地数组移除已删除的 recipe：

```typescript
function removeRecipe(slug: string) {
  for (let i = 0; i < recipes?.value?.length; i++) {
    if (recipes?.value[i].slug === slug) {
      recipes?.value.splice(i, 1);
      break;
    }
  }
}
```

---

## 七、后端查询、前端展示与分享场景的边界

### 7.1 三大访问通道对比

| 维度 | 用户私有 API | 公开 Explore API | 分享令牌通道 |
|------|-------------|-----------------|-------------|
| **路由前缀** | `/api/recipes`<br>`/api/households/cookbooks` | `/api/explore/groups/{groupSlug}` | 令牌管理：`/api/shared/recipes`<br>令牌读取：`/api/recipes/shared/{tokenId}` |
| **认证要求** | 需要登录 Token | 无需认证 | 令牌管理：需要认证<br>令牌读取：无需认证 |
| **Cookbook 可见性** | 整个 group 的所有 cookbook | 仅 `public=true` 且所属 household 非私有 | 不适用（令牌与 Cookbook 无关） |
| **Recipe 可见性** | 整个 group 的所有 recipe | 仅 `settings.public=true` 且所属 household 非私有 | 单个 recipe（忽略 `settings.public`） |
| **筛选能力** | 完整 query_filter_string | 完整 query_filter_string，但强制附加公开条件 | 无筛选（令牌直接绑定 recipe_id） |

### 7.2 用户私有 API（已登录用户）

**Cookbook 路由**：[controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/households/controller_cookbooks.py)
- 前缀：`/api/households/cookbooks`
- `GET` - 获取 group 内所有 cookbooks（跨 household）
- `POST/PUT/DELETE` - 仅可操作自己 household 的 cookbook

**Recipe 路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/recipe_crud_routes.py#L340-L395)
- 前缀：`/api/recipes`
- `GET` - 获取 group 内所有 recipes，支持 `cookbook` 参数筛选
- 查询时使用 `group_recipes.by_user(user.id)` 支持用户个性化排序（如评分、收藏）

**分享令牌管理路由**：[shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/shared/__init__.py#L13-L50)
- 前缀：`/api/shared/recipes`
- `GET` - 列出令牌（支持 `?recipe_id=` 过滤）
- `POST` - 创建新令牌（校验 recipe 属于当前 group）
- `GET /{item_id}` - 获取令牌详情
- `DELETE /{item_id}` - 撤销令牌

### 7.3 公开 Explore API（未登录/其他 group 用户）

#### 7.3.1 公开 Cookbook 筛选逻辑

路由：[controller_public_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_cookbooks.py)
- 前缀：`/api/explore/groups/{group_slug}/cookbooks`

**第一层：查询时强制附加公开条件**（所有列表查询生效）
```python
public_filter = "(household.preferences.privateHousehold = FALSE AND public = TRUE)"
# 与用户 query_filter 用 AND 合并
pagination_result.query_filter = f"({pagination_result.query_filter}) AND ({public_filter})"
```

**第二层：单个查询时二次校验**
```python
# controller_public_cookbooks.py get_one() 方法
if not cookbook.public or cookbook.household.preferences.private_household:
    raise HTTPException(status_code=404, detail="Cookbook not found")
```

#### 7.3.2 公开 Recipe 筛选逻辑

路由：[controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_recipes.py)
- 前缀：`/api/explore/groups/{group_slug}/recipes`

**第一层：查询时强制附加公开条件**（所有列表查询生效）
```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
pagination_result.query_filter = f"({pagination_result.query_filter}) AND ({public_filter})"
```

**第二层：当通过 cookbook 参数筛选时，额外校验 Cookbook 的公开性**
```python
# controller_public_recipes.py get_all() 方法
if cookbook:
    if not cookbook.public or cookbook.household.preferences.private_household:
        raise HTTPException(status_code=404, detail="Recipe not found")
```

**第三层：单个 recipe 查询时二次校验**
```python
# controller_public_recipes.py get_one() 方法
if not recipe.settings.public or recipe.household.preferences.private_household:
    raise HTTPException(status_code=404, detail="Recipe not found")
```

#### 7.3.3 公开 Cookbook 页与 Recipe 的共同限制（关键）

当用户访问公开 Cookbook 页面（`/api/explore/groups/{group_slug}/cookbooks/{slug}`）并加载其 recipes 时，筛选条件是**层层叠加**的：

```
最终可见的 Recipe 必须同时满足：
├─ Cookbook 级别限制
│   ├─ cookbook.public = TRUE
│   └─ cookbook.household.preferences.private_household = FALSE
│
├─ Recipe 级别限制（Explore API 强制附加）
│   ├─ recipe.settings.public = TRUE
│   └─ recipe.household.preferences.private_household = FALSE
│
└─ Cookbook.query_filter_string 自定义筛选（如 tags、categories 等）
```

即使用户在 Cookbook 的筛选条件中设置了很宽的范围（如 `tags.id IS NOT NULL`），**Explore API 仍会强制过滤掉非公开的 recipe 和属于私有 household 的 recipe**。

### 7.4 分享令牌通道（临时分享链接）

分享令牌分为**管理端**和**读取端**两套独立的 API：

#### 7.4.1 管理端（需要认证）

路由：[shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/shared/__init__.py#L13-L50)
- 前缀：`/api/shared/recipes`

前端实现：[recipe-share.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe-share.ts#L1-L18)

| 操作 | 路由 | 说明 |
|------|------|------|
| 列出现有令牌 | `GET /api/shared/recipes?recipe_id={id}` | 按 recipe 过滤 |
| 创建新令牌 | `POST /api/shared/recipes` | 默认 30 天过期，校验 recipe 属于当前 group |
| 获取令牌详情 | `GET /api/shared/recipes/{item_id}` | 返回 token + recipe 对象 |
| 撤销令牌 | `DELETE /api/shared/recipes/{item_id}` | 删除令牌使其失效 |

令牌创建时会附加当前用户的 `group_id`（通过 `RecipeShareTokenSave`），确保只能分享同 group 的 recipe。

#### 7.4.2 读取端（无需认证）

路由：[recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/shared_routes.py#L22-L58)
- 前缀：`/api/recipes/shared`（挂载在 recipe.router 下，无额外 prefix）

前端公开 API：[public/shared.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/shared.ts#L1-L13)

| 操作 | 路由 | 说明 |
|------|------|------|
| 获取 recipe | `GET /api/recipes/shared/{token_id}` | 无需认证，自动清理过期令牌 |
| 下载 zip | `GET /api/recipes/shared/{token_id}/zip` | 返回 recipe.json + 原始图片 |

读取逻辑：
```python
# shared_routes.py#L22-L39
token_summary = db.recipe_share_tokens.get_one(token_id)
if token_summary and token_summary.is_expired:
    db.recipe_share_tokens.delete(token_id)  # 自动清理过期令牌
    token_summary = None

if token_summary is None:
    raise HTTPException(status_code=404, detail="Token Not Found")

return token_summary.recipe  # 直接返回绑定的 recipe，忽略 settings.public
```

**关键边界**：
- 分享令牌**绕过** `recipe.settings.public` 和 `household.preferences.private_household` 限制
- 令牌与 Cookbook **完全无关**，仅绑定单个 recipe_id
- 令牌有过期时间（默认 30 天，可自定义），过期自动清理
- 令牌仅在创建者的 group 内有效

### 7.5 前端分享页面入口

前端分享页面：[pages/g/[groupSlug]/shared/r/[id].vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/pages/g/%5BgroupSlug%5D/shared/r/%5Bid%5D.vue#L1-L47)

分享链接格式（由 [RecipeDialogShare.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeDialogShare.vue#L169-L171) 生成）：
```
/g/{groupSlug}/shared/r/{tokenId}
```

页面流程：
1. 通过 `usePublicApi()` 调用 `api.shared.getShared(tokenId)`
2. 后端请求 `GET /api/recipes/shared/{tokenId}`（公开 API，无需认证）
3. 成功则用 [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) 渲染 recipe
4. 失败（令牌过期/不存在）则跳转到 group 首页

### 7.6 前端 API 路由选择

前端通过 `publicGroupSlug` 参数动态切换 API：[use-group-cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/use-group-cookbooks.ts#L5-L19)

```typescript
// passing the group slug switches to using the public API
const api = publicGroupSlug ? usePublicExploreApi(publicGroupSlug).explore : useUserApi();
```

判断逻辑：
- `isOwnGroup` → 使用私有 API（`/api/households/cookbooks`，完整功能，可编辑）
- 非 own group → 使用公开 Explore API（`/api/explore/groups/{groupSlug}/cookbooks`，只读，仅显示公开内容）

前端 API 文件映射：
| 场景 | 后端路由前缀 | 前端 API 文件 |
|------|------------|--------------|
| Cookbook 私有 | `/api/households/cookbooks` | [group-cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/group-cookbooks.ts) |
| Cookbook 公开 | `/api/explore/groups/{slug}/cookbooks` | [public/explore/cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/explore/cookbooks.ts) |
| Recipe 私有 | `/api/recipes` | [user/recipes/recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe.ts) |
| Recipe 公开 | `/api/explore/groups/{slug}/recipes` | [public/explore/recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/explore/recipes.ts) |
| 令牌管理 | `/api/shared/recipes` | [user/recipes/recipe-share.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe-share.ts) |
| 令牌读取 | `/api/recipes/shared/{tokenId}` | [public/shared.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/shared.ts) |

---

## 八、完整数据流

### 8.1 Cookbook 页面加载流程

```
用户访问 /g/{groupSlug}/cookbooks/{slug}
    ↓
[CookbookPage.vue]
    ↓
判断 isOwnGroup → 选择 API
    ├─ 是 → useUserApi().cookbooks  → /api/households/cookbooks
    └─ 否 → usePublicExploreApi()   → /api/explore/groups/{groupSlug}/cookbooks
    ↓
获取 Cookbook 数据（含 query_filter_string）
    ↓
[RecipeCardSection.vue] 传入 query: { cookbook: slug }
    ↓
useLazyRecipes.fetchMore()
    ↓
判断 isOwnGroup → 选择 Recipe API
    ├─ 是 → /api/recipes?cookbook={slug}&page=1&perPage=64
    └─ 否 → /api/explore/groups/{groupSlug}/recipes?cookbook={slug}&...
    ↓
后端 [RepositoryRecipes.page_all()]
    ├─ 公开 API 先附加: (household.preferences.privateHousehold = FALSE AND settings.public = TRUE)
    ├─ 然后合并 Cookbook.query_filter_string（如果传了 cookbook 参数）
    ├─ 公开 API 额外校验 cookbook.public 且 cookbook.household 非私有
    └─ QueryFilterBuilder 解析为 SQL 条件
    ↓
返回分页结果
    ↓
前端展示 Recipe 卡片列表
```

### 8.2 Cookbook 筛选条件编辑流程

```
用户在 CookbookEditor 中修改筛选条件
    ↓
QueryFilterBuilder 组件生成 query_filter_string
    ↓
保存时调用 actions.updateOne(cookbook)
    ↓
PUT /api/households/cookbooks/{id}
    ↓
后端校验 query_filter_string 合法性（用 QueryFilterBuilder 尝试构建查询）
    ↓
保存成功后前端 router.go(0) 刷新页面（重新拉取 recipes）
```

### 8.3 Recipe 分享令牌创建流程

```
用户在 RecipeDialogShare 中设置过期日期（默认30天后）
    ↓
点击 "New" 创建新令牌
    ↓
POST /api/shared/recipes
    body: { recipeId, expiresAt }
    ↓
后端 [shared/__init__.py#createOne]
    ├─ 校验 recipe 属于当前 group
    ├─ 保存 RecipeShareTokenSave（附加 group_id）
    └─ 返回令牌详情（含 recipe 对象）
    ↓
前端生成分享链接: /g/{groupSlug}/shared/r/{tokenId}
    ↓
用户复制链接或使用系统分享功能
```

---

## 九、关键代码索引

| 功能模块 | 后端 | 前端 |
|---------|------|------|
| Cookbook 模型 | [cookbook.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/household/cookbook.py) | [cookbook.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/types/cookbook.ts) |
| Cookbook 私有 API | [controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/households/controller_cookbooks.py) | [group-cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/group-cookbooks.ts) |
| Cookbook 公开 API | [controller_public_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_cookbooks.py) | [public/explore/cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/explore/cookbooks.ts) |
| Recipe 查询 | [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/repos/repository_recipes.py) | [use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/recipes/use-recipes.ts) |
| Recipe 私有 API | [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/recipe_crud_routes.py) | [user/recipes/recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe.ts) |
| Recipe 公开 API | [controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_recipes.py) | [public/explore/recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/explore/recipes.ts) |
| 查询过滤器 | [builder.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/services/query_filter/builder.py) | [CookbookEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Cookbook/CookbookEditor.vue) |
| 分享令牌管理 | [shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/shared/__init__.py) | [user/recipes/recipe-share.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe-share.ts) |
| 分享令牌读取 | [recipe/shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/shared_routes.py) | [public/shared.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/public/shared.ts) |
| 分享令牌模型 | [shared.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/recipe/shared.py) | [recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/types/recipe.ts#L364-L387) |
| 分享对话框 | - | [RecipeDialogShare.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeDialogShare.vue) |
| 分享页面 | - | [shared/r/[id].vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/pages/g/%5BgroupSlug%5D/shared/r/%5Bid%5D.vue) |
| Cookbook 页面组件 | - | [CookbookPage.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Cookbook/CookbookPage.vue) |
| Recipe 卡片组件 | - | [RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue) |
