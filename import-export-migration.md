# Mealie Import/Export 数据迁移代码链路分析

## 1. 整体架构概览

Mealie 的数据迁移系统分为 **四大独立链路**：

| 链路类型 | 场景 | 核心模块 |
|---------|------|---------|
| **全量备份/恢复 (Backup/Restore)** | 同版本实例迁移、灾难恢复 | `mealie.services.backups_v2` |
| **第三方数据迁移 (Migration)** | 从其他食谱应用导入数据 | `mealie.services.migrations` |
| **食谱批量导出** | 按食谱列表导出 JSON+资源 | `mealie.services.exporter` + `mealie.services.recipe.recipe_bulk_service` |
| **Recipe ZIP 单条导入** | 从 Mealie 导出的 ZIP 重新导入单条食谱 | `mealie.services.recipe.recipe_service.RecipeService.create_from_zip` |

---

## 2. 导出包结构

### 2.1 全量备份包 (BackupV2)

**生成入口**：[mealie/services/backups_v2/backup_v2.py](mealie/services/backups_v2/backup_v2.py#L44-L76) → `BackupV2.backup()`

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

**排除规则**（见 [mealie/services/backups_v2/backup_v2.py](mealie/services/backups_v2/backup_v2.py#L19-L25)）：
- 目录：`backups`、`.temp`
- 文件：`mealie.db`、`mealie.log*`、所有 `.zip` 文件

**database.json 结构**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L166-L187) → `AlchemyExporter.dump()`

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

**生成入口**：[mealie/services/recipe/recipe_bulk_service.py](mealie/services/recipe/recipe_bulk_service.py#L24-L28) → `RecipeBulkActionsService.export_recipes()`

**ZIP 包内结构**：

```
{export_id}.zip
└── recipes/
    └── {recipe_slug}/
        ├── {recipe_slug}.json   # Recipe Pydantic 模型序列化（model_dump_json）
        ├── images/
        │   ├── original.webp    # 原图
        │   ├── image.webp       # 主图
        │   ├── mini.webp        # 缩略图
        │   └── timeline/        # 时间线图片
        └── assets/              # RecipeAsset 文件（PDF/文档等）
```

导出实现：
- 基类遍历逻辑：[mealie/services/exporter/_abc_exporter.py](mealie/services/exporter/_abc_exporter.py#L48-L88) → `ABCExporter.export()`
  - JSON 写入：`zip.writestr(f"recipes/{slug}/{slug}.json", item.model.model_dump_json())`
  - 资源目录复制：`_post_export_hook()` 递归拷贝 recipe 目录，排除 `.json`
- Recipe 级导出器：[mealie/services/exporter/recipe_exporter.py](mealie/services/exporter/recipe_exporter.py)

### 2.3 共享食谱单条 ZIP 导出

**生成入口**：[mealie/routes/recipe/shared_routes.py](mealie/routes/recipe/shared_routes.py#L42-L58) → `get_shared_recipe_as_zip()`

**ZIP 包内结构（扁平，无子目录）**：

```
{recipe_slug}.zip
├── {recipe_slug}.json   # Recipe Pydantic 模型序列化
└── original.webp        # 仅原图（如存在）
```

实现：
```python
with ZipFile(temp_path, "w") as myzip:
    myzip.writestr(f"{recipe.slug}.json", recipe.model_dump_json())
    if image_asset.is_file():
        myzip.write(image_asset, arcname=image_asset.name)
```

---

## 3. 导入校验逻辑

### 3.1 全量恢复校验

**入口**：[mealie/services/backups_v2/backup_v2.py](mealie/services/backups_v2/backup_v2.py#L95-L133) → `BackupV2.restore()`

**校验链路**：

1. **BackupFile 解压**：[mealie/services/backups_v2/backup_file.py](mealie/services/backups_v2/backup_file.py#L75-L89)
   - 处理 Safari `__MACOSX` 目录污染问题（[BackupContents._find_base](mealie/services/backups_v2/backup_file.py#L16-L35)）

2. **结构校验**：[mealie/services/backups_v2/backup_file.py](mealie/services/backups_v2/backup_file.py#L45-L55) → `BackupContents.validate()`
   - 检查根目录、`data/` 目录、`database.json` 文件是否存在

3. **外键完整性校验**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L120-L145) → `AlchemyExporter.clean_rows()`
   - 逐表逐行检查所有 ForeignKey 指向的目标记录是否存在
   - 无效行被删除并记录日志警告

4. **类型转换**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L98-L118) → `AlchemyExporter.convert_types()`
   - UUID 字符串 → GUID（适配不同数据库方言）
   - 日期时间字段解析：`created_at`, `update_at`, `date_updated`, `timestamp`, `expires_at`, `locked_at`, `last_made` → `datetime`
   - 日期字段：`date_added`, `date` → `date`
   - 时间字段：`scheduled_time` → `time`

5. **Alembic 版本对齐**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L189-L201)
   - 从备份读取 `alembic_version`，执行 `alembic upgrade` 到该版本
   - 再执行 `init_db.main()` 完成剩余迁移（[fix_migration_data](mealie/db/fixes/fix_migration_data.py#L202-L209)）

6. **外键禁用/恢复**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L24-L51) → `ForeignKeyDisabler`
   - SQLite：`PRAGMA foreign_keys = OFF/ON`
   - PostgreSQL：`SET session_replication_role = 'replica'/'origin'`

### 3.2 第三方迁移导入校验

**入口**：[mealie/routes/groups/controller_migrations.py](mealie/routes/groups/controller_migrations.py#L30-L74) → `GroupMigrationController.start_data_migration()`

**支持的迁移类型**（[mealie/schema/group/group_migration.py](mealie/schema/group/group_migration.py#L6-L16)）：
```
nextcloud, chowdown, copymethat, paprika, mealie_alpha, tandoor,
plantoeat, myrecipebox, recipekeeper, cookn
```

**核心基类**：[mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py)

**校验流程**：

1. **字段别名重写**：[mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py#L230-L260) → `rewrite_alias()`
   - 通过 `MigrationAlias(key, alias, func)` 定义映射（如 `title` → `name`）
   - 可选 `func` 对值进行转换（如 `split_by_comma`）

2. **字段清理/标准化**：[mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py#L262-L273) → `clean_recipe_dictionary()`
   - 删除旧 `id`
   - 调用 `cleaner.clean()` 做数据标准化

3. **图片路径安全校验**：[mealie/services/migrations/utils/migration_helpers.py](mealie/services/migrations/utils/migration_helpers.py#L104-L147) → `safe_local_path()` + `import_image()`
   - 防止 `../../` 路径穿越读取任意本地文件
   - 图片路径必须在解压根目录内

4. **标签/分类去重绑定**：[mealie/services/migrations/utils/database_helpers.py](mealie/services/migrations/utils/database_helpers.py#L24-L65)
   - `get_or_set_tags()` / `get_or_set_category()`
   - 按 slug 查询，不存在则创建，确保同 group 内不重复

5. **逐条导入+异常隔离**：[mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py#L158-L228) → `import_recipes_to_database()`
   - 单条 recipe 异常时 `session.rollback()`，继续下一条
   - 所有结果写入 `ReportEntryCreate`，最终汇总为 migration report

### 3.3 Recipe ZIP 单条导入链路

**HTTP 入口**：[mealie/routes/recipe/recipe_crud_routes.py](mealie/routes/recipe/recipe_crud_routes.py#L295-L307) → `POST /api/recipes/create/zip`

```python
@router.post("/create/zip", status_code=201)
def create_recipe_from_zip(self, archive: UploadFile = File(...)):
    with get_temporary_zip_path() as temp_path:
        recipe = self.service.create_from_zip(archive, temp_path)
        # publish event...
    return recipe.slug
```

**核心处理函数**：[mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L302-L333) → `RecipeService.create_from_zip()`

#### 3.3.1 完整处理链路

```
HTTP UploadFile
    │
    ▼
① 写入临时 ZIP 文件（temp_path）
    │  shutil.copyfileobj(archive.file, buffer)
    ▼
② ZipFile 遍历 namelist()，按后缀抓取文件
    │  ├─ *.json → json.loads() → recipe_dict
    │  └─ *.webp → bytes → recipe_image
    │
    │  ⚠ 仅取"最后一个匹配"：循环中覆盖同名变量
    ▼
③ 若无 JSON → 抛出 UnexpectedNone("No json data found in Zip")
    ▼
④ 数据清洗 + Group/Household 绑定注入
    │  clean_recipe_dict(recipe_dict)
    │    └─ _process_recipe_data() 递归遍历
    │         ├─ 强制覆写 group_id = 当前用户 group_id
    │         ├─ 强制覆写 household_id = 当前用户 household_id
    │         ├─ recipe_category / tags：
    │         │    _transform_category_or_tag()
    │         │      ├─ 按 slug 查询已有记录 → 复用（model_dump）
    │         │      └─ 不存在 → repo.create(data) 新建
    │         └─ user_id：若目标用户不存在 → 回退为当前用户
    ▼
⑤ 构建 Recipe 对象 → create_one() 落库
    │  Recipe(**cleaned_dict)
    │    └─ _recipe_creation_factory()
    │         ├─ user_id / household_id / group_id 再次覆写
    │         ├─ tags[i]["group_id"] 批量注入
    │         └─ 默认 ingredient / step 填充
    │  repos.recipes.create(data)
    ▼
⑥ 图片写入
    │  RecipeDataService(recipe.id)
    │    └─ write_image(recipe_image_bytes, "webp")
    │         ├─ images/original.webp 写入
    │         ├─ PillowMinifier.minify() 生成 image.webp + mini.webp
    │         └─ 异常时删除 partial 文件并 re-raise
    ▼
返回 Recipe slug
```

#### 3.3.2 关键代码细节

**ZIP 内文件识别逻辑**（仅后缀匹配，无目录结构约束）：
```python
with ZipFile(temp_path) as myzip:
    for file in myzip.namelist():
        if file.endswith(".json"):
            with myzip.open(file) as myfile:
                recipe_dict = json.loads(myfile.read())
        elif file.endswith(".webp"):
            with myzip.open(file) as myfile:
                recipe_image = myfile.read()
```
- 不关心目录层级：`recipes/foo/foo.json`、`foo.json`、`nested/deep/foo.json` 均可
- 多个 `.json` / `.webp` 时，**最后一次循环赋值生效**（非确定性，取决于 ZIP 内 namelist 顺序）
- 仅支持 `.webp` 格式图片，`.jpg`、`.png` 等会被**静默忽略**

**Group/Household 强制重绑定**：[mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L283-L297)
```python
def _process_recipe_data(self, key, data):
    if isinstance(data, dict):
        data["group_id"] = str(self.user.group_id)
        data["household_id"] = str(self.user.household_id)
```
递归应用到所有嵌套 dict，意味着：
- Recipe JSON 中自带的 `group_id`、`household_id`、`user_id` 全部被**静默丢弃**
- 嵌套的 Tag / Category 对象的 `group_id` 也被强制替换

**Tag/Category Get-or-Create**：[mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L255-L267)
```python
def _transform_category_or_tag(self, data, repo):
    slug = data.get("slug")
    query = repo.get_one(slug, "slug")
    if query:
        return query.model_dump()   # ← 复用已有记录，导入属性全丢
    return repo.create(data).model_dump()
```

**Recipe 创建工厂**：[mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L163-L187)
```python
def _recipe_creation_factory(self, name, additional_attrs=None):
    additional_attrs["user_id"] = self.user.id
    additional_attrs["household_id"] = self.household.id
    additional_attrs["group_id"] = self.household.group_id
    for i in range(len(additional_attrs.get("tags", []))):
        additional_attrs["tags"][i]["group_id"] = self.user.group_id
```

**图片写入**：[mealie/services/recipe/recipe_data_service.py](mealie/services/recipe/recipe_data_service.py#L85-L109)
```python
def write_image(self, file_data, extension, image_dir=None):
    image_path = image_dir.joinpath(f"original.{extension}")
    image_path.unlink(missing_ok=True)   # ← 若存在先删除（覆盖语义）
    # 写 original.webp
    self.minifier.minify(image_path)     # ← 生成 image.webp + mini.webp
```

#### 3.3.3 导入 ZIP 与导出 ZIP 结构对应关系

| 导出类型 | ZIP 结构 | create_from_zip 能否读取 | 备注 |
|---------|---------|------------------------|------|
| **共享食谱单条导出** (`shared/{token}/zip`) | 扁平：`{slug}.json` + `original.webp` | ✅ 完全兼容 | 最匹配的导入来源 |
| **批量导出** (`bulk-actions/export`) | 嵌套：`recipes/{slug}/{slug}.json` + `images/original.webp` + `assets/` | ⚠ JSON 能读，图片 **读不到** | 图片在 `images/` 子目录但命名匹配（都是 `.webp`），但 **assets/ 下所有资源完全丢失** |
| **MealieAlpha 迁移包** (`mealie_alpha.py`) | `recipes/{slug}/{slug}.json` + 同级图片目录 | ⚠ 取决于具体子目录 | 同上 |
| **手工构造 ZIP** | 任意目录 + `.json`/`.webp` | ✅ 只要后缀匹配即可 | 但多个 JSON/图片时结果不确定 |

**关键不匹配点**：
- 批量导出的图片路径是 `recipes/{slug}/images/original.webp`，create_from_zip 不检查 `images/` 前缀，但 `.endswith(".webp")` 仍能命中——前提是 ZIP 内没有其他 webp 文件
- 批量导出的 `assets/` 目录（PDF、TXT 等 RecipeAsset 文件）在导入链路中**完全没有处理**，不会重建 RecipeAsset 记录，也不会写入磁盘
- 批量导出的 `timeline/` 图片、`mini.webp`、`image.webp` 在导入时会被 minifier 重新生成，不影响结果

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

**数据模型**：[mealie/db/models/recipe/recipe.py](mealie/db/models/recipe/recipe.py#L42-L183)

| 绑定关系 | 实现方式 | 说明 |
|---------|---------|------|
| Recipe → Group | 直接外键 `group_id` (NOT NULL) | **强绑定**，唯一约束 `(slug, group_id)` |
| Recipe → Household | **间接绑定**：`AssociationProxy` 通过 `user.household_id` | Household 不直接存于 recipes 表 |
| Recipe → User | 直接外键 `user_id` (可 NULL) | 标记创建者 |
| Recipe ↔ Tag | M:N 表 `recipes_to_tags` | Tag 本身归属于 Group |
| Recipe ↔ Category | M:N 表 `recipes_to_categories` | Category 归属于 Group |
| Recipe ↔ Household (made_by) | M:N 表 `households_to_recipes` | 记录哪个家庭做过该食谱 |
| Recipe ↔ User (rating/favorite) | M:N 表 `user_to_recipes` | 含 `rating`, `is_favorite` 字段 |

迁移导入时的绑定赋值（[mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py#L184-L205)）：
```python
recipe.user_id = self.user.id
recipe.group_id = self.group.id
# household 由 user.household_id 代理推断
```

### 4.3 Meal Plan 绑定

**数据模型**：[mealie/db/models/household/mealplan.py](mealie/db/models/household/mealplan.py#L55-L77)

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

**Tag 模型**：[mealie/db/models/recipe/tag.py](mealie/db/models/recipe/tag.py#L44-L69)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(slug, group_id)`

**Category 模型**：[mealie/db/models/recipe/category.py](mealie/db/models/recipe/category.py#L52-L75)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(slug, group_id)`
- 额外 M:N `group_to_categories` 表关联到 Group

**MultiPurposeLabel 模型**：[mealie/db/models/recipe/labels.py](mealie/db/models/recipe/labels.py#L17-L35)
- 直接外键 `group_id` (NOT NULL)
- 唯一约束 `(name, group_id)`
- 关联 ShoppingListItem、IngredientFood

**关键观察**：所有 Taxonomy 实体都**只绑定到 Group**，不直接绑定到 Household。Household 维度的隔离通过 Recipe → User → Household 的间接链实现。

### 4.5 CookBook 绑定

**模型**：[mealie/db/models/household/cookbook.py](mealie/db/models/household/cookbook.py#L18-L52)

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

#### 5.1.1 全量备份恢复

**现状机制**：
- 备份中存储 `alembic_version`，恢复时先 `alembic upgrade` 到该版本，再通过 `init_db.main()` → `fix_migration_data()` 做数据修补（见 [mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L189-L247) 和 [mealie/db/fixes/fix_migration_data.py](mealie/db/fixes/fix_migration_data.py)）

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

#### 5.1.2 Recipe ZIP 导入

| 风险点 | 触发条件 | 后果 |
|-------|---------|------|
| **Recipe schema 新增必填字段** | 新版本 Recipe Pydantic 模型新增无默认值的字段，旧版导出的 JSON 缺失该字段 | `Recipe(**data)` 抛 `ValidationError`，整条导入失败，HTTP 500 |
| **字段类型变更** | 字段从 `str` 改为 `int` / `list` 等 | Pydantic 校验失败，同上 |
| **嵌套子对象 schema 演进** | RecipeIngredient / RecipeStep / Nutrition 等新增必填字段 | 同上 |
| **Tag/Category schema 变更** | Tag 模型新增必填列（如 color 字段），旧 JSON 没有 | `_transform_category_or_tag` 在 `repo.create(data)` 时抛 IntegrityError |
| **废弃字段被删除** | 导出的 JSON 含已删除字段名 | Pydantic 默认 `extra="ignore"`，通常可容忍；但若 `extra="forbid"` 则失败 |

**关键缺失**：create_from_zip 链路**没有**调用 `cleaner.clean()` 做 schema 兼容层处理，完全依赖 Pydantic 的 `model_validate`，也**没有**版本号/迁移策略机制。

### 5.2 文件资源迁移风险

#### 5.2.1 全量备份恢复

- [mealie/services/backups_v2/backup_v2.py](mealie/services/backups_v2/backup_v2.py#L78-L93) → `_copy_data()` 采用**全量覆盖**策略：
  ```python
  shutil.rmtree(self.directories.DATA_DIR / f.name)
  shutil.copytree(f, self.directories.DATA_DIR / f.name)
  ```
- 对 `.secret` 文件单独复制，会触发 `get_app_settings.cache_clear()` 重置 JWT 密钥缓存。

**风险点**：
1. **DATA_DIR 整体覆盖**：恢复时删除目标目录再复制，若 DATA_DIR 中有备份未包含的新文件会被**静默删除**。
2. **食谱图片与 database.json 不同步**：全量备份的 database.json 是时间点快照，但图片是遍历文件系统收集。若备份期间有 recipe 删除，可能残留 orphan 图片。
3. **绝对路径依赖**：Recipe 模型的 `image` 字段存储的是文件名（不含路径），但 `directory` 属性依赖 `group_id` 生成路径。若 group_id 在恢复后发生变化，图片路径会失效。
4. **PostgreSQL 序列重置不完整**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L217-L240) 的序列重置列表是**硬编码**的，新增加 ID 自增序列的表如果忘记加进列表，会导致后续 INSERT 主键冲突。

#### 5.2.2 Recipe ZIP 导入

| 资源类型 | 处理方式 | 风险 |
|---------|---------|------|
| **original.webp** | `write_image(bytes, "webp")` → 写入 + minify | ✅ 覆盖写入，minify 失败自动清理 partial 文件 |
| **其他格式图片（jpg/png/gif...）** | `elif file.endswith(".webp")` 不匹配 → **静默丢弃** | 🔴 导出为非 webp 格式（如第三方工具）的图片全部丢失，无报错 |
| **assets/ 目录（PDF/TXT/MD/CSV 等）** | 完全不处理 | 🔴 RecipeAsset 记录不创建，文件不写入磁盘，静默丢失 |
| **images/mini.webp, image.webp** | 不读取，由 minify 重新生成 | 🟡 无功能损失，但浪费导出时的压缩算力 |
| **timeline/ 时间线图片** | 完全不处理 | 🟠 RecipeTimelineEvent 记录里可能引用不存在的图片路径 |
| **多个 webp 文件** | 循环最后赋值生效，前面的被丢弃 | 🟠 取决于 namelist 顺序，可能出现非原图被误当 original |
| **JSON 引用的 image 字段缓存 key** | JSON 中 `image` 字段（cache key）落库，但 minify 后产生新 cache key，两者不一致 | 🟡 `create_one` 后 recipe.image 仍是旧值，前端首次加载可能 404，刷新后正常 |

**路径穿越防护**：create_from_zip 直接使用 `myzip.open(file)` 读取 bytes，不写入磁盘（无路径拼接），因此不存在 `../../` 目录穿越风险，这一点优于第三方迁移链路。

### 5.3 冲突合并风险

#### 5.3.1 全链路总览

**全量备份恢复**：
- **完全替换策略**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L249-L287) → `drop_all()` 先删所有表，再重建导入。**不存在合并逻辑**，当前实例数据会被完全清空。

**第三方迁移导入**：
- **Recipe 冲突**：Recipe 模型的 `UniqueConstraint("slug", "group_id")` 导致同 slug 食谱导入冲突时直接抛异常，被 `session.rollback()` 捕获后跳过该条，不做任何合并。
- **Tag/Category 冲突**：使用 `get_or_set_*`，按 slug 取已有记录**复用**而非合并。若同名但属性不同（如颜色），会丢失导入数据的属性。
- **Meal Plan 冲突**：没有唯一约束，相同日期+餐次重复导入会产生重复记录。
- **Group/Household 不存在合并**：迁移导入时使用当前 session 的 `group_id` / `household_id`，导入数据全部归属于触发迁移的用户所在 Group/Household，无法选择目标。

#### 5.3.2 Recipe ZIP 导入专项风险

| 冲突场景 | 处理策略 | 风险等级 | 详细说明 |
|---------|---------|---------|---------|
| **Slug 冲突（同 Group 已存在同名食谱）** | DB IntegrityError → HTTP 400 "Recipe already exists" | 🟡 中 | 前端能感知到失败，但没有"覆盖/跳过/改名"选项，用户只能手动改名后重导 |
| **Tag slug 冲突（同 Group 已有同名 Tag）** | `get_one(slug)` 命中 → 返回已有记录的 `model_dump()` | 🟠 中高 | 导入 JSON 中 tag 的自定义属性（如 id、创建时间、color）被静默丢弃，直接复用目标实例已有 tag |
| **Category slug 冲突** | 同上 | 🟠 中高 | 同上 |
| **User ID 冲突（JSON 中 user_id 指向不存在/跨 Group 用户）** | `_transform_user_id` 回退到当前用户 | 🟡 中 | 原作者信息丢失，全部归为导入者 |
| **图片文件名冲突** | `write_image` 内部 `image_path.unlink(missing_ok=True)` 先删再写 | 🟢 低 | 新 recipe 有独立 id 目录，不会冲突；同一 recipe 重试会覆盖自己，正常 |
| **ID 冲突** | JSON 中 `id` 被 `_recipe_creation_factory` 忽略，由 ORM 重新生成 | 🟢 低 | UUID 冲突概率可忽略 |
| **Group 交叉（从 A Group 导出导入到 B Group）** | `_process_recipe_data` 强制重写 `group_id` / `household_id` | 🔴 高 | **所有嵌套对象的 group_id 被 B Group 覆盖**；但 Tag/Category 按 slug 复用，如果 B Group 没有该 slug 会新建归属于 B，数据归属整体正确 |

**Recipe ZIP 导入没有任何合并/更新能力**——它是纯 Create 语义，若 slug 已存在直接拒绝，不能原地覆盖更新。

---

**风险总表（所有链路对比）**：

| 场景 | 全量恢复 | 第三方迁移 | Recipe ZIP 导入 |
|-----|---------|----------|----------------|
| 目标数据是否被清空 | 🔴 是（drop_all） | 🟢 否 | 🟢 否 |
| Slug 冲突处理 | — | 🟡 跳过 | 🟡 拒绝（400） |
| Tag/Category 冲突处理 | — | 🟡 复用（丢属性） | 🟠 复用（丢属性） |
| Group/Household 归属 | 备份原样保留 | 🔴 归为当前 | 🔴 归为当前 |
| 字段缺失校验 | 🟡 Alembic 自动迁移 | 🟡 cleaner 清洗 | 🔴 纯 Pydantic 校验，无兼容层 |
| RecipeAsset 处理 | ✅ 全量恢复 | ⚠ 视迁移器 | 🔴 完全丢失 |
| 非 webp 图片 | ✅ 原样恢复 | ⚠ 视迁移器 | 🔴 静默丢弃 |
| 路径穿越风险 | 🟢 无 | 🟡 safe_local_path 防护 | 🟢 无（直接读 bytes） |

---

## 6. 关键代码索引

| 功能 | 文件 |
|-----|------|
| 全量备份/恢复入口 | [mealie/services/backups_v2/backup_v2.py](mealie/services/backups_v2/backup_v2.py) |
| 数据库导出/导入核心 | [mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py) |
| 备份包结构处理 | [mealie/services/backups_v2/backup_file.py](mealie/services/backups_v2/backup_file.py) |
| 第三方迁移基类 | [mealie/services/migrations/_migration_base.py](mealie/services/migrations/_migration_base.py) |
| 迁移控制器 | [mealie/routes/groups/controller_migrations.py](mealie/routes/groups/controller_migrations.py) |
| Recipe ZIP 导入接口 | [mealie/routes/recipe/recipe_crud_routes.py](mealie/routes/recipe/recipe_crud_routes.py)（`POST /create/zip`） |
| Recipe ZIP 导入核心 | [mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py)（`create_from_zip`） |
| Recipe 数据清洗（scraper 兼容层） | [mealie/services/scraper/cleaner.py](mealie/services/scraper/cleaner.py) |
| 图片/资源读写服务 | [mealie/services/recipe/recipe_data_service.py](mealie/services/recipe/recipe_data_service.py) |
| 食谱批量导出服务 | [mealie/services/recipe/recipe_bulk_service.py](mealie/services/recipe/recipe_bulk_service.py) |
| 食谱批量导出执行器 | [mealie/services/exporter/recipe_exporter.py](mealie/services/exporter/recipe_exporter.py) |
| 食谱批量导出基类 | [mealie/services/exporter/_abc_exporter.py](mealie/services/exporter/_abc_exporter.py) |
| 共享食谱 ZIP 导出 | [mealie/routes/recipe/shared_routes.py](mealie/routes/recipe/shared_routes.py)（`GET /shared/{token}/zip`） |
| 数据修复（迁移后） | [mealie/db/fixes/fix_migration_data.py](mealie/db/fixes/fix_migration_data.py) |
| Group 模型 | [mealie/db/models/group/group.py](mealie/db/models/group/group.py) |
| Household 模型 | [mealie/db/models/household/household.py](mealie/db/models/household/household.py) |
| Recipe 模型 | [mealie/db/models/recipe/recipe.py](mealie/db/models/recipe/recipe.py) |
| Meal Plan 模型 | [mealie/db/models/household/mealplan.py](mealie/db/models/household/mealplan.py) |
| Tag/Category 模型 | [mealie/db/models/recipe/tag.py](mealie/db/models/recipe/tag.py), [mealie/db/models/recipe/category.py](mealie/db/models/recipe/category.py) |
| 恢复测试用例 | [tests/unit_tests/services_tests/backup_v2_tests/test_backup_v2.py](tests/unit_tests/services_tests/backup_v2_tests/test_backup_v2.py) |
| Recipe ZIP 导入集成测试 | [tests/integration_tests/user_recipe_tests/test_recipe_bulk_import.py](tests/integration_tests/user_recipe_tests/test_recipe_bulk_import.py) |
