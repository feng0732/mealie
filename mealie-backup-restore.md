# Mealie Backup/Restore 与备份调度梳理

本文档按照代码执行顺序，系统梳理 Mealie 系统中的备份触发、文件生成、恢复写入和异常处理逻辑。

> **路径说明：本文档中所有代码定位均使用仓库相对路径，仓库根为项目根目录。**

---

## 一、整体架构概览

备份/恢复系统主要由以下模块组成：

| 模块 | 文件 | 职责 |
|------|------|------|
| 调度服务 | `mealie/services/scheduler/scheduler_service.py` | 管理定时任务的触发与执行 |
| 调度注册 | `mealie/services/scheduler/scheduler_registry.py` | 任务注册与回调管理 |
| 备份核心 | `mealie/services/backups_v2/backup_v2.py` | 备份创建与恢复的主流程控制 |
| 备份文件处理 | `mealie/services/backups_v2/backup_file.py` | ZIP 解压与备份内容解析 |
| 数据库导入导出 | `mealie/services/backups_v2/alchemy_exporter.py` | SQLAlchemy 数据库的序列化与反序列化 |
| API 路由 | `mealie/routes/admin/admin_backups.py` | 备份管理的 REST API 接口 |
| 数据模型 | `mealie/schema/admin/backup.py` | 备份相关的 Pydantic Schema |

---

## 二、调度系统详解

### 2.1 调度启动入口

调度服务在应用启动时通过 `start_scheduler()` 函数初始化，定义于 `mealie/app.py#L124-L144`：

```python
async def start_scheduler():
    SchedulerRegistry.register_daily(
        tasks.purge_expired_tokens,
        tasks.purge_group_registration,
        tasks.purge_password_reset_tokens,
        tasks.purge_group_data_exports,
        tasks.create_mealplan_timeline_events,
        tasks.delete_old_checked_list_items,
    )

    SchedulerRegistry.register_minutely(
        tasks.post_group_webhooks,
    )

    SchedulerRegistry.register_hourly(
        tasks.locked_user_reset,
    )

    SchedulerRegistry.print_jobs()
    await SchedulerService.start()
```

**注意：备份任务当前并未注册到调度系统中自动执行，备份只能通过 API 手动触发。**

### 2.2 调度任务注册表

`SchedulerRegistry`（`mealie/services/scheduler/scheduler_registry.py#L8-L59`）是一个静态容器类，管理三类调度任务：

```python
class SchedulerRegistry:
    _daily: list[Callable] = []       # 每日任务
    _hourly: list[Callable] = []      # 每小时任务
    _minutely: list[Callable] = []    # 每5分钟任务
```

注册方法：
- `register_daily(*callbacks)` - 注册每日任务
- `register_hourly(*callbacks)` - 注册每小时任务
- `register_minutely(*callbacks)` - 注册每5分钟任务

### 2.3 调度服务执行

`SchedulerService.start()`（`mealie/services/scheduler/scheduler_service.py#L20-L27`）启动三个调度循环：

```python
class SchedulerService:
    @staticmethod
    async def start():
        await run_minutely()     # 每5分钟执行
        await run_hourly()       # 每小时执行
        asyncio.create_task(schedule_daily())  # 每日定时执行
```

#### 2.3.1 每日任务调度时间计算

`schedule_daily()`（`mealie/services/scheduler/scheduler_service.py#L30-L53`）根据配置的 `DAILY_SCHEDULE_TIME`（默认 `23:45`）计算下一次执行时间：

1. 解析配置的本地时间，转换为 UTC 时间
2. 计算距离下次执行的时间差
3. 使用 `asyncio.sleep()` 等待到目标时间
4. 触发 `run_daily()` 执行

配置定义见 `mealie/core/settings/settings.py#L176-L200`。

#### 2.3.2 装饰器驱动的循环执行

三个调度函数均使用 `@repeat_every` 装饰器，定义于 `mealie/services/scheduler/runner.py#L19-L83`：

- `run_daily()` - `@repeat_every(minutes=1440, wait_first=False)` - 立即执行，之后每24小时
- `run_hourly()` - `@repeat_every(minutes=60, wait_first=True)` - 先等待1小时再执行
- `run_minutely()` - `@repeat_every(minutes=5, wait_first=True)` - 先等待5分钟再执行

#### 2.3.3 任务执行包装器

`_scheduled_task_wrapper()`（`mealie/services/scheduler/scheduler_service.py#L56-L60`）确保单个任务异常不会影响整个调度：

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.error("Error in scheduled task func='%s': exception='%s'", callable.__name__, e)
```

---

## 三、备份触发流程

### 3.1 API 触发入口

备份通过管理员 API 手动触发，入口位于 `mealie/routes/admin/admin_backups.py#L44-L54`：

```python
@router.post("", status_code=status.HTTP_201_CREATED, response_model=SuccessResponse)
def create_one(self):
    backup = BackupV2()

    try:
        backup.backup()
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e

    return SuccessResponse.respond("Backup created successfully")
```

### 3.2 BackupV2 初始化

`BackupV2.__init__()`（`mealie/services/backups_v2/backup_v2.py#L26-L32`）创建核心实例：

```python
def __init__(self, db_url: str | None = None) -> None:
    super().__init__()
    self.db_url: str = db_url or self.settings.DB_URL
    self.db_exporter = AlchemyExporter(self.db_url)
```

关键属性：
- `EXCLUDE_DIRS = {"backups", ".temp"}` - 排除目录
- `EXCLUDE_FILES = {"mealie.db"}` - 排除文件
- `EXCLUDE_FILES_REGEX = {re.compile(r"^mealie\.log(?:\.\d+)?$")}` - 排除日志文件
- `EXCLUDE_EXTENTIONS = {".zip"}` - 排除已有压缩包

---

## 四、备份文件生成流程

### 4.1 主流程：backup() 方法

`BackupV2.backup()`（`mealie/services/backups_v2/backup_v2.py#L44-L76`）执行完整备份：

#### 步骤 1：生成备份文件名

```python
timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y.%m.%d.%H.%M.%S")
short_hash = self.settings.GIT_COMMIT_HASH[:7]

if APP_VERSION == "develop":
    backup_name = f"mealie_dev-{short_hash}_{timestamp}.zip"
elif APP_VERSION == "nightly":
    backup_name = f"mealie_nightly-{short_hash}_{timestamp}.zip"
else:
    backup_name = f"mealie_{APP_VERSION}_{timestamp}.zip"

backup_file = self.directories.BACKUP_DIR / backup_name
```

备份目录由 `mealie/core/settings/directories.py#L7` 定义：`{DATA_DIR}/backups`。

#### 步骤 2：导出数据库数据

```python
database_json = self.db_exporter.dump()
```

数据库导出由 `AlchemyExporter.dump()`（`mealie/services/backups_v2/alchemy_exporter.py#L166-L187`）完成，详见 4.2 节。

#### 步骤 3：创建 ZIP 归档

```python
with ZipFile(backup_file, "w") as zip_file:
    zip_file.writestr("database.json", json.dumps(database_json))

    for data_file in self.directories.DATA_DIR.glob("**/*"):
        if data_file.name in self.EXCLUDE_FILES:
            continue
        if any(pattern.search(data_file.name) for pattern in self.EXCLUDE_FILES_REGEX):
            continue
        if data_file.is_file() and data_file.suffix not in self.EXCLUDE_EXTENTIONS:
            if data_file.parent.name in self.EXCLUDE_DIRS:
                continue
            zip_file.write(data_file, f"data/{data_file.relative_to(self.directories.DATA_DIR)}")
```

ZIP 内部结构：
```
backup.zip
├── database.json      # 数据库完整导出
└── data/              # 数据目录内容（排除项除外）
    ├── users/
    ├── recipes/
    ├── groups/
    ├── .secret        # 密钥文件
    └── ...
```

### 4.2 数据库导出详解

`AlchemyExporter.dump()`（`mealie/services/backups_v2/alchemy_exporter.py#L166-L187`）的执行流程：

#### 步骤 1：修复迁移数据

```python
with self.session_maker() as session:
    try:
        fix_migration_data(session)
    except Exception:
        self.logger.error("Error fixing migration data during export; continuing anyway")
```

在导出前尝试修复数据库中的迁移数据，**异常被捕获并忽略**，确保即使修复失败也能继续备份。

#### 步骤 2：反射数据库表结构

```python
with self.engine.connect() as connection:
    self.meta.reflect(bind=self.engine)
```

使用 SQLAlchemy 的反射机制动态获取数据库中所有表结构。

#### 步骤 3：逐表导出数据

```python
result = {
    table.name: [dict(row) for row in connection.execute(table.select()).mappings()]
    for table in self.meta.sorted_tables
}
```

- 使用 `sorted_tables` 按外键依赖顺序导出
- 每行数据转换为字典格式
- 最终通过 `jsonable_encoder` 确保 JSON 可序列化

导出数据结构示例：
```json
{
  "alembic_version": [{"version_num": "abc123"}],
  "users": [{"id": "...", "username": "...", ...}],
  "recipes": [...],
  ...
}
```

---

## 五、恢复写入流程

### 5.1 API 触发入口

恢复操作通过 `mealie/routes/admin/admin_backups.py#L103-L120` 触发：

```python
@router.post("/{file_name}/restore", response_model=SuccessResponse)
def import_one(self, file_name: str):
    backup = BackupV2()
    file = self._backup_path(file_name)

    try:
        backup.restore(file)
    except BackupSchemaMismatch as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            ErrorResponse.respond("database backup schema version does not match current database"),
        ) from e
    except Exception as e:
        logger.exception(e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR) from e

    return SuccessResponse.respond("Restore successful")
```

路径安全校验由 `_backup_path()`（`mealie/routes/admin/admin_backups.py#L22-L27`）确保：
- 校验路径不超出 BACKUP_DIR 范围（防止路径遍历攻击）
- 使用 `resolve()` 获取绝对路径后比较

### 5.2 主流程：restore() 方法

`BackupV2.restore()`（`mealie/services/backups_v2/backup_v2.py#L95-L133`）执行完整恢复：

```python
def restore(self, backup_path: Path) -> None:
    self.logger.info("initializing backup restore")
    backup = BackupFile(backup_path)

    # 步骤1：数据库预备份
    if self.settings.DB_ENGINE == "sqlite":
        self._sqlite()
    elif self.settings.DB_ENGINE == "postgres":
        self._postgres()

    with backup as contents:
        # 步骤2：验证备份文件
        if not contents.validate():
            self.logger.error("Invalid backup file...")
            raise ValueError("Invalid backup file")

        database_json = contents.read_tables()

        # 步骤3：清空现有数据库
        self.logger.info("dropping all database tables")
        self.db_exporter.drop_all()

        # 步骤4：恢复数据库
        self.logger.info("importing database tables")
        self.db_exporter.restore(database_json)
        self.logger.info("database tables imported successfully")

        # 步骤5：恢复数据目录
        self.logger.info("restoring data directory")
        self._copy_data(contents.data_directory)
        self.logger.info("data directory restored successfully")

    self.logger.info("backup restore complete")
```

### 5.3 步骤 1：数据库预备份

- **SQLite**：`_sqlite()`（`mealie/services/backups_v2/backup_v2.py#L34-L39`）将当前数据库文件复制为 `mealie_{YYYY.MM.DD}.bak.db`
- **PostgreSQL**：`_postgres()`（`mealie/services/backups_v2/backup_v2.py#L41-L42`）当前为空实现（无操作）

### 5.4 步骤 2：备份文件解压与验证

#### 5.4.1 BackupFile 上下文管理器

`BackupFile`（`mealie/services/backups_v2/backup_file.py#L75-L90`）使用上下文协议：

```python
def __enter__(self) -> BackupContents:
    self.temp_dir = Path(tempfile.mkdtemp())
    shutil.unpack_archive(str(self.zip), str(self.temp_dir))
    return BackupContents(self.temp_dir)

def __exit__(self, exc_type, exc_val, exc_tb):
    if self.temp_dir and self.temp_dir.is_dir():
        shutil.rmtree(self.temp_dir)
        self.temp_dir = None
```

- **进入**：创建临时目录，解压 ZIP
- **退出**：无论是否异常，均清理临时目录

#### 5.4.2 Safari ZIP 兼容处理

`BackupContents._find_base()`（`mealie/services/backups_v2/backup_file.py#L15-L35`）处理 Safari 浏览器解压 ZIP 时添加的 `__MACOSX` 目录：

1. 检查是否存在 `__` 开头的目录
2. 若存在且 `database.json` 不在根目录，则进入第一个非 dunder 子目录

#### 5.4.3 备份有效性验证

`BackupContents.validate()`（`mealie/services/backups_v2/backup_file.py#L45-L55`）检查：
- 基础路径是目录
- `data/` 子目录存在
- `database.json` 文件存在

### 5.5 步骤 3：清空数据库

`AlchemyExporter.drop_all()`（`mealie/services/backups_v2/alchemy_exporter.py#L249-L287`）：

**PostgreSQL**：
1. 获取所有表及其外键约束
2. 先删除所有外键约束
3. 逐表删除（DROP TABLE）
4. 删除自定义类型 `authmethod`

**SQLite/其他**：
1. 逐表删除（DROP TABLE）
2. SQLite 会级联删除外键约束

### 5.6 步骤 4：恢复数据库数据

`AlchemyExporter.restore()`（`mealie/services/backups_v2/alchemy_exporter.py#L189-L247`）执行流程：

#### 步骤 4.1：运行 Alembic 迁移到备份版本

```python
alembic_data = db_dump["alembic_version"]
alembic_version = alembic_data[0]["version_num"]
alembic_cfg = Config(alembic_cfg_path)
command.upgrade(alembic_cfg, alembic_version)
```

确保数据库 schema 与备份数据的版本一致。

#### 步骤 4.2：禁用外键约束

使用 `ForeignKeyDisabler`（`mealie/services/backups_v2/alchemy_exporter.py#L24-L51`）上下文管理器：

- **PostgreSQL**：`SET session_replication_role = 'replica'`
- **SQLite**：`PRAGMA foreign_keys = OFF`

退出时自动恢复原始设置，异常也保证恢复。

#### 步骤 4.3：数据类型转换

`convert_types()`（`mealie/services/backups_v2/alchemy_exporter.py#L98-L118`）递归遍历数据：
- UUID 字符串 → 数据库原生 GUID 类型
- 日期时间字段名匹配 → `datetime.datetime`
- 日期字段名匹配 → `datetime.date`
- 时间字段名匹配 → `datetime.time`

识别字段名：
- datetime: `created_at`, `update_at`, `date_updated`, `timestamp`, `expires_at`, `locked_at`, `last_made`
- date: `date_added`, `date`
- time: `scheduled_time`

#### 步骤 4.4：外键完整性清理

`clean_rows()`（`mealie/services/backups_v2/alchemy_exporter.py#L120-L145`）移除会违反外键约束的行：
- 逐行检查每个外键引用是否在目标表中存在
- 无效行被记录 warning 并移除

#### 步骤 4.5：批量插入数据

```python
for table_name, rows in data.items():
    if not rows:
        continue
    table = self.meta.tables[table_name]
    rows = self.clean_rows(db_dump, table, rows)
    connection.execute(table.delete())
    connection.execute(insert(table), rows)
```

- 先清空表（DELETE）
- 使用 SQLAlchemy Core 的 `insert()` 批量插入

#### 步骤 4.6：PostgreSQL 序列恢复

PostgreSQL 需手动恢复自增序列：
```python
sequences = [
    ("api_extras_id_seq", "api_extras"),
    ("group_meal_plans_id_seq", "group_meal_plans"),
    ...
]
sql = "\n".join([f"SELECT SETVAL('{seq}', (SELECT MAX(id) FROM {table}));" for seq, table in sequences])
connection.execute(text(dedent(sql)))
```

#### 步骤 4.7：完成数据库初始化

```python
self.engine.dispose()  # 释放连接
init_db.main()          # 重新初始化数据库（运行剩余迁移等）
```

### 5.7 步骤 5：恢复数据目录

`_copy_data()`（`mealie/services/backups_v2/backup_v2.py#L78-L93`）：

```python
def _copy_data(self, data_path: Path) -> None:
    for f in data_path.iterdir():
        if f.is_file():
            if f.name not in self.RESTORE_FILES:  # RESTORE_FILES = {".secret"}
                continue
            shutil.copyfile(f, self.directories.DATA_DIR / f.name)
            continue
        shutil.rmtree(self.directories.DATA_DIR / f.name)
        shutil.copytree(f, self.directories.DATA_DIR / f.name)

    get_app_settings.cache_clear()
    self.settings = get_app_settings()
```

- **文件**：仅复制 `.secret` 密钥文件
- **目录**：删除原有目录，完整替换为备份中的目录
- **缓存刷新**：由于 `.secret` 可能变化，清除并重新加载 AppSettings 缓存

---

## 六、异常处理梳理

### 6.1 备份阶段异常

| 位置 | 异常类型 | 处理方式 |
|------|----------|----------|
| `mealie/routes/admin/admin_backups.py#L48-L52` | `Exception`（备份过程中任意异常） | 记录日志 + 返回 500 HTTP 错误 |
| `mealie/services/backups_v2/alchemy_exporter.py#L174-L177` | `Exception`（迁移数据修复失败） | 记录 error 日志 + **忽略继续** |

### 6.2 恢复阶段异常

| 位置 | 异常类型 | 处理方式 |
|------|----------|----------|
| `mealie/routes/admin/admin_backups.py#L109-L118` | `BackupSchemaMismatch` | 返回 400 + "database backup schema version does not match" |
| `mealie/routes/admin/admin_backups.py#L116-L118` | `Exception`（恢复过程中任意异常） | 记录日志 + 返回 500 HTTP 错误 |
| `mealie/services/backups_v2/backup_v2.py#L108-L112` | 备份文件验证失败 | 记录 error 日志 + 抛出 `ValueError("Invalid backup file")` |
| `mealie/services/backups_v2/alchemy_exporter.py#L40-L51` | 外键约束恢复失败 | 记录 exception 日志 + 重新抛出异常 |
| `mealie/services/backups_v2/alchemy_exporter.py#L136-L139` | 外键引用无效 | 记录 warning 日志 + **移除无效行继续** |

#### 6.2.1 BackupSchemaMismatch 异常的特殊说明

`BackupSchemaMismatch` 定义于 `mealie/services/backups_v2/backup_v2.py#L15`：

```python
class BackupSchemaMismatch(Exception): ...
```

**经全仓库代码搜索确认：该异常类在当前代码库中没有任何 `raise` 抛出点，仅在 `mealie/routes/admin/admin_backups.py#L111` 处有 `except` 捕获。**

这意味着：
- 这是一处**死代码（Dead Code）**，当前恢复流程中永远不会触发该异常分支
- 该异常原本设计意图可能是用于校验备份文件的数据库 schema 版本与当前数据库是否匹配，但相关校验逻辑尚未实现（或已被移除）
- 实际的 schema 版本兼容由 Alembic 迁移机制处理（`mealie/services/backups_v2/alchemy_exporter.py#L189-L201` 中的 `command.upgrade(alembic_cfg, alembic_version)`），通过将数据库迁移到备份版本完成，而不是抛出 `BackupSchemaMismatch`

若迁移失败会由 alembic 自行抛出异常，最终被 `except Exception` 分支捕获并返回 500

### 6.3 调度阶段异常

| 位置 | 异常类型 | 处理方式 |
|------|----------|----------|
| `mealie/services/scheduler/scheduler_service.py#L56-L60` | 单个调度任务异常 | 记录 error 日志 + **继续执行其他任务** |
| `mealie/services/scheduler/runner.py#L71-L76` | `repeat_every` 装饰器内异常 | 可选记录日志 + 可选继续重复执行（默认不抛出） |

### 6.4 资源清理保证

| 资源 | 清理机制 |
|------|----------|
| 备份解压临时目录 | `BackupFile.__exit__()`（`mealie/services/backups_v2/backup_file.py#L86-L89`）上下文管理器，异常时也会删除 |
| 外键约束设置 | `ForeignKeyDisabler.__exit__()`（`mealie/services/backups_v2/alchemy_exporter.py#L40-L51`）上下文管理器 |
| 数据库连接 | `AlchemyExporter.restore()`（`mealie/services/backups_v2/alchemy_exporter.py#L242-L244`）中显式 `engine.dispose()` |
| SQLite 预备份文件 | 保留在 `DATA_DIR/mealie_{date}.bak.db`，**不自动清理** |

---

## 七、数据模型定义

### 7.1 备份 Schema

定义于 `mealie/schema/admin/backup.py`：

- `BackupOptions` - 备份选项（recipes/settings/themes/groups/users/notifications）
- `CreateBackup` - 创建备份请求（tag, options, templates）
- `BackupFile` - 单个备份文件信息（name, date, size）
- `AllBackups` - 备份列表响应（imports 列表 + templates 列表）

### 7.2 恢复 Schema

定义于 `mealie/schema/admin/restore.py`：

- `ImportBase` - 导入基础（name, status, exception）
- `RecipeImport` / `CommentImport` / `SettingsImport` / `GroupImport` / `UserImport` - 各类型导入结果

---

## 八、备份 API 端点一览

所有端点位于 `mealie/routes/admin/admin_backups.py`，前缀 `/api/admin/backups`：

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 获取所有备份文件列表 |
| POST | `/` | 触发创建新备份 |
| GET | `/{file_name}` | 获取备份文件下载令牌 |
| DELETE | `/{file_name}` | 删除指定备份文件 |
| POST | `/upload` | 上传备份 ZIP 文件 |
| POST | `/{file_name}/restore` | 从指定备份文件恢复 |

---

## 九、上传备份进入恢复列表的输入链路

上传一个外部备份 ZIP 文件，使其出现在备份列表中可供恢复，完整链路如下：

### 9.1 前端上传触发

**入口组件**：`frontend/app/pages/admin/backups.vue#L89-L95`

```html
<AppButtonUpload
  :text-btn="false"
  url="/api/admin/backups/upload"
  accept=".zip"
  color="info"
  @uploaded="refreshBackups()"
/>
```

- 用户点击上传按钮后触发文件选择
- 限制文件类型为 `.zip`
- 上传成功后回调 `refreshBackups()` 刷新列表

### 9.2 AppButtonUpload 上传逻辑

**组件代码**：`frontend/app/components/global/AppButtonUpload.vue#L96-L130`

执行步骤：
1. 监听隐藏的 `<input type="file">` 的 `change` 事件（`onFileChanged`）
2. 将选中的文件封装为 `FormData`（字段名默认为 `"archive"`）
3. 通过 `api.upload.file(url, formData)` 发起 POST 请求到 `/api/admin/backups/upload`
4. 上传成功后 `emit("uploaded", response)` 通知父组件刷新

### 9.3 后端接收上传

**路由处理**：`mealie/routes/admin/admin_backups.py#L79-L101`（`upload_one`）

```python
@router.post("/upload", response_model=SuccessResponse)
def upload_one(self, archive: UploadFile = File(...)):
    if "." not in archive.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    if archive.filename.split(".")[-1] != "zip":
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    name = Path(archive.filename).stem
    app_dirs = get_app_dirs()
    dest = app_dirs.BACKUP_DIR.joinpath(f"{name}.zip")

    if dest.resolve().parent != app_dirs.BACKUP_DIR.resolve():
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    with dest.open("wb") as buffer:
        shutil.copyfileobj(archive.file, buffer)

    if not dest.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    return SuccessResponse.respond("Upload successful")
```

安全校验：
- 校验文件扩展名必须是 `.zip`
- 校验目标路径必须位于 `BACKUP_DIR` 下（防止路径遍历）
- 校验文件确实被写入

### 9.4 进入备份列表

**刷新列表接口**：`mealie/routes/admin/admin_backups.py#L29-L42`（`get_all`）

```python
@router.get("", response_model=AllBackups)
def get_all(self):
    app_dirs = get_app_dirs()
    imports = []
    for archive in app_dirs.BACKUP_DIR.glob("*.zip"):
        backup = BackupFile(
            name=archive.name, date=archive.stat().st_mtime, size=pretty_size(archive.stat().st_size)
        )
        imports.append(backup)
    imports.sort(key=operator.attrgetter("date"), reverse=True)
    return AllBackups(imports=imports, templates=templates)
```

- 通过 `BACKUP_DIR.glob("*.zip")` 扫描目录下所有 ZIP 文件
- 按修改时间倒序排列
- 上传文件落盘后自然出现在列表中，可供后续点击"恢复"按钮使用

### 9.5 上传链路全景图

```
用户点击上传按钮
  │
  ▼
AppButtonUpload 组件
  ├─ onButtonClick() → 触发隐藏 <input type="file"> click
  ├─ onFileChanged() → 获取选中文件
  └─ upload()
      ├─ 构造 FormData，字段名为 fileName prop 默认值 "archive"
      └─ POST /api/admin/backups/upload
          │
          ▼
      upload_one() [admin_backups.py]
        ├─ 校验扩展名 == .zip
        ├─ 校验路径不越界
        ├─ shutil.copyfileobj() 写入 BACKUP_DIR/{name}.zip
        └─ 返回 SuccessResponse
          │
          ▼
      @uploaded 事件触发 → refreshBackups()
        │
        ▼
      GET /api/admin/backups → get_all()
        └─ 扫描 BACKUP_DIR/*.zip，返回 AllBackups
          │
          ▼
      前端 v-data-table 渲染新上传的备份文件
```

---

## 十、上传成功与实际可恢复成功的校验时机差异

备份 ZIP 的有效性校验分布在三个不同的时间点，"上传成功"不等于"可成功恢复"。以下是三级校验的时机、内容和失败表现对比：

### 10.1 三级校验时机对比表

| 校验阶段 | 触发时机 | 执行位置 | 校验内容 | 失败表现 | 用户是否可见 |
|----------|---------|---------|---------|---------|------------|
| **第一级：上传校验** | 用户选择文件、点击上传后 | `mealie/routes/admin/admin_backups.py#L79-L101` `upload_one()` | 1. 文件名含扩展名<br>2. 扩展名必须是 `.zip`<br>3. 目标路径位于 `BACKUP_DIR` 下（防路径遍历）<br>4. 文件写入磁盘后确实存在 | 返回 400 HTTP 错误 | ✅ 上传失败立即提示 |
| **第二级：列表加载校验** | 页面加载 / 刷新备份列表时 | `mealie/routes/admin/admin_backups.py#L29-L42` `get_all()` | **无内容校验**，仅扫描 `*.zip` 文件名、取 stat 的 mtime 和 size | 无（任何 .zip 都会出现在列表中） | ✅ 文件出现在列表中，看似正常 |
| **第三级：恢复校验** | 用户点击"恢复"按钮并确认后 | `mealie/services/backups_v2/backup_v2.py#L95-L133` `BackupV2.restore()` + `mealie/services/backups_v2/backup_file.py` + `mealie/services/backups_v2/alchemy_exporter.py` | 详见 10.2 节 | 返回 500 HTTP 错误 | ✅ 恢复失败提示，但此时数据库可能已被清空 |

### 10.2 恢复阶段的多层深度校验

恢复操作中包含了大量上传阶段完全没有的校验，按执行顺序：

#### (1) ZIP 结构完整性校验（`BackupFile.__enter__`）
- `shutil.unpack_archive()` 解压 ZIP → 若文件损坏 / 不是合法 ZIP → 抛出 `shutil.ReadError` 等异常
- 发生在 `mealie/services/backups_v2/backup_file.py#L78-L79`

#### (2) Safari 兼容后的基础路径校验（`BackupContents._find_base`）
- 检查是否存在 `__MACOSX` 目录并正确调整基路径
- 发生在 `mealie/services/backups_v2/backup_file.py#L15-L35`

#### (3) 备份目录结构校验（`BackupContents.validate`）
- 基路径必须是目录
- 必须存在 `data/` 子目录
- 必须存在 `database.json` 文件
- 发生在 `mealie/services/backups_v2/backup_file.py#L45-L55`

#### (4) database.json 解析校验（`BackupContents.read_tables`）
- 读取 `database.json` 并 `json.loads()` → 若 JSON 格式错误 → 抛出 `JSONDecodeError`
- 发生在 `mealie/services/backups_v2/backup_file.py#L57-L63`

#### (5) Alembic 版本迁移校验（`AlchemyExporter.restore`）
- 从 `alembic_version` 表数据中提取版本号
- 执行 `command.upgrade(alembic_cfg, alembic_version)` → 若版本不存在 / 迁移脚本出错 → 抛出 Alembic 异常
- 发生在 `mealie/services/backups_v2/alchemy_exporter.py#L189-L201`

#### (6) 外键完整性校验（`AlchemyExporter.clean_rows`）
- 逐表逐行检查每个外键引用的目标行是否存在
- 无效行被跳过（warning 日志），不中断整个恢复
- 发生在 `mealie/services/backups_v2/alchemy_exporter.py#L120-L145`

#### (7) 数据类型转换校验（`AlchemyExporter.convert_types`）
- UUID 字符串、日期时间字符串转换为 Python 原生类型
- 格式错误的字符串可能触发转换异常
- 发生在 `mealie/services/backups_v2/alchemy_exporter.py#L98-L118`

#### (8) 数据库写入校验（`AlchemyExporter.restore` 的 insert）
- 批量 `INSERT` 时数据库约束（NOT NULL、唯一索引、CHECK 等）可能触发 IntegrityError
- 发生在 `mealie/services/backups_v2/alchemy_exporter.py#L225-L229`

### 10.3 风险：校验滞后带来的破坏性问题

**关键风险**：第三级恢复校验是在**数据库已被清空之后**才执行的。

恢复代码的执行顺序是（`mealie/services/backups_v2/backup_v2.py#L103-L131`）：

```
with backup as contents:
    if not contents.validate():          # (3) 基础结构校验
        raise ValueError
    database_json = contents.read_tables() # (4) JSON 解析校验
    self.db_exporter.drop_all()           # ⚠️ 先清空所有表
    self.db_exporter.restore(database_json)  # (5)-(8) Alembic/外键/类型/写入校验均在此之后
    self._copy_data(contents.data_directory)
```

**问题场景**：用户上传了一个 ZIP 文件（第一级通过）→ 出现在列表中（第二级无校验）→ 用户点击恢复 → 解压发现是个合法 ZIP 但内部缺少 `data/` 目录或 `database.json` → `contents.validate()` 失败抛出 `ValueError` → **但此时数据库尚未被 drop_all()，问题不大**。

然而若 ZIP 合法且结构完整，但 `database.json` 中存在不兼容数据（比如 Alembic 版本不存在，或 UUID 格式错误），则异常会在 `drop_all()` **之后**、`restore()` 内部抛出，此时**数据库已被清空但恢复中断**，只能依赖 SQLite 的预备份文件（`mealie_YYYY.MM.DD.bak.db`）或其他备份回滚。PostgreSQL 用户因 `_postgres()` 是空实现，**没有预备份保护**。

### 10.4 小结：设计上的不足

1. **上传阶段仅校验扩展名**：不做 ZIP 魔数校验、不做解压测试、不校验内部结构
2. **列表阶段完全无校验**：任何能通过上传的 `.zip`（哪怕是空文件、txt 改后缀）都会出现在恢复列表中
3. **核心校验全部在恢复阶段**：且数据库清空操作早于多项关键数据校验，存在数据丢失风险
4. **缺少"预检 / 验证"API**：没有独立的 `POST /api/admin/backups/{name}/validate` 端点让用户在恢复前确认备份有效性

---

## 十一、前端页面到后端路由的调用顺序

### 11.1 涉及的前端文件

| 文件 | 角色 |
|------|------|
| `frontend/app/pages/admin/backups.vue` | 管理员备份管理页面（实际使用） |
| `frontend/app/composables/use-backups.ts` | 旧版 composable（未被 admin 页面使用） |
| `frontend/app/lib/api/admin/admin-backups.ts` | Admin API 客户端（实际使用） |
| `frontend/app/lib/api/user/backups.ts` | User API 客户端（旧版，未被 admin 页面使用） |
| `frontend/app/lib/api/client-admin.ts` | Admin API 聚合类 |
| `frontend/app/components/global/AppButtonUpload.vue` | 通用文件上传组件 |

### 11.2 Admin Backups 页面完整调用链

页面初始化与操作的完整前后端调用顺序：

#### (1) 页面加载 → 获取备份列表

```
frontend/app/pages/admin/backups.vue
  └─ onMounted(refreshBackups)              # 挂载时触发
      └─ adminApi.backups.getAll()           # frontend/app/lib/api/admin/admin-backups.ts#L14-L16
          └─ GET /api/admin/backups
              └─ get_all()                    # mealie/routes/admin/admin_backups.py#L29-L42
                  └─ 返回 AllBackups{imports, templates}
```

#### (2) 点击 "Create Backup" → 创建备份

```
backups.vue#L82-L88: <BaseButton @click="createBackup">
  └─ createBackup()                           # backups.vue#L180-L192
      └─ adminApi.backups.create()            # admin-backups.ts#L18-L20
          └─ POST /api/admin/backups, body={}
              └─ create_one()                 # admin_backups.py#L44-L54
                  ├─ BackupV2().backup()
                  └─ 返回 SuccessResponse
      ├─ 成功 → refreshBackups() + toast 成功
      └─ 失败 → toast 错误
```

#### (3) 点击 "Upload" 上传备份文件

```
backups.vue#L89-L95: <AppButtonUpload url="/api/admin/backups/upload" ...>
  └─ AppButtonUpload.onButtonClick()          # AppButtonUpload.vue#L141-L151
      └─ <input type="file">.click()
          └─ onFileChanged()                   # AppButtonUpload.vue#L132-L139
              └─ upload()                      # AppButtonUpload.vue#L96-L130
                  └─ POST /api/admin/backups/upload, FormData
                      └─ upload_one()          # admin_backups.py#L79-L101
                          └─ 返回 SuccessResponse
                  └─ emit("uploaded")
                      └─ backups.vue @uploaded="refreshBackups()"
                          └─ GET /api/admin/backups → 刷新列表
```

#### (4) 点击 "Restore" → 恢复备份

```
backups.vue#L131-L139: <BaseButton @click.stop="setSelected(item); state.importDialog = true">
  └─ 用户勾选确认 → <BaseButton @click="restoreBackup(selected)">  # backups.vue#L51-L60
      └─ restoreBackup(fileName)              # backups.vue#L194-L210
          └─ adminApi.backups.restore(fileName)  # admin-backups.ts#L30-L32
              └─ POST /api/admin/backups/{file_name}/restore, body={}
                  └─ import_one()              # admin_backups.py#L103-L120
                      ├─ _backup_path() 路径校验
                      ├─ BackupV2().restore(file)
                      └─ 返回 SuccessResponse
          ├─ 成功 → toast 成功 → window.location.reload()
          └─ 失败 → 关闭对话框 + toast 错误
```

#### (5) 点击 "Delete" → 删除备份

```
backups.vue#L112-L123: <v-btn @click.stop="state.deleteDialog = true; deleteTarget = item.name">
  └─ 确认对话框 → @confirm="deleteBackup()"     # backups.vue#L214-L221
      └─ adminApi.backups.delete(deleteTarget)  # admin-backups.ts#L26-L28
          └─ DELETE /api/admin/backups/{file_name}
              └─ delete_one()                    # admin_backups.py#L66-L77
                  └─ file.unlink()
      └─ 成功 → refreshBackups()
```

#### (6) 点击下载图标 → 下载备份文件

```
backups.vue#L124-L130: <BaseButton download :download-url="backupsFileNameDownload(item.name)">
  └─ backupsFileNameDownload()               # backups.vue#L246
      └─ 返回 URL: `api/admin/backups/${fileName}`
          └─ 浏览器直接访问 GET /api/admin/backups/{file_name}
              └─ get_one()                    # admin_backups.py#L56-L64
                  ├─ _backup_path() 路径校验
                  └─ 返回 FileTokenResponse{file_token}
                      └─ 前端再打开 /api/utils/download?token=... 下载实际文件
```

### 11.3 两套 API 客户端对比

项目中存在两套备份 API 客户端，是代码演进遗留：

| 客户端 | 文件 | 使用方 | 请求体 |
|--------|------|--------|--------|
| `AdminBackupsApi` | `frontend/app/lib/api/admin/admin-backups.ts` | `backups.vue` 页面（当前实际使用） | `create()` / `restore()` 均传空对象 `{}` |
| `BackupAPI` | `frontend/app/lib/api/user/backups.ts` | `use-backups.ts`（旧 composable，未被 admin 页面使用） | `createOne(payload: CreateBackup)` / `restoreDatabase(fileName, payload: BackupOptions)` |

同样，`use-backups.ts` 是旧版 composable，定义了完整的 `BackupOptions` 选项和 UI 交互，但管理员页面 `backups.vue` 直接使用 `AdminBackupsApi`，走的是简单的全量备份/恢复路径。

---

## 十二、文件下载的安全链路：fileToken → /api/utils/download

备份文件下载不直接暴露文件系统路径，而是通过"短期访问令牌 + 路径白名单"的安全机制完成。完整链路包含两次 HTTP 请求。

### 12.1 前端触发下载

**入口组件**：`frontend/app/pages/admin/backups.vue#L124-L130`

```html
<BaseButton
  small
  download
  :download-url="backupsFileNameDownload(item.name)"
  class="mx-1"
  @click.stop="() => { }"
/>
```

`backupsFileNameDownload()` 返回路径 `api/admin/backups/${fileName}`（`backups.vue#L246`），注意这只是**获取令牌的 URL**，不是直接下载 URL。

### 12.2 BaseButton 封装下载逻辑

**组件代码**：`frontend/app/components/global/BaseButton.vue#L195-L198`

```javascript
const api = useUserApi();
function downloadFile() {
  api.utils.download(props.downloadUrl);
}
```

当 `download` 为 `true` 时，点击按钮不走默认 click，而是调用 `downloadFile()` → `UtilsAPI.download()`。

### 12.3 UtilsAPI 获取 fileToken

**客户端代码**：`frontend/app/lib/api/user/utils.ts#L6-L19`

```typescript
export class UtilsAPI extends BaseAPI {
  async download(url: string) {
    const { response } = await this.requests.get<FileTokenResponse>(url);
    if (!response) { return; }
    const token: string = response.data.fileToken;
    const tokenURL = prefix + "/utils/download?token=" + token;
    window.open(tokenURL, "_blank");
    return response;
  }
}
```

执行步骤：
1. 先向传入的 URL（即 `/api/admin/backups/{fileName}`）发起 **GET 请求获取 fileToken**
2. 从响应中提取 `fileToken`（JWT 格式）
3. 拼接成 `/api/utils/download?token=xxx`
4. 用 `window.open()` 在新标签页打开此 URL 触发浏览器下载

### 12.4 后端签发 fileToken

**签发路由**：`mealie/routes/admin/admin_backups.py#L55-L64` `get_one()`

```python
@router.get("/{file_name}", response_model=FileTokenResponse)
def get_one(self, file_name: str):
    file = self._backup_path(file_name)   # 路径安全校验
    if not file.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileTokenResponse.respond(create_file_token(file))
```

- 先调用 `_backup_path()` 做路径越界校验（同恢复/删除接口）
- 校验文件存在后，调用 `create_file_token(file)` 生成令牌

**令牌生成实现**：`mealie/core/security/security.py#L43-L45`

```python
def create_file_token(file_path: Path) -> str:
    token_data = {"file": str(file_path)}
    return create_access_token(token_data, expires_delta=timedelta(minutes=30))
```

- fileToken 本质是标准 JWT（HS256 算法，使用 `settings.SECRET` 签名）
- payload 中嵌入文件的绝对路径
- **有效期仅 30 分钟**

### 12.5 /api/utils/download 校验与下载

**下载路由**：`mealie/routes/utility_routes.py#L12-L31`

```python
@router.get("/download")
async def download_file(file_path: Path = Depends(validate_file_token)):
    file_path = Path(file_path).resolve()
    dirs = get_app_dirs()
    allowed_dirs = [
        dirs.BACKUP_DIR,   # admin backups
        dirs.GROUPS_DIR,   # group exports
    ]
    if not any(file_path.is_relative_to(allowed_dir) for allowed_dir in allowed_dirs):
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    if not file_path.is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    return FileResponse(file_path, media_type="application/octet-stream", filename=file_path.name)
```

通过 `validate_file_token` 依赖（`mealie/core/dependencies/dependencies.py#L152-L176`）完成令牌校验：

```python
def validate_file_token(token: str | None = None) -> Path:
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    try:
        payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        file_path = Path(payload.get("file"))
    except PyJWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
            detail="could not validate file token") from e
    if not file_path.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST)
    return file_path
```

路由中额外做了**白名单目录校验**：文件必须位于 `BACKUP_DIR` 或 `GROUPS_DIR` 之下，防止令牌被用来下载任意系统文件。

### 12.6 下载安全链路全景图

```
用户点击下载图标
  │
  ▼
BaseButton (download=true)       frontend/app/components/global/BaseButton.vue#L196-L198
  └─ api.utils.download(downloadUrl)
      │
      ▼
UtilsAPI.download()              frontend/app/lib/api/user/utils.ts#L7-L18
  ├─ 第 1 次 HTTP: GET /api/admin/backups/{fileName}
  │   │
  │   ▼
  │ get_one()                     mealie/routes/admin/admin_backups.py#L55-L64
  │   ├─ _backup_path() 路径校验
  │   ├─ create_file_token()     mealie/core/security/security.py#L43-L45
  │   │   └─ JWT(payload={file: abs_path}, expires=30min)
  │   └─ 返回 FileTokenResponse{fileToken: JWT}
  │
  ├─ 第 2 次 HTTP: window.open("/api/utils/download?token=xxx")
  │   │
  │   ▼
  │ download_file()               mealie/routes/utility_routes.py#L12-L31
  │   ├─ validate_file_token()    mealie/core/dependencies/dependencies.py#L152-L176
  │   │   ├─ jwt.decode() 验证签名和过期
  │   │   ├─ 提取 file_path
  │   │   └─ 校验文件存在
  │   ├─ 白名单校验：必须在 BACKUP_DIR 或 GROUPS_DIR 下
  │   └─ FileResponse 返回文件流
  │
  ▼
浏览器触发文件保存对话框
```

### 12.7 安全设计要点

| 防护层 | 机制 | 位置 |
|--------|------|------|
| 第一层 | 路径越界校验（`_backup_path`） | `get_one()` 签发令牌前 |
| 第二层 | JWT 签名（HS256）防篡改 | `create_file_token()` + `validate_file_token()` |
| 第三层 | 30 分钟过期时间 | `create_file_token()` 中的 `timedelta(minutes=30)` |
| 第四层 | 文件存在性校验 | `validate_file_token()` 中 |
| 第五层 | 白名单目录限制（BACKUP_DIR / GROUPS_DIR） | `/api/utils/download` 路由中 |

即使攻击者能窃取到 fileToken，也只能下载 30 分钟内、限定目录下的备份文件，无法遍历任意路径。

---

## 十三、BackupOptions / CreateBackup / ImportJob 实际参与度分析

三个 Schema 均定义于 `mealie/schema/admin/backup.py`，但在当前代码中的实际参与度差异很大。

### 13.1 Schema 定义回顾

```python
# mealie/schema/admin/backup.py#L6-L24

class BackupOptions(BaseModel):
    recipes: bool = True
    settings: bool = True
    themes: bool = True
    groups: bool = True
    users: bool = True
    notifications: bool = True

class ImportJob(BackupOptions):
    name: str
    force: bool = False
    rebase: bool = False

class CreateBackup(BaseModel):
    tag: str | None = None
    options: BackupOptions
    templates: list[str] | None = None
```

### 13.2 后端路由使用情况

**结论：后端路由完全不使用这三个 Schema 做参数解析。**

| 端点 | 参数类型 | 是否使用 BackupOptions/CreateBackup/ImportJob |
|------|----------|---------------------------------------------|
| `POST /api/admin/backups`（创建备份） | 无 body 参数 | ❌ 不使用 CreateBackup |
| `POST /api/admin/backups/upload` | `UploadFile = File(...)` | ❌ 不使用 |
| `POST /api/admin/backups/{file_name}/restore` | 仅 `file_name: str`（路径参数） | ❌ 不使用 ImportJob 或 BackupOptions |

代码证据（`mealie/routes/admin/admin_backups.py`）：
- `create_one(self)` — 无 body 参数，直接 `BackupV2().backup()` 全量备份
- `import_one(self, file_name: str)` — 只有路径参数，直接 `BackupV2().restore(file)` 全量恢复

这三个 Schema 仅在 `mealie/schema/admin/__init__.py` 中被导出，没有任何路由函数将其作为 Pydantic 请求体模型。

### 13.3 前端使用情况

**`AdminBackupsApi`（实际使用的客户端）**：`frontend/app/lib/api/admin/admin-backups.ts`

```typescript
async create() {
  return await this.requests.post<SuccessResponse | ErrorResponse>(routes.base, {});
}

async restore(fileName: string) {
  return await this.requests.post<SuccessResponse | ErrorResponse>(routes.restore(fileName), {});
}
```

- `create()` 传空对象 `{}`，**不使用** `CreateBackup`
- `restore()` 传空对象 `{}`，**不使用** `BackupOptions`

**`BackupAPI`（旧版，未使用）**：`frontend/app/lib/api/user/backups.ts`

```typescript
async createOne(payload: CreateBackup) { ... }      // 类型声明了但页面未调用
async restoreDatabase(fileName: string, payload: BackupOptions) { ... }  // 类型声明了但页面未调用
```

**`use-backups.ts`（旧 composable，未使用）**：`frontend/app/composables/use-backups.ts`

- 构造了完整的 `backupOptions`（含所有 `BackupOptions` 字段）
- 调用 `api.backups.createOne(backupOptions)` 和 `api.backups.restoreDatabase(...)`
- 但管理员页面 `backups.vue` **未引入此 composable**，使用的是简化逻辑

### 13.4 旧版 API 的边界情况：已定义引用但后端无对应端点

旧版 `BackupAPI`（`frontend/app/lib/api/user/backups.ts`）定义了 6 个路由常量，但后端**不存在对应的 `/api/backups/*` 端点**。

#### 旧版 API 路由 vs 后端实际端点对照表

| 旧版 BackupAPI 路由常量 | 路径 | 后端是否存在对应端点 |
|------------------------|------|--------------------|
| `backupsAvailable` | `/api/backups/available` | ❌ 不存在 |
| `backupsExportDatabase` | `/api/backups/export/database` | ❌ 不存在 |
| `backupsUpload` | `/api/backups/upload` | ❌ 不存在 |
| `backupsFileNameDownload` | `/api/backups/{fileName}/download` | ❌ 不存在 |
| `backupsFileNameImport` | `/api/backups/{fileName}/import` | ❌ 不存在 |
| `backupsFileNameDelete` | `/api/backups/{fileName}/delete` | ❌ 不存在 |

**后端实际存在的端点**（`mealie/routes/admin/admin_backups.py`，前缀 `/api/admin/backups`）：

| 方法 | 后端实际路径 |
|------|-----------|
| GET | `/api/admin/backups` |
| POST | `/api/admin/backups` |
| GET | `/api/admin/backups/{file_name}` |
| DELETE | `/api/admin/backups/{file_name}` |
| POST | `/api/admin/backups/upload` |
| POST | `/api/admin/backups/{file_name}/restore` |

路由注册确认于 `mealie/routes/admin/__init__.py#L23`：仅注册了 `admin_backups.router`，没有任何 `/api/backups` 前缀的用户路由。

#### 旧版 API 引用的边界情况分析

**情况 1：`BackupOptions` 在旧版请求体中的传递**
- 旧版 `BackupAPI.restoreDatabase(fileName, payload: BackupOptions)` 将 `{ name: fileName, ...payload }` 合并为 body 发送
- 假设后端端点存在且接收 `ImportJob` Schema，则 `recipes/settings/themes/groups/users/notifications` 各布尔字段会用于选择性恢复
- 但实际后端 `import_one()` 仅接收路径参数 `file_name: str`，完全忽略请求体
- **边界风险**：若未来迁移代码时忘记更新后端，前端传递的选项会被静默丢弃，用户以为"只恢复食谱"但实际执行了全量恢复

**情况 2：`CreateBackup` 中的 tag / templates 字段**
- `CreateBackup.tag: str | None` 设计用于给备份打标签（如 "before-migration"）
- `CreateBackup.templates: list[str] | None` 设计用于指定备份模板集合
- `BackupOptions` 各布尔字段设计用于选择性导出（如只导食谱、不导用户）
- 但实际 `create_one()` 无 body 参数，直接全量备份，文件名由版本号 + UTC 时间戳生成（`mealie/services/backups_v2/backup_v2.py#L46-L61`），完全不考虑 tag 或 templates
- **边界风险**：若前端旧 composable 被误用调用，会因路径不匹配（请求 `/api/backups/export/database` 而非 `/api/admin/backups`）返回 404

**情况 3：`ImportJob` 特有的 force / rebase 字段**
- `force: bool = False` 推测设计用于"强制覆盖已有数据"
- `rebase: bool = False` 推测设计用于"重新映射 ID / 基线数据"
- 这两个字段在整个代码库中**无任何引用**（除 Schema 定义外）
- 当前 `BackupV2.restore()` 的行为相当于 `force=True`（无条件清空所有表后重新插入）和 `rebase=True`（全量替换数据目录）
- **边界风险**：如果未来要实现这两个开关，需要重构恢复流程以支持"非强制模式"（仅插入缺失数据）和"不 rebase 模式"（保留部分现有数据）

### 13.5 结论：当前 admin 链路未落地，旧线仍保留引用

| Schema | 后端路由使用 | 前端 Admin 页面使用 | 状态 |
|--------|------------|-------------------|------|
| `BackupOptions` | ❌ 未作为请求参数解析 | ❌ 传空对象，未传递选项字段 | 当前 admin 链路未落地；旧 `BackupAPI/use-backups.ts` 仍引用 |
| `CreateBackup` | ❌ 未作为请求参数解析 | ❌ 未传 tag/options/templates | 当前 admin 链路未落地；旧 `BackupAPI` 仍引用 |
| `ImportJob` | ❌ 未作为请求参数解析 | ❌ 前端未声明此类型变量 | 当前全链路仅见 Schema 定义，未发现调用 |

**设计意图推测**：这三个 Schema 原本是为了支持"选择性备份/恢复"功能（比如只备份食谱、不备份用户），以及带标签（tag）的备份管理。但当前实现走的是"全量备份/全量恢复"的简单路径，这些精细控制的接口尚未在后端路由中落地。

对应的前端代码也分为两条线：
- 新线（`AdminBackupsApi` + `backups.vue`）：简化实现，全量操作，实际运行
- 旧线（`BackupAPI` + `use-backups.ts`）：保留了选项参数传递逻辑，且引用的 `/api/backups/*` 端点在后端完全不存在

---

## 十四、关键设计要点

1. **备份非自动调度**：备份任务未注册到 SchedulerRegistry，需手动通过 API 触发
2. **SQLite 自动预备份**：恢复前自动创建数据库文件副本（PostgreSQL 暂未实现）
3. **Safari ZIP 兼容**：处理 macOS Safari 解压时的 `__MACOSX` 目录问题
4. **外键安全**：数据导入前后禁用/恢复外键约束，无效行自动清理
5. **类型重建**：JSON 中的字符串 UUID/日期/时间自动转换为数据库原生类型
6. **PostgreSQL 序列恢复**：手动恢复自增 ID 序列值
7. **分级异常策略**：修复/清理类异常忽略继续，核心流程异常终止并报告
8. **BackupSchemaMismatch 死代码**：异常已定义并捕获但从未抛出，schema 版本兼容由 Alembic 迁移机制实际处理
9. **上传即可见**：备份 ZIP 上传后直接写入 BACKUP_DIR，列表接口通过扫描 `*.zip` 使文件立即可用于恢复
10. **前后端两套遗留代码**：存在 `BackupAPI/use-backups.ts`（旧）和 `AdminBackupsApi/backups.vue`（新）两条实现线，当前实际使用后者
11. **BackupOptions/CreateBackup/ImportJob 未在当前 admin 链路落地**：后端 admin 路由不解析这些请求体；旧 `BackupAPI/use-backups.ts` 仍保留部分类型引用
12. **三级校验滞后风险**：上传仅校验扩展名，列表完全不校验，核心校验全部在恢复阶段且数据库清空早于多项数据校验
13. **下载五层安全防护**：路径越界校验 → JWT 签名防篡改 → 30 分钟过期 → 文件存在校验 → BACKUP_DIR/GROUPS_DIR 白名单限制
14. **旧版 API 端点完全缺失**：`BackupAPI` 引用的 6 个 `/api/backups/*` 端点在后端均不存在，存在 404 和选项静默丢弃的边界风险
