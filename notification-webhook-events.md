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

所有业务事件定义在 `mealie/services/event_bus_service/event_types.py` 的 `EventTypes` 枚举类中。

### 内部事件（不可通过 Apprise 偏好订阅）
| 事件名 | 说明 |
|--------|------|
| `test_message` | 测试消息，仅用于手动触发测试接口 |
| `webhook_task` | Webhook 定时调度任务，仅由 `post_group_webhooks` 派发 |

### 可订阅业务事件

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
4. **事件类型前置过滤**：`WebhookEventListener.get_subscribers()` 首先检查 `event.event_type == EventTypes.webhook_task`，非 webhook 任务事件直接返回空列表

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

### 4.1 统一发送入口：EventBusService.dispatch()

所有事件的派发入口是 `EventBusService.dispatch()`（`mealie/services/event_bus_service/event_bus_service.py`）：

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

`_publish_event()` 遍历所有监听器，按需调用：

```python
def _publish_event(self, event, group_id, household_id):
    for listener in self._get_listeners(group_id, household_id):
        if subscribers := listener.get_subscribers(event):
            listener.publish_to_subscribers(event, subscribers)
```

监听器列表固定为 `[AppriseEventListener, WebhookEventListener]`。

### 4.2 Webhook 定时触发流程

Webhook 采用**定时调度**模式，而非业务事件即时触发：

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

4. **监听器处理**：`WebhookEventListener`（`event_bus_listeners.py`）：
   - `get_subscribers()`：仅处理 `webhook_task` 事件，按时间窗口 + enabled + group/household 查询数据库
   - `publish_to_subscribers()`：对 `EventDocumentType.mealplan` 查询时间范围内的饮食计划填充到 `webhook_body`；若 `webhook_body` 非空，则调用 `WebhookPublisher.publish(event, [webhook.url, ...])`

### 4.3 Apprise 即时触发流程

Apprise 通知是**业务事件即时触发**模式，典型触发点：

- **用户注册**：`mealie/routes/users/registration.py` 注册成功后派发 `user_signup` 事件，附带 `EventUserSignupData`
- **CRUD 操作**：通过 `BaseUserController.dispatch_event()` 统一派发食谱、标签等实体的创建/更新/删除事件
- **所有 dispatch 均走 EventBusService.dispatch()**

`AppriseEventListener.publish_to_subscribers()` 直接把 URL 列表交给 `ApprisePublisher`。

### 4.4 手动测试入口

- **Webhook 测试**：`POST /households/webhooks/{item_id}/test` → `post_test_webhook(webhook, message)`，直接构造一个 `test_message` 事件并调用 `WebhookEventListener.publish_to_subscribers()`
- **Apprise 测试**：`POST /households/events/notifications/{item_id}/test` → 直接构造 `test_message` 事件并调用 `AppriseEventListener.publish_to_subscribers()`

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
| `enabled` | bool | 是否启用；`False` 时在 SQL 查询层面被完全过滤，永不触发 |
| `name` | str | 仅作显示名，不参与发送逻辑 |
| `url` | str | 直接作为 `requests.post()` 的目标 URL |
| `webhook_type` | WebhookType (StrEnum) | 当前仅定义了 `mealplan` 一个值；实际决定请求体内容的是 `EventWebhookData.document_type`，`WebhookEventListener.publish_to_subscribers()` 用 match 语句匹配该字段来决定是否查询饮食计划 |
| `scheduled_time` | time (UTC) | 核心调度参数；必须落在某次 `post_group_webhooks()` 执行窗口 `(start_dt, end_dt]` 内才会被选中；精度为时间（不含日期），即每天在该 UTC 时间触发一次 |

Webhook 偏好的 REST 接口位于 `mealie/routes/households/controller_webhooks.py`：
- `GET/POST /households/webhooks`
- `GET/PUT/DELETE /households/webhooks/{item_id}`
- `POST /households/webhooks/rerun` — 重跑当日全部窗口
- `POST /households/webhooks/{item_id}/test` — 发送测试请求

### 7.2 Apprise 通知用户偏好

Apprise 通知偏好定义在 `mealie/schema/household/group_events.py`，分为**通知器级**和**选项级**两层。

| 层级 | 字段 | 对发送的影响 |
|------|------|-------------|
| 通知器级 | `enabled` | 是否启用；`False` 时在查询层被过滤，该通知器下所有事件永不发送 |
| 通知器级 | `apprise_url` | Apprise 协议 URL，直接决定通知渠道（邮件、Slack、Telegram、Webhook 等）和认证凭据；该字段在对外 API 响应中始终被隐藏（`GroupEventNotifierOut` 不含，仅 `GroupEventNotifierPrivate` 含） |
| 选项级 `options` | `<event_type_name>: bool` | 与 `EventTypes` 枚举名一一对应的布尔字段，默认全部 `False`；通过 `getattr(notifier.options, event.event_type.name)` 动态读取，只有对应开关为 `True` 的事件才会推送到该通知器 |
| 选项级 `options` | `test_message` / `webhook_task` | 同样存在于 Options 中，但默认 `False`；主要用于手动测试接口，一般不会在真实通知中触发 |

核心过滤逻辑（`AppriseEventListener.get_subscribers()`）：
```python
urls = [notifier.apprise_url for notifier in notifiers
        if getattr(notifier.options, event.event_type.name)]
```

Apprise 通知偏好的 REST 接口位于 `mealie/routes/households/controller_group_notifications.py`：
- `GET/POST /households/events/notifications`
- `GET/PUT/DELETE /households/events/notifications/{item_id}`
- `POST /households/events/notifications/{item_id}/test`

### 7.3 两种通知方式的偏好对比

| 偏好维度 | Webhook | Apprise 通知 |
|---------|---------|-------------|
| 总开关 | `enabled`（Webhook 级） | `enabled`（通知器级） |
| 事件粒度过滤 | 无（只受时间窗口控制，只支持 mealplan） | 每种 `EventTypes` 独立开关（默认全关） |
| 目标地址 | 单一 `url` 字段 | `apprise_url`，由 Apprise 协议解析 |
| 触发时机偏好 | `scheduled_time`（每天某 UTC 时间） | 无（即时触发） |
| 请求内容偏好 | 由 `webhook_type` / `document_type` 隐式决定 | 自定义 URL 会自动注入事件元数据 query 参数 |
| 凭据/安全 | 无内置签名，依赖 URL 本身携带 | 依赖 `apprise_url` 内的凭据 |

---

## 八、完整调用流程图

```
                         业务代码 (用户注册 / CRUD / 定时任务)
                                       │
                                       ▼
                    EventBusService.dispatch(integration_id, group_id,
                         household_id, event_type, document_data)
                                       │
             ┌─────────────────────────┴─────────────────────────┐
             │                                                   │
  HTTP 请求上下文 (bg != None)                        其他上下文 (调度任务等)
             │                                                   │
  FastAPI BackgroundTasks.add_task (异步)          直接同步调用 _publish_event
             │                                                   │
             └─────────────────────┬─────────────────────────────┘
                                   ▼
          EventBusService._publish_event(event, group_id, household_id)
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   AppriseEventListener                       WebhookEventListener
              │                                         │
  get_subscribers(event)                    get_subscribers(event)
     │                                           │
     │  enabled=True AND                         │  event_type == webhook_task ?
     │  options.<event_type>=True                │      ├─ 否 → 返回 []
     │                                           │      └─ 是 → 查询 webhook_urls:
     ▼                                           │             enabled=True,
  publish_to_subscribers(event, urls)            │             scheduled_time in (start,end],
     │                                           │             group/household match
     ▼                                           │
  ApprisePublisher.publish()                     ▼
     │                                  publish_to_subscribers(event, webhooks)
     │  对每个 URL 添加 tag=event_id                   │
     │  apprise.notify(tag=tags)                       │  match document_type:
     │                                                 │    mealplan → 查询饮食计划 → webhook_body
     │                                                 ▼
     │                                     WebhookPublisher.publish()
     │                                                 │
     │                                                 │  requests.post(url, json=event, timeout=15)
     │                                                 │  - HTTP 非 2xx: 静默忽略 (默认)
     │                                                 │  - 网络异常/超时: 抛出 → 调度 wrapper 记日志
     │                                                 │  - 无重试
     │                                                 │  - 无签名
     ▼                                                 ▼
                       发送完成 (无持久化记录 / 无自动重试)
```
