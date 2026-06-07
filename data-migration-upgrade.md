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

这三次规范化迁移全部使用 **普通 `UPDATE ... SET ... WHERE id = :id` + `session.commit()`**，不依赖数据库冲突机制保证幂等，而是依赖 Alembic revision 的单次执行特性（详见第六章）。

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
3. 这是整个代码库中 **唯一使用 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE` 保证幂等的数据迁移**：

```python
# PostgreSQL
INSERT INTO users_to_recipes (...) VALUES (...) ON CONFLICT DO NOTHING
-- SQLite
INSERT OR IGNORE INTO users_to_recipes (...) VALUES (...)
```

对于已存在的 `(user_id, recipe_id)` 行（可能由第一步收藏迁移插入），第二步还会通过 `UPDATE` 语句覆写 rating 值以确保最终一致。

### 5.3 Household 引入（组织层级兼容）

这是最大规模的架构变更，涉及两个迁移：

**阶段1 — `mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py`**
- 为每个 Group 创建一个默认 Household（名称来自 settings.DEFAULT_HOUSEHOLD），使用纯 `INSERT INTO households`（无冲突处理）
- 将 group_preferences 的配置复制为 household_preferences（`private_group` → `private_household`），使用纯 `INSERT INTO household_preferences`
- 给 cookbooks、users、webhooks、invite_tokens 等 7 张表添加 `household_id` 列，用纯 `UPDATE ... SET household_id = :household_id WHERE group_id = :group_id` 批量回填

**阶段2 — `mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py`**
- 新建 `households_to_recipes` 关联表，将 `recipes.last_made` 迁移为每个 household 独立的 last_made 记录，使用 ORM `session.add(HouseholdToRecipe(...))` 逐行插入
- 新建 `households_to_ingredient_foods`、`households_to_tools`，迁移 on_hand 标记，使用纯 `INSERT INTO` 语句
- 同样采用"每个 group 的所有 household 各复制一份"策略

两个阶段均 **未使用任何冲突检测/忽略机制**，完全依赖 Alembic revision 单次执行保证数据正确性。

### 5.4 类型变更兼容（Quantity 整数→浮点）

`mealie/alembic/versions/2022-03-23-17.43.34_263dd6707191_convert_quantity_from_integer_to_float.py` 利用了 SQLite 弱类型特性：

```python
if is_postgres():
    op.alter_column("recipes_ingredients", "quantity", type_=sa.Float(), existing_type=sa.Integer())
# SQLite 不需要迁移，因为类型不强制
```

同时在 `mealie/alembic/env.py:44-51` 的 `include_object` 钩子中专门排除此列差异，避免 Alembic 每次 autogenerate 都报警。

---

## 六、幂等性与 Alembic revision 单次执行边界

### 6.1 幂等性实际使用情况核对

**结论：整个代码库中只有「评分与收藏迁移」这一个数据迁移脚本使用了 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`。** 其余所有数据迁移都依赖 Alembic 的 revision 追踪机制来避免重复执行。

逐脚本核对结果：

| 迁移脚本 | 数据写入方式 | 是否使用 ON CONFLICT / INSERT OR IGNORE |
|---------|------------|:---:|
| `5ab195a474eb`（规范化搜索字段） | `UPDATE ... SET ... WHERE id = :id` + `session.commit()` | 否 |
| `d7c6efd2de42`（评分与收藏迁移） | `INSERT ... ON CONFLICT DO NOTHING` / `INSERT OR IGNORE` + `UPDATE` | **是**（全库唯一） |
| `feecc8ffb956`（引入 households） | `INSERT INTO households` / `INSERT INTO household_preferences` / `UPDATE ... SET household_id` | 否 |
| `b9e516e2d3b3`（last_made/on_hand 迁移） | `session.add(...)`（ORM INSERT） / 纯 `INSERT INTO` 关联表 | 否 |
| `7cf3054cbbcc`（截断规范化字段） | ORM `setattr(record, field.key, field_value[:255])` + `session.commit()` | 否 |
| `c7427796f7b6`（更激进规范化） | `UPDATE ... SET col = :col WHERE id = :id` + `session.commit()` | 否 |
| `dded3119c1fe`（唯一约束前去重） | ORM 属性修改 + `DELETE FROM ... WHERE CTID > t2.CTID` + 多次 `session.commit()` | 否 |
| `263dd6707191`（quantity 类型转换） | 仅 DDL，无数据迁移 | — |

### 6.2 Alembic revision 单次执行的边界

绝大多数数据迁移脚本的正确性依赖以下前提：**每个 Alembic revision 在一个数据库实例上只会被执行一次。**

这个前提由 Alembic 内置机制保证：

1. `alembic_version` 表记录所有已成功执行的 revision 编号
2. `command.upgrade(cfg, "head")` 只会执行当前未出现在 `alembic_version` 中的 revision
3. 迁移脚本在 `context.begin_transaction()` 包裹的数据库事务中执行，失败回滚后该 revision 不会写入 `alembic_version`，下次启动会重新尝试

**revision 单次执行的边界条件（即该前提失效的场景）：**

- **手动修改 `alembic_version` 表**：删除已执行 revision 的记录后再次启动应用，对应迁移会被重新执行
- **执行 `alembic downgrade` 后再 upgrade**：downgrade 会从 `alembic_version` 移除记录，再次 upgrade 时重新执行
- **SQLite `batch_alter_table` 内部表重建失败**：表重建阶段如果进程崩溃，可能留下临时表但 revision 未记录，属于数据库损坏级别的故障

### 6.2.1 DDL 无任何 if_not_exists / checkfirst 保护

对全量迁移脚本的代码搜索确认：**整个 `mealie/alembic/versions/` 目录中没有任何一个 DDL 调用使用了 `if_not_exists=True` 或 `checkfirst=True` 参数。** 典型无保护调用示例：

- 建表：`mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py:202`
  ```python
  op.create_table(
      "households",
      sa.Column("id", mealie.db.migration_types.GUID(), nullable=False),
      ...  # 无 if_not_exists 参数
  )
  ```
- 加列：`mealie/alembic/versions/2023-02-14-20.45.41_5ab195a474eb_add_normalized_search_properties.py:93`
  ```python
  op.add_column("recipes", sa.Column("name_normalized", sa.String(), nullable=False, server_default=""))
  # 无 checkfirst 参数
  ```
- 建索引：`mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py:218`
  ```python
  op.create_index(op.f("ix_households_created_at"), "households", ["created_at"], unique=False)
  # 无 if_not_exists 参数
  ```
- 加外键：`mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py:250`
  ```python
  batch_op.create_foreign_key("fk_cookbooks_household_id", "households", ["household_id"], ["id"])
  # 无 checkfirst 参数
  ```

`mealie/alembic/env.py` 中也未配置任何全局默认保护参数。

### 6.2.2 边界失效时的 DDL 报错情况

**结论：`alembic_version` 被破坏后迁移被重复执行时，几乎所有 DDL 都会抛出数据库层错误，导致迁移中断。** 典型报错：

| DDL 操作 | 重复执行时 PostgreSQL 报错 | 重复执行时 SQLite 报错 |
|---------|------------------------|---------------------|
| `op.create_table("households")` | `relation "households" already exists` | `table households already exists` |
| `op.add_column("recipes", "name_normalized")` | `column "name_normalized" of relation "recipes" already exists` | `duplicate column name: name_normalized` |
| `op.create_index("ix_households_created_at")` | `relation "ix_households_created_at" already exists` | `index ix_households_created_at already exists` |
| `batch_op.create_foreign_key("fk_...")` | `constraint "fk_..." already exists` | 部分场景下会静默重复执行 |
| `op.drop_index("ix_recipes_name")` | `index "ix_recipes_name" does not exist` | `no such index: ix_recipes_name` |

这些错误均由数据库引擎抛出，**不在 Alembic 或迁移脚本层被捕获**，会直接触发外层 `command.upgrade()` 异常，最终导致**应用启动中断（致命）**。

### 6.2.3 边界失效时的完整后果总览

- **DDL 操作（建表/加列/建索引/加外键等）**：**数据库层报错，迁移中断，应用无法启动**。唯一例外是部分 `drop_*` 操作在 SQLite 上行为不一致。
- **纯 `UPDATE` 操作（规范化字段重算）**：结果正确但产生重复计算开销（幂等，因为按主键覆盖写入相同值）。
- **纯 `INSERT` 操作（household 创建、last_made 关联表插入等）**：**会产生重复数据或抛错**。例如 `feecc8ffb956` 的 `create_household()` 重复执行会给同一个 group 创建多个同名 household；`b9e516e2d3b3` 重复执行会插入重复的 `HouseholdToRecipe` 关联行（若无唯一约束则数据冗余，若有唯一约束则抛 IntegrityError 中断迁移）。
- **评分与收藏迁移（`d7c6efd2de42`）**：由于使用了 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`，即使重复执行也不会产生重复数据，是全库唯一具备数据库级幂等保护的迁移脚本。

### 6.3 Session 绑定与事务加入模式

#### 6.3.1 SQLAlchemy 2.0 `join_transaction_mode` 默认为 `conditional_savepoint`

项目使用 SQLAlchemy `2.0.50`（定义于 `pyproject.toml:12`）。所有迁移脚本中的 Session 通过以下方式创建：

```python
# 全代码库 21 处相同模式
session = orm.Session(bind=op.get_bind())
# 或
session = orm.Session(bind=bind)
```

两处都未显式传入 `join_transaction_mode` 参数，因此使用 SQLAlchemy 2.0 的默认值 **`conditional_savepoint`**。

该模式的精确定义（SQLAlchemy 2.0 官方规范）：

| 绑定 Connection 的状态 | `conditional_savepoint` 行为 |
|----------------------|---------------------------|
| **场景 A：已存在普通事务（BEGIN，但无 SAVEPOINT）** | Session 内部事务边界映射为真实的数据库 SAVEPOINT：`Session.begin()`（autobegin）→ `SAVEPOINT`；`Session.commit()` → `RELEASE SAVEPOINT`；`Session.rollback()` → `ROLLBACK TO SAVEPOINT`。外层 Connection 的普通事务保持开启状态。 |
| **场景 B：已存在 SAVEPOINT（已在嵌套事务中）** | Session 继续使用现有 SAVEPOINT 之上的嵌套 SAVEPOINT，同样 commit/rollback 映射为 RELEASE/ROLLBACK。 |
| **场景 C：Connection 未开启事务** | Session 使用正常的 `BEGIN` / `COMMIT` / `ROLLBACK`，直接控制 Connection 事务。 |

#### 6.3.2 Alembic 迁移上下文中的实际执行路径

Mealie 的迁移始终运行在 **场景 A** 之下，完整调用链如下：

```
mealie/alembic/env.py
  └─ connectable.connect()                  # 创建原始 Connection（无事务）
     └─ context.configure(connection=...)   # Alembic 绑定该 Connection
        └─ context.begin_transaction()      # 对 Connection 执行 BEGIN → 进入普通事务（场景 A 条件）
           └─ context.run_migrations()
              └─ migration.upgrade():
                 └─ orm.Session(bind=op.get_bind())
                    # bind = 同一个 Connection（已在普通事务中）
                    # join_transaction_mode=conditional_savepoint → 启用 SAVEPOINT 映射
```

在此场景下，迁移脚本中的调用实际对应的数据库 SQL：

| Python 代码 | 实际发出的 SQL（PostgreSQL / SQLite 均相同） |
|------------|----------------------------------------|
| 首次 `session.execute(...)` / `session.add(...)`（触发 autobegin） | `SAVEPOINT sa_savepoint_1` |
| `session.commit()` | `RELEASE SAVEPOINT sa_savepoint_1` |
| `session.rollback()` | `ROLLBACK TO SAVEPOINT sa_savepoint_1` |
| 下一次 session 操作（autobegin 重新触发） | `SAVEPOINT sa_savepoint_2` |

**关键结论——不是"PostgreSQL 下相当于"，而是所有支持 SAVEPOINT 的数据库（PostgreSQL、SQLite、MySQL）下都真实发出 SAVEPOINT 语句。** 该行为由 SQLAlchemy 2.0 的 `conditional_savepoint` 默认模式决定，与数据库方言无关（仅排除完全不支持 SAVEPOINT 的边缘数据库）。

#### 6.3.3 `session.commit()` 与 Alembic 外层事务的边界关系

- **Alembic 外层事务**：由 `context.begin_transaction()` 开启，包裹整个 revision 的 DDL 和数据迁移代码。revision 成功完成所有 migration 函数后执行 COMMIT；任何未被捕获的异常触发 ROLLBACK，回滚该 revision 内的全部变更。
- **SAVEPOINT 的可见性范围**：`session.commit()` 发出的 `RELEASE SAVEPOINT` 只是将内层保存点合并到外层事务中，**并未将数据真正持久化到磁盘**。此时如果 Alembic 外层事务执行 ROLLBACK（因为后续步骤抛异常），所有已 RELEASE 的 SAVEPOINT 变更同样被一并回滚。
- **异常捕获后的分支**：
  - **捕获后 `raise`（如 `b9e516e2d3b3` 的 `migrate_to_new_models()`）**：`session.rollback()` → `ROLLBACK TO SAVEPOINT` 回滚该子任务；随后 `raise` 将异常传到 Alembic 外层 → 外层事务整体 ROLLBACK → 整个 revision 的所有变更全部撤销。
  - **捕获后不 `raise`（如 `7cf3054cbbcc` 的 `truncate_normalized_fields()`）**：`session.rollback()` → `ROLLBACK TO SAVEPOINT` 回滚当前模型的修改；异常被吞掉，脚本继续处理下一个模型。后续若整个 revision 无其他异常，外层事务最终 COMMIT → 所有成功子任务（未被 rollback 的模型）的变更被持久化，失败子任务的变更因已被 SAVEPOINT 回滚而不存在。

#### 6.3.4 fixes 层的独立 Session

`db/fixes/` 下的修复脚本（`fix_migration_data`、`fix_slug_food_names`、`fix_group_with_no_name`）使用独立的 `session_context()` 创建 Session，**不参与 Alembic 事务**。fixes 在 `command.upgrade()` 成功返回后才执行，此时 Alembic 外层事务已 COMMIT。如果 fixes 脚本抛出异常被 `safe_try` 捕获，已部分执行的数据库变更**不会被自动回滚**，需要人工介入处理。

---

## 七、失败恢复机制与各层边界

> **重要区分**：Alembic schema 迁移失败和连接失败是**致命的**（将导致应用启动中断）；只有 fixes 层的数据修复脚本失败是非致命的（仅记录日志，应用继续启动）。

### 7.1 数据库连接重试——边界：10 次后强制终止

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

- **覆盖范围**：仅数据库连接建立阶段，尚未触及任何迁移逻辑
- **行为**：最多重试 10 次，每次间隔 1 秒
- **失败后果**：抛出 `ConnectionError`，沿调用栈向上传播到 FastAPI lifespan，**应用启动中断（致命）**，无数据被修改

### 7.2 Alembic 迁移事务层——边界：command.upgrade() 无外层捕获，失败即中断

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
- **事务粒度**：单个迁移脚本（revision）内的所有操作在一个数据库普通事务中执行（由 `context.begin_transaction()` 开启的 BEGIN）。迁移内 Session 的 `session.commit()` 仅发出 `RELEASE SAVEPOINT`，不会提交外层事务；revision 成功完成所有 migration 函数后，外层事务才执行真正的 COMMIT；任何未被捕获的异常触发外层事务 ROLLBACK，回滚该 revision 内的全部变更（含所有已 RELEASE 的 SAVEPOINT）。
- **失败后果**：`command.upgrade()` 抛出的任何异常都**没有外层 try/except 捕获**，直接传播到 lifespan，**应用启动中断（致命）**

这意味着：**任何一个 Alembic 迁移脚本失败，整个应用都无法启动。** 管理员必须修复数据库或迁移脚本后重新启动。数据库会停留在最后一个成功的 revision 版本。

### 7.3 迁移脚本内部的局部 try/except——边界：不同迁移策略不同

迁移脚本内部的数据迁移子任务有时会自带 try/except，但行为分为两类（结合 `join_transaction_mode=conditional_savepoint` 的 SAVEPOINT 机制）：

**类型 A：捕获后重新 raise（仍然致命）**

位置：`mealie/alembic/versions/2024-11-20-17.30.41_b9e516e2d3b3_add_household_to_recipe_last_made_.py:157-173` 的 `migrate_to_new_models()`：

```python
for migration_func in [...]:
    try:
        migration_func(session)
        session.commit()     # RELEASE SAVEPOINT sa_savepoint_N
    except Exception:
        session.rollback()   # ROLLBACK TO SAVEPOINT sa_savepoint_N
        logger.error(...)
        raise                # 显式向上抛出，终止整个迁移
```

- 行为：子任务抛异常时，`session.rollback()` 发出 `ROLLBACK TO SAVEPOINT` 撤销该子任务在当前保存点内的所有修改；然后 `raise` 重新抛出。
- 失败后果：异常被外层 `context.run_migrations()` 捕获，触发 Alembic 外层普通事务整体 ROLLBACK，整个 revision 的所有变更（含前面已成功 commit/RELEASE SAVEPOINT 的子任务）全部撤销，最终导致**应用启动中断（致命）**。

**类型 B：捕获后不 raise（局部失败，不影响同脚本其他子任务）**

位置：`mealie/alembic/versions/2025-02-09-15.31.00_7cf3054cbbcc_remove_instructions_index.py:103-128` 的 `truncate_normalized_fields()`：

```python
for model in models:
    ...
    try:
        session.commit()     # RELEASE SAVEPOINT sa_savepoint_N
    except Exception:
        logger.exception(f"Failed to truncate normalized fields for {model.__name__}")
        session.rollback()   # ROLLBACK TO SAVEPOINT sa_savepoint_N，不 raise，继续处理下一个模型
```

- 行为：单个模型失败时 `session.rollback()` 发出 `ROLLBACK TO SAVEPOINT`，仅撤销当前模型的修改；异常被吞掉，autobegin 为下一个模型开启新的 SAVEPOINT（`sa_savepoint_{N+1}`），循环继续。
- 失败后果：该迁移脚本本身无异常抛出，Alembic 外层事务最终 COMMIT，revision 标记为已执行（alembic_version 前进）。但失败模型的数据未被正确规范化，属于**静默数据不完整**风险；应用可正常启动。

### 7.4 Fixes 层的 safe_try——边界：仅记录日志，绝不阻塞启动

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
- **行为**：捕获所有 Exception，写完整堆栈日志，不重新抛出。fixes 使用独立 Session，不参与 Alembic 事务。
- **失败后果**：**非致命，不阻塞应用启动**。但失败意味着某些数据质量问题未被修复，后续使用中可能因脏数据引发业务逻辑错误。已部分执行的变更不会被自动回滚。

### 7.5 IntegrityError 重试——边界：fix_group_with_no_name 内的局部重试

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

### 7.6 各层致命性总览

| 层级 | 机制 | 失败是否中断启动 | 数据一致性影响 |
|------|------|:---:|---|
| 数据库连接 | 10 次重试 → `ConnectionError` | **是** | 未建立连接，无数据操作 |
| Alembic 迁移事务 | `command.upgrade()` 无外层捕获，外层普通事务 ROLLBACK | **是** | 当前 revision 全部变更（含已 RELEASE SAVEPOINT 的子任务）被外层事务回滚；已成功的 revision 不会回滚，数据库停留在中间版本 |
| 迁移脚本内（类型 A） | try/except + `raise` 重抛 | **是** | 当前子任务 `ROLLBACK TO SAVEPOINT`，异常传到外层触发外层事务整体 ROLLBACK，整个 revision 全部撤销 |
| 迁移脚本内（类型 B） | try/except + 不 raise | 否 | 失败子任务 `ROLLBACK TO SAVEPOINT` 单独撤销，其他子任务继续；外层事务最终 COMMIT，失败子任务的数据不完整（如某模型规范化未执行） |
| Fixes 层 | `safe_try` 吞掉所有异常 | 否 | 数据质量问题遗留（悬垂引用、缺失规范化、冲突 slug 等）；已部分执行的变更不会自动回滚（fixes 使用独立 Session，且在 Alembic 事务 COMMIT 后执行） |
| IntegrityError 重试 | 3 次后 `raise`，但外层有 `safe_try` | 否 | 个别空名称 group 未修复 |

---

## 八、索引变更策略

索引是影响查询性能和迁移时间的关键因素。Mealie 历史上发生过 3 类索引变更：

### 8.1 大规模补建索引

`mealie/alembic/versions/2023-02-07-20.57.21_ff5f73b01a7a_add_missing_foreign_key_and_order_.py` 一次性补建了 **90+ 个索引**，涵盖：
- 所有外键列（`*_id`）
- 所有 `created_at` 时间戳
- 排序字段（`position`）
- 频繁筛选列（`slug`、`name`、`token`、`rating` 等）

### 8.2 唯一约束 ↔ 普通索引的转换

| 迁移 | 操作 |
|------|------|
| `mealie/alembic/versions/2023-08-15-16.25.07_bcfdad6b7355_remove_tool_name_and_slug_unique_.py` | tools.name 和 tools.slug 从唯一索引降级为普通索引（允许跨 group 重名） |
| `mealie/alembic/versions/2023-10-04-14.29.26_dded3119c1fe_added_unique_constraints.py` | 11 张 M2M 表 + ingredient_foods/units/labels 全部加上组合唯一约束（先去重再加约束） |
| `mealie/alembic/versions/2024-07-12-16.16.29_feecc8ffb956_add_households.py` | cookbooks 增加 `(slug, group_id)` 唯一约束，迁移前先执行 `dedupe_cookbook_slugs()` 去重 |

### 8.3 搜索索引演进

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

## 九、外键清理与悬垂引用修复

`mealie/db/fixes/fix_migration_data.py` 中的 `fix_dangling_refs()` 是外键清理的核心。

### 9.1 两类引用的不同处理策略

```python
REASSIGN_REF_TABLES = ["group_meal_plans", "recipes", "shopping_lists"]
DELETE_REF_TABLES = ["long_live_tokens", "password_reset_tokens", "recipe_comments", "recipe_timeline_events"]
```

- **REASSIGN（重新分配）**：业务上重要的数据（如 recipe 本身），找到同 group 的 admin 用户（或第一个用户）作为兜底所有者，通过 UPDATE ... WHERE user_id NOT IN (...) 重新分配。
- **DELETE（直接删除）**：附属数据（token、评论、时间线事件），user_id 无效时直接 DELETE。

### 9.2 唯一约束前的外键重定向

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

### 9.3 购物清单标签设置修复

`mealie/db/fixes/fix_migration_data.py:103-133` 的 `fix_shopping_list_label_settings()`：
- 删除已不存在的 label 对应的 label_setting
- 为新增的 label 自动补全 label_setting（保持 position 顺序）

---

## 十、整体架构关系图

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

## 十一、关键设计总结

1. **迁移 ≠ 修复**：Alembic 负责 schema 演进，`db/fixes/` 负责数据质量修复。两者分离，修复可独立重跑。
2. **先建结构再填数据最后卸磨杀驴**：新 NOT NULL 列先给 `server_default`，数据填充后移除 server_default。
3. **DDL 完全无保护**：所有 `op.create_table` / `op.add_column` / `op.create_index` / `batch_op.create_foreign_key` 调用均**未使用 `if_not_exists` 或 `checkfirst` 参数**，`alembic/env.py` 也未配置全局默认保护。一旦 `alembic_version` 被破坏导致迁移重复执行，DDL 会直接抛出数据库层错误（如 "relation already exists" / "duplicate column name"），**迁移中断，应用无法启动**。
4. **幂等性并非普遍原则**：**只有评分与收藏迁移（`d7c6efd2de42`）** 这一个脚本使用了 `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE`；其余所有数据迁移均依赖 Alembic revision 的单次执行特性（`alembic_version` 表追踪），使用普通 `UPDATE` / `INSERT` / `session.commit()`。若 revision 单次执行前提被破坏（手动删 `alembic_version` 记录、downgrade 再 upgrade），纯 INSERT 类迁移会产生重复数据或抛 IntegrityError。
5. **中间模型自包含**：迁移脚本内部定义临时 ORM 模型，不依赖外部模型文件，避免未来模型变更破坏历史迁移。
6. **跨方言兼容**：GUID、数量类型、唯一约束都按 dialect 分支处理；SQLite 用 batch_alter_table 模拟 DDL。
7. **规范化字段是搜索的真相来源**：算法三次演进，每次全量重算，fix 层兜底补填。
8. **去重是加唯一约束的前置动作**：M2M 表靠数据库内部行号，实体表靠外键重定向+删除。
9. **事务边界与 SAVEPOINT 机制精确可控**：迁移内 Session 以 `join_transaction_mode=conditional_savepoint`（SQLAlchemy 2.0 默认）绑定 Alembic 的已开启事务 Connection，`session.commit()` 真实发出 `RELEASE SAVEPOINT`，`session.rollback()` 真实发出 `ROLLBACK TO SAVEPOINT`；RELEASE 后的数据仍在外层事务中，Alembic 外层 ROLLBACK 会一并撤销；fixes 层使用独立 Session，在 Alembic 事务 COMMIT 后执行，失败被 `safe_try` 吞异常且已执行部分不会自动回滚。
10. **失败的致命性分层明确**：连接失败和 Alembic schema 迁移失败是**致命的**（应用无法启动，数据库可能停留在中间 revision 版本）；迁移脚本内部个别数据子任务失败（不 raise 类型）可能静默遗留数据不完整；fixes 层数据修复失败**非致命**但会遗留脏数据风险，需人工排查日志处理。
