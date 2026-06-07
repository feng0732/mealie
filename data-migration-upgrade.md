# Mealie 数据迁移与版本升级机制详解

## 一、整体启动流程与迁移入口

Mealie 的数据迁移流程在 FastAPI 应用启动时通过 lifespan 钩子触发，完整调用链路如下：

[app.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/app.py) → `lifespan_fn()` → [init_db.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/init_db.py) → `main()`

迁移执行的核心顺序在 [init_db.py:85-132](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/init_db.py#L85-L132) 中定义：

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

每个迁移脚本在 [mealie/alembic/versions/](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/) 目录下，通过 `revision` 和 `down_revision` 字段构成单向链表。

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

在 [env.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/env.py) 中配置：
- `render_as_batch=True`：启用批处理模式以支持 SQLite 的表重建式 schema 变更（SQLite 原生 ALTER TABLE 能力有限）
- `user_module_prefix="mealie.db.migration_types."`：自定义类型（GUID、NaiveDateTime）通过 [migration_types.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/migration_types.py) 注入
- `include_object` 钩子：手动排除部分对象（如 `ingredient_foods_name_group_id_key` 唯一约束），避免 Alembic 误报差异

---

## 三、默认值填充策略

默认值填充发生在三个层面，各有分工：

### 3.1 ORM 模型层默认值

在 [SqlAlchemyBase](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/models/_model_base.py#L18-L33) 和各模型中定义：

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

见 [5ab195a474eb_add_normalized_search_properties.py:89-116](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py#L89-L116)：

```python
# 步骤1：加列时给 server_default
op.add_column("recipes", sa.Column("name_normalized", sa.String(), nullable=False, server_default=""))

# 步骤2：do_data_migration() 填充所有历史数据

# 步骤3：移除 server_default，后续由应用层控制
batch_op.alter_column("name_normalized", existing_type=sa.String(), server_default=None)
```

### 3.3 数据迁移中的默认值回填

在复杂 schema 演进中，新表的行通过 Python 逻辑构造，显式填入默认值。

例如 [feecc8ffb956_add_households.py:108-137](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py#L108-L137) 创建 household_preferences 时：

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

[GUID](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/models/_model_utils/guid.py) 是自定义 TypeDecorator，在不同数据库间产生不同的存储格式：

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

典型模式：在迁移文件内定义 **临时 ORM 模型**（不依赖外部模型，防止未来模型变更破坏历史迁移），例如 [d7c6efd2de42_migrate_favorites_and_ratings.py:31-46](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-03-18-02.28.15_d7c6efd2de42_migrate_favorites_and_ratings_to_user_.py#L31-L46) 中的 `new_user_rating()` 函数。

### 4.3 清理旧结构（可选）

数据迁移完成后 drop 旧列、旧索引、旧表。

---

## 五、旧 Recipe 数据兼容机制

Recipe 是 Mealie 的核心实体，其数据兼容主要通过 **规范化字段演进**、**关联表迁移** 和 **评分体系重构** 三条路径实现。

### 5.1 规范化字段演进（搜索兼容）

Mealie 的搜索依赖 `_normalized` 后缀字段，规范化算法在 [_model_base.py:30-33](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/models/_model_base.py#L30-L33) 中定义：

```python
@classmethod
def normalize(cls, val: str) -> str:
    return unidecode(val).translate(_NORMALIZE_PUNCTUATION_TABLE).lower().strip()[:255]
```

涉及 Recipe 相关规范化字段的迁移共三次：

| 迁移版本 | 变更内容 |
|---------|---------|
| [5ab195a474eb](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py) | 首次引入 `name_normalized`、`description_normalized`、`note_normalized`、`original_text_normalized`，用 `unidecode().lower().strip()` 填充 |
| [7cf3054cbbcc](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2025-02-09-15.31.00_7cf3054cbbcc_remove_instructions_index.py) | 截断所有 `_normalized` 字段到 255 字符（PostgreSQL btree 索引限制），并移除 `ix_recipe_instructions_text` 索引 |
| [c7427796f7b6](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2026-05-10-18.44.53_c7427796f7b6_more_aggresive_normalization.py) | **更激进的规范化**：将所有标点符号（除单/双引号）替换为空格，对 6 张表全部重新规范化 |

此外，每次迁移后的 [fix_migration_data](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/fixes/fix_migration_data.py) 还会检查并补填缺失的规范化字段（`fix_recipe_normalized_search_properties`、`fix_normalized_unit_and_food_names`）。

### 5.2 评分与收藏体系重构（用户级兼容）

[d7c6efd2de42_migrate_favorites_and_ratings_to_user_.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-03-18-02.28.15_d7c6efd2de42_migrate_favorites_and_ratings_to_user_.py) 将 **Group 级别的 rating** 和 **独立的 users_to_favorites 表** 重构为统一的 `users_to_recipes` 关联表：

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

**阶段1 — [feecc8ffb956_add_households.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py)**
- 为每个 Group 创建一个默认 Household（名称来自 settings.DEFAULT_HOUSEHOLD）
- 将 group_preferences 的配置复制为 household_preferences（`private_group` → `private_household`）
- 给 cookbooks、users、webhooks、invite_tokens 等 7 张表添加 `household_id` 列并回填

**阶段2 — [b9e516e2d3b3_add_household_to_recipe_last_made_.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py)**
- 新建 `households_to_recipes` 关联表，将 `recipes.last_made` 迁移为每个 household 独立的 last_made 记录
- 新建 `households_to_ingredient_foods`、`households_to_tools`，迁移 on_hand 标记
- 同样采用"每个 group 的所有 household 各复制一份"策略

### 5.4 类型变更兼容（Quantity 整数→浮点）

[263dd6707191_convert_quantity_from_integer_to_float.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2022-03-23-17.43.34_263dd6707191_convert_quantity_from_integer_to_float.py) 利用了 SQLite 弱类型特性：

```python
if is_postgres():
    op.alter_column("recipes_ingredients", "quantity", type_=sa.Float(), existing_type=sa.Integer())
# SQLite 不需要迁移，因为类型不强制
```

同时在 [env.py:44-51](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/env.py#L44-L51) 的 `include_object` 钩子中专门排除此列差异，避免 Alembic 每次 autogenerate 都报警。

---

## 六、失败恢复机制

Mealie 的失败恢复采用 **多层防御** 架构：

### 6.1 数据库连接层

[init_db.py:76-102](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/init_db.py#L76-L102)：最多重试 10 次，每次间隔 1 秒。超时后抛出 `ConnectionError` 使应用无法启动（fail-fast）。

### 6.2 Alembic 迁移事务层

在 [env.py:102-103](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/env.py#L102-L103) 中：

```python
with context.begin_transaction():
    context.run_migrations()
```

单个迁移脚本内的所有 DDL 在一个事务中执行，失败自动回滚。

### 6.3 数据迁移脚本内的局部 try/except

[b9e516e2d3b3:157-173](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py#L157-L173) 的 `migrate_to_new_models()`：

```python
for migration_func in [...]:
    try:
        migration_func(session)
        session.commit()
    except Exception:
        session.rollback()
        logger.error(...)
        raise  # 选择向上抛出，终止整个迁移
```

[7cf3054cbbcc:103-128](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2025-02-09-15.31.00_7cf3054cbbcc_remove_instructions_index.py#L103-L128) 的 `truncate_normalized_fields()`：每个模型独立提交，一个失败不影响其他模型。

### 6.4 Fixes 层的 safe_try

[init_db.py:69-73](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/init_db.py#L69-L73) 定义：

```python
def safe_try(func: Callable):
    try:
        func()
    except Exception:
        logger.exception(f"Error calling '{func.__name__}'")
```

三个数据修复脚本（fix_migration_data、fix_slug_food_names、fix_group_with_no_name）都被 safe_try 包裹——修复失败仅记录日志，不阻塞应用启动。

### 6.5 IntegrityError 重试

[fix_group_with_no_name.py:22-48](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/fixes/fix_group_with_no_name.py#L22-L48)：给空名称 group 赋值时如果 slug 冲突，最多重试 3 次，每次递增后缀数字。

---

## 七、索引变更策略

索引是影响查询性能和迁移时间的关键因素。Mealie 历史上发生过 3 类索引变更：

### 7.1 大规模补建索引

[ff5f73b01a7a_add_missing_foreign_key_and_order_.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-02-07-20.57.21_ff5f73b01a7a_add_missing_foreign_key_and_order_.py) 一次性补建了 **90+ 个索引**，涵盖：
- 所有外键列（`*_id`）
- 所有 `created_at` 时间戳
- 排序字段（`position`）
- 频繁筛选列（`slug`、`name`、`token`、`rating` 等）

### 7.2 唯一约束 ↔ 普通索引的转换

| 迁移 | 操作 |
|------|------|
| [bcfdad6b7355](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-08-15-16.25.07_bcfdad6b7355_remove_tool_name_and_slug_unique_.py) | tools.name 和 tools.slug 从唯一索引降级为普通索引（允许跨 group 重名） |
| [dded3119c1fe](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-10-04-14.29.26_dded3119c1fe_added_unique_constraints.py) | 11 张 M2M 表 + ingredient_foods/units/labels 全部加上组合唯一约束（先去重再加约束） |
| [feecc8ffb956](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py) | cookbooks 增加 `(slug, group_id)` 唯一约束，迁移前先执行 `dedupe_cookbook_slugs()` 去重 |

### 7.3 搜索索引演进

规范化字段引入时，**先建索引再填数据** 的反向操作：

见 [5ab195a474eb:91-116](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py#L91-L116)：

```python
# 删除旧索引（基于原始文本）
op.drop_index("ix_recipes_name", table_name="recipes")
op.drop_index("ix_recipes_description", table_name="recipes")
# 创建新索引（基于规范化文本）
op.create_index(op.f("ix_recipes_name_normalized"), "recipes", ["name_normalized"], unique=False)
op.create_index(op.f("ix_recipes_description_normalized"), "recipes", ["description_normalized"], unique=False)
```

PostgreSQL 还需要在 [init_db.py:118-119](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/init_db.py#L118-L119) 创建 `pg_trgm` 扩展支持 trigram 模糊搜索。

---

## 八、外键清理与悬垂引用修复

[fix_migration_data.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/fixes/fix_migration_data.py) 中的 `fix_dangling_refs()` 是外键清理的核心。

### 8.1 两类引用的不同处理策略

```python
REASSIGN_REF_TABLES = ["group_meal_plans", "recipes", "shopping_lists"]
DELETE_REF_TABLES = ["long_live_tokens", "password_reset_tokens", "recipe_comments", "recipe_timeline_events"]
```

- **REASSIGN（重新分配）**：业务上重要的数据（如 recipe 本身），找到同 group 的 admin 用户（或第一个用户）作为兜底所有者，通过 UPDATE ... WHERE user_id NOT IN (...) 重新分配。
- **DELETE（直接删除）**：附属数据（token、评论、时间线事件），user_id 无效时直接 DELETE。

### 8.2 唯一约束前的外键重定向

[dded3119c1fe_added_unique_constraints.py](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/alembic/versions/2023-10-04-14.29.26_dded3119c1fe_added_unique_constraints.py) 在为 foods/units/labels 加唯一约束前，必须先合并重复项：

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

[fix_migration_data.py:103-133](file:///d:/fz/0601/solo-dogfeeding/code/83-mealie/mealie/db/fixes/fix_migration_data.py#L103-L133) 的 `fix_shopping_list_label_settings()`：
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
    └─────────┬──────────┘           │ safe_try 修复 │  │ init_db()  │
              │                      └───────┬───────┘  │ 默认数据    │
              │ run_fixes=True               │          └────────────┘
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
8. **失败不致命**：连接层重试、迁移层事务、修复层 safe_try，三层防御保证升级失败可诊断、可恢复。
