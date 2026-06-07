# Mealie 标签、分类、工具与 Recipe 关联分析

## 一、作用范围差异

### 1.1 总体层级：均为 Group 级别，但有 Household 扩展

| 维度 | 标签 (Tag) | 分类 (Category) | 工具 (Tool) |
|------|-----------|-----------------|-------------|
| 所属级别 | Group 级别 | Group 级别 | Group 级别 |
| 是否有 Group 唯一约束 | 是 (`tags_slug_group_id_key`) | 是 (`category_slug_group_id_key`) | 是 (`tools_slug_group_id_key`) |
| 是否扩展到 Household | 否 | 否 | 是 (`households_to_tools`) |
| Repository 类型 | `RepositoryTags` (Group 级) | `RepositoryCategories` (Group 级) | `GroupRepositoryGeneric` (Group 级) |

核心模型文件：
- [tag.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tag.py)
- [category.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/category.py)
- [tool.py](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tool.py)

### 1.2 各 taxonomy 的关联对象范围对比

#### 标签 (Tag) 的关联范围
定义在 [tag.py#L19-L41](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tag.py#L19-L41)：

1. **Recipe 关联**: `recipes_to_tags` 关联表
2. **Meal Plan Rules 关联**: `plan_rules_to_tags` 关联表
3. **CookBook 关联**: `cookbooks_to_tags` 关联表
4. **Group 级联拥有**: [group.py#L92](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L92)，`cascade="all, delete-orphan"`

#### 分类 (Category) 的关联范围
定义在 [category.py#L19-L49](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/category.py#L19-L49)：

1. **Group 直接关联**: `group_to_categories` 关联表（与 Tag/Tool 的不同点）
2. **Meal Plan Rules 关联**: `plan_rules_to_categories` 关联表
3. **Recipe 关联**: `recipes_to_categories` 关联表
4. **CookBook 关联**: `cookbooks_to_categories` 关联表
5. **Group 级联拥有**: [group.py#L40](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L40)

#### 工具 (Tool) 的关联范围
定义在 [tool.py#L17-L39](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/tool.py#L17-L39)：

1. **Household 手边工具关联**: `households_to_tools` 关联表（独有特性）
2. **Recipe 关联**: `recipes_to_tools` 关联表
3. **CookBook 关联**: `cookbooks_to_tools` 关联表
4. **Group 级联拥有**: [group.py#L91](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L91)，`cascade="all, delete-orphan"`

**关键差异总结：**
- 分类多一个 `group_to_categories` 直接关联表
- 工具多一个 `households_to_tools` Household 级关联（用于 "手边有什么工具" 的功能）
- 标签和分类支持 Meal Plan Rules 关联，工具不支持
- 三者均支持 Recipe 和 CookBook 关联

### 1.3 Recipe 模型中的关联定义
在 [recipe.py#L98-L102](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/recipe.py#L98-L102) 和 [recipe.py#L138](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/recipe/recipe.py#L138)：

```python
recipe_category: Mapped[list["Category"]] = orm.relationship(
    "Category", secondary=recipes_to_categories, back_populates="recipes"
)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", secondary=recipes_to_tools, back_populates="recipes")
tags: Mapped[list["Tag"]] = orm.relationship("Tag", secondary=recipes_to_tags, back_populates="recipes")
```

**注意**：Recipe 到这三者的 relationship **都没有配置 `cascade`**，依赖 SQLAlchemy 对多对多关联表的默认处理。

---

## 二、筛选参数的来源

筛选参数主要通过三层来源传入，最终汇聚到 `RepositoryRecipes.page_all()` 方法执行查询。

### 2.1 第一层：API 路由 Query 参数

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

**直接 Query 参数**：
- `categories`：分类 ID 或 slug 列表（支持 UUID 和字符串 slug 混合）
- `tags`：标签 ID 或 slug 列表
- `tools`：工具 ID 或 slug 列表
- `foods`：食材 ID 或 slug 列表
- `households`：家庭 ID 或 slug 列表

### 2.2 第二层：RecipeSearchQuery 对象
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

**筛选语义参数**：
- `require_all_*`: 控制是 "AND" 逻辑（需要同时满足所有）还是 "OR" 逻辑（满足任一即可）
- `cookbook`: 通过食谱集筛选，会优先使用食谱集的 `query_filter_string`
- `search`: 全文搜索关键词

### 2.3 第三层：PaginationQuery 的 query_filter
定义在 [pagination.py#L32-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/response/pagination.py#L32-L48)：

```python
class PaginationQuery(RequestQuery):
    page: int = 1
    per_page: int = 50
    # 继承自 RequestQuery:
    # order_by, order_direction, query_filter, pagination_seed
```

`query_filter` 是一个字符串表达式，通过 `QueryFilterBuilder` 解析，支持更复杂的过滤逻辑（如比较运算、嵌套属性过滤等）。

### 2.4 CookBook 中的筛选配置
在 [cookbook.py#L36-L48](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/household/cookbook.py#L36-L48)：

```python
query_filter_string: Mapped[str]  # 新版：自定义查询字符串
# 旧版（已弃用）：
categories: Mapped[list[Category]]  # 通过 cookbooks_to_categories 关联
require_all_categories: FilterableColumn[bool]
tags: Mapped[list[Tag]]
require_all_tags: FilterableColumn[bool]
tools: Mapped[list[Tool]]
require_all_tools: FilterableColumn[bool]
```

**优先级**：当提供 `cookbook` 参数时，优先使用 `cookbook.query_filter_string` 与 `PaginationQuery.query_filter` 合并，忽略直接传入的 `categories/tags/tools` 参数。

相关逻辑在 [repository_recipes.py#L243-L249](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L243-L249)：
```python
if cookbook:
    if pagination_result.query_filter and cookbook.query_filter_string:
        pagination_result.query_filter = (
            f"({pagination_result.query_filter}) AND ({cookbook.query_filter_string})"
        )
    else:
        pagination_result.query_filter = cookbook.query_filter_string
```

### 2.5 参数 ID/Slug 自动解析
在 [repository_recipes.py#L183-L202](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L183-L202)，`_uuids_for_items` 方法统一处理：
- 若输入为 UUID 格式，直接使用
- 若输入为字符串 slug，自动查表转换为对应 ID

### 2.6 筛选逻辑执行
核心筛选构建在 [repository_recipes.py#L295-L337](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L295-L337) 的 `_build_recipe_filter` 方法：

- `require_all_* = True` 时：使用多个 `Model.relation.any(id == x)` 组合（AND 逻辑）
- `require_all_* = False` 时：使用单个 `Model.relation.any(id.in_(list))`（OR 逻辑）
- 自动附加 `group_id` 和 `household_id` 过滤（取决于 Repository 的作用域）

---

## 三、删除历史数据对关联的影响

### 3.1 删除 Tag / Category / Tool 本身

#### API 文档声明的行为
- 标签删除注释 [controller_tags.py#L97-L101](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_tags.py#L97-L101)：
  > "Removes a recipe tag from the database. Deleting a tag does not impact a recipe. The tag will be removed from any recipes that contain it"
- 分类删除注释 [controller_categories.py#L108-L112](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/routes/organizers/controller_categories.py#L108-L112)：
  > 同上类似描述

#### 底层实现机制
删除操作在 [repository_generic.py#L256-L269](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_generic.py#L256-L269)：

```python
def delete(self, value, match_key: str | None = None) -> Schema:
    result = self._query_one(value, match_key)
    self.session.delete(result)  # SQLAlchemy ORM 删除
    self.session.commit()
```

**关联表自动清理原理**：
SQLAlchemy 的多对多 `relationship` 在删除任一侧对象时，会**自动清理关联表中的对应行**（这是 ORM 级别的行为，不依赖数据库 `ON DELETE CASCADE`）。

因此删除一个 Tag/Category/Tool 时：
- ✅ `recipes_to_*` 关联表中对应的行被自动删除
- ✅ `cookbooks_to_*` 关联表中对应的行被自动删除
- ✅ 工具特有的 `households_to_tools` 关联表中对应的行被自动删除
- ✅ 标签/分类特有的 `plan_rules_to_*` 关联表中对应的行被自动删除
- ✅ Recipe 对象本身不受影响（数据仍完整，只是少了该标签/分类/工具关联）

### 3.2 删除 Recipe

#### 显式清理步骤
在 [repository_recipes.py#L110-L130](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L110-L130)：

1. 先删除 `UserToRecipe` 关联记录（避免级联问题）
2. 再 `session.delete(recipe)` 删除 Recipe 本身

#### 关联表清理
Recipe 删除后，SQLAlchemy ORM 自动清理：
- `recipes_to_tags` 中对应 recipe_id 的行
- `recipes_to_categories` 中对应 recipe_id 的行
- `recipes_to_tools` 中对应 recipe_id 的行

**注意**：PostgreSQL 批量删除时存在级联异常问题，代码通过逐条删除 Recipe 而非批量 DELETE 语句绕过。见 [repository_recipes.py#L143](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/repos/repository_recipes.py#L143)：
```python
# we don't delete the whole query in one statement because postgres doesn't cascade correctly
```

### 3.3 删除 Group 时的级联
Group 对 Tag、Tool、Category 的 relationship 配置了 `cascade="all, delete-orphan"`（见 [group.py#L66-L92](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/db/models/group/group.py#L66-L92)）：

```python
common_args = {
    "back_populates": "group",
    "cascade": "all, delete-orphan",
    "single_parent": True,
}
tags: Mapped[list["Tag"]] = orm.relationship("Tag", **common_args)
tools: Mapped[list["Tool"]] = orm.relationship("Tool", **common_args)
```

因此删除一个 Group 时，其下所有 Tag/Category/Tool 会被级联删除，进而触发 3.1 节所述的关联表清理。

### 3.4 "空" Taxonomy 的检测
代码提供了检测无 Recipe 关联的 Tag/Category 的功能（Tool 暂无此 API）：

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

### 3.5 Recipe 更新时空列表的显式处理
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

当更新 Recipe 时传入空列表 `tags=[]` 或 `recipe_category=[]` 或 `tools=[]`，代码会显式将关联清空，确保关联表中的对应记录被删除。

---

## 四、Schema 数据结构参考

### 4.1 Pydantic Schema 继承关系

```
RecipeTag (基础)
├── RecipeCategory (直接继承 RecipeTag)
└── RecipeTool (扩展 households_with_tool 字段)
```

定义在 [recipe.py#L61-L99](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe.py#L61-L99)。

### 4.2 创建/保存 Schema

| 类型 | 输入 Schema | 保存 Schema (注入 group_id) | 文件 |
|------|------------|---------------------------|------|
| Tag | `TagIn` (name) | `TagSave` (name + group_id) | [recipe_category.py#L35-L40](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_category.py#L35-L40) |
| Category | `CategoryIn` (name) | `CategorySave` (name + group_id) | [recipe_category.py#L9-L15](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_category.py#L9-L15) |
| Tool | `RecipeToolCreate` (name + households_with_tool) | `RecipeToolSave` (+ group_id) | [recipe_tool.py#L9-L16](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/schema/recipe/recipe_tool.py#L9-L16) |

### 4.3 Recipe 创建/更新时的 taxonomy 自动转换
在 [recipe_service.py#L255-L291](file:///d:/fz/0601/solo-dogfeeding/code/73-mealie/mealie/services/recipe/recipe_service.py#L255-L291) 的 `_transform_category_or_tag`：

当 Recipe 数据中传入的 Tag/Category 通过 slug 无法在数据库中找到时，会自动创建对应的 Tag/Category（Tool 无此自动创建逻辑）。
