# Mealie Household 与 Group 权限边界深度分析

## 一、层级结构与数据模型关系

### 1.1 实体层级

```
Group (组)
  └── Household (家庭) ← 一个 Group 可包含多个 Household
        └── User (用户) ← 一个 User 只属于一个 Group 和一个 Household
```

### 1.2 核心模型关系

**Group** 模型定义见 [group.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/group/group.py)：
- `id`: 主键
- `name`: 组名（全局唯一）
- `slug`: 组 URL 标识（全局唯一）
- `households`: 一对多关系，指向该 Group 下的所有 Household
- `users`: 一对多关系，指向该 Group 下的所有 User
- `recipes`: 一对多关系，该 Group 下的所有 Recipe
- `mealplans`, `shopping_lists`, `cookbooks` 等：Group 层面的资源聚合关系

**Household** 模型定义见 [household.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/household.py)：
- `id`: 主键
- `name`: 家庭名（在 Group 内唯一）
- `slug`: 家庭 URL 标识（在 Group 内唯一）
- `group_id`: 外键，指向所属 Group
- `group`: 多对一关系
- `users`: 一对多关系，该 Household 下的所有 User
- `preferences`: 一对一关系，Household 偏好设置
- `made_recipes`: 多对多关系（通过 `HouseholdToRecipe` 关联表），记录哪些 Household "做过" 哪些 Recipe

**User** 模型定义见 [users.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py)：
- `group_id`: 外键（NOT NULL），所属 Group
- `household_id`: 外键（可 NULL），所属 Household
- `group` / `household`: 多对一关系
- 权限标志字段：`admin`, `can_manage_household`, `can_manage`, `can_invite`, `can_organize`

---

## 二、用户角色与权限体系

### 2.1 权限标志位定义

所有用户权限存储在 `users` 表的布尔字段中，定义于 [users.py#L78-L83](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py#L78-L83)：

| 权限标志 | 含义 | 作用范围 |
|---------|------|---------|
| `admin` | 超级管理员 | 系统全局，可跨 Group/Household |
| `can_manage_household` | 可管理 Household 设置 | Household 级 |
| `can_manage` | 可管理成员权限 | Household 级 |
| `can_invite` | 可创建邀请令牌 | Household 级 |
| `can_organize` | 可组织数据 | Household 级 |
| `advanced` | 高级功能访问 | 用户级功能特性开关 |

**权限联动规则**（见 [users.py#L207-L230](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py#L207-L230)）：
- 若 `admin=True`，则自动将 `can_manage_household`、`can_manage`、`can_invite`、`can_organize`、`advanced` 全部设为 `True`
- 非 admin 用户，各项权限独立设置

### 2.2 权限检查入口

权限检查通过 `OperationChecks` 类实现，定义于 [checks.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/_base/checks.py)：

- `can_manage_household()`: 检查 Household 管理权限
- `can_manage()`: 检查成员管理权限
- `can_invite()`: 检查邀请权限
- `can_organize()`: 检查数据组织权限

所有检查失败均抛出 `403 Forbidden`。

### 2.3 权限设置 Schema

`SetPermissions` 定义于 [household_permissions.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/schema/household/household_permissions.py)，用于通过 API 修改成员权限。

---

## 三、授权入口与认证流程

### 3.1 认证依赖注入链

认证核心位于 [dependencies.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/core/dependencies/dependencies.py)：

1. **`get_current_user`** (L88-L123):
   - 支持 OAuth2 Bearer Token 和 Cookie (`mealie.access_token`) 两种方式
   - 解析 JWT，提取 `user_id` 或 `long_token`
   - 从数据库获取用户，返回 `PrivateUser` 对象

2. **`get_admin_user`** (L135-L138):
   - 依赖 `get_current_user`
   - 额外检查 `user.admin == True`，否则抛出 403

### 3.2 控制器基类与 Repository 初始化

所有路由控制器继承自 `_BaseController` 体系，定义于 [base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/_base/base_controllers.py)：

**`BaseUserController`** (L132-L172) - 登录用户访问的控制器基类：
- 注入 `user: PrivateUser = Depends(get_current_user)`
- `group_id` 属性返回 `user.group_id`
- `household_id` 属性返回 `user.household_id`
- `repos` 属性通过 `AllRepositories(session, group_id=self.group_id, household_id=self.household_id)` 创建

**`BaseAdminController`** (L175-L189) - 管理员访问的控制器基类：
- 注入 `user: PrivateUser = Depends(get_admin_user)`
- **关键点**: `repos` 使用 `group_id=None, household_id=None` 初始化，绕过所有范围过滤

**`BasePublicGroupExploreController`** / **`BasePublicHouseholdExploreController`** (L91-L130) - 公开探索 API：
- 通过 `get_public_group` 依赖获取非私有 Group
- Repository 不绑定 household，由调用方根据 `private_household` 和 `recipe.settings.public` 过滤

### 3.3 Repository 范围过滤机制

Repository 层的权限边界是整个系统最核心的数据隔离机制，位于 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/repos/repository_generic.py)：

**三级 Repository 类**：

1. **`RepositoryGeneric`** (L33-L487) - 无任何范围限制的基类
   - `_filter_builder()` (L94-L102): 根据 `self._group_id` 和 `self._household_id` 构建过滤字典

2. **`GroupRepositoryGeneric`** (L489-L502) - Group 范围过滤
   - 强制要求传入 `group_id`（传 `NOT_SET` 抛异常）
   - 所有查询自动附加 `WHERE group_id = ?` 条件

3. **`HouseholdRepositoryGeneric`** (L505-L523) - Household 范围过滤
   - 强制要求传入 `group_id` 和 `household_id`
   - 所有查询自动附加 `WHERE group_id = ? AND household_id = ?` 条件

**过滤构建逻辑**（`_filter_builder` 方法）：
```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

该方法被所有查询方法（`get_one`、`page_all`、`multi_query`、`_query_one` 等）调用。

### 3.4 各 Repository 的范围分类

完整映射见 [repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/repos/repository_factory.py)：

| 资源类型 | Repository 基类 | 过滤范围 | 说明 |
|---------|---------------|---------|------|
| Recipes | `HouseholdRepositoryGeneric` | Household | 支持跨 Household 查询（手动设 household_id=None） |
| Ingredient Foods | `GroupRepositoryGeneric` | Group | 共享数据 |
| Ingredient Units | `GroupRepositoryGeneric` | Group | 共享数据 |
| Tools | `GroupRepositoryGeneric` | Group | 共享数据 |
| Comments | `GroupRepositoryGeneric` | Group | 跨 Household 可评论 |
| Categories | `GroupRepositoryGeneric` | Group | 共享数据 |
| Tags | `GroupRepositoryGeneric` | Group | 共享数据 |
| Recipe Timeline Events | `GroupRepositoryGeneric` | Group | 跨 Household 可发布事件 |
| Users | `GroupRepositoryGeneric` | Group | 同 Group 可见 |
| Groups | 无过滤 (`RepositoryGeneric`) | 无限制 | Admin 使用 |
| Households | `GroupRepositoryGeneric` | Group | 同 Group 可见 |
| Household Preferences | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Cookbooks | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Meal Plans | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Meal Plan Rules | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Shopping Lists | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Webhooks | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Recipe Actions | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Event Notifiers | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Invite Tokens | `HouseholdRepositoryGeneric` | Household | 仅本 Household |
| Labels | `GroupRepositoryGeneric` | Group | 共享数据 |

---

## 四、核心数据归属模型

### 4.1 Recipe（食谱）

定义于 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/recipe/recipe.py)

**归属设计特点**：
```python
group_id: GUID = mapped_column(...)           # 实际存储的列
user_id: GUID | None = mapped_column(...)      # 实际存储的列
household_id: AssociationProxy[GUID] = association_proxy("user", "household_id")  # 代理属性，不存列
```

**关键洞察**：`household_id` **不是** recipes 表中的实际列！它是通过 `user_id → user.household_id` 的 SQLAlchemy AssociationProxy 动态获取的。这意味着：
- Recipe 的 Group 归属是固定的（存储在 `group_id` 列）
- Recipe 的 Household 归属是**动态的**，取决于创建者用户当前的 `household_id`

Recipe 查询在 [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/repos/repository_recipes.py) 的 `_build_recipe_filter` 方法（L295-L337）中使用 AssociationProxy 进行过滤：
```python
if self.household_id:
    fltr.append(RecipeModel.household_id == self.household_id)
```

SQLAlchemy 会将此转化为 JOIN users 表的查询。

### 4.2 Meal Plan（膳食计划）

定义于 [mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/mealplan.py)

与 Recipe 相同的设计：
```python
group_id: GUID = mapped_column(...)            # 实际存储
user_id: GUID = mapped_column(...)             # 实际存储
household_id: AssociationProxy = association_proxy("user", "household_id")  # 动态代理
```

### 4.3 Shopping List（购物清单）

定义于 [shopping_list.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/shopping_list.py#L147-L181)

同样采用动态代理：
```python
group_id: GUID = mapped_column(...)            # 实际存储
user_id: GUID = mapped_column(...)             # 实际存储
household_id: AssociationProxy = association_proxy("user", "household_id")  # 动态代理
```

### 4.4 Cookbook（食谱书）

定义于 [cookbook.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/cookbook.py)

**与以上不同，Cookbook 是静态绑定**：
```python
group_id: GUID = mapped_column(...)     # 实际存储
household_id: GUID = mapped_column(...) # 实际存储（外键）
```

Cookbook 的 Household 归属是固定的，不随用户迁移而改变。

### 4.5 Group 级共享数据

以下数据是 Group 级共享的（所有 Household 可见）：
- `Category`（分类）、`Tag`（标签）、`Tool`（工具）
- `IngredientFoodModel`（食材）、`IngredientUnitModel`（单位）
- `MultiPurposeLabel`（多用途标签）
- `RecipeComment`（食谱评论）、`RecipeTimelineEvent`（食谱时间线事件）

---

## 五、Recipe 细粒度权限控制

Recipe 有超越 Repository 范围过滤的额外权限检查，实现在 [recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/services/recipe/recipe_service.py)。

### 5.1 可见性（Read）

Repository 范围控制是主要机制：
- **同 Group 内登录用户**：Recipe 列表查询使用 `group_recipes`（`household_id=None`），可见 Group 内所有 Household 的 Recipe（见 [recipe_crud_routes.py#L340-L395](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/recipe/recipe_crud_routes.py#L340-L395)）
- **单个 Recipe 查询**：同样可见同 Group 内所有 Recipe
- **公开访问**：需同时满足 `private_household=False` 和 `recipe.settings.public=True`（见 [controller_public_recipes.py#L114-L125](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/explore/controller_public_recipes.py#L114-L125)）

### 5.2 更新权限（Update）

通过 `RecipeService.can_update()` 方法检查（L87-L131），使用原始 SQL 判断：

```
可以更新的条件（全部满足）：
  ├── 用户是 Recipe 的所有者（r.user_id = 当前 user_id）
  └── 或者同时满足：
        ├── Recipe 未锁定（COALESCE(rs.locked, TRUE) = FALSE）
        └── (同 Household) 或 (不同 Household 且目标 Household 未锁定其他 Household 编辑)
```

Household 偏好设置中的 `lock_recipe_edits_from_other_households`（默认 `True`）控制跨 Household 编辑权限，见 [preferences.py#L29](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/preferences.py#L29)。

### 5.3 删除权限（Delete）

通过 `RecipeService.can_delete()` 方法检查（L70-L85）：
- **Admin 用户**：始终可以删除
- **普通用户**：必须是所有待删除 Recipe 的创建者（`recipe.user_id == 当前 user_id`）
- 跨 Household 用户即使有编辑权限也**不能删除**

测试验证见 [test_recipe_cross_household.py#L179-L209](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/tests/integration_tests/user_recipe_tests/test_recipe_cross_household.py#L179-L209)。

### 5.4 锁定/解锁权限

仅 Recipe 创建者可锁定/解锁 Recipe（`can_lock_unlock` 方法，L133-L134）。

### 5.5 Last Made 标记权限

任何登录用户均可对任意 Group 内 Recipe 更新 "最后制作时间"（`update_last_made` 方法，L559-L566），该操作绕过了 `_pre_update_check` 检查。每个 Household 的 last_made 值独立存储在 `HouseholdToRecipe` 关联表中。

---

## 六、成员变更对数据可见性的影响

### 6.1 用户 Household 变更

#### 变更入口

- **普通用户**：无法自行更改 household。[\_helpers.py#L28-L33](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/users/_helpers.py#L28-L33) 明确禁止。
- **Admin 用户**：通过 Admin API (`/admin/users/{id}` PUT) 可更改任意用户的 group 和 household，见 [admin_management_users.py#L64-L70](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/admin/admin_management_users.py#L64-L70)。
- **User.update()** 方法：允许通过名称查找 Group/Household 并更新关联（[users.py#L178-L202](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py#L178-L202)）。

#### 变更影响

由于 Recipe、Meal Plan、Shopping List 的 `household_id` 是通过 `AssociationProxy` 动态关联的，**用户迁移 Household 会导致其创建的所有此类资源自动"跟随"迁移到新 Household**。

示例场景：
1. User A 在 Household H1 创建了 Recipe R1、R2
2. Admin 将 User A 从 H1 移动到 H2
3. 结果：R1、R2 在 H1 的查询中**不再可见**，而在 H2 的查询中**变为可见**
4. 但 Recipe 的 `group_id` 不变，因此如果迁移到不同 Group，Recipe 仍留在原 Group

### 6.2 用户 Group 变更

- **普通用户**：无法自行更改 group（[\_helpers.py#L22-L26](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/users/_helpers.py#L22-L26)）
- **Admin 用户**：可通过 Admin API 变更
- 由于 Recipe、Meal Plan、Shopping List 的 `group_id` 是**实际存储的列**（非代理），用户迁移 Group 后，其创建的资源**仍保留在原 Group** 中
- 但这些资源在原 Group 中的 Household 归属会变为"不确定"（因为创建者已不在该 Group，AssociationProxy 可能返回 NULL 或报错）

### 6.3 成员移除（用户删除）

- Admin API 提供用户删除功能（[admin_management_users.py#L72-L74](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/admin/admin_management_users.py#L72-L74)）
- 删除 Household 前检查：必须无成员，否则拒绝（[admin_management_households.py#L79-L90](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/admin/admin_management_households.py#L79-L90)）

---

## 七、成员移除后可能存在的访问风险

### 风险 1：Recipe 的 Household 归属漂移

**问题**：Recipe 通过 AssociationProxy 关联到创建者的 `household_id`。当创建者被移动到其他 Household 或被删除时：

- **移动到同 Group 内其他 Household**：其所有 Recipe、Meal Plan、Shopping List 也会"归属"到新 Household，可能导致原 Household 丢失数据访问权，新 Household 意外获得大量数据。
- **用户被删除**：`recipe.user_id` 变为 NULL（如果 ON DELETE SET NULL）或整个 Recipe 级联删除。由于 `household_id` 是通过 user 代理的，user 删除后该代理可能失效。

**关联代码**：
- Recipe: [recipe.py#L55-L56](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/recipe/recipe.py#L55-L56)
- MealPlan: [mealplan.py#L65-L66](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/mealplan.py#L65-L66)
- ShoppingList: [shopping_list.py#L153-L154](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/shopping_list.py#L153-L154)

### 风险 2：跨 Household Recipe 编辑权限检查绕过

**问题**：`can_update()` 方法通过 `u.household_id`（Recipe 创建者的 household）和 `hp.lock_recipe_edits_from_other_households`（创建者 Household 的偏好）来判断。当创建者被迁移到其他 Household 后：

- 创建者 household_id 的改变可能导致原本"锁定"的 Recipe 变为可编辑
- 或相反，原本可编辑的变为锁定

**关联代码**：
- [recipe_service.py#L87-L131](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/services/recipe/recipe_service.py#L87-L131)

### 风险 3：收藏夹和评分的残留数据

用户与 Recipe 的交互数据（收藏、评分）存储在 `UserToRecipe` 关联表中，通过 `cascade="all, delete, delete-orphan"` 设置级联删除。理论上用户删除时会清理，但需注意：

- 用户在 A Household 的 Recipe 收藏，如果用户被移动到 B Household，这些收藏关系**不随用户迁移自动清除**（因为它们是 user_id → recipe_id 关联，与 household 无关）
- 这可能导致用户在新 Household 中仍能看到旧 Group/Household Recipe 的"已收藏"标记

**关联代码**：
- User 模型 sp_args: [users.py#L84-L88](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py#L84-L88)

### 风险 4：邀请令牌与 Household 绑定不一致

邀请令牌（`GroupInviteToken`）绑定了 `group_id` 和 `household_id`（实际列存储）。当 Household 被删除但令牌未清理时（虽然 Household 级联设置了 `cascade="all, delete-orphan"`），使用残留令牌可能导致异常。

**关联代码**：
- Household invite_tokens 关系: [household.py#L40-L42](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/household/household.py#L40-L42)
- 邀请创建: [controller_invitations.py#L33-L60](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/households/controller_invitations.py#L33-L60)

### 风险 5：Long-Live Token 的隐式权限

`LongLiveToken` 通过 `AssociationProxy` 继承 user 的 group_id 和 household_id。当用户权限变更（被降级或移出 household）后，已签发的长期令牌：

- 仍然有效（直到过期或被撤销）
- 每次请求都会从关联用户重新获取 group_id/household_id
- 但如果用户被删除，token 可能因外键约束被级联删除或失效

**关联代码**：
- LongLiveToken 模型: [users.py#L28-L43](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/db/models/users/users.py#L28-L43)

### 风险 6：Cookbook 与创建者 Household 解耦

Cookbook 的 `household_id` 是实际存储的列，不随创建者迁移。但如果创建者被移走：
- Cookbook 仍留在原 Household（正确行为）
- 但 Cookbook 的 `public` 标志和 `query_filter_string` 配置可能继续影响跨 Household 的 Recipe 可见性

### 风险 7：Household 删除前的用户数量检查不彻底

删除 Household 时仅检查当前 `User.household_id = target_id` 的用户数量。但：
- 该用户创建的 Recipe、Meal Plan、Shopping List（通过 AssociationProxy 关联）可能在删除后 household_id 代理失效
- Cookbook（实际列存储）因 `cascade="all, delete-orphan"` 会被级联删除

---

## 八、公开访问（Explore API）权限流

公开探索 API 的控制器位于 `BasePublicHouseholdExploreController`，通过多重检查保护隐私：

1. **Group 级别**：`get_public_group` 依赖检查 `group.preferences.private_group`（私有 Group 不公开）
2. **Household 级别**：`get_public_household` 检查 `household.preferences.private_household`
3. **Recipe 级别**：`PublicRecipesController` 额外附加查询过滤 `"(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"`

**关联代码**：
- [dependencies.py#L67-L74](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/core/dependencies/dependencies.py#L67-L74)
- [base_controllers.py#L91-L129](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/_base/base_controllers.py#L91-L129)
- [controller_public_recipes.py#L30-L125](file:///d:/fz/0601/solo-dogfeeding/code/71-mealie/mealie/routes/explore/controller_public_recipes.py#L30-L125)

---

## 九、总结：权限边界速查表

| 操作 | Admin | Household 管理员 (can_manage) | 同 Household 普通用户 | 同 Group 其他 Household 用户 |
|-----|:-----:|:------------------------------:|:--------------------:|:--------------------------:|
| 查看 Group 内所有 Recipe | ✅ | ✅ | ✅ | ✅ |
| 查看本 Household Meal Plan | ✅ | ✅ | ✅ | ❌ |
| 查看本 Household Shopping List | ✅ | ✅ | ✅ | ❌ |
| 查看本 Household Cookbook | ✅ | ✅ | ✅ | ❌ |
| 创建 Recipe | ✅ | ✅ | ✅ | ✅（在自己 Household） |
| 编辑自己创建的 Recipe | ✅ | ✅ | ✅ | ✅ |
| 编辑同 Household 他人 Recipe（未锁定） | ✅ | ✅ | ✅ | 取决于目标 Household 设置 |
| 编辑其他 Household 的 Recipe | ✅ | ❌ | ❌ | 仅当目标 Household 允许 |
| 删除自己创建的 Recipe | ✅ | ✅ | ✅ | ✅ |
| 删除他人创建的 Recipe | ✅ | ❌ | ❌ | ❌ |
| 管理 Household 偏好设置 | ✅ | ✅ (can_manage_household) | ❌ | ❌ |
| 修改成员权限 | ✅ | ✅ (can_manage) | ❌ | ❌ |
| 创建邀请令牌 | ✅ | ✅ (can_invite) | ❌ | ❌ |
| 更改用户 Group/Household | ✅ | ❌ | ❌ | ❌ |
| 删除用户 | ✅ | ❌ | ❌ | ❌ |
| 删除 Household | ✅（需空） | ❌ | ❌ | ❌ |
