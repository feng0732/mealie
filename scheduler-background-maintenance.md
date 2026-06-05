# 后台调度任务完整流程梳理

## 一、调度入口：应用启动时触发

### 1.1 启动时机
调度器在 FastAPI 应用的生命周期管理中启动。入口在 [app.py](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/app.py#L54-L95) 的 `lifespan_fn` 函数中：

```python
@asynccontextmanager
async def lifespan_fn(_: FastAPI) -> AsyncGenerator[None, None]:
    # 1. 数据库初始化
    init_db.main()
    
    # 2. 启动调度器（关键入口）
    await start_scheduler()
    
    # 3. 系统启动日志...
    
    yield  # 应用运行期间
    
    # 4. 应用关闭时的收尾（目前仅打日志）
    logger.info("-----SYSTEM SHUTDOWN----- \n")
```

### 1.2 启动流程
[start_scheduler()](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/app.py#L124-L144) 函数执行两步操作：

```python
async def start_scheduler():
    # 第一步：注册所有任务
    SchedulerRegistry.register_daily(...)      # 每日任务
    SchedulerRegistry.register_minutely(...)    # 每5分钟任务
    SchedulerRegistry.register_hourly(...)      # 每小时任务
    
    SchedulerRegistry.print_jobs()  # 打印已注册任务
    
    # 第二步：启动调度服务
    await SchedulerService.start()
```

---

## 二、任务注册表：SchedulerRegistry

[SchedulerRegistry](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/scheduler_registry.py) 是任务容器，维护三个任务列表：

| 调度频率 | 存储列表 | 注册方法 | 已注册任务 |
|---------|---------|---------|-----------|
| 每日 | `_daily` | `register_daily()` | `purge_expired_tokens` 清理过期分享令牌<br>`purge_group_registration` 清理未完成的组注册<br>`purge_password_reset_tokens` 清理过期密码重置令牌<br>`purge_group_data_exports` 清理过期数据导出<br>`create_mealplan_timeline_events` 创建餐计划时间线事件<br>`delete_old_checked_list_items` 删除旧的已勾选购物清单项 |
| 每小时 | `_hourly` | `register_hourly()` | `locked_user_reset` 重置锁定用户 |
| 每5分钟 | `_minutely` | `register_minutely()` | `post_group_webhooks` 发送群组 Webhook |

---

## 三、任务执行：三层调度机制

### 3.1 第一层：SchedulerService.start()
[SchedulerService.start()](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/scheduler_service.py#L20-L27) 启动三个调度循环：

```python
class SchedulerService:
    @staticmethod
    async def start():
        await run_minutely()   # 每5分钟
        await run_hourly()     # 每小时
        asyncio.create_task(schedule_daily())  # 每日（需计算首次触发时间）
```

### 3.2 第二层：@repeat_every 装饰器
[runner.py](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/runner.py) 中的 `@repeat_every` 装饰器是核心执行引擎：

```python
@repeat_every(minutes=MINUTES_DAY, wait_first=False, logger=logger)
def run_daily():
    for func in SchedulerRegistry._daily:
        _scheduled_task_wrapper(func)

@repeat_every(minutes=MINUTES_HOUR, wait_first=True, logger=logger)
def run_hourly(): ...

@repeat_every(minutes=MINUTES_5, wait_first=True, logger=logger)
def run_minutely(): ...
```

**装饰器工作原理**：
1. 被装饰的函数首次调用时，通过 `asyncio.ensure_future(loop())` 启动一个后台协程
2. 协程内部是一个 `while` 循环，执行逻辑：
   - 如果 `wait_first=True`，先 sleep 一个周期
   - 执行任务函数（同步函数用 `run_in_threadpool` 跑在线程池中）
   - 计数 +1
   - sleep 对应周期时间
   - 循环直到达到 `max_repetitions`（默认 None，永久循环）

### 3.3 第三层：每日任务的首次触发时间计算
每日任务比较特殊，不是立即启动，而是先计算到下一个 `DAILY_SCHEDULE_TIME_UTC` 的时间差：

[schedule_daily()](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/scheduler_service.py#L30-L53):

```python
async def schedule_daily():
    now = datetime.now(UTC)
    daily_schedule_time = get_app_settings().DAILY_SCHEDULE_TIME_UTC
    
    # 计算下一次触发时间点
    next_schedule = now.replace(
        hour=daily_schedule_time.hour, 
        minute=daily_schedule_time.minute, 
        second=0, microsecond=0
    )
    
    # 如果当前时间已过今天的调度点，推迟到明天
    delta = next_schedule - now
    if delta < timedelta(0):
        next_schedule = next_schedule + timedelta(days=1)
        delta = next_schedule - now
    
    # sleep 到目标时间，然后启动每日循环
    await asyncio.sleep(delta.total_seconds())
    await run_daily()  # 启动 @repeat_every 装饰的每日循环
```

---

## 四、任务执行细节

### 4.1 任务包装与执行
每个任务通过 `_scheduled_task_wrapper` 调用：

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()  # 直接调用，同步执行
    except Exception as e:
        logger.error("Error in scheduled task func='%s': exception='%s'", 
                    callable.__name__, e)
```

### 4.2 典型任务实现

以 [purge_expired_tokens()](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/tasks/purge_expired_share_tokens.py#L8-L19) 为例：

```python
def purge_expired_tokens() -> None:
    current_time = datetime.now(UTC)
    
    with session_context() as session:  # 数据库会话管理
        db = get_repositories(session, group_id=None)
        tokens_response = db.recipe_share_tokens.page_all(
            PaginationQuery(page=1, per_page=-1, 
                           query_filter=f"expiresAt < {current_time}")
        )
        if not (tokens := tokens_response.items):
            return
        db.recipe_share_tokens.delete_many([token.id for token in tokens])
```

另一个例子 [post_group_webhooks()](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/tasks/post_webhooks.py#L24-L79) 使用全局变量追踪状态：

```python
last_ran = datetime.now(UTC)  # 全局状态变量

def post_group_webhooks(start_dt: datetime | None = None, ...) -> None:
    global last_ran
    start_dt = start_dt or last_ran  # 从上一次运行时间开始查询
    last_ran = end_dt = datetime.now(UTC)  # 更新本次运行时间
    # ... 查询并发送 webhook
```

---

## 五、维护动作（收尾）

### 5.1 数据库会话管理
所有任务统一使用 `session_context` 上下文管理器：

```python
@contextmanager
def session_context() -> Generator[Session, None, None]:
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()  # 退出上下文时自动关闭会话
```

**关键特性**：
- 每个任务独立创建数据库会话
- 通过 `with` 语句确保会话在任务完成后自动关闭
- 异常情况下也能保证资源释放

### 5.2 任务间的状态维护
- `post_group_webhooks` 使用全局变量 `last_ran` 记录上次执行时间，避免重复发送
- 其他无状态任务（如清理类）每次独立执行

### 5.3 应用关闭时的收尾
目前 [lifespan_fn](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/app.py#L93-L95) 的 `yield` 之后仅打印日志，**没有显式停止调度任务的逻辑**：

```python
    yield
    logger.info("-----SYSTEM SHUTDOWN----- \n")
```

这意味着：
- 应用关闭时，正在运行的 `asyncio.sleep()` 会被取消
- 正在执行的任务函数（同步函数跑在线程池）可能会被中断
- `@repeat_every` 的 while 循环会随事件循环销毁而终止

---

## 六、异常处理：两层防护

### 第一层：@repeat_every 内部异常捕获
[runner.py](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/runner.py#L60-L77) 的 loop 函数中：

```python
try:
    if is_coroutine:
        await func()
    else:
        await run_in_threadpool(func)
    repetitions += 1
except Exception as exc:
    if logger is not None:
        formatted_exception = "".join(format_exception(type(exc), exc, exc.__traceback__))
        logger.error(formatted_exception)  # 打印完整堆栈
    if raise_exceptions:
        raise exc  # 默认 False，不会抛出
await asyncio.sleep(minutes * 60)  # 异常后继续等待下一次
```

**效果**：单个任务抛出异常不会终止调度循环，sleep 后继续下一次执行。

### 第二层：_scheduled_task_wrapper 异常捕获
[scheduler_service.py](file:///d:/fz/0601/solo-dogfeeding/code/35-mealie/mealie/services/scheduler/scheduler_service.py#L56-L61):

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.error("Error in scheduled task func='%s': exception='%s'", 
                    callable.__name__, e)
```

**效果**：单个任务失败不会影响同批次的其他任务执行。

### 异常处理总结
```
任务A执行 → 抛出异常 → wrapper 捕获打日志 → 继续执行任务B → ...
                                 ↓
                          @repeat_every 捕获
                                 ↓
                          打印完整堆栈
                                 ↓
                          sleep 等待下一轮
```

**关键特性**：
1. 单个任务失败 → 不影响同批次其他任务
2. 单个任务失败 → 不终止调度循环，下一轮继续尝试
3. 异常信息完整记录（函数名 + 异常信息 + 堆栈跟踪）

---

## 七、完整调用链时序图

```
应用启动
   ↓
lifespan_fn()
   ├─ init_db.main()          # 数据库初始化
   └─ start_scheduler()
         ├─ 注册所有任务到 SchedulerRegistry
         ├─ 打印任务列表
         └─ SchedulerService.start()
               ├─ run_minutely() → 启动 @repeat_every 循环
               ├─ run_hourly()   → 启动 @repeat_every 循环
               └─ schedule_daily()
                     ├─ 计算到 DAILY_SCHEDULE_TIME 的等待时间
                     ├─ asyncio.sleep(...)
                     └─ run_daily() → 启动 @repeat_every 循环

应用运行中...
   ↓
每5分钟触发: run_minutely() → 遍历 _minutely → wrapper(task)
每小时触发: run_hourly() → 遍历 _hourly → wrapper(task)  
每日触发: run_daily() → 遍历 _daily → wrapper(task)

应用关闭
   ↓
lifespan_fn yield 后执行
   ↓
logger.info("-----SYSTEM SHUTDOWN-----")
   ↓
事件循环销毁 → 所有后台协程终止
```

---

## 八、潜在问题与注意事项

1. **关闭时无优雅停机**：应用关闭时没有等待正在执行的任务完成，可能导致任务中断
2. **单 worker 限制**：tasks 模块注释中说明 "Scheduler object is only available to a single worker"，因此 uvicorn 配置为 `workers=1`
3. **全局状态**：`post_group_webhooks` 使用全局变量 `last_ran`，多实例部署时会有问题
4. **任务串行执行**：同批次任务按注册顺序串行执行，前一个任务慢会影响后一个
5. **无重试机制**：任务失败后仅打日志，不会重试，需等待下一个周期
