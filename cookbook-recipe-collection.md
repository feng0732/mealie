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

## 二、条件筛选机制：QueryFilterBuilder

### 2.1 query_filter_string 语法

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

### 2.2 Cookbook 编辑器可用的筛选字段

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

### 2.3 查询执行流程

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

## 三、"手工收录" 的实现本质

### 3.1 没有传统意义的"手工收录"

由于 Cookbook 采用动态筛选，不存在"将某个 recipe 添加到 Cookbook"的操作。要让特定 recipe 出现在 Cookbook 中，需要：

1. 为 recipe 设置合适的 tag/category/tool 等属性
2. 或者修改 Cookbook 的 `query_filter_string` 来扩大筛选范围

### 3.2 间接实现"精确收录"的方式

如果要实现类似收藏夹的功能（只包含特定 recipes），可以通过以下方式：

- 为这些 recipes 添加一个**专用 tag**（如 `收藏-我的中餐`）
- 在 Cookbook 的筛选条件中设置 `tags.id = "该tag的UUID"`

---

## 四、排序展示机制

### 4.1 前端排序选项

[RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue#L207-L214) 支持以下排序方式：

| 排序类型 | orderBy 参数 | 默认方向 | 说明 |
|----------|--------------|----------|------|
| 字母顺序 | `name` | asc | A-Z / Z-A |
| 评分 | `rating` | desc | 高到低 |
| 创建时间 | `created_at` | desc | 新到旧 |
| 更新时间 | `updated_at` | desc | 新到旧 |
| 最近制作 | `last_made` | desc | 新到旧 |
| 随机 | `random` | - | 每次刷新种子不同 |

### 4.2 排序偏好存储

排序偏好存储在用户设置中，由 [useUserSortPreferences](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/use-users/preferences.ts) 管理，包含：
- `orderBy`: 当前排序字段
- `orderDirection`: 排序方向 (asc/desc)
- `filterNull`: 是否过滤空值
- `sortIcon`: 排序图标

### 4.3 Cookbook 自身排序

Cookbook 列表的排序通过 `position` 字段控制，前端使用 VueDraggable 拖拽排序，更新时调用 [updateAll](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/store/use-cookbook-store.ts#L24-L32) 批量更新所有 cookbook 的 position 值。

### 4.4 分页加载

前端采用**无限滚动**分页：
- 默认每页 32 条 recipes
- 首次加载 2 页（避免大屏只渲染 1 页无法触发滚动）
- 由 [useLazyRecipes](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/recipes/use-recipes.ts#L39-L110) 管理状态

---

## 五、删除 Recipe 后的集合联动

### 5.1 Recipe 删除流程

后端删除实现在 [RepositoryRecipes._delete_recipe()](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/repos/repository_recipes.py#L110-L130)：

1. 先删除 `UserToRecipe` 关联记录（避免脏数据）
2. 再删除 recipe 本身（级联删除相关的 ingredients/instructions/assets/comments 等）
3. API 层发布 `recipe_deleted` 事件

### 5.2 Cookbook 集合自动更新

由于 Cookbook 采用动态筛选，**删除 recipe 后无需任何额外操作**来更新 Cookbook：
- Cookbook 不存储 recipe ID 列表
- 下次查询时已删除的 recipe 自然不会出现在结果中

### 5.3 前端状态更新

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

## 六、后端查询、前端展示与分享场景的边界

### 6.1 三大访问通道对比

| 维度 | 用户私有 API | 公开 Explore API | 分享令牌 API |
|------|-------------|-----------------|-------------|
| **路由前缀** | `/api/recipes` `/api/households/cookbooks` | `/api/explore/groups/{groupSlug}` | `/api/shared/{tokenId}` |
| **认证要求** | 需要登录 Token | 无需认证 | 无需认证 |
| **Cookbook 可见性** | 整个 group 的所有 cookbook | 仅 `public=true` 且所属 household 非私有 | 不适用 |
| **Recipe 可见性** | 整个 group 的所有 recipe | 仅 `settings.public=true` 且所属 household 非私有 | 单个 recipe |
| **筛选能力** | 完整 query_filter_string | 完整 query_filter_string，但强制附加公开条件 | 无筛选 |

### 6.2 用户私有 API（已登录用户）

**Cookbook 路由**：[controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/households/controller_cookbooks.py)
- `GET /api/households/cookbooks` - 获取 group 内所有 cookbooks（跨 household）
- `POST/PUT/DELETE` - 仅可操作自己 household 的 cookbook

**Recipe 路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/recipe_crud_routes.py#L340-L395)
- `GET /api/recipes` - 获取 group 内所有 recipes，支持 cookbook 参数筛选
- 查询时使用 `group_recipes.by_user(user.id)` 支持用户个性化排序（如评分、收藏）

### 6.3 公开 Explore API（未登录/其他 group 用户）

**公开 Cookbook 路由**：[controller_public_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_cookbooks.py)

强制附加筛选条件：
```python
public_filter = "(household.preferences.privateHousehold = FALSE AND public = TRUE)"
```

额外检查：
- 单个 cookbook 查询时，验证 `cookbook.public == true`
- 验证所属 household 非私有 (`private_household == false`)

**公开 Recipe 路由**：[controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_recipes.py)

强制附加筛选条件：
```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
```

当通过 cookbook 参数筛选时，除了验证 cookbook 本身公开外，还需要其所属 household 非私有。

### 6.4 分享令牌 API（临时分享链接）

模型定义：[shared.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/recipe/shared.py#L21-L34)

路由实现：[shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/recipe/shared_routes.py#L22-L58)

特点：
- 每个分享令牌绑定一个 recipe_id，默认 30 天过期
- `GET /api/shared/{tokenId}` - 通过令牌获取单个 recipe（无需 recipe.settings.public=true）
- `GET /api/shared/{tokenId}/zip` - 下载 recipe 及原始图片的 zip 包
- 令牌过期或不存在时返回 404，并自动清理过期令牌
- **分享令牌与 Cookbook 无关**，仅用于单个 recipe 的临时分享

### 6.5 前端 API 路由选择

前端通过 `publicGroupSlug` 参数动态切换 API：[use-group-cookbooks.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/use-group-cookbooks.ts#L5-L19)

```typescript
// passing the group slug switches to using the public API
const api = publicGroupSlug ? usePublicExploreApi(publicGroupSlug).explore : useUserApi();
```

判断逻辑：
- `isOwnGroup` → 使用私有 API（完整功能，可编辑）
- 非 own group → 使用公开 Explore API（只读，仅显示公开内容）

---

## 七、完整数据流

### 7.1 Cookbook 页面加载流程

```
用户访问 /g/{groupSlug}/cookbooks/{slug}
    ↓
[CookbookPage.vue]
    ↓
useCookbook(isOwnGroup ? null : groupSlug).getOne(slug)
    ↓
API: GET /api/households/cookbooks/{slug} 或 /api/explore/groups/{groupSlug}/cookbooks/{slug}
    ↓
获取 Cookbook 数据（含 query_filter_string）
    ↓
[RecipeCardSection.vue] 传入 query: { cookbook: slug }
    ↓
useLazyRecipes.fetchMore() 调用 recipes.getAll()
    ↓
API: GET /api/recipes?cookbook={slug}&page=1&perPage=64
    ↓
后端 [RepositoryRecipes.page_all()]
    → 读取 Cookbook.query_filter_string
    → QueryFilterBuilder 解析为 SQL 条件
    → 返回分页结果
    ↓
前端展示 Recipe 卡片列表
```

### 7.2 Cookbook 筛选条件编辑流程

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

---

## 八、关键代码索引

| 功能模块 | 后端 | 前端 |
|---------|------|------|
| Cookbook 模型 | [cookbook.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/household/cookbook.py) | [cookbook.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/types/cookbook.ts) |
| Cookbook API | [controller_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/households/controller_cookbooks.py) | [use-cookbook-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/store/use-cookbook-store.ts) |
| Recipe 查询 | [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/repos/repository_recipes.py) | [use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/composables/recipes/use-recipes.ts) |
| 查询过滤器 | [builder.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/services/query_filter/builder.py) | [CookbookEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Cookbook/CookbookEditor.vue) |
| 公开 Cookbook | [controller_public_cookbooks.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_cookbooks.py) | [CookbookPage.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Cookbook/CookbookPage.vue) |
| 公开 Recipe | [controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/routes/explore/controller_public_recipes.py) | [RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue) |
| Recipe 分享令牌 | [shared.py](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/mealie/db/models/recipe/shared.py) | [recipe-share.ts](file:///d:/fz/0601/solo-dogfeeding/code/72-mealie/frontend/app/lib/api/user/recipes/recipe-share.ts) |
