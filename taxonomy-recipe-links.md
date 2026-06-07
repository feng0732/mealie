# Mealie 标签、分类、工具与 Recipe 关联实现分析

## 一、作用范围差异

### 1.1 基本层级

| 维度 | 标签 Tag | 分类 Category | 工具 Tool |
|------|---------|--------------|----------|
| 所属层级 | Group 级 | Group 级 | Group 级 |
| 唯一约束 | (slug, group_id) | (slug, group_id) | (slug, group_id) |
| 主键归属方式 | group_id 外键 + 一对多 | group_id 外键 + 一对多；另有 group_to_categories 多对多表 | group_id 外键 + 一对多 |
| 是否关联 Household | 否 | 否 | 是（households_to_tools 表） |
| 是否关联 MealPlanRules | 是 | 是 | 否 |
| Recipe 创建时自动创建 | 是 | 是 | 否 |
| 提供"空"检测 API | 是 | 是 | 否 |
| Repository | RepositoryTags（继承 GroupRepositoryGeneric，有 get_empty） | RepositoryCategories（继承 GroupRepositoryGeneric，有 get_empty） | GroupRepositoryGeneric（无自定义方法） |

### 1.2 各 taxonomy 定义的 Relationship

此节很关键：**只有在模型中定义了 relationship 的方向，删除该对象时 ORM 才会自动清理对应 secondary 关联表**。

**标签 Tag**（mealie/db/models/recipe/tag.py）:
- `group`：通过外键 group_id 与 Group 形成多对一，`back_populates="tags"`
- `recipes`：通过 `recipes_to_tags` 关联表与 Recipe 形成多对多，`back_populates="tags"`（双向）
- **未定义**：cookbooks、plan_rules 的反向 relationship

**分类 Category**（mealie/db/models/recipe/category.py）:
- `group`：通过外键 group_id 与 Group 形成多对一，`back_populates="categories"`
- `recipes`：通过 `recipes_to_categories` 关联表与 Recipe 形成多对多，`back_populates="recipe_category"`（双向）
- **未定义**：cookbooks、plan_rules、group_to_categories 的反向 relationship

**工具 Tool**（mealie/db/models/recipe/tool.py）:
- `group`：通过外键 group_id 与 Group 形成多对一，`back_populates="tools"`
- `recipes`：通过 `recipes_to_tools` 关联表与 Recipe 形成多对多，`back_populates="tools"`（双向）
- `households_with_tool`：通过 `households_to_tools` 关联表与 Household 形成多对多，`back_populates="tools_on_hand"`（双向）
- **未定义**：cookbooks 的反向 relationship

### 1.3 其他对象到 taxonomy 的 Relationship

**Recipe**（mealie/db/models/recipe/recipe.py）到 taxonomy 的关系（均无 cascade 参数）：
- `recipe_category: Mapped[list[Category]]`，secondary=recipes_to_categories，back_populates="recipes"
- `tools: Mapped[list[Tool]]`，secondary=recipes_to_tools，back_populates="recipes"
- `tags: Mapped[list[Tag]]`，secondary=recipes_to_tags，back_populates="recipes"

**CookBook**（mealie/db/models/household/cookbook.py）：
- `categories: Mapped[list[Category]]`，secondary=cookbooks_to_categories，single_parent=True
- `tags: Mapped[list[Tag]]`，secondary=cookbooks_to_tags，single_parent=True
- `tools: Mapped[list[Tool]]`，secondary=cookbooks_to_tools，single_parent=True
- （均为单向，无 back_populates）

**GroupMealPlanRules**（mealie/db/models/household/mealplan.py）：
- `categories: Mapped[list[Category]]`，secondary=plan_rules_to_categories
- `tags: Mapped[list[Tag]]`，secondary=plan_rules_to_tags
- `households: Mapped[list[Household]]`，secondary=plan_rules_to_households
- （均为单向，无 back_populates，无 tools）

**Group**（mealie/db/models/group/group.py）：
- `tags: Mapped[list[Tag]]`，一对多（Tag.group_id 外键），back_populates="group"，cascade="all, delete-orphan"
- `tools: Mapped[list[Tool]]`，一对多（Tool.group_id 外键），back_populates="group"，cascade="all, delete-orphan"
- `categories: Mapped[list[Category]]`，**多对多**（secondary=group_to_categories），single_parent=True，无 back_populates、无 cascade 参数
- 注：Category 本身也有 group_id 外键，Group.categories 的定义方式与 Tag/Tool 不一致

**Household**（mealie/db/models/household/household.py）：
- `tools_on_hand: Mapped[list[Tool]]`，secondary=households_to_tools，back_populates="households_with_tool"（双向）
- `cookbooks: Mapped[list[CookBook]]`，一对多（CookBook.household_id 外键），cascade="all, delete-orphan"

---

## 二、筛选参数来源与 query_filter 合并

### 2.1 参数来源三层链路

**第一层：API 路由 Query 参数**（mealie/routes/recipe/recipe_crud_routes.py）

```python
def get_all(
    self,
    q: PaginationQuery = Depends(...),
    search_query: RecipeSearchQuery = Depends(...),
    categories: list[UUID4 | str] | None = Query(None),
    tags: list[UUID4 | str] | None = Query(None),
    tools: list[UUID4 | str] | None = Query(None),
    foods: list[UUID4 | str] | None = Query(None),
    households: list[UUID4 | str] | None = Query(None),
):
```

直接传入列表，每个元素支持 UUID 或 slug 字符串混合。

**第二层：RecipeSearchQuery 对象**（mealie/schema/response/pagination.py）

```python
class RecipeSearchQuery(MealieModel):
    cookbook: UUID4 | str | None = None
    require_all_categories: bool = False   # 默认 OR 逻辑
    require_all_tags: bool = False         # 默认 OR 逻辑
    require_all_tools: bool = False        # 默认 OR 逻辑
    require_all_foods: bool = False        # 默认 OR 逻辑
    search: str | None = None
```

- `cookbook`：ID 或 slug，指定后走 CookBook 筛选路径
- `require_all_*`：`False`=OR（满足任一），`True`=AND（必须全部满足）；**默认值为 False**
- `search`：全文搜索关键词

**第三层：PaginationQuery**（mealie/schema/response/pagination.py）

```python
class RequestQuery(MealieModel):
    order_by: str | None = None
    order_direction: OrderDirection = OrderDirection.desc
    query_filter: str | None = None        # 字符串表达式过滤
    pagination_seed: str | None = None

class PaginationQuery(RequestQuery):
    page: int = 1
    per_page: int = 50
```

`query_filter` 是自由格式字符串，通过 QueryFilterBuilder 解析为 SQL where 子句。

### 2.2 CookBook 筛选与参数合并

CookBook 模型保存两套筛选配置（mealie/db/models/household/cookbook.py）：

```python
query_filter_string: Mapped[str]          # 新版：自定义查询字符串（推荐）
categories: Mapped[list[Category]]        # 旧版（已弃用）
require_all_categories: FilterableColumn[bool | None]
tags: Mapped[list[Tag]]
require_all_tags: FilterableColumn[bool | None]
tools: Mapped[list[Tool]]
require_all_tools: FilterableColumn[bool | None]
```

在 RepositoryRecipes.page_all 中的合并逻辑（mealie/repos/repository_recipes.py）：

```python
if cookbook:
    # 如果 cookbook.query_filter_string 存在，它与 pagination.query_filter 用 AND 合并
    # 并直接跳过 categories/tags/tools/foods 的处理
    if pagination_result.query_filter and cookbook.query_filter_string:
        pagination_result.query_filter = (
            f"({pagination_result.query_filter}) AND ({cookbook.query_filter_string})"
        )
    else:
        pagination_result.query_filter = cookbook.query_filter_string
else:
    # 否则走常规 categories/tags/tools/foods 筛选路径
    category_ids = self._uuids_for_items(categories, Category)
    tag_ids = self._uuids_for_items(tags, Tag)
    tool_ids = self._uuids_for_items(tools, Tool)
    household_ids = self._uuids_for_items(households, Household)
    filters = self._build_recipe_filter(...)
    q = q.filter(*filters)
```

**结论**：当指定 `cookbook` 参数时：
1. CookBook 的 `query_filter_string` 与 `PaginationQuery.query_filter` 用 AND 合并
2. 直接传入的 `categories/tags/tools/foods` Query 参数被**完全忽略**
3. 旧版 CookBook.categories/tags/tools 字段在 page_all 中未被读取

### 2.3 ID/Slug 解析

`_uuids_for_items` 方法签名（mealie/repos/repository_recipes.py）：

```python
def _uuids_for_items(self, items: list[UUID | str] | None, model: type[SqlAlchemyBase]) -> list[UUID] | None:
```

处理逻辑：
- 若元素为 UUID 类型，直接加入结果
- 若元素为字符串：尝试解析为 UUID，成功则加入；失败则作为 slug 加入待查列表
- 对所有 slug 执行 `select(model.id).filter(model.slug.in_(slugs))`，将查到的 id 合并返回

### 2.4 筛选执行逻辑

`_build_recipe_filter` 方法构建条件（mealie/repos/repository_recipes.py）：

```python
# AND 逻辑（require_all_* = True）
for category_id in categories:
    fltr.append(RecipeModel.recipe_category.any(Category.id == category_id))

# OR 逻辑（require_all_* = False，默认）
fltr.append(RecipeModel.recipe_category.any(Category.id.in_(categories)))
```

同时附加：
- 若 Repository 设置了 group_id：附加 `RecipeModel.group_id == self.group_id`
- 若 Repository 设置了 household_id：附加 `RecipeModel.household_id == self.household_id`

---

## 三、删除清理路径核准

### 3.0 基础机制

所有关联表的数据库外键约束**均未配置 `ON DELETE CASCADE`**（可从 Alembic 迁移文件的 ForeignKeyConstraint 定义确认，均无 `ondelete` 参数）。关联表的清理完全依赖 SQLAlchemy ORM 的行为。

SQLAlchemy ORM 清理 secondary 关联表的条件：**删除对象时，只有该对象自身定义了 relationship 的 secondary 表，才会被自动清理**。没有定义反向 relationship 的方向，删除时不会自动清理。

### 3.1 删除单个 Tag / Category / Tool

三者共用 RepositoryGeneric.delete：

```python
def delete(self, value, match_key: str | None = None) -> Schema:
    result = self._query_one(value, match_key)
    result_as_model = self.schema.model_validate(result)
    try:
        self.session.delete(result)
        self.session.commit()
    except Exception as e:
        self.session.rollback()
        raise e
    return result_as_model
```

**删除 Tag 时的清理范围**：
- `recipes_to_tags`：会被清理（Tag 定义了 `recipes` relationship）
- `cookbooks_to_tags`：**不会自动清理**（Tag 未定义 `cookbooks` 反向 relationship）
- `plan_rules_to_tags`：**不会自动清理**（Tag 未定义 `plan_rules` 反向 relationship）

**删除 Category 时的清理范围**：
- `recipes_to_categories`：会被清理（Category 定义了 `recipes` relationship）
- `cookbooks_to_categories`：**不会自动清理**（Category 未定义 `cookbooks` 反向）
- `plan_rules_to_categories`：**不会自动清理**（Category 未定义 `plan_rules` 反向）
- `group_to_categories`：**不会自动清理**（Category 未定义反向）

**删除 Tool 时的清理范围**：
- `recipes_to_tools`：会被清理（Tool 定义了 `recipes` relationship）
- `households_to_tools`：会被清理（Tool 定义了 `households_with_tool` relationship）
- `cookbooks_to_tools`：**不会自动清理**（Tool 未定义 `cookbooks` 反向）

API 文档注释（mealie/routes/organizers/controller_tags.py 等）声明：
> "Deleting a tag does not impact a recipe. The tag will be removed from any recipes that contain it"

该声明仅对应 recipes_to_* 表的清理，未提及 cookbooks_to_* 和 plan_rules_to_* 表。

### 3.2 删除 Recipe

专用实现位于 mealie/repos/repository_recipes.py：

```python
def _delete_recipe(self, recipe: Recipe) -> Recipe:
    # Clear users_to_recipes first, otherwise the recipe can't be deleted
    stmt = delete(users_to_recipes).where(users_to_recipes.c.recipe_id == recipe.id)
    self.session.execute(stmt)

    self.session.delete(recipe)
    return recipe
```

删除触发链：
1. 先显式执行 SQL `DELETE FROM users_to_recipes WHERE recipe_id = ?`
2. 执行 `session.delete(recipe)`，ORM 自动清理：
   - `recipes_to_tags`（双向关系，Recipe 定义了 tags）
   - `recipes_to_categories`（双向关系，Recipe 定义了 recipe_category）
   - `recipes_to_tools`（双向关系，Recipe 定义了 tools）

批量删除的特殊处理（RepositoryRecipes.delete_many）：

```python
def delete_many(self, values: Iterable) -> list[Recipe]:
    ...
    for result in results:
        # we don't delete the whole query in one statement because postgres doesn't cascade correctly
        self.session.delete(result)
    self.session.commit()
```

使用逐条 `session.delete()` 而非单条 SQL DELETE，绕过 PostgreSQL 批量级联的异常。

### 3.3 删除 CookBook

入口位于 mealie/routes/households/controller_cookbooks.py → RepositoryCookbooks.delete（继承 RepositoryGeneric.delete）。

删除触发链：
- `cookbooks_to_tags`：会被清理（CookBook 定义了 `tags` relationship）
- `cookbooks_to_categories`：会被清理（CookBook 定义了 `categories` relationship）
- `cookbooks_to_tools`：会被清理（CookBook 定义了 `tools` relationship）

CookBook 删除仅清理自身与 taxonomy 的关联，不影响 Recipe 与 taxonomy 的关联。

Household 级联：CookBook 通过 Household 的 relationship（cascade="all, delete-orphan"）被级联删除。

Group 级联：CookBook 通过 Group 的 relationship（cascade="all, delete-orphan"）被级联删除。

### 3.4 删除 MealPlan（GroupMealPlan）

入口位于 mealie/routes/households/controller_mealplan.py → RepositoryMeals.delete（继承 HouseholdRepositoryGeneric → RepositoryGeneric.delete）。

GroupMealPlan 模型与 taxonomy 无直接多对多关系（categories/tags 关联在 GroupMealPlanRules 上），因此 MealPlan 删除**不涉及** taxonomy 关联表清理。

### 3.5 删除 MealPlanRules（GroupMealPlanRules）

入口位于 mealie/routes/households/controller_mealplan_rules.py → RepositoryMealPlanRules.delete（继承 HouseholdRepositoryGeneric → RepositoryGeneric.delete）。

删除触发链：
- `plan_rules_to_tags`：会被清理（GroupMealPlanRules 定义了 `tags` relationship）
- `plan_rules_to_categories`：会被清理（GroupMealPlanRules 定义了 `categories` relationship）
- `plan_rules_to_households`：会被清理（GroupMealPlanRules 定义了 `households` relationship）

### 3.6 删除 Household

Household 的级联 relationship（mealie/db/models/household/household.py）：

```python
COMMON_ARGS = {
    "back_populates": "household",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}
preferences: ... cascade="all, delete-orphan"
invite_tokens: ... cascade="all, delete-orphan"
recipe_actions: Mapped[list["GroupRecipeAction"]] = orm.relationship(..., **COMMON_ARGS)
cookbooks: Mapped[list["CookBook"]] = orm.relationship("CookBook", **COMMON_ARGS)
webhooks: ... = orm.relationship(..., **COMMON_ARGS)
group_event_notifiers: ... = orm.relationship(..., **COMMON_ARGS)
```

多对多关系（无 cascade）：
- `made_recipes`：secondary=household_to_recipe，back_populates="made_by"
- `ingredient_foods_on_hand`：secondary=households_to_ingredient_foods
- `tools_on_hand`：secondary=households_to_tools，back_populates="tools_on_hand"（双向）

删除触发链：
1. cascade="all, delete-orphan" 级联删除：
   - HouseholdPreferences
   - 所有 InviteTokens
   - 所有 RecipeActions
   - 所有 CookBooks → 每个 CookBook 删除触发 cookbooks_to_tags/categories/tools 的清理（见 3.3）
   - 所有 Webhooks
   - 所有 EventNotifiers
2. 多对多 secondary 表由 ORM 自动清理：
   - `households_to_tools`（双向，tools_on_hand 有定义）
   - `household_to_recipe`（made_recipes 有定义）
   - `households_to_ingredient_foods`（ingredient_foods_on_hand 有定义）

注意：Household 删除**不直接删除** Group 级的 tags/categories/tools 本身，也不直接删除 recipes。

### 3.7 删除 Group

Group 的级联 relationship（mealie/db/models/group/group.py）：

```python
common_args = {
    "back_populates": "group",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}

# 带 cascade 的一对多 / 一对一：
preferences: ... cascade="all, delete-orphan"
ai_provider_settings: ... cascade="all, delete-orphan"
invite_tokens: ... cascade="all, delete-orphan"
labels: Mapped[list[MultiPurposeLabel]] = orm.relationship(..., **common_args)
mealplans: Mapped[list[GroupMealPlan]] = orm.relationship(..., **common_args)
webhooks: ... = orm.relationship(..., **common_args)
recipe_actions: ... = orm.relationship(..., **common_args)
cookbooks: Mapped[list[CookBook]] = orm.relationship(CookBook, **common_args)
server_tasks: ... = orm.relationship(..., **common_args)
data_exports: ... = orm.relationship(..., **common_args)
shopping_lists: ... = orm.relationship(..., **common_args)
group_reports: ... = orm.relationship(..., **common_args)
group_event_notifiers: ... = orm.relationship(..., **common_args)
ingredient_units: ... = orm.relationship(..., **common_args)
ingredient_foods: ... = orm.relationship(..., **common_args)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", **common_args)
tags: Mapped[list["Tag"]] = orm.relationship("Tag", **common_args)

# 无 cascade：
households: Mapped[list["Household"]] = orm.relationship("Household", back_populates="group")
users: Mapped[list["User"]] = orm.relationship("User", back_populates="group")
recipes: Mapped[list["RecipeModel"]] = orm.relationship("RecipeModel", back_populates="group")

# 多对多，无 cascade，无 back_populates：
categories: Mapped[list[Category]] = orm.relationship(Category, secondary=group_to_categories, single_parent=True)
```

删除触发链（级联展开）：

1. cascade="all, delete-orphan" 删除 tags：
   - 每个 Tag 通过 `session.delete(tag)` 被删除
   - 按 3.1 节所述，ORM 清理 `recipes_to_tags`
   - `cookbooks_to_tags` 和 `plan_rules_to_tags` 不会自动清理（Tag 未定义反向 relationship）

2. cascade="all, delete-orphan" 删除 tools：
   - 每个 Tool 通过 `session.delete(tool)` 被删除
   - ORM 清理 `recipes_to_tools`、`households_to_tools`
   - `cookbooks_to_tools` 不会自动清理

3. Group.categories 多对多（无 cascade）：
   - ORM 清理 `group_to_categories` 关联表中该 group_id 的行
   - 但 Category 对象本身不会通过此 relationship 被级联删除
   - 不过 Category 同样有 group_id 外键，若数据库层面没有阻止，Category 仍可能因外键约束报错或被单独处理

4. cascade="all, delete-orphan" 删除 cookbooks：
   - 每个 CookBook 删除触发 `cookbooks_to_tags`、`cookbooks_to_categories`、`cookbooks_to_tools` 清理
   - （这恰好弥补了删除 Tag/Category/Tool 时未清理的 cookbooks_to_* 表）

5. cascade="all, delete-orphan" 删除 mealplans（GroupMealPlan）：
   - 不涉及 taxonomy 关联表

6. 其他级联删除：mealplans、shopping_lists、webhooks 等

注意：`group.households`、`group.users`、`group.recipes` 都没有 cascade，Group 删除时这些对象不会被 ORM 自动删除。由于这些表的外键是 NOT NULL，实际应用中需要先单独处理这些对象。

### 3.8 Recipe 更新时空列表的显式处理

mealie/db/models/_model_base.py 中 BaseMixins.update：

```python
def update(self, *args, **kwargs):
    self.__init__(*args, **kwargs)
    # sqlalchemy doesn't like this method to remove all instances of a 1:many relationship,
    # so we explicitly check for that here
    for k, v in kwargs.items():
        if hasattr(self, k) and v == []:
            setattr(self, k, v)
```

当 Recipe 更新传入 `tags=[]`、`recipe_category=[]`、`tools=[]` 时，此逻辑显式将 relationship 集合置空，确保 ORM 正确追踪并删除对应 secondary 表行。

### 3.9 "空" Taxonomy 检测

RepositoryCategories 和 RepositoryTags 提供 get_empty 方法（mealie/repos/repository_factory.py）：

```python
def get_empty(self) -> Sequence[Category]:
    stmt = select(Category).filter(~Category.recipes.any())
    return self.session.execute(stmt).scalars().all()
```

Tag 同理。Tool 未提供此方法。

对应 API：
- `GET /organizers/tags/empty`
- `GET /organizers/categories/empty`

---

## 四、Recipe 创建时的 taxonomy 自动创建

mealie/services/recipe/recipe_service.py 中的 `_transform_category_or_tag`：

```python
def _transform_category_or_tag(self, data: dict, repo: RepositoryGeneric) -> dict:
    slug = data.get("slug")
    if not slug:
        return data

    query = repo.get_one(slug, "slug")
    if query:
        return query.model_dump()

    new_item = repo.create(data)
    return new_item.model_dump()
```

在 `_process_recipe_data` 中调用：
- `key == "recipe_category"` → 使用 `self.repos.categories`
- `key == "tags"` → 使用 `self.repos.tags`

Tool 没有对应的自动创建逻辑。

---

## 五、关联表与删除清理行为总结

| 关联表 | 定义 relationship 的对象 | 删除左表对象 | 删除右表对象 | 清理机制 |
|--------|------------------------|------------|------------|---------|
| recipes_to_tags | Tag（recipes） + Recipe（tags）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| recipes_to_categories | Category（recipes） + Recipe（recipe_category）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| recipes_to_tools | Tool（recipes） + Recipe（tools）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| cookbooks_to_tags | CookBook（tags）单向 | ✅ CookBook 删除时清理 | ❌ Tag 删除时不清理 | 仅 CookBook 侧定义 |
| cookbooks_to_categories | CookBook（categories）单向 | ✅ CookBook 删除时清理 | ❌ Category 删除时不清理 | 仅 CookBook 侧定义 |
| cookbooks_to_tools | CookBook（tools）单向 | ✅ CookBook 删除时清理 | ❌ Tool 删除时不清理 | 仅 CookBook 侧定义 |
| plan_rules_to_tags | GroupMealPlanRules（tags）单向 | ✅ MealPlanRules 删除时清理 | ❌ Tag 删除时不清理 | 仅 MealPlanRules 侧定义 |
| plan_rules_to_categories | GroupMealPlanRules（categories）单向 | ✅ MealPlanRules 删除时清理 | ❌ Category 删除时不清理 | 仅 MealPlanRules 侧定义 |
| households_to_tools | Tool（households_with_tool） + Household（tools_on_hand）双向 | ✅ Household 删除时清理 | ✅ Tool 删除时清理 | ORM 多对多双向 |
| group_to_categories | Group（categories）单向 | ✅ Group 删除时清理关联表行 | ❌ Category 删除时不清理 | 仅 Group 侧定义，无 cascade |
| plan_rules_to_households | GroupMealPlanRules（households）单向 | ✅ MealPlanRules 删除时清理 | ❌ Household 删除时不清理 | 仅 MealPlanRules 侧定义 |

**所有清理均不依赖数据库级 `ON DELETE CASCADE`，完全由 SQLAlchemy ORM 在 `session.delete()` 时根据对象自身定义的 relationship 处理。**
