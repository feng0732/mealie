# 后台调度任务完整流程梳理

## 一、调度入口：应用启动时触发

### 1.1 启动时机
调度器在 FastAPI 应用的生命周期管理中启动。入口在 `app.py` 的 `lifespan_fn` 函数中：

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
`start_scheduler()` 函数执行两步操作：

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

`SchedulerRegistry` 是任务容器，维护三个任务列表：

| 调度频率 | 存储列表 | 注册方法 | 已注册任务 |
|---------|---------|---------|-----------|
| 每日 | `_daily` | `register_daily()` | `purge_expired_tokens` 清理过期分享令牌<br>`purge_group_registration` 清理未完成的组注册<br>`purge_password_reset_tokens` 清理过期密码重置令牌<br>`purge_group_data_exports` 清理过期数据导出<br>`create_mealplan_timeline_events` 创建餐计划时间线事件<br>`delete_old_checked_list_items` 删除旧的已勾选购物清单项 |
| 每小时 | `_hourly` | `register_hourly()` | `locked_user_reset` 重置锁定用户 |
| 每5分钟 | `_minutely` | `register_minutely()` | `post_group_webhooks` 发送群组 Webhook |

---

## 三、任务执行：三层调度机制

### 3.1 第一层：SchedulerService.start()
`SchedulerService.start()` 启动三个调度循环：

```python
class SchedulerService:
    @staticmethod
    async def start():
        await run_minutely()   # 每5分钟
        await run_hourly()     # 每小时
        asyncio.create_task(schedule_daily())  # 每日（需计算首次触发时间）
```

### 3.2 第二层：@repeat_every 装饰器
`runner.py` 中的 `@repeat_every` 装饰器是核心执行引擎：

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

**重要**：这个 wrapper 捕获异常后**没有重新抛出**，异常到此为止。

### 4.2 典型任务实现

以 `purge_expired_tokens()` 为例：

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

另一个例子 `post_group_webhooks()` 使用全局变量追踪状态：

```python
last_ran = datetime.now(UTC)  # 全局状态变量

def post_group_webhooks(start_dt: datetime | None = None, ...) -> None:
    global last_ran
    start_dt = start_dt or last_ran  # 从上一次运行时间开始查询
    last_ran = end_dt = datetime.now(UTC)  # 更新本次运行时间
    # ... 查询并发送 webhook
```

---

## 五、手动触发：Webhooks Rerun 入口

### 5.1 API 入口
手动触发 webhook 重投的 API 入口在 `controller_webhooks.py` 中：

```python
@router.post("/rerun")
def rerun_webhooks(self):
    """Manually re-fires all previously scheduled webhooks for today"""
    
    start_time = datetime.min.time()
    start_dt = datetime.combine(datetime.now(UTC).date(), start_time)
    post_group_webhooks(start_dt=start_dt, group_id=self.group.id, household_id=self.household.id)
```

### 5.2 调用流程

```
用户调用 POST /households/webhooks/rerun
    ↓
rerun_webhooks() 控制器方法
    ↓
构造 start_dt = 今日 00:00:00 UTC
    ↓
直接调用 post_group_webhooks(start_dt, group_id, household_id)
    ↓
post_group_webhooks() 执行
    ├─ 不使用 last_ran，使用传入的 start_dt
    ├─ 更新全局变量 last_ran = 当前时间
    ├─ 查询指定 group 和 household 的 mealplan 事件
    └─ 通过 EventBusService 分发 webhook 事件
```

### 5.3 关键特性

1. **同步执行**：直接调用任务函数，不经过调度器，API 响应会等待任务完成
2. **参数定制**：
   - `start_dt` 设为今日零点，重投今天所有 webhook
   - `group_id` 和 `household_id` 限定为当前用户所属组
3. **副作用**：更新全局变量 `last_ran`，会影响后续调度器触发的执行（下一次调度从当前时间开始查询）
4. **无异常处理**：控制器直接调用，没有 try-catch，如果任务抛出异常会返回 500 错误

### 5.4 与调度触发的区别

| 触发方式 | 调用路径 | start_dt | group_id | 异常处理 |
|---------|---------|----------|----------|---------|
| 调度触发 | run_minutely() → wrapper → post_group_webhooks() | last_ran（上一次运行时间） | 所有组 | wrapper 捕获，打日志 |
| 手动触发 | rerun_webhooks() → post_group_webhooks() | 今日 00:00:00 | 当前用户组 | 无捕获，抛出 500 |

---

## 六、注册生命周期与重复注册问题

### 6.1 SchedulerRegistry 注册机制

`SchedulerRegistry._register` 方法实现：

```python
@staticmethod
def _register(name: str, callbacks: list[Callable], callback: Iterable[Callable]):
    for cb in callback:
        logger.debug(f"Registering {name} callback: {cb.__name__}")
        callbacks.append(cb)  # 直接 append，无去重检查！
```

**问题**：没有去重逻辑，如果 `register_daily()` 等方法被多次调用，同一个任务会被多次添加到列表中。

### 6.2 重复注册场景

场景 1：`start_scheduler()` 被多次调用
```python
# 第一次调用
await start_scheduler()  # _daily = [task1, task2, ...]

# 第二次调用（例如热重载、测试代码重复调用）
await start_scheduler()  # _daily = [task1, task2, ..., task1, task2, ...]
```

场景 2：其他地方也调用了注册方法
```python
# app.py 中已注册
SchedulerRegistry.register_daily(task1, task2)

# 另一个模块中再次注册
SchedulerRegistry.register_daily(task1)  # task1 被添加第二次
```

### 6.3 重复执行的后果

每次调度触发时，`run_daily()` 会遍历整个列表：

```python
def run_daily():
    for func in SchedulerRegistry._daily:  # 列表中有重复项
        _scheduled_task_wrapper(func)      # 重复的任务会被执行多次
```

**影响**：
- 清理类任务（如 `purge_expired_tokens`）：多次执行无害但浪费资源
- 发送类任务（如 `post_group_webhooks`）：可能导致重复发送 webhook
- 状态修改类任务：可能导致数据不一致
- 性能影响：任务越多，调度周期被拉长越多

### 6.4 其他注册生命周期问题

1. **无注销机制**：应用运行中无法动态移除已注册的任务（虽然有 `remove_daily()` 方法，但没有被使用）
2. **注册时机**：必须在 `SchedulerService.start()` 之前注册，否则已启动的循环不会包含新任务
3. **列表非线程安全**：`_daily` 等是普通 list，并发注册可能有问题（虽然当前都是启动时同步注册）

---

## 七、维护动作（收尾）

### 7.1 数据库会话管理
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
- 异常情况下也能保证资源释放（finally 块执行）

### 7.2 任务间的状态维护
- `post_group_webhooks` 使用全局变量 `last_ran` 记录上次执行时间，避免重复发送
- 其他无状态任务（如清理类）每次独立执行

### 7.3 应用关闭时的收尾
目前 `lifespan_fn` 的 `yield` 之后仅打印日志，**没有显式停止调度任务的逻辑**：

```python
    yield
    logger.info("-----SYSTEM SHUTDOWN----- \n")
```

这意味着：
- 应用关闭时，事件循环开始销毁，正在运行的 `asyncio.sleep()` 会被取消
- 正在执行的任务函数（同步函数跑在线程池）可能会被中断
- `@repeat_every` 的 while 循环会随事件循环销毁而终止
- **没有优雅停机机制**，无法等待正在执行的任务完成

---

## 八、异常处理：关键纠正（只有一层在起作用！）

### 完整调用链与异常传播

```
@repeat_every 装饰器内部 loop()
    ↓
    try:
        run_daily()  ← 被装饰的原始函数（同步，跑在线程池）
            ↓
            for func in SchedulerRegistry._daily:
                _scheduled_task_wrapper(func)
                    ↓
                    try:
                        func()  ← 实际任务执行
                    except Exception as e:
                        logger.error("Error in scheduled task func='%s': exception='%s'", ...)
                        # 异常被捕获，没有重新抛出！
                        # 异常到此为止，不会传播到 for 循环之外
        repetitions += 1  ← 这行正常执行！
    except Exception as exc:
        # 【重要】这里几乎不会执行到！
        # 因为 _scheduled_task_wrapper 已经吞掉了所有任务异常
        # 只有当 run_daily() 本身（for 循环之外）抛出异常时才会走到这里
        logger.error(formatted_exception)  ← 完整堆栈打印代码实际是死代码
    ↓
    await asyncio.sleep(minutes * 60)  ← 正常进入下一次等待
```

### 8.1 单个任务失败时的日志来源

**日志只来自 `_scheduled_task_wrapper`**，格式为：
```
Error in scheduled task func='purge_expired_tokens': exception='...'
```

**注意**：
- 只有函数名和异常消息，**没有堆栈跟踪**
- `@repeat_every` 中打印完整堆栈的代码实际上不会被触发
- 这会导致调试困难，无法知道异常具体发生在哪一行

### 8.2 同一批次任务是否继续执行？

**继续执行**。原因：
- `_scheduled_task_wrapper` 捕获异常后没有重新抛出
- for 循环继续执行下一个迭代
- 同批次的其他任务不受影响

**示例场景**：
```
每日任务批次触发
    ↓
执行 purge_expired_tokens → 成功
    ↓
执行 purge_group_registration → 抛出异常
    ↓
    _scheduled_task_wrapper 捕获 → 打日志
    ↓
    for 循环继续 → 执行下一个任务
    ↓
执行 purge_password_reset_tokens → 成功
... 后续任务全部正常执行
```

### 8.3 下一次周期如何处理？

**下一次周期正常触发**。原因：
- 异常没有传播到 `run_daily()` 之外
- `run_daily()` 函数正常返回
- `@repeat_every` 的 try 块中 `repetitions += 1` 正常执行
- 然后执行 `await asyncio.sleep(minutes * 60)`
- 等待下一个周期后再次触发

**关键结论**：单个任务失败不会影响任何后续执行，既不会影响同批次其他任务，也不会影响下一次周期。

### 8.4 什么时候会触发 @repeat_every 的异常捕获？

只有以下极端情况才会走到 `@repeat_every` 的 except 块：
1. `_scheduled_task_wrapper` 函数本身抛出异常（例如 logger 出问题）
2. `run_daily()` 中 for 循环之外的代码抛出异常（目前只有 `logger.debug("Running daily callbacks")`）
3. `SchedulerRegistry._daily` 列表本身在遍历过程中被修改导致异常

这些情况在正常运行中几乎不会发生。

### 8.5 异常处理总结表

| 问题 | 答案 | 原因 |
|------|------|------|
| 日志来源 | `_scheduled_task_wrapper` | 异常被 wrapper 捕获，不向外传播 |
| 日志内容 | 函数名 + 异常消息，无堆栈 | wrapper 的 logger.error 格式决定 |
| 同批次其他任务 | 继续执行 | for 循环不受影响 |
| 下一次周期 | 正常触发 | `run_daily()` 正常返回，repetitions 正常计数 |
| `@repeat_every` 的 except 块 | 几乎不执行 | 异常已被内层 wrapper 吞掉 |
| 调度循环是否终止 | 不会 | 异常没有传播到 while 循环级别 |

---

## 九、任务包装器向外抛错的改进建议

### 9.1 问题分析

当前 `_scheduled_task_wrapper` 不向外抛错，导致：
- 异常无堆栈跟踪，调试困难
- `@repeat_every` 的异常处理逻辑成为死代码

但如果简单地让 wrapper 向外抛错：

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.error(...)
        raise  # 向外抛错
```

**后果**：异常会传播到 for 循环，导致循环终止，**同批次后续任务不再执行**。

```python
def run_daily():
    for func in SchedulerRegistry._daily:
        _scheduled_task_wrapper(func)  # 如果这里抛出异常
        # 后续任务不会执行！
```

### 9.2 改进方案对比

| 方案 | 堆栈信息 | 同批任务继续 | 外层捕获生效 | 复杂度 |
|------|---------|-------------|-------------|-------|
| 当前方案（不抛出） | ❌ 无 | ✅ 是 | ❌ 否 | 低 |
| 直接向外抛出 | ✅ 有 | ❌ 否 | ✅ 是 | 低 |
| logger.exception | ✅ 有 | ✅ 是 | ❌ 否 | 低 |
| 收集异常最后抛出 | ✅ 有 | ✅ 是 | ✅ 是 | 中 |
| 移除 wrapper，在 for 循环内捕获 | ✅ 有 | ✅ 是 | ❌ 否 | 低 |

### 9.3 推荐方案

#### 方案 A：使用 logger.exception（最简单，推荐）

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        # logger.exception 自动包含异常堆栈
        logger.exception("Error in scheduled task func='%s'", callable.__name__)
        # 不向外抛出，保证同批任务继续执行
```

**优点**：
- 保留完整堆栈跟踪，便于调试
- 同批次任务继续执行
- 改动最小，只需修改一行代码

**缺点**：
- `@repeat_every` 的异常处理逻辑仍然是死代码

#### 方案 B：收集所有异常，批量抛出（最完整）

修改 `run_daily()` 等函数，收集所有任务的异常：

```python
def run_daily():
    logger.debug("Running daily callbacks")
    exceptions = []
    
    for func in SchedulerRegistry._daily:
        try:
            func()  # 直接调用，不使用 wrapper
        except Exception as e:
            logger.exception("Error in scheduled task func='%s'", func.__name__)
            exceptions.append((func.__name__, e))
    
    # 所有任务执行完毕后，如果有异常，向外抛出
    if exceptions:
        error_msg = f"{len(exceptions)} tasks failed: " + \
                   ", ".join([f"{name}: {exc}" for name, exc in exceptions])
        raise RuntimeError(error_msg)
```

**优点**：
- 每个任务有完整堆栈
- 同批次任务全部执行
- 外层 `@repeat_every` 可以捕获到异常，进行统一处理
- 可以知道本次批次有多少任务失败

**缺点**：
- 改动较大，需要修改三个 `run_xxx` 函数
- 异常聚合后可能丢失原始异常类型

#### 方案 C：移除 wrapper，在 for 循环内捕获（简洁）

```python
def run_daily():
    logger.debug("Running daily callbacks")
    for func in SchedulerRegistry._daily:
        try:
            func()
        except Exception as e:
            logger.exception("Error in scheduled task func='%s'", func.__name__)
            # 不抛出，继续下一个任务
```

**优点**：
- 保留完整堆栈
- 同批次任务继续执行
- 减少一层函数调用，更简洁

**缺点**：
- 代码重复（三个 `run_xxx` 函数都要写 try-catch）
- `@repeat_every` 的异常处理逻辑仍然是死代码

### 9.4 最终建议

**短期方案**：采用方案 A，将 `logger.error` 改为 `logger.exception`，保留堆栈。

**长期方案**：如果需要 `@repeat_every` 的异常处理生效，采用方案 B，收集所有异常后批量抛出。

---

## 十、完整调用链时序图

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

手动触发: POST /households/webhooks/rerun
   ↓
rerun_webhooks() → 直接调用 post_group_webhooks(今日零点, 当前组)

应用关闭
   ↓
lifespan_fn yield 后执行
   ↓
logger.info("-----SYSTEM SHUTDOWN-----")
   ↓
事件循环销毁 → 所有后台协程终止
```

---

## 十一、潜在问题与代码缺陷

### 11.1 已确认的问题

1. **关闭时无优雅停机**：应用关闭时没有等待正在执行的任务完成，可能导致任务中断
2. **单 worker 限制**：tasks 模块注释中说明 "Scheduler object is only available to a single worker"，因此 uvicorn 配置为 `workers=1`
3. **全局状态**：`post_group_webhooks` 使用全局变量 `last_ran`，多实例部署时会有问题
4. **任务串行执行**：同批次任务按注册顺序串行执行，前一个任务慢会影响后一个
5. **无重试机制**：任务失败后仅打日志，不会重试，需等待下一个周期
6. **注册无去重**：`SchedulerRegistry._register` 直接 append，多次注册导致任务重复执行
7. **手动触发副作用**：`rerun_webhooks` 更新 `last_ran`，影响后续调度触发的查询范围

### 11.2 异常处理缺陷

8. **异常无堆栈跟踪**：`_scheduled_task_wrapper` 只用 `logger.error`，没有堆栈，调试困难
9. **异常处理代码冗余**：`@repeat_every` 中的完整堆栈打印代码实际上是死代码，不会被触发
10. **两层 try-catch 意图冲突**：设计上看似有两层防护，但内层 wrapper 吞掉异常导致外层失效
11. **异常静默**：任务失败后除了日志没有任何告警机制，问题可能长时间不被发现
12. **手动触发无异常处理**：`rerun_webhooks` 直接调用任务函数，异常会导致 API 返回 500

### 11.3 改进建议汇总

#### 注册去重
```python
@staticmethod
def _register(name: str, callbacks: list[Callable], callback: Iterable[Callable]):
    for cb in callback:
        if cb not in callbacks:  # 添加去重检查
            logger.debug(f"Registering {name} callback: {cb.__name__}")
            callbacks.append(cb)
        else:
            logger.debug(f"Skipping duplicate {name} callback: {cb.__name__}")
```

#### 异常处理（推荐方案 A）
```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.exception("Error in scheduled task func='%s'", callable.__name__)
```

#### 手动触发避免副作用
```python
@router.post("/rerun")
def rerun_webhooks(self):
    start_time = datetime.min.time()
    start_dt = datetime.combine(datetime.now(UTC).date(), start_time)
    # 传入一个临时变量，不更新全局 last_ran
    original_last_ran = post_group_webhooks.last_ran
    try:
        post_group_webhooks(start_dt=start_dt, group_id=self.group.id, 
                           household_id=self.household.id, update_last_ran=False)
    finally:
        post_group_webhooks.last_ran = original_last_ran
```
