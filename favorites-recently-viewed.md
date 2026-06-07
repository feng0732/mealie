# Favorites、last_made 与 Shared Recipe 代码协作深度分析

## 1. 概念澄清：recentRecipes ≠ 最近浏览

**结论：Mealie 不存在任何"用户访问历史/最近浏览"功能。** 前端存在 `recentRecipes` 变量，但其语义是"首页最近 Recipe 列表"，与"浏览"毫无关系。

### 1.1 前端 `recentRecipes` 的真实含义

**核心文件**：[use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/composables/recipes/use-recipes.ts#L8-L160)

```typescript
export const allRecipes = ref<Recipe[]>([]);
export const recentRecipes = ref<Recipe[]>([]);  // 全局缓存，命名高度误导

export const useRecipes = (
  all = false,
  fetchRecipes = true,
  loadFood = false,
  queryFilter: string | null = null,
  publicGroupSlug: string | null = null,
) => {
  // all=true: 拉取全部；all=false: 取前30条作为"首页最近"
  const { recipes, page, perPage } = all
    ? { recipes: allRecipes, page: 1, perPage: -1 }
    : { recipes: recentRecipes, page: 1, perPage: 30 };

  async function refreshRecipes() {
    // 关键：orderBy: "created_at" —— 按创建时间倒序
    const { data } = await api.recipes.getAll(page, perPage, {
      loadFood, orderBy: "created_at", queryFilter
    });
    if (data) recipes.value = data.items;
  }
};
```

| 变量 | 分页参数 | 排序依据 | 实际语义 |
|------|----------|----------|----------|
| `allRecipes` | `perPage=-1` | `created_at` | 全部 Recipe |
| `recentRecipes` | `perPage=30` | `created_at` | 最近**创建**的前 30 条 Recipe |

---

## 2. `last_made`（最近制作）— Household 维度时间状态

系统中唯一的用户时间维度状态是 **`last_made`（最近制作时间）**，维度是 **Household**，而非 User。

### 2.1 两层 last_made 协作关系

| 层级 | 存储位置 | 更新方式 | 含义 |
|------|----------|----------|------|
| Household 独立值 | `HouseholdToRecipe.last_made` | 用户手动标记 / mealplan 每日定时任务 | 该 Household "什么时候做过这个菜" |
| Recipe 全局值 | `RecipeModel.last_made` | SQLAlchemy 事件自动取所有 Household 最大值 | 所有 Household 中最近一次制作时间 |

**核心文件**：[household_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py#L39-L84)

```python
class HouseholdToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "households_to_recipes"
    household_id = Column(GUID, ForeignKey("households.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    last_made: Mapped[datetime | None] = mapped_column(NaiveDateTime)


def update_recipe_last_made(session: Session, target: HouseholdToRecipe):
    if not target.last_made:
        return
    from mealie.db.models.recipe.recipe import RecipeModel
    recipe = session.query(RecipeModel).filter(RecipeModel.id == target.recipe_id).first()
    if not recipe:
        return
    # 全局 last_made = max(旧全局值, 当前 Household 的值)
    recipe.last_made = recipe.last_made or target.last_made
    recipe.last_made = max(recipe.last_made, target.last_made)


# 同时监听 insert/update/delete 三种事件
@event.listens_for(HouseholdToRecipe, "after_insert")
@event.listens_for(HouseholdToRecipe, "after_update")
@event.listens_for(HouseholdToRecipe, "after_delete")
def update_recipe_rating_on_insert_or_delete(_, connection: Connection, target: HouseholdToRecipe):
    session = Session(bind=connection)
    update_recipe_last_made(session, target)
    session.commit()
```

> **潜在不一致**：`after_delete` 事件触发时，`target.last_made` 仍是删除前的值，`max()` 逻辑不会让全局值**回退**。如果最近制作的那个 Household 删除了记录，全局 `RecipeModel.last_made` 会一直保留那个"已被删除"的时间戳，直到有更大的值出现。

### 2.2 API 路由与时间戳来源

**实际 API 路径**（之前描述有误，已修正）：

| 操作 | 实际 API 路径 | 方法 |
|------|--------------|------|
| 更新 last_made | `/api/recipes/{slug}/last-made` | PATCH |
| 获取当前 Household 的 last_made | `/api/households/self/recipes/{recipe_slug}` | GET |

**Schema 定义**，见 [recipe.py#L386-L387](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/schema/recipe/recipe.py#L386-L387)：

```python
class RecipeLastMade(BaseModel):
    timestamp: datetime.datetime
```

### 2.3 last_made 的三个写入入口

#### 入口 1：用户手动标记（Recipe 详情页 "Made this" 按钮）

**前端组件**：[RecipeLastMade.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/components/Domain/Recipe/RecipeLastMade.vue)

时间戳来源流程：
1. 用户打开对话框 → `newTimelineEventTimestamp.value = new Date()`（客户端本地当前时间）
2. 用户选择日期（date picker）→ `newTimelineEventTimestamp` 更新
3. 表单提交时计算：`new Date(dateString + "T23:59:59").toISOString()`（选当天则是"当天 23:59:59 本地时间"的 ISO 字符串）
4. 先调用 `createTimelineEvent`（创建 timeline 事件），成功后再调用 `updateLastMade` 更新 last_made

```typescript
// 第 263 行：时间戳为用户选择日期的"当天 23:59:59"
newTimelineEvent.value.timestamp = new Date(newTimelineEventTimestampString.value + "T23:59:59").toISOString();

// 第 267 行：先创建 timeline event
const eventResponse = await userApi.recipes.createTimelineEvent(newTimelineEvent.value);

// 第 281-284 行：仅当新时间戳 > 旧 last_made 时才更新
if (!lastMade.value || newTimelineEvent.value.timestamp > lastMade.value) {
  lastMade.value = newTimelineEvent.value.timestamp;
  await userApi.recipes.updateLastMade(props.recipe.slug, newTimelineEvent.value.timestamp);
}
```

> **前端保护**：只有新时间戳大于现有 last_made 才会发请求，但这只是客户端保护——后端不做校验，直接覆盖。

**后端路由**：[recipe_crud_routes.py#L568-L587](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/recipe_crud_routes.py#L568-L587)

```python
@router.patch("/{slug}/last-made")
def update_last_made(self, slug: str, data: RecipeLastMade):
    """Update a recipe's last made timestamp"""
    try:
        recipe = self.service.update_last_made(slug, data.timestamp)
    except Exception as e:
        self.handle_exceptions(e)
    # publish event...
```

**RecipeService 写入流程**：[recipe_service.py#L559-L566](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L559-L566)

```python
def update_last_made(self, slug_or_id: str | UUID, timestamp: datetime) -> Recipe:
    # 显式绕过权限检查：任何用户都可以更新 last_made
    # （即使 Recipe 被锁定，或用户属于不同 Household）
    household_service = HouseholdService(self.user.group_id, self.user.household_id, self.repos)
    household_service.set_household_recipe(slug_or_id, HouseholdRecipeUpdate(last_made=timestamp))
    return self.get_one(slug_or_id)
```

**HouseholdService.set_household_recipe**：[household_service.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/household_services/household_service.py#L99-L127)

```python
def set_household_recipe(self, recipe_slug, data: HouseholdRecipeUpdate):
    recipe = self._get_recipe(recipe_slug)
    existing = self.repos.household_recipes.get_by_recipe(recipe.id)
    if existing:
        return self.repos.household_recipes.patch(existing.id, updated_data)  # UPDATE
    else:
        return self.repos.household_recipes.create(create_data)               # INSERT
```

写入 `HouseholdToRecipe` 后，SQLAlchemy 的 `after_insert`/`after_update` 事件自动更新全局 `RecipeModel.last_made`。

#### 入口 2：Mealplan 每日定时任务自动更新

**核心文件**：[create_timeline_events.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/scheduler/tasks/create_timeline_events.py#L25-L134)

调度器每天遍历所有 Group → Household → 当天的 mealplan：

```python
def _create_mealplan_timeline_events_for_household(event_time, session, group_id, household_id):
    # ...
    for mealplan in mealplans:
        if not (mealplan.recipe and mealplan.user_id):
            continue

        # 去重：今天已经创建过该事件就跳过
        # 条件：当前没有 last_made 或 last_made 日期早于今天
        household_to_recipe = household_service.get_household_recipe(mealplan.recipe.slug)
        last_made = household_to_recipe.last_made if household_to_recipe else None
        if (not last_made or last_made.date() < event_time.date()) and mealplan.recipe_id not in recipes_to_update:
            recipes_to_update[mealplan.recipe_id] = mealplan.recipe

        timeline_events_to_create.append(RecipeTimelineEventCreate(...))

    # 1. 批量写入 timeline events
    for event in timeline_events_to_create:
        repos.recipe_timeline_events.create(event)

    # 2. 逐个更新 HouseholdToRecipe.last_made + RecipeModel.last_made（手动）
    for recipe in recipes_to_update.values():
        household_service.set_household_recipe(recipe.slug, HouseholdRecipeUpdate(last_made=event_time))
        repos.recipes.patch(recipe.slug, {"last_made": event_time})  # 手动更新全局值（不依赖事件）
```

> 定时任务**同时**写入了 `HouseholdToRecipe`（触发 ORM 事件更新全局值）和手动 `repos.recipes.patch` 更新 `RecipeModel.last_made`。两者幂等（都是取最大值），不会冲突。

**"保留未来日期"测试验证**，见 [test_create_timeline_events.py#L208-L253](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/services_tests/scheduler/tasks/test_create_timeline_events.py#L208-L253)：如果用户手动设置了未来日期的 last_made，定时任务不会用今天的日期覆盖它。

#### 入口 3：RepositoryRecipes 列别名（排序用）

`RepositoryRecipes.by_user(user_id)` 注入的 `last_made` 列别名优先取当前 Household 的值，否则回退到 `1900-01-01`（排序时排到最后），见 [repository_recipes.py#L36-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L36-L93)。

---

## 3. Favorites（收藏）与评分 — UserToRecipe 的 secondary 关系

### 3.1 数据模型与约束

**核心文件**：[user_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py)

```python
class UserToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "users_to_recipes"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id", name="user_id_recipe_id_rating_key"),)

    user_id = Column(GUID, ForeignKey("users.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    rating = Column(Float, index=True, nullable=True)
    is_favorite = Column(Boolean, index=True, nullable=False)

    # 通过 association_proxy 从 Recipe 间接获取 group_id/household_id（用于 Group 级查询过滤）
    group_id: AssociationProxy[GUID] = association_proxy("recipe", "group_id")
    household_id: AssociationProxy[GUID] = association_proxy("recipe", "household_id")
```

> **关键**：两个 ForeignKey（`user_id`、`recipe_id`）**都没有配置 `ondelete` 参数**，数据库层面默认是 `ON DELETE NO ACTION`（严格外键约束）。

### 3.2 SQLAlchemy secondary 多对多关系配置

收藏和评分通过 `secondary=UserToRecipe.__tablename__` 实现多对多，两侧各定义了两组关系（rating + favorite），共用同一张 secondary 表。

**User 侧**：[users.py#L103-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L103-L115)

```python
rated_recipes: Mapped[list["RecipeModel"]] = orm.relationship(
    "RecipeModel",
    secondary=UserToRecipe.__tablename__,
    back_populates="rated_by",
    overlaps="recipe,favorited_by,favorited_recipes",
)
favorite_recipes: Mapped[list["RecipeModel"]] = orm.relationship(
    "RecipeModel",
    secondary=UserToRecipe.__tablename__,
    primaryjoin="and_(User.id==UserToRecipe.user_id, UserToRecipe.is_favorite==True)",  # 额外过滤
    back_populates="favorited_by",
    overlaps="recipe,rated_by,rated_recipes",
)
```

**RecipeModel 侧**：[recipe.py#L62-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L62-L74)

```python
rated_by: Mapped[list["User"]] = orm.relationship(
    "User",
    secondary=UserToRecipe.__tablename__,
    back_populates="rated_recipes",
    overlaps="recipe,favorited_by,favorited_recipes",
)
favorited_by: Mapped[list["User"]] = orm.relationship(
    "User",
    secondary=UserToRecipe.__tablename__,
    primaryjoin="and_(RecipeModel.id==UserToRecipe.recipe_id, UserToRecipe.is_favorite==True)",
    back_populates="favorite_recipes",
    overlaps="recipe,rated_by,rated_recipes",
)
```

> **secondary 关系配置细节**：
> 1. `overlaps` 参数告诉 SQLAlchemy：四组关系（rated_recipes/favorite_recipes/rated_by/favorited_by）**共享同一张 secondary 表**，不要试图重复 INSERT/DELETE 同一行。
> 2. `favorite_recipes` / `favorited_by` 通过 `primaryjoin` 条件 `is_favorite==True` 做视图级过滤，但底层仍复用同一条 `UserToRecipe` 行（与 rating 共用）。
> 3. **四个关系都没有配置 `cascade`** —— 这意味着 ORM 的 `session.delete(user)` 或 `session.delete(recipe)` **不会自动清理 `UserToRecipe` 行**。
> 4. 这四个关系也没有 `viewonly=True`，理论上可以通过 `user.favorite_recipes.append(recipe)` 添加收藏，但实际业务代码全部走 `RepositoryUserRatings` 直接操作 `UserToRecipe` 表。

### 3.3 Recipe 综合评分的事件驱动重算

**核心文件**：[user_to_recipe.py#L36-L53](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py#L36-L53) + [recipe.py#L284-L300](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L284-L300)

**事件链路**：
1. `UserToRecipe` 发生 `after_insert` / `after_update` / `after_delete` 事件
2. 监听器把目标 Recipe 的 `rating` 设为 `-1` 作为"脏标记"
3. `RecipeModel.before_update` 检测到 `rating` 被修改，从 `UserToRecipe` 表重算 `AVG(rating)`（排除 rating 为空或 ≤0 的行）

### 3.4 收藏 API 端点

| 操作 | API 路径 | 方法 |
|------|---------|------|
| 获取用户收藏列表 | `/api/users/{id}/favorites` 或 `/api/users/self/favorites` | GET |
| 添加收藏 | `/api/users/{id}/favorites/{slug}` | POST |
| 移除收藏 | `/api/users/{id}/favorites/{slug}` | DELETE |
| 获取用户评分列表 | `/api/users/{id}/ratings` 或 `/api/users/self/ratings` | GET |
| 设置评分+收藏（同一条记录） | `/api/users/{id}/ratings/{slug}` | POST |
| 获取单条评分+收藏 | `/api/users/self/ratings/{recipe_id}` | GET |

**收藏查询前端实现**，见 [favorites.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/pages/user/[id]/favorites.vue#L31)：

```typescript
const query = { queryFilter: `favoritedBy.id = "${userId}"` };
```

后端 `QueryFilterBuilder` 将其解析为跨 `RecipeModel.favorited_by → User.id` 关系的 JOIN。

### 3.5 评分保留收藏、收藏保留评分

测试验证：更新评分时如果 `is_favorite=None`，不会覆盖已有收藏值；更新收藏时如果 `rating=None`，不会覆盖已有评分值，见 [test_recipe_ratings.py#L188-L262](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py#L188-L262)。

---

## 4. Shared Recipe（共享 Token）— Group 边界与时效

### 4.1 数据模型

**核心文件**：[shared.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/shared.py)

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    group_id: FilterableColumn[GUID] = mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)
    recipe_id: FilterableColumn[GUID] = mapped_column(GUID, sa.ForeignKey("recipes.id"), nullable=False, index=True)
    recipe: Mapped["RecipeModel"] = relationship("RecipeModel", back_populates="share_tokens", uselist=False)
    expires_at: FilterableColumn[datetime] = mapped_column(NaiveDateTime, nullable=False)  # 默认 30 天
```

`RecipeModel.share_tokens` 配置了 `cascade="all, delete, delete-orphan"`，见 [recipe.py#L122-L124](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L122-L124)。

### 4.2 边界校验

**创建侧（登录态）**：[routes/shared/__init__.py#L34-L42](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/shared/__init__.py#L34-L42) — 强制 `recipe.group_id == user.group_id`，只能共享同 Group 内的 Recipe。

**访问侧（公开）**：[shared_routes.py#L26-L34](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py#L26-L34) — 不校验用户身份，仅校验 Token 存在且未过期。过期 Token 在访问时被**懒删除**。

> 两阶段边界：①创建时必须同 Group；②访问时无 Group/用户限制（公开 URL 即公开访问），仅靠 `expires_at` 控制时效窗口。

---

## 5. 外键约束与级联配置全景

### 5.1 数据库级外键：全部无 `ON DELETE CASCADE`

所有 `ForeignKey` 定义（包括 Alembic 迁移）都**没有配置 `ondelete` 参数**，数据库层面默认是 `ON DELETE NO ACTION`（严格模式）。这意味着直接 SQL `DELETE FROM users ...` 会因外键约束直接报错——所有删除必须由 ORM 先行处理或业务层手动清理。

### 5.2 ORM 级联关系一览

| 关系 | 定义侧 | cascade 配置 | 说明 |
|------|--------|-------------|------|
| User → tokens/comments/recipe_timeline_events/password_reset_tokens/mealplans/shopping_lists | [users.py#L84-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L84-L93) | `all, delete, delete-orphan` | ✅ 删除用户时级联清理 |
| User → owned_recipes | [users.py#L95-L98](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L95-L98) | 无 | ❌ Recipe.user_id 变为无效引用 |
| User → rated_recipes / favorite_recipes | [users.py#L103-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L103-L115) | 无（secondary） | ❌ 不清理 UserToRecipe |
| RecipeModel → share_tokens/comments/timeline_events/settings/... | [recipe.py#L96-L132](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L96-L132) | `all, delete, delete-orphan` | ✅ 删除 Recipe 时级联清理 |
| RecipeModel → rated_by / favorited_by | [recipe.py#L62-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L62-L74) | 无（secondary） | ❌ 不自动清理（需手动 `sa.delete`） |
| RecipeModel → made_by（Household） | [recipe.py#L148-L150](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L148-L150) | 无（secondary） | ❌ 不清理 HouseholdToRecipe |
| Household → made_recipes | [household.py#L69-L71](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household.py#L69-L71) | 无（secondary） | ❌ 不清理 HouseholdToRecipe |
| Group → recipes / ... | [group.py#L66-L86](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/group/group.py#L66-L86) | 大部分有 `common_args` cascade | ✅ 大部分级联清理 |
| Group → RecipeShareToken | [group.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/group/group.py) | 有 cascade | ✅ 删除 Group 时清理 Token |

---

## 6. 成员退出 / 删除时的实际清理边界

### 6.1 场景 A：删除 Recipe

**RepositoryGeneric.delete** 核心逻辑，见 [repository_generic.py#L256-L269](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_generic.py#L256-L269)：

```python
def delete(self, value, match_key: str | None = None) -> Schema:
    match_key = match_key or self.primary_key
    result = self._query_one(value, match_key)
    result_as_model = self.schema.model_validate(result)
    try:
        self.session.delete(result)   # 触发 ORM 级联
        self.session.commit()
    except Exception as e:
        self.session.rollback()
        raise e
    return result_as_model
```

**RepositoryRecipes 覆写了删除逻辑**（因为 PostgreSQL 级联 secondary 表有 bug），见 [repository_recipes.py#L110-L153](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L110-L153)：

```python
def _delete_recipe(self, recipe: RecipeModel) -> Recipe:
    recipe_as_model = self.schema.model_validate(recipe)

    # Step 1：先手动 DELETE 所有 UserToRecipe
    try:
        user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
        self.session.execute(user_to_recipe_delete_query)
        self.session.commit()
    except Exception:
        self.session.rollback()
        raise

    # Step 2：session.delete(recipe) → 触发 ORM 级联 share_tokens/comments/...
    try:
        self.session.delete(recipe)
        self.session.commit()
    except Exception:
        self.session.rollback()
        raise

    return recipe_as_model
```

**清理结果**：

| 关联数据 | 是否清理 | 方式 |
|----------|----------|------|
| UserToRecipe（所有用户的收藏+评分） | ✅ | 手动 `sa.delete()` |
| HouseholdToRecipe（所有 Household 的 last_made） | ❌ **残留** | 没有手动清理，secondary 关系无 cascade，数据库外键也无 `ON DELETE` |
| ShareTokens | ✅ | ORM cascade |
| Comments / TimelineEvents / Assets / Notes / Settings / Nutrition | ✅ | ORM cascade |
| Meal entries（GroupMealPlan.recipe_id） | ✅ | ORM cascade |
| Recipe 综合评分重算 | ❌ 无意义 | Recipe 已被删除 |

> **清理不一致**：手动清理了 UserToRecipe 却遗漏了同样是 secondary 关系的 HouseholdToRecipe。不过 `RecipeModel.last_made` 列也随 Recipe 被删除，所以残留的 HouseholdToRecipe 只是孤儿行（recipe_id 指向不存在的 Recipe），不影响正常功能，但会占用空间。

**测试覆盖**：`test_delete_recipe_deletes_ratings`，见 [test_recipe_ratings.py#L295-L316](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py#L295-L316) — 验证删除 Recipe 后 `GET /api/users/self/ratings/{recipe_id}` 返回 404（即 UserToRecipe 已被清理）。但**没有测试 HouseholdToRecipe 是否残留**。

### 6.2 场景 B：删除 User

**RepositoryUsers.delete**，见 [repository_users.py#L55-L65](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_users.py#L55-L65)：

```python
def delete(self, value: str | UUID4, match_key: str | None = None) -> User:
    if settings.IS_DEMO:
        user_to_delete = self.get_one(value, match_key)
        if user_to_delete and user_to_delete.is_default_user:
            return user_to_delete

    entry = super().delete(value, match_key)   # 调用 RepositoryGeneric.delete → session.delete(user)
    shutil.rmtree(PrivateUser.get_directory(value))  # 删除用户私有文件目录
    return entry
```

`session.delete(user)` 触发 ORM 级联仅限于 User 模型配置了 cascade 的关系。

**清理结果**：

| 关联数据 | 是否清理 | 方式 |
|----------|----------|------|
| LongLiveToken / RecipeComment / RecipeTimelineEvent / PasswordResetToken | ✅ | ORM cascade（`sp_args` 配置） |
| GroupMealPlan（用户 mealplans） | ✅ | ORM cascade |
| ShoppingList（用户 shopping_lists） | ✅ | ORM cascade |
| **UserToRecipe（该用户所有收藏+评分）** | ❌ **残留** | 无 ondelete、无 ORM cascade、无手动清理 |
| 用户创建的 Recipe（owned_recipes） | ❌ **残留** | 无 cascade，Recipe.user_id 变为无效引用 |
| RecipeShareToken | ✅ 与 user 无关 | Token 绑定 group_id，不受用户删除影响 |
| 用户私有文件目录 | ✅ | `shutil.rmtree` 手动删除 |
| HouseholdToRecipe | ✅ 与 user 无关 | 绑定 household_id |

**残留 UserToRecipe 的后果**：
1. 数据库中存在孤儿行（`user_id` 指向不存在的用户），但数据库不会报错——因为外键约束在 `session.delete(user)` 时 ORM 已先行 commit（没有关联到 UserToRecipe 的外键被检测到冲突？不对，实际上应该报错。让我再仔细看...）

等等，这里有一个关键问题需要澄清：如果 `UserToRecipe.user_id` 有外键到 `users.id`，而删除 User 时没有先清理 UserToRecipe，数据库应该在 COMMIT 时报外键约束错误。让我检查实际情况...

实际上，`RepositoryGeneric.delete` 是 `session.delete(result)` 然后 `session.commit()`。SQLAlchemy 的 `session.delete()` 在处理对象时，**不会自动处理 secondary 多对多关系的关联表**——但在这种情况下，UserToRecipe 是一个**独立的 ORM 实体类**（有自己的 class 定义和主键），不是纯 secondary 关联表。SQLAlchemy 会把它当作独立对象对待，外键约束在 commit 时会被数据库检查。

**关键事实**：测试 `test_user_directory_deleted_on_delete`，见 [test_user_repository.py#L5-L10](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/repository_tests/test_user_repository.py#L5-L10) — 只验证了用户目录被删除，**没有验证 UserToRecipe 是否被清理**。测试用户是全新创建的 fixture user，没有任何收藏/评分，所以 UserToRecipe 为空，外键约束不会报错。

**实际影响**：当一个有收藏/评分记录的用户被删除时，`session.commit()` 大概率会因为外键约束失败而抛出 IntegrityError，整个删除操作被回滚——也就是说，**有收藏/评分的用户根本删不掉**（数据库阻断删除）。

### 6.3 场景 C：用户切换 Household（成员迁移）

User.household_id 从旧值改为新值时：

| 关联数据 | 绑定维度 | 切换后的行为 |
|----------|----------|--------------|
| UserToRecipe（收藏+评分） | `user_id` | ✅ 完全保留，与 household_id 无关 |
| 用户拥有的 Recipe | `user_id` + 独立 `household_id` 列 | ✅ 保留原 household_id，不随用户迁移 |
| HouseholdToRecipe.last_made | `household_id` + `recipe_id` | ❌ 新 Household 无记录（从空开始），旧 Household 记录保留 |
| RecipeShareToken | `group_id` | ✅ 保留（只要仍在同一 Group） |
| 权限 | `user_id` 本地字段 | ❌ 重置为新 Household 的默认值 |
| Mealplans / Shopping lists | mealplans 绑定 user_id；shopping_lists 绑定 household_id | ✅ Mealplans 随用户；Shopping lists 不迁移 |

**测试覆盖**：`test_user_can_rate_recipes_in_other_households` 和 `test_average_recipe_rating_includes_all_households`，见 [test_recipe_ratings.py#L373-L414](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py#L373-L414) — 验证跨 Household 可以评分/收藏，综合评分包含所有 Household 的用户。

### 6.4 场景 D：删除 Household

Household 删除仅 ORM 级联配置了 cascade 的关系（invite_tokens / preferences / recipe_actions / cookbooks / webhooks / group_event_notifiers），没有任何 secondary 关系级联。

**清理结果**：

| 关联数据 | 是否清理 |
|----------|----------|
| Household.users（User.household_id） | ❌ 指向已删除 household 的无效引用 |
| HouseholdToRecipe（所有 Recipe 的 last_made） | ❌ **残留** |
| UserToRecipe | ✅ 与 household 无关（通过 association_proxy 间接访问） |
| Mealplans | ✅（通过 GroupMealPlan.household_id cascade） |
| Shopping lists | ✅（有 cascade） |
| 配置了 cascade 的 preferences / invite_tokens 等 | ✅ |

### 6.5 场景 E：删除 Group

Group 删除级联几乎所有子对象（见 Group 模型的 `common_args`），但仍有漏洞：

| 关联数据 | 是否清理 |
|----------|----------|
| recipes / categories / tags / ... | ✅ 大部分配置了 cascade |
| RecipeShareToken | ✅ 绑定 group_id，有 cascade |
| UserToRecipe | ❌ **残留**（通过 association_proxy 绑定，非直接外键） |
| HouseholdToRecipe | ❌ **残留**（通过 association_proxy 绑定） |

---

## 7. 测试覆盖分析

| 功能 | 测试文件 | 覆盖内容 | 缺失覆盖 |
|------|---------|----------|----------|
| 收藏 CRUD | [test_recipe_ratings.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py) | 添加/移除收藏、评分、评分与收藏互相保留、Recipe 删除后评分清理、跨 Household 评分、综合评分计算 | 用户删除时的收藏/评分清理、外键约束阻断删除的情况 |
| last_made | [test_create_timeline_events.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/services_tests/scheduler/tasks/test_create_timeline_events.py) + [test_recipe_cross_household.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_cross_household.py#L250-L310) | Mealplan 自动更新时间线事件和 last_made、去重、保留未来日期、跨 Household 更新 last_made、全局值取 max | 删除 Recipe/Household 时 HouseholdToRecipe 残留、全局值删除后不回退 |
| 用户删除 | [test_user_repository.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/repository_tests/test_user_repository.py) | 用户目录被删除 | 有收藏/评分的用户删除时外键约束报错、UserToRecipe 残留 |
| Household Self Service | [test_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_household_tests/test_household_self_service.py) | 获取当前 Household 的 last_made、跨 Household 隔离 | set_household_recipe 的创建/更新路径 |

---

## 8. 总结与设计洞察

| 维度 | Favorites | recentRecipes | last_made | Shared Recipe |
|------|-----------|---------------|-----------|---------------|
| **实际含义** | 用户收藏 | 首页最近**创建**（不是浏览） | Household 维度"最近制作" | 临时公开共享 |
| **存储表** | `users_to_recipes` | 无（前端 ref 缓存） | `households_to_recipes` | `recipe_share_tokens` |
| **主键维度** | user_id + recipe_id | N/A | household_id + recipe_id | token_id |
| **ORM 关系类型** | secondary 多对多（无 cascade） | N/A | secondary 多对多（无 cascade） | 普通外键（有 cascade） |
| **数据库外键** | 无 ondelete（NO ACTION） | N/A | 无 ondelete（NO ACTION） | 无 ondelete（NO ACTION） |
| **全局同步机制** | 通过事件重算 Recipe.rating | N/A | 通过事件同步 max(last_made) 到 Recipe | N/A |
| **隐私边界** | 用户私有 | Group/Household 可见 | Household 私有 | Group 内创建，公开 URL 访问 |
| **时效控制** | 永久（手动取消） | 实时刷新 | 永久（手动更新 / mealplan 每日自动） | 默认 30 天过期（懒清理） |

| 删除场景 | UserToRecipe（收藏/评分） | HouseholdToRecipe（last_made） | ShareToken |
|----------|--------------------------|--------------------------------|------------|
| 删除 Recipe | ✅ 手动 `sa.delete()` 清理 | ❌ **残留** | ✅ ORM cascade |
| 删除 User | ❌ **残留（且可能外键阻断删除）** | ✅ 与 user 无关 | ✅ 与 user 无关 |
| 用户切换 Household | ✅ 保留 | ❌ 新 Household 为空 | ✅（同 Group 时）保留 |
| 删除 Household | ✅ 与 household 无关 | ❌ **残留** | ✅ 与 household 无关 |
| 删除 Group | ❌ **残留** | ❌ **残留** | ✅ ORM cascade |

**用户感觉"不直观"的核心原因**：
1. `recentRecipes` 命名误导，实际是"最近创建"而非"最近访问/浏览"，且系统完全没有访问历史记录
2. `last_made` 是 Household 维度而非 User 维度，同一用户在不同 Household 看到不同的"最近做过"状态
3. 收藏与评分共用同一条 `UserToRecipe` 行，语义耦合但 UI 上是两个独立操作
4. 所有 secondary 多对多关系（UserToRecipe、HouseholdToRecipe）都没有配置 cascade 或数据库级 `ON DELETE`：
   - 删除 Recipe 时手动清理了 UserToRecipe 却遗漏了 HouseholdToRecipe，清理逻辑不一致
   - 删除有收藏/评分的用户时，数据库外键约束（`ON DELETE NO ACTION`）可能直接阻断删除操作并抛出 IntegrityError，或（取决于 ORM flush 顺序）残留孤儿数据
5. 测试覆盖缺失：没有任何测试验证"有收藏/评分的用户被删除"时的行为
