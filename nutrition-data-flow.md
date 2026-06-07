# Mealie Nutrition 数据流转分析

## 一、总体架构概览

Nutrition（营养）数据在 Mealie 中采用 **"单一存储、每份为基准、前端按需缩放、缺失字段静默过滤"** 的设计理念。整个数据流分为五层：

```
┌──────────────────────────────────────────────────────────────────┐
│  1. 数据录入层  (前端表单 / URL 抓取 / OpenAI 图像识别)            │
├──────────────────────────────────────────────────────────────────┤
│  2. API 传输层  (Pydantic Schema ↔ TypeScript Interface)         │
├──────────────────────────────────────────────────────────────────┤
│  3. 数据持久层  (SQLAlchemy ORM → PostgreSQL/SQLite)              │
├──────────────────────────────────────────────────────────────────┤
│  4. 业务逻辑层  (RecipeService / Repository / Cleaner)            │
├──────────────────────────────────────────────────────────────────┤
│  5. 前端展示层  (Vue 组件 + Composables + 缩放计算)               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 二、数据模型层：字段定义与「每份 vs 整份」的设计

### 2.1 数据库模型

**文件**：[nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/db/models/recipe/nutrition.py)

数据库表名为 `recipe_nutrition`，与 `recipes` 表为 **一对一关系**（`uselist=False`）。所有营养字段采用 `String` 类型而非数值类型，支持存储带单位或不规则格式的值。

```python
class Nutrition(SqlAlchemyBase):
    __tablename__ = "recipe_nutrition"
    id: int
    recipe_id: GUID  # FK → recipes.id

    # 11 个营养字段，全部为 String | None
    calories: str | None                    # 卡路里
    carbohydrate_content: str | None        # 碳水化合物
    cholesterol_content: str | None         # 胆固醇
    fat_content: str | None                 # 脂肪
    fiber_content: str | None               # 膳食纤维
    protein_content: str | None             # 蛋白质
    saturated_fat_content: str | None       # 饱和脂肪
    sodium_content: str | None              # 钠
    sugar_content: str | None               # 糖
    trans_fat_content: str | None           # 反式脂肪
    unsaturated_fat_content: str | None     # 不饱和脂肪
```

**关键设计决策 —— 「每份」的基准**：

数据库模型中有一段被注释掉的 `serving_size` 字段（第 21-29 行），注释清晰地说明了设计哲学：
- Nutrition 数据默认为 **"每份（per-serving）"** 的数值
- 「份」的基准由 Recipe 的 `recipe_servings`（份数）字段决定
- 未实现 `serving_size`（每份的体积/质量，如 "500 g"），因为前端单位换算复杂，且易与 `servings`（份数）概念混淆

**Recipe 与 Nutrition 的关联**：[recipe.py#L97](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/db/models/recipe/recipe.py#L97)

```python
nutrition: Mapped[Nutrition] = orm.relationship(
    "Nutrition", uselist=False, cascade="all, delete-orphan"
)
```

在 `RecipeModel.__init__` 中（[recipe.py#L205](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/db/models/recipe/recipe.py#L205)），Nutrition 对象总是会被创建：

```python
self.nutrition = Nutrition(**(nutrition or {}))
```

这意味着即使没有营养数据，也会存在一个所有字段为 `None` 的 Nutrition 行，保证一对一关系完整性。

### 2.2 Pydantic Schema（后端 DTO）

**文件**：[recipe_nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe_nutrition.py)

```python
class Nutrition(MealieModel):
    calories: str | None = None
    carbohydrate_content: str | None = None
    # ... 其余 9 个字段

    model_config = ConfigDict(
        from_attributes=True,
        coerce_numbers_to_str=True,   # 自动将数字转为字符串
        alias_generator=to_camel,     # snake_case → camelCase
    )
```

关键点：
- `coerce_numbers_to_str=True`：允许传入数字，自动转换为字符串存储
- `alias_generator=to_camel`：JSON 序列化时字段名为 camelCase（如 `carbohydrateContent`），与前端 TypeScript 接口对齐

在 Recipe Schema 中（[recipe.py#L295-L297](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe.py#L295-L297)），nutrition 字段有一个特殊校验器：

```python
@field_validator("nutrition", mode="before")
def validate_nutrition(cls, v):
    return v or None
```

如果传入空对象 `{}`，会被转换为 `None`。但在数据库层仍会创建空 Nutrition 行（由于 `RecipeModel.__init__` 的逻辑），这是 **Schema 层与 ORM 层的细微差异**。

### 2.3 TypeScript 类型（前端 DTO）

**文件**：[recipe.ts#L199-L211](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/lib/api/types/recipe.ts#L199-L211)

```typescript
export interface Nutrition {
  calories?: string | null;
  carbohydrateContent?: string | null;
  cholesterolContent?: string | null;
  fatContent?: string | null;
  fiberContent?: string | null;
  proteinContent?: string | null;
  saturatedFatContent?: string | null;
  sodiumContent?: string | null;
  sugarContent?: string | null;
  transFatContent?: string | null;
  unsaturatedFatContent?: string | null;
}
```

该文件由 `pydantic2ts` 自动生成，与后端 Pydantic 模型完全对齐。

---

## 三、数据录入流程

### 3.1 录入路径总览

Nutrition 数据有三条录入路径：

| 路径 | 触发场景 | 处理模块 |
|------|----------|----------|
| **路径 A** | 用户在前端手动编辑营养值 | `RecipeNutrition.vue` → `PUT /api/recipes/{slug}` |
| **路径 B** | 通过 URL 抓取食谱（recipe_scrapers） | `cleaner.clean_nutrition()` → `create_from_html()` |
| **路径 C** | 通过图像 / OpenAI 识别 | `OpenAIRecipeService`（目前不提取营养）→ 走路径 A |

### 3.2 路径 A：前端手动录入

**编辑组件**：[RecipeNutrition.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeNutrition.vue)

组件结构：
- 通过 `defineModel<Nutrition>()` 双向绑定到父组件的 `recipe.nutrition`
- 使用 `v-number-input` 为每个营养字段渲染一个数字输入框
- 输入值通过 `updateValue()` 直接写入响应式对象：
  ```typescript
  function updateValue(key: number | string, event: Event) {
    modelValue.value = { ...modelValue.value, [key]: event };
  }
  ```

**字段标签映射**：[use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts)

每个字段都配置了 i18n 标签和单位后缀：

| 字段 Key | 默认单位 |
|----------|----------|
| calories | kcal |
| cholesterolContent / sodiumContent | mg |
| 其余所有字段 | g |

**保存流程**（RecipePage 主组件）：[RecipePage.vue#L363-L378](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L363-L378)

```typescript
async function saveRecipe() {
  const { data, error } = await api.recipes.updateOne(recipe.value.slug, recipe.value);
  // ...
  recipe.value = data;  // 用服务端返回的数据覆盖本地状态
}
```

前端 API 客户端：[recipe.ts#L93-L95](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/lib/api/user/recipes/recipe.ts#L93-L95)，通过继承 `BaseCRUDAPI` 的 `updateOne` 方法发送 `PUT /api/recipes/{slug}`。

### 3.3 路径 B：URL 抓取 / 数据清洗

**抓取策略**：[scraper_strategies.py#L274](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/scraper/scraper_strategies.py)

```python
nutrition=try_get_default(scraped_data.nutrients, "nutrition", None, cleaner.clean_nutrition)
```

**核心清洗函数**：[cleaner.py#L562-L595](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/scraper/cleaner.py#L562-L595)

`clean_nutrition()` 做了三件事：

1. **提取数字**：用正则 `MATCH_DIGITS = re.compile(r"\d+([.,]\d+)?")` 从带单位的字符串（如 "25 g"）中提取纯数字，支持逗号小数（欧洲格式）
2. **单位换算**：对 `sodiumContent` 和 `cholesterolContent`，如果原始值以 "g" 结尾且不含 "m"（即不是 mg），自动乘以 1000 转换为 mg
3. **卡路里直传**：`calories` 字段如果是 int/float，直接转字符串

### 3.4 后端接收与持久化

**API 路由层**：[recipe_crud_routes.py#L472-L493](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/routes/recipe/recipe_crud_routes.py#L472-L493)

```python
@router.put("/{slug}")
def update_one(self, slug: str, data: Recipe):
    recipe = self.service.update_one(slug, data)
    return recipe
```

**Service 层**：[recipe_service.py#L520-L528](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/recipe/recipe_service.py#L520-L528)

```python
def update_one(self, slug_or_id: str | UUID, update_data: Recipe) -> Recipe:
    recipe = self._pre_update_check(slug_or_id, update_data)
    update_data = self._remove_non_existent_ingredient_references(update_data)
    update_data = self._resolve_ingredient_sub_recipes(update_data)
    new_data = self.group_recipes.update(recipe.slug, update_data)
    return new_data
```

**Repository 层**：[repository_recipes.py#L204-L218](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/repos/repository_recipes.py#L204-L218)

```python
def update(self, match_value, new_data: dict | Recipe) -> Recipe:
    new_data = new_data if isinstance(new_data, dict) else new_data.model_dump()
    entry = self._query_one(match_value=match_value)
    entry.update(session=self.session, **new_data)
    self.session.commit()
    return self.schema.model_validate(entry)
```

`entry.update()` 是 SQLAlchemy 模型的通用更新方法，会遍历 new_data 的键值对，设置到 ORM 对象上。对于 nutrition 字段，Pydantic `model_dump()` 会输出 camelCase 键名（如 `carbohydrateContent`），由 ORM 的别名机制或字段映射处理。

---

## 四、缩放同步机制

### 4.1 缩放基准

**缩放只作用于食材数量，不作用于 Nutrition 数值本身。**

Nutrition 数据在数据库中始终是 **"每份"** 的值。缩放由前端根据 `recipe_servings`（原始份数）动态计算。

相关字段：
- `recipe.recipeServings`：食谱设计的原始份数（如 4）
- `recipe.recipeYieldQuantity`：产量数值（如 2，配合 `recipeYield` 文本如 "pies"）

### 4.2 前端缩放计算

**缩放因子定义**：[RecipePageScale.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue) + [RecipeScaleEditButton.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue)

```typescript
// RecipePage.vue 中定义的全局缩放因子
const scale = ref(1);

// RecipeScaleEditButton.vue 中的计算逻辑
function recalculateScale(newYield: number) {
  if (props.recipeServings <= 0) {
    scale.value = 1;
  } else {
    scale.value = newYield / props.recipeServings;  // 核心公式
  }
}
```

例如：原始份数为 4，用户将份数改为 6，则 scale = 6 / 4 = 1.5。

**缩放只被应用于食材数量**：[use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/composables/recipes/use-scaled-amount.ts)

```typescript
export function useScaledAmount(amount: number, scale = 1) {
  const scaledAmount = Number(((amount || 0) * scale).toFixed(3));
  const scaledAmountDisplay = scaledAmount ? formatQuantity(scaledAmount) : "";
  return { scaledAmount, scaledAmountDisplay };
}
```

### 4.3 Nutrition 缩放的缺失——重要发现

**Nutrition 数据在前端展示时 **不会** 根据 scale 因子进行缩放。**

证据：
1. [RecipeNutrition.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeNutrition.vue) 的 `renderedList` 计算属性直接使用 `modelValue.value[key]`，未乘 scale
2. [RecipePrintView.vue#L162-L184](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue#L162-L184) 的打印视图遍历 `recipe.nutrition`，也未乘 scale
3. [RecipePageOrganizers.vue#L72-L77](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageOrganizers.vue#L72-L77) 将 `RecipeNutrition` 组件嵌入，但未传递 scale prop

**设计含义**：
- Nutrition 始终以「每份」展示，与当前用户选择的份数无关
- 用户如果改了份数（如从 4 人份改为 2 人份），Nutrition 卡片显示的仍是原来 4 人份中「1 份」的数值
- 如果用户修改了 `recipeServings` 本身（而非临时缩放），保存后 Nutrition 数值会被当作新的「每份」值

### 4.4 临时缩放 vs 持久化修改的边界

| 操作 | 作用域 | 对 Nutrition 的影响 |
|------|--------|---------------------|
| 通过 RecipeScaleEditButton 调整份数 | 前端内存临时 scale | 无影响（Nutrition 仍按原每份展示） |
| 在 RecipePageInfoEditor 中修改 `recipeServings` 字段 | 会保存到数据库 | Nutrition 数值被视为新份数下的每份值（数据库层面不做换算） |

---

## 五、缺失字段的处理

### 5.1 数据库层面

所有 11 个 Nutrition 字段均为可空（`String | None`），允许部分或全部字段为 `None`。即使 nutrition 完全为空，也会存在一条 `recipe_nutrition` 记录（所有字段为 NULL），由 `RecipeModel.__init__` 保证。

### 5.2 Schema 校验层

[recipe.py#L295-L297](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe.py#L295-L297) 的校验器将空对象转为 None：
```python
@field_validator("nutrition", mode="before")
def validate_nutrition(cls, v):
    return v or None
```

### 5.3 前端展示层（核心过滤逻辑）

**RecipeNutrition.vue 中的三级过滤**：

1. **组件级隐藏**：当所有字段都为 null 且不是编辑模式时，整个卡片不显示
   ```typescript
   const valueNotNull = computed(() => {
     let key: keyof Nutrition;
     for (key in modelValue.value) {
       if (modelValue.value[key] !== null) {
         return true;
       }
     }
     return false;
   });
   // 模板中 v-if="valueNotNull || edit"
   ```

2. **展示模式过滤**：仅渲染非空值的字段
   ```typescript
   const renderedList = computed(() => {
     return Object.entries(labels).reduce((item, [key, label]) => {
       if (modelValue.value[key]?.trim()) {  // 非空且 trim 后有内容
         item[key] = { ...label, value: modelValue.value[key] };
       }
       return item;
     }, {});
   });
   ```

3. **编辑模式**：即使所有字段为空，也会渲染全部 11 个输入框供用户填写

**RecipePrintView.vue 中的过滤**：
```html
<template v-if="value">
  <td>{{ labels[key].label }}</td>
  <td>{{ value ? (labels[key].suffix ? `${value} ${labels[key].suffix}` : value) : '-' }}</td>
</template>
```
用 `v-if="value"` 跳过空值行，打印时只输出有数据的营养项。

### 5.4 显示开关（showNutrition）

Nutrition 卡片是否可见还受 `recipe.settings.showNutrition` 控制：

**RecipeSettings 模型**：[recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe_settings.py)

```python
class RecipeSettings(MealieModel):
    public: bool = False
    show_nutrition: bool = False   # 默认不显示
    # ...
```

在 [RecipePageOrganizers.vue#L72](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageOrganizers.vue#L72)：

```html
<RecipeNutrition v-if="recipe.settings.showNutrition" v-model="recipe.nutrition" ... />
```

开关本身由 [RecipeSettingsSwitches.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeSettingsSwitches.vue) 控制，对应 i18n 标签为 `recipe.show-nutrition-values`。

默认值来源：创建食谱时，如果未显式设置 settings，会从 Household Preferences 继承（[recipe_service.py#L208-L218](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/recipe/recipe_service.py#L208-L218)）：
```python
data.settings = RecipeSettings(
    show_nutrition=self.household.preferences.recipe_show_nutrition,
    # ...
)
```

---

## 六、Recipe 数据读取与前端展示链路

### 6.1 单食谱读取（详情页）

```
用户访问 /g/{group}/r/{slug}
    │
    ▼
useRecipe(slug)  [use-recipe.ts]
    │  fetchRecipe()
    ▼
api.recipes.getOne(slug)  [recipe.ts]
    │  GET /api/recipes/{slug}
    ▼
RecipeController.get_one()  [recipe_crud_routes.py#L415-L424]
    │
    ▼
RecipeService.get_one()  [recipe_service.py#L189-L200]
    │
    ▼
RepositoryRecipes.get_one()
    │  (使用 joinedload(RecipeModel.nutrition) 做 Eager Load)
    ▼
Pydantic Recipe.model_validate(db_model)
    │  (snake_case → camelCase via alias_generator)
    ▼
JSON Response
    │
    ▼
前端 RecipePage.vue
    ├─ RecipePageOrganizers.vue  →  RecipeNutrition.vue（根据 showNutrition 显示）
    ├─ RecipePrintView.vue（打印视图中的 Nutrition table）
    └─ scale = ref(1)（缩放因子，仅用于食材）
```

Eager Load 配置在 [recipe.py#L316](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe.py#L316)：
```python
@classmethod
def loader_options(cls) -> list[LoaderOption]:
    return [
        # ...
        joinedload(RecipeModel.nutrition),
        # ...
    ]
```

### 6.2 食谱列表（分页）

分页接口 `GET /api/recipes` 返回的是 `RecipeSummary`，该类型 **不包含 nutrition 字段**。列表页不显示营养信息，避免 N+1 查询问题。只有进入详情页后才加载 Nutrition。

### 6.3 Meal Plan 中的使用

Meal Plan 类型（[meal-plan.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/lib/api/types/meal-plan.ts)）中的 `ReadPlanEntry.recipe` 也是 `RecipeSummary` 类型，不含 Nutrition。后端 meal plan 控制器中也没有 nutrition 相关处理代码。即 **Meal Plan 模块不做营养汇总统计**。

---

## 七、关键文件索引

| 层级 | 文件 | 作用 |
|------|------|------|
| **DB Model** | [nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/db/models/recipe/nutrition.py) | Nutrition 数据库表定义 |
| **DB Model** | [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/db/models/recipe/recipe.py) | Recipe 与 Nutrition 的一对一关联 |
| **Schema** | [recipe_nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe_nutrition.py) | Pydantic Nutrition DTO |
| **Schema** | [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe.py) | Recipe Schema，含 nutrition 校验器与 loader_options |
| **Schema** | [recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/schema/recipe/recipe_settings.py) | showNutrition 等设置 |
| **Service** | [recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/recipe/recipe_service.py) | CRUD 业务逻辑 |
| **Scraper** | [cleaner.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/services/scraper/cleaner.py) | clean_nutrition() 数据清洗 |
| **Repository** | [repository_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/repos/repository_recipes.py) | 数据库持久化操作 |
| **API Route** | [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/mealie/routes/recipe/recipe_crud_routes.py) | REST API 端点 |
| **前端类型** | [recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/lib/api/types/recipe.ts) | TypeScript Nutrition 接口 |
| **前端 API** | [recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/lib/api/user/recipes/recipe.ts) | RecipeAPI 客户端 |
| **前端 Composable** | [use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts) | 营养字段标签与单位映射 |
| **前端 Composable** | [use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/composables/recipes/use-scaled-amount.ts) | 缩放计算（仅用于食材） |
| **前端 Composable** | [use-recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/composables/recipes/use-recipe.ts) | 食谱数据加载 |
| **前端组件** | [RecipeNutrition.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeNutrition.vue) | 营养编辑/展示卡片 |
| **前端组件** | [RecipePageScale.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue) | 缩放 UI 容器 |
| **前端组件** | [RecipeScaleEditButton.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue) | 缩放计算逻辑 |
| **前端组件** | [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) | 食谱详情页主容器 |
| **前端组件** | [RecipePageOrganizers.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageOrganizers.vue) | Nutrition 卡片嵌入点 |
| **前端组件** | [RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue) | 打印视图中的 Nutrition 表格 |
| **前端组件** | [RecipeSettingsSwitches.vue](file:///d:/fz/0601/solo-dogfeeding/code/75-mealie/frontend/app/components/Domain/Recipe/RecipeSettingsSwitches.vue) | showNutrition 开关 |

---

## 八、总结与注意事项

### 核心设计结论

1. **Nutrition 是 Recipe 的一对一从属实体**，永远跟随 Recipe 生命周期，无独立 CRUD
2. **存储基准为「每份」**，所有数值都是单份含量，份数由 `recipeServings` 定义
3. **前端缩放不影响 Nutrition**，缩放因子只对食材数量生效，Nutrition 展示不做乘法
4. **缺失字段采用静默过滤策略**：空值不展示、不报错、不占位
5. **所有字段为 String 类型**，由 Pydantic 的 `coerce_numbers_to_str` 做兼容，支持存储任意格式
6. **可见性受双重控制**：`showNutrition` 开关 + 数据非空判断，两者同时满足才渲染
7. **列表页不加载 Nutrition**，仅详情页通过 `joinedload` 一次性加载，避免性能问题

### 潜在风险 / 可改进点

- **Nutrition 不跟随缩放同步**：用户调整份数后，Nutrition 仍显示原每份值，可能与用户直觉不符（用户可能期望看到调整后总份数对应的总营养）
- **无数据一致性校验**：字符串存储无法保证数值有效性，可能出现 "abc" 这样的无效值
- **无单位标准化**：存储前仅在抓取时对钠/胆固醇做 g→mg 换算，手动录入时完全信任用户输入
- **Meal Plan 无营养汇总**：作为饮食管理应用，meal plan 模块未做每日/每周营养摄入统计
