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

**Recipe 数据目录实际磁盘结构**（[mealie/schema/recipe/recipe.py](mealie/schema/recipe/recipe.py#L202-L237)）：

```
RECIPE_DATA_DIR/{recipe_id}/       ← recipe.directory
├── images/                        ← recipe.image_dir
│   ├── original.webp              ← max 2048×2048, quality 80
│   ├── min-original.webp          ← max 1024×1024, quality 80
│   ├── tiny-original.webp         ← 300×300 center-cropped, quality 80
│   └── timeline/
│       └── {timeline_event_id}/
│           ├── original.webp
│           ├── min-original.webp
│           └── tiny-original.webp
└── assets/                        ← recipe.asset_dir
    ├── {filename1}.pdf
    ├── {filename2}.md
    └── ...
```

图片文件名枚举定义见 [mealie/schema/recipe/recipe_image_types.py](mealie/schema/recipe/recipe_image_types.py#L4-L7)，实际生成逻辑见 [mealie/pkgs/img/minify.py](mealie/pkgs/img/minify.py#L156-L206) → `PillowMinifier.minify()`。

**批量导出 ZIP 内实际结构**（由 [mealie/services/exporter/_abc_exporter.py](mealie/services/exporter/_abc_exporter.py#L48-L88) 和 [mealie/services/exporter/recipe_exporter.py](mealie/services/exporter/recipe_exporter.py#L36-L41) 产生）：

```
{export_id}.zip
└── recipes/
    └── {recipe_slug}/
        ├── {recipe_slug}.json                        # ① zip.writestr() 最先写入
        ├── images/                                    # ② _post_export_hook 递归拷贝 recipe 目录
        │   ├── original.webp
        │   ├── min-original.webp
        │   ├── tiny-original.webp
        │   └── timeline/
        │       └── {timeline_event_uuid}/
        │           ├── original.webp
        │           ├── min-original.webp
        │           └── tiny-original.webp
        └── assets/                                    # 非 .json 文件全量拷贝
            ├── {file1}.pdf
            └── ...
```

**写入顺序**：
1. 先调用 `zip.writestr(f"recipes/{slug}/{slug}.json", ...)` 写入 JSON
2. 再调用 `write_dir_to_zip(recipe_dir, f"recipes/{slug}", {".json"})` 递归遍历 `recipe.directory` 下所有非 `.json` 文件

`Path.iterdir()` 的顺序取决于 OS/文件系统，典型（字母序）下目录内容为：
```
assets/          # 目录
images/          # 目录
```

在 `images/` 内按字母序：
```
min-original.webp    # 'm' < 'o' < 't'
original.webp
tiny-original.webp
timeline/            # 目录
```

**因此，单食谱批量导出 ZIP 的 namelist() 典型顺序为**：
```
recipes/slug/slug.json
recipes/slug/assets/file1.pdf
recipes/slug/images/min-original.webp
recipes/slug/images/original.webp
recipes/slug/images/tiny-original.webp
recipes/slug/images/timeline/{uuid}/min-original.webp
recipes/slug/images/timeline/{uuid}/original.webp
recipes/slug/images/timeline/{uuid}/tiny-original.webp
```

**多食谱批量导出 ZIP（2 个食谱 A→B）namelist() 典型顺序**：
```
recipes/A/A.json
recipes/A/assets/...
recipes/A/images/min-original.webp
recipes/A/images/original.webp
recipes/A/images/tiny-original.webp
recipes/A/images/timeline/{uuid}/.../tiny-original.webp
recipes/B/B.json
recipes/B/assets/...
recipes/B/images/min-original.webp
recipes/B/images/original.webp
recipes/B/images/tiny-original.webp
recipes/B/images/timeline/{uuid}/.../tiny-original.webp   ← 最后一个 .webp
```

### 2.3 共享食谱单条 ZIP 导出

**生成入口**：[mealie/routes/recipe/shared_routes.py](mealie/routes/recipe/shared_routes.py#L42-L58) → `get_shared_recipe_as_zip()`

**ZIP 包内结构（扁平，无子目录，仅 2 个文件）**：

```
{recipe_slug}.zip
├── {recipe_slug}.json           # Recipe Pydantic 模型序列化
└── original.webp                # 仅 images/original.webp，无缩略图、无 timeline、无 assets
```

实现：
```python
image_asset = recipe.image_dir.joinpath(RecipeImageTypes.original.value)  # "original.webp"
with ZipFile(temp_path, "w") as myzip:
    myzip.writestr(f"{recipe.slug}.json", recipe.model_dump_json())
    if image_asset.is_file():
        myzip.write(image_asset, arcname=image_asset.name)   # arcname = "original.webp"
```

namelist() 顺序固定：`["{slug}.json", "original.webp"]`。

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
    │         ├─ images/original.webp 写入（无论源是什么分辨率，都以 original 名义保存）
    │         ├─ PillowMinifier.minify() 生成 min-original.webp + tiny-original.webp
    │         │  （如果 source 本身就是 tiny，此处会再次压缩 tiny→tiny，画质严重损失）
    │         └─ 异常时删除 partial 文件并 re-raise
    ▼
返回 Recipe slug
```

#### 3.3.2 关键代码细节

**ZIP 内文件识别逻辑（仅后缀匹配，无目录结构约束）**：

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

代码见 [mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L313-L320)。

**嵌套 webp 匹配规则（代码核实结论）**：
- `file.endswith(".webp")` 对路径中的 `/` 完全不敏感，**任意深度子目录中的 webp 都会匹配**
  - `recipes/slug/images/original.webp` ✅ 匹配
  - `recipes/slug/images/timeline/{uuid}/tiny-original.webp` ✅ 匹配
  - `original.webp` ✅ 匹配
- 多个 `.json` / `.webp` 时，**最后一次循环赋值生效**（非确定性，完全取决于 ZIP 内 `namelist()` 顺序）
- 仅支持 `.webp` 后缀，`.jpg`、`.png`、`.jpeg` 等会被**静默忽略**，无任何警告

**多张 webp "最后覆盖" 实际选中哪类图片（按典型 namelist 字母序推导）**：

| ZIP 来源 | namelist() 中最后一个 .webp | 实际选中图片 | 分辨率 | 后果 |
|---------|--------------------------|------------|-------|------|
| **共享食谱单条导出** | `original.webp` | `images/original.webp` | max 2048×2048 | ✅ 正确 |
| **单食谱批量导出（无 timeline）** | `images/tiny-original.webp` | **tiny 缩略图** | 300×300 center-crop | 🔴 严重画质下降 |
| **单食谱批量导出（有 timeline）** | `images/timeline/{uuid}/tiny-original.webp` | **时间线事件的 tiny 缩略图** | 300×300 center-crop | 🔴 严重画质下降 + 可能不是食谱主图 |
| **多食谱批量导出（N 个食谱）** | 最后一个食谱的 `.../tiny-original.webp` | **最后一个食谱**的 tiny 缩略图 | 300×300 | 🔴 最后一个食谱的 tiny 被当作"原图"，且 JSON 也是最后一个食谱的（JSON 和图片恰好同食谱，但图片分辨率错了） |
| **手工构造 ZIP** | 取决于 namelist 顺序 | 不确定 | 不确定 | 🟠 非确定性 |

**Group/Household 强制重绑定**：[mealie/services/recipe/recipe_service.py](mealie/services/recipe/recipe_service.py#L283-L297)

```python
def _process_recipe_data(self, key, data):
    if isinstance(data, dict):
        data["group_id"] = str(self.user.group_id)
        data["household_id"] = str(self.user.household_id)
```

递归应用到所有嵌套 dict，意味着：
- Recipe JSON 中自带的 `group_id`、`household_id`、`user_id` 全部被**静默丢弃**
- 嵌套的 Tag / Category / RecipeIngredient 对象的 `group_id` 也被强制替换
- **注意**：RecipeAsset 对象（列表中的字典）同样会被递归替换 group_id/household_id

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

**图片写入（核实文件名）**：[mealie/services/recipe/recipe_data_service.py](mealie/services/recipe/recipe_data_service.py#L85-L109) 和 [mealie/pkgs/img/minify.py](mealie/pkgs/img/minify.py#L156-L206)

```python
def write_image(self, file_data, extension, image_dir=None):
    image_path = image_dir.joinpath(f"original.{extension}")
    image_path.unlink(missing_ok=True)   # 若存在先删除（覆盖语义）
    with image_path.open("wb") as f:
        f.write(file_data)
    self.minifier.minify(image_path)     # 生成 min-original.webp + tiny-original.webp
```

minify 的实际输出文件名：
```python
org_dest = image_path.parent.joinpath("original.webp")     # ← 输入本身就叫 original.webp
min_dest = image_path.parent.joinpath("min-original.webp")
tiny_dest = image_path.parent.joinpath("tiny-original.webp")
```

**严重隐患**：如果输入的 bytes 本身是一张 300×300 的 `tiny-original.webp`，它会被当作 `original.webp` 写入磁盘，然后 minify 会再次从它生成：
- `min-original.webp`：300×300 thumbnail → 被缩到 1024×1024 canvas（实际无放大，等于原图）
- `tiny-original.webp`：300×300 → 再次 center-crop 到 300×300（重编码，画质进一步下降）
- 最终新 recipe 的所有图片都来自时间线的 tiny 缩略图，最大分辨率仅 300×300。

#### 3.3.3 导入 ZIP 与导出 ZIP 结构对应关系（代码核实版）

| 导出类型 | ZIP 结构 | create_from_zip 匹配行为 | 兼容性结论 |
|---------|---------|------------------------|----------|
| **共享食谱单条导出** (`shared/{token}/zip`) | 扁平：`{slug}.json` + `original.webp`，共 2 个文件 | ✅ JSON 和 webp 都唯一匹配，namelist 顺序确定 | **完全兼容**（最匹配的导入来源） |
| **单食谱批量导出（无 timeline 事件）** | `recipes/{slug}/{slug}.json` + `images/` 下 3 个 webp + `assets/` | `namelist()` 最后一个 webp 是 `images/tiny-original.webp`（300×300） | ⚠ **JSON 能读，图片错拿 tiny 缩略图当原图**；assets 文件和 DB 记录不匹配 |
| **单食谱批量导出（有 timeline 事件）** | 同上 + `images/timeline/{uuid}/` 下 3 个 webp | 最后一个 webp 是 timeline 的 `tiny-original.webp` | 🔴 **JSON 能读，但图片拿了时间线事件的 300×300 缩略图**；可能不是食谱主图 |
| **多食谱批量导出（N 个食谱）** | N 份 `recipes/{slug}/...` 叠加 | 最后 JSON 和最后 webp 都来自**最后一个食谱** | 🟠 **仅导入最后一个食谱**，其余 N-1 个完全丢失；且仍拿的是 tiny 缩略图 |
| **MealieAlpha 迁移包** | `recipes/{slug}/{slug}.json` + 同级图片目录 | 取决于具体子目录结构 | ⚠ 不确定 |
| **手工构造 ZIP** | 任意目录 + `.json`/`.webp` | 只要后缀匹配即可，但多文件结果不确定 | 🟡 需保证仅 1 JSON + 1 webp |

**代码核实的关键不匹配点**：

1. **🔴 图片错配是确定性问题，不是"可能读不到"**：批量导出的 3+ 张 webp 全部匹配 `.endswith(".webp")`，不是之前认为的"图片读不到"；实际是**读多了且拿错了**。字母序下 `tiny-original.webp`（'t' 开头）几乎总是最后一个。

2. **🔴 RecipeAsset 孤儿 DB 记录**：
   - JSON 中 `recipe_asset: [{...}, {...}]` 列表随 Recipe 一起通过 `Recipe(**cleaned_dict)` 写入数据库（`recipe_assets` 表有记录）
   - 但 `assets/` 下的实际文件（PDF/MD 等）在 create_from_zip 中**完全不处理**，不会写入新 recipe 的 `assets/` 目录
   - 结果：DB 里有 RecipeAsset 行，磁盘上文件不存在 → 前端点击下载 404

3. **🔴 Timeline 事件孤儿图片**：
   - JSON 中 `recipe_timeline: [{... image: true ...}, ...]` 随 Recipe 写入 `recipe_timeline_events` 表
   - 但 `images/timeline/{event_id}/` 下的 3 个 webp 文件完全不拷贝
   - 结果：时间线事件引用图片路径，但文件不存在 → `/api/recipes/{id}/images/timeline/...` 返回 404

4. **🟡 Recipe.image 缓存 key 不匹配**：
   - `recipes.image` 列（[mealie/db/models/recipe/recipe.py](mealie/db/models/recipe/recipe.py#L84)）存储的是源 Recipe 的缓存 key 字符串
   - 新 Recipe 落库时沿用了 JSON 中的旧值
   - 但实际图片存放在 `RECIPE_DATA_DIR/{new_recipe_id}/images/`（按新 recipe id 生成路径）
   - 前端首次渲染可能尝试用旧缓存 key 取图 → 404；刷新后从新路径读取正常

5. **🟡 多食谱批量导出 ZIP 仅导入最后一个**：
   - `ABCExporter.export()` 按 `items()` 顺序逐个写入 A→B→C→...
   - 最后 JSON 和最后 webp 都来自最后一个食谱
   - 前面 N-1 个食谱的 JSON 和图片被完全静默丢弃，无报错、无日志

---

## 4. Recipe、Meal Plan、Taxonomy 与 Household 绑定关系

### 4.1 核心层级模型

```
Group (组)
  ├── 1:N → Household (家庭)
  │        └── 1:N → User (用户)
  │                 └─ 1:N → Recipe (食谱，通过 user 关联)
  │                 └─ 1:N → GroupMealPlan (用餐计划)
  │                 └─ 1:N → ShoppingList (购物清单)
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
| **嵌套子对象 schema 演进** | RecipeIngredient / RecipeStep / Nutrition / RecipeAsset 等新增必填字段 | 同上 |
| **Tag/Category schema 变更** | Tag 模型新增必填列（如 color 字段），旧 JSON 没有 | `_transform_category_or_tag` 在 `repo.create(data)` 时抛 IntegrityError |
| **废弃字段被删除** | 导出的 JSON 含已删除字段名 | Pydantic 默认 `extra="ignore"`，通常可容忍；但若 `extra="forbid"` 则失败 |

**关键缺失**：create_from_zip 链路**没有**调用 `cleaner.clean()` 做 schema 兼容层处理（第三方迁移链路有），完全依赖 Pydantic 的 `model_validate`，也**没有**版本号/迁移策略机制。

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
3. **PostgreSQL 序列重置不完整**：[mealie/services/backups_v2/alchemy_exporter.py](mealie/services/backups_v2/alchemy_exporter.py#L217-L240) 的序列重置列表是**硬编码**的，新增加 ID 自增序列的表如果忘记加进列表，会导致后续 INSERT 主键冲突。

#### 5.2.2 Recipe ZIP 导入（代码核实版）

| 资源类型 | 处理方式 | 风险等级 | 详细后果 |
|---------|---------|---------|---------|
| **共享 ZIP 的 `original.webp`** | `write_image(bytes, "webp")` → 写入 + minify 生成 3 尺寸 | ✅ 正常 | 仅共享 ZIP 可靠 |
| **批量 ZIP 的 `images/original.webp`** | 被 `min-original.webp` 和 `tiny-original.webp` 覆盖，实际不会被选中 | 🔴 严重 | 原图永远不会被用到 |
| **批量 ZIP 的 `images/tiny-original.webp`** | 被当作 "original" 写入 → 再次 minify 生成 min+tiny | 🔴 严重 | 300×300 center-crop 成为最高清版本，所有尺寸都模糊 |
| **批量 ZIP 的 timeline `tiny-original.webp`** | 同上，但可能是**非食谱主图**的时间线事件图 | 🔴 严重 | 食物成品照片被替换成操作步骤截图 / 空盘照等 |
| **非 webp 格式图片（jpg/png/gif...）** | `elif file.endswith(".webp")` 不匹配 → **静默丢弃** | 🔴 严重 | 图片完全丢失，无任何报错或日志 |
| **RecipeAsset 文件（PDF/MD/TXT...）** | 完全不读取不拷贝 | 🔴 严重 | **DB 已创建 RecipeAsset 记录**（随 JSON 写入），磁盘上无文件 → 404 孤儿行 |
| **Timeline 事件图片** | 完全不读取不拷贝 | 🟠 中高 | DB 已创建 `recipe_timeline_events` 记录，磁盘上无图片 → 时间线图片 404 |
| **Recipe.image 缓存 key** | JSON 中源 recipe 的缓存 key 直接落库 | 🟡 中 | 缓存 key 与新 recipe 的图片路径/UUID 不匹配，首次前端加载可能 404 |
| **多食谱 ZIP 中除最后一个外的所有食谱** | JSON 和图片被循环覆盖 | 🔴 严重 | N-1 个食谱完全静默丢失，无提示 |

**路径穿越防护**：create_from_zip 直接使用 `myzip.open(file)` 读取 bytes 到内存，不写入磁盘（无路径拼接），因此不存在 `../../` 目录穿越风险，这一点优于第三方迁移链路。

**图片被误用的连锁损失（代码核实）**：

假设源食谱有一张 4000×3000 的原图，导出到批量 ZIP 后再导入：
1. 源 `tiny-original.webp`：300×300 center-crop（丢弃了边缘内容），~15KB
2. 该 bytes 被写入新 recipe 的 `original.webp`
3. minify 从这张 300×300 再次生成：
   - 新 `min-original.webp`：300×300 → thumbnail 到 1024 canvas（实际还是 300×300，被重新编码）
   - 新 `tiny-original.webp`：300×300 → 再次 center-crop 到 300×300（二次重编码，进一步损失）
4. 最终新 recipe 的最高清图片就是那张 300×300 center-crop，原图细节永久丢失

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
| **Tag slug 冲突（同 Group 已有同名 Tag）** | `get_one(slug)` 命中 → 返回已有记录的 `model_dump()` | 🟠 中高 | 导入 JSON 中 tag 的自定义属性（如 id、创建时间）被静默丢弃，直接复用目标实例已有 tag |
| **Category slug 冲突** | 同上 | 🟠 中高 | 同上 |
| **User ID 冲突（JSON 中 user_id 指向不存在/跨 Group 用户）** | `_transform_user_id` 回退到当前用户 | 🟡 中 | 原作者信息丢失，全部归为导入者 |
| **多 webp 互相覆盖** | 循环最后赋值生效 | 🔴 高 | 字母序下最后是 tiny 缩略图，高概率拿错图片 |
| **多 JSON 互相覆盖（多食谱 ZIP）** | 循环最后赋值生效 | 🔴 高 | 仅最后一个食谱被导入，其余静默丢失 |
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
| RecipeAsset 处理 | ✅ 全量恢复 | ⚠ 视迁移器 | 🔴 **DB 孤儿记录，文件丢失** |
| Timeline 图片处理 | ✅ 全量恢复 | ⚠ 视迁移器 | 🔴 **DB 孤儿记录，图片丢失** |
| 主图分辨率正确性 | ✅ 原样恢复 | ⚠ 视迁移器 | 🔴 **仅共享 ZIP 正确，批量 ZIP 拿 tiny 缩略图** |
| 非 webp 图片 | ✅ 原样恢复 | ⚠ 视迁移器 | 🔴 静默丢弃 |
| 多食谱 ZIP 处理 | — | — | 🔴 **仅导入最后一个** |
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
| 图片压缩/尺寸生成 | [mealie/pkgs/img/minify.py](mealie/pkgs/img/minify.py) |
| 图片文件名枚举 | [mealie/schema/recipe/recipe_image_types.py](mealie/schema/recipe/recipe_image_types.py) |
| 食谱批量导出服务 | [mealie/services/recipe/recipe_bulk_service.py](mealie/services/recipe/recipe_bulk_service.py) |
| 食谱批量导出执行器 | [mealie/services/exporter/recipe_exporter.py](mealie/services/exporter/recipe_exporter.py) |
| 食谱批量导出基类 | [mealie/services/exporter/_abc_exporter.py](mealie/services/exporter/_abc_exporter.py) |
| 共享食谱 ZIP 导出 | [mealie/routes/recipe/shared_routes.py](mealie/routes/recipe/shared_routes.py)（`GET /shared/{token}/zip`） |
| 数据修复（迁移后） | [mealie/db/fixes/fix_migration_data.py](mealie/db/fixes/fix_migration_data.py) |
| Recipe Schema（含 directory/image_dir/asset_dir 定义） | [mealie/schema/recipe/recipe.py](mealie/schema/recipe/recipe.py) |
| Group 模型 | [mealie/db/models/group/group.py](mealie/db/models/group/group.py) |
| Household 模型 | [mealie/db/models/household/household.py](mealie/db/models/household/household.py) |
| Recipe 模型 | [mealie/db/models/recipe/recipe.py](mealie/db/models/recipe/recipe.py) |
| Meal Plan 模型 | [mealie/db/models/household/mealplan.py](mealie/db/models/household/mealplan.py) |
| Tag/Category 模型 | [mealie/db/models/recipe/tag.py](mealie/db/models/recipe/tag.py), [mealie/db/models/recipe/category.py](mealie/db/models/recipe/category.py) |
| 静态图片路由（含 timeline） | [mealie/routes/media/media_recipe.py](mealie/routes/media/media_recipe.py) |
| 恢复测试用例 | [tests/unit_tests/services_tests/backup_v2_tests/test_backup_v2.py](tests/unit_tests/services_tests/backup_v2_tests/test_backup_v2.py) |
| Recipe ZIP 导入集成测试 | [tests/integration_tests/user_recipe_tests/test_recipe_bulk_import.py](tests/integration_tests/user_recipe_tests/test_recipe_bulk_import.py) |
