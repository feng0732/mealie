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

## 6. 关键设计总结

### 6.1 规则生效机制
1. **规则匹配**：`day` 和 `entry_type` 采用**通配匹配**（精确值 / NULL / `"unset"` 均有效），使得通用规则（如"所有晚餐都要素食"）和特定规则（如"周一面吃意面"）可以共存
2. **多规则组合**：所有匹配的规则的 `query_filter_string` 使用 **AND** 连接，意味着**所有规则必须同时满足**
3. **无规则退化**：若无有效规则，则 `query_filter` 为空，从用户 group 下所有家庭的全部食谱中随机抽取

### 6.2 随机推荐机制
1. **过滤优先，随机其次**：先通过 `QueryFilterBuilder` 应用所有规则过滤，再在结果集内做随机排序
2. **应用层随机**：使用 Python `random` + SQL `CASE` 表达式实现，避免数据库函数差异，支持通过 `pagination_seed` 复现顺序
3. **跨家庭搜索**：默认搜索 group 内所有家庭的食谱，可通过规则中的 `household_id IN [...]` 限制范围

### 6.3 数据校验
1. **规则创建/更新时**：`query_filter_string` 会通过 `QueryFilterBuilder` 实际构造一次 SQL 查询做语法校验
2. **计划条目创建时**：`CreatePlanEntry` 要求 `recipe_id` 和 `title` 至少提供一个（参见 [new_meal.py#L41-L47](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/mealie/schema/meal_plan/new_meal.py#L41-L47)）

---

## 7. 相关测试参考

- 餐食计划 CRUD + 随机推荐集成测试：[test_group_mealplan.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan.py)
  - `test_get_mealplan_with_rules_categories_and_tags_filter` — 分类+标签联合过滤
  - `test_get_mealplan_with_rules_date_and_type_filter` — 不同日期和餐次的规则隔离
  - `test_get_mealplan_with_rules_includes_other_households` — 跨家庭食谱搜索
  - `test_get_mealplan_with_rules_households_filter` — 规则内限制家庭范围
- 餐食计划规则 CRUD + 校验测试：[test_group_mealplan_rules.py](file:///d:/fz/0601/solo-dogfeeding/code/120-mealie/tests/integration_tests/user_household_tests/test_group_mealplan_rules.py)
  - `test_group_mealplan_rules_validate_query_filter_string` — 非法过滤字符串返回 422
