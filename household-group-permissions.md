# Mealie Household 与 Group 权限边界深度分析

> **可信度标记说明：**
> - **[代码支撑]**：有明确的 ORM 模型定义、迁移文件或路由/服务层代码作为证据
> - **[推测]**：基于代码架构的合理推断，未经运行时验证
> - **[风险]**：发现的潜在安全/数据一致性问题，有代码支撑

---

## 一、层级结构与数据模型关系

### 1.1 实体层级

```
Group (组)
  └── Household (家庭) ← 一个 Group 可包含多个 Household
        └── User (用户) ← 一个 User 只属于一个 Group 和一个 Household
```

### 1.2 核心模型关系

**[代码支撑] Group** 模型（`mealie/db/models/group/group.py`）：
- `id`: 主键
- `name`: 组名（全局唯一）
- `slug`: 组 URL 标识（全局唯一）
- `households`: 一对多关系，指向该 Group 下的所有 Household
- `users`: 一对多关系，指向该 Group 下的所有 User
- `recipes`: 一对多关系，该 Group 下的所有 Recipe
- `mealplans`, `shopping_lists`, `cookbooks` 等：Group 层面的资源聚合关系

**[代码支撑] Household** 模型（`mealie/db/models/household/household.py`）：
- `id`: 主键
- `name`: 家庭名（在 Group 内唯一）
- `slug`: 家庭 URL 标识（在 Group 内唯一）
- `group_id`: 外键，指向所属 Group
- `group`: 多对一关系
- `users`: 一对多关系，该 Household 下的所有 User
- `preferences`: 一对一关系，Household 偏好设置
- `made_recipes`: 多对多关系（通过 `HouseholdToRecipe` 关联表），记录哪些 Household "做过" 哪些 Recipe

**[代码支撑] User** 模型（`mealie/db/models/users/users.py#L60-L68`）：
- `group_id`: 外键（NOT NULL），所属 Group
- `household_id`: 外键（可 NULL），所属 Household
- `group` / `household`: 多对一关系
- 权限标志字段：`admin`, `can_manage_household`, `can_manage`, `can_invite`, `can_organize`

---

## 二、用户角色与权限体系

### 2.1 权限标志位定义（路由层实际使用核对）

**[代码支撑]** 所有用户权限存储在 `users` 表的布尔字段中（`mealie/db/models/users/users.py#L60-L82`）。
下表中的「实际控制功能」已通过路由层的全部调用点逐一核对：

| 权限标志 | 数据库字段 | 实际控制功能（路由层调用点） | **不**控制的功能（常见误解） |
|---------|-----------|--------------------------|---------------------------|
| `admin` | `admin` | 系统全局超级管理员。可：① 绕过 Repository 的 group_id/household_id 过滤（通过 `BaseAdminController`）；② 列出所有邀请令牌；③ 为其他 Group/Household 创建邀请；④ 删除任意用户；⑤ 修改任意用户的 group/household 归属；⑥ 删除空 Household。 | — |
| `can_manage_household` | `can_manage_household` | **仅控制一项**：修改当前 Household 的偏好设置（`private_household`、`lock_recipe_edits_from_other_households` 等），调用点：`controller_household_self_service.py#L60` | ❌ 不控制成员权限调整（那是 `can_manage`）；❌ 不控制 Household 创建删除（那是 Admin 专属） |
| `can_manage` | `can_manage` | 两项功能：① **调整同 Household 其他成员的四个权限标志**（`can_invite`、`can_manage`、`can_manage_household`、`can_organize`），调用点：`controller_household_self_service.py#L66`，集成测试确认：`test_household_permissions.py#L20-L56`；② 管理 Group AI Provider（设置 + Provider 本身的 CRUD），调用点：`controller_group_ai_providers.py`（共 6 处） | ❌ 不控制 Tag/Category/工具/单位管理（那些是 `can_organize` 或无权限检查） |
| `can_organize` | `can_organize` | Group 级「组织者」资源的增删改：① Recipe Tag（创建/更新/删除，3 处）；② Recipe Category（创建/更新/删除，3 处）；③ Ingredient Food（创建/合并/更新/删除，4 处）。调用文件：`controller_tags.py`、`controller_categories.py`、`foods.py` | ❌ 不控制 Cookbook（Cookbook 无任何权限检查）；❌ 不控制 Tool（无权限检查）；❌ 不控制 Unit（无权限检查）；❌ 不控制 Multi-purpose Label（无权限检查） |
| `can_invite` | `can_invite` | 邀请功能：① 创建邀请令牌（直接检查 `self.user.can_invite`：`controller_invitations.py#L42-L46`）；② 发送邀请邮件（直接检查：`controller_invitations.py#L75-L79`）。注意：**从未通过 `checks.can_invite()` 调用**，始终直接检查字段。 | 邀请令牌列表查询不由 `can_invite` 控制，而是 `self.user.admin`（`controller_invitations.py#L25-L29`）。跨 Group/Household 创建邀请也需要 Admin。 |

### 2.2 无需权限检查即可操作的资源

**[代码支撑]** 以下资源的创建/更新/删除接口**无任何显式权限检查**，同 Group 内任意已登录用户均可操作：

| 资源 | 控制器文件 | 说明 |
|-----|-----------|------|
| Cookbook（食谱书） | `mealie/routes/households/controller_cookbooks.py` | 列表按 Group 查询；创建/更新/删除均无检查 |
| Recipe Tool（工具） | `mealie/routes/organizers/controller_tools.py` | 全部 CRUD 无权限检查 |
| Ingredient Unit（单位） | `mealie/routes/unit_and_foods/units.py` | 全部 CRUD + 合并均无权限检查 |
| Multi-purpose Label（多用途标签） | `mealie/routes/groups/controller_labels.py` | 全部 CRUD 无权限检查 |

### 2.3 权限联动规则

**[代码支撑]** 在 `_set_permissions()` 方法中（`mealie/db/models/users/users.py`）：
- 若 `admin=True`，则自动授予 `can_manage_household`、`can_manage`、`can_invite`、`can_organize` 全部为 `True`
- Admin 权限是唯一可以跨 Group/Household 的角色

### 2.4 权限设置 Schema

**[代码支撑]** `mealie/schema/household/household_permissions.py` 中定义：
```python
class SetPermissions(MealieModel):
    user_id: UUID4
    can_manage_household: bool = False
    can_manage: bool = False
    can_invite: bool = False
    can_organize: bool = False
```
注意：Admin 权限不在此 Schema 中，只能通过其他途径授予。

---

## 三、授权入口与认证流程

### 3.1 认证核心入口

**[代码支撑]** `mealie/core/dependencies/dependencies.py`：

- `get_current_user()`（L88-L123）：支持 OAuth2 Bearer Token 和 Cookie 两种方式，反序列化后返回 `PrivateUser` 对象
- `get_admin_user()`（L135-L138）：在 `get_current_user()` 基础上额外校验 `admin=True`，不满足则抛 403
- `get_public_group()`（L67-L74）：用于公开 API，检查 group 的 `private_group` 设置

### 3.2 控制器基类与 Repository 注入

**[代码支撑]** `mealie/routes/_base/base_controllers.py`：

| 控制器基类 | Repository 初始化参数 | 效果 |
|-----------|---------------------|------|
| `BaseUserController` | `group_id=self.user.group_id`, `household_id=self.user.household_id` | 按用户当前 Group+Household 双维度过滤 |
| `BaseAdminController` | `group_id=None`, `household_id=None` | **绕过所有范围过滤**，可见全部数据 |

这是权限体系中最关键的设计：Admin 角色通过使用不带过滤参数的 Repository 获得全局访问权。

### 3.3 Repository 三级过滤机制

**[代码支撑]** `mealie/repos/repository_generic.py`：

```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

三级继承关系：
1. `RepositoryGeneric`：无任何过滤（基类）
2. `GroupRepositoryGeneric`：自动按 `group_id` 过滤
3. `HouseholdRepositoryGeneric`：自动按 `group_id` + `household_id` 过滤

### 3.4 各资源类型的 Repository 范围分类

**[代码支撑]** `mealie/repos/repository_factory.py`：

| 资源类型 | Repository 基类 | 可见范围 |
|---------|----------------|---------|
| Recipes | `HouseholdRepositoryGeneric`（但支持 household_id=None 跨 Household 查询） | 默认 Household 级，可提升至 Group 级 |
| Cookbooks | `HouseholdRepositoryGeneric` | Household 级 |
| MealPlans | `HouseholdRepositoryGeneric` | Household 级 |
| ShoppingLists | `HouseholdRepositoryGeneric` | Household 级 |
| Categories | `GroupRepositoryGeneric` | Group 级 |
| Tags | `GroupRepositoryGeneric` | Group 级 |
| Tools | `GroupRepositoryGeneric` | Group 级 |
| Comments | `GroupRepositoryGeneric` | Group 级（用户可对跨 Household Recipe 评论） |
| Users | `GroupRepositoryGeneric` | Group 级 |
| HouseholdPreferences | `GroupRepositoryGeneric` | Group 级 |
| Ratings/Favorites | `GroupRepositoryGeneric` | Group 级 |

### 3.5 权限检查辅助类

**[代码支撑]** `mealie/routes/_base/checks.py` 中定义了 `OperationChecks` 类，提供以下方法（权限不足则抛 HTTP 403）：

| 检查方法 | 实际在路由中被调用的次数 | 说明 |
|---------|------------------------|------|
| `can_manage_household()` | 1 次 | 仅用于修改 Household 偏好设置 |
| `can_manage()` | 7 次 | 成员权限调整（1 次）+ AI Provider 管理（6 次） |
| `can_organize()` | 10+ 次 | Tag/Category/Food 的增删改 |
| `can_invite()` | **0 次** | 定义了但从未被调用。邀请功能通过直接检查 `self.user.can_invite` 字段实现 |

---

## 四、核心数据归属模型（重点）

### 4.1 两种数据归属模式：静态 vs 动态

**[代码支撑]** 这是权限体系中最容易被忽略的核心设计：

| 数据模型 | group_id 实现 | household_id 实现 | user_id 实现 |
|---------|-------------|-----------------|-------------|
| **Recipe** | 实际列（外键，NOT NULL） | **AssociationProxy**（动态代理） | 实际列（外键，可 NULL） |
| **GroupMealPlan** | 实际列（外键） | **AssociationProxy**（动态代理） | 实际列（外键，可 NULL） |
| **ShoppingList** | 实际列（外键，NOT NULL） | **AssociationProxy**（动态代理） | 实际列（外键，NOT NULL） |
| **CookBook** | 实际列（外键） | 实际列（外键，静态存储） | 无 user_id |
| **RecipeComment** | **AssociationProxy** | **AssociationProxy** | 实际列（外键，NOT NULL） |
| **RecipeTimelineEvent** | **AssociationProxy** | **AssociationProxy** | 实际列（外键，NOT NULL） |
| **LongLiveToken** | **AssociationProxy** | **AssociationProxy** | 实际列（外键） |
| **UserToRecipe**（收藏评分） | **AssociationProxy** | **AssociationProxy** | 实际列（外键） |
| **RecipeShareToken** | 实际列（外键，NOT NULL） | 无 | 无 user_id |

### 4.2 AssociationProxy 动态代理的含义

**[代码支撑]** 以 Recipe 为例（`mealie/db/models/recipe/recipe.py#L52-L59`）：

```python
group_id: GUID = mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False)
user_id: GUID | None = mapped_column(GUID, sa.ForeignKey("users.id", use_alter=True))
household_id: AssociationProxy[GUID] = association_proxy("user", "household_id")
```

**含义：**
- `group_id` 和 `user_id` 是数据库中**实际存在的列**，有对应的外键约束
- `household_id` **不存储在数据库中**，它是 SQLAlchemy 的 AssociationProxy，每次访问时通过 `recipe.user.household_id` **动态计算**

**[代码支撑]** 相同模式出现在（使用 `association_proxy("user", "household_id")`）：
- `mealie/db/models/household/mealplan.py#L65-L66`（GroupMealPlan）
- `mealie/db/models/household/shopping_list.py#L153-L154`（ShoppingList）
- `mealie/db/models/users/users.py#L36-L37`（LongLiveToken 的 group_id 和 household_id）
- `mealie/db/models/recipe/comment.py#L25-L26`（RecipeComment）
- `mealie/db/models/recipe/recipe_timeline.py#L27-L28`（RecipeTimelineEvent）
- `mealie/db/models/users/user_to_recipe.py#L25-L26`（UserToRecipe）

**[代码支撑]** Cookbook 是例外（`mealie/db/models/household/cookbook.py#L27-L30`）：
```python
group_id: GUID | None = mapped_column(GUID, ForeignKey("groups.id"), index=True)
household_id: GUID | None = mapped_column(GUID, ForeignKey("households.id"), index=True)
```
Cookbook 的 `household_id` 是**实际存储的列**，与创建者用户的 household 没有动态绑定。

### 4.3 Household 偏好设置（跨边界控制）

**[代码支撑]** `mealie/db/models/household/preferences.py`：
- `private_household` (bool, 默认 True)：控制该 Household 下的 Recipe 是否在 Explore API 中可见
- `lock_recipe_edits_from_other_households` (bool, 默认 True)：禁止其他 Household 的用户编辑本 Household 的 Recipe

---

## 五、Recipe 细粒度权限控制

### 5.1 可见性：Group 内跨 Household 可见

**[代码支撑]** Recipe 列表查询使用 `group_recipes` 属性（`mealie/routes/recipe/_base.py`），该属性通过 `household_id=None` 初始化 Repository，从而**只按 group_id 过滤，不按 household_id 过滤**。

**[代码支撑]** `mealie/routes/recipe/recipe_crud_routes.py` 中的 `get_all` 方法使用 `self.group_recipes`，证明同 Group 内所有用户都可以看到所有 Household 的 Recipe。

**[代码支撑]** 集成测试 `tests/integration_tests/user_recipe_tests/test_recipe_cross_household.py` 验证了此行为。

### 5.2 编辑权限：can_update()

**[代码支撑]** `mealie/services/recipe/recipe_service.py#L87-L131`，`can_update()` 方法使用原始 SQL 执行检查：

1. 若当前用户是 Recipe 的创建者（`recipe.user_id == current_user_id`）→ 允许
2. 若 Recipe 被锁定（`recipe.lock_recipe` 为 True）→ **除所有者外全部拒绝**
3. 若 Recipe 所在 Household 设置了 `lock_recipe_edits_from_other_households=True` 且当前用户不在该 Household → 拒绝
4. 其他情况 → 允许同 Group 内跨 Household 编辑

### 5.3 删除权限：can_delete()

**[代码支撑]** `mealie/services/recipe/recipe_service.py#L70-L85`：
- 仅 Recipe 的创建者（`user_id` 匹配）或 Admin 可以删除
- 跨 Household 用户即使拥有编辑权限也**不能删除**

### 5.4 锁定/解锁权限：can_lock_unlock()

**[代码支撑]** 权限检查实现（`mealie/services/recipe/recipe_service.py#L133-L134`）：
```python
def can_lock_unlock(self, recipe: Recipe) -> bool:
    return recipe.user_id == self.user.id
```

**[代码支撑]** 关键发现：
1. **仅创建者本人可以锁定/解锁**：检查逻辑只比较 `recipe.user_id == self.user.id`，**没有任何 Admin 绕过**
2. **与删除权限形成对比**：`can_delete()` 方法（同文件 L70-L72）有 `if self.user.admin: return True`，但 `can_lock_unlock()` 完全没有类似逻辑
3. **调用路径**：`_pre_update_check()`（L462-L478）会先检查 `can_update()`，然后**额外单独检查**如果请求中 `settings.locked` 发生了变化，则必须通过 `can_lock_unlock()`
4. **批量接口保护**：Bulk Service 的 `set_settings()`（`mealie/services/recipe/recipe_bulk_service.py#L62-L76`）显式执行 `settings.locked = recipe.settings.locked`，**强制锁定状态不变**，从批量 API 层面完全阻止了锁定状态的修改
5. **无 Admin 专用路由**：`mealie/routes/admin/` 下没有任何 Recipe 管理路由，所有 Recipe 更新统一走 `BaseRecipeController`（继承自 `BaseUserController`，非 Admin 控制器）

**结论**：Admin 不能锁定/解锁非自己创建的 Recipe，与普通用户权限一致。

### 5.5 Last Made 更新

**[代码支撑]** `mealie/services/recipe/recipe_service.py#L559-L566`：
- `update_last_made()` 不经过权限预检查，任何能访问到 Recipe 的用户都可以更新"最近制作"时间和 Household 关联记录
- 这可能导致跨 Household 用户可以向任意 Recipe 添加制作记录

---

## 六、数据库级级联删除：核心发现

### 6.1 数据库层面：无任何 ON DELETE 子句

**[代码支撑]** 对 `mealie/alembic/versions/` 目录下所有迁移文件的 grep 搜索确认：
- 全部 migration 文件中**没有任何** `ondelete`、`on_delete` 或 `ON DELETE` 关键字
- 这意味着所有外键约束的级联行为完全依赖数据库默认值（通常是 `NO ACTION` / `RESTRICT`）
- 所有级联删除行为必须通过 SQLAlchemy ORM 层面的 `cascade` 参数实现

**[代码支撑]** 注释佐证：`mealie/repos/repository_generic.py#L278` 中 `delete_many()` 的注释写着：
> "we don't delete the whole query in one statement because postgres doesn't cascade correctly"

这证实了开发团队意识到数据库级级联不可靠，因此依赖 ORM 级级联。

### 6.2 ORM 级级联：User 端的 sp_args

**[代码支撑]** `mealie/db/models/users/users.py#L84-L88`：
```python
sp_args = {
    "back_populates": "user",
    "cascade": "all, delete, delete-orphan",
    "single_parent": True,
}
```

**[代码支撑]** 应用了 `sp_args` 的关系（用户删除时会被级联删除）：

| 关系 | 目标模型 | 位置 |
|-----|---------|-----|
| `tokens` | LongLiveToken | users.py#L90 |
| `comments` | RecipeComment | users.py#L91 |
| `recipe_timeline_events` | RecipeTimelineEvent | users.py#L92 |
| `password_reset_tokens` | PasswordResetModel | users.py#L93 |
| `mealplans` | GroupMealPlan | users.py#L99-L101 |
| `shopping_lists` | ShoppingList | users.py#L102 |

**[代码支撑]** 未应用 `sp_args` 的关系（用户删除时**不会被级联删除**）：

| 关系 | 目标模型 | 位置 |
|-----|---------|-----|
| `rated_recipes` | UserToRecipe（通过 secondary 表） | users.py#L103-L108 |
| `favorite_recipes` | UserToRecipe（通过 secondary 表） | users.py#L109-L115 |
| `owned_recipes` | RecipeModel（不同的 FK） | users.py#L95-L98 |

### 6.3 Recipe 删除时的手动清理

**[代码支撑]** `mealie/repos/repository_recipes.py#L110-L130`，`_delete_recipe()` 方法：
```python
# first remove UserToRecipe entries so we don't run into stale data errors
user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
self.session.execute(user_to_recipe_delete_query)
self.session.commit()

# remove the recipe
self.session.delete(recipe)
self.session.commit()
```

Recipe 删除会手动清理 UserToRecipe（收藏/评分）记录，然后再删除 Recipe 本身。这确认了开发团队知道 UserToRecipe 没有自动级联。

---

## 七、成员变更对数据可见性的影响

### 7.1 用户切换 Household

**[代码支撑]** Admin 可以通过用户管理 API 修改用户的 `household_id`（`mealie/routes/admin/admin_management_users.py#L64-L70`）。

**[代码支撑]** 对于使用 AssociationProxy 动态代理 `household_id` 的资源（Recipe、MealPlan、ShoppingList、LongLiveToken、Comment、TimelineEvent、UserToRecipe）：
- 这些资源的 "归属 Household" 完全取决于**创建者用户当前的 household_id**
- 当用户从 Household A 迁移到 Household B 时，该用户创建的所有 Recipe、MealPlan、ShoppingList 等资源的 `household_id` 会**动态变更为 Household B**
- 这意味着资源的可见性范围（在 Repository 过滤层面）会"漂移"到新的 Household

**[代码支撑]** 对于 Cookbook（household_id 是实际存储列）：
- Cookbook 的 household 归属是静态的，创建者用户迁移 Household **不影响** Cookbook 的归属

### 7.2 用户切换 Group

**[代码支撑]** Admin 可以修改用户的 `group_id`。

**[代码支撑]** 对于 Recipe、MealPlan、ShoppingList：
- `group_id` 是实际存储的列，不是 AssociationProxy
- 用户切换 Group **不改变**已有资源的 group_id 归属
- 但用户在新 Group 中通过 Repository 过滤（按新 group_id）时，**看不到**旧 Group 中创建的资源（这些资源仍留在原 Group 中，成为"孤儿"数据）

**[推测]** LongLiveToken、Comment、TimelineEvent 的 `group_id` 是 AssociationProxy，用户切换 Group 后这些资源的 group_id 会动态改变，可能导致它们出现在新 Group 中但指向旧 Group 的 Recipe（数据不一致）。

### 7.3 用户被删除

**[代码支撑]** 用户删除入口（`mealie/routes/admin/admin_management_users.py#L72-L74`）调用 `mixins.delete_one()` → `RepositoryUsers.delete()` → `RepositoryGeneric.delete()` → `session.delete(user)`。

**[代码支撑]** `RepositoryUsers.delete()`（`mealie/repos/repository_users.py#L55-L65`）仅额外删除用户文件目录，未处理任何数据关联清理。

**[风险][代码支撑]** 用户删除时，以下关联会被 ORM 级联删除：
- LongLiveToken、RecipeComment、RecipeTimelineEvent、PasswordResetModel、GroupMealPlan、ShoppingList

**[风险][代码支撑]** 用户删除时，以下关联**不会**被自动处理，会导致 FK 约束冲突或数据残留：

1. **Recipe.user_id**：Recipe 表中 `user_id` 是外键指向 `users.id`（`mealie/db/models/recipe/recipe.py#L58`），定义为 `use_alter=True`、`nullable=True`，但**无 ON DELETE 子句**。User → Recipe 之间没有定义带 cascade 的 relationship。
   - **[风险]**：删除有 Recipe 的用户可能触发数据库 IntegrityError（外键约束违反）
   - **推测**：若数据库允许（某些配置下 FK 检查不严格或 `nullable=True` 允许），被删用户的 Recipe 会保留但 `user_id` 仍指向不存在的用户 ID

2. **UserToRecipe（收藏/评分）**：UserToRecipe 表中 `user_id` 是外键（`mealie/db/models/users/user_to_recipe.py#L22`），无 ON DELETE。User 的 `rated_recipes` 和 `favorite_recipes` 关系未设置 cascade。
   - **[风险]**：删除有收藏或评分记录的用户会触发数据库 IntegrityError

3. **Recipe.last_made 等关联**：HouseholdToRecipe 等记录未被处理

**[推测]** 在实际运行中，如果数据库 FK 约束严格（如 PostgreSQL 默认配置），删除任何有 Recipe 或收藏评分的用户都会直接失败并回滚。

---

## 八、Household 删除行为

### 8.1 删除前置检查

**[代码支撑]** `mealie/routes/admin/admin_management_households.py#L79-L91`：
```python
stmt = select(func.count(User.id)).filter_by(group_id=item.group_id, household_id=item_id)
user_count = self.session.scalar(stmt)
if user_count:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorResponse.respond(message="Cannot delete household with users"),
    )
```

Household 删除前会检查是否有用户隶属于该 Household，有则拒绝删除。这从根本上避免了"用户所属 Household 不存在"的问题。

### 8.2 被删 Household 的资源归属

**[代码支撑]** 由于用户不迁移出 Household 就无法删除 Household，所以：
- Recipe、MealPlan、ShoppingList 等通过 AssociationProxy 绑定用户 household_id 的资源，必然随用户一起离开被删 Household
- Cookbook（静态绑定 household_id 的资源）**未被 Household 删除逻辑处理**

**[风险][代码支撑]** Cookbook 的 `household_id` 是实际存储的外键（`mealie/db/models/household/cookbook.py#L29-L30`），无 ON DELETE 子句。如果未来绕过用户计数检查直接删除 Household，Cookbook 表会出现 FK 约束违反。

---

## 九、成员移除后可能存在的访问风险汇总

### 风险 1：Recipe / MealPlan / ShoppingList 的 Household 归属漂移
**[代码支撑]**
- 成因：`household_id` 通过 `AssociationProxy("user", "household_id")` 动态获取
- 场景：用户被从 Household A 移至 Household B
- 结果：该用户创建的所有 Recipe / MealPlan / ShoppingList 在 Repository 过滤层面"漂移"到 Household B。如果 Household B 设置了 `private_household=False`，这些 Recipe 可能突然对外可见
- 涉及代码：
  - `mealie/db/models/recipe/recipe.py#L55`
  - `mealie/db/models/household/mealplan.py#L65`
  - `mealie/db/models/household/shopping_list.py#L153`

### 风险 2：跨 Household 编辑权限检查依赖创建者当前 Household
**[代码支撑]**
- 成因：`can_update()` 中的 Household 归属检查基于 Recipe.user 的 household，而非 Recipe 创建时的 Household
- 场景：用户 A 在 Household X 创建了 Recipe R，然后用户 A 被移至 Household Y
- 结果：对 Recipe R 的跨 Household 编辑检查会使用 Household Y 的 `lock_recipe_edits_from_other_households` 设置，而非原始的 Household X 设置
- 涉及代码：`mealie/services/recipe/recipe_service.py#L87-L131`

### 风险 3：用户删除导致 IntegrityError
**[风险][代码支撑]**
- 成因：Recipe.user_id 和 UserToRecipe.user_id 外键无 ON DELETE，User 端也无 cascade 设置
- 场景：Admin 删除一个有 Recipe 或有收藏/评分记录的用户
- 结果：数据库抛出 FK 约束违规错误，用户删除操作失败并回滚
- 涉及代码：
  - `mealie/db/models/recipe/recipe.py#L58`
  - `mealie/db/models/users/user_to_recipe.py#L22`
  - `mealie/repos/repository_users.py#L55-L65`（未做任何关联清理）

### 风险 4：LongLiveToken 的隐式权限范围变化
**[代码支撑]**
- 成因：LongLiveToken 的 `group_id` 和 `household_id` 是 AssociationProxy
- 场景：用户迁移 Group 或 Household 后，其之前签发的 Token 依然有效
- 结果：Token 的访问范围（在 Repository 过滤层）会随用户当前的 group/household 动态变化，旧 Token 会自动获得新范围的访问权限
- 涉及代码：`mealie/db/models/users/users.py#L36-L37`

### 风险 5：Cookbook 与创建者解耦
**[代码支撑]**
- 成因：Cookbook 的 `household_id` 是静态存储列，没有 user_id 字段
- 场景：创建 Cookbook 的用户被删除或迁移 Household
- 结果：Cookbook 仍留在原 Household，不会随用户迁移；用户删除时 Cookbook 不受影响（无 user_id 外键约束）
- 涉及代码：`mealie/db/models/household/cookbook.py#L27-L30`

### 风险 6：邀请令牌的不一致状态
**[代码支撑]**
- 成因：Household 邀请令牌绑定了具体的 `group_id` 和 `household_id`（静态存储）
- 场景：创建邀请后 Household 被删除（目前代码阻止了这种情况，但如果未来逻辑变更）
- 结果：邀请令牌指向不存在的 Household，接受邀请时会出错
- 涉及代码：`mealie/routes/households/controller_invitations.py`

### 风险 7：RecipeShareToken 与用户无关
**[代码支撑]**
- 成因：RecipeShareToken 只有 `group_id` 和 `recipe_id`，没有 `user_id`
- 场景：创建分享 Token 的用户被删除或移出 Group
- 结果：分享 Token 仍然有效，任何持有 Token 的人仍可访问 Recipe
- 涉及代码：`mealie/db/models/recipe/shared.py`

---

## 十、公开访问 Explore API 权限流

**[代码支撑]** `mealie/routes/explore/controller_public_recipes.py`：

公开 API 的可见性需要通过**三重检查**，全部满足才可见：

1. Group 层面：`group.private_group == False`
2. Household 层面：`household.preferences.private_household == False`
3. Recipe 层面：`recipe.settings.public == True`

---

## 十一、权限边界速查表

**列定义：**
- **Admin** = `admin=True`（自动被授予全部其他权限）
- **Household Admin** = `can_manage_household=True`（不保证拥有其他权限标志）
- **同 Household 普通用户** = 无特殊权限标志，与目标资源在同一 Household
- **同 Group 跨 Household 用户** = 无特殊权限标志，同 Group 但不同 Household
- **跨 Group 用户** = 不属于目标 Group

---

### A. Recipe 相关

| 操作 | Admin | Household Admin | 同 Household 普通用户 | 同 Group 跨 Household 用户 | 跨 Group 用户 |
|-----|-------|----------------|---------------------|------------------------|-------------|
| 查看同 Group Recipe | ✅ | ✅ | ✅ | ✅ | ❌ |
| 编辑自己创建的 Recipe | ✅ | ✅ | ✅ | N/A | ❌ |
| 编辑他人 Recipe（未锁定） | 取决于对方 Household 设置¹ | 取决于对方 Household 设置¹ | ✅ | 取决于对方 Household 设置¹ | ❌ |
| 编辑他人 Recipe（已锁定） | ❌（除非是所有者本人）² | ❌（除非是所有者本人） | ❌（除非是所有者本人） | ❌ | ❌ |
| 删除他人 Recipe | ✅³ | ❌ | ❌ | ❌ | ❌ |
| 锁定/解锁 Recipe | ❌（除非是该 Recipe 的创建者本人）⁴ | ❌（除非是该 Recipe 的创建者本人） | ❌（除非是该 Recipe 的创建者本人） | ❌ | ❌ |

> ¹ `can_update()` 中无 Admin 绕过逻辑；若对方 Household 设置 `lock_recipe_edits_from_other_households=True` 则禁止跨 Household 编辑
> ² 锁定的 Recipe 只有所有者可以编辑，`can_update()` 的 SQL 中无 Admin 绕过
> ³ `can_delete()` 显式包含 `if self.user.admin: return True`
> ⁴ `can_lock_unlock()` 仅判断 `recipe.user_id == self.user.id`，无 Admin 绕过

---

### B. 家庭 / 计划 资源

| 操作 | Admin | Household Admin | 同 Household 普通用户 | 同 Group 跨 Household 用户 | 跨 Group 用户 |
|-----|-------|----------------|---------------------|------------------------|-------------|
| 查看 MealPlan/ShoppingList | ✅ | ✅（同 Household） | ✅ | ❌（被 Repository 过滤） | ❌ |
| 查看 Cookbook（列表+详情） | ✅ | ✅ | ✅ | ✅（Cookbook 列表按 Group 查询）⁵ | ❌ |
| 创建/更新/删除 Cookbook | ✅ | ✅ | ✅ | ✅（无任何权限检查）⁶ | ❌ |
| 创建/更新/删除 MealPlan | ✅ | ✅（同 Household） | ✅ | ❌（被 Repository 过滤） | ❌ |
| 创建/更新/删除 ShoppingList | ✅ | ✅（同 Household） | ✅ | ❌（被 Repository 过滤） | ❌ |

> ⁵ Cookbook 的 `get_all()` 和 `get_one()` 都使用 `group_cookbooks`（`household_id=None`），同 Group 内跨 Household 可见
> ⁶ Cookbook 控制器的 create/update/delete 方法均**未调用任何权限检查**

---

### C. 权限与偏好管理

| 操作 | Admin | Household Admin | 同 Household 普通用户 | 同 Group 跨 Household 用户 | 跨 Group 用户 |
|-----|-------|----------------|---------------------|------------------------|-------------|
| 修改 Household 偏好设置（private_household 等） | ✅ | ✅⁷ | ❌ | ❌ | ❌ |
| 调整同 Household 其他成员的四个权限标志 | ✅ | ❌（需要 `can_manage`，不是 `can_manage_household`）⁸ | ❌ | ❌ | ❌ |
| 调整自己的权限 | ❌（禁止自改）⁹ | ❌（禁止自改） | ❌（禁止自改） | N/A | N/A |
| 修改用户 Group/Household 归属 | ✅¹⁰ | ❌ | ❌ | ❌ | ❌ |
| 删除其他用户 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 删除 Household（有用户） | ❌¹¹ | ❌ | ❌ | ❌ | ❌ |
| 删除 Household（无用户） | ✅ | ❌ | ❌ | ❌ | ❌ |

> ⁷ 由 `checks.can_manage_household()` 控制，调用点：`controller_household_self_service.py#L60`
> ⁸ 由 `checks.can_manage()` 控制（不是 `can_manage_household`），调用点：`controller_household_self_service.py#L66`；集成测试确认：`test_household_permissions.py#L20-L56`
> ⁹ 代码显式禁止：`if target_user.id == self.user.id: raise 403`
> ¹⁰ 只能通过 Admin 管理 API：`mealie/routes/admin/admin_management_users.py`
> ¹¹ 代码显式检查用户数：`user_count > 0 → raise 400`

---

### D. 邀请功能

| 操作 | Admin | Household Admin | 同 Household 普通用户 | 同 Group 跨 Household 用户 | 跨 Group 用户 |
|-----|-------|----------------|---------------------|------------------------|-------------|
| 查看全部邀请令牌列表 | ✅¹² | ❌ | ❌ | ❌ | ❌ |
| 为本 Group/Household 创建邀请令牌 | ✅ | 有 `can_invite` 即可 | 有 `can_invite` 即可 | 有 `can_invite` 即可 | ❌ |
| 为**其他** Group/Household 创建邀请令牌 | ✅¹³ | ❌ | ❌ | ❌ | ❌ |
| 发送邀请邮件 | ✅ | 有 `can_invite` 即可 | 有 `can_invite` 即可 | 有 `can_invite` 即可 | ❌ |

> ¹² 列表接口仅检查 `self.user.admin`，不检查 `can_invite`：`controller_invitations.py#L25-L29`
> ¹³ 创建接口检查：`not self.user.admin and (body.group_id != self.group_id or body.household_id != self.household_id) → 403`

---

### E. Group 级组织者资源

| 操作 | Admin | Household Admin | 同 Household 普通用户 | 同 Group 跨 Household 用户 | 跨 Group 用户 |
|-----|-------|----------------|---------------------|------------------------|-------------|
| 创建/更新/删除 Recipe Tag | ✅ | 有 `can_organize` 即可 | 有 `can_organize` 即可 | 有 `can_organize` 即可 | ❌ |
| 创建/更新/删除 Recipe Category | ✅ | 有 `can_organize` 即可 | 有 `can_organize` 即可 | 有 `can_organize` 即可 | ❌ |
| 创建/合并/更新/删除 Ingredient Food | ✅ | 有 `can_organize` 即可 | 有 `can_organize` 即可 | 有 `can_organize` 即可 | ❌ |
| 创建/更新/删除 Recipe Tool | ✅ | ✅¹⁴ | ✅¹⁴ | ✅¹⁴ | ❌ |
| 创建/合并/更新/删除 Ingredient Unit | ✅ | ✅¹⁴ | ✅¹⁴ | ✅¹⁴ | ❌ |
| 创建/更新/删除 Multi-purpose Label | ✅ | ✅¹⁴ | ✅¹⁴ | ✅¹⁴ | ❌ |
| Group AI Provider 设置 + Provider CRUD | ✅ | 有 `can_manage` 即可 | 有 `can_manage` 即可 | 有 `can_manage` 即可 | ❌ |

> ¹⁴ **无任何权限检查**：这些资源的 CRUD 接口对同 Group 内任意已登录用户开放

---

## 十二、用户自助更新保护

**[代码支撑]** `mealie/routes/users/_helpers.py`：
- 普通用户不能通过自助更新 API 修改自己的 `group_id`、`household_id` 和任何权限字段
- Admin 不能通过用户 API 修改其他用户，也不能通过自助 API 更改自己的 Admin 状态（防止自降权限）
- Admin 用户的 Group/Household 变更必须通过 Admin 管理 API（`mealie/routes/admin/admin_management_users.py`）
