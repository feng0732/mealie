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
│             │                                                    │
│             ▼                                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 定时任务 (APScheduler)                                   │   │
│  │  ├─ 每日: create_mealplan_timeline_events                │   │
│  │  │   ├─ 生成食谱时间线事件                                │   │
│  │  │   └─ 更新 last_made 时间                              │   │
│  │  └─ 每分钟: post_group_webhooks                          │   │
│  │      └─ 按时间窗口推送排期数据                            │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、排期数据结构

### 2.1 数据库模型

**文件**: `mealie/db/models/household/mealplan.py#L55-L77`

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

**文件**: `mealie/schema/meal_plan/new_meal.py`

| 类名 | 用途 | 关键字段 |
|------|------|---------|
| `CreatePlanEntry` | 创建请求 | date, entryType, title, text, recipeId |
| `SavePlanEntry` | 入库前准备 | CreatePlanEntry + groupId, userId |
| `UpdatePlanEntry` | 更新请求 | CreatePlanEntry + id, groupId, userId |
| `ReadPlanEntry` | 响应返回 | UpdatePlanEntry + householdId, recipe |

**校验规则** `mealie/schema/meal_plan/new_meal.py#L41-L47`:
- `recipe_id` 和 `title` 不能同时为空
- 至少提供一个才能创建排期

### 2.3 前端类型定义

**文件**: `frontend/app/lib/api/types/meal-plan.ts`

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

**文件**: `mealie/routes/households/controller_mealplan.py`

| 方法 | 路由 | 说明 |
|------|------|------|
| GET | `/households/mealplans` | 分页查询，支持 `start_date` / `end_date` 作为独立参数，拼接到 query_filter |
| POST | `/households/mealplans` | 创建排期，触发 `mealplan_entry_created` 事件 |
| GET | `/households/mealplans/today` | 获取今日排期 |
| POST | `/households/mealplans/random` | 根据规则创建随机排期 |
| GET | `/households/mealplans/{id}` | 获取单个排期 |
| PUT | `/households/mealplans/{id}` | 更新排期，触发 `mealplan_entry_updated` 事件 |
| DELETE | `/households/mealplans/{id}` | 删除排期，触发 `mealplan_entry_deleted` 事件 |

### 3.2 数据访问层 (Repository)

**文件**: `mealie/repos/repository_meals.py`

```python
class RepositoryMeals(HouseholdRepositoryGeneric[ReadPlanEntry, GroupMealPlan]):
    # 获取今日排期
    def get_today(self, tz=UTC) -> list[ReadPlanEntry]
    
    # 按日期范围查询 - 仅 Webhook 内部使用，API 不暴露此方法
    def get_meals_by_date_range(self, start_date, end_date) -> list[ReadPlanEntry]
```

**查询优化**: `mealie/schema/meal_plan/new_meal.py#L68-L74` 使用 `selectinload` 预加载关联数据（recipe、category、tags、tools、user），避免 N+1 查询。

### 3.3 前端API客户端

**文件**: `frontend/app/lib/api/user/group-mealplan.ts`

```typescript
export class MealPlanAPI extends BaseCRUDAPI<CreatePlanEntry, ReadPlanEntry, UpdatePlanEntry> {
  baseRoute = "/api/households/mealplans";
  itemRoute = (id) => `/api/households/mealplans/${id}`;
  
  async setRandom(payload: CreateRandomEntry) {
    return await this.requests.post<ReadPlanEntry>("/api/households/mealplans/random", payload);
  }
}
```

继承自 `frontend/app/lib/api/base/base-clients.ts#L58-L81`，自动获得 `getAll`、`getOne`、`createOne`、`updateOne`、`deleteOne` 等标准方法。

---

## 四、日历视图渲染逻辑

### 4.1 主页面容器

**文件**: `frontend/app/pages/household/mealplan/planner.vue`

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
- `weekRange` `frontend/app/pages/household/mealplan/planner.vue#L172-L185`: 响应式日期范围
- `days` `frontend/app/pages/household/mealplan/planner.vue#L217-L231`: 展开为日期数组
- `mealsByDate` `frontend/app/pages/household/mealplan/planner.vue#L233-L237`: 按日期分组的排期

### 4.2 视图模式 (View)

**文件**: `frontend/app/pages/household/mealplan/planner/view.vue`

**渲染流程**:
```
遍历 mealsByDate → 每天一列
  ├─ 日期标题卡片（今天高亮）
  ├─ 按餐类型分组 (breakfast → lunch → dinner → ...)
  │   └─ 过滤空分组
  └─ 每组内渲染 RecipeCardMobile 卡片
```

**关键逻辑** `frontend/app/pages/household/mealplan/planner/view.vue#L84-L135`:
- 固定7种餐类型顺序
- 动态过滤掉没有排期的餐类型分组
- 收集当天所有食谱用于右键菜单

### 4.3 编辑模式 (Edit)

**文件**: `frontend/app/pages/household/mealplan/planner/edit.vue`

**本地状态同步** `frontend/app/pages/household/mealplan/planner/edit.vue#L269-L284`:

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

**文件**: `frontend/app/composables/use-group-mealplan.ts`

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

**自动刷新**: `frontend/app/composables/use-group-mealplan.ts#L112`
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

**代码位置**: `frontend/app/pages/household/mealplan/planner/edit.vue#L350-L353`

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

**代码位置**: `frontend/app/pages/household/mealplan/planner/edit.vue#L355-L372`

### 5.4 删除排期

**流程**:
```
点击删除图标 → actions.deleteOne(id)
  ├─ DELETE /api/households/mealplans/{id}
  └─ refreshAll()
```

**代码位置**: `frontend/app/pages/household/mealplan/planner/edit.vue#L158-L160`

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

**代码位置**: `frontend/app/pages/household/mealplan/planner/edit.vue#L286-L310`

### 5.6 切换餐类型

**流程**:
```
点击餐类型Chip → 展开菜单
  └─ 选择新类型 → actions.setType(mealplan, newType)
      ├─ 修改 payload.entryType = newType
      ├─ 调用 updateOne
      └─ refreshAll()
```

**代码位置**: `frontend/app/composables/use-group-mealplan.ts#L104-L107`

### 5.7 随机餐

**流程**:
```
点击「随机晚餐」等按钮 → randomMeal(date, type)
  ├─ POST /api/households/mealplans/random
  │   └─ 后端根据 PlanRules 过滤随机选择食谱
  └─ refreshAll()
```

**后端随机逻辑** `mealie/routes/households/controller_mealplan.py#L49-L74`:
1. 查询匹配 `(date, entry_type)` 的规则
2. 组合规则的 `query_filter_string`
3. 按 `order_by="random"` 查询食谱
4. 取第一个创建排期

---

## 六、状态同步机制深度分析

### 6.1 前端状态同步模式

**当前采用「悲观刷新」模式**:
```
每次修改操作 → 调用后端API → 调用 refreshAll() 重新拉取全量数据
```

**特点**:
- ✅ 实现简单，状态一致性有保障
- ❌ 网络开销大，每次操作都要重新拉取整个日期范围的数据
- ❌ 无实时推送，其他用户的修改需要手动刷新才能看到

**关键代码**: `frontend/app/composables/use-group-mealplan.ts#L73-L102`

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

### 6.2 定时任务调度框架

**调度器注册**: `mealie/app.py#L124-L144`

```python
async def start_scheduler():
    # 每日任务 - 每天执行一次
    SchedulerRegistry.register_daily(
        tasks.create_mealplan_timeline_events,  # 生成时间线 + 更新last_made
        tasks.purge_expired_tokens,
        # ... 其他每日任务
    )

    # 每分钟任务 - 每60秒执行一次
    SchedulerRegistry.register_minutely(
        tasks.post_group_webhooks,  # Webhook 推送
    )

    # 每小时任务
    SchedulerRegistry.register_hourly(
        tasks.locked_user_reset,
    )

    await SchedulerService.start()
```

**任务注册中心**: `mealie/services/scheduler/scheduler_registry.py` 提供三种频率的任务注册机制：
- `register_daily()`: 每日任务
- `register_hourly()`: 每小时任务  
- `register_minutely()`: 每分钟任务

---

### 6.3 定时任务一：生成时间线事件

**文件**: `mealie/services/scheduler/tasks/create_timeline_events.py`

**执行频率**: 每日一次（`register_daily`）

**完整流程**:

```
create_mealplan_timeline_events()  [L125-L134]
  └─ 遍历所有 group
      └─ _create_mealplan_timeline_events_for_group()  [L117-L122]
          └─ 遍历 group 下所有 household
              └─ _create_mealplan_timeline_events_for_household()  [L25-L114]
                  ├─ 步骤1: 获取今日所有排期
                  │   └─ repos.meals.get_today(tz=local_tz)  [L37]
                  │
                  ├─ 步骤2: 遍历每个排期
                  │   ├─ 跳过无食谱或无用户的排期  [L39-L40]
                  │   ├─ 生成事件主题: "{用户名} made this for {餐类型}"  [L47-L51]
                  │   │
                  │   ├─ 步骤3: 幂等性检查（防止重复创建）
                  │   │   └─ 查询今日是否已有相同主题的事件  [L53-L67]
                  │   │      query_filter = "recipe_id = X AND timestamp >= 今日0点 AND timestamp < 明日0点 AND subject = Y"
                  │   │
                  │   ├─ 步骤4: 收集需要更新 last_made 的食谱
                  │   │   ├─ 获取该食谱在当前家庭的 last_made 记录  [L70-L71]
                  │   │   └─ 如果 last_made 早于今天，则加入更新队列  [L72-L73]
                  │   │
                  │   └─ 步骤5: 收集待创建的时间线事件  [L75-L83]
                  │
                  ├─ 步骤6: 批量创建时间线事件  [L91-L103]
                  │   ├─ repos.recipe_timeline_events.create(event)
                  │   └─ 发布 recipe_updated 事件通知前端
                  │
                  └─ 步骤7: 批量更新食谱 last_made 时间  [L105-L114]
                      ├─ household_service.set_household_recipe(slug, {last_made: event_time})
                      ├─ repos.recipes.patch(slug, {last_made: event_time})
                      └─ 发布 recipe_updated 事件通知前端
```

**关键逻辑详解**:

**幂等性检查** `mealie/services/scheduler/tasks/create_timeline_events.py#L53-L67`:
```python
# 查询时间窗口: 今日 00:00 ~ 次日 00:00
query_start_time = datetime.combine(datetime.now(UTC).date(), time.min)
query_end_time = query_start_time + timedelta(days=1)

query = PaginationQuery(
    query_filter=(
        f'recipe_id = "{mealplan.recipe_id}" '
        f'AND timestamp >= "{query_start_time.isoformat()}" '
        f'AND timestamp < "{query_end_time.isoformat()}" '
        f'AND subject = "{event_subject}"'
    )
)

events = repos.recipe_timeline_events.page_all(pagination=query)
if events.items:
    continue  # 已有相同事件，跳过
```

**时间线事件数据结构**: `mealie/schema/recipe/recipe_timeline_events.py`
```python
class RecipeTimelineEventCreate(MealieModel):
    user_id: UUID4                # 关联用户
    subject: str                  # 事件标题，如 "张三 made this for dinner"
    event_type: TimelineEventType # info / warning / success 等
    timestamp: datetime           # 事件时间
    recipe_id: UUID4              # 关联食谱
    text: str | None = None       # 详细文本
```

---

### 6.4 状态同步二：食谱最近制作时间（last_made）更新

**双表存储设计**:

| 表名 | 字段 | 说明 | 更新时机 |
|------|------|------|---------|
| `households_to_recipes` | `last_made` | 家庭级别的最近制作时间 | 定时任务每日更新 + DB触发器 |
| `recipes` | `last_made` | 全局的最近制作时间（取所有家庭最大值） | DB触发器自动同步 |

**1. 定时任务主动更新**: `mealie/services/scheduler/tasks/create_timeline_events.py#L69-L73, L105-L114`

```python
# 步骤4: 收集需要更新 last_made 的食谱
household_to_recipe = household_service.get_household_recipe(mealplan.recipe.slug)
last_made = household_to_recipe.last_made if household_to_recipe else None

# 仅当 last_made 为空或早于今天时才更新，且每个食谱只更新一次
if (not last_made or last_made.date() < event_time.date()) and mealplan.recipe_id not in recipes_to_update:
    recipes_to_update[mealplan.recipe_id] = mealplan.recipe

# 步骤7: 执行更新
for recipe in recipes_to_update.values():
    # 更新家庭级别的 last_made
    household_service.set_household_recipe(recipe.slug, HouseholdRecipeUpdate(last_made=event_time))
    # 更新食谱表的全局 last_made（会触发DB触发器）
    repos.recipes.patch(recipe.slug, {"last_made": event_time})
    # 发布事件通知
    event_bus_service.dispatch(event_type=EventTypes.recipe_updated, ...)
```

**2. DB 触发器自动同步**: `mealie/db/models/household/household_to_recipe.py#L39-L60`

```python
# 家庭食谱表的 last_made 更新后，自动同步到食谱表的全局 last_made
def update_recipe_last_made(session: Session, target: HouseholdToRecipe):
    if not target.last_made:
        return

    recipe = session.query(RecipeModel).filter(RecipeModel.id == target.recipe_id).first()
    if not recipe:
        return

    # 取最大值，确保全局 last_made 是所有家庭中最新的
    recipe.last_made = recipe.last_made or target.last_made
    recipe.last_made = max(recipe.last_made, target.last_made)

# 监听 insert/update/delete 事件
@event.listens_for(HouseholdToRecipe, "after_insert")
@event.listens_for(HouseholdToRecipe, "after_update")
@event.listens_for(HouseholdToRecipe, "after_delete")
def update_recipe_rating_on_insert_or_delete(_, connection: Connection, target: HouseholdToRecipe):
    session = Session(bind=connection)
    update_recipe_last_made(session, target)
    session.commit()
```

**3. HouseholdService 封装**: `mealie/services/household_services/household_service.py#L70-L99`

```python
class HouseholdService(BaseService):
    def get_household_recipe(self, recipe_slug: str) -> HouseholdRecipeSummary | None:
        """获取当前家庭对某食谱的关联数据（含last_made）"""
        recipe = self._get_recipe(recipe_slug)
        household_recipe_out = self.repos.household_recipes.get_by_recipe(recipe.id)
        if household_recipe_out:
            return household_recipe_out.cast(HouseholdRecipeSummary)
        return HouseholdRecipeSummary(recipe_id=recipe.id)

    def set_household_recipe(self, recipe_slug: str | UUID, data: HouseholdRecipeUpdate) -> HouseholdRecipeSummary:
        """设置家庭食谱数据（如last_made），不存在则创建"""
        recipe = self._get_recipe(recipe_slug)
        existing_household_recipe = self.repos.household_recipes.get_by_recipe(recipe.id)

        if existing_household_recipe:
            # 已存在则更新
            updated_data = existing_household_recipe.cast(HouseholdRecipeUpdate, **data.model_dump())
            household_recipe_out = self.repos.household_recipes.patch(existing_household_recipe.id, updated_data)
        else:
            # 不存在则创建
            create_data = HouseholdRecipeCreate(
                household_id=self.household_id, recipe_id=recipe.id, **data.model_dump()
            )
            household_recipe_out = self.repos.household_recipes.create(create_data)

        return household_recipe_out.cast(HouseholdRecipeSummary)
```

**更新路径总结**:
```
定时任务每日执行
  └─ create_mealplan_timeline_events()
      ├─ 发现今日排期食谱需要更新 last_made
      ├─ HouseholdService.set_household_recipe(..., {last_made: now})
      │   ├─ 更新 households_to_recipes 表
      │   │   └─ 触发 SQLAlchemy after_update 事件
      │   │       └─ update_recipe_last_made() 触发器
      │   │           └─ 自动更新 recipes 表的全局 last_made（取最大值）
      │   └─ 发布 recipe_updated 事件（家庭级别）
      └─ repos.recipes.patch(slug, {last_made: now})
          └─ 发布 recipe_updated 事件（全局级别）
```

---

### 6.5 状态同步三：Webhook 推送取数范围

**执行频率**: 每分钟一次（`register_minutely`）

**文件**: `mealie/services/scheduler/tasks/post_webhooks.py` + `mealie/services/event_bus_service/event_bus_listeners.py`

**核心方法对比**:

| 方法 | 用途 | 调用方 | 代码位置 |
|------|------|--------|---------|
| `get_meals_by_date_range()` | 按日期范围查询，供 Webhook 内部使用 | WebhookEventListener | `mealie/repos/repository_meals.py#L23-L33` |
| `get_all()` + `page_all()` | API 分页查询，将日期拼接到 query_filter | 前端 API 调用 | `mealie/routes/households/controller_mealplan.py#L76-L100` |

**注意**: `get_meals_by_date_range()` 仅在 Webhook 监听器中调用，前端 API 不使用此方法。

**完整流程**:

```
post_group_webhooks()  [L24-L79]
  ├─ 步骤1: 确定时间窗口
  │   ├─ start_dt = 上次运行时间（或传入参数） [L32]
  │   └─ end_dt = 当前时间（同时更新 last_ran） [L35]
  │   └─ 时间窗口 = (上次运行时间, 当前时间]
  │
  ├─ 步骤2: 遍历所有 group 和 household
  │   └─ 为每个家庭发布 webhook_task 事件到 EventBus
  │
  └─ 步骤3: WebhookEventListener 处理事件
      └─ publish_to_subscribers()  [L148-L167]
          ├─ 匹配 document_type = EventDocumentType.mealplan
          ├─ 调用 get_meals_by_date_range(start_dt, end_dt)
          │   └─ 取数范围: [start_dt.date(), end_dt.date()] 闭区间
          ├─ 将查询结果设置到 webhook_body
          └─ POST 到所有匹配的 Webhook URL
```

**关键逻辑详解**:

**1. 时间窗口计算**: `mealie/services/scheduler/tasks/post_webhooks.py#L21-L35`

```python
last_ran = datetime.now(UTC)  # 模块级变量，记录上次运行时间

def post_group_webhooks(start_dt: datetime | None = None, ...):
    global last_ran
    
    # 开始时间: 未指定则使用上次运行时间
    start_dt = start_dt or last_ran
    
    # 结束时间: 当前时间，同时更新 last_ran 供下次使用
    last_ran = end_dt = datetime.now(UTC)
    
    # 时间窗口示例（每分钟执行）:
    # 第1次运行: 10:00:00 → (10:00:00, 10:00:00] → 空窗口
    # 第2次运行: 10:01:00 → (10:00:00, 10:01:00] → 1分钟窗口
    # 第3次运行: 10:02:00 → (10:01:00, 10:02:00] → 1分钟窗口
```

**2. Webhook 订阅匹配**: `mealie/services/event_bus_service/event_bus_listeners.py#L169-L179`

```python
def get_scheduled_webhooks(self, start_dt: datetime, end_dt: datetime) -> list[ReadWebhook]:
    stmt = select(GroupWebhooksModel).where(
        GroupWebhooksModel.enabled == True,
        # 比较的是 scheduled_time (time类型) 是否落在时间窗口内
        GroupWebhooksModel.scheduled_time > start_dt.astimezone(UTC).time(),
        GroupWebhooksModel.scheduled_time <= end_dt.astimezone(UTC).time(),
        GroupWebhooksModel.group_id == self.group_id,
        GroupWebhooksModel.household_id == self.household_id,
    )
    return session.execute(stmt).scalars().all()
```

**3. 取数范围**: `mealie/repos/repository_meals.py#L23-L33`

```python
def get_meals_by_date_range(self, start_date: datetime, end_date: datetime) -> list[ReadPlanEntry]:
    if not self.household_id:
        raise Exception("household_id not set")

    stmt = select(GroupMealPlan).filter(
        GroupMealPlan.date >= start_date.date(),  # 闭区间: >= 开始日期
        GroupMealPlan.date <= end_date.date(),    # 闭区间: <= 结束日期
        GroupMealPlan.household_id == self.household_id,
    )
    plans = self.session.execute(stmt).scalars().all()
    return [self.schema.model_validate(x) for x in plans]
```

**取数范围示例**:

假设 Webhook 配置的 `scheduled_time` 是每天早上 8:00，运行时间线如下：

```
时间点: 08:00:00 → post_group_webhooks() 执行
  ├─ start_dt = 上次运行时间 (如 07:59:00)
  ├─ end_dt = 当前时间 (08:00:00)
  │
  ├─ Webhook 匹配检查:
  │   scheduled_time (08:00:00) > start_dt.time (07:59:00) ✓
  │   scheduled_time (08:00:00) <= end_dt.time (08:00:00) ✓
  │   → 匹配成功
  │
  └─ 取数范围: [07:59:00.date(), 08:00:00.date()]
      └─ 即: [今天, 今天] → 获取今日所有排期
```

**注意**: 虽然时间窗口可能只有1分钟，但 `get_meals_by_date_range` 使用的是日期级别（`date()`），所以实际返回的是**整天**的排期数据，而不是时间窗口内的增量数据。

---

### 6.5.1 跨午夜日期窗口深度分析

**核心问题**: 当 Webhook 的时间窗口跨午夜时（如 23:59 ~ 00:01），取数范围如何计算？

**三层时间窗口设计**:

| 层次 | 用途 | 粒度 | 代码位置 |
|------|------|------|---------|
| 滑动时间窗口 | Webhook 订阅匹配 | 分钟级 (time) | `mealie/services/scheduler/tasks/post_webhooks.py#L31-L35` |
| 取数日期窗口 | Webhook 实际查询数据 | 日期级 (date) | `mealie/repos/repository_meals.py#L27-L30` |
| Webhook 调度时间 | 触发推送时机 | 分钟级 (time) | `mealie/services/event_bus_service/event_bus_listeners.py#L172-L176` |

**1. 跨午夜时的取数范围计算**

**文件**: `mealie/repos/repository_meals.py#L23-L33`

```python
def get_meals_by_date_range(self, start_date: datetime, end_date: datetime) -> list[ReadPlanEntry]:
    stmt = select(GroupMealPlan).filter(
        GroupMealPlan.date >= start_date.date(),  # 转换为 date，丢弃时间部分
        GroupMealPlan.date <= end_date.date(),    # 转换为 date，丢弃时间部分
        GroupMealPlan.household_id == self.household_id,
    )
```

**关键转换**: `datetime.date()` 方法只保留日期部分，丢弃时分秒。

**跨午夜场景示例**:

```
场景1: 正常情况（不跨午夜）
  start_dt = 2026-06-05 07:59:00 UTC
  end_dt   = 2026-06-05 08:00:00 UTC
  → start_date.date() = 2026-06-05
  → end_date.date()   = 2026-06-05
  → 查询条件: date >= 2026-06-05 AND date <= 2026-06-05
  → 取数范围: 6月5日当天所有排期

场景2: 跨午夜边界
  start_dt = 2026-06-05 23:59:00 UTC
  end_dt   = 2026-06-06 00:00:00 UTC
  → start_date.date() = 2026-06-05
  → end_date.date()   = 2026-06-06
  → 查询条件: date >= 2026-06-05 AND date <= 2026-06-06
  → 取数范围: 6月5日 + 6月6日 两天所有排期

场景3: 跨午夜（常见情况）
  start_dt = 2026-06-05 23:59:00 UTC
  end_dt   = 2026-06-06 00:01:00 UTC
  → start_date.date() = 2026-06-05
  → end_date.date()   = 2026-06-06
  → 查询条件: date >= 2026-06-05 AND date <= 2026-06-06
  → 取数范围: 6月5日 + 6月6日 两天所有排期

场景4: 跨两天
  start_dt = 2026-06-05 23:30:00 UTC
  end_dt   = 2026-06-07 00:30:00 UTC
  → start_date.date() = 2026-06-05
  → end_date.date()   = 2026-06-07
  → 查询条件: date >= 2026-06-05 AND date <= 2026-06-07
  → 取数范围: 6月5日 + 6月6日 + 6月7日 三天所有排期
```

**2. 跨午夜时的 Webhook 订阅匹配**

**文件**: `mealie/services/event_bus_service/event_bus_listeners.py#L169-L179`

```python
def get_scheduled_webhooks(self, start_dt: datetime, end_dt: datetime) -> list[ReadWebhook]:
    stmt = select(GroupWebhooksModel).where(
        GroupWebhooksModel.enabled == True,
        # 比较的是 time 类型，不是 datetime 或 date
        GroupWebhooksModel.scheduled_time > start_dt.astimezone(UTC).time(),
        GroupWebhooksModel.scheduled_time <= end_dt.astimezone(UTC).time(),
        GroupWebhooksModel.group_id == self.group_id,
        GroupWebhooksModel.household_id == self.household_id,
    )
```

**跨午夜匹配问题**: 当 `start_dt.time()` > `end_dt.time()` 时（即跨午夜），SQL 查询条件 `time > start.time AND time <= end.time` 永远不成立。

**示例**:
```
start_dt = 2026-06-05 23:30:00 UTC → time = 23:30:00
end_dt   = 2026-06-06 00:30:00 UTC → time = 00:30:00

查询条件: scheduled_time > '23:30:00' AND scheduled_time <= '00:30:00'
→ 没有任何 time 值能同时满足 >23:30:00 和 <=00:30:00
→ 结果: 没有 Webhook 被匹配
```

**结论**: 当 `post_group_webhooks` 的执行时间跨午夜时，如果 Webhook 配置的 `scheduled_time` 落在跨午夜的时间窗口内（如 00:00），该 Webhook **不会被触发**。

**3. EventWebhookData 数据结构**

**文件**: `mealie/services/event_bus_service/event_types.py#L173-L176`

```python
class EventWebhookData(EventDocumentDataBase):
    webhook_start_dt: datetime   # 完整 datetime，含时分秒
    webhook_end_dt: datetime     # 完整 datetime，含时分秒
    webhook_body: Any = None     # 存储查询结果（List[ReadPlanEntry]）
```

注意：`webhook_start_dt` 和 `webhook_end_dt` 保存的是完整的 `datetime`，但实际查询时会被转换为 `date`。

---

### 6.5.2 前端日期范围与查询方式

**前端日期范围计算**: `frontend/app/pages/household/mealplan/planner.vue#L172-L199`

```typescript
const weekRange = computed(() => {
  const sorted = [...state.value.range].sort((a, b) => a.getTime() - b.getTime());
  const start = sorted[0];
  const end = sorted[sorted.length - 1];
  if (start && end) {
    return { start, end };
  }
  return {
    start: addDays(new Date(), adjustForToday(-numberOfDaysPast.value)),
    end: addDays(new Date(), adjustForToday(numberOfDays.value)),
  };
});

// URL query 同步
watch(weekRange, (newRange) => {
  router.replace({
    query: {
      start: format(newRange.start, "yyyy-MM-dd"),  // 日期格式，无时间
      end: format(newRange.end, "yyyy-MM-dd"),      // 日期格式，无时间
    },
  });
}, { immediate: true });
```

**前端按日期过滤**: `frontend/app/pages/household/mealplan/planner.vue#L203-L208`

```typescript
function filterMealByDate(date: Date) {
  if (!mealplans.value) return [];
  return mealplans.value.filter((meal) => {
    const mealDate = parseISO(meal.date);  // meal.date 是 "yyyy-MM-dd" 字符串
    return isSameDay(mealDate, date);      // 比较日期部分
  });
}
```

**后端日期查询真实入口**: `mealie/routes/households/controller_mealplan.py#L76-L100`

```python
@router.get("", response_model=PlanEntryPagination)
def get_all(
    self,
    q: PaginationQuery = Depends(PaginationQuery),
    start_date: date | None = None,      # date 类型，无时分秒
    end_date: date | None = None,        # date 类型，无时分秒
):
    # 将日期范围拼接为 query_filter 字符串
    if start_date or end_date:
        if not start_date:
            date_filter = f"date <= {end_date}"
        elif not end_date:
            date_filter = f"date >= {start_date}"
        else:
            date_filter = f"date >= {start_date} AND date <= {end_date}"

        # 与已有 query_filter 合并
        if q.query_filter:
            q.query_filter = f"({q.query_filter}) AND ({date_filter})"
        else:
            q.query_filter = date_filter

    # 通过通用分页方法查询，不调用 get_meals_by_date_range
    return self.repo.page_all(pagination=q)
```

**前端调用参数**: `frontend/app/composables/use-group-mealplan.ts#L42-L46`

```typescript
const query = {
  start_date: format(range.value.start, "yyyy-MM-dd"),  // 字符串日期
  end_date: format(range.value.end, "yyyy-MM-dd"),      // 字符串日期
};
const { data } = await api.mealplans.getAll(1, -1, { 
  start_date: query.start_date, 
  end_date: query.end_date 
});
```

**日期查询流程**:
```
前端 URL: /planner/view?start=2026-06-01&end=2026-06-07
  → API: GET /api/households/mealplans?start_date=2026-06-01&end_date=2026-06-07
  → 后端: 拼接为 query_filter = "date >= 2026-06-01 AND date <= 2026-06-07"
  → 调用: repo.page_all(pagination=q)
  → 底层: 通过 PaginationQuery 解析过滤条件查询
```

**前后端日期查询对比**:

| 场景 | 前端API调用 | 后端Webhook调用 |
|------|------------|----------------|
| 方法 | `api.mealplans.getAll()` | `repos.meals.get_meals_by_date_range()` |
| 参数 | `start_date: string (yyyy-MM-dd)` | `start_date: datetime` |
| | `end_date: string (yyyy-MM-dd)` | `end_date: datetime` |
| 处理方式 | 拼接为 `query_filter` 字符串 | 直接 `datetime.date()` 转换 |
| 查询方法 | `repo.page_all()` | 直接 SQL `select` |
| 返回格式 | `PlanEntryPagination`（含 items） | `list[ReadPlanEntry]` |

---

### 6.5.3 相对路径引用说明

本文档中所有代码引用均采用**仓库根目录相对路径**格式：

| 格式 | 示例 | 说明 |
|------|------|------|
| 文件 | `mealie/repos/repository_meals.py` | 相对于仓库根目录的文件路径 |
| 行号 | `mealie/repos/repository_meals.py#L23-L33` | 具体代码行范围 |

**路径转换规则**:
- Windows 路径的 `\` 统一转换为 `/`
- 省略仓库根目录，从 `mealie/` 或 `frontend/` 开始
- 行号引用格式为 `#L<start>-L<end>`

---

### 6.6 后端事件总线

**文件**: `mealie/services/event_bus_service/event_types.py`

**三种排期相关事件类型**:
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

**发布时机** `mealie/routes/households/controller_mealplan.py`:
- 创建成功后: `mealie/routes/households/controller_mealplan.py#L107-L120`
- 更新成功后: `mealie/routes/households/controller_mealplan.py#L181-L194`
- 删除成功后: `mealie/routes/households/controller_mealplan.py#L202-L215`

### 6.7 通知监听系统

**文件**: `mealie/services/event_bus_service/event_bus_listeners.py`

**两类监听器**:

1. **AppriseEventListener** `mealie/services/event_bus_service/event_bus_listeners.py#L72-L131`
   - 查询启用的通知器配置
   - 检查是否订阅了对应事件类型
   - 通过 Apprise 推送通知（支持邮件、Telegram、Slack 等）

2. **WebhookEventListener** `mealie/services/event_bus_service/event_bus_listeners.py#L134-L179`
   - 定时触发的 Webhook 任务
   - 可按 `mealplan` 类型推送整周排期数据

**注意**: 事件总线目前仅用于通知，**不用于前端实时数据同步**。前端仍采用手动刷新模式，无 WebSocket 实时推送。

### 6.8 订阅配置

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
| 数据模型 | `mealie/db/models/household/mealplan.py` | DB 模型定义 |
| 数据结构 | `mealie/schema/meal_plan/new_meal.py` | 后端请求/响应 Schema |
| 数据访问 | `mealie/repos/repository_meals.py` | Repository 层 |
| API 路由 | `mealie/routes/households/controller_mealplan.py` | FastAPI 控制器 |
| 事件类型 | `mealie/services/event_bus_service/event_types.py` | 事件数据结构 |
| 事件监听 | `mealie/services/event_bus_service/event_bus_listeners.py` | 通知监听器 |
| 时间线生成 | `mealie/services/scheduler/tasks/create_timeline_events.py` | 每日定时任务 |
| Webhook 推送 | `mealie/services/scheduler/tasks/post_webhooks.py` | 每分钟定时任务 |
| last_made 触发器 | `mealie/db/models/household/household_to_recipe.py` | DB 级别自动同步 |
| 家庭服务 | `mealie/services/household_services/household_service.py` | HouseholdService 封装 |
| 调度器注册 | `mealie/app.py#L124-L144` | 定时任务注册入口 |
| 前端类型 | `frontend/app/lib/api/types/meal-plan.ts` | TypeScript 类型 |
| 前端API | `frontend/app/lib/api/user/group-mealplan.ts` | API 客户端 |
| 状态管理 | `frontend/app/composables/use-group-mealplan.ts` | Composable |
| 主页面 | `frontend/app/pages/household/mealplan/planner.vue` | 主容器组件 |
| 视图模式 | `frontend/app/pages/household/mealplan/planner/view.vue` | 只读视图 |
| 编辑模式 | `frontend/app/pages/household/mealplan/planner/edit.vue` | 可编辑视图 |
| API 基类 | `frontend/app/lib/api/base/base-clients.ts` | CRUD 基类 |
| API 聚合 | `frontend/app/lib/api/client-user.ts` | 用户 API 聚合 |

---

## 八、架构总结

### 8.1 设计优点

1. **分层清晰**: Model → Schema → Repository → Controller → API Client → Composable → Component，职责明确
2. **类型安全**: 前后端类型通过 `pydantic2ts` 自动生成，保持一致
3. **查询优化**: 使用 `selectinload` 预加载关联数据，避免 N+1 问题
4. **事件驱动**: 后端操作后发布事件，便于扩展通知和集成
5. **响应式设计**: 前端使用 Vue 3 Composition API，日期范围变化自动触发刷新
6. **幂等性保障**: 时间线事件生成有重复检查，避免每日任务重复创建
7. **双表同步**: `last_made` 采用家庭级别 + 全局级别双表设计，通过 DB 触发器自动同步
8. **时间窗口设计**: Webhook 推送使用滑动时间窗口（上次运行时间 ~ 当前时间），确保不遗漏

### 8.2 可改进点

1. **状态同步**: 当前采用「每次修改全量刷新」模式，可优化为:
   - 乐观更新: 本地先更新状态，API 成功后再同步
   - 增量更新: 后端返回修改后的单个对象，前端局部更新
   - 实时推送: 引入 WebSocket/SSE，接收后端事件推送

2. **缓存策略**: 相同日期范围的查询结果可缓存，避免重复请求

3. **编辑模式本地状态**: 当前拖拽修改后立即调用 API，可增加本地暂存 + 批量保存模式

4. **Webhook 取数范围**: 当前按日期级别取整 day 数据，可优化为按时间窗口取增量数据

5. **last_made 实时性**: 当前依赖每日定时任务更新，可在排期创建/更新时立即更新
