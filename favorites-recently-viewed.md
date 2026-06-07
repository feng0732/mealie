# Favorites、Recently Viewed 与 Shared Recipe 代码协作分析

## 1. 核心数据模型与状态存储

### 1.1 Favorites（收藏）— 用户维度

收藏状态存储在 **`UserToRecipe`** 关联表中，是用户与 Recipe 的多对多关系表。

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
- 用户侧：`User.favorite_recipes` — 通过 `primaryjoin` 条件 `is_favorite==True` 过滤，见 [users.py#L109-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L109-L115)
- Recipe 侧：`RecipeModel.favorited_by` — 反向关系，见 [recipe.py#L68-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L68-L74)

> **边界要点**：`UniqueConstraint("user_id", "recipe_id")` 确保同一用户对同一 Recipe 只有一条记录，收藏和评分共用同一条关联行。

---

### 1.2 Recently Viewed（最近访问）— 实际不存在

**重要发现**：在整个代码库中搜索 `recently`、`recent_view`、`last_viewed` 均 **无匹配结果**。Mealie 当前 **没有实现 "最近访问" 功能**。

最接近的概念是 **`HouseholdToRecipe.last_made`**（最近制作时间），但这是 Household 维度的"最近烹饪记录"，而非用户维度的"最近访问"。

**核心文件**：[household_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py)

```python
class HouseholdToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "households_to_recipes"
    household_id = Column(GUID, ForeignKey("households.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    last_made: Mapped[datetime | None] = mapped_column(NaiveDateTime)  # 最近制作时间
```

> **用户感知差异**：前端/API 层面用户可能期望"最近访问"，但实际存储的是"最近制作"，且维度是 Household 而非 User。这正是用户反馈"读起来不直观"的根源。

---

### 1.3 Shared Recipe（共享 Recipe）— Group 维度 + 时效

共享通过 **`RecipeShareTokenModel`** 实现，基于带过期时间的 Token。

**核心文件**：[shared.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/shared.py)

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    id: Mapped[GUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    group_id: FilterableColumn[GUID]  # 绑定到 Group
    recipe_id: FilterableColumn[GUID]
    expires_at: FilterableColumn[datetime]  # 默认 30 天后过期
```

**Recipe 级联关系**：`RecipeModel.share_tokens` 配置了 `cascade="all, delete, delete-orphan"`，见 [recipe.py#L122-L124](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L122-L124)

---

## 2. API 端点与协作流程

### 2.1 Favorites API

**核心文件**：[ratings.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/users/ratings.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/users/{id}/favorites` | 获取用户收藏列表 |
| POST | `/users/{id}/favorites/{slug}` | 添加收藏 |
| DELETE | `/users/{id}/favorites/{slug}` | 移除收藏 |
| POST | `/users/{id}/ratings/{slug}` | 设置评分+收藏（共用） |

**协作逻辑**（`set_rating` 方法，[ratings.py#L54-L76](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/users/ratings.py#L54-L76)）：

1. 先调用 `assert_user_change_allowed` 校验权限（只能改自己的）
2. 查询 Recipe 是否存在（跨 Household 可查，但限制在同 Group）
3. 查询 `UserToRecipe` 是否已存在记录：
   - **不存在** → 创建新记录，写入 `rating` + `is_favorite`
   - **已存在** → 更新对应字段
4. SQLAlchemy 事件监听器自动触发 Recipe 评分重算，见 [user_to_recipe.py#L46-L53](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py#L46-L53)

### 2.2 Shared Recipe API

共享分两类端点：

**用户侧（管理 Token）**：[routes/shared/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/shared/__init__.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/shared/recipes` | 列出当前用户 Group 的共享 Token |
| POST | `/shared/recipes` | 创建共享 Token（校验 Recipe 属于同 Group） |
| GET | `/shared/recipes/{item_id}` | 获取单个 Token 详情 |
| DELETE | `/shared/recipes/{item_id}` | 撤销共享 Token |

**公开侧（无需登录）**：[shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py)

| 方法 | 端点 | 功能 |
|------|------|------|
| GET | `/shared/{token_id}` | 通过 Token 获取 Recipe |
| GET | `/shared/{token_id}/zip` | 通过 Token 下载 Recipe 压缩包 |

**过期自动清理逻辑**：访问共享 Token 时，若 `is_expired` 为 True 则先删除 Token 再返回 404，见 [shared_routes.py#L26-L34](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/shared_routes.py#L26-L34)

---

## 3. 列表排序与查询过滤

### 3.1 收藏列表的查询方式

前端收藏页面使用 QueryFilter 字符串进行过滤：

**核心文件**：[favorites.vue](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/pages/user/[id]/favorites.vue#L31)

```typescript
const query = { queryFilter: `favoritedBy.id = "${userId}" };
```

该过滤字符串由 **`QueryFilterBuilder`** 解析，支持链式属性访问（`favoritedBy.id` 会遍历 `RecipeModel.favorited_by` 关系到 `User.id`），见 [builder.py#L214-L276](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/query_filter/builder.py#L214-L276)

### 3.2 用户维度的列别名（排序关键）

`RepositoryRecipes.by_user(user_id)` 会注入两个计算列别名，这是用户维度状态参与排序的核心机制：

**核心文件**：[repository_recipes.py#L36-L93](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L36-L93)

```python
def by_user(self, user_id):
    self.user_id = user_id
    return self

@property
def column_aliases(self):
    return {
        "last_made": self._get_last_made_col_alias(),   # 当前 Household 的 last_made，否则 1900-01-01
        "rating": self._get_rating_col_alias(),          # 用户个人评分优先，否则 Recipe 综合评分
    }
```

这两个别名在 QueryFilterBuilder 中会被优先使用（覆盖原始列），因此用户可以按"我上次做的时间"和"我的评分"排序。

### 3.3 默认排序

`page_all` 方法在无搜索和无指定排序时，默认按 `created_at` 降序，见 [repository_recipes.py#L270-L272](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L270-L272)

---

## 4. 隐私保留与访问边界

### 4.1 三层权限边界

| 层级 | 控制项 | 位置 |
|------|--------|------|
| Recipe 设置 | `public`、`locked`、`disable_comments` | [recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/schema/recipe/recipe_settings.py) |
| Household 偏好 | `lock_recipe_edits_from_other_households`、`recipe_public` 默认值 | HouseholdPreferences |
| Group 偏好 | `private_group` 控制默认 Household 偏好 | GroupPreferences |

### 4.2 更新权限判断逻辑

`RecipeService.can_update` 使用原生 SQL 判断是否可编辑，综合考虑：
- 是否是所有者
- Recipe 是否被锁定
- 跨 Household 编辑策略，见 [recipe_service.py#L87-L131](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L87-L131)

### 4.3 删除权限

仅 **所有者** 或 **管理员** 可删除 Recipe，协作编辑权限不适用于删除，见 [recipe_service.py#L70-L85](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L70-L85)

---

## 5. 成员退出后的状态处理（级联与清理）

### 5.1 删除 Recipe 时的清理

**核心文件**：[repository_recipes.py#L110-L153](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L110-L153)

```python
def _delete_recipe(self, recipe):
    # 1. 先手动删除 UserToRecipe 关联（所有用户的收藏+评分记录）
    user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
    self.session.execute(user_to_recipe_delete_query)
    
    # 2. 再删除 Recipe 本身（级联删除 share_tokens、comments、timeline_events 等）
    self.session.delete(recipe)
```

> **注意**：PostgreSQL 无法正确级联多对多关联表，因此必须 **先手动删除 UserToRecipe**，否则会报错。这是 `delete_many` 中逐行删除而非批量删除的原因。

### 5.2 删除 User 时的清理

**核心文件**：[repository_users.py#L55-L65](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_users.py#L55-L65)

```python
def delete(self, value, match_key=None):
    entry = super().delete(value, match_key)  # 调用 RepositoryGeneric.delete
    shutil.rmtree(PrivateUser.get_directory(value))  # 删除用户文件目录
    return entry
```

**User 模型级联配置**，见 [users.py#L84-L102](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L84-L102)：

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

**关键缺失**：
- `UserToRecipe` 表的外键 **没有配置 `ondelete` 级联**，且 User 模型也没有配置对 `rated_recipes`/`favorite_recipes` 的 cascade
- 因此 **删除用户后，其 UserToRecipe 记录（收藏+评分）会残留在数据库中**，成为孤立数据
- Recipe 的 `rating` 字段（综合评分）在下次有人更新评分时会通过 `before_update` 事件重算，但中间可能出现包含失效用户评分的不准确值

### 5.3 Household 变更（成员迁移/退出）时的处理

`HouseholdToRecipe.last_made` 是 **Household 维度** 的数据：
- 用户切换 Household 后，新的 Household 没有该用户的 `last_made` 记录
- 旧 Household 的记录保留（因为是按 household_id 存储的）
- Recipe 的 `last_made` 字段是所有 Household 的最大值，通过 SQLAlchemy 事件更新，见 [household_to_recipe.py#L39-L60](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py#L39-L60)

用户变更 Household 时：
- 其 UserToRecipe（收藏/评分）**不受影响**，因为绑定 user_id，与 household_id 无关（通过 association_proxy 获取）
- 其拥有的 Recipe 的 user_id 不变（Recipe 通过 user_id 关联所有者）

### 5.4 删除 Group/Household 的级联风险

代码库中未显式配置数据库级 `ON DELETE CASCADE`，完全依赖 SQLAlchemy 的 ORM 级联。批量删除 Group/Household 可能出现：
- `UserToRecipe`、`HouseholdToRecipe` 残留孤立记录
- 需要通过管理端的 data migration 脚本手动清理（见 `db/fixes/` 目录）

---

## 6. 总结与设计洞察

| 维度 | Favorites | Recently Viewed | Shared Recipe |
|------|-----------|-----------------|---------------|
| **存储表** | `users_to_recipes` | 不存在（只有 `households_to_recipes.last_made`） | `recipe_share_tokens` |
| **主键维度** | user_id + recipe_id | N/A | token_id（独立） |
| **隐私边界** | 用户私有 | N/A | Group 内创建，公开访问（带 Token） |
| **时效控制** | 永久（手动取消） | N/A | 默认 30 天过期 |
| **排序参与** | 通过 QueryFilter 过滤，默认 `created_at` | N/A | 不参与列表排序 |
| **删除清理** | Recipe 删除时手动清理；**用户删除时残留** | N/A | Recipe 删除时级联；访问时自动清理过期 Token |
| **成员退出影响** | 收藏保留（user_id 绑定） | N/A | 共享 Token 保留（group_id 绑定） |

**关键不直观之处**：
1. "最近访问"功能缺失，用户可能混淆为"最近制作"
2. Favorites 和 Ratings 共用同一张表、同一条记录，语义上耦合
3. 删除用户后其收藏/评分记录残留，可能影响 Recipe 综合评分准确性
