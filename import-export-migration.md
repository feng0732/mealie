# Mealie Import/Export 数据迁移代码链路分析

## 1. 整体架构概览

Mealie 的数据迁移系统分为两大独立链路：

| 链路类型 | 场景 | 核心模块 |
|---------|------|---------|
| **全量备份/恢复 (Backup/Restore)** | 同版本实例迁移、灾难恢复 | `mealie.services.backups_v2` |
| **第三方数据迁移 (Migration)** | 从其他食谱应用导入数据 | `mealie.services.migrations` |
| **食谱批量导出** | 按食谱列表导出 JSON+资源 | `mealie.services.exporter` + `mealie.services.recipe.recipe_bulk_service` |

---

## 2. 导出包结构

### 2.1 全量备份包 (BackupV2)

**生成入口**：[backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_v2.py#L44-L76) → `BackupV2.backup()`

**ZIP 包内结构**：

```
mealie_{APP_VERSION}_{YYYY.MM.DD.HH.MM.SS}.zip
├── database.json          # 完整数据库转储（所有表的 JSON）
└── data/                  # DATA_DIR 下的所有文件资源
    ├── groups/            # 每个 group 的目录
    │   └── {group_id}/
    │       ├── recipes/   # 食谱图片、资产
    │       ├── exports/   # 历史导出文件（本包会被排除）
    │       └── ...
    ├── templates/         # 邮件模板等
    ├── .secret            # 加密密钥文件（仅 restore 时复制）
    └── ...
```

**排除规则**（见 [backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_v2.py#L19-L25)）：
- 目录：`backups`、`.temp`
- 文件：`mealie.db`、`mealie.log*`、所有 `.zip` 文件

**database.json 结构**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L166-L187) → `AlchemyExporter.dump()`

```json
{
  "alembic_version": [{"version_num": "abc123..."}],
  "groups": [...],
  "households": [...],
  "users": [...],
  "recipes": [...],
  "recipes_to_tags": [...],
  "recipes_to_categories": [...],
  "tags": [...],
  "categories": [...],
  "group_meal_plans": [...],
  "group_meal_plan_rules": [...],
  "cookbooks": [...],
  "shopping_lists": [...],
  "ingredient_foods": [...],
  "ingredient_units": [...],
  "...": "所有其他表"
}
```

导出名录顺序遵循 SQLAlchemy `MetaData.sorted_tables`（按外键依赖拓扑排序）。

### 2.2 食谱批量导出包

**生成入口**：[recipe_bulk_service.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/recipe/recipe_bulk_service.py#L24-L28) → `RecipeBulkActionsService.export_recipes()`

**ZIP 包内结构**：

```
{export_id}.zip
└── recipes/
    └── {recipe_slug}/
        ├── {recipe_slug}.json   # Recipe Pydantic 模型序列化
        ├── image.webp           # 主图（如存在）
        ├── mini.webp            # 缩略图
        ├── original.webp        # 原图
        └── assets/              # RecipeAsset 文件
```

实现见 [_abc_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/exporter/_abc_exporter.py#L48-L88) 和 [recipe_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/exporter/recipe_exporter.py)

---

## 3. 导入校验逻辑

### 3.1 全量恢复校验

**入口**：[backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_v2.py#L95-L133) → `BackupV2.restore()`

**校验链路**：

1. **BackupFile 解压**：[backup_file.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_file.py#L75-L89)
   - 处理 Safari `__MACOSX` 目录污染问题（[BackupContents._find_base](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_file.py#L16-L35)）

2. **结构校验**：[backup_file.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_file.py#L45-L55) → `BackupContents.validate()`
   - 检查根目录、`data/` 目录、`database.json` 文件是否存在

3. **外键完整性校验**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L120-L145) → `AlchemyExporter.clean_rows()`
   - 逐表逐行检查所有 ForeignKey 指向的目标记录是否存在
   - 无效行被删除并记录日志警告

4. **类型转换**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L98-L118) → `AlchemyExporter.convert_types()`
   - UUID 字符串 → GUID（适配不同数据库方言）
   - 日期时间字段解析：`created_at`, `update_at`, `date_updated`, `timestamp`, `expires_at`, `locked_at`, `last_made` → `datetime`
   - 日期字段：`date_added`, `date` → `date`
   - 时间字段：`scheduled_time` → `time`

5. **Alembic 版本对齐**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L189-L201)
   - 从备份读取 `alembic_version`，执行 `alembic upgrade` 到该版本
   - 再执行 `init_db.main()` 完成剩余迁移（[fix_migration_data](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/fixes/fix_migration_data.py#L202-L209)）

6. **外键禁用/恢复**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L24-L51) → `ForeignKeyDisabler`
   - SQLite：`PRAGMA foreign_keys = OFF/ON`
   - PostgreSQL：`SET session_replication_role = 'replica'/'origin'`

### 3.2 第三方迁移导入校验

**入口**：[controller_migrations.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/routes/groups/controller_migrations.py#L30-L74) → `GroupMigrationController.start_data_migration()`

**支持的迁移类型**（[group_migration.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/schema/group/group_migration.py#L6-L16)）：
```
nextcloud, chowdown, copymethat, paprika, mealie_alpha, tandoor,
plantoeat, myrecipebox, recipekeeper, cookn
```

**核心基类**：[_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py)

**校验流程**：

1. **字段别名重写**：[_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py#L230-L260) → `rewrite_alias()`
   - 通过 `MigrationAlias(key, alias, func)` 定义映射（如 `title` → `name`）
   - 可选 `func` 对值进行转换（如 `split_by_comma`）

2. **字段清理/标准化**：[_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py#L262-L273) → `clean_recipe_dictionary()`
   - 删除旧 `id`
   - 调用 `cleaner.clean()` 做数据标准化

3. **图片路径安全校验**：[migration_helpers.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/utils/migration_helpers.py#L104-L147) → `safe_local_path()` + `import_image()`
   - 防止 `../../` 路径穿越读取任意本地文件
   - 图片路径必须在解压根目录内

4. **标签/分类去重绑定**：[database_helpers.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/utils/database_helpers.py#L24-L65)
   - `get_or_set_tags()` / `get_or_set_category()`
   - 按 slug 查询，不存在则创建，确保同 group 内不重复

5. **逐条导入+异常隔离**：[_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py#L158-L228) → `import_recipes_to_database()`
   - 单条 recipe 异常时 `session.rollback()`，继续下一条
   - 所有结果写入 `ReportEntryCreate`，最终汇总为 migration report

---

## 4. Recipe、Meal Plan、Taxonomy 与 Household 绑定关系

### 4.1 核心层级模型

```
Group (组)
  ├── 1:N → Household (家庭)
  │        └── 1:N → User (用户)
  │                 └── 1:N → Recipe (食谱，通过 user 关联)
  │                 └── 1:N → GroupMealPlan (用餐计划)
  │                 └── 1:N → ShoppingList (购物清单)
  │
  ├── 1:N → Tag (标签)
  ├── 1:N → Category (分类)
  ├── 1:N → MultiPurposeLabel (多用途标签)
  ├── 1:N → IngredientFood (食材)
  ├── 1:N → IngredientUnit (单位)
  ├── 1:N → Tool (工具)
  ├── 1:N → CookBook (食谱书，同时可绑定 household)
  └── 1:N → GroupMealPlanRules (用餐计划规则，同时可绑定 household)
```

### 4.2 Recipe 绑定

**数据模型**：[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/recipe.py#L42-L183)

| 绑定关系 | 实现方式 | 说明 |
|---------|---------|------|
| Recipe → Group | 直接外键 `group_id` (NOT NULL) | **强绑定**，唯一约束 `(slug, group_id)` |
| Recipe → Household | **间接绑定**：`AssociationProxy` 通过 `user.household_id` | Household 不直接存于 recipes 表 |
| Recipe → User | 直接外键 `user_id` (可 NULL) | 标记创建者 |
| Recipe ↔ Tag | M:N 表 `recipes_to_tags` | Tag 本身归属于 Group |
| Recipe ↔ Category | M:N 表 `recipes_to_categories` | Category 归属于 Group |
| Recipe ↔ Household (made_by) | M:N 表 `households_to_recipes` | 记录哪个家庭做过该食谱 |
| Recipe ↔ User (rating/favorite) | M:N 表 `user_to_recipes` | 含 `rating`, `is_favorite` 字段 |

迁移导入时的绑定赋值（[_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py#L184-L205)）：
```python
recipe.user_id = self.user.id
recipe.group_id = self.group.id
# household 由 user.household_id 代理推断
```

### 4.3 Meal Plan 绑定

**数据模型**：[mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/household/mealplan.py#L55-L77)

| 绑定关系 | 实现方式 |
|---------|---------|
| GroupMealPlan → Group | 直接外键 `group_id` |
| GroupMealPlan → Household | `AssociationProxy` 通过 `user.household_id` |
| GroupMealPlan → User | 直接外键 `user_id` |
| GroupMealPlan → Recipe | 直接外键 `recipe_id` (可 NULL) |
| GroupMealPlanRules → Group | 直接外键 `group_id` (NOT NULL) |
| GroupMealPlanRules → Household | 直接外键 `household_id` (可 NULL) + M:N `plan_rules_to_households` |
| GroupMealPlanRules ↔ Tag | M:N 表 `plan_rules_to_tags` |
| GroupMealPlanRules ↔ Category | M:N 表 `plan_rules_to_categories` |

### 4.4 Taxonomy (分类法) 绑定

**Tag 模型**：[tag.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/tag.py#L44-L69)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(slug, group_id)`

**Category 模型**：[category.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/category.py#L52-L75)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(slug, group_id)`
- 额外 M:N `group_to_categories` 表关联到 Group

**MultiPurposeLabel 模型**：[labels.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/labels.py#L17-L35)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(name, group_id)`
- 关联 ShoppingListItem、IngredientFood

**关键观察**：所有 Taxonomy 实体都**只绑定到 Group**，不直接绑定到 Household。Household 维度的隔离通过 Recipe → User → Household 的间接链实现。

### 4.5 CookBook 绑定

**模型**：[cookbook.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/household/cookbook.py#L18-L52)

| 绑定关系 | 实现方式 |
|---------|---------|
| CookBook → Group | 直接外键 `group_id` (可 NULL) |
| CookBook → Household | 直接外键 `household_id` (可 NULL) |
| CookBook ↔ Tag | M:N `cookbooks_to_tags` |
| CookBook ↔ Category | M:N `cookbooks_to_categories` |
| CookBook ↔ Tool | M:N `cookbooks_to_tools` |

CookBook 是少数**同时持有 group_id 和 household_id 两个独立外键**的模型，可实现 Group 级共享食谱书或 Household 级私有食谱书。

---

## 5. 风险分析

### 5.1 跨版本字段缺失风险

**现状机制**：
- 备份中存储 `alembic_version`，恢复时先 `alembic upgrade` 到该版本，再通过 `init_db.main()` → `fix_migration_data()` 做数据修补（见 [alembic_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L189-L247) 和 [fix_migration_data.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/fixes/fix_migration_data.py)）

**已识别的修复函数**：
| 修复函数 | 修补内容 |
|---------|---------|
| `fix_dangling_refs` | 将无效 `user_id` 的记录重定向到默认用户，或删除无主 token/评论 |
| `fix_recipe_normalized_search_properties` | 补全 `name_normalized`, `description_normalized` 等搜索字段 |
| `fix_shopping_list_label_settings` | 为 ShoppingList 同步缺失的 MultiPurposeLabel 设置 |
| `fix_group_slugs` | 补全缺失的 Group slug，处理冲突 |
| `fix_normalized_unit_and_food_names` | 补全单位/食材的标准化搜索字段 |

**高风险点**：
1. **向前兼容缺失**：恢复流程假设备份是旧版本 → 当前新版本。若备份来自**更新版本**（alembic_version 领先于当前代码），`alembic upgrade` 会找不到迁移脚本而失败。
2. **新增 NOT NULL 字段无默认值**：若表结构新增了 NOT NULL 且无 default 的列，旧备份没有该列数据，`insert` 会失败。当前 `clean_rows` 只检查外键不检查列存在性。
3. **列重命名**：`convert_types` 依赖硬编码列名匹配（如 `date_added`, `last_made`），列改名后转换逻辑失效。
4. **第三方迁移器版本锁定**：每个迁移器（如 `MealieAlphaMigrator`）只针对特定源版本 schema，源格式升级后导入会静默丢字段（仅写入 exception 到 report）。

### 5.2 文件资源迁移风险

**全量备份**：
- [backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_v2.py#L78-L93) → `_copy_data()` 采用**全量覆盖**策略：
  ```python
  shutil.rmtree(self.directories.DATA_DIR / f.name)
  shutil.copytree(f, self.directories.DATA_DIR / f.name)
  ```
- 对 `.secret` 文件单独复制，会触发 `get_app_settings.cache_clear()` 重置 JWT 密钥缓存。

**食谱导入**：
- [migration_helpers.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/utils/migration_helpers.py#L123-L147) → `import_image()` 通过 `RecipeDataService.write_image()` 写入。

**风险点**：
1. **DATA_DIR 整体覆盖**：恢复时删除目标目录再复制，若 DATA_DIR 中有备份未包含的新文件会被**静默删除**。
2. **食谱图片与 database.json 不同步**：全量备份的 database.json 是时间点快照，但图片是遍历文件系统收集。若备份期间有 recipe 删除，可能残留 orphan 图片。
3. **绝对路径依赖**：Recipe 模型的 `image` 字段存储的是文件名（不含路径），但 `directory` 属性依赖 `group_id` 生成路径。若 group_id 在恢复后发生变化，图片路径会失效。
4. **PostgreSQL 序列重置不完整**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L217-L240) 的序列重置列表是**硬编码**的，新增加 ID 自增序列的表如果忘记加进列表，会导致后续 INSERT 主键冲突。

### 5.3 冲突合并风险

**全量备份恢复**：
- **完全替换策略**：[alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py#L249-L287) → `drop_all()` 先删所有表，再重建导入。**不存在合并逻辑**，当前实例数据会被完全清空。

**第三方迁移导入**：
- **Recipe 冲突**：[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/recipe.py#L44-L46) 的 `UniqueConstraint("slug", "group_id")` 导致同 slug 食谱导入冲突时直接抛异常，被 `session.rollback()` 捕获后跳过该条，不做任何合并。
- **Tag/Category 冲突**：[database_helpers.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/utils/database_helpers.py#L24-L49) 使用 `get_or_set_*`，按 slug 取已有记录**复用**而非合并。若同名但属性不同（如颜色），会丢失导入数据的属性。
- **Meal Plan 冲突**：没有唯一约束，相同日期+餐次重复导入会产生重复记录。
- **Group/Household 不存在合并**：迁移导入时使用当前 session 的 `group_id` / `household_id`，导入数据全部归属于触发迁移的用户所在 Group/Household，无法选择目标。

**风险总结**：
| 场景 | 策略 | 风险等级 | 说明 |
|-----|------|---------|------|
| 全量恢复 | 删除重建 | 🔴 高 | 目标数据完全丢失，无回滚 |
| Recipe 导入冲突 | 跳过 | 🟡 中 | 报告中仅显示失败，不提示冲突详情 |
| Tag/Category 冲突 | 复用已有 | 🟡 中 | 导入的属性被静默丢弃 |
| Meal Plan 冲突 | 允许重复 | 🟠 中高 | 产生脏数据 |
| 跨 Group 导入 | 归为当前 Group | 🔴 高 | 无法指定目标 Group/Household |

---

## 6. 关键代码索引

| 功能 | 文件 |
|-----|------|
| 全量备份/恢复入口 | [backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_v2.py) |
| 数据库导出/导入核心 | [alchemy_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/alchemy_exporter.py) |
| 备份包结构处理 | [backup_file.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/backups_v2/backup_file.py) |
| 第三方迁移基类 | [_migration_base.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/migrations/_migration_base.py) |
| 迁移控制器 | [controller_migrations.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/routes/groups/controller_migrations.py) |
| 食谱批量导出 | [recipe_bulk_service.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/recipe/recipe_bulk_service.py) |
| Recipe Exporter | [recipe_exporter.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/services/exporter/recipe_exporter.py) |
| 数据修复（迁移后） | [fix_migration_data.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/fixes/fix_migration_data.py) |
| Group 模型 | [group.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/group/group.py) |
| Household 模型 | [household.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/household/household.py) |
| Recipe 模型 | [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/recipe.py) |
| Meal Plan 模型 | [mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/household/mealplan.py) |
| Tag/Category 模型 | [tag.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/tag.py), [category.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/mealie/db/models/recipe/category.py) |
| 恢复测试用例 | [test_backup_v2.py](file:///d:/fz/0601/solo-dogfeeding/code/80-mealie/tests/unit_tests/services_tests/backup_v2_tests/test_backup_v2.py) |
