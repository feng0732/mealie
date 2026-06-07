# Mealie 标签、分类、工具与 Recipe 关联实现核准分析

## 一、作用范围差异

### 1.1 基本层级

| 维度 | 标签 Tag | 分类 Category | 工具 Tool |
|------|---------|--------------|----------|
| 所属层级 | Group 级 | Group 级 | Group 级 |
| 唯一约束 | (slug, group_id) | (slug, group_id) | (slug, group_id) |
| 归属方式 | group_id 外键 + 一对多 | group_id 外键 + 一对多；另有 group_to_categories 多对多表（设计冗余/不一致） | group_id 外键 + 一对多 |
| 是否关联 Household | 否 | 否 | 是（households_to_tools 表） |
| 是否关联 MealPlanRules | 是 | 是 | 否 |
| Recipe 创建时自动创建 | 是 | 是 | 否 |
| 提供"空"检测 API | 是 | 是 | 否 |
| Repository | RepositoryTags（继承 GroupRepositoryGeneric，有 get_empty） | RepositoryCategories（继承 GroupRepositoryGeneric，有 get_empty） | GroupRepositoryGeneric（无自定义方法） |

### 1.2 各 taxonomy 定义的 Relationship

核心规则：**只有在模型中明确定义了 `relationship(..., secondary=...)` 的对象，删除该对象时 ORM 才会自动清理对应 secondary 关联表的行**。没有在该侧定义反向 relationship 的，删除时不会自动清理。

**标签 Tag**（mealie/db/models/recipe/tag.py）:
- `group`: 通过外键 group_id 与 Group 多对一，`back_populates="tags"`
- `recipes`: 通过 `recipes_to_tags`（sa.Table）与 Recipe 多对多，`back_populates="tags"`（双向）
- **未定义**：cookbooks、plan_rules 的反向 relationship

**分类 Category**（mealie/db/models/recipe/category.py）:
- `group`: 通过外键 group_id 与 Group 多对一，`back_populates="categories"`
- `recipes`: 通过 `recipes_to_categories`（sa.Table）与 Recipe 多对多，`back_populates="recipe_category"`（双向）
- **未定义**：cookbooks、plan_rules、group_to_categories 的反向 relationship
- 注意：`group.back_populates="categories"` 与 Group.categories 的实际定义不一致（见第三章）

**工具 Tool**（mealie/db/models/recipe/tool.py）:
- `group`: 通过外键 group_id 与 Group 多对一，`back_populates="tools"`
- `recipes`: 通过 `recipes_to_tools`（sa.Table）与 Recipe 多对多，`back_populates="tools"`（双向）
- `households_with_tool`: 通过 `households_to_tools`（sa.Table）与 Household 多对多，`back_populates="tools_on_hand"`（双向）
- **未定义**：cookbooks 的反向 relationship

### 1.3 其他对象到 taxonomy 的 Relationship

**Recipe**（mealie/db/models/recipe/recipe.py）到 taxonomy 的关系（均无 cascade 参数，双向）:
- `recipe_category: Mapped[list[Category]]`，secondary=recipes_to_categories，back_populates="recipes"
- `tools: Mapped[list[Tool]]`，secondary=recipes_to_tools，back_populates="recipes"
- `tags: Mapped[list[Tag]]`，secondary=recipes_to_tags，back_populates="recipes"

**CookBook**（mealie/db/models/household/cookbook.py）单向关系（均无 back_populates）:
- `categories: Mapped[list[Category]]`，secondary=cookbooks_to_categories，single_parent=True
- `tags: Mapped[list[Tag]]`，secondary=cookbooks_to_tags，single_parent=True
- `tools: Mapped[list[Tool]]`，secondary=cookbooks_to_tools，single_parent=True

**GroupMealPlanRules**（mealie/db/models/household/mealplan.py）单向关系（均无 back_populates，无 tools）:
- `categories: Mapped[list[Category]]`，secondary=plan_rules_to_categories
- `tags: Mapped[list[Tag]]`，secondary=plan_rules_to_tags
- `households: Mapped[list[Household]]`，secondary=plan_rules_to_households

**Group**（mealie/db/models/group/group.py）:
- `tags: Mapped[list[Tag]]`：一对多（Tag.group_id 外键），back_populates="group"，cascade="all, delete-orphan"
- `tools: Mapped[list[Tool]]`：一对多（Tool.group_id 外键），back_populates="group"，cascade="all, delete-orphan"
- `categories: Mapped[list[Category]]`：**多对多**（secondary=group_to_categories），single_parent=True，**无 back_populates、无 cascade 参数**
- 注：Category 也通过 group_id 外键直接归属 Group，此处存在双路径归属

**Household**（mealie/db/models/household/household.py）:
- `tools_on_hand: Mapped[list[Tool]]`：secondary=households_to_tools，back_populates="households_with_tool"（双向）
- `cookbooks: Mapped[list[CookBook]]`：一对多（CookBook.household_id 外键），cascade="all, delete-orphan"
- `made_recipes: Mapped[list[RecipeModel]]`：secondary=HouseholdToRecipe.__tablename__（表名字符串），back_populates="made_by"（双向）

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

说明：
- `cookbook`：ID 或 slug，指定后走 CookBook 筛选路径
- `require_all_*`：`False`=OR（满足任一即可），`True`=AND（必须全部满足）；**路由层默认值为 False**。虽然 RepositoryRecipes.page_all 签名中这些参数默认 True，但实际调用时被 search_query 的值覆盖。
- `search`：全文搜索关键词

**第三层：PaginationQuery**（mealie/schema/response/pagination.py）

```python
class RequestQuery(MealieModel):
    order_by: str | None = None
    order_direction: OrderDirection = OrderDirection.desc
    query_filter: str | None = None        # 自由格式字符串表达式过滤
    pagination_seed: str | None = None

class PaginationQuery(RequestQuery):
    page: int = 1
    per_page: int = 50
```

`query_filter` 通过 QueryFilterBuilder 解析为 SQL WHERE 子句。

### 2.2 CookBook 筛选与参数合并优先级

CookBook 模型保存两套筛选配置（mealie/db/models/household/cookbook.py）:

```python
query_filter_string: Mapped[str]          # 新版：自定义查询字符串（推荐）
categories: Mapped[list[Category]]        # 旧版（已弃用）
require_all_categories: FilterableColumn[bool | None]
tags: Mapped[list[Tag]]
require_all_tags: FilterableColumn[bool | None]
tools: Mapped[list[Tool]]
require_all_tools: FilterableColumn[bool | None]
```

RepositoryRecipes.page_all 中的逻辑（mealie/repos/repository_recipes.py）：

```python
if cookbook:
    # CookBook 的 query_filter_string 与 PaginationQuery.query_filter 用 AND 合并
    if pagination_result.query_filter and cookbook.query_filter_string:
        pagination_result.query_filter = (
            f"({pagination_result.query_filter}) AND ({cookbook.query_filter_string})"
        )
    else:
        pagination_result.query_filter = cookbook.query_filter_string
    # 注意：此分支下完全跳过 categories/tags/tools/foods 参数的处理
else:
    # 否则走常规 categories/tags/tools/foods 筛选路径
    category_ids = self._uuids_for_items(categories, Category)
    tag_ids = self._uuids_for_items(tags, Tag)
    tool_ids = self._uuids_for_items(tools, Tool)
    household_ids = self._uuids_for_items(households, Household)
    filters = self._build_recipe_filter(...)
    q = q.filter(*filters)
```

结论：当指定 `cookbook` 参数时：
1. CookBook 的 `query_filter_string` 与 `PaginationQuery.query_filter` 用 AND 合并
2. 直接传入的 `categories/tags/tools/foods` Query 参数被**完全忽略**
3. CookBook 旧版的 categories/tags/tools 字段在 page_all 中**未被读取**

### 2.3 ID/Slug 自动解析

`_uuids_for_items` 方法（mealie/repos/repository_recipes.py）:

```python
def _uuids_for_items(self, items: list[UUID | str] | None, model: type[SqlAlchemyBase]) -> list[UUID] | None:
```

处理逻辑：
- UUID 类型：直接加入结果
- 字符串：尝试解析为 UUID，成功则加入；失败则作为 slug
- 对所有 slug 执行 `select(model.id).filter(model.slug.in_(slugs))`，合并到结果

### 2.4 筛选执行逻辑

`_build_recipe_filter` 方法（mealie/repos/repository_recipes.py）构建条件：

```python
# AND 逻辑（require_all_* = True）
for category_id in categories:
    fltr.append(RecipeModel.recipe_category.any(Category.id == category_id))

# OR 逻辑（require_all_* = False，默认）
fltr.append(RecipeModel.recipe_category.any(Category.id.in_(categories)))
```

同时自动附加：
- 若 Repository 设置了 group_id：附加 `RecipeModel.group_id == self.group_id`
- 若 Repository 设置了 household_id：附加 `RecipeModel.household_id == self.household_id`

---

## 三、Group/Category 关系的不一致设计

Group 对三个 taxonomy 的关系定义存在明显不一致：

### Tag 和 Tool（一致的一对多模式）

Tag 侧：
```python
group_id: ... mapped_column(..., ForeignKey("groups.id"), nullable=False)
group: Mapped["Group"] = orm.relationship("Group", back_populates="tags", foreign_keys=[group_id])
```

Group 侧：
```python
common_args = {"back_populates": "group", "cascade": "all, delete-orphan", "single_parent": True}
tags: Mapped[list["Tag"]] = orm.relationship("Tag", **common_args)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", **common_args)
```

特征：
- 双向一对多关系，通过外键 group_id
- 两侧 `back_populates` 正确匹配（Tag.group ↔ Group.tags，Tool.group ↔ Group.tools）
- Group 侧带 `cascade="all, delete-orphan"`，删除 Group 时级联删除 Tag/Tool 对象本身

### Category（不一致的双路径归属）

Category 侧：
```python
group_id: ... mapped_column(..., ForeignKey("groups.id"), nullable=False)
group: Mapped["Group"] = orm.relationship("Group", back_populates="categories", foreign_keys=[group_id])
```

Group 侧：
```python
categories: Mapped[list[Category]] = orm.relationship(
    Category,
    secondary=group_to_categories,    # 多对多关联表，而非外键反向
    single_parent=True
    # 无 back_populates 参数
    # 无 cascade 参数
)
```

问题点：
1. **back_populates 不匹配**：Category.group 声明 `back_populates="categories"`，期望 Group.categories 是外键反向的一对多，但 Group.categories 实际是基于 `group_to_categories` 表的多对多。两者类型不一致，back_populates 无法正确生效。
2. **双路径归属**：Category 同时通过 (a) 外键 group_id 直接归属 Group，(b) 多对多表 group_to_categories 关联 Group。存在语义冗余。
3. **无 cascade**：Group.categories 无 cascade 参数，删除 Group 时不会通过此 relationship 级联删除 Category 对象本身；但 Category.group_id 是 NOT NULL 外键，删除 Group 会因外键约束失败而需预先单独处理 Category。

---

## 四、删除清理路径核准

### 4.0 基础机制

所有关联表的数据库外键约束**均未配置 `ON DELETE CASCADE`**（Alembic 迁移文件中 ForeignKeyConstraint 均无 ondelete 参数）。关联清理完全依赖 SQLAlchemy ORM 的行为。

SQLAlchemy ORM 对 secondary 关联表的清理规则：
- **对于 `sa.Table` 类型的纯关联表**：若对象 A 定义了 `relationship(..., secondary=table_obj, back_populates="...")`，则 `session.delete(A)` 时会自动清理该表中对应 A 的行。双向关系中任一侧删除均会清理。
- **对于仅单侧定义 relationship 的 sa.Table**：只有定义了 relationship 的那一侧被删除时才会清理；另一侧被删除时不会。
- **对于 secondary 指向实体类（带业务字段的关联对象）**：情况复杂，ORM 可能不会当作简单关联表处理，常需代码显式清理。

### 4.1 删除单个 Tag / Category / Tool

三者共用 RepositoryGeneric.delete：

```python
def delete(self, value, match_key: str | None = None) -> Schema:
    match_key = match_key or self.primary_key
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
- `recipes_to_tags`：会被清理（Tag 定义了 `recipes` 双向 relationship，sa.Table secondary）
- `cookbooks_to_tags`：**不会自动清理**（Tag 未定义 cookbooks 反向 relationship）
- `plan_rules_to_tags`：**不会自动清理**（Tag 未定义 plan_rules 反向 relationship）

**删除 Category 时的清理范围**：
- `recipes_to_categories`：会被清理（Category 定义了 `recipes` 双向 relationship）
- `cookbooks_to_categories`：**不会自动清理**
- `plan_rules_to_categories`：**不会自动清理**
- `group_to_categories`：**不会自动清理**（Category 未定义反向）

**删除 Tool 时的清理范围**：
- `recipes_to_tools`：会被清理（Tool 定义了 `recipes` 双向 relationship）
- `households_to_tools`：会被清理（Tool 定义了 `households_with_tool` 双向 relationship）
- `cookbooks_to_tools`：**不会自动清理**（Tool 未定义 cookbooks 反向）

API 文档注释（mealie/routes/organizers/controller_tags.py 等）声明：
> "Deleting a tag does not impact a recipe. The tag will be removed from any recipes that contain it"

该声明仅对应 recipes_to_* 表的清理，未提及 cookbooks_to_* 和 plan_rules_to_* 表。

### 4.2 删除 Recipe

专用实现位于 mealie/repos/repository_recipes.py。

#### 单条删除 `delete` → `_delete_recipe`

```python
def _delete_recipe(self, recipe: RecipeModel) -> Recipe:
    recipe_as_model = self.schema.model_validate(recipe)

    # 第一步：显式删除 UserToRecipe 关联记录
    try:
        user_to_recipe_delete_query = sa.delete(UserToRecipe).where(UserToRecipe.recipe_id == recipe.id)
        self.session.execute(user_to_recipe_delete_query)
        self.session.commit()   # 第一次 commit
    except Exception:
        self.session.rollback()
        raise

    # 第二步：删除 Recipe 本身
    try:
        self.session.delete(recipe)
        self.session.commit()   # 第二次 commit
    except Exception:
        self.session.rollback()
        raise

    return recipe_as_model
```

关键细节：
1. **两次独立 commit**：先单独 commit UserToRecipe 的删除，再单独 commit Recipe 的删除。
2. **UserToRecipe 需显式清理**：UserToRecipe 不是简单 sa.Table，而是继承 SqlAlchemyBase/BaseMixins 的**实体类**（带 id、rating、is_favorite 业务字段，还有 after_insert/after_update/after_delete event listener 用于更新 Recipe.rating）。ORM 无法把它当作简单 secondary 表自动清理，因此代码用 `sa.delete(UserToRecipe)` 显式 SQL 删除。
3. Recipe 中与 User 的关系：
   - `rated_by`：`secondary=UserToRecipe.__tablename__`，多对多
   - `favorited_by`：同上，附加 is_favorite=True 条件
   这两个 relationship 都不会自动清理 UserToRecipe 行，因为 secondary 指向的是实体类而非纯 Table。

#### `session.delete(recipe)` 触发的 ORM 自动清理

- `recipes_to_tags`（sa.Table，Recipe.tags 双向）：自动清理
- `recipes_to_categories`（sa.Table，Recipe.recipe_category 双向）：自动清理
- `recipes_to_tools`（sa.Table，Recipe.tools 双向）：自动清理
- 所有带 `cascade="all, delete-orphan"` 或 `cascade="all, delete, delete-orphan"` 的子对象：
  - assets、nutrition、recipe_ingredient、recipe_instructions、notes、extras、settings
  - share_tokens、comments、timeline_events
  - meal_entries（GroupMealPlan）
  - shopping_list_refs、shopping_list_item_refs
- 带 cascade 删除的子对象自身还可能触发更深层清理。

#### `HouseholdToRecipe` 清理情况

- Recipe.made_by: `orm.relationship("Household", secondary=HouseholdToRecipe.__tablename__, back_populates="made_by")`
- HouseholdToRecipe 同样是**实体类**（带 id、last_made 字段，有 event listener 更新 Recipe.last_made）
- `_delete_recipe` 中**没有显式清理** HouseholdToRecipe 记录
- secondary 参数传的是字符串表名 `HouseholdToRecipe.__tablename__`（"households_to_recipes"），而非 Table 对象
- 这种情况下 session.delete(recipe) 是否会清理 households_to_recipes 取决于 SQLAlchemy 对 secondary 字符串表名的处理，代码中未对此做显式保证。

#### 批量删除 `delete_many`

```python
def delete_many(self, values: Iterable) -> list[Recipe]:
    query = self._query().filter(self.model.slug.in_(values)).filter_by(**self._filter_builder())
    recipes_in_db = self.session.execute(query).unique().scalars().all()
    results: list[Recipe] = []

    for recipe_in_db in recipes_in_db:
        results.append(self._delete_recipe(recipe_in_db))  # 每条都触发两次内部 commit

    try:
        self.session.commit()  # 额外一次 commit（此时 Session 已在 _delete_recipe 中提交过）
    except Exception as e:
        self.session.rollback()
        raise e

    return results
```

使用逐条调用 `_delete_recipe` 而非单条 SQL DELETE，注释（继承自 RepositoryGeneric）说明原因：
> "we don't delete the whole query in one statement because postgres doesn't cascade correctly"

### 4.3 删除 CookBook

入口：mealie/routes/households/controller_cookbooks.py → RepositoryCookbooks.delete（继承 RepositoryGeneric.delete）。

清理触发链（session.delete(cookbook)）：
- `cookbooks_to_tags`：清理（CookBook 定义了 `tags` relationship，sa.Table secondary）
- `cookbooks_to_categories`：清理（CookBook 定义了 `categories` relationship）
- `cookbooks_to_tools`：清理（CookBook 定义了 `tools` relationship）

CookBook 删除仅清理自身与 taxonomy 的关联，不影响 Recipe 与 taxonomy 的关联。

级联来源：
- Household 级联：CookBook 通过 `Household.cookbooks`（cascade="all, delete-orphan"）被级联删除
- Group 级联：CookBook 通过 `Group.cookbooks`（cascade="all, delete-orphan"）被级联删除

### 4.4 删除 MealPlan（GroupMealPlan）

入口：mealie/routes/households/controller_mealplan.py → RepositoryMeals.delete。

GroupMealPlan 与 taxonomy 无直接多对多关系（categories/tags 关联在 GroupMealPlanRules 上），因此 MealPlan 删除**不涉及** taxonomy 关联表清理。

但 GroupMealPlan 通过外键 `recipe_id` 关联 Recipe，Recipe.meal_entries 带 `cascade="all, delete-orphan"`，所以删除 Recipe 时会级联删除引用它的 GroupMealPlan 条目。

### 4.5 删除 MealPlanRules（GroupMealPlanRules）

入口：mealie/routes/households/controller_mealplan_rules.py → RepositoryMealPlanRules.delete。

清理触发链（session.delete(meal_plan_rule)）：
- `plan_rules_to_tags`：清理（GroupMealPlanRules 定义了 `tags` relationship，sa.Table secondary）
- `plan_rules_to_categories`：清理（GroupMealPlanRules 定义了 `categories` relationship）
- `plan_rules_to_households`：清理（GroupMealPlanRules 定义了 `households` relationship）

### 4.6 删除 Household

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

多对多关系：
- `made_recipes`: secondary=HouseholdToRecipe.__tablename__（表名字符串），back_populates="made_by"（双向，实体类 secondary）
- `ingredient_foods_on_hand`: secondary=households_to_ingredient_foods（sa.Table），back_populates="households_with_ingredient_food"（双向）
- `tools_on_hand`: secondary=households_to_tools（sa.Table），back_populates="households_with_tool"（双向）

删除触发链：
1. cascade="all, delete-orphan" 级联删除：
   - HouseholdPreferences
   - 所有 InviteTokens
   - 所有 RecipeActions
   - 所有 CookBooks → 每个 CookBook 删除触发 cookbooks_to_tags/categories/tools 的清理（见 4.3）
   - 所有 Webhooks
   - 所有 EventNotifiers
2. sa.Table secondary 表由 ORM 自动清理：
   - `households_to_tools`（双向，tools_on_hand 有定义）
   - `households_to_ingredient_foods`（双向，ingredient_foods_on_hand 有定义）
3. `HouseholdToRecipe` 实体类 secondary：清理行为未在代码中显式保证。

注意：Household 删除**不直接删除** Group 级的 tags/categories/tools 本身，也不直接删除 recipes。

### 4.7 删除 Group

Group 的级联 relationship（mealie/db/models/group/group.py）：

```python
common_args = {
    "back_populates": "group",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}

# 带 cascade="all, delete-orphan" 的一对多 / 一对一：
preferences / ai_provider_settings / invite_tokens: cascade="all, delete-orphan"
labels / mealplans / webhooks / recipe_actions / cookbooks / server_tasks / data_exports
    / shopping_lists / group_reports / group_event_notifiers
    / ingredient_units / ingredient_foods / tools / tags: 均使用 common_args

# 无 cascade：
households: Mapped[list["Household"]] = orm.relationship("Household", back_populates="group")
users: Mapped[list["User"]] = orm.relationship("User", back_populates="group")
recipes: Mapped[list["RecipeModel"]] = orm.relationship("RecipeModel", back_populates="group")

# 多对多，无 cascade，无 back_populates：
categories: Mapped[list[Category]] = orm.relationship(Category, secondary=group_to_categories, single_parent=True)
```

删除触发链（级联展开）：

1. cascade="all, delete-orphan" 删除 tags：
   - 每个 Tag 通过 `session.delete(tag)` 被级联删除
   - ORM 清理 `recipes_to_tags`（双向 relationship）
   - `cookbooks_to_tags` 和 `plan_rules_to_tags` **不会自动清理**（Tag 未定义反向 relationship）

2. cascade="all, delete-orphan" 删除 tools：
   - 每个 Tool 通过 `session.delete(tool)` 被级联删除
   - ORM 清理 `recipes_to_tools`、`households_to_tools`（双向 relationship）
   - `cookbooks_to_tools` **不会自动清理**

3. Group.categories（多对多，无 cascade）：
   - ORM 清理 `group_to_categories` 关联表中该 group_id 的行（因为 Group 侧定义了 relationship）
   - 但 Category 对象本身**不会**通过此 relationship 被级联删除
   - Category.group_id 是 NOT NULL 外键，删除 Group 时数据库会因外键约束报错；实际应用中需先单独处理 Category

4. cascade="all, delete-orphan" 删除 cookbooks：
   - 每个 CookBook 删除触发 `cookbooks_to_tags`、`cookbooks_to_categories`、`cookbooks_to_tools` 清理
   - （恰好弥补了 Tag/Category/Tool 被级联删除时未清理的 cookbooks_to_* 表，前提是 CookBook 级联删除先于 Tag/Tool 删除执行）

5. cascade="all, delete-orphan" 删除 mealplans（GroupMealPlan）：
   - 不涉及 taxonomy 关联表

6. 其他级联删除：shopping_lists、webhooks、recipe_actions、data_exports、server_tasks、ingredient_units、ingredient_foods、labels、group_reports、group_event_notifiers 等。

注意：`group.households`、`group.users`、`group.recipes` 都没有 cascade，Group 删除时这些对象不会被 ORM 自动删除。由于这些表的外键 group_id 是 NOT NULL，实际中需先单独处理这些对象。

### 4.8 Recipe 更新时空列表的显式处理

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

### 4.9 "空" Taxonomy 检测

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

## 五、Recipe 创建时的 taxonomy 自动创建

mealie/services/recipe/recipe_service.py：

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

## 六、关联表与删除清理行为总结

| 关联表 | Secondary 类型 | 定义 relationship 的对象 | 删除左表对象 | 删除右表对象 | 说明 |
|--------|-------------|------------------------|------------|------------|------|
| recipes_to_tags | sa.Table | Tag（recipes）+ Recipe（tags）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| recipes_to_categories | sa.Table | Category（recipes）+ Recipe（recipe_category）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| recipes_to_tools | sa.Table | Tool（recipes）+ Recipe（tools）双向 | ✅ 清理 | ✅ 清理 | ORM 多对多双向 |
| cookbooks_to_tags | sa.Table | CookBook（tags）单向 | ✅ CookBook 删除时清理 | ❌ Tag 删除时不清理 | 仅 CookBook 侧定义 |
| cookbooks_to_categories | sa.Table | CookBook（categories）单向 | ✅ CookBook 删除时清理 | ❌ Category 删除时不清理 | 仅 CookBook 侧定义 |
| cookbooks_to_tools | sa.Table | CookBook（tools）单向 | ✅ CookBook 删除时清理 | ❌ Tool 删除时不清理 | 仅 CookBook 侧定义 |
| plan_rules_to_tags | sa.Table | GroupMealPlanRules（tags）单向 | ✅ MealPlanRules 删除时清理 | ❌ Tag 删除时不清理 | 仅 MealPlanRules 侧定义 |
| plan_rules_to_categories | sa.Table | GroupMealPlanRules（categories）单向 | ✅ MealPlanRules 删除时清理 | ❌ Category 删除时不清理 | 仅 MealPlanRules 侧定义 |
| households_to_tools | sa.Table | Tool（households_with_tool）+ Household（tools_on_hand）双向 | ✅ Household 删除时清理 | ✅ Tool 删除时清理 | ORM 多对多双向 |
| group_to_categories | sa.Table | Group（categories）单向 | ✅ Group 删除时清理关联表行 | ❌ Category 删除时不清理 | 仅 Group 侧定义，无 cascade |
| plan_rules_to_households | sa.Table | GroupMealPlanRules（households）单向 | ✅ MealPlanRules 删除时清理 | ❌ Household 删除时不清理 | 仅 MealPlanRules 侧定义 |
| users_to_recipes | **实体类** UserToRecipe | Recipe（rated_by/favorited_by）+ User（rated_recipes/favorite_recipes） | ✅ 代码显式 sa.delete() 清理 | - | 实体类 secondary，ORM 不自动处理，需代码显式 |
| households_to_recipes | **实体类** HouseholdToRecipe | Recipe（made_by）+ Household（made_recipes） | ⚠️ 代码未显式处理 | ⚠️ 代码未显式处理 | 实体类 secondary，清理行为未在代码中显式保证 |

**备注**：所有 sa.Table 类型的关联表清理均不依赖数据库级 `ON DELETE CASCADE`，完全由 SQLAlchemy ORM 在 `session.delete()` 时根据对象自身定义的 relationship 处理。实体类类型的关联（UserToRecipe、HouseholdToRecipe）带有业务字段和事件监听，无法靠 ORM 默认行为可靠清理。
