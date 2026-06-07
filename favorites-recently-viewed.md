# Favorites、recentRecipes 与 Shared Recipe 代码协作分析

## 1. 核心澄清：实际访问历史是否存在？

**结论：Mealie 不存在"用户访问历史/最近浏览"功能。** 前端存在一个名为 `recentRecipes` 的全局状态变量，但它的语义是"首页展示的最近 Recipe"，与"最近访问/浏览"无关。

### 1.1 前端 `recentRecipes` — 命名误导

**核心文件**：[use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/composables/recipes/use-recipes.ts#L8-L160)

```typescript
export const allRecipes = ref<Recipe[]>([]);
export const recentRecipes = ref<Recipe[]>([]);  // 容易误解为"最近浏览"

export const useRecipes = (
  all = false,
  fetchRecipes = true,
  loadFood = false,
  queryFilter: string | null = null,
  publicGroupSlug: string | null = null,
) => {
  // ...
  const { recipes, page, perPage } = (() => {
    if (all) {
      return { recipes: allRecipes, page: 1, perPage: -1 };
    } else {
      return { recipes: recentRecipes, page: 1, perPage: 30 };  // 默认分页 30 条
    }
  })();

  async function refreshRecipes() {
    // 按 created_at 排序，不是按访问时间
    const { data } = await api.recipes.getAll(page, perPage, { loadFood, orderBy: "created_at", queryFilter });
    if (data) {
      recipes.value = data.items;
    }
  }
};
```

**事实要点**：
- `recentRecipes` 只是一个全局 Vue ref，缓存首页展示的 Recipe 列表
- 后端查询 `orderBy: "created_at"`，即按 **创建时间** 倒序取前 30 条
- 与用户"是否访问过该 Recipe"完全无关，没有任何访问时间戳写入逻辑

### 1.2 真正存在的"最近"概念 — `last_made`（最近制作）

系统中唯一的"时间"类用户状态是 **`last_made`（最近制作时间）**，维度是 **Household**（家庭/组织），而非 User。

**两层 last_made 语义**：

| 层级 | 位置 | 含义 |
|------|------|------|
| Recipe 表 | `RecipeModel.last_made` | 全局值，所有 Household 中最大的 `last_made` 时间 |
| HouseholdToRecipe 表 | `HouseholdToRecipe.last_made` | 当前 Household 维度的独立制作时间 |

**核心文件**：[household_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py)

```python
class HouseholdToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "households_to_recipes"
    household_id = Column(GUID, ForeignKey("households.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    last_made: Mapped[datetime | None] = mapped_column(NaiveDateTime)
```

**前端展示逻辑**：[RecipeLastMade.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/components/Domain/Recipe/RecipeLastMade.vue#L179-L191)

```typescript
const lastMade = ref(props.recipe.lastMade);  // 先用 Recipe 全局值兜底

onMounted(async () => {
  if (!auth.user?.value?.householdSlug) {
    lastMade.value = props.recipe.lastMade;
  } else {
    // 真正的用户维度：调用 API 获取"当前用户所在 Household"的独立 last_made
    const { data } = await userApi.households.getCurrentUserHouseholdRecipe(props.recipe.slug || "");
    lastMade.value = data?.lastMade;
  }
  lastMadeReady.value = true;
});
```

对应后端 API：[controller_household_self_service.py#L30-L37](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/households/controller_household_self_service.py#L30-L37)

```python
@router.get("/self/recipes/{recipe_slug}", response_model=HouseholdRecipeSummary)
def get_household_recipe(self, recipe_slug: str):
    response = self.service.get_household_recipe(recipe_slug)
    if not response:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Recipe not found")
    return response
```

Service 层：[household_service.py#L70-L99](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/household_services/household_service.py#L70-L99)

```python
def get_household_recipe(self, recipe_slug: str) -> HouseholdRecipeSummary | None:
    recipe = self._get_recipe(recipe_slug)
    if not recipe:
        return None
    household_recipe_out = self.repos.household_recipes.get_by_recipe(recipe.id)
    if household_recipe_out:
        return household_recipe_out.cast(HouseholdRecipeSummary)
    else:
        return HouseholdRecipeSummary(recipe_id=recipe.id)  # 无记录时返回空（lastMade=None）
```

**更新 last_made 的完整链路**：
1. 前端点击"我做过了"按钮 → `RecipeLastMade.vue:createTimelineEvent()`
2. 调用 `PATCH /api/recipes/{slug}/last-made`，见 [recipe_crud_routes.py#L568-L581](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/recipe_crud_routes.py#L568-L581)
3. `RecipeService.update_last_made()` → bypass 权限检查（任何人都可以标记自己 Household 的 last_made）
4. 委托 `HouseholdService.set_household_recipe()` 写入/更新 `HouseholdToRecipe` 行
5. SQLAlchemy 事件监听器自动更新 `RecipeModel.last_made` 全局值（取所有 Household 最大值）

---

## 2. Favorites（收藏）— 用户维度

### 2.1 数据模型

收藏和评分共用同一张关联表 **`UserToRecipe`**。

**核心文件**：[user_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py)

```python
class UserToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "users_to_recipes"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id", name="user_id_recipe_id_rating_key"),)

    user_id = Column(GUID, ForeignKey("users.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    rating = Column(Float, index=True, nullable=True)
    is_favorite = Column(Boolean, index=True, nullable=False)  # 收藏标志
```

**关系映射**：
- 用户侧 `User.favorite_recipes`：用 `primaryjoin and_(UserToRecipe.user_id == User.id, UserToRecipe.is_favorite == True)` 过滤，见 [users.py#L109-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L109-L115)
- Recipe 侧 `RecipeModel.favorited_by`：反向关系，见 [recipe.py#L68-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L68-L74)

> **边界约束**：`UniqueConstraint("user_id", "recipe_id")` 确保同一用户对同一 Recipe 只有一条记录，收藏状态和评分共用同一行。

### 2.2 收藏 API 与协作流程

**核心文件**：[ratings.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/users/ratings.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/users/{id}/favorites` | 获取用户收藏列表 |
| POST | `/users/{id}/favorites/{slug}` | 添加收藏 |
| DELETE | `/users/{id}/favorites/{slug}` | 移除收藏 |
| POST | `/users/{id}/ratings/{slug}` | 设置评分+收藏（同一条记录） |

**set_rating 协作逻辑**（[ratings.py#L54-L76](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/users/ratings.py#L54-L76)）：
1. `assert_user_change_allowed` 校验：只能修改自己的
2. 跨 Household 可查 Recipe，但限制在同 Group
3. 查询 `UserToRecipe` 记录：不存在则创建（同时写 rating + is_favorite），已存在则更新字段
4. SQLAlchemy `before_update` 事件触发 Recipe 综合评分重算，见 [user_to_recipe.py#L46-L53](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py#L46-L53)

### 2.3 前端收藏列表的筛选方式

**核心文件**：[favorites.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/pages/user/[id]/favorites.vue#L31)

```typescript
const query = { queryFilter: `favoritedBy.id = "${userId}"` };
```

这个字符串由后端 **`QueryFilterBuilder`** 解析，通过链式关系访问遍历：
`RecipeModel.favorited_by` → `User.id`，最终生成 SQL JOIN 条件过滤出被该用户收藏的 Recipe，见 [builder.py#L214-L276](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/query_filter/builder.py#L214-L276)

---

## 3. Shared Recipe（共享 Recipe）— Group 维度 + 时效

### 3.1 数据模型

**核心文件**：[shared.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/shared.py)

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    group_id: FilterableColumn[GUID]
    recipe_id: FilterableColumn[GUID]
    expires_at: FilterableColumn[datetime]  # 默认 30 天后过期
```

**级联**：`RecipeModel.share_tokens` 配置了 `cascade="all, delete, delete-orphan"`，见 [recipe.py#L122-L124](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L122-L124)

### 3.2 API 边界与校验

**用户侧（管理 Token）**：[routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/shared/__init__.py)

| 方法 | 端点 | 边界校验 |
|------|------|----------|
| GET | `/shared/recipes` | 仅当前 Group 的 Token |
| POST | `/shared/recipes` | 强制校验 `recipe.group_id == user.group_id`，只能共享同 Group Recipe |
| GET | `/shared/recipes/{id}` | 登录态获取 Token 详情 |
| DELETE | `/shared/recipes/{id}` | 登录态撤销 |

创建时的关键边界检查（[routes/shared/__init__.py#L34-L42](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/shared/__init__.py#L34-L42)）：
```python
def create_one(self, data: RecipeShareTokenCreate) -> RecipeShareToken:
    group_repos = get_repositories(self.repos.session, group_id=self.group_id, household_id=None)
    recipe = group_repos.recipes.get_one(data.recipe_id, "id")
    if recipe is None or recipe.group_id != self.group_id:
        raise HTTPException(status_code=404, detail="Recipe not found in your group")
    save_data = RecipeShareTokenSave(**data.model_dump(), group_id=self.group_id)
    return self.mixins.create_one(save_data)
```

**公开侧（无需登录）**：[shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/shared/{token_id}` | 凭 Token 获取 Recipe，**不校验用户身份** |
| GET | `/shared/{token_id}/zip` | 凭 Token 下载压缩包 |

**过期懒清理逻辑**（[shared_routes.py#L26-L34](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py#L26-L34)）：
```python
token_summary = db.recipe_share_tokens.get_one(token_id)
if token_summary and token_summary.is_expired:
    try:
        db.recipe_share_tokens.delete(token_id)
        session.commit()
    except Exception:
        logger.exception(f"Failed to delete expired token {token_id}")
        session.rollback()
    token_summary = None

if token_summary is None:
    raise HTTPException(status_code=404, detail=ErrorResponse.respond("Token Not Found"))
```

> 共享 Token 的隐私边界：**创建时必须同 Group，访问时无 Group 限制**（公开 URL 即公开访问），依靠 `expires_at` 控制时效窗口。

---

## 4. 列表排序与隐私保留的协作

### 4.1 用户维度列别名（排序关键）

`RepositoryRecipes.by_user(user_id)` 会注入两个动态列别名，是用户个性化排序的核心机制：

**核心文件**：[repository_recipes.py#L36-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L36-L93)

```python
@property
def column_aliases(self):
    return {
        "last_made": self._get_last_made_col_alias(),  # 当前 Household 的 last_made，否则 1900-01-01
        "rating": self._get_rating_col_alias(),         # 用户个人评分优先，否则 Recipe 综合评分
    }
```

这两个别名在 QueryFilterBuilder 中会**优先于原始列**被使用，因此用户可以按：
- "我上次做的时间"（last_made 别名，当前 Household 独立值）
- "我的评分"（rating 别名，用户个人值优先）

来排序。

### 4.2 默认排序

`page_all` 无搜索无指定排序时，默认按 `created_at` 降序，见 [repository_recipes.py#L270-L272](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L270-L272)

### 4.3 三层隐私边界

| 层级 | 控制项 | 位置 |
|------|--------|------|
| Recipe 设置 | `public`、`locked`、`disable_comments` | [recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/schema/recipe/recipe_settings.py) |
| Household 偏好 | `lock_recipe_edits_from_other_households`、`recipe_public` 默认值 | HouseholdPreferences |
| Group 偏好 | `private_group` 控制默认 Household 偏好 | GroupPreferences |

### 4.4 编辑权限判断

`RecipeService.can_update` 使用原生 SQL 判断，综合考虑：所有者身份、Recipe 是否锁定、跨 Household 编辑策略，见 [recipe_service.py#L87-L131](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L87-L131)

删除权限仅所有者或管理员，协作编辑权限不适用于删除，见 [recipe_service.py#L70-L85](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L70-L85)

---

## 5. 成员退出后的状态保留与清理

### 5.1 删除 Recipe 时的清理

**核心文件**：[repository_recipes.py#L110-L153](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L110-L153)

```python
def _delete_recipe(self, recipe):
    # 1. 先手动删除所有用户的收藏+评分关联
    user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
    self.session.execute(user_to_recipe_delete_query)

    # 2. 再删除 Recipe 本身（级联删除 share_tokens、comments、timeline_events、assets 等）
    self.session.delete(recipe)
```

> **原因**：PostgreSQL 无法正确级联多对多关联表，必须先手动清理 `UserToRecipe`，否则外键约束报错。这也是 `delete_many` 只能逐行删除而非批量删除的原因。

### 5.2 删除 User 时的清理（关键缺陷）

**核心文件**：[repository_users.py#L55-L65](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_users.py#L55-L65)

```python
def delete(self, value, match_key=None):
    entry = super().delete(value, match_key)
    shutil.rmtree(PrivateUser.get_directory(value))  # 仅删除用户私有文件目录
    return entry
```

**User 模型级联配置**（[users.py#L84-L102](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L84-L102)）：

```python
sp_args = {
    "back_populates": "user",
    "cascade": "all, delete, delete-orphan",
    "single_parent": True,
}
tokens = relationship(LongLiveToken, **sp_args)
comments = relationship("RecipeComment", **sp_args)
recipe_timeline_events = relationship("RecipeTimelineEvent", **sp_args)
password_reset_tokens = relationship("PasswordResetModel", **sp_args)
```

**清理状态一览**：

| 用户相关数据 | 是否被级联删除 | 说明 |
|--------------|----------------|------|
| LongLiveToken | ✅ | 有 cascade 配置 |
| RecipeComment | ✅ | 有 cascade 配置 |
| RecipeTimelineEvent | ✅ | 有 cascade 配置 |
| PasswordResetToken | ✅ | 有 cascade 配置 |
| **UserToRecipe（收藏+评分）** | ❌ **残留** | 无 ondelete 级联，User 侧无关系 cascade |
| 用户私有文件目录 | ✅ | shutil.rmtree 手动删除 |
| 用户创建的 Recipe | ❌ **保留** | Recipe.user_id 外键无 ondelete，成为 orphan（但仍通过 group_id/household_id 可被访问） |
| RecipeShareToken | ❌ **保留** | Token 通过 group_id 绑定 Group，与 user_id 无直接关联 |

**后果**：
- 删除用户后其 `UserToRecipe` 行残留数据库，成为孤儿数据
- Recipe 综合评分（`RecipeModel.rating`）下次被触发重算时才会排除失效用户，否则中间值不准确
- 用户创建的 Recipe 不会被删除，但 `user_id` 指向不存在的用户（前端显示会降级为匿名）

### 5.3 成员切换 Household（迁移）时的状态保留

| 数据 | 绑定维度 | 切换后的行为 |
|------|----------|--------------|
| UserToRecipe（收藏/评分） | `user_id` | ✅ 完全保留，与 household_id 无关 |
| 拥有的 Recipe | `user_id` + `group_id` + `household_id` | 保留原 household_id，不会随用户迁移 |
| HouseholdToRecipe.last_made | `household_id` + `recipe_id` | ❌ 新 Household 无记录（从空开始），旧 Household 记录保留 |
| RecipeShareToken | `group_id` | ✅ 保留（只要仍在同一 Group） |
| 权限（can_manage 等） | `household_id` | ❌ 重置为新 Household 的默认值 |

### 5.4 删除 Group / Household 的级联风险

代码库未显式配置数据库级 `ON DELETE CASCADE`，完全依赖 SQLAlchemy ORM 级联和业务层手动清理。批量删除 Group/Household 可能残留：
- `UserToRecipe`（关联已失效的 user_id/recipe_id）
- `HouseholdToRecipe`（关联已失效的 household_id）
- `RecipeShareToken`（关联已失效的 group_id）

需要通过 `db/fixes/` 目录下的 data migration 脚本手动定期清理。

---

## 6. 总结与设计洞察

| 维度 | Favorites | recentRecipes | last_made | Shared Recipe |
|------|-----------|---------------|-----------|---------------|
| **实际含义** | 用户收藏 | 首页"最近 Recipe"（按创建时间） | 最近**制作**时间 | 临时公开共享 |
| **存储表** | `users_to_recipes` | 无（前端 ref 缓存） | `households_to_recipes` | `recipe_share_tokens` |
| **主键维度** | user_id + recipe_id | N/A | household_id + recipe_id | token_id |
| **隐私边界** | 用户私有 | Group/Household 可见 | Household 私有 | Group 内创建，公开 URL 访问 |
| **时效控制** | 永久（手动取消） | 实时刷新 | 永久（手动更新） | 默认 30 天过期 |
| **参与排序** | 作为 QueryFilter 过滤条件 | N/A（列表本身就是排序结果） | 作为 orderBy 选项（`last_made` 列别名） | 不参与 |
| **Recipe 删除时** | ✅ 手动 DELETE 清理 | N/A | ✅ ORM 级联 | ✅ cascade delete-orphan |
| **用户删除时** | ❌ **残留** | N/A | ✅ 与 user 无关（household 维度） | ✅ 与 user 无关（group 维度） |
| **用户切换 Household** | ✅ 保留 | N/A | ❌ 新 Household 为空 | ✅（同 Group 时）保留 |

**用户感觉"不直观"的核心原因**：
1. `recentRecipes` 命名误导，实际是"最近创建"而非"最近访问/浏览"，且系统完全没有访问历史记录
2. `last_made` 是 Household 维度而非 User 维度，同一用户在不同 Household 看到不同的"最近做过"状态
3. 收藏与评分共用同一条数据库记录，语义上耦合但 UI 上是两个独立操作
4. 用户被删除后其收藏/评分残留，可能导致 Recipe 综合评分短时间内包含失效用户的数据
