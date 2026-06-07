# Favorites、recentRecipes、last_made 与 Shared Recipe 代码协作深度分析

## 1. 概念澄清：recentRecipes ≠ 最近浏览

**结论：Mealie 不存在任何"用户访问历史/最近浏览"功能。** 前端存在 `recentRecipes` 变量，但其语义是"首页最近 Recipe 列表"，与"浏览"毫无关系。

### 1.1 前端 `recentRecipes` 的真实含义

**核心文件**：[use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/composables/recipes/use-recipes.ts#L8-L160)

```typescript
export const allRecipes = ref<Recipe[]>([]);
export const recentRecipes = ref<Recipe[]>([]);  // 全局缓存

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

`recentRecipes` 命名高度误导，容易被理解为"最近浏览"。

### 1.2 真正存在的"时间"状态 — `last_made`（最近制作）

系统中唯一的用户时间维度状态是 **`last_made`**，存储在 **`HouseholdToRecipe`** 关联表中，维度是 **Household（家庭/组织）**，而非 User。

#### 两层 last_made 的协作关系

| 层级 | 存储位置 | 更新方式 | 含义 |
|------|----------|----------|------|
| Household 独立值 | `HouseholdToRecipe.last_made` | 用户手动标记 / mealplan 定时任务 | 该 Household "什么时候做过这个菜" |
| Recipe 全局值 | `RecipeModel.last_made` | SQLAlchemy 事件自动同步（所有 Household 的最大值） | 所有 Household 中最近一次的制作时间 |

**核心文件**：[household_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py#L39-L60)

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

> **潜在不一致**：`after_delete` 事件被触发时，`target.last_made` 仍是删除前的值，所以 `max()` 逻辑不会让全局值**回退**。如果最近制作的那个 Household 删除了记录，全局 `RecipeModel.last_made` 会一直保留那个"已被删除"的时间戳，直到有其他 Household 更新更大的值。

#### last_made 的三个写入入口

**入口 1：用户手动标记（Recipe 详情页 "我做过了" 按钮）**

前端组件 [RecipeLastMade.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/components/Domain/Recipe/RecipeLastMade.vue#L179-L191) 先显示 Recipe 全局 `lastMade` 兜底，再通过 API 获取当前 Household 的独立值覆盖：

```typescript
const lastMade = ref(props.recipe.lastMade);

onMounted(async () => {
  if (!auth.user?.value?.householdSlug) {
    lastMade.value = props.recipe.lastMade;
  } else {
    const { data } = await userApi.households.getCurrentUserHouseholdRecipe(props.recipe.slug || "");
    lastMade.value = data?.lastMade;
  }
});
```

用户点击按钮后调用 `PATCH /api/recipes/{slug}/last-made`，对应路由 [recipe_crud_routes.py#L568-L581](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/recipe_crud_routes.py#L568-L581)：

```python
@router.patch("/slug/{slug}/last-made", response_model=Recipe)
def update_recipe_last_made(self, slug: str):
    recipe = self.repos.recipes.get_one(slug)
    self.service.update_last_made(recipe, datetime.now(UTC))
    return self.repos.recipes.get_one(slug)
```

`RecipeService.update_last_made` 绕过权限检查（任何人都可以标记自己 Household 的 last_made），委托给 `HouseholdService.set_household_recipe` 写入 `HouseholdToRecipe`：

```python
def set_household_recipe(self, recipe_slug, data: HouseholdRecipeUpdate):
    recipe = self._get_recipe(recipe_slug)
    existing = self.repos.household_recipes.get_by_recipe(recipe.id)
    if existing:
        return self.repos.household_recipes.patch(existing.id, updated_data)  # UPDATE
    else:
        return self.repos.household_recipes.create(create_data)               # INSERT
```

**入口 2：Mealplan 定时任务自动更新**

**核心文件**：[create_timeline_events.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/scheduler/tasks/create_timeline_events.py#L25-L134)

调度器每天遍历所有 Group → Household → 当天的 mealplan：

```python
def _create_mealplan_timeline_events_for_household(event_time, session, group_id, household_id):
    # ...
    for mealplan in mealplans:
        if not (mealplan.recipe and mealplan.user_id):
            continue
        # ...
        # 去重：今天已经创建过该事件就跳过
        events = repos.recipe_timeline_events.page_all(pagination=query)
        if events.items:
            continue

        # 条件：当前没有 last_made 或 last_made 日期早于今天
        household_to_recipe = household_service.get_household_recipe(mealplan.recipe.slug)
        last_made = household_to_recipe.last_made if household_to_recipe else None
        if (not last_made or last_made.date() < event_time.date()) and mealplan.recipe_id not in recipes_to_update:
            recipes_to_update[mealplan.recipe_id] = mealplan.recipe

        # 同时创建 timeline event
        timeline_events_to_create.append(RecipeTimelineEventCreate(...))

    # 1. 批量写入 timeline events
    for event in timeline_events_to_create:
        repos.recipe_timeline_events.create(event)
    # 2. 逐个更新 HouseholdToRecipe.last_made + RecipeModel.last_made（全局）
    for recipe in recipes_to_update.values():
        household_service.set_household_recipe(recipe.slug, HouseholdRecipeUpdate(last_made=event_time))
        repos.recipes.patch(recipe.slug, {"last_made": event_time})  # 手动更新全局值（不依赖事件）
```

> 注意：定时任务同时写入了 `HouseholdToRecipe` 和 `RecipeModel.last_made`。前者的 `after_insert/after_update` 事件也会尝试更新全局值，但因为 `max()` 幂等，不会有冲突。

**入口 3：RepositoryRecipes 的列别名（排序用）**

`RepositoryRecipes.by_user(user_id)` 注入的 `last_made` 列别名优先取当前 Household 的值，否则回退到 `1900-01-01`（保证排序时排到最后），见 [repository_recipes.py#L36-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L36-L93)。

---

## 2. Favorites（收藏）与评分 — UserToRecipe 的 secondary 关系

### 2.1 数据模型与约束

**核心文件**：[user_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py)

```python
class UserToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "users_to_recipes"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id", name="user_id_recipe_id_rating_key"),)

    user_id = Column(GUID, ForeignKey("users.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    rating = Column(Float, index=True, nullable=True)
    is_favorite = Column(Boolean, index=True, nullable=False)  # 收藏标志

    # 通过 association_proxy 从 Recipe 间接获取 group_id/household_id
    group_id: AssociationProxy[GUID] = association_proxy("recipe", "group_id")
    household_id: AssociationProxy[GUID] = association_proxy("recipe", "household_id")
```

### 2.2 SQLAlchemy secondary 关系配置

收藏和评分都通过 `secondary=UserToRecipe.__tablename__` 实现多对多关系，两侧分别在 `User` 和 `RecipeModel` 中定义。

**User 侧**，见 [users.py#L103-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L103-L115)：

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

**RecipeModel 侧**，见 [recipe.py#L62-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L62-L74)：

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

> **关键细节**：
> 1. `overlaps` 参数告诉 SQLAlchemy：`rated_recipes` 和 `favorite_recipes` 实际上共享同一张 secondary 表，不要试图重复操作同一行。
> 2. `favorite_recipes` / `favorited_by` 通过 `primaryjoin` 条件 `UserToRecipe.is_favorite==True` 实现"只看收藏"的视图，但**仍然复用同一条 `UserToRecipe` 行**（与 rating 共用）。
> 3. **所有四个关系都没有配置 cascade** —— 这意味着 ORM 级联删除不会自动清理 `UserToRecipe`。

### 2.3 Recipe 综合评分的事件驱动重算

**核心文件**：[user_to_recipe.py#L36-L53](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py#L36-L53) + [recipe.py#L284-L300](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L284-L300)

**事件链路**：
1. `UserToRecipe` 发生 `after_insert` / `after_update` / `after_delete` 事件
2. 监听器把目标 Recipe 的 `rating` 设为 `-1` 作为"脏标记"
3. RecipeModel 的 `before_update` 事件检测到 `rating` 被修改，触发实际重算：

```python
@event.listens_for(RecipeModel, "before_update")
def calculate_rating(mapper, connection, target: RecipeModel):
    session = object_session(target)
    if not (session and session.is_modified(target, "rating")):
        return
    history = get_history(target, "rating")
    old_value = history.deleted[0] if history.deleted else None
    new_value = history.added[0] if history.added else None
    if old_value == new_value:
        return

    # 重算：从 UserToRecipe 表取 AVG(rating)，排除 rating 为空或 <=0 的行
    target.rating = (
        session.query(sa.func.avg(UserToRecipe.rating))
        .filter(
            UserToRecipe.recipe_id == target.id,
            UserToRecipe.rating is not None,
            UserToRecipe.rating > 0,
        )
        .scalar()
    )
```

### 2.4 收藏列表的查询方式

前端收藏页 [favorites.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/pages/user/[id]/favorites.vue#L31) 通过 QueryFilter 字符串过滤：

```typescript
const query = { queryFilter: `favoritedBy.id = "${userId}"` };
```

后端 `QueryFilterBuilder` 将其解析为跨关系 JOIN：`RecipeModel.favorited_by → User.id`，最终生成 SQL，见 [builder.py#L214-L276](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/query_filter/builder.py#L214-L276)。

---

## 3. Shared Recipe（共享 Token）— Group 边界与时效

### 3.1 数据模型

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

### 3.2 边界校验

**创建侧（登录态）**：[routes/shared/__init__.py#L34-L42](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/shared/__init__.py#L34-L42)

```python
def create_one(self, data: RecipeShareTokenCreate) -> RecipeShareToken:
    group_repos = get_repositories(self.repos.session, group_id=self.group_id, household_id=None)
    recipe = group_repos.recipes.get_one(data.recipe_id, "id")
    # 强制：只能共享同 Group 内的 Recipe
    if recipe is None or recipe.group_id != self.group_id:
        raise HTTPException(status_code=404, detail="Recipe not found in your group")
    save_data = RecipeShareTokenSave(**data.model_dump(), group_id=self.group_id)
    return self.mixins.create_one(save_data)
```

**访问侧（公开）**：[shared_routes.py#L26-L34](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py#L26-L34)

```python
token_summary = db.recipe_share_tokens.get_one(token_id)
if token_summary and token_summary.is_expired:
    # 懒清理：过期 Token 访问时直接删除
    try:
        db.recipe_share_tokens.delete(token_id)
        session.commit()
    except Exception:
        session.rollback()
    token_summary = None

if token_summary is None:
    raise HTTPException(status_code=404, detail="Token Not Found")
```

> 共享边界两阶段：①创建时必须同 Group；②访问时无 Group/用户身份限制（公开 URL 即公开访问），仅靠 `expires_at` 控制时效窗口。

---

## 4. 外键约束与级联配置

### 4.1 数据库级外键：完全没有 `ON DELETE CASCADE`

在所有 Alembic 迁移和 SQLAlchemy `ForeignKey` 定义中，`users_to_recipes`、`households_to_recipes`、`recipe_share_tokens` 等关联表的外键**均未配置 `ondelete` 参数**，即数据库层面默认是 `ON DELETE NO ACTION`（严格模式）。

这意味着：
- 如果尝试直接 SQL `DELETE FROM users WHERE id = ...`，会因 `users_to_recipes.user_id` 外键约束直接报错
- 所有删除必须由 SQLAlchemy ORM 先行处理，或业务层手动 DELETE 关联表

### 4.2 ORM 级联关系一览

| 关系 | 定义侧 | cascade 配置 | 说明 |
|------|--------|-------------|------|
| User → tokens/comments/recipe_timeline_events/password_reset_tokens | [users.py#L84-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L84-L93) | `all, delete, delete-orphan` | 删除用户时级联清理 |
| User → owned_recipes | [users.py#L95-L98](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L95-L98) | 无 | 删除用户后，Recipe.user_id 变为无效引用 |
| User → rated_recipes/favorite_recipes | [users.py#L103-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L103-L115) | 无（secondary） | 删除用户时 **不清理** UserToRecipe |
| RecipeModel → share_tokens/comments/timeline_events/settings/... | [recipe.py#L96-L132](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L96-L132) | `all, delete, delete-orphan` | 删除 Recipe 时级联清理 |
| RecipeModel → rated_by/favorited_by | [recipe.py#L62-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L62-L74) | 无（secondary） | 删除 Recipe 时 **不自动清理** UserToRecipe（需要手动） |
| RecipeModel → made_by | [recipe.py#L148-L150](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L148-L150) | 无（secondary） | 删除 Recipe 时 **不自动清理** HouseholdToRecipe |
| Household → made_recipes | [household.py#L69-L71](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household.py#L69-L71) | 无（secondary） | 删除 Household 时 **不清理** HouseholdToRecipe |
| Group → recipes | [group.py#L63](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/group/group.py#L63) | 无 | 删除 Group 时 Recipe 残留（但 Group 删除本身很少发生） |

---

## 5. 成员退出 / 删除时的实际清理边界

### 5.1 场景 A：删除 Recipe

**核心文件**：[repository_recipes.py#L110-L153](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L110-L153)

```python
def _delete_recipe(self, recipe: RecipeModel) -> Recipe:
    recipe_as_model = self.schema.model_validate(recipe)

    # Step 1：手动 DELETE 所有 UserToRecipe（PostgreSQL 级联 secondary 表有 bug）
    try:
        user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
        self.session.execute(user_to_recipe_delete_query)
        self.session.commit()
    except Exception:
        self.session.rollback()
        raise

    # Step 2：session.delete(recipe) → 触发 ORM 级联
    #   share_tokens/comments/timeline_events/settings/nutrition/assets/notes/...
    #   → 这些都配置了 cascade="all, delete, delete-orphan"
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
| UserToRating 触发 Recipe 综合评分重算 | ❌ 无意义 | Recipe 已被删除 |
| HouseholdToRecipe（所有 Household 的 last_made） | ❌ **残留** | 没有手动清理，也没有 ORM 级联（secondary 无 cascade） |
| ShareTokens | ✅ | ORM cascade |
| Comments/TimelineEvents/Assets/Notes/... | ✅ | ORM cascade |
| Meal entries（GroupMealPlan.recipe_id） | ✅ | ORM cascade (`meal_entries` 配置了 cascade) |

> **已知缺陷**：删除 Recipe 后 `HouseholdToRecipe` 行会残留数据库，因为：①没手动删；②secondary 关系无 cascade；③数据库外键也无 `ON DELETE CASCADE`。这些行会通过 `recipe_id` 指向已不存在的 Recipe。

### 5.2 场景 B：删除 User

**核心文件**：[repository_users.py#L55-L65](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_users.py#L55-L65)

```python
def delete(self, value: str | UUID4, match_key: str | None = None) -> User:
    if settings.IS_DEMO:
        user_to_delete = self.get_one(value, match_key)
        if user_to_delete and user_to_delete.is_default_user:
            return user_to_delete

    entry = super().delete(value, match_key)  # RepositoryGeneric.delete → session.delete(user)
    shutil.rmtree(PrivateUser.get_directory(value))  # 删除用户私有文件目录
    return entry
```

`session.delete(user)` 触发的 ORM 级联仅限于 User 模型配置了 cascade 的关系。

**清理结果**：

| 关联数据 | 是否清理 | 方式 |
|----------|----------|------|
| LongLiveToken | ✅ | ORM cascade (`sp_args`) |
| RecipeComment | ✅ | ORM cascade |
| RecipeTimelineEvent | ✅ | ORM cascade |
| PasswordResetToken | ✅ | ORM cascade |
| GroupMealPlan（mealplans） | ✅ | ORM cascade (`sp_args`) |
| ShoppingList（shopping_lists） | ✅ | ORM cascade (`sp_args`) |
| **UserToRecipe（该用户所有收藏+评分）** | ❌ **残留** | 无 ondelete、无 ORM cascade、无手动清理 |
| 用户创建的 Recipe（owned_recipes） | ❌ **残留** | 无 cascade，Recipe.user_id 变为无效引用 |
| RecipeShareToken | ✅ 与 user 无关 | Token 绑定 group_id，不受用户删除影响 |
| 用户私有文件目录 | ✅ | `shutil.rmtree` 手动删除 |

**残留 UserToRecipe 的后果**：
1. 数据库中存在孤儿行（`user_id` 指向不存在的用户）
2. Recipe 综合评分 `RecipeModel.rating` 在下次有人评分触发 `before_update` 重算前，仍包含已删除用户的评分值
3. 收藏查询（`favoritedBy.id = X`）不会出错，因为用户 X 已不存在，JOIN 自然不命中

### 5.3 场景 C：用户切换 Household（成员迁移）

User.household_id 从旧值改为新值时：

| 关联数据 | 绑定维度 | 切换后的行为 |
|----------|----------|--------------|
| UserToRecipe（收藏+评分） | `user_id` | ✅ 完全保留，与 household_id 无关 |
| 用户拥有的 Recipe | `user_id` + `group_id` + 独立 `household_id` | ✅ 保留原 household_id，不随用户迁移 |
| HouseholdToRecipe.last_made | `household_id` + `recipe_id` | ❌ 新 Household 无记录（从空开始），旧 Household 记录保留 |
| RecipeShareToken | `group_id` | ✅ 保留（只要仍在同一 Group） |
| 权限（can_manage_household 等） | `user_id` 本地字段 | ❌ 重置为新 Household 的默认值（由管理员配置） |
| Mealplans/Shopping lists | `user_id` / `household_id` | ✅ Mealplans 绑定 user_id 保留；Shopping lists 绑定 household_id 不迁移 |

### 5.4 场景 D：删除 Household

Household 删除仅 ORM 级联配置了 cascade 的关系（invite_tokens/preferences/recipe_actions/cookbooks/webhooks/...），没有任何 secondary 关系级联。

**清理结果**：

| 关联数据 | 是否清理 |
|----------|----------|
| Household.users | ❌ User.household_id 变为无效引用（但 User 仍在） |
| HouseholdToRecipe（所有 Recipe 的 last_made） | ❌ **残留** |
| UserToRecipe | ✅ 与 household 无关 |
| Mealplans | ✅（如果通过 GroupMealPlan.household_id） |
| 配置了 cascade 的 preferences/invite_tokens 等 | ✅ |

### 5.5 场景 E：删除 Group

Group 删除级联几乎所有子对象（见 [group.py#L66-L86](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/group/group.py#L66-L86) 的 `common_args`），但仍有漏洞：

| 关联数据 | 是否清理 |
|----------|----------|
| recipes/categories/tags/... | ✅ 大部分配置了 cascade |
| RecipeShareToken | ✅ 绑定 group_id，有 cascade |
| UserToRecipe | ❌ **残留**（通过 association_proxy 绑定，非直接外键） |
| HouseholdToRecipe | ❌ **残留**（通过 association_proxy 绑定） |

---

## 6. 总结与设计洞察

| 维度 | Favorites | recentRecipes | last_made | Shared Recipe |
|------|-----------|---------------|-----------|---------------|
| **实际含义** | 用户收藏 | 首页最近**创建**（不是浏览） | Household 维度"最近制作" | 临时公开共享 |
| **存储表** | `users_to_recipes` | 无（前端 ref 缓存） | `households_to_recipes` | `recipe_share_tokens` |
| **主键维度** | user_id + recipe_id | N/A | household_id + recipe_id | token_id |
| **与 ORM 关系** | secondary 多对多（无 cascade） | N/A | secondary 多对多（无 cascade） | 普通外键（有 cascade） |
| **数据库外键** | 无 ondelete | N/A | 无 ondelete | 无 ondelete |
| **全局同步机制** | 通过事件重算 Recipe.rating | N/A | 通过事件同步 max(last_made) 到 Recipe | N/A |
| **隐私边界** | 用户私有 | Group/Household 可见 | Household 私有 | Group 内创建，公开 URL 访问 |
| **时效控制** | 永久（手动取消） | 实时刷新 | 永久（手动更新 / mealplan 每日自动） | 默认 30 天过期（懒清理） |

| 删除场景 | UserToRecipe（收藏/评分） | HouseholdToRecipe（last_made） | ShareToken |
|----------|--------------------------|--------------------------------|------------|
| 删除 Recipe | ✅ 手动 `sa.delete()` 清理 | ❌ **残留** | ✅ ORM cascade |
| 删除 User | ❌ **残留** | ✅ 与 user 无关 | ✅ 与 user 无关 |
| 用户切换 Household | ✅ 保留 | ❌ 新 Household 为空 | ✅（同 Group 时）保留 |
| 删除 Household | ✅ 与 household 无关 | ❌ **残留** | ✅ 与 household 无关 |
| 删除 Group | ❌ **残留** | ❌ **残留** | ✅ ORM cascade |

**用户感觉"不直观"的核心原因**：
1. `recentRecipes` 命名误导，实际是"最近创建"而非"最近访问/浏览"，且系统完全没有访问历史记录
2. `last_made` 是 Household 维度而非 User 维度，同一用户在不同 Household 看到不同的"最近做过"状态
3. 收藏与评分共用同一条 `UserToRecipe` 行，语义耦合但 UI 上是两个独立操作
4. 所有 secondary 多对多关系（UserToRecipe、HouseholdToRecipe）都没有配置 cascade 或数据库级 `ON DELETE`，在删除 User/Recipe/Household/Group 时多处残留孤儿数据
5. 删除 Recipe 时手动清理了 UserToRecipe 却遗漏了 HouseholdToRecipe，清理逻辑不一致
