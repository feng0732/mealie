# Recipe Scaling 与 Serving 显示规则深度解析

本文档系统梳理 Mealie 中 Recipe 的数量缩放（scaling）、份量（servings/yield）、原料显示、营养值、单位换算、小数/分数格式化以及打印视图等各环节的联动机制。

---

## 目录

1. [核心数据模型与字段](#1-核心数据模型与字段)
2. [缩放比例（Scale）的计算与传播](#2-缩放比例scale的计算与传播)
3. [数量格式化：小数 vs 分数](#3-数量格式化小数-vs-分数)
4. [原料（Ingredient）的显示合成](#4-原料ingredient的显示合成)
5. [单位换算与合并](#5-单位换算与合并)
6. [营养值（Nutrition）的处理](#6-营养值nutrition的处理)
7. [复数形式与本地化策略](#7-复数形式与本地化策略)
8. [打印视图的影响](#8-打印视图的影响)
9. [关键文件速查表](#9-关键文件速查表)

---

## 1. 核心数据模型与字段

### 1.1 Recipe 级别字段

后端定义于 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe.py#L116-L167)，前端类型在 [recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/lib/api/types/recipe.ts#L228-L264)。

| 字段 | 类型 | 说明 |
|------|------|------|
| `recipe_servings` (前端 `recipeServings`) | `float` | 配方原始份数（人数），是缩放计算的基准 |
| `recipe_yield_quantity` (前端 `recipeYieldQuantity`) | `float` | 产出数量（如 "2 个蛋糕" 中的 2） |
| `recipe_yield` (前端 `recipeYield`) | `str \| None` | 产出单位文本（如 "蛋糕"、"份"） |

**校验/清洗规则**（[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe.py#L151-L162)）：
- 数字字段（`recipe_servings`、`recipe_yield_quantity`）：`None` 或空字符串转为 `0`
- 文本字段（`recipe_yield` 等）：数字类型会被强制转成字符串

### 1.2 Ingredient 级别字段

定义于 [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L191-L358)。

| 字段 | 类型 | 说明 |
|------|------|------|
| `quantity` | `float \| None` | 原始数量，存储精度为 3 位小数 |
| `unit` | `IngredientUnit \| None` | 单位对象，含 `fraction`、`name`、`pluralName`、`abbreviation`、`standard_unit`、`standard_quantity` 等 |
| `food` | `IngredientFood \| None` | 食材对象，含 `name`、`pluralName` |
| `note` | `str \| None` | 备注文本 |
| `display` | `str` | 后端自动计算的显示字符串（可被覆盖） |
| `original_text` | `str \| None` | 解析前的原始文本 |
| `title` | `str \| None` | 原料分组标题 |

**Unit（单位）核心属性**（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L148-L188)）：
- `fraction: bool` — 决定该单位的数量用分数还是小数显示
- `standard_unit: str \| None` — 标准化单位名（如 "gram"、"milliliter"）
- `standard_quantity: float \| None` — 对应标准化单位的换算系数
- `use_abbreviation: bool` — 是否使用缩写
- `abbreviation` / `plural_abbreviation` — 缩写（单数/复数）
- `name` / `plural_name` — 全名（单数/复数）

---

## 2. 缩放比例（Scale）的计算与传播

### 2.1 Scale 的本质

`scale` 是一个简单的浮点乘数，默认值为 `1`，**完全由前端状态管理**，不写入数据库。

定义位置：[RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L449)

```typescript
const scale = ref(1);
```

### 2.2 Scale 的计算

用户通过 [RecipeScaleEditButton.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue#L143-L154) 调整目标份数：

```typescript
function recalculateScale(newYield: number) {
  if (isNaN(newYield) || newYield <= 0) return;
  if (props.recipeServings <= 0) {
    scale.value = 1;
  } else {
    scale.value = newYield / props.recipeServings;  // 核心公式
  }
}
```

基准 servings 的取值优先级（[RecipePageScale.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue#L24-L27)）：
1. `recipe.recipeServings`
2. 若为 0，回退到 `recipe.recipeYieldQuantity`
3. 若仍为 0，回退到 `1`

### 2.3 Scale 的传播路径

Scale 作为 prop 从 RecipePage 向下传递到所有需要显示数量的子组件：

```
RecipePage.vue
  ├─ scale = ref(1)
  ├─ RecipePageScale.vue  ← v-model:scale（读写）
  ├─ RecipePageIngredientToolsView.vue  ← :scale
  │   └─ RecipeIngredients.vue  ← :scale
  │       └─ RecipeIngredientListItem.vue  ← :scale
  │           └─ useIngredientTextParser() 使用 scale
  ├─ RecipePageInstructions.vue  ← :scale
  │   └─ RecipeIngredientHtml.vue  ← :scale
  └─ RecipePrintContainer.vue  ← :scale
      └─ RecipePrintView.vue  ← :scale
          └─ useIngredientTextParser() / useScaledAmount() 使用 scale
```

### 2.4 数量缩放计算

#### 前端通用缩放：useScaledAmount

位于 [use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.ts#L24-L32)

```typescript
export function useScaledAmount(amount: number, scale = 1) {
  const scaledAmount = Number(((amount || 0) * scale).toFixed(3));
  const scaledAmountDisplay = scaledAmount ? formatQuantity(scaledAmount) : "";
  return { scaledAmount, scaledAmountDisplay };
}
```

**关键细节**：
- 结果被 `toFixed(3)` 截断到 3 位小数，再转回 `Number`
- 若结果为 0，`scaledAmountDisplay` 返回空字符串（不显示 "0"）

#### 原料数量缩放

位于 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L95-L124)

```typescript
if (quantity && Number(quantity) !== 0) {
  const scaledQuantity = Number((quantity * scale));
  // 根据 unit.fraction 决定走小数分支还是分数分支
  if (unit && !unit.fraction) {
    // 小数模式（见 3.1）
  } else {
    // 分数模式（见 3.2）
  }
}
```

**注意**：原料缩放不先 `toFixed(3)`，而是直接用 `quantity * scale` 的结果送入格式化函数。这与 `useScaledAmount` 略有差异。

---

## 3. 数量格式化：小数 vs 分数

数量格式化在后端和前端各有一套实现，逻辑保持一致。是否使用分数取决于单位的 `fraction` 属性。

### 3.1 常量

| 常量 | 值 | 位置 |
|------|-----|------|
| `INGREDIENT_QTY_PRECISION` | `3` | 后端 [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L23) |
| `MAX_INGREDIENT_DENOMINATOR` | `32` | 后端 [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L24) |
| `DECIMAL_PRECISION` | `3` | 前端 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L9) |
| `FRAC_MIN_DENOM` | `10` | 前端 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L8) |

### 3.2 小数模式（`unit.fraction === false`）

#### 后端实现

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L228-L240)

```python
def _format_quantity_for_display(self) -> str:
    # decimal
    if self.unit and not self.unit.fraction:
        qty = round(self.quantity or 0, INGREDIENT_QTY_PRECISION)  # 3 位
        if qty.is_integer():
            return str(int(qty))      # 整数显示为 "2"
        else:
            return str(qty)           # 小数显示为 "0.5"
```

#### 前端实现

[use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L99-L104)

```typescript
if (unit && !unit.fraction) {
  const minVal = 10 ** -DECIMAL_PRECISION;  // = 0.001
  returnQty = scaledQuantity >= minVal
    ? Number(scaledQuantity.toPrecision(DECIMAL_PRECISION)).toString()
    : `< ${minVal}`;
}
```

**前端特有行为**：小于 `0.001` 的数量显示为 `< 0.001`（后端无此逻辑，后端由 Python Fraction 处理）。

### 3.3 分数模式（`unit.fraction === true` 或无单位）

#### 后端实现

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L242-L256)

使用 Python 标准库 `fractions.Fraction`，限制最大分母为 32：

```python
qty = Fraction(self.quantity or 0).limit_denominator(MAX_INGREDIENT_DENOMINATOR)

if qty.denominator == 1:
    return str(qty.numerator)                              # 2 → "2"

if qty.numerator <= qty.denominator:
    return display_fraction(qty)                            # 1/2 → "¹⁄₂" (Unicode)

# 假分数转换为带分数: 11/4 → 2 ³⁄₄
whole_number = 0
while qty.numerator > qty.denominator:
    whole_number += 1
    qty -= 1
return f"{whole_number} {display_fraction(qty)}"
```

分数的 Unicode 显示（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L26-L35)）：
- 分子字符转上标：`1234567890` → `¹²³⁴⁵⁶⁷⁸⁹⁰`
- 分母字符转下标：`1234567890` → `₁₂₃₄₅₆₇₈₉₀`
- 中间用普通斜杠 `/`

#### 前端实现

使用自定义 mediant 算法 [use-fraction.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-fraction.ts#L4-L38)：

```typescript
frac(x, D, mixed)  // x=数值, D=最大分母, mixed=是否带分数
```

返回数组 `[整数部分, 分子, 分母]`。

前端在 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L105-L123) 中应用：

```typescript
const minVal = 1 / FRAC_MIN_DENOM;  // = 0.1
const isUnderMinVal = !(scaledQuantity >= minVal);

const fraction = !isUnderMinVal ? frac(scaledQuantity, FRAC_MIN_DENOM, true) : [0, 1, FRAC_MIN_DENOM];

// 拼接整数 + 分数的 HTML
if (fraction[0] !== undefined && fraction[0] > 0)
  returnQty += fraction[0];
if (fraction[1] > 0)
  returnQty += includeFormating
    ? `<sup>${fraction[1]}</sup><span>&frasl;</span><sub>${fraction[2]}</sub>`
    : ` ${fraction[1]}/${fraction[2]}`;

if (isUnderMinVal)
  returnQty = `< ${returnQty}`;  // 显示 "< ¹⁄₁₀"
```

**前端 vs 后端差异**：
| 维度 | 后端 | 前端 |
|------|------|------|
| 最大分母 | 32 | 10（用于原料显示）；`useScaledAmount` 用 10 |
| 分数渲染 | Unicode 上下标 | HTML `<sup>/<sub>` 或纯文本 `x/y` |
| 极小值处理 | 由 Fraction 自动收敛 | `< 1/10`（分数）或 `< 0.001`（小数） |

### 3.4 `useScaledAmount` 的格式化（用于 servings/yield）

[use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.ts#L3-L22)

```typescript
function formatQuantity(val: number): string {
  if (Number.isInteger(val)) return val.toString();
  const { frac } = useFraction();
  const fraction = frac(val, 10, true);   // 最大分母 10
  // 输出 HTML: "1<sup>1</sup><span>&frasl;</span><sub>2</sub>"
}
```

用于 servings 和 yield 数量的显示，**始终使用分数模式**（不受 `unit.fraction` 影响）。

### 3.5 测试用例验证

[use-scaled-amount.test.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.test.ts) 中的典型案例：

| 输入 | scale | scaledAmount | scaledAmountDisplay |
|------|-------|--------------|---------------------|
| 3 | 1 | 3 | "3" |
| 3 | 2 | 6 | "6" |
| 3 | 0 | 0 | "" |
| 0.5 | 1 | 0.5 | "¹⁄₂"（HTML） |
| 1.5 | 1 | 1.5 | "1¹⁄₂" |
| 1.5 | 9 | 13.5 | "13¹⁄₂" |
| 1 | 0.125 | 0.125 | "¹⁄₈" |
| 1.3344559997 | 1 | 1.334 | "1¹⁄₃" |

---

## 4. 原料（Ingredient）的显示合成

### 4.1 后端自动合成 display 字段

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L205-L323)

在 `@model_validator(mode="after")` 中自动触发：

```python
@model_validator(mode="after")
def format_display(self):
    if not self.display:
        self.display = self._format_display()
    return self
```

合成逻辑 `_format_display()`：

```python
components = []
if self.quantity:
    components.append(self._format_quantity_for_display())  # 数量
if self.quantity and self.unit:
    components.append(self._format_unit_for_display())      # 单位
if self.food:
    components.append(self._format_food_for_display(...))   # 食材名
if self.note:
    components.append(self.note)                            # 备注
return " ".join(components).strip()
```

**关键点**：只有当 `quantity` 存在时才显示单位；如果 `quantity` 是 0/None，单位也不显示。

### 4.2 前端合成 parseIngredientText

[use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L138-L143)

前端在组件中实时合成（**不使用后端的 `display` 字段**），因为要考虑 scale：

```typescript
function parseIngredientText(ingredient, scale = 1, includeFormating = true): string {
  const { quantity, unit, name, note } = useParsedIngredientText(ingredient, scale, includeFormating);
  const text = `${quantity || ""} ${unit || ""} ${name || ""} ${note || ""}`
    .replace(/ {2,}/g, " ").trim();
  return sanitizeIngredientHTML(text);
}
```

`useParsedIngredientText` 返回结构化结果，供不同组件按需组装：

- [RecipeIngredientListItem.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue#L51-L53) — 用于常规列表
- [RecipeIngredientHtml.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue#L25-L29) — 用于步骤中的内联引用
- [RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue#L365-L369) — 用于打印

### 4.3 复制文本（无 HTML 格式）

[RecipeIngredients.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredients.vue#L78-L93) 中复制功能使用 `includeFormating = false`，输出纯文本分数如 "1 1/2" 而非 HTML。

### 4.4 ingredientToParserString（用于重新解析）

[use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L145-L158)

优先级：
1. 如果有 `originalText`，直接使用
2. 如果既无 `unit` 也无 `food`（未解析的原料），直接使用 `note`
3. 否则用 `parseIngredientText(ingredient, 1, false)` 重建

---

## 5. 单位换算与合并

单位换算主要用于购物清单，在常规配方显示中不做自动换算（只做数量缩放）。

### 5.1 UnitConverter 核心

基于 Python `pint` 库实现，位于 [unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L20-L105)

```python
class UnitConverter:
    def __init__(self):
        self.ureg = UnitRegistry()

    def can_convert(self, unit, to_unit) -> bool: ...
    def convert(self, quantity, unit, to_unit) -> tuple[float, Unit]: ...
    def merge(self, qty1, unit1, qty2, unit2) -> tuple[float, Unit]: ...
```

### 5.2 Ounce 特殊处理

[unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L24-L40)

当 "ounce"（盎司，重量单位）与体积单位一起使用时，自动视为 "fluid_ounce"（液盎司）。

### 5.3 自定义单位的标准化换算

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L158-L167) 定义了校验：
- `standard_unit` 和 `standard_quantity` 必须同时设置
- `standard_quantity` 必须 > 0，否则视为未设置

标准化单位列表（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L38-L57)）：

| 英制 | 公制 |
|------|------|
| `fluid_ounce`, `cup` | `milliliter`, `liter` |
| `ounce`, `pound` | `gram`, `kilogram` |

### 5.4 购物清单中的合并逻辑

[shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L45-L128)

```python
def can_merge(self, item1, item2) -> bool:
    # food_id 必须相同
    # 若 unit_id 不同，则两个单位都必须有 standard_unit 且可互相转换
    ...

def merge_items(self, from_item, to_item):
    # 通过 merge_quantity_and_unit() 换算后合并
    # 结果选择较大的单位（当数量 >= 1），否则选择较小的单位
```

[unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L107-L146) 中 `merge_quantity_and_unit` 的策略：
1. 将两个自定义单位通过 `standard_quantity * standard_unit` 注册到 pint
2. 合并后先转换到较大单位
3. 若结果 < 1，则回退到较小单位（避免 "0.5 kg"，优先显示 "500 g"）

---

## 6. 营养值（Nutrition）的处理

### 6.1 数据模型

[recipe_nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_nutrition.py#L7-L24)

```python
class Nutrition(MealieModel):
    calories: str | None = None
    fat_content: str | None = None
    protein_content: str | None = None
    carbohydrate_content: str | None = None
    sodium_content: str | None = None
    sugar_content: str | None = None
    fiber_content: str | None = None
    cholesterol_content: str | None = None
    saturated_fat_content: str | None = None
    trans_fat_content: str | None = None
    unsaturated_fat_content: str | None = None

    model_config = ConfigDict(
        coerce_numbers_to_str=True,   # 数字自动转字符串存储
        alias_generator=to_camel,     # fatContent → fat_content
    )
```

**所有字段均为 `string` 类型**，不是数字。

### 6.2 显示与缩放

**关键事实：营养值不随 scale 自动缩放。**

- 前端 [use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts) 仅提供标签和单位后缀映射，不含任何缩放逻辑
- [RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue#L171-L179) 中直接显示原始值：
  ```html
  <td>{{ value ? (labels[key].suffix ? `${value} ${labels[key].suffix}` : value) : '-' }}</td>
  ```
- 各营养字段的后缀（单位）：
  - 卡路里：kcal
  - 脂肪/蛋白质/碳水/糖/纤维/饱和脂肪/反式脂肪/不饱和脂肪：g（克）
  - 胆固醇/钠：mg（毫克）

### 6.3 可见性控制

通过 `recipe.settings.showNutrition` 控制是否显示，同时在打印视图中可由用户偏好独立控制（见 8.2）。

---

## 7. 复数形式与本地化策略

### 7.1 单位的复数判断

前端 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L90)：

```typescript
const usePluralUnit = quantity !== undefined &&
  ((quantity || 0) * scale > 1 || (quantity || 0) * scale === 0);
```

规则：`quantity * scale > 1` 或 `quantity * scale === 0` 时用复数。
- `0` 个 → 复数（"0 tablespoons"）
- `0.5` 个 → 单数（"0.5 tablespoon"）
- `1` 个 → 单数
- `2` 个 → 复数

后端 [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L262) 规则相同：

```python
use_plural = self.quantity and self.quantity > 1
```

### 7.2 食材名的复数策略（Locale 相关）

由 [locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/lang/locale_config.py#L10-L77) 中的 `LocalePluralFoodHandling` 决定：

| 策略 | 含义 | 使用语言 |
|------|------|----------|
| `ALWAYS` | 只要 quantity > 1 就用复数 | 大部分欧洲语言（法语、西班牙语、德语等） |
| `WITHOUT_UNIT` | 有单位时用单数，无单位时才用复数 | **en-US**, en-GB |
| `NEVER` | 永远不用复数 | 中文（zh-CN, zh-TW）、日语、韩语、越南语、土耳其语 |

前端实现 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L63-L80)：

```typescript
function shouldUsePluralFood(quantity, hasUnit, pluralFoodHandling): boolean {
  if (quantity && quantity <= 1) return false;   // 0 < qty <= 1 永不为复数
  switch (pluralFoodHandling) {
    case "always":       return true;
    case "without-unit": return !(quantity && hasUnit);
    case "never":        return false;
    default:             return !(quantity && hasUnit);
  }
}
```

**注意**：`quantity * scale` 的结果传入此函数，即缩放后的数量影响食材复数形式。

### 7.3 单位缩写 vs 全名

显示优先级（前后端逻辑一致）：
1. 如果 `useAbbreviation === true`：
   - 用 `pluralAbbreviation`（复数）或 `abbreviation`（单数）
   - 若缩写为空，回退到全名
2. 否则用 `pluralName`（复数）或 `name`（单数）
3. 若 `pluralName` / `pluralAbbreviation` 未设置，回退到单数形式

---

## 8. 打印视图的影响

### 8.1 打印容器与 CSS 媒体查询

[RecipePrintContainer.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintContainer.vue#L25-L54)

```css
.print-container {
  display: none;   /* 屏幕上默认隐藏 */
}

@media print {
  .print-container {
    display: block !important;   /* 打印时显示 */
  }
  .v-main__wrap {
    position: absolute; top: 0; left: 0;  /* 占满打印页 */
  }
  /* 所有文字强制黑色、不透明 */
}
```

scale 传入打印容器，打印视图与普通视图使用**相同的缩放值**。

### 8.2 用户打印偏好

[RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue) 可配置：

| 偏好项 | 作用 |
|--------|------|
| `imagePosition` | left / right / hidden |
| `showDescription` | 是否显示描述 |
| `showNotes` | 是否显示备注 |
| `showNutrition` | 是否显示营养信息（独立于 recipe.settings） |
| `expandChildRecipes` | 是否展开关联配方的原料 |

由 [useUserPrintPreferences](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/use-users/preferences.ts) 持久化。

### 8.3 RecipePrintView 的显示差异

[RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue)

**Yield/Servings 显示**（L232-L254）：
- 使用 `useScaledAmount(recipe.recipeYieldQuantity, scale)` + `i18n.t("recipe.yields-amount-with-text", ...)`
- 使用 `useScaledAmount(recipe.recipeServings, scale)` + `i18n.t("recipe.serves-amount", ...)`
- 两者都有时用分号分隔；否则只显示非空的那个

**原料布局**（L68-L95）：
- 双栏网格（`grid-template-columns: 1fr 1fr`）
- 每页尽量不拆分段（`page-break-inside: avoid`）
- 当 `expandChildRecipes=true` 时递归展开关联配方的原料

**营养表格**（L167-L184）：
- 仅当 `preferences.showNutrition === true` 时显示
- 不缩放，直接使用原始字符串值

**样式差异**：
- 所有颜色强制黑色，透明度强制 1
- 原料、步骤、备注字体均为 14px
- 步骤标题带下划线

### 8.4 屏幕上的 d-print-none

在 [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L24) 中：
```html
<v-card flat class="d-print-none">
```
所有交互元素（编辑按钮、评论、导航等）在打印时自动隐藏。

---

## 9. 关键文件速查表

### 后端 Python

| 文件 | 职责 |
|------|------|
| [mealie/schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe.py) | Recipe 数据模型、servings/yield 字段校验 |
| [mealie/schema/recipe/recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py) | Ingredient 模型、数量格式化（小数/分数）、单位/食材显示 |
| [mealie/schema/recipe/recipe_nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_nutrition.py) | Nutrition 模型（全字符串字段） |
| [mealie/services/parser_services/parser_utils/unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py) | 单位换算（pint）、购物清单合并 |
| [mealie/services/household_services/shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py) | 购物清单原料合并（含 scale 累加） |
| [mealie/services/recipe/recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/recipe/recipe_service.py) | 配方 CRUD 服务 |
| [mealie/lang/locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/lang/locale_config.py) | 本地化复数策略配置 |

### 前端 TypeScript / Vue

| 文件 | 职责 |
|------|------|
| [frontend/app/composables/recipes/use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.ts) | 通用数量缩放 + 格式化（用于 servings/yield） |
| [frontend/app/composables/recipes/use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts) | 原料文本解析、缩放、小数/分数格式化、复数策略 |
| [frontend/app/composables/recipes/use-fraction.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-fraction.ts) | 分数算法（mediant） |
| [frontend/app/composables/recipes/use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts) | 营养标签与单位后缀映射 |
| [frontend/app/composables/recipe-page/shared-state.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipe-page/shared-state.ts) | 配方页面共享状态（不含 scale，scale 由 RecipePage 管理） |
| [frontend/app/composables/store/use-unit-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/store/use-unit-store.ts) | 单位数据 store |
| [frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) | scale 定义位置，向子组件传播 |
| [frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue) | scale 基准值计算（servings/yield 回退逻辑） |
| [frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue) | scale 交互（用户调整份数） |
| [frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue) | 原料列表项渲染（带 scale） |
| [frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue) | 步骤内联原料引用渲染 |
| [frontend/app/components/Domain/Recipe/RecipePrintContainer.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintContainer.vue) | 打印容器（@media print CSS） |
| [frontend/app/components/Domain/Recipe/RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue) | 打印视图内容（双栏原料、营养表等） |
| [frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue) | 打印偏好对话框 |
| [frontend/app/lib/api/types/recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/lib/api/types/recipe.ts) | 前端 TypeScript 类型定义 |

### 测试文件

| 文件 | 覆盖内容 |
|------|----------|
| [frontend/app/composables/recipes/use-scaled-amount.test.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.test.ts) | useScaledAmount 的整数、分数、带分数、缩放、极小值测试 |
| [frontend/app/composables/recipes/use-recipe-ingredients.test.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.test.ts) | 原料文本解析、小数/分数格式化、复数策略、极小值显示、HTML 净化、ingredientToParserString |
| [tests/unit_tests/schema_tests/test_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/tests/unit_tests/schema_tests/test_recipe.py) | Recipe servings/yield 字段校验 |

---

## 总结：一致性如何保证

整个系统通过以下机制保持原始数量、缩放数量、格式化、营养值和打印视图的一致性：

1. **单一 scale 源**：`scale` 在 [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L449) 定义为单一根状态，通过 prop 逐级向下传递，所有子组件共享同一个 scale 值。

2. **一致的格式化函数**：后端（Python `Fraction` + `round(,3)`）和前端（`frac()` mediant 算法 + `toPrecision(3)`）使用相同的精度规则（3 位小数、分数最大分母约束）。

3. **单位决定显示模式**：`unit.fraction` 属性统一控制小数/分数选择，无论是后端 `display` 字段还是前端 `parseIngredientText` 都遵循同一规则。

4. **营养值独立**：Nutrition 字段全部以字符串存储和显示，不参与缩放计算，避免了浮点精度问题，但也意味着营养值不会随份数调整而变化。

5. **打印视图复用逻辑**：[RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue) 使用完全相同的 `useScaledAmount` 和 `useIngredientTextParser` 函数，仅在布局（双栏网格）和可见性（偏好控制）上有差异。
