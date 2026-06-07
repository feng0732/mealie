# Notification / Webhook 事件触发机制详解

## 一、整体架构概览

Mealie 的通知系统采用**事件总线（Event Bus）**架构，由以下核心组件构成：

- **事件类型定义**：统一定义所有可触发的业务事件
- **事件总线服务** (`EventBusService`)：负责事件的接收与分发
- **事件监听器** (`EventListenerBase` 子类)：负责获取订阅者并发布通知
  - `WebhookEventListener`：处理 Webhook 定时推送
  - `AppriseEventListener`：处理基于 Apprise 的外部通知
- **发布者** (`PublisherLike`)：执行实际的 HTTP 请求发送
  - `WebhookPublisher`：向 Webhook URL 发送 POST 请求
  - `ApprisePublisher`：通过 Apprise 库发送多渠道通知
- **调度器** (`SchedulerService`)：定时触发 webhook 任务

关键文件（仓库相对路径）：

```
mealie/services/event_bus_service/event_types.py          # 事件类型、Event 数据结构
mealie/services/event_bus_service/event_bus_service.py    # EventBusService 事件分发
mealie/services/event_bus_service/event_bus_listeners.py  # WebhookEventListener、AppriseEventListener
mealie/services/event_bus_service/publisher.py            # WebhookPublisher、ApprisePublisher
mealie/services/scheduler/scheduler_service.py            # SchedulerService 及调度循环
mealie/services/scheduler/scheduler_registry.py           # 调度任务注册表
mealie/services/scheduler/tasks/post_webhooks.py          # Webhook 定时任务入口
mealie/schema/household/webhook.py                        # Webhook Schema
mealie/schema/household/group_events.py                   # 通知器偏好 Schema
mealie/db/models/household/webhooks.py                    # GroupWebhooksModel
mealie/db/models/household/events.py                      # GroupEventNotifierModel / OptionsModel
mealie/routes/households/controller_webhooks.py           # Webhook REST 接口
mealie/routes/households/controller_group_notifications.py # Apprise 通知器 REST 接口
mealie/routes/users/registration.py                       # 用户注册事件触发点示例
mealie/app.py                                              # 调度任务注册
```

---

## 二、业务事件类型定义

所有事件定义在 `mealie/services/event_bus_service/event_types.py` 的 `EventTypes` 枚举类中，**共计 27 个枚举值**，按用途分为两类：
- **内部事件**：`test_message`、`webhook_task` — 共 **2 个**，由系统内部或测试接口使用
- **可订阅业务事件**：其余 `recipe_created` 等 — 共 **25 个**，面向普通用户，可通过 Apprise 通知器偏好配置订阅

### 内部事件（Schema/数据库中有对应 options 字段，但不作为普通用户可订阅的业务事件）

`EventTypes` 枚举与 `GroupEventNotifierOptions` Schema/`group_events_notifier_options` 数据库表是一一对齐生成的，因此 `test_message` 和 `webhook_task` 在 options 中确实存在布尔字段，默认均为 `False`。

但这两个事件在真实业务中**不作为普通用户订阅事件使用**：
- 前端 UI（`frontend/app/pages/household/notifiers.vue` 的 `optionsSections`）只暴露了食谱、用户、饮食计划等 8 类业务事件的开关，**完全没有呈现 `test_message` 和 `webhook_task` 的选项**，普通用户无法在界面上开启。
- 只有绕过前端直接调用 API 或修改数据库，才能把它们设为 `True`；即便如此，这两个事件的触发频率和语义也不适合作为日常通知。

| 事件名 | 实际用途 | 触发方式 |
|--------|---------|---------|
| `test_message` | 手动测试接口专用事件，仅用来验证某个 Webhook 或 Apprise 通知器的连通性 | 仅由两个 `/test` REST 接口派发（见下文"三类触发入口的边界"） |
| `webhook_task` | Webhook 定时调度系统的内部事件，用来把"某个时间窗口到了"这一信号派发给 WebhookEventListener | 仅由定时任务 `post_group_webhooks` 每 5 分钟派发一次 |

### 可订阅业务事件（前端 UI 呈现、普通用户可配置）

**食谱相关：**
- `recipe_created` / `recipe_updated` / `recipe_deleted`

**用户相关：**
- `user_signup`

**数据管理相关：**
- `data_migrations` / `data_export` / `data_import`

**饮食计划相关：**
- `mealplan_entry_created` / `mealplan_entry_updated` / `mealplan_entry_deleted`

**购物清单相关：**
- `shopping_list_created` / `shopping_list_updated` / `shopping_list_deleted`

**食谱集相关：**
- `cookbook_created` / `cookbook_updated` / `cookbook_deleted`

**标签 / 分类 / 标记：**
- `tag_created` / `tag_updated` / `tag_deleted`
- `category_created` / `category_updated` / `category_deleted`
- `label_created` / `label_updated` / `label_deleted`

### 事件数据结构

事件消息统一封装为 `Event` 类（`event_types.py`），在构造时自动生成 `event_id`（UUID）和 `timestamp`（UTC 时间）：

```python
class Event(MealieModel):
    message: EventBusMessage              # 标题和正文
    event_type: EventTypes                # 事件类型枚举
    integration_id: str                   # 触发来源标识（如 "registration"、"mealie_generic_user"）
    document_data: EventDocumentDataBase  # 业务数据（多态）
    event_id: UUID4 | None = None         # 实例化时自动生成
    timestamp: datetime | None = None     # 实例化时自动生成
```

不同事件类型携带不同的 `document_data`（均继承自 `EventDocumentDataBase`，带有 `document_type` 和 `operation` 字段），例如：
- `EventUserSignupData`：用户名、邮箱
- `EventRecipeData`：食谱 slug
- `EventMealplanData`：饮食计划 ID、日期、食谱信息
- `EventWebhookData`：Webhook 时间窗口 `webhook_start_dt` / `webhook_end_dt` 及待发送的 `webhook_body`

---

## 三、订阅过滤机制

系统存在两种完全独立的订阅机制：**Webhook 定时订阅** 和 **Apprise 事件通知订阅**。

### 3.1 Webhook 订阅过滤

Webhook 订阅存储在 `webhook_urls` 表中，对应 `mealie/db/models/household/webhooks.py` 的 `GroupWebhooksModel`。

过滤逻辑位于 `WebhookEventListener.get_scheduled_webhooks()`（`mealie/services/event_bus_service/event_bus_listeners.py`）：

```python
stmt = select(GroupWebhooksModel).where(
    GroupWebhooksModel.enabled == True,
    GroupWebhooksModel.scheduled_time > start_dt.astimezone(UTC).time(),
    GroupWebhooksModel.scheduled_time <= end_dt.astimezone(UTC).time(),
    GroupWebhooksModel.group_id == self.group_id,
    GroupWebhooksModel.household_id == self.household_id,
)
```

过滤维度：
1. **启用状态**：`enabled` 必须为 `True`
2. **时间窗口**：`scheduled_time` 必须落在 `(start_dt, end_dt]` 区间内（**左开右闭**）
3. **组织隔离**：`group_id` 和 `household_id` 必须匹配当前监听器上下文
4. **事件类型前置过滤**：`WebhookEventListener.get_subscribers()` 首先检查 `event.event_type == EventTypes.webhook_task`，非 `webhook_task` 事件直接返回空列表。这意味着：
   - 只有定时任务派发的 `webhook_task` 事件能通过 `get_subscribers()` 查到 Webhook 订阅者
   - `test_message` 以及所有业务事件（`recipe_created` 等）经过 WebhookEventListener 时均不会匹配任何 Webhook
   - 测试 Webhook 连通性时会**绕过 `get_subscribers()`**（直接传入目标 webhook），不受此限制

### 3.2 Apprise 通知订阅过滤

Apprise 通知订阅存储在两张关联表中：
- `group_events_notifiers`：通知器基本信息（名称、`apprise_url`、启用状态、所属 group/household）
- `group_events_notifier_options`：每种事件类型的订阅开关（与通知器一对一关联）

过滤逻辑位于 `AppriseEventListener.get_subscribers()`（`event_bus_listeners.py`）：

```python
notifiers = repos.group_event_notifier.multi_query(
    {"enabled": True}, override_schema=GroupEventNotifierPrivate
)
urls = [notifier.apprise_url for notifier in notifiers if getattr(notifier.options, event.event_type.name)]
```

过滤维度：
1. **启用状态**：通知器本身 `enabled == True`
2. **事件偏好开关**：`notifier.options.<event_type_name>` 必须为 `True`（通过 `getattr` 动态反射读取，与 `EventTypes` 枚举名一一对应）
3. **组织隔离**：通过 `repos` 构造时传入的 `group_id` / `household_id` 作用域隐式过滤

此外，对于 `form` / `forms` / `json` / `jsons` / `xml` / `xmls` 开头的"自定义 URL"，`AppriseEventListener.update_urls_with_event_data()` 会把事件元数据（`event_type`、`integration_id`、`document_data`、`event_id`、`timestamp`）以 `:<key>=<value>` 的形式追加到 URL query 参数中，注入到 Apprise 的自定义 payload 里。

---

## 四、发送入口与调用流程

### 三类触发入口的边界

系统中共有三条完全不同的事件派发路径，分别对应不同的使用场景、事件类型和过滤逻辑。**三者不可混用**，否则会出现"事件发出了但无人接收"的情况。

| 入口类型 | 触发方式 | 事件类型 | 是否走 `EventBusService.dispatch()` | 是否走 `listener.get_subscribers()` 过滤 | 目标接收方 |
|---------|---------|---------|-------------------------------------|------------------------------------------|-----------|
| **手动测试入口** | 用户点击 Web/API 的 Test 按钮 | `test_message` | **否**（直接构造 Event 并调用 `publish_to_subscribers`） | **否**（由调用方直接传入目标 webhook / apprise_url） | 仅当前被测试的单个 Webhook 或通知器 |
| **定时任务入口** | 调度器每 5 分钟自动执行 | `webhook_task` | 是 | 是（WebhookEventListener 按时间窗口+enabled 过滤） | 所有命中调度窗口的 Webhook；Apprise 端几乎不会收到（UI 不暴露该选项） |
| **业务事件入口** | 正常业务操作（CRUD、用户注册等） | 其余 25 种业务事件（`recipe_created` 等） | 是 | 是（AppriseEventListener 按 options 开关过滤） | 所有开启了对应事件偏好的 Apprise 通知器；Webhook 端**不会**收到 |

下文分别详述每条路径。

---

### 4.1 统一事件分发内核：EventBusService

`EventBusService` 是定时任务入口和业务事件入口的共同内核，手动测试入口不经过它。

#### dispatch() 方法

`EventBusService.dispatch()`（`mealie/services/event_bus_service/event_bus_service.py`）：

```python
def dispatch(self, integration_id, group_id, household_id, event_type, document_data, message=""):
    event = Event(...)  # 自动生成 event_id 和 timestamp

    if not household_id:
        # 查询该 group 下所有 household，广播到每个 household
        household_ids = [...]
    else:
        household_ids = [household_id]

    for household_id in household_ids:
        if self.bg:
            # FastAPI 请求上下文中，放入 BackgroundTasks 异步发送
            self.bg.add_task(self._publish_event, event=event, ...)
        else:
            # 调度任务等无请求上下文场景，同步发送
            self._publish_event(event, group_id, household_id)
```

#### _publish_event() 方法

遍历所有监听器，按需调用：

```python
def _publish_event(self, event, group_id, household_id):
    for listener in self._get_listeners(group_id, household_id):
        if subscribers := listener.get_subscribers(event):
            listener.publish_to_subscribers(event, subscribers)
```

监听器列表固定为 `[AppriseEventListener, WebhookEventListener]`，两个监听器各自独立判断是否有订阅者。

---

### 4.2 定时任务入口（webhook_task）

Webhook 的真实业务发送走**定时调度**模式，而非业务事件即时触发：

1. **调度注册**：在 `mealie/app.py` 的 `start_scheduler()` 中将 `post_group_webhooks` 注册为**每 5 分钟**执行一次：
   ```python
   SchedulerRegistry.register_minutely(tasks.post_group_webhooks)
   ```

2. **调度器执行**：`SchedulerService` 通过 `@repeat_every(minutes=5)` 装饰的 `run_minutely()` 每 5 分钟遍历 `SchedulerRegistry._minutely` 中的回调。每个回调由 `_scheduled_task_wrapper` 包裹，确保异常不会中断调度循环。

3. **任务入口**：`post_group_webhooks()`（`mealie/services/scheduler/tasks/post_webhooks.py`）：
   - 维护模块级全局变量 `last_ran` 记录上次运行时间
   - 计算本次时间窗口：`start_dt = last_ran`，`end_dt = datetime.now(UTC)`，然后 `last_ran = end_dt`
   - 遍历所有 group → 遍历该 group 的所有 household，对每个 household 创建 `EventBusService` 并派发一个 `EventTypes.webhook_task` 事件
   - 事件的 `document_data` 为 `EventWebhookData`，携带 `document_type=mealplan` 和本次时间窗口

4. **监听器处理**：
   - **WebhookEventListener**：`get_subscribers()` 仅处理 `webhook_task` 事件，按时间窗口 + enabled + group/household 查询数据库；`publish_to_subscribers()` 对 `EventDocumentType.mealplan` 查询时间范围内的饮食计划填充到 `webhook_body`，若 `webhook_body` 非空则调用 `WebhookPublisher.publish()`
   - **AppriseEventListener**：理论上如果有人绕过 UI 手动把 `options.webhook_task = True`，也会收到该事件（每 5 分钟一次）；但前端 UI 不暴露此选项，不作为用户可配置功能

---

### 4.3 业务事件入口（用户可配置通知）

Apprise 通知是**业务事件即时触发**模式，由正常业务操作驱动：

**典型触发点：**
- **用户注册**：`mealie/routes/users/registration.py` 注册成功后派发 `user_signup` 事件，附带 `EventUserSignupData`
- **CRUD 操作**：通过 `BaseUserController.dispatch_event()` 统一派发食谱、标签、分类、购物清单等实体的创建/更新/删除事件

**分发路径：**
- 所有业务事件均走 `EventBusService.dispatch()` → `_publish_event()`
- **AppriseEventListener**：`get_subscribers()` 查询 `enabled=True` 的通知器，并按 `getattr(notifier.options, event.event_type.name)` 动态过滤事件偏好开关；`publish_to_subscribers()` 直接把 URL 列表交给 `ApprisePublisher`
- **WebhookEventListener**：`get_subscribers()` 对非 `webhook_task` 的事件一律返回空列表，因此业务事件**不会触发任何 Webhook**

---

### 4.4 手动测试入口（test_message）

两个测试接口的共同特点是：**完全绕过 `EventBusService.dispatch()` 和 `listener.get_subscribers()`**，由调用方直接指定目标接收方。

#### Webhook 测试接口

`POST /households/webhooks/{item_id}/test` → `post_test_webhook(webhook, message)`（`mealie/services/scheduler/tasks/post_webhooks.py`）：

```python
def post_test_webhook(webhook: ReadWebhook, message: str = "") -> None:
    event = Event(
        message=EventBusMessage.from_type(EventTypes.test_message, body=message),
        event_type=EventTypes.test_message,
        integration_id=INTERNAL_INTEGRATION_ID,
        document_data=EventWebhookData(document_type=EventDocumentType.generic, ...),
    )
    listener = WebhookEventListener(webhook.group_id, webhook.household_id)
    listener.publish_to_subscribers(event, [webhook])  # 直接传入目标 webhook
```

关键点：
- 事件类型为 `test_message`，`document_type=generic`
- **不调用 `get_subscribers()`**，因此不检查 `enabled`、不看 `scheduled_time`、不看 `event_type` 是否匹配；哪怕该 webhook 是禁用状态，也会发出测试请求
- 仅发送给 URL 路径中指定的那一个 webhook

#### Apprise 测试接口

`POST /households/events/notifications/{item_id}/test`（`mealie/routes/households/controller_group_notifications.py`）：

```python
def test_notification(self, item_id: UUID4):
    item = self.repo.get_one(item_id, override_schema=GroupEventNotifierPrivate)
    test_event = Event(
        message=EventBusMessage.from_type(EventTypes.test_message, "test message"),
        event_type=EventTypes.test_message,
        integration_id="test_event",
        document_data=EventDocumentDataBase(document_type=EventDocumentType.generic, ...),
    )
    test_listener = AppriseEventListener(self.group_id, self.household_id)
    test_listener.publish_to_subscribers(test_event, [item.apprise_url])  # 直接传入目标 URL
```

关键点同样是绕过订阅过滤，不检查通知器的 `enabled` 状态，也不检查 `options.test_message` 是否为 `True`。

---

## 五、失败处理与重复发送防护

### 5.1 失败处理

#### WebhookPublisher 的分层失败行为

`WebhookPublisher.publish()` 位于 `mealie/services/event_bus_service/publisher.py`：

```python
def publish(self, event: Event, notification_urls: list[str]):
    event_payload = jsonable_encoder(event)
    for url in notification_urls:
        r = requests.post(url, json=event_payload, timeout=15)
        if self.hard_fail:
            r.raise_for_status()
```

按失败类型区分：

| 失败类型 | `hard_fail=False`（默认） | `hard_fail=True`（仅测试） |
|---------|-------------------------|--------------------------|
| **HTTP 非 2xx 响应**（4xx / 5xx） | `requests.post()` 默认不会对状态码抛异常；代码未调用 `r.raise_for_status()` → **静默忽略**，继续下一个 URL | 调用 `r.raise_for_status()` → 抛出 `HTTPError` |
| **网络异常**（DNS 失败、连接拒绝、TLS 错误等） | `requests.post()` 直接抛出 `ConnectionError` 等 → **向上冒泡** | 同样向上冒泡 |
| **超时**（超过 15 秒） | `requests.post()` 抛出 `Timeout` → **向上冒泡** | 同样向上冒泡 |

默认场景下 `hard_fail=False`，Webhook 只对网络异常和超时抛异常，HTTP 业务错误码会被吞掉。

#### ApprisePublisher 的失败行为

`ApprisePublisher`（`publisher.py`）以 `async_mode=True` 初始化 Apprise，调用 `self.apprise.notify()` 后立即返回；所有异步发送的错误由 Apprise 库内部处理，不冒泡到调用方。仅当 `hard_fail=True` 时，添加 URL 失败才会抛出异常。

#### 调度任务级别的异常捕获

调度器对每个回调有统一包装（`mealie/services/scheduler/scheduler_service.py`）：

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.error("Error in scheduled task func='%s': exception='%s'", callable.__name__, e)
```

因此 Webhook 中冒泡的网络异常/超时最终只会记录一条 ERROR 日志，**不会中断后续调度**。

#### 关于自动重试

**整个通知链路没有任何自动重试机制**：
- `WebhookPublisher` 和 `ApprisePublisher` 内部均无 retry 逻辑
- 调度器不会对失败的批次进行回滚或重放（`post_group_webhooks` 的 `last_ran` 在执行开始即被推进到 `now`）
- 没有持久化的"待发送 / 失败"事件表
- Webhook 可以通过 `POST /households/webhooks/rerun` 手动重跑当日（从 00:00 起）的调度，但这不是自动行为

### 5.2 重复发送防护

#### Apprise 侧：基于 event_id 的 tag 去重

`ApprisePublisher.publish()` 中：

```python
tags = []
for dest in notification_urls:
    tag = str(event.event_id)   # 使用事件唯一 UUID 作 tag
    tags.append(tag)
    self.apprise.add(dest, tag=tag)

self.apprise.notify(title=..., body=..., tag=tags)
```

每个 URL 在添加时绑定当前事件的 `event_id` 作为 tag，`notify()` 按 tag 匹配目标，确保**同一个 `Event` 对象对同一个 URL 只触发一次**（在 Apprise 内部不去重的前提下）。

#### Webhook 侧：基于时间窗口的调度去重

`post_group_webhooks()` 通过模块级 `last_ran` 维护相邻两次执行的窗口：

```python
last_ran = datetime.now(UTC)

def post_group_webhooks(start_dt=None, ...):
    global last_ran
    start_dt = start_dt or last_ran
    last_ran = end_dt = datetime.now(UTC)
    # SQL: scheduled_time > start_dt.time() AND scheduled_time <= end_dt.time()
```

- 正常运行时，相邻窗口 `(start_dt, end_dt]` 是**连续且不重叠**的，配合左开右闭查询，理论上每个 `scheduled_time` 在服务不重启的前提下**只会命中一次**
- 如果服务重启，`last_ran` 被重置为启动时间，重启期间错过的窗口将被跳过（可调用 `/households/webhooks/rerun` 手动补跑当日）
- **HTTP 请求层面没有幂等性保护**：同一个 Webhook URL 如果因并发或手动重跑而被多次命中，会收到多个请求。接收方如需去重，可使用 `event.event_id` 字段自行判断

---

## 六、签名校验实现

### 核准结论：当前版本没有实现签名校验

经过对以下文件的逐行审查：
- `mealie/services/event_bus_service/publisher.py` — `WebhookPublisher` 和 `ApprisePublisher` 的完整实现
- `mealie/db/models/household/webhooks.py` — `GroupWebhooksModel` 的全部字段
- `mealie/schema/household/webhook.py` — Webhook 入参 / 出参 Schema

最终确认：**当前代码中不存在任何 Webhook 或 Apprise 通知的签名 / HMAC 校验机制。**

#### 证据摘要

1. `WebhookPublisher.publish()` 只做一件事：
   ```python
   r = requests.post(url, json=event_payload, timeout=15)
   ```
   没有构造 `X-Signature`、`X-Mealie-Signature` 等请求头，也没有对 body 做哈希。请求头仅包含 `requests` 默认的 `Content-Type: application/json` 等标准头。

2. `GroupWebhooksModel` 中没有 `secret`、`api_key`、`signing_key` 或任何类似字段；Schema `CreateWebhook` / `SaveWebhook` 也无对应字段，意味着用户无法在创建 Webhook 时配置签名密钥。

3. `event_bus_service.py` 中虽然定义了 `ALGORITHM = "HS256"` 常量，但该常量在整个文件及其他文件中均未被引用，属于未使用的死代码，不代表签名功能已经落地。

4. Apprise 通知同样不做签名，其认证信息完全由用户在 `apprise_url` 里自行配置（例如 `json://token@host/path`）。

### 接收方可利用的替代校验字段

尽管没有官方签名，Webhook 接收方仍可利用以下字段做基础校验：
- `event.event_id`：每个事件唯一的 UUID，可用于幂等判断
- `event.timestamp`：事件创建时间，可用于判断请求时效性（防重放）
- `event.integration_id`：事件来源标识
- 在 Webhook URL 中自行携带 secret query 参数（例如 `https://example.com/hook?secret=xxx`），由接收方校验

---

## 七、用户偏好对外部通知的影响

### 7.1 Webhook 用户偏好

Webhook 的 Schema 定义在 `mealie/schema/household/webhook.py`，对通知发送的影响如下：

| 字段 | 类型 | 对发送的影响 |
|------|------|-------------|
| `enabled` | bool | 是否启用；`False` 时在 SQL 查询层面被完全过滤，正常调度永不触发（但测试接口绕过此检查） |
| `name` | str | 仅作显示名，不参与发送逻辑 |
| `url` | str | 直接作为 `requests.post()` 的目标 URL |
| `webhook_type` | WebhookType (StrEnum) | 当前仅定义了 `mealplan` 一个值；实际决定请求体内容的是 `EventWebhookData.document_type`，`WebhookEventListener.publish_to_subscribers()` 用 match 语句匹配该字段来决定是否查询饮食计划 |
| `scheduled_time` | time (UTC) | 核心调度参数；必须落在某次 `post_group_webhooks()` 执行窗口 `(start_dt, end_dt]` 内才会被选中；精度为时间（不含日期），即每天在该 UTC 时间触发一次（测试接口不检查此字段） |

Webhook 偏好的 REST 接口位于 `mealie/routes/households/controller_webhooks.py`：
- `GET/POST /households/webhooks`
- `GET/PUT/DELETE /households/webhooks/{item_id}`
- `POST /households/webhooks/rerun` — 重跑当日全部窗口
- `POST /households/webhooks/{item_id}/test` — 发送测试请求

### 7.2 Apprise 通知用户偏好

Apprise 通知偏好定义在 `mealie/schema/household/group_events.py`，分为**通知器级**和**选项级**两层。

| 层级 | 字段 | 对发送的影响 |
|------|------|-------------|
| 通知器级 | `enabled` | 是否启用；`False` 时在查询层被过滤，该通知器下所有事件永不发送（但测试接口绕过此检查） |
| 通知器级 | `apprise_url` | Apprise 协议 URL，直接决定通知渠道（邮件、Slack、Telegram、Webhook 等）和认证凭据；该字段在对外 API 响应中始终被隐藏（`GroupEventNotifierOut` 不含，仅 `GroupEventNotifierPrivate` 含） |
| 选项级 `options` | `<业务事件名>: bool` | 与 `recipe_created`、`user_signup` 等**业务事件枚举名一一对应**的布尔字段，默认全部 `False`；前端 UI（`frontend/app/pages/household/notifiers.vue`）以分组复选框形式呈现给用户；通过 `getattr(notifier.options, event.event_type.name)` 动态读取，只有对应开关为 `True` 的事件才会推送到该通知器 |
| 选项级 `options` | `test_message` / `webhook_task` | 因 Schema 与 `EventTypes` 枚举一一对齐而存在的字段，默认 `False`；**前端 UI 未提供勾选入口**，普通用户无法在界面中开启；直接操作 API/数据库可开启但无实际业务意义：`test_message` 仅由测试接口派发且会绕过 options 过滤，`webhook_task` 由调度器每 5 分钟派发一次（会导致过于频繁的通知） |

核心过滤逻辑（`AppriseEventListener.get_subscribers()`）：
```python
urls = [notifier.apprise_url for notifier in notifiers
        if getattr(notifier.options, event.event_type.name)]
```

**注意**：以上过滤仅对业务事件入口和定时任务入口生效；手动测试入口直接传入目标 URL，**不检查 `enabled` 和 `options` 开关**。

Apprise 通知偏好的 REST 接口位于 `mealie/routes/households/controller_group_notifications.py`：
- `GET/POST /households/events/notifications`
- `GET/PUT/DELETE /households/events/notifications/{item_id}`
- `POST /households/events/notifications/{item_id}/test` — 测试接口，绕过订阅过滤

### 7.3 两种通知方式的偏好对比

| 偏好维度 | Webhook | Apprise 通知 |
|---------|---------|-------------|
| 总开关 | `enabled`（Webhook 级；测试接口绕过） | `enabled`（通知器级；测试接口绕过） |
| 事件粒度过滤 | 不按事件类型过滤；仅由**调度时间窗口**隐式控制；所有业务事件均不会触发 Webhook | 每种业务事件独立开关（默认全关；前端 UI 仅呈现业务事件，不呈现 test_message/webhook_task） |
| 目标地址 | 单一 `url` 字段 | `apprise_url`，由 Apprise 协议解析 |
| 触发时机偏好 | `scheduled_time`（每天某 UTC 时间；测试接口不检查） | 无（即时触发） |
| 请求内容偏好 | 由 `webhook_type` / `document_type` 隐式决定（当前仅 mealplan） | 自定义 URL 会自动注入事件元数据 query 参数 |
| 凭据/安全 | 无内置签名，依赖 URL 本身携带 | 依赖 `apprise_url` 内的凭据 |

---

## 八、完整调用流程图

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                    三类触发入口                                         │
├──────────────────────────────┬──────────────────────────────┬──────────────────────────┤
│   手动测试入口               │   定时任务入口               │   业务事件入口           │
│   POST .../{item_id}/test    │   调度器每 5 分钟            │   用户注册 / CRUD 等     │
│   (事件类型: test_message)   │   (事件类型: webhook_task)   │   (recipe_created 等)   │
└──────────────┬───────────────┴──────────────┬───────────────┴──────────┬───────────────┘
               │                              │                         │
               │  绕过 dispatch()             │ 走 EventBusService       │ 走 EventBusService
               │  绕过 get_subscribers()      │  .dispatch()             │  .dispatch()
               │  直接指定目标                │                         │
               ▼                              ▼                         ▼
  直接构造 Event(test_message)      EventBusService.dispatch()   EventBusService.dispatch()
               │                              │                         │
               │                     ┌────────┴─────────┐     ┌───────┴────────┐
               │                     │ HTTP 上下文?      │     │ HTTP 上下文?   │
               │                     ├────────┬─────────┤     ├───────┬────────┤
               │                     │ 是     │ 否      │     │ 是    │ 否     │
               │                     ▼        ▼         │     ▼       ▼        │
               │               Background   同步调用     │  Background  同步调用  │
               │               Tasks                  │     Tasks              │
               │                     │                  │        │               │
               │                     └────────┬─────────┘        └───────┬───────┘
               │                              │                         │
               │                              ▼                         ▼
               │                  EventBusService._publish_event()      │
               │                              │                         │
               │              ┌───────────────┴───────────────┐         │
               │              ▼                               ▼         │
               │   AppriseEventListener              WebhookEventListener│
               │      (几乎不会命中，                     │               │
               │       UI 不暴露选项)                     │               │
               │              │                               │         │
               │    get_subscribers(event)         get_subscribers(event)│
               │      │                                      │           │
               │      │ enabled=True AND               │ event_type ==  │
               │      │ options.<event_type>=True      │ webhook_task?  │
               │      │                                      │           │
               │      ▼                                      ▼           │
               │   publish_to_subscribers()          ├─ 否 → 返回 []    │
               │      │                               └─ 是 → 查询      │
               │      │                                      │           │
               │      │                               enabled=True,    │
               │      │                               scheduled_time    │
               │      │                               in (start,end],   │
               │      │                               group/household   │
               │      │                                      │           │
               │      ▼                                      ▼           │
               │   ApprisePublisher.publish()       publish_to_subscribers()
               │      │                                      │           │
               │      │  tag=event_id 去重                   │ match doc_type:
               │      │  apprise.notify()                    │  mealplan →
               │      │                                      │  查询饮食计划
               │      │                                      ▼           │
               │      │                            WebhookPublisher.publish()
               │      │                                      │           │
               │      │                                      │ requests.post()
               │      │                                      │ - HTTP 非 2xx: 静默
               │      │                                      │ - 网络异常/超时:
               │      │                                      │   抛给调度 wrapper
               │      │                                      │ - 无重试  无签名
               │      ▼                                      ▼           │
               └──────┴──────────────────────────────────────┴───────────┘
                                      │
                                      ▼
                     发送完成（无持久化记录 / 无自动重试）
```
