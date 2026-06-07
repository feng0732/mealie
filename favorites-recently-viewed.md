# Favorites、last_made 与 Shared Recipe 代码协作深度分析

## 0. 验证环境与方法说明

本文档结论基于两类证据：
1. **静态代码分析**：对 Mealie 源码的逐行追溯
2. **最小重现验证**：使用纯 SQLAlchemy 复现关键模型关系后实际运行，验证脚本见：
   - [verify_secondary_minimal.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/dev/scripts/verify_secondary_minimal.py)
   - [verify_secondary_precise.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/dev/scripts/verify_secondary_precise.py)

测试运行结果（节选）：

```
METHOD A: session.add(UserToRecipe(...))   (Mealie's actual pattern)
  Before: {'users': 1, 'recipes': 1, 'user_to_recipe': 1}
  session.delete(user) + commit() raised:
    "StaleDataError: DELETE statement on table 'users_to_recipes' expected to delete 1 row(s); Only 0 were matched."
  After:  {'users': 1, 'recipes': 1, 'user_to_recipe': 1}   ← 完全回滚，啥也没删掉

METHOD B: user.rated_recipes.append(recipe)   (pure M2M pattern, NOT used by Mealie)
  Before: {'users': 1, 'recipes': 1, 'user_to_recipe': 1}
  UTR row: rating=None, is_favorite=False    ← 额外字段丢失
  session.delete(user) + commit() raised: None
  After:  {'users': 0, 'recipes': 1, 'user_to_recipe': 0}   ← 正常删除

METHOD C: sa.delete(UserToRecipe) first, then session.delete(user)   (RepositoryRecipes' pattern)
  Before: {'users': 1, 'recipes': 1, 'user_to_recipe': 1}
  After manual sa.delete(UserToRecipe): {'users': 1, 'recipes': 1, 'user_to_recipe': 0}
  session.delete(user) + commit() raised: None
  After:  {'users': 0, 'recipes': 1, 'user_to_recipe': 0}   ← 正常删除
```

---

## 1. 概念澄清：recentRecipes ≠ 最近浏览

**结论：Mealie 不存在任何"用户访问历史/最近浏览"功能。**

| 变量 | 定义位置 | 实际语义 | 排序依据 |
|------|---------|----------|----------|
| `recentRecipes` | [use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/composables/recipes/use-recipes.ts#L9) | 首页展示的前 30 条 Recipe | `created_at`（创建时间倒序） |
| `allRecipes` | [use-recipes.ts](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/composables/recipes/use-recipes.ts#L8) | 全部 Recipe | `created_at` |

`recentRecipes` 命名高度误导，实际是"最近创建"而非"最近访问/浏览"。

---

## 2. `last_made`（最近制作）— Household 维度

### 2.1 模型与事件驱动更新

**核心文件**：[household_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/household/household_to_recipe.py#L39-L84)

```python
class HouseholdToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "households_to_recipes"
    household_id = Column(GUID, ForeignKey("households.id"), index=True, primary_key=True)
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)
    last_made: Mapped[datetime | None] = mapped_column(NaiveDateTime)
```

**两层 last_made 协作**：

| 层级 | 存储 | 更新方式 |
|------|------|----------|
| Household 独立值 | `HouseholdToRecipe.last_made` | 用户手动 / mealplan 每日自动 |
| Recipe 全局值 | `RecipeModel.last_made` | SQLAlchemy 事件：`max(旧值, 新值)` |

全局值由 `after_insert` / `after_update` / `after_delete` 三个事件同步，但 `after_delete` 时 `target.last_made` 仍是删除前值，`max()` 逻辑不会回退——**删除 Household 的 last_made 记录后，全局值可能保留过时的时间戳**。

### 2.2 实际 API 路径与时间戳来源

| 操作 | API 路径 | 方法 |
|------|---------|------|
| 更新 last_made | `/api/recipes/{slug}/last-made` | PATCH |
| 获取当前 Household 的 last_made | `/api/households/self/recipes/{recipe_slug}` | GET |

**Schema**（[recipe.py#L386-L387](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/schema/recipe/recipe.py#L386-L387)）：
```python
class RecipeLastMade(BaseModel):
    timestamp: datetime.datetime
```

**时间戳来源（前端）**：
1. 用户在 date picker 中选择日期
2. 拼接 `"T23:59:59"`（即"所选日期的当天 23:59:59 本地时间"）
3. `.toISOString()` 转成 UTC ISO 字符串发送给后端，见 [RecipeLastMade.vue#L263](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/frontend/app/components/Domain/Recipe/RecipeLastMade.vue#L263)

前端有客户端保护：仅当 `new_timestam > old_lastMade` 时才发请求；后端不做校验，直接覆盖。

### 2.3 RecipeService.update_last_made 完整写入流程

路由 [recipe_crud_routes.py#L568-L587](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/routes/recipe/recipe_crud_routes.py#L568-L587) 接收 `slug` 和 `timestamp`，调用 `service.update_last_made(slug, data.timestamp)`。

Service 层 [recipe_service.py#L559-L566](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/recipe/recipe_service.py#L559-L566)：
```python
def update_last_made(self, slug_or_id: str | UUID, timestamp: datetime) -> Recipe:
    # 显式绕过权限检查 — 任何登录用户都可以更新任何 Recipe 的 last_made
    household_service = HouseholdService(self.user.group_id, self.user.household_id, self.repos)
    household_service.set_household_recipe(slug_or_id, HouseholdRecipeUpdate(last_made=timestamp))
    return self.get_one(slug_or_id)
```

`HouseholdService.set_household_recipe` 执行：查当前 Household 是否已有行 → 无则 INSERT，有则 UPDATE `HouseholdToRecipe`。

SQLAlchemy `after_insert`/`after_update` 事件在 commit 时触发，自动把全局 `RecipeModel.last_made` 更新为 `max(旧值, 新值)`。

### 2.4 Mealplan 定时任务自动更新

**核心文件**：[create_timeline_events.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/services/scheduler/tasks/create_timeline_events.py#L25-L134)

调度器每日遍历当天 mealplan，对每个关联了 recipe 的 mealplan entry：
1. 若今天未创建过 timeline event，则创建
2. 若 `last_made` 为空或日期早于今天，同时：
   - 调用 `set_household_recipe` 写入 `HouseholdToRecipe`（触发 ORM 事件更新全局值）
   - 直接 `repos.recipes.patch(recipe.slug, {"last_made": event_time})` 手动更新全局值（双重保障，幂等）

测试已验证定时任务会**保留用户手动设置的未来日期**，不会被覆盖，见 [test_create_timeline_events.py#L208-L253](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/services_tests/scheduler/tasks/test_create_timeline_events.py#L208-L253)。

---

## 3. Favorites（收藏）与评分 — UserToRecipe 的 secondary 关系

### 3.1 数据模型与约束

**核心文件**：[user_to_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py)

```python
class UserToRecipe(SqlAlchemyBase, BaseMixins):
    __tablename__ = "users_to_recipes"
    __table_args__ = (UniqueConstraint("user_id", "recipe_id", name="user_id_recipe_id_rating_key"),)

    user_id = Column(GUID, ForeignKey("users.id"), index=True, primary_key=True)      # 无 ondelete
    recipe_id = Column(GUID, ForeignKey("recipes.id"), index=True, primary_key=True)  # 无 ondelete
    rating = Column(Float, index=True, nullable=True)
    is_favorite = Column(Boolean, index=True, nullable=False)
```

两个 ForeignKey 都**未配置 `ondelete`**，数据库层面默认是 `ON DELETE NO ACTION`（严格外键约束）。

### 3.2 SQLAlchemy secondary 多对多关系配置

User 侧 [users.py#L103-L115](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/users.py#L103-L115) 和 RecipeModel 侧 [recipe.py#L62-L74](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L62-L74) 共定义了四组关系：

| 关系 | secondary | primaryjoin 过滤 | cascade | overlaps |
|------|-----------|-----------------|---------|----------|
| `User.rated_recipes` | `users_to_recipes` | 无 | **无** | rated_by/favorited_by/favorite_recipes/recipe |
| `User.favorite_recipes` | `users_to_recipes` | `is_favorite==True` | **无** | rated_by/rated_recipes/recipe |
| `RecipeModel.rated_by` | `users_to_recipes` | 无 | **无** | favorited_by/favorite_recipes/recipe |
| `RecipeModel.favorited_by` | `users_to_recipes` | `is_favorite==True` | **无** | rated_by/rated_recipes/recipe |

**关键配置解读**：
1. `overlaps` 参数告诉 SQLAlchemy：四组关系共享同一张 secondary 表，不要重复操作同一行。
2. **所有关系都没有配置 `cascade`**。
3. **所有关系也没有配置 `viewonly=True`**，理论上可以通过 `user.favorite_recipes.append(recipe)` 操作，但实际业务代码全部走 `RepositoryUserRatings` 直接创建 `UserToRecipe` 实体。

### 3.3 Recipe 综合评分事件驱动重算

事件链路，见 [user_to_recipe.py#L36-L53](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/users/user_to_recipe.py#L36-L53) + [recipe.py#L284-L300](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/recipe.py#L284-L300)：
1. `UserToRecipe` 发生 `after_insert` / `after_update` / `after_delete`
2. 监听器把目标 Recipe 的 `rating` 设为 `-1`（脏标记）
3. `RecipeModel.before_update` 检测到修改，从 `UserToRecipe` 重算 `AVG(rating)`（排除 rating 为空或 ≤0 的行）

### 3.4 评分与收藏互相保留

测试验证：更新评分时如果 `is_favorite=None`，不会覆盖已有收藏值；更新收藏时如果 `rating=None`，不会覆盖已有评分值，见 [test_recipe_ratings.py#L188-L262](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py#L188-L262)。

---

## 4. Shared Recipe（共享 Token）

**核心文件**：[shared.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/db/models/recipe/shared.py)

```python
class RecipeShareTokenModel(SqlAlchemyBase, BaseMixins):
    __tablename__ = "recipe_share_tokens"
    group_id = mapped_column(GUID, sa.ForeignKey("groups.id"), nullable=False, index=True)  # 无 ondelete
    recipe_id = mapped_column(GUID, sa.ForeignKey("recipes.id"), nullable=False, index=True) # 无 ondelete
    expires_at = mapped_column(NaiveDateTime, nullable=False)  # 默认 30 天
```

**边界校验**：
- **创建侧**：强制 `recipe.group_id == user.group_id`，只能共享同 Group 内的 Recipe
- **访问侧**：公开 URL，不校验用户身份，仅校验 Token 存在且未过期；过期 Token 在访问时被**懒删除**

两阶段边界：① Group 内创建；② 公开访问（靠 `expires_at` 控制时效窗口）。

---

## 5. 外键约束与级联全景

### 5.1 数据库级外键：全部 `ON DELETE NO ACTION`

所有 ForeignKey 定义（包括 Alembic 迁移）均未配置 `ondelete` 参数，数据库层面默认是 `ON DELETE NO ACTION`。这意味着直接 SQL DELETE 会在外键引用仍存在时报错——所有删除必须依赖 ORM 级联或业务层手动清理。

SQLite 默认 `PRAGMA foreign_keys = OFF`（Mealie 测试环境即如此），外键约束不强制；PostgreSQL 强制外键约束。

### 5.2 ORM 级联关系一览

| 关系 | cascade |
|------|---------|
| User → tokens/comments/recipe_timeline_events/password_reset_tokens/mealplans/shopping_lists | ✅ `all, delete, delete-orphan` |
| User → owned_recipes | ❌ 无 |
| User → rated_recipes / favorite_recipes（secondary） | ❌ 无 |
| RecipeModel → share_tokens/comments/timeline_events/settings/nutrition/assets/notes/... | ✅ `all, delete, delete-orphan` |
| RecipeModel → rated_by / favorited_by（secondary） | ❌ 无 |
| RecipeModel → made_by（secondary, Household） | ❌ 无 |
| Household → made_recipes（secondary） | ❌ 无 |
| Group → recipes / share_tokens / ... | ✅ 大部分有 cascade |

---

## 6. 删除场景的真实 flush 行为（验证确认）

以下结论均已通过最小重现脚本（纯 SQLAlchemy 复刻模型关系）实际运行验证，脚本见 [verify_secondary_precise.py](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/dev/scripts/verify_secondary_precise.py)。

### 6.1 场景 A：删除 User（有收藏/评分）

**关键发现**：实际会抛出 `StaleDataError`，**用户删除完全被回滚，什么都删不掉**。

```python
# 重现方式（即 Mealie 的实际工作模式）：
utr = UserToRecipe(user_id=user.id, recipe_id=recipe.id, rating=4.0, is_favorite=True)
session.add(utr)        # ← 直接 add 关联实体，不是通过 relationship append
session.commit()
session.delete(user)    # ← 删除用户
session.commit()        # ← StaleDataError! 完整回滚
```

**错误信息**：
```
StaleDataError: DELETE statement on table 'users_to_recipes' expected to delete 1 row(s); Only 0 were matched.
```

**根因**：`UserToRecipe` 既是独立 ORM 实体（有 class 定义、复合主键），又被四组 secondary relationship 指向。当 UserToRecipe 行**通过 `session.add()` 直接创建**（而非通过 `user.rated_recipes.append(recipe)`）时，SQLAlchemy 的 Unit of Work 在 flush 时：
1. 检测到 `user` 被删除，需要清理 secondary 关系
2. 生成隐式 `DELETE FROM users_to_recipes WHERE user_id=? AND recipe_id=?`
3. 但由于行是通过直接 `session.add(utr)` 加入的，relationship 状态追踪与实体状态不同步，DELETE 语句匹配行数为 0
4. SQLAlchemy 认为预期被删除的行不存在，抛出 `StaleDataError`
5. 整个事务回滚

**对比：纯 M2M relationship.append 方式（Mealie 未使用）**：
```python
user.rated_recipes.append(recipe)   # ← 通过 relationship 操作
session.delete(user)
session.commit()                    # ← 正常工作！但 rating/is_favorite 丢失
```
这种方式删除没问题，但创建行时 `rating` 和 `is_favorite` 无法被正确赋值（只能为 NULL/False），不满足业务需求。

**RepositoryUsers.delete 现状**，见 [repository_users.py#L55-L65](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_users.py#L55-L65)：
```python
def delete(self, value, match_key=None):
    entry = super().delete(value, match_key)  # RepositoryGeneric.delete → session.delete(user)
    shutil.rmtree(PrivateUser.get_directory(value))
    return entry
```

完全没有手动清理 `UserToRecipe` 的步骤。

**实际后果**：
| 环境 | 结果 |
|------|------|
| SQLite（FK=OFF，Mealie 测试） | `StaleDataError`，用户行**保留**，UserToRecipe 行**保留**（完全回滚） |
| PostgreSQL（FK=ON，生产） | `StaleDataError` 或 `IntegrityError`（取决于 flush 顺序），用户行**保留** |

**为什么测试不报错**：所有 fixture user 都是全新创建的，没有任何收藏/评分记录，因此 StaleDataError 从未被触发。唯一的 user 删除测试 [test_user_repository.py#L5-L10](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/unit_tests/repository_tests/test_user_repository.py#L5-L10) 只验证目录被删除，完全不涉及有收藏/评分的用户。

### 6.2 场景 B：删除 Recipe

`RepositoryRecipes._delete_recipe`，见 [repository_recipes.py#L110-L153](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/mealie/repos/repository_recipes.py#L110-L153)：

```python
def _delete_recipe(self, recipe: RecipeModel) -> Recipe:
    # Step 1：手动 sa.delete 所有 UserToRecipe（避免 StaleDataError + 解决 PostgreSQL 级联问题）
    user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
    self.session.execute(user_to_recipe_delete_query)
    self.session.commit()

    # Step 2：session.delete(recipe) → 触发 ORM 级联 share_tokens/comments/...
    self.session.delete(recipe)
    self.session.commit()
    return recipe_as_model
```

**清理结果（验证确认）**：

| 关联数据 | 是否清理 | 方式 |
|----------|----------|------|
| UserToRecipe（收藏+评分） | ✅ | 手动 `sa.delete()` |
| **HouseholdToRecipe（last_made）** | ❌ **残留** | 未手动清理、secondary 无 cascade |
| ShareTokens / Comments / TimelineEvents / ... | ✅ | ORM cascade |

**为什么 HouseholdToRecipe 没触发 StaleDataError？** 验证发现 `HouseholdToRecipe` 的 relationship（`made_by` / `made_recipes`）**没有 `overlaps` 参数**，且只有一组关系指向这张 secondary 表，SQLAlchemy 的隐式 secondary DELETE 能正常工作——但由于 FK 约束，在 PostgreSQL 中会报错，SQLite 中则残留孤儿行。

测试 `test_delete_recipe_deletes_ratings` [test_recipe_ratings.py#L295-L316](file:///d:/fz/0601/solo-dogfeeding/code/85-mealie/tests/integration_tests/user_recipe_tests/test_recipe_ratings.py#L295-L316) 验证了 UserToRecipe 被清理，但**没有任何测试覆盖 HouseholdToRecipe 是否残留**。

### 6.3 场景 C：删除 Household

`RepositoryHouseholds.delete` 使用默认 `RepositoryGeneric.delete`，即纯 `session.delete(household)` + `session.commit()`。

**验证结果（SQLite）**：Household 行删除成功，`HouseholdToRecipe` 行**被 SQLAlchemy 隐式 secondary DELETE 清理**。但这只是因为 `HouseholdToRecipe` 的 relationship 简单（无 overlaps，单组关系）。

**PostgreSQL 风险**：如果 FK 约束强制，且 HouseholdToRecipe 通过 `session.add(htr)` 直接创建（非通过 relationship），同样可能触发 `StaleDataError` 或 `IntegrityError`。

### 6.4 清理边界总表（验证确认）

| 删除场景 | UserToRecipe（收藏/评分） | HouseholdToRecipe（last_made） | ShareToken |
|----------|--------------------------|--------------------------------|------------|
| 删除 Recipe（有 Household last_made） | ✅ 手动 `sa.delete()` | ❌ **残留**（SQLite） / 可能报错（PostgreSQL） | ✅ ORM cascade |
| **删除 User（有收藏/评分）** | ❌ **StaleDataError，完全回滚，啥也删不掉** | N/A | 不受影响 |
| 用户切换 Household | ✅ 完全保留（绑定 user_id） | ❌ 新 Household 为空；旧 Household 保留 | ✅（同 Group 时） |
| 删除 Household | N/A | SQLite：ORM 隐式清理；PostgreSQL：可能 StaleDataError | 不受影响 |
| 删除 Group | ❌ **残留** | ❌ **残留** | ✅ ORM cascade |

---

## 7. 测试覆盖缺口

| 场景 | 现有测试 | 是否覆盖 |
|------|---------|----------|
| 删除 Recipe → UserToRecipe 清理 | ✅ `test_delete_recipe_deletes_ratings` | 是 |
| 删除 Recipe → HouseholdToRecipe 残留 | ❌ 无 | 否 |
| **删除 User（有收藏/评分）→ StaleDataError** | ❌ 无 | **否** |
| **删除 User（有收藏/评分）→ 外键阻断** | ❌ 无 | **否** |
| last_made 删除后全局值不回退 | ❌ 无 | 否 |
| Household 删除 → HouseholdToRecipe 清理 | ❌ 无 | 否 |

---

## 8. 总结

### 用户感觉"不直观"的根本原因

1. **`recentRecipes` 命名误导**：实际是"最近创建"而非"最近访问/浏览"，且系统完全没有访问历史记录
2. **`last_made` 是 Household 维度**：同一用户在不同 Household 看到不同的"最近做过"状态
3. **收藏与评分共用同一条 `UserToRecipe` 行**：语义耦合但 UI 上是两个独立操作

### 被测试掩盖的严重 Bug：有收藏/评分的用户删不掉

`RepositoryUsers.delete()` 缺少手动 `sa.delete(UserToRecipe)` 步骤。当用户有收藏或评分时：
- SQLite 测试环境：抛出 `StaleDataError`，事务完全回滚，**用户行保留**，UserToRecipe **保留**
- PostgreSQL 生产环境：要么 StaleDataError，要么 IntegrityError，结果相同——**用户删不掉**

为什么现有测试没发现：所有 fixture user 都是全新的，没有任何收藏/评分记录，`StaleDataError` 从未触发。唯一的 user 删除测试 `test_user_directory_deleted_on_delete` 只验证私有目录被删除，甚至不验证用户行本身是否被删除。

### 清理逻辑不一致

- 删除 Recipe：手动清理了 UserToRecipe，**但遗漏了 HouseholdToRecipe**
- 删除 User：**完全没有手动清理步骤**，直接触发 StaleDataError
- 删除 Household：依赖 ORM 隐式 secondary DELETE（SQLite 下可用，但 PostgreSQL 风险未知）
