# Mealie 标签、分类、工具与 Recipe 关联实现深度分析

## 一、作用范围差异

### 1.1 总体层级：均为 Group 级别，但有 Household 扩展

| 维度 | 标签 (Tag) | 分类 (Category) | 工具 (Tool) |
|------|-----------|-----------------|-------------|
| 所属级别 | Group 级别 | Group 级别 | Group 级别 |
| Group 唯一约束 | `tags_slug_group_id_key` | `category_slug_group_id_key` | `tools_slug_group_id_key` |
| 是否扩展到 Household | 否 | 否 | 是 (`households_to_tools`) |
| Repository 类型 | `RepositoryTags` | `RepositoryCategories` | `GroupRepositoryGeneric` |
| 自动创建能力 | Recipe 创建/更新时自动创建 | Recipe 创建/更新时自动创建 | 不支持自动创建 |

核心模型文件：
- [tag.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tag.py)
- [category.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/category.py)
- [tool.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tool.py)

### 1.2 各 taxonomy 的关联对象范围全景对比

#### 标签 (Tag) 的完整关联范围
定义在 [tag.py#L17-L54](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tag.py#L17-L54)：

```python
recipes_to_tags = Table(...)   # Tag ↔ Recipe
plan_rules_to_tags = Table(...)  # Tag ↔ MealPlanRules
cookbooks_to_tags = Table(...)   # Tag ↔ CookBook
```

关联对象：
1. **Recipe**（多对多）
2. **MealPlanRules**（多对多，随机选餐规则筛选）
3. **CookBook**（多对多，旧版筛选配置）
4. **Group**（级联拥有，`cascade="all, delete-orphan"`）

#### 分类 (Category) 的完整关联范围
定义在 [category.py#L17-L60](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/category.py#L17-L60)：

```python
group_to_categories = Table(...)        # Group ↔ Category (独有)
plan_rules_to_categories = Table(...)    # Category ↔ MealPlanRules
recipes_to_categories = Table(...)       # Category ↔ Recipe
cookbooks_to_categories = Table(...)     # Category ↔ CookBook
```

关联对象：
1. **Group**（独有 `group_to_categories` 直接关联表，Tag/Tool 无此表）
2. **MealPlanRules**
3. **Recipe**
4. **CookBook**
5. **Group 级联拥有**（通过 `group.categories` relationship）

#### 工具 (Tool) 的完整关联范围
定义在 [tool.py#L17-L57](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tool.py#L17-L57)：

```python
households_to_tools = Table(...)  # Household ↔ Tool (独有，手边工具)
cookbooks_to_tools = Table(...)   # Tool ↔ CookBook
```

关联对象：
1. **Household**（独有 `households_to_tools`，表示家庭手边有哪些工具可用）
2. **Recipe**
3. **CookBook**
4. **Group**（级联拥有，`cascade="all, delete-orphan"`）
5. **无 MealPlanRules 关联**（工具不参与随机选餐规则）

### 1.3 Recipe 模型中的关联定义
在 [recipe.py#L98-L102](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/recipe.py#L98-L102) 和 [recipe.py#L138](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/recipe.py#L138)：

```python
recipe_category: Mapped[list["Category"]] = orm.relationship(
    "Category", secondary=recipes_to_categories, back_populates="recipes"
)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", secondary=recipes_to_tools, back_populates="recipes")
tags: Mapped[list["Tag"]] = orm.relationship("Tag", secondary=recipes_to_tags, back_populates="recipes")
```

**关键实现细节**：Recipe 到这三者的 relationship **都没有配置 `cascade` 参数**，完全依赖 SQLAlchemy 对多对多关联表的默认行为（即删除任一侧时自动清理关联表行）。

### 1.4 CookBook 模型中的关联定义
在 [cookbook.py#L36-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/cookbook.py#L36-L48)：

```python
categories: Mapped[list[Category]] = orm.relationship(
    Category, secondary=cookbooks_to_categories, single_parent=True
)
tags: Mapped[list[Tag]] = orm.relationship(Tag, secondary=cookbooks_to_tags, single_parent=True)
tools: Mapped[list[Tool]] = orm.relationship(Tool, secondary=cookbooks_to_tools, single_parent=True)
```

CookBook 通过 Household 的 relationship 级联拥有：[household.py#L62](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/household.py#L62)：
```python
cookbooks: Mapped[list["CookBook"]] = orm.relationship("CookBook", **COMMON_ARGS)
# COMMON_ARGS = {"back_populates": "household", "cascade": "all, delete-orphan", "single_parent": True}
```

### 1.5 MealPlanRules 模型中的关联定义
在 [mealplan.py#L30-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/mealplan.py#L30-L48)：

```python
class GroupMealPlanRules(BaseMixins, SqlAlchemyBase):
    categories: Mapped[list[Category]] = orm.relationship(Category, secondary=plan_rules_to_categories)
    tags: Mapped[list[Tag]] = orm.relationship(Tag, secondary=plan_rules_to_tags)
    households: Mapped[list["Household"]] = orm.relationship("Household", secondary=plan_rules_to_households)
```

注意：**只有 categories 和 tags，没有 tools**。

### 1.6 Household 中的工具关联（独有）
在 [household.py#L77-L79](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/household.py#L77-L79)：

```python
tools_on_hand: Mapped[list["Tool"]] = orm.relationship(
    "Tool", secondary=households_to_tools, back_populates="households_with_tool"
)
```

反向定义在 [tool.py#L53-L56](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tool.py#L53-L56)：

```python
households_with_tool: Mapped[list["Household"]] = orm.relationship(
    "Household", secondary=households_to_tools, back_populates="tools_on_hand"
)
```

---

## 二、筛选参数的完整来源链路

筛选参数通过三层来源传入，最终在 `RepositoryRecipes.page_all()` → `_build_recipe_filter()` 中合并执行。

### 2.1 第一层：API 路由 Query 参数（直接传参）

#### 用户侧接口
在 [recipe_crud_routes.py#L340-L383](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/recipe/recipe_crud_routes.py#L340-L383)：

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

#### 公开探索接口
在 [controller_public_recipes.py#L30-L80](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/explore/controller_public_recipes.py#L30-L80) 参数与用户侧相同。

**直接 Query 参数清单**：
- `categories`：分类 ID 或 slug 列表（支持 UUID 和字符串 slug 混合输入）
- `tags`：标签 ID 或 slug 列表
- `tools`：工具 ID 或 slug 列表
- `foods`：食材 ID 或 slug 列表
- `households`：家庭 ID 或 slug 列表

### 2.2 第二层：RecipeSearchQuery 对象（逻辑控制）
定义在 [pagination.py#L22-L29](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/response/pagination.py#L22-L29)：

```python
class RecipeSearchQuery(MealieModel):
    cookbook: UUID4 | str | None = None
    require_all_categories: bool = False
    require_all_tags: bool = False
    require_all_tools: bool = False
    require_all_foods: bool = False
    search: str | None = None
```

**语义参数详解**：
- `require_all_*`: 控制筛选逻辑（`True`=AND 必须全部满足，`False`=OR 满足任一即可）
- `cookbook`: 通过 CookBook 筛选，会触发 CookBook 筛选配置的优先合并逻辑
- `search`: 全文搜索关键词（搜索 recipe 名称、描述等）

### 2.3 第三层：PaginationQuery（通用查询）
定义在 [pagination.py#L32-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/response/pagination.py#L32-L48)：

```python
class PaginationQuery(RequestQuery):
    page: int = 1
    per_page: int = 50
    # 继承自 RequestQuery:
    # order_by, order_direction, query_filter, pagination_seed
```

`query_filter` 是字符串表达式，通过 `QueryFilterBuilder` 解析，支持复杂过滤（如 `created_at > 2024-01-01`、属性比较、嵌套属性过滤等）。

### 2.4 CookBook 筛选配置（高优先级覆盖）
CookBook 模型的新旧两版筛选字段：[cookbook.py#L36-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/cookbook.py#L36-L48)：

```python
query_filter_string: Mapped[str]  # 新版：自定义查询字符串（推荐）
# 旧版（已弃用）：
categories: Mapped[list[Category]]
require_all_categories: FilterableColumn[bool]
tags: Mapped[list[Tag]]
require_all_tags: FilterableColumn[bool]
tools: Mapped[list[Tool]]
require_all_tools: FilterableColumn[bool]
```

**合并优先级逻辑**在 [repository_recipes.py#L243-L249](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L243-L249)：

```python
if cookbook:
    if pagination_result.query_filter and cookbook.query_filter_string:
        pagination_result.query_filter = (
            f"({pagination_result.query_filter}) AND ({cookbook.query_filter_string})"
        )
    else:
        pagination_result.query_filter = cookbook.query_filter_string
```

当提供 `cookbook` 参数时：
1. CookBook 的 `query_filter_string` 会与 `PaginationQuery.query_filter` 用 AND 合并
2. 直接传入的 `categories/tags/tools` Query 参数会被忽略

### 2.5 参数 ID/Slug 自动解析层
在 [repository_recipes.py#L183-L202](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L183-L202) 的 `_uuids_for_items` 方法：

```python
def _uuids_for_items(
    self, category_items: list | None, tag_items: list | None, tool_items: list | None, food_items: list | None
):
    def _uuid_from_str_or_uuid(items: list, model_class: type) -> list[UUID] | None:
        if items is None:
            return None
        result = []
        for item in items:
            if isinstance(item, UUID):
                result.append(item)
            else:
                item = str(item)
                stmt = select(model_class.id).filter(model_class.slug == item)
                result.extend(self.session.execute(stmt).scalars().all())
        return result if result else None
```

处理规则：
- UUID 格式 → 直接使用
- 字符串 slug → 查表转换为对应模型的 ID

### 2.6 筛选逻辑执行层
核心在 [repository_recipes.py#L295-L337](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L295-L337) 的 `_build_recipe_filter`：

**AND 逻辑**（require_all_* = True）：
```python
for category_id in category_ids:
    stmt = stmt.where(RecipeModel.recipe_category.any(Category.id == category_id))
```

**OR 逻辑**（require_all_* = False，默认）：
```python
stmt = stmt.where(RecipeModel.recipe_category.any(Category.id.in_(category_ids)))
```

同时自动附加 `group_id` 和 `household_id` 过滤条件（取决于 Repository 的作用域）。

---

## 三、删除清理路径全景分析

### 3.0 核心架构：ORM 级 vs 数据库级

**关键发现**：所有关联表的数据库外键约束都没有配置 `ON DELETE CASCADE`。

在 Alembic 迁移中可以看到，例如 [2022-02-21-19.56.24_6b0f5f32d602_initial_tables.py#L573-L597](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/alembic/versions/2022-02-21-19.56.24_6b0f5f32d602_initial_tables.py#L573-L597)：

```python
sa.ForeignKeyConstraint(["category_id"], ["categories.id"]),  # 无 ondelete 参数
sa.ForeignKeyConstraint(["recipe_id"], ["recipes.id"]),        # 无 ondelete 参数
```

这意味着：**所有关联表的清理完全依赖 SQLAlchemy ORM 层的处理，而非数据库外键级联**。SQLAlchemy 在 `session.delete(obj)` 时，会自动追踪并清理多对多 `secondary` 关联表中的对应行。

### 3.1 删除单个 Tag / Category / Tool

#### 统一删除入口
三者共用 [repository_generic.py#L256-L269](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_generic.py#L256-L269) 的 `delete` 方法：

```python
def delete(self, value, match_key: str | None = None) -> Schema:
    result = self._query_one(value, match_key)
    self.session.delete(result)  # ORM 删除
    self.session.commit()
    return self.schema.model_validate(result)
```

#### API 路由层注释声明
- Tag 删除：[controller_tags.py#L97-L101](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_tags.py#L97-L101)
- Category 删除：[controller_categories.py#L108-L112](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_categories.py#L108-L112)
- Tool 删除：[controller_tools.py#L97-L101](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_tools.py#L97-L101)

均声明："Deleting a tag does not impact a recipe. The tag will be removed from any recipes that contain it"

#### 清理触发链（以删除 Tag 为例）

```
session.delete(tag)
    │
    ├── ORM 自动清理 recipes_to_tags 中 tag_id=该 id 的行
    ├── ORM 自动清理 cookbooks_to_tags 中 tag_id=该 id 的行
    └── ORM 自动清理 plan_rules_to_tags 中 tag_id=该 id 的行
```

各类的具体清理范围：

| 关联表 | Tag | Category | Tool |
|-------|-----|----------|------|
| `recipes_to_*` | ✅ | ✅ | ✅ |
| `cookbooks_to_*` | ✅ | ✅ | ✅ |
| `plan_rules_to_*` | ✅ | ✅ | ❌ |
| `households_to_tools` | ❌ | ❌ | ✅ |
| `group_to_categories` | ❌ | ✅ | ❌ |

### 3.2 删除 Recipe

#### 专用删除实现
Recipe 删除逻辑在 [repository_recipes.py#L110-L130](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L110-L130)：

```python
def _delete_recipe(self, recipe: Recipe) -> Recipe:
    # Clear users_to_recipes first, otherwise the recipe can't be deleted
    stmt = delete(users_to_recipes).where(users_to_recipes.c.recipe_id == recipe.id)
    self.session.execute(stmt)

    self.session.delete(recipe)
    return recipe
```

#### 清理触发链

```
session.delete(recipe)
    │
    ├── 先显式删除 users_to_recipes 关联（避免级联异常）
    ├── ORM 自动清理 recipes_to_tags 中 recipe_id=该 id 的行
    ├── ORM 自动清理 recipes_to_categories 中 recipe_id=该 id 的行
    └── ORM 自动清理 recipes_to_tools 中 recipe_id=该 id 的行
```

#### PostgreSQL 批量级联 Bug 绕过
在 [repository_recipes.py#L137-L157](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L137-L157)：

```python
def delete_many(self, values: Iterable) -> list[Recipe]:
    result = [self._delete_recipe(r) for r in self._query_many(values)]
    self.session.commit()
    return result
```

注释明确指出：
```python
# we don't delete the whole query in one statement because postgres doesn't cascade correctly
```

因此批量删除 Recipe 时，使用逐条 `session.delete()` 而非单条 `delete()` 语句，确保 ORM 能正确追踪所有关联表清理。

### 3.3 删除 CookBook

#### 删除入口
CookBook 删除在 [controller_cookbooks.py#L136-L148](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/households/controller_cookbooks.py#L136-L148)：

```python
@router.delete("/{item_id}", response_model=ReadCookBook)
def delete_one(self, item_id: str):
    cookbook = self.mixins.delete_one(item_id)
    # 发布事件通知
```

底层使用 `RepositoryCookbooks` → 继承 [repository_generic.py#L256-L269](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_generic.py#L256-L269) 的 `delete`。

#### 清理触发链

```
session.delete(cookbook)
    │
    ├── ORM 自动清理 cookbooks_to_tags 中 cookbook_id=该 id 的行
    ├── ORM 自动清理 cookbooks_to_categories 中 cookbook_id=该 id 的行
    └── ORM 自动清理 cookbooks_to_tools 中 cookbook_id=该 id 的行
```

**注意**：CookBook 删除仅清理自身与 taxonomy 的关联，不影响 Recipe 与 taxonomy 的关联。

### 3.4 删除 MealPlan（餐单条目）

#### 删除入口
MealPlan 删除在 [controller_mealplan.py#L198-L217](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/households/controller_mealplan.py#L198-L217)：

```python
@router.delete("/{item_id}", response_model=ReadPlanEntry)
def delete_one(self, item_id: int):
    result = self.mixins.delete_one(item_id)
```

底层使用 `RepositoryMeals` → 继承 `HouseholdRepositoryGeneric` → `RepositoryGeneric.delete`。

#### 清理范围
`GroupMealPlan` 模型中与 taxonomy 无直接多对多关系（只关联 categories/tags 是在 `GroupMealPlanRules` 上），因此 MealPlan 删除不涉及 taxonomy 关联表清理。仅删除该条目本身。

### 3.5 删除 MealPlanRules（随机选餐规则）

#### 删除入口
MealPlanRules 删除在 [controller_mealplan_rules.py#L50-L52](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/households/controller_mealplan_rules.py#L50-L52)：

```python
@router.delete("/{item_id}", response_model=PlanRulesOut)
def delete_one(self, item_id: UUID4):
    return self.mixins.delete_one(item_id)
```

底层使用 `RepositoryMealPlanRules` → 继承 `HouseholdRepositoryGeneric` → `RepositoryGeneric.delete`。

#### 清理触发链

```
session.delete(meal_plan_rule)
    │
    ├── ORM 自动清理 plan_rules_to_tags 中 plan_rule_id=该 id 的行
    ├── ORM 自动清理 plan_rules_to_categories 中 plan_rule_id=该 id 的行
    └── ORM 自动清理 plan_rules_to_households 中 group_plan_rule_id=该 id 的行
```

### 3.6 删除 Household

#### Household 的级联关系定义
在 [household.py#L40-L67](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/household.py#L40-L67)：

```python
COMMON_ARGS = {
    "back_populates": "household",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}

preferences: Mapped["HouseholdPreferencesModel"] = orm.relationship(..., cascade="all, delete-orphan")
invite_tokens: Mapped[list["GroupInviteToken"]] = orm.relationship(..., cascade="all, delete-orphan")
recipe_actions: Mapped[list["GroupRecipeAction"]] = orm.relationship("GroupRecipeAction", **COMMON_ARGS)
cookbooks: Mapped[list["CookBook"]] = orm.relationship("CookBook", **COMMON_ARGS)
webhooks: Mapped[list["GroupWebhooksModel"]] = orm.relationship("GroupWebhooksModel", **COMMON_ARGS)
group_event_notifiers: ... = orm.relationship(..., **COMMON_ARGS)
```

#### 删除触发链（级联展开）

```
session.delete(household)
    │
    ├── cascade="all, delete-orphan" 触发子对象级联删除
    │   ├── 删除 HouseholdPreferences
    │   ├── 删除所有 InviteTokens
    │   ├── 删除所有 RecipeActions
    │   ├── 删除所有 CookBooks
    │   │   └── 每个 CookBook 删除触发 3.3 节关联表清理
    │   │       ├── cookbooks_to_tags
    │   │       ├── cookbooks_to_categories
    │   │       └── cookbooks_to_tools
    │   ├── 删除所有 Webhooks
    │   └── 删除所有 EventNotifiers
    │
    ├── tools_on_hand 是多对多关系（无 cascade）
    │   └── ORM 自动清理 households_to_tools 中 household_id=该 id 的行
    │
    ├── made_recipes 是多对多关系（无 cascade）
    │   └── ORM 自动清理 household_to_recipe 中 household_id=该 id 的行
    │
    └── ingredient_foods_on_hand 是多对多关系（无 cascade）
        └── ORM 自动清理 households_to_ingredient_foods 中 household_id=该 id 的行
```

**注意**：Household 删除不直接删除其下的 recipes（recipes 归属于 Group），也不直接删除 Group 级的 taxonomy（tags/categories/tools）。

### 3.7 删除 Group（最高层级）

#### Group 的级联关系定义
在 [group.py#L66-L92](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L66-L92)：

```python
common_args = {
    "back_populates": "group",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}

# 级联删除：
mealplans: Mapped[list[GroupMealPlan]] = orm.relationship(GroupMealPlan, **common_args)
cookbooks: Mapped[list[CookBook]] = orm.relationship(CookBook, **common_args)
shopping_lists: ... = orm.relationship(..., **common_args)
ingredient_units: ... = orm.relationship(..., **common_args)
ingredient_foods: ... = orm.relationship(..., **common_args)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", **common_args)
tags: Mapped[list["Tag"]] = orm.relationship("Tag", **common_args)
labels: Mapped[list[MultiPurposeLabel]] = orm.relationship(..., **common_args)
# ... 其他
```

另外 Group 对 categories 的关系在 [group.py#L40](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L40)：

```python
categories: Mapped[list[Category]] = orm.relationship(Category, secondary=group_to_categories, single_parent=True)
```

**注意**：categories 的 relationship 没有显式指定 `cascade` 参数，但 Group 删除仍会触发 cascade。

#### 删除触发链（完整级联展开）

```
session.delete(group)
    │
    ├── cascade="all, delete-orphan" 触发第一层级联
    │   ├── 删除所有 GroupPreferences / AIProviderSettings
    │   ├── 删除所有 InviteTokens
    │   ├── 删除所有 MealPlans (GroupMealPlan)
    │   ├── 删除所有 CookBooks (每个触发 3.3 节)
    │   ├── 删除所有 ShoppingLists
    │   ├── 删除所有 RecipeActions
    │   ├── 删除所有 Webhooks / EventNotifiers
    │   ├── 删除所有 DataExports / Reports
    │   ├── 删除所有 ServerTasks
    │   │
    │   ├── 删除所有 IngredientUnits
    │   ├── 删除所有 IngredientFoods
    │   ├── 删除所有 Labels (MultiPurposeLabel)
    │   │
    │   ├── 删除所有 Tags (每个触发 3.1 节 Tag 清理)
    │   │   └── 每个 Tag 删除 → 清理 recipes_to_tags, cookbooks_to_tags, plan_rules_to_tags
    │   │
    │   ├── 删除所有 Tools (每个触发 3.1 节 Tool 清理)
    │   │   └── 每个 Tool 删除 → 清理 recipes_to_tools, cookbooks_to_tools, households_to_tools
    │   │
    │   └── 删除所有 Categories (每个触发 3.1 节 Category 清理)
    │       └── 每个 Category 删除 → 清理 recipes_to_categories, cookbooks_to_categories, plan_rules_to_categories, group_to_categories
    │
    ├── group.categories (secondary=group_to_categories) 无 cascade
    │   └── ORM 自动清理 group_to_categories 中 group_id=该 id 的行
    │
    ├── group.households 无 cascade
    │   └── ORM 将 households 的 group_id 置 NULL（但 household.group_id 是 NOT NULL，
    │       实际会由应用层先处理或删除 households）
    │
    ├── group.users 无 cascade
    │   └── ORM 将 users 的 group_id 置 NULL
    │
    └── group.recipes 无 cascade
        └── ORM 将 recipes 的 group_id 置 NULL（recipe.group_id 是 NOT NULL，
            实际会先处理 recipes）
```

### 3.8 Recipe 更新时空列表的显式处理

在 [_model_base.py#L36-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/_model_base.py#L36-L48) 的 `BaseMixins.update`：

```python
def update(self, *args, **kwargs):
    self.__init__(*args, **kwargs)
    # sqlalchemy doesn't like this method to remove all instances of a 1:many relationship,
    # so we explicitly check for that here
    for k, v in kwargs.items():
        if hasattr(self, k) and v == []:
            setattr(self, k, v)
```

当 Recipe 更新传入 `tags=[]`、`recipe_category=[]` 或 `tools=[]` 时，代码显式调用 `setattr` 将关联置空，确保 ORM 正确追踪并删除关联表中对应记录。

### 3.9 "空" Taxonomy 检测 API

代码提供了检测未被任何 Recipe 引用的 Tag/Category 的功能（Tool 暂无此 API）：

- Tag 空检测 [repository_factory.py#L99-L102](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_factory.py#L99-L102)：
  ```python
  def get_empty(self) -> Sequence[Tag]:
      stmt = select(Tag).filter(~Tag.recipes.any())
      return self.session.execute(stmt).scalars().all()
  ```
- Category 空检测 [repository_factory.py#L92-L96](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_factory.py#L92-L96)：同上逻辑

对应 API 端点：
- `GET /organizers/tags/empty` [controller_tags.py#L41-L44](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_tags.py#L41-L44)
- `GET /organizers/categories/empty` [controller_categories.py#L72-L75](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_categories.py#L72-L75)

---

## 四、Schema 数据结构参考

### 4.1 Pydantic Schema 继承关系

```
RecipeTag (基础，仅 name + id)
├── RecipeCategory (直接继承 RecipeTag，无新增字段)
└── RecipeTool (扩展 households_with_tool 字段)
```

定义在 [recipe.py#L61-L99](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe.py#L61-L99)。

### 4.2 创建/保存 Schema 转换

| 类型 | 输入 Schema | 保存 Schema (注入 group_id) | 文件 |
|------|------------|---------------------------|------|
| Tag | `TagIn` (name) | `TagSave` (name + group_id) | [recipe_category.py#L35-L40](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_category.py#L35-L40) |
| Category | `CategoryIn` (name) | `CategorySave` (name + group_id) | [recipe_category.py#L9-L15](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_category.py#L9-L15) |
| Tool | `RecipeToolCreate` (name + households_with_tool) | `RecipeToolSave` (+ group_id) | [recipe_tool.py#L9-L16](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_tool.py#L9-L16) |

### 4.3 Recipe 创建/更新时的 taxonomy 自动创建
在 [recipe_service.py#L255-L291](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/services/recipe/recipe_service.py#L255-L291) 的 `_transform_category_or_tag`：

```python
def _transform_category_or_tag(cls, data, value, schema_in, repos, target_attr):
    ...
    if not item_repo.get_one(item.get("slug"), "slug"):
        item_repo.create(
            schema_in.model_validate(item).cast(
                schema_in.SaveSchema, group_id=data.group_id
            )
        )
```

当 Recipe 数据中传入的 Tag/Category 通过 slug 无法在数据库中找到时，会自动创建对应的 Tag/Category。**Tool 无此自动创建逻辑**。

---

## 五、关联表与删除清理矩阵总结

| 关联表 | 左表 | 右表 | 删除左表时 | 删除右表时 | 清理机制 |
|--------|------|------|-----------|-----------|---------|
| `recipes_to_tags` | recipes | tags | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `recipes_to_categories` | recipes | categories | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `recipes_to_tools` | recipes | tools | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `cookbooks_to_tags` | cookbooks | tags | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `cookbooks_to_categories` | cookbooks | categories | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `cookbooks_to_tools` | cookbooks | tools | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `plan_rules_to_tags` | group_meal_plan_rules | tags | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `plan_rules_to_categories` | group_meal_plan_rules | categories | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `households_to_tools` | households | tools | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `group_to_categories` | groups | categories | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |
| `plan_rules_to_households` | group_meal_plan_rules | households | ✅ 清理 | ✅ 清理 | ORM 多对多默认 |

**所有这些清理都不依赖数据库级 `ON DELETE CASCADE`，完全由 SQLAlchemy ORM 在 `session.delete()` 时自动处理。**
