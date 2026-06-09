# Mealie 餐食计划（Meal Plan）规则与随机推荐代码分析

## 1. 整体架构

餐食计划系统由以下几个核心模块组成：

| 层级 | 文件 | 说明 |
|------|------|------|
| API 控制器 | [controller_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/routes/households/controller_mealplan.py) | 餐食计划入口，含随机推荐 API |
| API 控制器 | [controller_mealplan_rules.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/routes/households/controller_mealplan_rules.py) | 餐食计划规则的 CRUD |
| 数据仓库 | [repository_meals.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_meals.py) | 餐食计划条目数据访问 |
| 数据仓库 | [repository_meal_plan_rules.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_meal_plan_rules.py) | 规则数据访问与匹配 |
| 数据仓库 | [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_recipes.py) | 食谱查询，含分页与过滤 |
| 数据仓库 | [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py) | 通用仓库，含随机排序实现 |
| 数据库模型 | [mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/db/models/household/mealplan.py) | `GroupMealPlan` 和 `GroupMealPlanRules` 表定义 |
| Schema | [new_meal.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/new_meal.py) | 餐食条目请求/响应结构 |
| Schema | [plan_rules.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py) | 规则结构定义与校验 |

---

## 2. 计划生成流程（Plan Generation）

### 2.1 API 入口

随机推荐的 API 入口位于 [controller_mealplan.py#L129-L171](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/routes/households/controller_mealplan.py#L129-L171)：

```python
@router.post("/random", response_model=ReadPlanEntry)
def create_random_meal(self, data: CreateRandomEntry):
    random_recipes = self._get_random_recipes_from_mealplan(data.date, data.entry_type)
    if not random_recipes:
        raise HTTPException(404, ...)  # 无匹配食谱

    recipe = random_recipes[0]
    result = self.mixins.create_one(SavePlanEntry(...))
    # 发布事件 mealplan_entry_created
    return result
```

请求体 `CreateRandomEntry` 仅包含两个字段：
- `date`：目标日期
- `entry_type`：餐次类型（breakfast/lunch/dinner/side/snack/drink/dessert），默认为 dinner

### 2.2 核心流程 `_get_random_recipes_from_mealplan`

位于 [controller_mealplan.py#L49-L74](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/routes/households/controller_mealplan.py#L49-L74)，整体步骤如下：

```
输入: plan_date (date), entry_type (PlanEntryType), limit (int=1)
  │
  ├─ 1. 获取匹配的规则集合
  │     rules = repos.group_meal_plan_rules.get_rules(day, entry_type)
  │
  ├─ 2. 构建跨家庭食谱仓库（household_id=None）
  │     cross_household_recipes = get_repositories(..., household_id=None).recipes.by_user(user.id)
  │
  ├─ 3. 组装查询过滤字符串
  │     所有规则的 query_filter_string 用 AND 连接
  │     qf_string = " AND ".join( [f"({rule.query_filter_string})" ...] )
  │
  └─ 4. 分页查询 + 随机排序
        recipes_data = cross_household_recipes.page_all(
            pagination=PaginationQuery(
                page=1,
                per_page=limit,
                query_filter=qf_string,
                order_by="random",
                pagination_seed=repo._random_seed(),  # 当前 UTC 时间字符串
            )
        )
输出: list[Recipe]
```

**关键点**：
- `household_id=None` 意味着默认搜索**用户所在 group 下所有家庭**的食谱，除非规则中显式指定了家庭过滤
- `by_user(user.id)` 注入用户上下文，用于计算 `last_made`、`rating` 等用户相关字段别名
- limit 默认值为 1，每次取 1 条随机结果

---

## 3. 规则约束（Rules Constraints）

### 3.1 规则数据模型

数据库表 `group_meal_plan_rules` 定义于 [mealplan.py#L30-L52](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/db/models/household/mealplan.py#L30-L52)：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | GUID | 主键 |
| `group_id` | GUID | 所属 group（必填） |
| `household_id` | GUID | 所属 household（可选） |
| `day` | String | 星期几：`monday`~`sunday` 或 `unset` |
| `entry_type` | String | 餐次类型：`breakfast`/`lunch`/... 或空字符串 `""` |
| `query_filter_string` | String | **核心过滤表达式**，使用 Mealie 的查询过滤语法 |
| `categories` | M:N | 旧版分类过滤（已弃用，推荐使用 query_filter_string） |
| `tags` | M:N | 旧版标签过滤（已弃用） |
| `households` | M:N | 旧版家庭过滤（已弃用） |

### 3.2 规则匹配逻辑 `get_rules`

位于 [repository_meal_plan_rules.py#L10-L31](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_meal_plan_rules.py#L10-L31)：

```python
def get_rules(self, day: PlanRulesDay, entry_type: PlanRulesType) -> list[PlanRulesOut]:
    stmt = select(GroupMealPlanRules).filter(
        or_(
            GroupMealPlanRules.day == day,            # 精确匹配星期
            GroupMealPlanRules.day.is_(None),         # NULL 视为通配
            GroupMealPlanRules.day == PlanRulesDay.unset.value,  # "unset" 视为通配
        ),
        or_(
            GroupMealPlanRules.entry_type == entry_type,       # 精确匹配餐次
            GroupMealPlanRules.entry_type.is_(None),           # NULL 视为通配
            GroupMealPlanRules.entry_type == PlanRulesType.unset.value,  # "unset" 视为通配
        ),
    )
    # 同时按 group_id 和 household_id 过滤
    if self.group_id:
        stmt = stmt.filter(GroupMealPlanRules.group_id == self.group_id)
    if self.household_id:
        stmt = stmt.filter(GroupMealPlanRules.household_id == self.household_id)
```

**匹配规则解读**：
- `day` 和 `entry_type` 各自使用 `OR` 逻辑：**精确匹配** 或 **NULL** 或 **"unset"** 均算匹配
- 换言之，`day="unset"` 的规则对**所有星期**生效；`entry_type="unset"` 的规则对**所有餐次**生效
- 最终的规则集合是满足条件的**所有规则的并集**

### 3.3 日期转星期

`PlanRulesDay.from_date(date)` 位于 [plan_rules.py#L27-L33](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L27-L33)：

```python
@staticmethod
def from_date(date: datetime.date):
    try:
        return PlanRulesDay[(date.strftime("%A").lower())]
    except KeyError:
        return PlanRulesDay.unset
```

将 Python 的 `date.strftime("%A")`（如 `"Monday"`）转为小写后匹配枚举值。

### 3.4 规则过滤表达式的合并

回到 `_get_random_recipes_from_mealplan`：

```python
qf_string = " AND ".join([f"({rule.query_filter_string})" for rule in rules if rule.query_filter_string])
```

- 每条规则的 `query_filter_string` 会被括号包裹
- 多条规则之间用 **`AND`** 连接
- 空字符串的规则会被跳过
- 如果**没有任何规则**或所有规则的过滤字符串为空，则 `qf_string` 为空，相当于**不过滤**，从所有食谱中随机选

### 3.5 查询过滤字符串的校验

规则创建/更新时会在校验器中验证 `query_filter_string` 的合法性，位于 [plan_rules.py#L52-L63](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L52-L63)：

```python
@field_validator("query_filter_string")
def validate_query_filter_string(cls, value: str) -> str:
    builder = QueryFilterBuilder(value)
    try:
        builder.filter_query(sa.select(RecipeModel), RecipeModel)
    except Exception as e:
        raise ValueError("Invalid query filter string") from e
    return value
```

通过实际构造一次 SQL 查询来验证语法正确性。

### 3.6 规则约束示例（来自测试）

来自 [test_group_mealplan.py#L35-L60](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py#L35-L60) 的 `create_rule` 辅助函数展示了规则过滤字符串的构造方式：

```python
qf_parts: list[str] = []
if tags:
    qf_parts.append(f"tags.id CONTAINS ALL [{','.join([str(tag.id) for tag in tags])}]")
if categories:
    qf_parts.append(f"recipe_category.id CONTAINS ALL [{','.join([str(cat.id) for cat in categories])}]")
if households:
    qf_parts.append(f"household_id IN [{','.join([str(household.id) for household in households])}]")
query_filter_string = " AND ".join(qf_parts)
```

查询过滤语法示例：
- `tags.id CONTAINS ALL ["uuid1","uuid2"]` — 食谱必须同时包含所有指定标签
- `recipe_category.id IN ["uuid1"]` — 食谱分类属于指定集合
- `household_id IN ["uuid1","uuid2"]` — 食谱来自指定家庭
- `created_at >= "2024-01-01"` — 按创建日期过滤
- 多个条件用 `AND` / `OR` 组合

---

## 4. 候选食谱选择（Candidate Recipe Selection）

### 4.1 `page_all` 食谱分页查询

位于 [repository_recipes.py#L220-L293](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_recipes.py#L220-L293)，核心流程：

1. 基础查询：`SELECT ... FROM recipes WHERE recipes.household_id IS NOT NULL`
2. 应用 group_id / household_id 过滤（`_filter_builder`）
3. 如果提供了 `cookbook`，将 cookbook 的 `query_filter_string` 合并进 `pagination.query_filter`
4. 否则按参数构造 categories/tags/tools/foods/households 过滤条件
5. 应用全文搜索（search 参数）
6. 默认排序（无搜索时）：`created_at` 降序
7. 调用 `add_pagination_to_query` 处理查询过滤、计数、分页、排序

### 4.2 查询过滤应用 `add_pagination_to_query`

位于 [repository_generic.py#L357-L405](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py#L357-L405)：

```python
def add_pagination_to_query(self, query: Select, pagination: PaginationQuery):
    # 1. 解析并应用 query_filter
    if pagination.query_filter:
        query_filter_builder = QueryFilterBuilder(pagination.query_filter)
        query = query_filter_builder.filter_query(query, model=self.model, column_aliases=self.column_aliases)

    # 2. 执行 COUNT 查询得到总数
    count_query = select(func.count()).select_from(query.order_by(None).distinct().subquery())
    count = self.session.scalar(count_query)

    # 3. 处理分页参数（per_page=-1 表示全部，page=-1 表示最后一页）
    ...

    # 4. 应用 ORDER BY（含随机排序）
    query = self.add_order_by_to_query(query, pagination)

    # 5. 应用 LIMIT / OFFSET
    return query.offset(...).limit(...), count, total_pages
```

### 4.3 随机排序实现 `add_order_by_to_query`

位于 [repository_generic.py#L432-L482](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py#L432-L482)，当 `order_by="random"` 时：

```python
elif request_query.order_by == "random":
    # 不使用数据库的 RANDOM() 函数，而是在应用层实现以跨数据库兼容
    temp_query = query.with_only_columns(self.model.id)
    allids = self.session.execute(temp_query).scalars().all()  # 取出所有匹配行的 id
    if not allids:
        return query

    order = list(range(len(allids)))
    random.seed(request_query.pagination_seed)  # 使用种子保证可复现
    random.shuffle(order)
    random_dict = dict(zip(allids, order, strict=True))
    case_stmt = case(random_dict, value=self.model.id)  # 构造 SQL CASE 表达式
    return query.order_by(case_stmt)
```

**随机排序设计要点**：

| 方面 | 说明 |
|------|------|
| **实现方式** | 不在 SQL 层用 `ORDER BY RANDOM()`，而是先查所有 id → Python `random.shuffle` → 生成 SQL `CASE` 表达式排序 |
| **跨数据库兼容** | 兼容 PostgreSQL、SQLite、MySQL 等不同数据库的随机函数差异 |
| **可复现性** | 使用 `pagination_seed` 作为 `random.seed()`，相同种子产生相同随机顺序，便于分页稳定 |
| **种子来源** | `_random_seed()` 返回当前 UTC 时间字符串 `str(datetime.now(tz=UTC))`，参见 [repository_generic.py#L72-L73](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py#L72-L73) |
| **性能注意** | 需先执行一次查询取出所有匹配 id，数据量大时可能有性能开销 |

### 4.4 选择最终食谱

回到 `create_random_meal`：

```python
random_recipes = self._get_random_recipes_from_mealplan(data.date, data.entry_type)
recipe = random_recipes[0]  # 取第一条（limit=1）
```

因为 `per_page=limit=1`，分页查询只返回随机排序后的第一条，直接取 `[0]` 即为本次随机推荐的食谱。

---

## 5. 完整调用链路图

```
POST /households/mealplans/random
  │
  ▼
GroupMealplanController.create_random_meal(CreateRandomEntry{date, entry_type})
  │
  ├─► _get_random_recipes_from_mealplan(date, entry_type)
  │     │
  │     ├─ 1. PlanRulesDay.from_date(date) → monday/tuesday/...
  │     │
  │     ├─ 2. RepositoryMealPlanRules.get_rules(day, entry_type)
  │     │     └─ SELECT * FROM group_meal_plan_rules
  │     │        WHERE (day=? OR day IS NULL OR day='unset')
  │     │          AND (entry_type=? OR entry_type IS NULL OR entry_type='unset')
  │     │          AND group_id=? AND household_id=?
  │     │
  │     ├─ 3. 构建 qf_string = " AND ".join([(rule.query_filter_string) ...])
  │     │
  │     ├─ 4. RepositoryRecipes.page_all(PaginationQuery{
  │     │        page=1, per_page=1,
  │     │        query_filter=qf_string,
  │     │        order_by="random",
  │     │        pagination_seed=str(datetime.now(UTC))
  │     │     })
  │     │     │
  │     │     ├─ a. _filter_builder() → group_id, household_id 过滤
  │     │     ├─ b. add_pagination_to_query()
  │     │     │     ├─ QueryFilterBuilder(qf_string).filter_query()
  │     │     │     ├─ COUNT 查询
  │     │     │     └─ add_order_by_to_query(order_by="random")
  │     │     │           ├─ 查询所有匹配 recipe.id
  │     │     │           ├─ random.seed(pagination_seed); random.shuffle()
  │     │     │           └─ ORDER BY CASE(id, ...)
  │     │     └─ c. LIMIT 1 → 返回 1 条 Recipe
  │     │
  │     └─ 返回 list[Recipe]（长度 ≤ 1）
  │
  ├─► 无匹配 → HTTP 404 "mealplan.no-recipes-match-your-rules"
  │
  ├─► RepositoryMeals.create(SavePlanEntry{date, entry_type, recipe_id, ...})
  │     └─ INSERT INTO group_meal_plans ...
  │
  └─► 发布事件 mealplan_entry_created → 返回 ReadPlanEntry
```

---

## 6. 边界问题详解

### 6.1 餐次类型（entry_type）未设置值与空值的区别

餐次类型的"未设置"在代码中存在两种不同的表示，它们在数据库存储和规则匹配时行为**截然不同**，这是最容易混淆的边界点。

#### 6.1.1 三层定义对比

| 层级 | 定义位置 | 默认值 / 可选值 | 说明 |
|------|---------|----------------|------|
| **枚举 Schema** | [plan_rules.py#L36-L44](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L36-L44) | `PlanRulesType.unset = "unset"` | Pydantic 枚举，API 输入层使用 |
| **Create Schema** | [plan_rules.py#L47-L50](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L47-L50) | `entry_type: PlanRulesType = PlanRulesType.unset` | 创建规则时若不指定，默认为 `"unset"` |
| **数据库模型** | [mealplan.py#L40-L42](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/db/models/household/mealplan.py#L40-L42) | `mapped_column(String, nullable=False, default="")` | 数据库字段默认值是**空字符串 `""`**，且 NOT NULL |

#### 6.1.2 规则匹配逻辑对各值的处理

在 [repository_meal_plan_rules.py#L17-L21](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_meal_plan_rules.py#L17-L21) 中：

```python
or_(
    GroupMealPlanRules.entry_type == entry_type,       # ① 精确匹配目标餐次
    GroupMealPlanRules.entry_type.is_(None),           # ② NULL 视为通配
    GroupMealPlanRules.entry_type == PlanRulesType.unset.value,  # ③ "unset" 视为通配
)
```

三种 `entry_type` 存储值的匹配行为：

| 存储值 | 是否匹配任意餐次 | 是否匹配具体餐次（如 dinner） | 产生方式 |
|--------|-----------------|------------------------------|---------|
| `"unset"` | ✅ 是（条件③） | ✅ 是（通配） | 通过 API 正常创建规则且未指定 entry_type |
| `NULL` | ✅ 是（条件②） | ✅ 是（通配） | 数据库直接操作且字段允许 NULL（但模型定义了 NOT NULL，实际不应出现） |
| `""`（空字符串） | ❌ 否 | ❌ 否（三条均不满足） | **绕过 API 直接写库**且使用了 DB 层默认值 |
| `"dinner"` 等具体值 | ❌ 否 | ✅ 仅匹配 dinner（条件①） | 通过 API 显式指定 |

#### 6.1.3 ⚠️ 关键风险：空字符串 `""` 导致规则"静默失效"

由于数据库模型的 `default=""` 与匹配逻辑**不一致**，若数据行的 `entry_type` 存的是空字符串 `""`，则该规则在 `get_rules()` 查询时**不会被任何餐次匹配到**——既不满足精确匹配，也不满足 NULL，也不等于 `"unset"`。

**正常流程下不会出现此问题**：因为通过 API 创建规则时，Pydantic Schema 的默认值是 `PlanRulesType.unset`（值为 `"unset"`），写入数据库的值也是 `"unset"`。只有绕过 API 直接操作数据库且未显式指定 entry_type 时，才会触发 DB 默认值 `""`，导致规则失效。

`day` 字段的模型定义是 `default="unset"`（[mealplan.py#L37-L39](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/db/models/household/mealplan.py#L37-L39)），与枚举一致，不存在此问题。只有 `entry_type` 的 DB 默认值是 `""`，存在不一致。

---

### 6.2 规则所属家庭与跨家庭候选食谱的关系

家庭过滤在整个链路中分为**两个独立的层面**："规则的可见范围"和"候选食谱的搜索范围"，二者互不隐含，需要分别理解。

#### 6.2.1 层面一：规则查询时的家庭过滤（哪些规则生效）

在 `RepositoryMealPlanRules.get_rules()` 中，[repository_meal_plan_rules.py#L24-L27](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_meal_plan_rules.py#L24-L27)：

```python
if self.group_id:
    stmt = stmt.filter(GroupMealPlanRules.group_id == self.group_id)
if self.household_id:
    stmt = stmt.filter(GroupMealPlanRules.household_id == self.household_id)
```

而 `group_meal_plan_rules` 仓库是通过 [repository_factory.py#L303-L312](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_factory.py#L303-L312) 创建的，传入了当前请求的 `household_id`：

```python
def group_meal_plan_rules(self) -> RepositoryMealPlanRules:
    return RepositoryMealPlanRules(
        ...,
        group_id=self.group_id,
        household_id=self.household_id,   # ← 当前用户所在家庭
    )
```

**结论**：规则匹配阶段只检索**当前家庭（household_id）**下定义的规则。其他家庭的规则即使在同一个 group 内也不会生效。每条规则归属哪个家庭，由 `GroupMealPlanRules.household_id` 字段决定（创建规则时写入）。

#### 6.2.2 层面二：食谱搜索时的跨家庭范围（从哪些食谱里选）

在 `_get_random_recipes_from_mealplan()` 中，[controller_mealplan.py#L60-L62](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/routes/households/controller_mealplan.py#L60-L62) 显式构建了一个 `household_id=None` 的食谱仓库：

```python
cross_household_recipes = get_repositories(
    self.session, group_id=self.group_id, household_id=None  # ← household_id 传 None
).recipes.by_user(self.user.id)
```

结合 `_filter_builder()` 的逻辑，[repository_generic.py#L94-L102](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py#L94-L102)：

```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:      # ← None 为假值，不会加入 household_id 过滤
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

**结论**：食谱候选池默认是**当前 group 下所有家庭**的食谱（`group_id` 过滤保留，`household_id` 过滤被跳过）。

#### 6.2.3 层面三：规则的 query_filter_string 可进一步限制家庭

虽然候选池默认跨家庭，但规则的过滤表达式可以通过 `household_id IN [...]` 再次收窄范围。来自测试的示例 [test_group_mealplan.py#L303-L305](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py#L303-L305)：

```python
qf_parts.append(f"household_id IN [{','.join([str(household.id) for household in households])}]")
```

#### 6.2.4 三层家庭过滤总结

| 层级 | 位置 | 过滤对象 | 行为 |
|------|------|---------|------|
| ① 规则归属 | `get_rules()` 的 `household_id` 过滤 | **规则** | 仅当前家庭定义的规则才会被加载 |
| ② 候选池范围 | 食谱仓库 `household_id=None` | **食谱** | 默认从 group 内所有家庭的食谱中挑选 |
| ③ 规则内过滤 | `query_filter_string` 中的 `household_id IN [...]` | **食谱** | 可在规则中进一步限制只从某些家庭选食谱 |

三者叠加的最终效果：
- **从哪里选规则**：只能用当前家庭创建的规则
- **从哪里选食谱**：默认所有家庭，可被规则内 `household_id IN` 收窄

测试验证参见：
- [test_group_mealplan.py#L279-L292](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py#L279-L292) `test_get_mealplan_with_rules_includes_other_households`：验证跨家庭食谱默认可见
- [test_group_mealplan.py#L295-L313](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py#L295-L313) `test_get_mealplan_with_rules_households_filter`：验证规则内 household_id IN 可限制范围

---

### 6.3 过滤表达式校验对随机推荐的影响

`query_filter_string` 的校验发生在**多个阶段**，每个阶段的失败后果不同，需要区分理解。

#### 6.3.1 校验发生的三个阶段

| 阶段 | 触发时机 | 校验位置 | 失败后果 |
|------|---------|---------|---------|
| **① 创建/更新规则时** | POST/PUT `/households/mealplans/rules` | [plan_rules.py#L52-L63](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L52-L63) `PlanRulesCreate.validate_query_filter_string` | 返回 **HTTP 422**，规则不会被写入数据库 |
| **② 读取规则返回前端时** | GET `/households/mealplans/rules` | [plan_rules.py#L82-L90](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/plan_rules.py#L82-L90) `PlanRulesOut.validate_query_filter` | **静默降级**：`query_filter` 字段返回空对象 `{}`，但不抛出异常，规则本身仍可返回 |
| **③ 随机推荐执行查询时** | POST `/households/mealplans/random` | [repository_generic.py#L367-L374](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/repos/repository_generic.py#L367-L374) `add_pagination_to_query` | 返回 **HTTP 400**，整个推荐请求失败 |

#### 6.3.2 阶段①详解：规则写入前的强校验

创建规则时，Pydantic 的 `@field_validator` 会实例化 `QueryFilterBuilder` 并**实际执行一次查询构建**：

```python
@field_validator("query_filter_string")
def validate_query_filter_string(cls, value: str) -> str:
    builder = QueryFilterBuilder(value)
    try:
        builder.filter_query(sa.select(RecipeModel), RecipeModel)  # 真正跑一遍解析+SQL构造
    except Exception as e:
        raise ValueError("Invalid query filter string") from e
    return value
```

校验内容包括（来自 [builder.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/services/query_filter/builder.py) 的解析逻辑）：
- 字段名是否在 RecipeModel 上真实存在且可过滤
- 操作符与值类型是否匹配（如 `IN` 操作符必须跟列表值、`IS` 只能跟 `NULL`）
- UUID 值格式、日期值格式是否合法

测试 [test_group_mealplan_rules.py#L139-L172](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan_rules.py#L139-L172) 验证了多种非法输入（不存在字段、格式错误的 UUID、格式错误的日期）均返回 422。

**作用**：从源头杜绝非法过滤表达式进入数据库。

#### 6.3.3 阶段②详解：规则读取时的静默降级

`PlanRulesOut` 是用于读取返回的 Schema，它对 `query_filter_string` 做了两个处理：
1. `validate_query_filter_string` 直接 `return value`，**跳过校验**（避免读操作因历史脏数据失败）
2. `validate_query_filter` 将 `query_filter_string` 解析为结构化的 `QueryFilterJSON` 供前端展示，解析失败时返回空对象并记日志：

```python
@field_validator("query_filter", mode="before")
def validate_query_filter(cls, _, info: ValidationInfo) -> QueryFilterJSON:
    try:
        query_filter_string: str = info.data.get("query_filter_string") or ""
        builder = QueryFilterBuilder(query_filter_string)
        return builder.as_json_model()
    except Exception:
        logger.exception(f"Invalid query filter string: {query_filter_string}")
        return QueryFilterJSON()  # ← 空对象，不抛出异常
```

**作用**：历史脏数据（如代码升级后过滤语法变更导致旧表达式失效）不会影响规则列表的读取，前端仅会看到 `queryFilter.parts` 为空。

#### 6.3.4 阶段③详解：随机推荐执行时的运行时校验

在 `add_pagination_to_query()` 中拼接最终 SQL 时：

```python
if pagination.query_filter:
    try:
        query_filter_builder = QueryFilterBuilder(pagination.query_filter)
        query = query_filter_builder.filter_query(query, model=self.model, column_aliases=self.column_aliases)
    except ValueError as e:
        self.logger.error(e)
        raise HTTPException(status_code=400, detail=str(e)) from e  # ← HTTP 400
```

虽然阶段①已保证新写入的规则合法，但以下场景仍可能在运行时触发此错误：
- **数据库被直接修改**：绕过 API 写入了非法 `query_filter_string`
- **代码版本升级**：新版本的 `QueryFilterBuilder` 对字段/操作符的约束发生变化，旧规则不再兼容
- **多规则 AND 拼接后的副作用**：单条规则合法，但拼接后产生语法歧义（理论上不太可能，因为每条都加了括号）

**影响**：整个随机推荐请求失败，用户看到 400 错误，而不是"无匹配食谱"的 404。

#### 6.3.5 校验链路总结

```
创建规则 POST /rules
  │
  ├─ 阶段① PlanRulesCreate.validate_query_filter_string
  │     校验通过 → 写入 DB
  │     校验失败 → HTTP 422（阻止写入）
  │
读取规则 GET /rules
  │
  └─ 阶段② PlanRulesOut.validate_query_filter
        解析成功 → 返回结构化 queryFilter
        解析失败 → 静默降级返回 {}，记录日志（不影响读取）

随机推荐 POST /mealplans/random
  │
  └─ 阶段③ RepositoryGeneric.add_pagination_to_query
        解析成功 → 正常执行查询 + 随机排序
        解析失败 → HTTP 400（整个推荐失败）
```

对随机推荐的影响可以概括为：**阶段①挡住大部分问题，阶段②保证读操作不崩溃，阶段③是最后一道防线但失败代价最高**。

---

## 7. 关键设计总结

### 7.1 规则生效机制
1. **规则匹配**：`day` 和 `entry_type` 采用**通配匹配**（精确值 / NULL / `"unset"` 均有效），使得通用规则（如"所有晚餐都要素食"）和特定规则（如"周一面吃意面"）可以共存
2. **多规则组合**：所有匹配的规则的 `query_filter_string` 使用 **AND** 连接，意味着**所有规则必须同时满足**
3. **无规则退化**：若无有效规则，则 `query_filter` 为空，从用户 group 下所有家庭的全部食谱中随机抽取

### 7.2 随机推荐机制
1. **过滤优先，随机其次**：先通过 `QueryFilterBuilder` 应用所有规则过滤，再在结果集内做随机排序
2. **应用层随机**：使用 Python `random` + SQL `CASE` 表达式实现，避免数据库函数差异，支持通过 `pagination_seed` 复现顺序
3. **跨家庭搜索**：默认搜索 group 内所有家庭的食谱，可通过规则中的 `household_id IN [...]` 限制范围

### 7.3 数据校验
1. **规则创建/更新时**：`query_filter_string` 会通过 `QueryFilterBuilder` 实际构造一次 SQL 查询做语法校验
2. **计划条目创建时**：`CreatePlanEntry` 要求 `recipe_id` 和 `title` 至少提供一个（参见 [new_meal.py#L41-L47](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/new_meal.py#L41-L47)）

---

## 8. 相关测试参考

- 餐食计划 CRUD + 随机推荐集成测试：[test_group_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py)
  - `test_get_mealplan_with_rules_categories_and_tags_filter` — 分类+标签联合过滤
  - `test_get_mealplan_with_rules_date_and_type_filter` — 不同日期和餐次的规则隔离
  - `test_get_mealplan_with_rules_includes_other_households` — 跨家庭食谱搜索
  - `test_get_mealplan_with_rules_households_filter` — 规则内限制家庭范围
- 餐食计划规则 CRUD + 校验测试：[test_group_mealplan_rules.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan_rules.py)
  - `test_group_mealplan_rules_validate_query_filter_string` — 非法过滤字符串返回 422
