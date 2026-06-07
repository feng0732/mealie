# Mealie 数据迁移与版本升级机制详解

## 一、整体启动流程与迁移入口

Mealie 的数据迁移流程在 FastAPI 应用启动时通过 lifespan 钩子触发，完整调用链路如下：

`mealie/app.py` → `lifespan_fn()` → `mealie/db/init_db.py` → `main()`

迁移执行的核心顺序在 `mealie/db/init_db.py:85-132` 中定义：

```
1. 数据库连接重试（最多10次，间隔1秒）
2. 加载 Alembic 配置文件
3. db_is_at_head() 检查当前 DB 版本是否为最新
4. 若需要迁移 → command.upgrade(alembic_cfg, "head") 按顺序执行所有 Alembic 迁移脚本
5. PostgreSQL 创建 pg_trgm 扩展（用于模糊搜索和 GIN 索引）
6. 若存在用户（非首次安装）且执行过迁移：
   - safe_try(fix_migration_data)
   - safe_try(fix_slug_food_names)
   - safe_try(fix_group_with_no_name)
7. 若无用户（首次安装）：init_db() 初始化默认 Group / Household / User
```

关键点：Alembic 迁移在前，数据修复（fixes）在后。因为数据修复往往依赖刚完成的 schema 变更。

---

## 二、迁移顺序管理

### 2.1 Alembic 版本链

每个迁移脚本在 `mealie/alembic/versions/` 目录下，通过 `revision` 和 `down_revision` 字段构成单向链表。

命名格式：`YYYY-MM-DD-HH.MM.SS_<revision_id>_<description>.py`

示例（从旧到新的关键节点）：
- `6b0f5f32d602` (2022-02-21) Initial tables
- `263dd6707191` (2022-03-23) Convert quantity from integer to float
- `5ab195a474eb` (2023-02-14) Add normalized search properties
- `ff5f73b01a7a` (2023-02-07) Add missing foreign key and order indices
- `dded3119c1fe` (2023-10-04) Added unique constraints
- `bcfdad6b7355` (2023-08-15) Remove tool name and slug unique constraints
- `d7c6efd2de42` (2024-03-18) Migrate favorites and ratings to user_ratings
- `feecc8ffb956` (2024-07-12) Add households
- `b9e516e2d3b3` (2024-11-20) Add household to recipe last made
- `7cf3054cbbcc` (2025-02-09) Remove instructions index
- `c7427796f7b6` (2026-05-10) More aggressive normalization
- `2187537c52b8` (2026-05-18) Add table for AI providers

### 2.2 迁移执行模式

在 `mealie/alembic/env.py` 中配置：
- `render_as_batch=True`：启用批处理模式以支持 SQLite 的表重建式 schema 变更（SQLite 原生 ALTER TABLE 能力有限）
- `user_module_prefix="mealie.db.migration_types."`：自定义类型（GUID、NaiveDateTime）通过 `mealie/db/migration_types.py` 注入
- `include_object` 钩子：手动排除部分对象（如 `ingredient_foods_name_group_id_key` 唯一约束），避免 Alembic 误报差异

---

## 三、默认值填充策略

默认值填充发生在三个层面，各有分工：

### 3.1 ORM 模型层默认值

在 `mealie/db/models/_model_base.py:18-33` 的 `SqlAlchemyBase` 和各模型中定义：

```python
# SqlAlchemyBase 基类
created_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, default=get_utc_now, index=True)
update_at: Mapped[datetime | None] = mapped_column(NaiveDateTime, default=get_utc_now, onupdate=get_utc_now)

# RecipeModel 中
recipe_yield_quantity: mapped_column(sa.Float, index=True, default=0)
recipe_servings: mapped_column(sa.Float, index=True, default=0)
rating: mapped_column(sa.Float, index=True, nullable=True)  # 允许 NULL
```

### 3.2 迁移过程中的默认值

新添加 NOT NULL 列时，先通过 `server_default` 赋予临时默认值，数据填充后再移除：

见 `mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py:89-116`：

```python
# 步骤1：加列时给 server_default
op.add_column("recipes", sa.Column("name_normalized", sa.String(), nullable=False, server_default=""))

# 步骤2：do_data_migration() 填充所有历史数据

# 步骤3：移除 server_default，后续由应用层控制
batch_op.alter_column("name_normalized", existing_type=sa.String(), server_default=None)
```

### 3.3 数据迁移中的默认值回填

在复杂 schema 演进中，新表的行通过 Python 逻辑构造，显式填入默认值。

例如 `mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py:108-137` 创建 household_preferences 时：

```python
migrated_field_defaults = {
    "private_group": True,
    "first_day_of_week": 0,
    "recipe_public": True,
    "recipe_show_nutrition": False,
    "recipe_show_assets": False,
    "recipe_landscape_view": False,
    "recipe_disable_comments": False,
    "recipe_disable_amount": True,
}
# 若 group_preferences 中值为 None，用上述默认值兜底
value = group_preferences[i] if value is not None else default_value
```

### 3.4 GUID 跨平台兼容的"默认值"

`mealie/db/models/_model_utils/guid.py` 中的 `GUID` 是自定义 TypeDecorator，在不同数据库间产生不同的存储格式：

- PostgreSQL：原生 UUID 类型，存储为 `str(uuid4())`
- SQLite/其他：CHAR(32)，存储为 `f"{uuid4().int:032x}"`（无连字符的 32 位十六进制）

所有迁移脚本在需要手动生成 GUID 时（如 INSERT 语句），都使用 `generate_id()` 辅助函数判断 dialect。

---

## 四、Schema 演进模式

Mealie 的 schema 演进呈现出清晰的 **三步曲** 模式：

### 4.1 DDL（结构变更）先行

```python
op.create_table(...) / op.add_column(...) / batch_op.create_foreign_key(...)
```

### 4.2 数据迁移（Python 逻辑填充）

通过 `op.get_bind()` 获取连接，创建本地 ORM Session，读取旧表/旧列，写入新表/新列。

典型模式：在迁移文件内定义 **临时 ORM 模型**（不依赖外部模型，防止未来模型变更破坏历史迁移），例如 `mealie/alembic/versions/2024-03-18-02.28.15_d7c6efd2de42_migrate_favorites_and_ratings_to_user_.py:31-46` 中的 `new_user_rating()` 函数。

### 4.3 清理旧结构（可选）

数据迁移完成后 drop 旧列、旧索引、旧表。

---

## 五、旧 Recipe 数据兼容机制

Recipe 是 Mealie 的核心实体，其数据兼容主要通过 **规范化字段演进**、**关联表迁移** 和 **评分体系重构** 三条路径实现。

### 5.1 规范化字段演进（搜索兼容）

Mealie 的搜索依赖 `_normalized` 后缀字段，规范化算法在 `mealie/db/models/_model_base.py:30-33` 中定义：

```python
@classmethod
def normalize(cls, val: str) -> str:
    return unidecode(val).translate(_NORMALIZE_PUNCTUATION_TABLE).lower().strip()[:255]
```

涉及 Recipe 相关规范化字段的迁移共三次：

| 迁移版本 | 变更内容 |
|---------|---------|
| `mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py` | 首次引入 `name_normalized`、`description_normalized`、`note_normalized`、`original_text_normalized`，用 `unidecode().lower().strip()` 填充 |
| `mealie/alembic/versions/2025-02-09-15.31.00_7cf3054cbbcc_remove_instructions_index.py` | 截断所有 `_normalized` 字段到 255 字符（PostgreSQL btree 索引限制），并移除 `ix_recipe_instructions_text` 索引 |
| `mealie/alembic/versions/2026-05-10-18.44.53_c7427796f7b6_more_aggresive_normalization.py` | **更激进的规范化**：将所有标点符号（除单/双引号）替换为空格，对 6 张表全部重新规范化 |

此外，每次迁移后的 `mealie/db/fixes/fix_migration_data.py` 还会检查并补填缺失的规范化字段（`fix_recipe_normalized_search_properties`、`fix_normalized_unit_and_food_names`）。

### 5.2 评分与收藏体系重构（用户级兼容）

`mealie/alembic/versions/2024-03-18-02.28.15_d7c6efd2de42_migrate_favorites_and_ratings_to_user_.py` 将 **Group 级别的 rating** 和 **独立的 users_to_favorites 表** 重构为统一的 `users_to_recipes` 关联表：

```
旧模型：
  recipes.rating (integer, group级别)
  users_to_favorites (user_id, recipe_id)

新模型：
  users_to_recipes (user_id PK, recipe_id PK, rating, is_favorite)
  recipes.rating (float, nullable, 保留但降级使用)
```

数据迁移策略：
1. 将 `users_to_favorites` 全部记录复制为 `is_favorite=True` 的行
2. 对每个 group，将 `recipes.rating` 复制给该 group 的 **所有用户**（因为不知道是谁评的分）
3. 用 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE` 保证幂等

### 5.3 Household 引入（组织层级兼容）

这是最大规模的架构变更，涉及两个迁移：

**阶段1 — `mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py`**
- 为每个 Group 创建一个默认 Household（名称来自 settings.DEFAULT_HOUSEHOLD）
- 将 group_preferences 的配置复制为 household_preferences（`private_group` → `private_household`）
- 给 cookbooks、users、webhooks、invite_tokens 等 7 张表添加 `household_id` 列并回填

**阶段2 — `mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py`**
- 新建 `households_to_recipes` 关联表，将 `recipes.last_made` 迁移为每个 household 独立的 last_made 记录
- 新建 `households_to_ingredient_foods`、`households_to_tools`，迁移 on_hand 标记
- 同样采用"每个 group 的所有 household 各复制一份"策略

### 5.4 类型变更兼容（Quantity 整数→浮点）

`mealie/alembic/versions/2022-03-23-17.43.34_263dd6707191_convert_quantity_from_integer_to_float.py` 利用了 SQLite 弱类型特性：

```python
if is_postgres():
    op.alter_column("recipes_ingredients", "quantity", type_=sa.Float(), existing_type=sa.Integer())
# SQLite 不需要迁移，因为类型不强制
```

同时在 `mealie/alembic/env.py:44-51` 的 `include_object` 钩子中专门排除此列差异，避免 Alembic 每次 autogenerate 都报警。

---

## 六、失败恢复机制与各层边界

> **重要区分**：Alembic schema 迁移失败和连接失败是**致命的**（将导致应用启动中断）；只有 fixes 层的数据修复脚本失败是非致命的（仅记录日志，应用继续启动）。

### 6.1 数据库连接重试——边界：10 次后强制终止

位置：`mealie/db/init_db.py:86-102`

```python
max_retry = 10
wait_seconds = 1
while True:
    if connect(session):
        break
    max_retry -= 1
    sleep(wait_seconds)
    if max_retry == 0:
        raise ConnectionError("Database connection failed - exiting application.")
```

- **覆盖范围**：仅数据库连接建立阶段
- **行为**：最多重试 10 次，每次间隔 1 秒
- **失败后果**：抛出 `ConnectionError`，沿调用栈向上传播到 FastAPI lifespan，**应用启动中断（致命）**

### 6.2 Alembic 迁移事务层——边界：command.upgrade() 无外层捕获，失败即中断

位置：`mealie/alembic/env.py:102-103`

```python
with context.begin_transaction():
    context.run_migrations()
```

位置：`mealie/db/init_db.py:113-116`

```python
else:
    logger.info("Migration needed. Performing migration...")
    command.upgrade(alembic_cfg, "head")   # 无 try/except 包裹
    run_fixes = True
```

- **覆盖范围**：所有 Alembic 迁移脚本的 DDL 和其中的数据迁移逻辑
- **事务粒度**：单个迁移脚本（revision）内的所有操作在一个数据库事务中执行，失败自动回滚该脚本的全部变更
- **失败后果**：`command.upgrade()` 抛出的任何异常都**没有外层 try/except 捕获**，直接传播到 lifespan，**应用启动中断（致命）**

这意味着：**任何一个 Alembic 迁移脚本失败，整个应用都无法启动。** 管理员必须修复数据库或迁移脚本后重新启动。

### 6.3 迁移脚本内部的局部 try/except——边界：不同迁移策略不同

迁移脚本内部的数据迁移子任务有时会自带 try/except，但行为分为两类：

**类型 A：捕获后重新 raise（仍然致命）**

位置：`mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py:157-173` 的 `migrate_to_new_models()`：

```python
for migration_func in [...]:
    try:
        migration_func(session)
        session.commit()
    except Exception:
        session.rollback()
        logger.error(...)
        raise  # 显式向上抛出，终止整个迁移
```

- 行为：回滚当前子任务的 session，记录错误日志，然后 `raise` 重新抛出
- 失败后果：被外层 `context.run_migrations()` 捕获，触发事务回滚，最终导致**应用启动中断（致命）**

**类型 B：捕获后不 raise（局部失败，不影响同脚本其他子任务）**

位置：`mealie/alembic/versions/2025-02-09-15.31.00_7cf3054cbbcc_remove_instructions_index.py:103-128` 的 `truncate_normalized_fields()`：

```python
for model in models:
    ...
    try:
        session.commit()
    except Exception:
        logger.exception(f"Failed to truncate normalized fields for {model.__name__}")
        session.rollback()  # 不 raise，继续处理下一个模型
```

- 行为：单个模型失败仅回滚该模型的更新，记录日志后继续处理后续模型
- 失败后果：该迁移脚本本身标记为成功执行（Alembic revision 前进），但部分数据可能未被正确规范化；应用可正常启动，属于**静默数据不完整**风险

### 6.4 Fixes 层的 safe_try——边界：仅记录日志，绝不阻塞启动

位置：`mealie/db/init_db.py:69-73`

```python
def safe_try(func: Callable):
    try:
        func()
    except Exception:
        logger.exception(f"Error calling '{func.__name__}'")
```

位置：`mealie/db/init_db.py:125-128`

```python
if run_fixes:
    safe_try(lambda: fix_migration_data(session))
    safe_try(lambda: fix_slug_food_names(db))
    safe_try(lambda: fix_group_with_no_name(session))
```

- **覆盖范围**：仅以下三个数据质量修复脚本
  1. `mealie/db/fixes/fix_migration_data.py`（悬垂引用清理、规范化字段补填、标签设置修复、group slug 生成、单位/食品规范化）
  2. `mealie/db/fixes/fix_slug_foods.py`（食品种子数据名称同步）
  3. `mealie/db/fixes/fix_group_with_no_name.py`（空名称 group 赋默认值）
- **行为**：捕获所有 Exception，写完整堆栈日志，不重新抛出
- **失败后果**：**非致命，不阻塞应用启动**。但失败意味着某些数据质量问题未被修复，后续使用中可能因脏数据引发业务逻辑错误

### 6.5 IntegrityError 重试——边界：fix_group_with_no_name 内的局部重试

位置：`mealie/db/fixes/fix_group_with_no_name.py:22-48`

```python
for i, group in enumerate(groups):
    attempts = 0
    while True:
        if attempts >= 3:
            raise Exception(
                f'Unable to fix empty group name for group_id "{group.id}": too many attempts ({attempts})'
            )
        ...
        try:
            _do_fix(session, group, counter)
            break
        except IntegrityError:
            session.rollback()
            attempts += 1
            offset += 1
            continue
```

- **覆盖范围**：仅空名称 group 的 slug 生成阶段（用于解决 slug 冲突）
- **行为**：最多重试 3 次（通过递增后缀数字规避冲突）
- **失败后果**：超过重试次数后 `raise Exception`——但由于整个 `fix_group_with_no_name` 被 `safe_try` 包裹，异常最终被 safe_try 捕获，仅记日志不中断启动

### 6.6 各层致命性总览

| 层级 | 机制 | 失败是否中断启动 | 数据一致性影响 |
|------|------|:---:|---|
| 数据库连接 | 10 次重试 → `ConnectionError` | **是** | 未建立连接，无数据操作 |
| Alembic 迁移事务 | `command.upgrade()` 无外层捕获，迁移脚本事 务回滚 | **是** | 当前 revision 的 DDL 回滚，已成功的 revision 不会回滚（数据库停留在中间版本） |
| 迁移脚本内（类型 A） | try/except + `raise` 重抛 | **是** | 子任务 session 回滚，整脚本事 务回滚 |
| 迁移脚本内（类型 B） | try/except + 不 raise | 否 | 局部数据可能不完整（如某模型规范化未执行） |
| Fixes 层 | `safe_try` 吞掉所有异常 | 否 | 数据质量问题遗留（悬垂引用、缺失规范化、冲突 slug 等） |
| IntegrityError 重试 | 3 次后 `raise`，但外层有 `safe_try` | 否 | 个别空名称 group 未修复 |

---

## 七、索引变更策略

索引是影响查询性能和迁移时间的关键因素。Mealie 历史上发生过 3 类索引变更：

### 7.1 大规模补建索引

`mealie/alembic/versions/2023-02-07-20.57.21_ff5f73b01a7a_add_missing_foreign_key_and_order_.py` 一次性补建了 **90+ 个索引**，涵盖：
- 所有外键列（`*_id`）
- 所有 `created_at` 时间戳
- 排序字段（`position`）
- 频繁筛选列（`slug`、`name`、`token`、`rating` 等）

### 7.2 唯一约束 ↔ 普通索引的转换

| 迁移 | 操作 |
|------|------|
| `mealie/alembic/versions/2023-08-15-16.25.07_bcfdad6b7355_remove_tool_name_and_slug_unique_.py` | tools.name 和 tools.slug 从唯一索引降级为普通索引（允许跨 group 重名） |
| `mealie/alembic/versions/2023-10-04-14.29.26_dded3119c1fe_added_unique_constraints.py` | 11 张 M2M 表 + ingredient_foods/units/labels 全部加上组合唯一约束（先去重再加约束） |
| `mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py` | cookbooks 增加 `(slug, group_id)` 唯一约束，迁移前先执行 `dedupe_cookbook_slugs()` 去重 |

### 7.3 搜索索引演进

规范化字段引入时，**先建索引再填数据** 的反向操作：

见 `mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py:91-116`：

```python
# 删除旧索引（基于原始文本）
op.drop_index("ix_recipes_name", table_name="recipes")
op.drop_index("ix_recipes_description", table_name="recipes")
# 创建新索引（基于规范化文本）
op.create_index(op.f("ix_recipes_name_normalized"), "recipes", ["name_normalized"], unique=False)
op.create_index(op.f("ix_recipes_description_normalized"), "recipes", ["description_normalized"], unique=False)
```

PostgreSQL 还需要在 `mealie/db/init_db.py:118-119` 创建 `pg_trgm` 扩展支持 trigram 模糊搜索。

---

## 八、外键清理与悬垂引用修复

`mealie/db/fixes/fix_migration_data.py` 中的 `fix_dangling_refs()` 是外键清理的核心。

### 8.1 两类引用的不同处理策略

```python
REASSIGN_REF_TABLES = ["group_meal_plans", "recipes", "shopping_lists"]
DELETE_REF_TABLES = ["long_live_tokens", "password_reset_tokens", "recipe_comments", "recipe_timeline_events"]
```

- **REASSIGN（重新分配）**：业务上重要的数据（如 recipe 本身），找到同 group 的 admin 用户（或第一个用户）作为兜底所有者，通过 UPDATE ... WHERE user_id NOT IN (...) 重新分配。
- **DELETE（直接删除）**：附属数据（token、评论、时间线事件），user_id 无效时直接 DELETE。

### 8.2 唯一约束前的外键重定向

`mealie/alembic/versions/2023-10-04-14.29.26_dded3119c1fe_added_unique_constraints.py` 在为 foods/units/labels 加唯一约束前，必须先合并重复项：

```python
def _resolve_duplicate_food(session, keep_food_id, dupe_food_id):
    # 1. 将所有引用 dupe_food_id 的 shopping_list_items 指向 keep_food_id
    # 2. 将所有引用 dupe_food_id 的 recipe_ingredients 指向 keep_food_id
    # 3. 删除 dupe_food_id 本身
```

对 11 张 M2M 表，则使用基于数据库内部行号（PostgreSQL CTID / SQLite ROWID）的 DELETE 语句：

```sql
DELETE FROM cookbooks_to_categories
WHERE EXISTS (
    SELECT 1 FROM cookbooks_to_categories t2
    WHERE cookbooks_to_categories.cookbook_id = t2.cookbook_id
      AND cookbooks_to_categories.category_id = t2.category_id
      AND cookbooks_to_categories.CTID > t2.CTID   -- 保留最早插入的行
)
```

### 8.3 购物清单标签设置修复

`mealie/db/fixes/fix_migration_data.py:103-133` 的 `fix_shopping_list_label_settings()`：
- 删除已不存在的 label 对应的 label_setting
- 为新增的 label 自动补全 label_setting（保持 position 顺序）

---

## 九、整体架构关系图

```
                    ┌─────────────────────┐
                    │  FastAPI lifespan   │
                    │   (app.py)          │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  init_db.main()     │
                    │  (10 次连接重试)    │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐  ┌─────▼──────┐  ┌──────▼─────────┐
    │ db_is_at_head()│  │ pg_trgm    │  │ 用户存在？     │
    │ (版本检查)     │  │ (PG only)  │  │                │
    └─────────┬──────┘  └────────────┘  └──────┬─────────┘
              │ 不是 HEAD                     │
    ┌─────────▼──────────┐               ┌────▼────┐     ┌──────────────┐
    │ command.upgrade()  │               │ 是      │     │ 否           │
    │ (Alembic 按顺序执行│               └────┬────┘     └──────┬───────┘
    │ 每个迁移: DDL→数据 │                    │                 │
    │ 迁移→清理)          │           ┌───────▼───────┐  ┌─────▼──────┐
    │ ⚠️ 失败：中断启动   │           │ safe_try 修复 │  │ init_db()  │
    └─────────┬──────────┘           └───────┬───────┘  │ 默认数据    │
              │                      ⚠️ 失败：仅记日志  └────────────┘
              │ run_fixes=True               │
              └──────────────┬───────────────┘
                             │
              ┌──────────────┼───────────────────┐
              │              │                   │
    ┌─────────▼──────┐ ┌────▼──────────┐ ┌──────▼──────────┐
    │ fix_migration_ │ │ fix_slug_food_│ │ fix_group_with_ │
    │ data()         │ │ names()       │ │ no_name()       │
    │  - dangling    │ │ (种子数据名称 │ │ (空名称 group    │
    │    refs        │ │  同步)        │ │  赋默认值)      │
    │  - normalized  │ └───────────────┘ └─────────────────┘
    │    fields      │
    │  - label sett. │
    │  - group slugs │
    │  - unit/food   │
    │    normalized  │
    └────────────────┘
```

---

## 十、关键设计总结

1. **迁移 ≠ 修复**：Alembic 负责 schema 演进，`db/fixes/` 负责数据质量修复。两者分离，修复可独立重跑。
2. **先建结构再填数据最后卸磨杀驴**：新 NOT NULL 列先给 `server_default`，数据填充后移除 server_default。
3. **幂等优先**：所有数据迁移使用 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`，可重复执行。
4. **中间模型自包含**：迁移脚本内部定义临时 ORM 模型，不依赖外部模型文件，避免未来模型变更破坏历史迁移。
5. **跨方言兼容**：GUID、数量类型、唯一约束都按 dialect 分支处理；SQLite 用 batch_alter_table 模拟 DDL。
6. **规范化字段是搜索的真相来源**：算法三次演进，每次全量重算，fix 层兜底补填。
7. **去重是加唯一约束的前置动作**：M2M 表靠数据库内部行号，实体表靠外键重定向+删除。
8. **失败的致命性分层明确**：连接失败和 Alembic schema 迁移失败是**致命的**（应用无法启动，数据库可能停留在中间 revision 版本）；迁移脚本内部个别数据子任务失败可能静默遗留数据不完整；fixes 层数据修复失败**非致命**但会遗留脏数据风险，需人工排查日志处理。
