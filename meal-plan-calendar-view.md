# 计划日历（Meal Plan Calendar）代码运转分析

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                         前端 (Vue 3)                            │
│  ┌─────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
│  │  planner.vue│──▶│ useMealplans    │──▶│ MealPlanAPI      │  │
│  │  view.vue   │   │ (状态管理)      │   │ (API客户端)      │  │
│  │  edit.vue   │   │                 │   │                  │  │
│  └─────────────┘   └─────────────────┘   └────────┬─────────┘  │
│                                                    │            │
└────────────────────────────────────────────────────┼────────────┘
                                                     │ HTTP
┌────────────────────────────────────────────────────┼────────────┐
│                         后端 (FastAPI)                            │
│  ┌─────────────────────┐   ┌──────────────┐   ┌───────────────┐ │
│  │ controller_mealplan │──▶│ Repository   │──▶│ GroupMealPlan │ │
│  │     (API路由)       │   │ repository_  │   │  (DB Model)   │ │
│  │                     │   │ meals.py     │   │  mealplan.py  │ │
│  └──────────┬──────────┘   └──────────────┘   └───────────────┘ │
│             │                                                    │
│             ▼                                                    │
│  ┌─────────────────────┐   ┌────────────────────────────────┐   │
│  │ EventBus (事件总线) │──▶│ Apprise/Webhook 通知系统       │   │
│  │ event_types.py      │   │ event_bus_listeners.py         │   │
│  └─────────────────────┘   └────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、排期数据结构

### 2.1 数据库模型

**文件**: [mealie/db/models/household/mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/db/models/household/mealplan.py#L55-L77)

```python
class GroupMealPlan(SqlAlchemyBase, BaseMixins):
    __tablename__ = "group_meal_plans"

    date: datetime.date          # 排期日期 (索引)
    entry_type: str              # 餐类型: breakfast/lunch/dinner/side/snack/drink/dessert
    title: str                   # 标题 (非食谱时使用)
    text: str                    # 备注文本
    
    group_id: GUID               # 组ID
    user_id: GUID                # 创建用户ID
    household_id: AssociationProxy  # 通过user关联的家庭ID
    
    recipe_id: GUID | None       # 关联食谱ID (可选)
    recipe: Mapped[RecipeModel]  # 关联食谱对象
```

### 2.2 后端Schema分层

**文件**: [mealie/schema/meal_plan/new_meal.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/schema/meal_plan/new_meal.py)

| 类名 | 用途 | 关键字段 |
|------|------|---------|
| `CreatePlanEntry` | 创建请求 | date, entryType, title, text, recipeId |
| `SavePlanEntry` | 入库前准备 | CreatePlanEntry + groupId, userId |
| `UpdatePlanEntry` | 更新请求 | CreatePlanEntry + id, groupId, userId |
| `ReadPlanEntry` | 响应返回 | UpdatePlanEntry + householdId, recipe |

**校验规则** [L41-L47](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/schema/meal_plan/new_meal.py#L41-L47):
- `recipe_id` 和 `title` 不能同时为空
- 至少提供一个才能创建排期

### 2.3 前端类型定义

**文件**: [frontend/app/lib/api/types/meal-plan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/types/meal-plan.ts)

```typescript
export type PlanEntryType = 
  "breakfast" | "lunch" | "dinner" | "side" | "snack" | "drink" | "dessert";

export interface ReadPlanEntry {
  date: string;                     // ISO日期字符串
  entryType?: PlanEntryType;
  title?: string;
  text?: string;
  recipeId?: string | null;
  id: number;
  groupId: string;
  userId: string;
  householdId: string;
  recipe?: RecipeSummary | null;    // 关联食谱摘要
}
```

---

## 三、数据流全链路

### 3.1 API 接口定义

**文件**: [mealie/routes/households/controller_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py)

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/households/mealplans` | 分页查询，支持 `start_date` / `end_date` 过滤 |
| POST | `/households/mealplans` | 创建排期，触发 `mealplan_entry_created` 事件 |
| GET | `/households/mealplans/today` | 获取今日排期 |
| POST | `/households/mealplans/random` | 根据规则创建随机排期 |
| GET | `/households/mealplans/{id}` | 获取单个排期 |
| PUT | `/households/mealplans/{id}` | 更新排期，触发 `mealplan_entry_updated` 事件 |
| DELETE | `/households/mealplans/{id}` | 删除排期，触发 `mealplan_entry_deleted` 事件 |

### 3.2 数据访问层 (Repository)

**文件**: [mealie/repos/repository_meals.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/repos/repository_meals.py)

```python
class RepositoryMeals(HouseholdRepositoryGeneric[ReadPlanEntry, GroupMealPlan]):
    # 获取今日排期
    def get_today(self, tz=UTC) -> list[ReadPlanEntry]
    
    # 按日期范围查询
    def get_meals_by_date_range(self, start_date, end_date) -> list[ReadPlanEntry]
```

**查询优化**: [ReadPlanEntry.loader_options](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/schema/meal_plan/new_meal.py#L68-L74) 使用 `selectinload` 预加载关联数据（recipe、category、tags、tools、user），避免 N+1 查询。

### 3.3 前端API客户端

**文件**: [frontend/app/lib/api/user/group-mealplan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/user/group-mealplan.ts)

```typescript
export class MealPlanAPI extends BaseCRUDAPI<CreatePlanEntry, ReadPlanEntry, UpdatePlanEntry> {
  baseRoute = "/api/households/mealplans";
  itemRoute = (id) => `/api/households/mealplans/${id}`;
  
  async setRandom(payload: CreateRandomEntry) {
    return await this.requests.post<ReadPlanEntry>("/api/households/mealplans/random", payload);
  }
}
```

继承自 [BaseCRUDAPI](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/base/base-clients.ts#L58-L81)，自动获得 `getAll`、`getOne`、`createOne`、`updateOne`、`deleteOne` 等标准方法。

---

## 四、日历视图渲染逻辑

### 4.1 主页面容器

**文件**: [frontend/app/pages/household/mealplan/planner.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner.vue)

**核心数据流转**:

```
1. 日期范围初始化
   ├─ 从 URL query 参数读取 start/end
   └─ 默认: 今天 ± N 天（可配置）

2. 调用 useMealplans(weekRange) 获取数据
   └─ 返回响应式 mealplans 和 actions

3. 计算属性 mealsByDate
   ├─ 遍历日期范围的每一天
   └─ 过滤出对应日期的排期
   └─ 结构: [{ date: Date, meals: ReadPlanEntry[] }]

4. 传递给子组件
   ├─ <NuxtPage :mealplans="mealsByDate" :actions="actions" />
   └─ 根据路由切换 view.vue 或 edit.vue
```

**关键计算属性**:
- `weekRange` [L172-L185](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner.vue#L172-L185): 响应式日期范围
- `days` [L217-L231](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner.vue#L217-L231): 展开为日期数组
- `mealsByDate` [L233-L237](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner.vue#L233-L237): 按日期分组的排期

### 4.2 视图模式 (View)

**文件**: [frontend/app/pages/household/mealplan/planner/view.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/view.vue)

**渲染流程**:
```
遍历 mealsByDate → 每天一列
  ├─ 日期标题卡片（今天高亮）
  ├─ 按餐类型分组 (breakfast → lunch → dinner → ...)
  │   └─ 过滤空分组
  └─ 每组内渲染 RecipeCardMobile 卡片
```

**关键逻辑** [L84-L135](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/view.vue#L84-L135):
- 固定7种餐类型顺序
- 动态过滤掉没有排期的餐类型分组
- 收集当天所有食谱用于右键菜单

### 4.3 编辑模式 (Edit)

**文件**: [frontend/app/pages/household/mealplan/planner/edit.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue)

**本地状态同步** [L269-L284](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue#L269-L284):

```typescript
// 本地可变映射表，按日期字符串索引
const mealplansByDate = reactive<{ [date: string]: UpdatePlanEntry[] }>({});

// 深度监听 props.mealplans，同步到本地状态
watch(() => props.mealplans, (plans) => {
  for (const plan of plans) {
    mealplansByDate[plan.date.toString()] = plan.meals ? [...plan.meals] : [];
  }
}, { immediate: true, deep: true });
```

---

## 五、编辑动作交互流程

### 5.1 状态管理 Composable

**文件**: [frontend/app/composables/use-group-mealplan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/composables/use-group-mealplan.ts)

**核心 API**:

```typescript
export const useMealplans = function (range: Ref<DateRange>) {
  const mealplans = actions.getAll();  // 初始获取
  
  return {
    mealplans,     // Ref<ReadPlanEntry[]> 响应式数据
    actions: {
      getAll(),       // 使用 useAsyncData 获取
      refreshAll(),   // 强制刷新
      createOne(),    // 创建后刷新
      updateOne(),    // 更新后刷新
      deleteOne(),    // 删除后刷新
      setType(),      // 更改餐类型
    },
    loading,
    validForm,
  };
};
```

**自动刷新**: [L112](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/composables/use-group-mealplan.ts#L112)
```typescript
watch(range, actions.refreshAll);  // 日期范围变化时自动刷新
```

### 5.2 创建排期

**流程**:
```
点击「新建」按钮 → openDialog(date)
  ├─ 设置 newMeal.date = 点击的日期
  └─ 打开 BaseDialog 表单
      ├─ 日期选择器
      ├─ 餐类型下拉
      ├─ 切换: 食谱选择 / 纯文本笔记
      └─ 提交
          ├─ 校验: 食谱ID 或 标题 必填
          ├─ 调用 actions.createOne(payload)
          │   └─ POST /api/households/mealplans
          └─ 调用 refreshAll() 刷新列表
```

**代码位置**: [edit.vue L350-L353](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue#L350-L353)

### 5.3 更新排期

**流程**:
```
点击排期卡片 → editMeal(mealplan)
  ├─ 填充 newMeal 响应式对象
  ├─ 打开编辑对话框（同创建表单）
  └─ 提交 → actions.updateOne(updated)
      ├─ PUT /api/households/mealplans/{id}
      └─ refreshAll()
```

**代码位置**: [edit.vue L355-L372](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue#L355-L372)

### 5.4 删除排期

**流程**:
```
点击删除图标 → actions.deleteOne(id)
  ├─ DELETE /api/households/mealplans/{id}
  └─ refreshAll()
```

**代码位置**: [edit.vue L158-L160](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue#L158-L160)

### 5.5 拖拽调整日期

**使用库**: `vue-draggable-plus` (基于 SortableJS)

**流程**:
```
拖拽卡片到另一列 → onMoveCallback(evt)
  ├─ 获取源日期列索引和目标日期列索引
  ├─ 获取移动后的 mealData 对象
  ├─ 修改 mealData.date = 目标日期
  └─ 调用 actions.updateOne(mealData)
      ├─ PUT /api/households/mealplans/{id}
      └─ refreshAll()
```

**代码位置**: [edit.vue L286-L310](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue#L286-L310)

### 5.6 切换餐类型

**流程**:
```
点击餐类型Chip → 展开菜单
  └─ 选择新类型 → actions.setType(mealplan, newType)
      ├─ 修改 payload.entryType = newType
      ├─ 调用 updateOne
      └─ refreshAll()
```

**代码位置**: [use-group-mealplan.ts L104-L107](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/composables/use-group-mealplan.ts#L104-L107)

### 5.7 随机餐

**流程**:
```
点击「随机晚餐」等按钮 → randomMeal(date, type)
  ├─ POST /api/households/mealplans/random
  │   └─ 后端根据 PlanRules 过滤随机选择食谱
  └─ refreshAll()
```

**后端随机逻辑** [controller_mealplan.py L49-L74](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py#L49-L74):
1. 查询匹配 `(date, entry_type)` 的规则
2. 组合规则的 `query_filter_string`
3. 按 `order_by="random"` 查询食谱
4. 取第一个创建排期

---

## 六、状态同步机制

### 6.1 前端状态同步模式

**当前采用「悲观刷新」模式**:
```
每次修改操作 → 调用后端API → 调用 refreshAll() 重新拉取全量数据
```

**特点**:
- ✅ 实现简单，状态一致性有保障
- ❌ 网络开销大，每次操作都要重新拉取整个日期范围的数据
- ❌ 无实时推送，其他用户的修改需要手动刷新才能看到

**关键代码**: [use-group-mealplan.ts L73-L102](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/composables/use-group-mealplan.ts#L73-L102)

```typescript
async createOne(payload: CreatePlanEntry) {
  const { data } = await api.mealplans.createOne(payload);
  if (data) this.refreshAll();  // 每次创建后全量刷新
}

async updateOne(updateData: UpdatePlanEntry) {
  const { data } = await api.mealplans.updateOne(updateData.id, updateData);
  if (data) this.refreshAll();  // 每次更新后全量刷新
}

async deleteOne(id: string | number) {
  const { data } = await api.mealplans.deleteOne(id);
  if (data) this.refreshAll();  // 每次删除后全量刷新
}
```

### 6.2 后端事件总线

**文件**: [mealie/services/event_bus_service/event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_types.py)

**三种事件类型**:
```python
class EventTypes(Enum):
    mealplan_entry_created = auto()  # 创建
    mealplan_entry_updated = auto()  # 更新
    mealplan_entry_deleted = auto()  # 删除
```

**事件数据结构**:
```python
class EventMealplanData(EventDocumentDataBase):
    document_type = EventDocumentType.mealplan
    mealplan_id: int
    date: date
    recipe_id: UUID4 | None
    recipe_name: str | None
    recipe_slug: str | None
```

**发布时机** [controller_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py):
- 创建成功后: [L107-L120](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py#L107-L120)
- 更新成功后: [L181-L194](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py#L181-L194)
- 删除成功后: [L202-L215](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py#L202-L215)

### 6.3 通知监听系统

**文件**: [mealie/services/event_bus_service/event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_bus_listeners.py)

**两类监听器**:

1. **AppriseEventListener** [L72-L131](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L72-L131)
   - 查询启用的通知器配置
   - 检查是否订阅了对应事件类型
   - 通过 Apprise 推送通知（支持邮件、Telegram、Slack 等）

2. **WebhookEventListener** [L134-L179](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_bus_listeners.py#L134-L179)
   - 定时触发的 Webhook 任务
   - 可按 `mealplan` 类型推送整周排期数据

**注意**: 事件总线目前仅用于通知，**不用于前端实时数据同步**。前端仍采用手动刷新模式。

### 6.4 订阅配置

**数据库表**: `group_events_notifier_options`

每个通知器可以独立订阅不同事件:
```python
mealplan_entry_created: bool = False
mealplan_entry_updated: bool = False
mealplan_entry_deleted: bool = False
```

---

## 七、关键文件索引

| 层级 | 文件 | 说明 |
|------|------|------|
| 数据模型 | [mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/db/models/household/mealplan.py) | DB 模型定义 |
| 数据结构 | [new_meal.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/schema/meal_plan/new_meal.py) | 后端请求/响应 Schema |
| 数据访问 | [repository_meals.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/repos/repository_meals.py) | Repository 层 |
| API 路由 | [controller_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/routes/households/controller_mealplan.py) | FastAPI 控制器 |
| 事件类型 | [event_types.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_types.py) | 事件数据结构 |
| 事件监听 | [event_bus_listeners.py](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/mealie/services/event_bus_service/event_bus_listeners.py) | 通知监听器 |
| 前端类型 | [meal-plan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/types/meal-plan.ts) | TypeScript 类型 |
| 前端API | [group-mealplan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/user/group-mealplan.ts) | API 客户端 |
| 状态管理 | [use-group-mealplan.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/composables/use-group-mealplan.ts) | Composable |
| 主页面 | [planner.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner.vue) | 主容器组件 |
| 视图模式 | [view.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/view.vue) | 只读视图 |
| 编辑模式 | [edit.vue](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/pages/household/mealplan/planner/edit.vue) | 可编辑视图 |
| API 基类 | [base-clients.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/base/base-clients.ts) | CRUD 基类 |
| API 聚合 | [client-user.ts](file:///d:/fz/0601/solo-dogfeeding/code/32-mealie/frontend/app/lib/api/client-user.ts) | 用户 API 聚合 |

---

## 八、架构总结

### 8.1 设计优点

1. **分层清晰**: Model → Schema → Repository → Controller → API Client → Composable → Component，职责明确
2. **类型安全**: 前后端类型通过 `pydantic2ts` 自动生成，保持一致
3. **查询优化**: 使用 `selectinload` 预加载关联数据，避免 N+1 问题
4. **事件驱动**: 后端操作后发布事件，便于扩展通知和集成
5. **响应式设计**: 前端使用 Vue 3 Composition API，日期范围变化自动触发刷新

### 8.2 可改进点

1. **状态同步**: 当前采用「每次修改全量刷新」模式，可优化为:
   - 乐观更新: 本地先更新状态，API 成功后再同步
   - 增量更新: 后端返回修改后的单个对象，前端局部更新
   - 实时推送: 引入 WebSocket/SSE，接收后端事件推送

2. **缓存策略**: 相同日期范围的查询结果可缓存，避免重复请求

3. **编辑模式本地状态**: 当前拖拽修改后立即调用 API，可增加本地暂存 + 批量保存模式
