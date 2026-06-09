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

## 九、关键设计要点

1. **备份非自动调度**：备份任务未注册到 SchedulerRegistry，需手动通过 API 触发
2. **SQLite 自动预备份**：恢复前自动创建数据库文件副本（PostgreSQL 暂未实现）
3. **Safari ZIP 兼容**：处理 macOS Safari 解压时的 `__MACOSX` 目录问题
4. **外键安全**：数据导入前后禁用/恢复外键约束，无效行自动清理
5. **类型重建**：JSON 中的字符串 UUID/日期/时间自动转换为数据库原生类型
6. **PostgreSQL 序列恢复**：手动恢复自增 ID 序列值
7. **分级异常策略**：修复/清理类异常忽略继续，核心流程异常终止并报告
8. **BackupSchemaMismatch 死代码**：异常已定义并捕获但从未抛出，schema 版本兼容由 Alembic 迁移机制实际处理
