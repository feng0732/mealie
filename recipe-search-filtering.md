# Recipe 搜索与过滤实现流程详解

本文档按照代码执行顺序，详细梳理 Mealie 项目中 Recipe 搜索与推荐过滤的完整实现流程。

---

## 一、整体调用链路

### 1.1 普通搜索用户 API 入口

请求从路由层的 `GET /api/recipes` 端点进入：

- 路由定义：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/recipe_crud_routes.py#L340-L395)
- Controller 类：`RecipeController` → 继承 `BaseRecipeController` → `BaseCrudController` → `BaseUserController`

核心调用链：

```
RecipeController.get_all()
  └─> self.group_recipes.by_user(self.user.id).page_all(...)
        └─> RepositoryRecipes.page_all()
              ├─> 基础权限过滤 (_filter_builder)
              ├─> CookBook 或 Taxonomy 条件过滤 (_build_recipe_filter)
              ├─> 文本搜索 (add_search_to_query)
              ├─> 分页 + query_filter + 排序 + 总数 (add_pagination_to_query)
              └─> 返回 RecipePagination
```

### 1.2 普通搜索公共 API (Explore) 入口

公共探索接口在：[controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/explore/controller_public_recipes.py#L30-L92)

与用户 API 的区别在于会额外注入公共权限过滤：

```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
if q.query_filter:
    q.query_filter = f"({q.query_filter}) AND {public_filter}"
else:
    q.query_filter = public_filter
```

### 1.3 推荐建议 (Suggestions) API 入口

详见本文第十一章。用户 API 在 [recipe_crud_routes.py L397-L413](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/recipe_crud_routes.py#L397-L413)，公共 API 在 [controller_public_recipes.py L94-L112](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/explore/controller_public_recipes.py#L94-L112)。

---

## 二、筛选参数的来源

所有筛选参数在路由层通过 FastAPI 的依赖注入系统接收，分为三类：

### 2.1 PaginationQuery（分页与排序参数）

定义于 [pagination.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/response/pagination.py#L46-L49)：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page` | int | 1 | 页码 |
| `per_page` | int | 50 | 每页条数（-1 表示全部） |
| `order_by` | str \| None | None | 排序字段，支持逗号分隔多字段；特殊值 "random" 表示随机排序 |
| `order_direction` | OrderDirection | desc | 排序方向 asc/desc |
| `order_by_null_position` | OrderByNullPosition \| None | None | NULL 值排序位置 first/last |
| `query_filter` | str \| None | None | 高级查询过滤表达式（类似 SQL WHERE 子句） |
| `pagination_seed` | str \| None | None | 随机排序时的种子（orderBy=random 时必填） |

### 2.2 RecipeSearchQuery（Recipe 专用搜索参数）

定义于 [pagination.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/response/pagination.py#L22-L29)：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cookbook` | UUID4 \| str \| None | None | 食谱书 ID 或 slug，使用其内置的 query_filter_string |
| `require_all_categories` | bool | **False** | categories 是否需要全部匹配（AND） |
| `require_all_tags` | bool | **False** | tags 是否需要全部匹配（AND） |
| `require_all_tools` | bool | **False** | tools 是否需要全部匹配（AND） |
| `require_all_foods` | bool | **False** | foods 是否需要全部匹配（AND） |
| `search` | str \| None | None | 文本搜索关键词 |

### 2.3 独立 Query 参数（Taxonomy 筛选）

在路由 [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/recipe_crud_routes.py#L346-L350) 中直接定义：

| 参数 | 类型 | 说明 |
|------|------|------|
| `categories` | list[UUID4 \| str] \| None | 分类 ID 或 slug 列表 |
| `tags` | list[UUID4 \| str] \| None | 标签 ID 或 slug 列表 |
| `tools` | list[UUID4 \| str] \| None | 工具 ID 或 slug 列表 |
| `foods` | list[UUID4 \| str] \| None | 食材 ID 列表 |
| `households` | list[UUID4 \| str] \| None | 家庭 ID 或 slug 列表 |

---

## 三、文本搜索的实现

### 3.1 搜索入口

在 [RepositoryRecipes.page_all()](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L267-L268)：

```python
if search:
    q = self.add_search_to_query(q, self.schema, search)
```

`add_search_to_query` 定义于 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_generic.py#L484-L486)：

```python
def add_search_to_query(self, query: Select, schema: type[Schema], search: str) -> Select:
    search_filter = SearchFilter(self.session, search, schema._normalize_search)
    return search_filter.filter_query_by_search(query, schema, self.model)
```

> **代码事实纠正**：方法名是 `filter_query_by_search`，不是 `filter_query_by_search_query`。定义于 [query_search.py L66](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/response/query_search.py#L66)。

### 3.2 SearchFilter 搜索类型决策

定义于 [query_search.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/response/query_search.py#L11-L67)。

搜索类型自动选择逻辑：

- **PostgreSQL 且搜索字符串不含引号** → 使用 `SearchType.fuzzy`（模糊匹配，trigram 相似度）
- **其他情况（SQLite / 含引号）** → 使用 `SearchType.tokenized`（分词 LIKE 匹配）

```python
if session.get_bind().name != "postgresql" or self.quoted_regex.search(search.strip()):
    self.search_type = SearchType.tokenized
else:
    self.search_type = SearchType.fuzzy
```

### 3.3 搜索预处理（文本规范化）

在 `SearchFilter._normalize_search()` 中：

1. 标点符号替换为空格（但保留单引号和双引号，因为引号用于精确短语匹配）
2. 根据 `_normalize_search` 标志决定是否做 `unidecode`（去除变音符号）并转小写

**RecipeSummary** 设置了 `_normalize_search = True`（定义于 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/recipe/recipe.py#L118)）。

### 3.4 搜索分词（_build_search_list）

1. 提取引号包裹的精确短语（保留为完整 token，不拆分）
2. 剩余部分按空格拆分为独立 token
3. 最终得到 `search_list: list[str]`

### 3.5 Recipe 专属搜索逻辑（filter_search_query）

Recipe 覆写了默认的搜索实现，定义于 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/recipe/recipe.py#L322-L383)。

搜索涉及的**规范化字段**（存储在数据库中，通过 SQLAlchemy event 自动更新）：

| 数据库字段 | 说明 | 自动更新事件 |
|-----------|------|-------------|
| `RecipeModel.name_normalized` | 菜谱名称规范化 | name set 事件 |
| `RecipeModel.description_normalized` | 描述规范化 | description set 事件 |
| `RecipeIngredientModel.note_normalized` | 食材备注规范化 | note set 事件 |
| `RecipeIngredientModel.original_text_normalized` | 食材原始文本规范化 | original_text set 事件 |

规范化逻辑：[SqlAlchemyBase.normalize()](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/db/models/_model_base.py#L29-L33)

#### Ingredient 规范化字段的更新事件详解

`RecipeIngredientModel` 的两个规范化字段通过 SQLAlchemy `@event.listens_for` 监听 set 事件自动更新，定义于 [ingredient.py L487-L500](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/db/models/recipe/ingredient.py#L487-L500)：

```python
@event.listens_for(RecipeIngredientModel.note, "set")
def receive_ingredient_note(target, value, oldvalue, initiator):
    if value is not None:
        target.note_normalized = RecipeIngredientModel.normalize(value)
    else:
        target.note_normalized = None

@event.listens_for(RecipeIngredientModel.original_text, "set")
def receive_ingredient_original_text(target, value, oldvalue, initiator):
    if value is not None:
        target.original_text_normalized = RecipeIngredientModel.normalize(value)
    else:
        target.original_text_normalized = None
```

`__init__` 构造函数中对 `note` 会直接写入 `note_normalized`，因为代码注释说明 auto_init 期间的赋值不一定触发事件；`original_text` 的稳定更新路径仍以 `original_text` set 事件为准，源码中另有 `orginal_text` 拼写分支，阅读时需要区分。

除食材外，其他 recipe 相关模型也具备规范化字段及 set 事件：
- `IngredientUnitModel`：name_normalized, plural_name_normalized, abbreviation_normalized, plural_abbreviation_normalized
- `IngredientFoodModel`：name_normalized, plural_name_normalized
- `IngredientFoodAliasModel` / `IngredientUnitAliasModel`：name_normalized

#### 3.5.1 Fuzzy 搜索（PostgreSQL trigram）

定义于 [recipe.py L333-L361](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/recipe/recipe.py#L333-L361)。

执行顺序与 pg_trgm 阈值影响：

```
1. 先对 RecipeIngredientModel 执行食材预查询（此时还未设置 pg_trgm 阈值）
2. 然后才设置 pg_trgm.word_similarity_threshold
3. 最后对 RecipeModel 的 name/description 执行主查询过滤
```

**关键细节：pg_trgm 阈值对食材预查询不生效**

```python
# 步骤 1：食材预查询（使用本次 SET 之前的 session 当前阈值）
ingredient_ids = session.execute(
    select(RecipeIngredientModel.id).filter(
        or_(
            RecipeIngredientModel.note_normalized.op("%>")(search),
            RecipeIngredientModel.original_text_normalized.op("%>")(search),
        )
    )
).scalars().all()

# 步骤 2：设置阈值（在食材预查询之后！）
session.execute(text(f"set pg_trgm.word_similarity_threshold = {cls._fuzzy_similarity_threshold};"))
# _fuzzy_similarity_threshold 默认 0.5，定义于 mealie_model.py

# 步骤 3：主查询过滤（此时阈值已生效为 0.5）
return query.filter(
    or_(
        RecipeModel.name_normalized.op("%>")(search),
        RecipeModel.description_normalized.op("%>")(search),
        RecipeModel.recipe_ingredient.any(RecipeIngredientModel.id.in_(ingredient_ids)),
    )
).order_by(func.least(RecipeModel.name_normalized.op("<->>")(search)))
```

因此：
- **食材预查询**使用本次 `SET` 执行前的 session 当前阈值；新连接通常是 PostgreSQL 默认 **0.3**，但若连接曾设置过阈值则以已有 session 值为准
- **菜谱名称 / 描述过滤**使用自定义阈值 **0.5**（更严格）

排序：按 name 的 trigram 距离升序（最相似的在前）
```sql
ORDER BY LEAST(name_normalized <->> search)
```

##### PostgreSQL trigram 索引覆盖范围

PostgreSQL 下列规范化搜索相关字段创建了 GIN 索引（`gin_trgm_ops` 操作符类）以加速 trigram 搜索：

| 模型 | 字段 | 索引名 |
|------|------|--------|
| `RecipeModel` | `name_normalized` | `ix_recipes_name_normalized_gin` |
| `RecipeModel` | `description_normalized` | `ix_recipes_description_normalized_gin` |
| `RecipeIngredientModel` | `note_normalized` | `ix_recipes_ingredients_note_normalized_gin` |
| `RecipeIngredientModel` | `original_text_normalized` | `ix_recipes_ingredients_original_text_normalized_gin` |
| `IngredientUnitModel` | 4 个规范化字段 | 各自对应 `_gin` 索引 |
| `IngredientFoodModel` | 2 个规范化字段 | 各自对应 `_gin` 索引 |

此外，相关规范化字段还声明了普通索引；不过 tokenized 搜索使用前后通配的 `LIKE "%token%"`，普通 B-tree 索引不一定能被数据库有效利用，真正的 trigram 加速主要来自 PostgreSQL 的 GIN 索引。

#### 3.5.2 Tokenized 搜索（通用 LIKE 匹配）

定义于 [recipe.py L363-L383](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/recipe/recipe.py#L363-L383)。

```python
# 食材预查询（任一 token 命中 note 或 original_text 即可）
ingredient_ids = session.execute(
    select(RecipeIngredientModel.id).filter(
        or_(
            *[RecipeIngredientModel.note_normalized.like(f"%{ns}%") for ns in search_list],
            *[RecipeIngredientModel.original_text_normalized.like(f"%{ns}%") for ns in search_list],
        )
    )
).scalars().all()

# 主查询过滤
return query.filter(
    or_(
        *[RecipeModel.name_normalized.like(f"%{ns}%") for ns in search_list],
        *[RecipeModel.description_normalized.like(f"%{ns}%") for ns in search_list],
        RecipeModel.recipe_ingredient.any(RecipeIngredientModel.id.in_(ingredient_ids)),
    )
).order_by(desc(RecipeModel.name_normalized.like(f"%{search}%")))
```

过滤条件（对每个 search token，三部分 OR）：
1. `RecipeModel.name_normalized LIKE '%token%'`
2. `RecipeModel.description_normalized LIKE '%token%'`
3. 食材中命中任一 token：先查出符合的 ingredient_ids，再用 `recipe_ingredient.any(id IN (...))`

排序：按完整搜索字符串在 name 中的匹配程度降序
```sql
ORDER BY (name_normalized LIKE '%完整搜索词%') DESC
```

---

## 四、Taxonomy 条件过滤

### 4.1 CookBook 模式 vs 独立参数模式

在 [RepositoryRecipes.page_all()](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L243-L266) 中：

- 如果提供了 `cookbook` 参数：将 `cookbook.query_filter_string` 合并到 `pagination_result.query_filter`，走通用 QueryFilterBuilder 流程
- 否则：调用 `_build_recipe_filter()` 构造 taxonomy 过滤条件

### 4.2 ID/Slug 统一解析

在 `_uuids_for_items()` 方法中（[repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L183-L202)），将 UUID 或 slug 混合列表统一转换为 UUID 列表：

1. 尝试将每个 item 解析为 UUID
2. 解析失败的视为 slug，通过数据库查询获取对应 ID

### 4.3 requireAll 默认值的真实语义

**这是一个容易混淆的点，需注意三层默认值的差异：**

| 位置 | 默认值 | 说明 |
|------|--------|------|
| `RecipeSearchQuery`（路由参数接收） | **False** | 定义于 [pagination.py L24-L27](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/response/pagination.py#L24-L27)，HTTP API 请求不传时生效 |
| `RepositoryRecipes.page_all()` 方法签名 | **True** | 定义于 [repository_recipes.py L230-L233](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L230-L233)，仅在直接调用 page_all 且不传这些参数时生效 |
| `_build_recipe_filter()` 方法签名 | **True** | 定义于 [repository_recipes.py L302-L305](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L302-L305)，同上 |
| `CookBook` 数据库模型字段 | **True** | 定义于 [cookbook.py L42/L45/L48](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/db/models/household/cookbook.py#L42)，但 CookBook 现在走 `query_filter_string` 不再走独立 taxonomy 过滤 |

**实际生效的默认值（用户 API + 公共 API）：`False`（OR 语义）**

原因：路由层在调用 page_all 时**始终显式传递** `search_query.require_all_categories` 等参数：
- 用户 API：[recipe_crud_routes.py L378-L381](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/recipe_crud_routes.py#L378-L381)
- 公共 API：[controller_public_recipes.py L75-L78](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/explore/controller_public_recipes.py#L75-L78)

传递的是 `RecipeSearchQuery` 中的值，而其默认值是 `False`。page_all 方法签名上的 `True` 默认值只有在绕过路由层、直接以 Python 代码调用 page_all 且不传这些参数时才会生效。

### 4.4 _build_recipe_filter 条件构造

定义于 [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L295-L337)。

#### 基础权限条件（始终添加）

```python
if self.group_id:
    fltr.append(RecipeModel.group_id == self.group_id)
if self.household_id:
    fltr.append(RecipeModel.household_id == self.household_id)
```

#### Categories 过滤

- `require_all_categories=True`：对每个分类 ID 分别加 `.any(Category.id == cat_id)` → **AND** 语义（必须包含所有分类）
- `require_all_categories=False`：`.any(Category.id IN (categories))` → **OR** 语义（包含任一分类即可）

#### Tags / Tools / Foods 过滤

逻辑与 categories 完全一致，只是关联的模型不同：
- Tags → `RecipeModel.tags.any(Tag.id ...)`
- Tools → `RecipeModel.tools.any(Tool.id ...)`
- Foods → `RecipeModel.recipe_ingredient.any(RecipeIngredientModel.food_id ...)`

#### Households 过滤

```python
if households:
    fltr.append(RecipeModel.household_id.in_(households))
```

---

## 五、排序（Order By）

排序在 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_generic.py#L432-L482) 的 `add_order_by_to_query()` 中处理。

### 5.1 默认排序

- **无文本搜索**：默认 `order_by = "created_at"`（在 page_all 中设置）
- **有文本搜索**：由搜索引擎自身提供排序（trigram 距离或 LIKE 匹配度），不设置默认值

### 5.2 随机排序（order_by = "random"）

```python
elif request_query.order_by == "random":
    temp_query = query.with_only_columns(self.model.id)
    allids = self.session.execute(temp_query).scalars().all()
    order = list(range(len(allids)))
    random.seed(request_query.pagination_seed)
    random.shuffle(order)
    random_dict = dict(zip(allids, order, strict=True))
    case_stmt = case(random_dict, value=self.model.id)
    return query.order_by(case_stmt)
```

特点：
- 不在数据库层面随机（为了跨数据库兼容和分页稳定性）
- 使用 `pagination_seed` 作为随机种子，保证同一 seed 下分页结果一致

### 5.3 常规字段排序

支持逗号分隔多字段，每个字段可指定方向（格式 `field:asc` 或 `field:desc`），未指定则使用全局 `order_direction`。

字段解析通过 `QueryFilterBuilder.get_model_and_model_attr_from_attr_string()` 支持：
- 简单字段：`name`, `created_at` 等
- 关联字段（点号链式）：如 `user.name`，会自动 JOIN 关联表
- 列别名（column_aliases）：Recipe 专用

### 5.4 Recipe 专属计算列别名

[RepositoryRecipes.column_aliases](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L39-L47) 提供两个计算列：

| 别名 | 说明 |
|------|------|
| `last_made` | 当前用户所在家庭的 `HouseholdToRecipe.last_made`，无则回退到 1900-01-01 |
| `rating` | 用户个人评分（如存在且>0），否则回退到菜谱全局评分 |

### 5.5 排序细节处理

在 `add_order_attr_to_query()` 中：
- **字符串列**：用 `func.lower()` 包装，消除大小写差异
- **NULL 值位置**：通过 `nulls_first()` / `nulls_last()` 控制

---

## 六、权限过滤

权限过滤在多个层次叠加：

### 6.1 Repository 层基础过滤（_filter_builder）

定义于 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_generic.py#L94-L102)：

```python
def _filter_builder(self, **kwargs) -> dict[str, Any]:
    dct = {}
    if self.group_id:
        dct["group_id"] = self.group_id
    if self.household_id:
        dct["household_id"] = self.household_id
    return {**dct, **kwargs}
```

Repository 通过 `group_id` / `household_id` 在构造时确定：

| 使用场景 | Repository 构造 | group_id | household_id |
|---------|----------------|----------|-------------|
| 当前家庭菜谱 | `self.repos.recipes` | 用户 group_id | 用户 household_id |
| 全组菜谱（用户 API） | `self.group_recipes` | 用户 group_id | None |
| 公共探索（Explore API） | `self.cross_household_repos` | group_id | None |
| Admin | Admin repos | None | None |

### 6.2 公共 API 额外权限过滤

在 [controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/explore/controller_public_recipes.py#L61-L65)：

```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
```

这个条件通过 `query_filter` 注入，由 `QueryFilterBuilder` 解析为 SQL：
1. `household.preferences.privateHousehold = FALSE`：所在家庭不是私密家庭
2. `settings.public = TRUE`：菜谱本身设置为公开

两个条件通过 AND 连接。如果用户已提供 `query_filter`，则用 AND 合并。

### 6.3 CookBook 公共权限检查

在公共 API 中，CookBook 除了存在性检查外，还需满足：
- `cookbook_data.public == True`
- 所在家庭不是 `private_household`

### 6.4 单条菜谱详情权限

公共 API 获取单条菜谱时（`/explore/.../recipes/{slug}`）：
- `recipe.settings.public` 必须为 True
- 所在家庭不能是私密家庭

---

## 七、结果总数与分页

总数计算在 [repository_generic.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_generic.py#L357-L405) 的 `add_pagination_to_query()` 中完成。

### 7.1 执行顺序（关键！）

```
1. 先应用 query_filter（QueryFilterBuilder.filter_query）
2. 基于过滤后的 query 构造 COUNT 子查询（此时还未 order_by/limit/offset）
3. 计算 count 和 total_pages
4. 处理特殊分页值：
   - per_page = -1 → 取 count 作为每页条数（即获取全部）
   - page = -1 → 取最后一页
5. 应用排序（add_order_by_to_query）
6. 应用 limit 和 offset
7. 返回 (query, count, total_pages)
```

### 7.2 COUNT 查询实现

```python
count_query = select(func.count()).select_from(query.order_by(None).distinct().subquery())
count = self.session.scalar(count_query)
```

关键点：
- 使用 `order_by(None)` 去除排序，避免 COUNT 不必要的排序开销
- 使用 `distinct()` 去重（因为 JOIN 可能产生重复行）
- 先 COUNT 后排序/分页，保证总数准确

### 7.3 总数的最终呈现

在 `RecipePagination` / `PaginationBase` 中：
- `total`：符合条件的总记录数
- `total_pages`：总页数 = `ceil(total / per_page)`
- `next` / `previous`：由前端路由路径 + 查询参数拼接而成

---

## 八、高级查询过滤（QueryFilterBuilder）

当 `pagination.query_filter` 不为空时，使用 [builder.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/services/query_filter/builder.py) 中的 `QueryFilterBuilder` 解析。

### 8.1 过滤字段白名单

只有被 `FilterableColumn` 标记的列才能过滤，定义于 [_filterable_column.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/db/models/_filterable_column.py)。

标记方式：
```python
name: FilterableColumn[str] = mapped_column(sa.String, nullable=False)
```

RecipeModel 中可过滤的主要字段（见 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/db/models/recipe/recipe.py)）：
`id`, `slug`, `group_id`, `user_id`, `rating`, `name`, `description`, `image`, `total_time`, `prep_time`, `perform_time`, `cook_time`, `recipe_yield`, `recipe_yield_quantity`, `recipe_servings`, `org_url`, `date_added`, `date_updated`, `last_made`, `name_normalized`, `description_normalized`, `created_at`, `update_at`

### 8.2 支持的运算符

- **关系关键词**：`IN`, `NOT IN`, `IS`, `IS NOT`, `CONTAINS ALL`, `LIKE`, `NOT LIKE`
- **关系运算符**：`=`, `!=`, `>`, `<`, `>=`, `<=`
- **逻辑运算符**：`AND`, `OR`
- **分组**：括号 `()`
- **列表值**：方括号 `[val1, val2, ...]`

### 8.3 占位符关键词

支持 `$today`, `$now` 等占位符（在 `PlaceholderKeyword.parse_value` 中处理）。

---

## 九、缓存机制及与过滤的关系

各缓存/优化机制的来源、作用域，以及**是否参与过滤逻辑**的区分如下：

| 机制 | 来源 | 作用域 | 是否参与过滤 | 说明 |
|------|------|--------|-------------|------|
| 搜索结果 | 无缓存 | 每次请求实时查询 | N/A | 搜索/建议结果完全实时，无任何结果级缓存 |
| Repository 实例 | `@cached_property` | 单次 HTTP 请求内 | **否** | 仅缓存 repository **对象实例**，不缓存查询结果，不影响过滤 |
| 图片 cache_key | `cache.cache_key.new_key()` | 跨请求持久化 | **否** | 仅用于图片 URL 的浏览器缓存失效，与搜索过滤完全无关 |
| 前端搜索 debounce | Vue composable | 前端客户端 | **否** | 仅减少 HTTP 请求频率，不改变后端过滤逻辑 |

### 9.1 Repository 实例缓存

[repository_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_factory.py) 中 `AllRepositories` 类的各 repository 属性使用 `@cached_property` 装饰，在单次请求生命周期内只初始化一次。

同样，[BaseRecipeController](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/_base.py#L37-L56) 中的 `recipes`, `group_recipes`, `group_cookbooks`, `service`, `mixins` 也使用 `@cached_property`。

**与过滤的关系**：此缓存仅避免重复构造 repository 对象（包括 group_id/household_id 绑定），每次 page_all / find_suggested_recipes 调用都是**独立执行完整的 SQL 查询**，不受此缓存影响。

### 9.2 前端图片缓存键

[cache_key.py](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/pkgs/cache/cache_key.py) 生成 4 位随机字符串（字母+数字），用于图片 URL 的缓存失效：

```python
recipe.image = cache.cache_key.new_key()  # 例如 "aB3x"
```

当图片更新时，缓存键改变，前端自动拉取新图片。

**与过滤的关系**：完全无关。cache_key 仅出现在 Recipe 响应的图片 URL 中（如 `/media/recipes/xxx-aB3x/original.jpg`），不参与任何搜索条件、排序或权限过滤。

### 9.3 前端搜索防抖

[use-recipe-search.ts](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/frontend/app/composables/recipes/use-recipe-search.ts#L51-L57) 使用 500ms debounce 避免用户输入时频繁发请求。

**与过滤的关系**：完全无关。debounce 仅在前端控制请求发送时机，一旦请求到达后端，过滤逻辑按正常流程完整执行。

---

## 十、普通搜索完整执行顺序总结

以用户 API `GET /api/recipes` 为例，完整的查询构造顺序：

| 步骤 | 操作 | 代码位置 |
|------|------|---------|
| 1 | 初始化 `RepositoryRecipes`，设置 group_id, household_id | repository_factory.py |
| 2 | 调用 `by_user(user_id)` 绑定当前用户（用于 last_made/rating 计算列） | repository_recipes.py |
| 3 | 基础查询：`SELECT * FROM recipes WHERE household_id IS NOT NULL` | repository_recipes.py L238 |
| 4 | 基础权限过滤：`filter_by(group_id=..., household_id=...)` | _filter_builder |
| 5 | CookBook / Taxonomy 条件过滤（requireAll 默认 **False** 即 OR） | _build_recipe_filter 或合并 cookbook.query_filter_string |
| 6 | 文本搜索：调用 `SearchFilter.filter_query_by_search()` 添加 fuzzy/tokenized 搜索条件及排序 | add_search_to_query → Recipe.filter_search_query |
| 7 | query_filter 高级过滤（包含公共 API 的权限条件） | QueryFilterBuilder.filter_query |
| 8 | COUNT 查询：基于已过滤未排序 query 计算总条数 | add_pagination_to_query L376-L379 |
| 9 | 计算 total_pages，处理特殊分页值 | add_pagination_to_query L381-L398 |
| 10 | 应用排序（默认 created_at / 搜索排序 / 自定义 / 随机） | add_order_by_to_query |
| 11 | 应用 LIMIT / OFFSET | add_pagination_to_query L402-L405 |
| 12 | 添加 SQLAlchemy loader_options（joinedload/selectinload）避免 N+1 | page_all 末尾 |
| 13 | 执行查询，结果序列化为 RecipeSummary 列表 | page_all 末尾 |
| 14 | 构造 RecipePagination，设置 next/previous 链接 | 路由层 |

---

## 十一、Recipe Suggestions（推荐建议）过滤链路

### 11.1 路由入口

#### 用户 API

[recipe_crud_routes.py L397-L413](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/recipe/recipe_crud_routes.py#L397-L413)

```python
@router.get("/suggestions", response_model=RecipeSuggestionResponse)
def suggest_recipes(
    self,
    q: RecipeSuggestionQuery = Depends(make_dependable(RecipeSuggestionQuery)),
    foods: list[UUID4] | None = Query(None),
    tools: list[UUID4] | None = Query(None),
) -> RecipeSuggestionResponse:
    group_recipes_by_user = get_repositories(
        self.session, group_id=self.group_id, household_id=None
    ).recipes.by_user(self.user.id)

    recipes = group_recipes_by_user.find_suggested_recipes(q, foods, tools)
```

- Repository：`group_id=用户group_id, household_id=None`（全组范围），并绑定 `by_user(user.id)`
- 接收参数：`RecipeSuggestionQuery`（继承 `RequestQuery`）+ 独立的 `foods` 和 `tools` Query 参数

#### 公共 Explore API

[controller_public_recipes.py L94-L112](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/routes/explore/controller_public_recipes.py#L94-L112)

与用户 API 的区别在于**先注入公共权限过滤**到 `q.query_filter`：

```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
if q.query_filter:
    q.query_filter = f"({q.query_filter}) AND {public_filter}"
else:
    q.query_filter = public_filter
```

然后调用 `self.cross_household_recipes.find_suggested_recipes(q, foods, tools)`。

### 11.2 RecipeSuggestionQuery 参数结构

定义于 [recipe_suggestion.py L7-L15](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/schema/recipe/recipe_suggestion.py#L7-L15)，继承自 `RequestQuery`：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| **继承自 RequestQuery** | | | |
| `order_by` | str \| None | None | 用户自定义排序字段 |
| `order_direction` | OrderDirection | desc | 排序方向 |
| `order_by_null_position` | OrderByNullPosition \| None | None | NULL 值排序位置 |
| `query_filter` | str \| None | None | 高级查询过滤表达式 |
| `pagination_seed` | str \| None | None | 随机排序种子 |
| **Suggestion 专属** | | | |
| `limit` | int | **10** | 返回结果最大数量 |
| `max_missing_foods` | int | **5** | 允许缺少的最大食材数 |
| `max_missing_tools` | int | **5** | 允许缺少的最大工具数 |
| `include_foods_on_hand` | bool | **True** | 是否包含用户家庭已有的食材 |
| `include_tools_on_hand` | bool | **True** | 是否包含用户家庭已有的工具 |

### 11.3 find_suggested_recipes 完整执行流程

定义于 [repository_recipes.py L361-L532](file:///d:/fz/0601/solo-dogfeeding/code/77-mealie/mealie/repos/repository_recipes.py#L361-L532)。

#### 步骤 1：默认排序兜底

```python
if not params.order_by:
    params.order_by = "created_at"
```

若用户未指定排序，默认按 `created_at` 排序。这与普通搜索的默认排序一致，但 suggestions 的排序是**叠加在缺失度排序之后**的（见步骤 5）。

#### 步骤 2：处理 foods/tools + on_hand（已有物品）

```python
user_food_ids = list(set(food_ids or []))
user_tool_ids = list(set(tool_ids or []))

food_ids_with_on_hand = user_food_ids.copy()
tool_ids_with_on_hand = user_tool_ids.copy()
```

- `user_food_ids` / `user_tool_ids`：用户通过 Query 参数显式传入的 ID（去重），这些是用户**想要使用**的物品
- `food_ids_with_on_hand` / `tool_ids_with_on_hand`：后续将追加用户家庭**已拥有**的物品 ID，用于计算"缺少"时排除已有物品

##### 步骤 2a：合并用户家庭已有食材（on-hand foods）

当 `include_foods_on_hand=True` 且存在 `self.user_id` 时：

```python
if params.include_foods_on_hand and self.user_id:
    foods_on_hand_query = (
        sa.select(households_to_ingredient_foods.c.food_id)
        .join(User, households_to_ingredient_foods.c.household_id == User.household_id)
        .filter(
            sa.not_(households_to_ingredient_foods.c.food_id.in_(food_ids_with_on_hand)),
            User.id == self.user_id,
        )
    )
    foods_on_hand = self.session.execute(foods_on_hand_query).scalars().all()
    food_ids_with_on_hand.extend(foods_on_hand)
```

- 通过关联表 `households_to_ingredient_foods` 查出用户所在家庭已标记为"在手"的食材
- 排除已在 `user_food_ids` 中的重复项
- 追加到 `food_ids_with_on_hand`

##### 步骤 2b：合并用户家庭已有工具（on-hand tools）

逻辑完全同食材，只是查询关联表 `households_to_tools`。

最终语义：
- `food_ids_with_on_hand` = 用户显式传入的食材 ∪ 家庭已有食材
- `tool_ids_with_on_hand` = 用户显式传入的工具 ∪ 家庭已有工具
- 计算"缺少"时，用这两个集合判断是否已拥有

#### 步骤 3：基础查询与权限过滤

```python
q = sa.select(self.model).filter(self.model.household_id.is_not(None))
fltr = self._filter_builder()
q = q.filter_by(**fltr)
```

- 过滤掉没有 household_id 的菜谱
- 应用 group_id / household_id 基础权限（同普通搜索）

#### 步骤 4：工具缺失度子查询 + 过滤 + 排序（仅当 user_tool_ids 非空时）

```python
if user_tool_ids:
    unmatched_tools_query = (
        sa.select(recipes_to_tools.c.recipe_id, sa.func.count().label("unmatched_tools_count"))
        .join(tools_alias, recipes_to_tools.c.tool_id == tools_alias.id)
        .filter(sa.not_(tools_alias.id.in_(tool_ids_with_on_hand)))
        .group_by(recipes_to_tools.c.recipe_id)
        .subquery()
    )
```

子查询逻辑：统计每个菜谱**不包含在** `tool_ids_with_on_hand`（用户拥有的全部工具）中的工具数量 → 即"缺少的工具数"。

```python
q = (
    q.outerjoin(unmatched_tools_query, self.model.id == unmatched_tools_query.c.recipe_id)
    .filter(
        sa.or_(
            unmatched_tools_query.c.unmatched_tools_count.is_(None),  # 该菜谱没有任何工具需求
            unmatched_tools_query.c.unmatched_tools_count <= params.max_missing_tools,
        )
    )
    .order_by(unmatched_tools_query.c.unmatched_tools_count.asc().nulls_first())
)
```

- `outerjoin`：没有工具需求的菜谱 unmatched_tools_count 为 NULL
- `filter`：允许"缺少工具数 ≤ max_missing_tools"或"完全没有工具需求"的菜谱
- `order_by`：**缺少工具数升序**（缺得越少越靠前），NULL 排最前

#### 步骤 5：食材缺失度子查询 + 过滤 + 排序（仅当 user_food_ids 非空时）

```python
if user_food_ids:
    unmatched_foods_query = (
        sa.select(ingredients_alias.recipe_id, sa.func.count().label("unmatched_foods_count"))
        .filter(sa.not_(ingredients_alias.food_id.in_(food_ids_with_on_hand)))
        .filter(ingredients_alias.food_id.isnot(None))
        .group_by(ingredients_alias.recipe_id)
        .subquery()
    )
    total_user_foods_query = (
        sa.select(ingredients_alias.recipe_id, sa.func.count().label("total_user_foods_count"))
        .filter(ingredients_alias.food_id.in_(user_food_ids))  # 注意这里用 user_food_ids，不含 on_hand
        .group_by(ingredients_alias.recipe_id)
        .subquery()
    )
```

两个子查询：
- `unmatched_foods_query`：统计每个菜谱中**不在** `food_ids_with_on_hand` 中的食材数量 → "缺少的食材数"
- `total_user_foods_query`：统计每个菜谱中**恰好匹配**用户显式传入食材（不含 on-hand）的数量 → "用户想要用的食材匹配数"

```python
q = (
    q.join(settings_alias, self.model.settings)
    .outerjoin(unmatched_foods_query, self.model.id == unmatched_foods_query.c.recipe_id)
    .outerjoin(total_user_foods_query, self.model.id == total_user_foods_query.c.recipe_id)
    .filter(
        sa.or_(
            unmatched_foods_query.c.unmatched_foods_count.is_(None),
            unmatched_foods_query.c.unmatched_foods_count <= params.max_missing_foods,
        ),
    )
    .order_by(
        unmatched_foods_query.c.unmatched_foods_count.asc().nulls_first(),
        total_user_foods_query.c.total_user_foods_count.desc().nulls_last(),
    )
)
```

- 过滤：允许"缺少食材数 ≤ max_missing_foods"的菜谱
- 排序优先级（叠加在工具排序之后）：
  1. **缺少食材数升序**（越少越前）
  2. **用户显式传入食材匹配数降序**（越多越前，越符合用户意图）

```python
# 额外过滤：必须至少匹配一个用户显式传入的食材
if user_food_ids:
    q = q.filter(total_user_foods_query.c.total_user_foods_count > 0)
```

这意味着：用户如果传了 foods 参数，结果**必须至少包含其中一个**食材，避免推荐毫不相关的菜谱。

#### 步骤 6：附加条件（group / household / query_filter）

```python
if self.group_id:
    q = q.filter(self.model.group_id == self.group_id)
if self.household_id:
    q = q.filter(self.model.household_id == self.household_id)
if params.query_filter:
    query_filter_builder = QueryFilterBuilder(params.query_filter)
    q = query_filter_builder.filter_query(q, model=self.model)
```

注意：这里 group_id / household_id 又加了一遍过滤（与步骤 3 的 filter_by 有重叠，但 SQLAlchemy 会合并处理）。公共 API 的权限过滤通过 `params.query_filter` 注入到这里。

#### 步骤 7：用户自定义排序 + limit

```python
q = self.add_order_by_to_query(q, params)
q = q.limit(params.limit).options(*RecipeSummary.loader_options())
```

- `add_order_by_to_query` 追加用户指定排序（默认 `created_at`）。由于 SQL `ORDER BY` 是**按出现顺序优先级从高到低**，因此最终排序优先级为：
  1. 缺少工具数升序（步骤 4）→ 最高优先级
  2. 缺少食材数升序（步骤 5）
  3. 用户食材匹配数降序（步骤 5）
  4. 用户自定义排序 / created_at（步骤 7）→ 最低优先级
- `limit(params.limit)`：限制返回数量，默认 10
- `loader_options`：joinedload 预加载 category/tag/tool/user 关联

#### 步骤 8：Python 层计算缺失物品并返回

查询执行后，在 Python 层再次遍历每个菜谱的 ingredients 和 tools，基于 `food_ids_with_on_hand` / `tool_ids_with_on_hand` 计算实际缺失的物品列表，封装为 `RecipeSuggestionResponseItem` 返回。

### 11.4 Suggestions 执行顺序总表

| 步骤 | 操作 | 条件 |
|------|------|------|
| 1 | 默认 order_by = "created_at" | 仅当用户未指定 |
| 2 | foods/tools 参数去重，初始化 *_with_on_hand 集合 | 始终 |
| 3 | 合并家庭已有食材（on-hand foods） | include_foods_on_hand=True 且有 user_id |
| 4 | 合并家庭已有工具（on-hand tools） | include_tools_on_hand=True 且有 user_id |
| 5 | 基础查询 + _filter_builder 权限过滤 | 始终 |
| 6 | 工具缺失度子查询 → outerjoin → 过滤（≤max_missing_tools）→ 排序（缺越少越前） | user_tool_ids 非空 |
| 7 | 食材缺失度 + 食材匹配数子查询 → outerjoin → 过滤（≤max_missing_foods 且 ≥1 匹配）→ 排序（缺越少越前、匹配越多越前） | user_food_ids 非空 |
| 8 | 附加 group_id/household_id/query_filter 过滤 | 始终 |
| 9 | 追加用户自定义排序（叠加在缺失度排序之后） | 始终 |
| 10 | LIMIT + loader_options | 始终 |
| 11 | Python 层计算缺失物品明细，封装返回 | 始终 |
