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

关键文件目录：

```
mealie/services/event_bus_service/        # 事件总线核心
mealie/services/scheduler/                # 定时调度服务
mealie/schema/household/webhook.py        # Webhook Schema
mealie/schema/household/group_events.py   # 通知偏好 Schema
mealie/db/models/household/webhooks.py    # Webhook 数据库模型
mealie/db/models/household/events.py      # 通知器数据库模型
mealie/routes/households/controller_webhooks.py
mealie/routes/households/controller_group_notifications.py
```

---

## 二、业务事件类型定义

所有业务事件定义在 [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_types.py#L13-L60) 的 `EventTypes` 枚举类中：

### 内部事件（不可订阅）
| 事件名 | 说明 |
|--------|------|
| `test_message` | 测试消息，用于手动触发测试 |
| `webhook_task` | Webhook 定时调度任务 |

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

**标签/分类/标记：**
- `tag_created` / `tag_updated` / `tag_deleted`
- `category_created` / `category_updated` / `category_deleted`
- `label_created` / `label_updated` / `label_deleted`

### 事件数据结构

事件消息统一封装为 `Event` 类 ([event_types.py:194-207](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_types.py#L194-L207))：

```python
class Event(MealieModel):
    message: EventBusMessage          # 标题和正文
    event_type: EventTypes            # 事件类型枚举
    integration_id: str               # 触发来源标识
    document_data: EventDocumentDataBase  # 业务数据（多态）
    event_id: UUID4 | None = None     # 自动生成的事件唯一ID
    timestamp: datetime | None = None # 自动生成的时间戳
```

不同事件类型携带不同的 `document_data`（均继承自 `EventDocumentDataBase`），例如：
- `EventUserSignupData`：用户名、邮箱
- `EventRecipeData`：食谱 slug
- `EventMealplanData`：饮食计划 ID、日期、食谱信息
- `EventWebhookData`：Webhook 时间窗口和请求体

---

## 三、订阅过滤机制

系统存在两种完全独立的订阅机制：**Webhook 定时订阅** 和 **Apprise 事件通知订阅**。

### 3.1 Webhook 订阅过滤

Webhook 订阅存储在 `webhook_urls` 表中，对应 [GroupWebhooksModel](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/db/models/household/webhooks.py#L16-L40)。

过滤逻辑位于 [WebhookEventListener.get_scheduled_webhooks()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L169-L179)：

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
2. **时间窗口**：`scheduled_time` 必须落在 `(start_dt, end_dt]` 区间内（注意左开右闭）
3. **组织隔离**：`group_id` 和 `household_id` 必须匹配
4. **事件类型过滤**：`get_subscribers()` 首先检查 `event.event_type == EventTypes.webhook_task`，非 webhook 任务直接返回空列表

### 3.2 Apprise 通知订阅过滤

Apprise 通知订阅存储在两张关联表中：
- `group_events_notifiers`：通知器基本信息（名称、Apprise URL、启用状态）
- `group_events_notifier_options`：每种事件类型的订阅开关（一对一关联）

过滤逻辑位于 [AppriseEventListener.get_subscribers()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L76-L85)：

```python
notifiers = repos.group_event_notifier.multi_query(
    {"enabled": True}, override_schema=GroupEventNotifierPrivate
)
urls = [notifier.apprise_url for notifier in notifiers if getattr(notifier.options, event.event_type.name)]
```

过滤维度：
1. **启用状态**：通知器本身 `enabled == True`
2. **事件偏好**：`notifier.options.<event_type_name>` 必须为 `True`（动态反射获取）
3. **组织隔离**：通过 `repos` 的 `group_id` / `household_id` 作用域隐式过滤

---

## 四、发送入口与调用流程

### 4.1 统一发送入口：EventBusService.dispatch()

所有事件的派发入口是 [EventBusService.dispatch()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service.py#L66-L96)：

```python
def dispatch(self, integration_id, group_id, household_id, event_type, document_data, message=""):
    event = Event(...)  # 自动生成 event_id 和 timestamp

    # 确定目标 household 列表
    if not household_id:
        # 查询该 group 下所有 household
        household_ids = [...]
    else:
        household_ids = [household_id]

    for household_id in household_ids:
        if self.bg:
            # FastAPI 请求上下文中，放入后台任务异步发送
            self.bg.add_task(self._publish_event, event=event, ...)
        else:
            # 调度任务等无请求上下文场景，同步发送
            self._publish_event(event, group_id, household_id)
```

`_publish_event()` 遍历所有监听器：

```python
def _publish_event(self, event, group_id, household_id):
    for listener in self._get_listeners(group_id, household_id):
        if subscribers := listener.get_subscribers(event):
            listener.publish_to_subscribers(event, subscribers)
```

### 4.2 Webhook 定时触发流程

Webhook 采用**定时调度**模式，而非事件即时触发：

1. **调度注册**：在 [app.py](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/app.py#L134-L136) 中将 `post_group_webhooks` 注册为**每 5 分钟**执行一次：
   ```python
   SchedulerRegistry.register_minutely(tasks.post_group_webhooks)
   ```

2. **调度器执行**：[SchedulerService](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/scheduler/scheduler_service.py) 通过 `repeat_every` 装饰器驱动 `run_minutely()`，每 5 分钟遍历注册表中的回调。

3. **任务入口**：[post_group_webhooks()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/scheduler/tasks/post_webhooks.py#L24-L79)：
   - 维护全局变量 `last_ran` 记录上次运行时间
   - 计算时间窗口 `[start_dt, end_dt]`（即 `[last_ran, now]`）
   - 遍历所有 group → household，向每个 household 的事件总线派发 `EventTypes.webhook_task` 事件
   - 事件携带 `EventWebhookData`，包含时间窗口信息

4. **监听器处理**：[WebhookEventListener](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L134-L179)：
   - `get_subscribers()`：根据时间窗口查询符合条件的 webhook
   - `publish_to_subscribers()`：根据 `document_type` 组装数据（当前仅支持 `mealplan`，即查询时间范围内的饮食计划），然后调用 `WebhookPublisher.publish()`

### 4.3 Apprise 即时触发流程

Apprise 通知是**事件即时触发**模式，典型触发点：

- **用户注册**：[registration.py:38](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/routes/users/registration.py#L38) 派发 `user_signup` 事件
- **CRUD 操作**：通过 `BaseUserController.dispatch_event()` 统一派发食谱、标签等实体的创建/更新/删除事件

[AppriseEventListener.publish_to_subscribers()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L87-L88) 直接将 URL 列表交给 `ApprisePublisher`。

### 4.4 手动测试入口

- **Webhook 测试**：`POST /households/webhooks/{item_id}/test` → `post_test_webhook()`，直接构造事件并发布
- **Apprise 测试**：`POST /households/events/notifications/{item_id}/test` → 直接调用 `AppriseEventListener.publish_to_subscribers()`

---

## 五、失败重试与重复发送防护

### 5.1 失败处理

#### Webhook 失败处理
[WebhookPublisher.publish()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service/publisher.py#L40-L49)：

```python
def publish(self, event: Event, notification_urls: list[str]):
    event_payload = jsonable_encoder(event)
    for url in notification_urls:
        r = requests.post(url, json=event_payload, timeout=15)
        if self.hard_fail:
            r.raise_for_status()
```

- 默认情况下（`hard_fail=False`），HTTP 请求失败会被**静默忽略**，无任何重试或持久化记录
- 仅测试场景设置 `hard_fail=True` 抛出异常
- 请求超时固定为 **15 秒**

#### Apprise 失败处理
[ApprisePublisher.publish()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service/publisher.py#L14-L37) 依赖 Apprise 库自身的错误处理，异步模式下失败不会阻塞。

#### 调度任务级别的异常捕获
调度器对每个任务函数有统一的异常包装 ([scheduler_service.py:56-60](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/scheduler/scheduler_service.py#L56-L60))：

```python
def _scheduled_task_wrapper(callable):
    try:
        callable()
    except Exception as e:
        logger.error("Error in scheduled task func='%s': exception='%s'", callable.__name__, e)
```

任务异常仅记录日志，不会中断调度器，也不会重试当前批次。

### 5.2 重复发送防护

#### Apprise 侧：基于 event_id 的 tag 去重
[ApprisePublisher.publish()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service/publisher.py#L26-L37)：

```python
tags = []
for dest in notification_urls:
    tag = str(event.event_id)  # 使用事件唯一ID作为tag
    tags.append(tag)
    self.apprise.add(dest, tag=tag)

self.apprise.notify(title=..., body=..., tag=tags)
```

每个 URL 在添加时绑定当前事件的 `event_id` 作为 tag，通知时按 tag 匹配，确保**同一个事件对同一个 URL 只触发一次**。

#### Webhook 侧：基于时间窗口的调度去重
[post_group_webhooks()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/scheduler/tasks/post_webhooks.py#L21-L35) 维护全局 `last_ran` 变量：

```python
last_ran = datetime.now(UTC)

def post_group_webhooks(start_dt=None, ...):
    global last_ran
    start_dt = start_dt or last_ran
    last_ran = end_dt = datetime.now(UTC)
    # 查询 scheduled_time > start_dt.time() AND <= end_dt.time()
```

机制分析：
- 正常调度时，相邻两次执行的时间窗口 `(start_dt, end_dt]` 是**连续且不重叠**的
- 配合数据库查询条件 `scheduled_time > start_dt AND scheduled_time <= end_dt`，理论上每个 `scheduled_time` 只会命中一次
- **注意**：如果服务重启导致 `last_ran` 重置为启动时间，距离上次执行期间的 webhook 将被跳过（但提供了 `/households/webhooks/rerun` 接口手动重跑当日所有 webhook）
- Webhook 请求层面**没有幂等性保护**，如需去重需接收方自行使用 `event_id` 字段判断

---

## 六、签名校验实现

### 当前实现状态

经过代码审查，**当前版本的 Webhook 发送不包含任何签名校验机制**。

[WebhookPublisher.publish()](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service/publisher.py#L40-L49) 的完整实现：

```python
class WebhookPublisher:
    def __init__(self, hard_fail=False) -> None:
        self.hard_fail = hard_fail

    def publish(self, event: Event, notification_urls: list[str]):
        event_payload = jsonable_encoder(event)
        for url in notification_urls:
            r = requests.post(url, json=event_payload, timeout=15)
            if self.hard_fail:
                r.raise_for_status()
```

相关观察：
- `GroupWebhooksModel` 数据模型中**没有 `secret` 或类似密钥字段**
- HTTP 请求头中**没有添加 `X-Signature`、`X-Mealie-Signature` 等认证头**
- 整个请求仅使用标准的 `Content-Type: application/json`，无额外安全头
- 虽然 [event_bus_service.py](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/services/event_bus_service/event_bus_service.py#L18) 中定义了 `ALGORITHM = "HS256"` 常量，但该文件中并无实际使用，可能是为未来签名功能预留

### 接收方可利用的字段

尽管没有官方签名，接收方仍可利用以下字段进行基础校验：
- `event.event_id`：UUID，可用于幂等判断
- `event.timestamp`：事件创建时间，可用于判断时效性
- `event.integration_id`：事件来源标识
- Webhook URL 本身可包含 Secret Query 参数（由用户自行配置）

---

## 七、用户偏好对外部通知的影响

### 7.1 Webhook 用户偏好

Webhook 配置项 ([CreateWebhook](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/schema/household/webhook.py#L16-L48))：

| 字段 | 类型 | 说明 | 对发送的影响 |
|------|------|------|-------------|
| `enabled` | bool | 是否启用 | `False` 时该 webhook 被完全过滤 |
| `name` | str | 显示名称 | 不影响发送 |
| `url` | str | 目标地址 | 直接决定 POST 目标 |
| `webhook_type` | WebhookType | webhook 类型 | 当前仅支持 `mealplan`，决定请求体内容类型 |
| `scheduled_time` | time | 触发时间（UTC） | 决定该 webhook 被哪一次调度窗口命中 |

Webhook 偏好配置的 REST 接口在 [controller_webhooks.py](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/routes/households/controller_webhooks.py)。

### 7.2 Apprise 通知用户偏好

Apprise 通知配置 ([GroupEventNotifierSave](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/schema/household/group_events.py#L75-L79))：

| 层级 | 字段 | 对发送的影响 |
|------|------|-------------|
| 通知器级 | `enabled` | `False` 时该通知器所有事件均不发送 |
| 通知器级 | `apprise_url` | 决定通知渠道（邮件、Slack、Telegram 等） |
| 选项级（`options`） | `<event_type_name>: bool` | 每种事件类型独立开关，`True` 才会发送对应事件 |

`options` 是 [GroupEventNotifierOptions](file:///d:/fz/0601/solo-dogfeeding/code/82-mealie/mealie/schema/household/group_events.py#L13-L54)，包含与 `EventTypes` 一一对应的布尔字段，默认全部 `False`。

**核心过滤逻辑**（动态反射）：
```python
urls = [notifier.apprise_url for notifier in notifiers 
        if getattr(notifier.options, event.event_type.name)]
```

即：当且仅当 `notifier.options[event.event_type.name] == True` 时，该通知器才会收到该事件的通知。

### 7.3 两种通知方式的对比

| 特性 | Webhook | Apprise 通知 |
|------|---------|-------------|
| 触发时机 | 定时（每 5 分钟检查一次） | 即时（业务事件发生时） |
| 过滤粒度 | 按时间窗口 + 启用状态 | 按事件类型逐个开关 |
| 数据内容 | 批量数据（时间范围内的饮食计划） | 单事件数据 |
| 支持事件类型 | 仅 mealplan（当前） | 所有业务事件类型 |
| 配置入口 | `/households/webhooks` | `/households/events/notifications` |

---

## 八、完整调用流程图

```
业务代码 (e.g. 用户注册、CRUD)
    │
    ▼
EventBusService.dispatch(event_type, document_data, ...)
    │
    ├─► 在 HTTP 请求上下文?
    │      ├─ 是 → FastAPI BackgroundTasks.add_task (异步)
    │      └─ 否 → 直接调用 (同步，如调度任务)
    │
    ▼
EventBusService._publish_event(event, group_id, household_id)
    │
    ├─► AppriseEventListener
    │       ├─ get_subscribers(event)
    │       │     └─ 查询 enabled=True 的通知器，过滤 options.<event_type>==True
    │       └─ publish_to_subscribers(event, urls)
    │             └─ ApprisePublisher.publish()
    │                    └─ 按 event_id tag 去重，调用 Apprise 库发送
    │
    └─► WebhookEventListener
            ├─ get_subscribers(event)
            │     └─ 仅处理 webhook_task 事件
            │           └─ 按时间窗口 + group/household + enabled 查询
            └─ publish_to_subscribers(event, webhooks)
                  ├─ 根据 document_type 组装 body (mealplan → 查询饮食计划)
                  └─ WebhookPublisher.publish()
                        └─ requests.post(url, json=event, timeout=15)
```
