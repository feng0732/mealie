# Recipe Scaling 与 Serving 显示规则深度解析

本文档系统梳理 Mealie 中 Recipe 的数量缩放（scaling）、份量（servings/yield）、原料显示、营养值、单位换算、小数/分数格式化以及打印视图等各环节的联动机制，并逐一厘清每条路径的 scale 使用差异、HTML/纯文本差异，以及这些差异对营养值和单位换算理解的边界影响。

---

## 目录

1. [核心数据模型与字段](#1-核心数据模型与字段)
2. [缩放比例（Scale）的计算与完整传播链](#2-缩放比例scale的计算与完整传播链)
3. [数量格式化：小数 vs 分数 & HTML vs 纯文本](#3-数量格式化小数-vs-分数--html-vs-纯文本)
4. [原料（Ingredient）的显示合成与输出路径](#4-原料ingredient的显示合成与输出路径)
5. [单位换算与合并的边界](#5-单位换算与合并的边界)
6. [营养值（Nutrition）的处理与理解边界](#6-营养值nutrition的处理与理解边界)
7. [复数形式与本地化策略](#7-复数形式与本地化策略)
8. [打印视图的两条路径与差异](#8-打印视图的两条路径与差异)
9. [全路径 Scale 使用对照表](#9-全路径-scale-使用对照表)
10. [关键文件速查表](#10-关键文件速查表)
11. [差异总结与一致性边界](#11-差异总结与一致性边界)

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
| `referenced_recipe` (前端 `referencedRecipe`) | `Recipe \| None` | 关联的子配方，用于递归展开 |

**Unit（单位）核心属性**（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L148-L188)）：
- `fraction: bool` — 决定该单位的数量用分数还是小数显示
- `standard_unit: str \| None` — 标准化单位名（如 "gram"、"milliliter"）
- `standard_quantity: float \| None` — 对应标准化单位的换算系数
- `use_abbreviation: bool` — 是否使用缩写
- `abbreviation` / `plural_abbreviation` — 缩写（单数/复数）
- `name` / `plural_name` — 全名（单数/复数）

---

## 2. 缩放比例（Scale）的计算与完整传播链

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

### 2.3 Scale 的完整传播链（含所有分支）

```
RecipePage.vue                        scale = ref(1)
│
├─ RecipePageScale.vue                ← v-model:scale（双向读写）
│
├─ RecipePageHeader.vue               ← :recipe-scale="scale"
│   ├─ RecipePageInfoCard.vue         ← :recipe-scale="recipeScale"
│   │   └─ RecipeYield.vue            ← :scale="recipeScale"
│   │       └─ useScaledAmount(yieldQuantity, scale)
│   │
│   └─ RecipeActionMenu.vue           ← :recipe-scale="recipeScale"
│       └─ RecipeContextMenu.vue      ← :recipe-scale="recipeScale"
│           └─ contentProps 展开所有 props
│               └─ RecipeContextMenuContent.vue   ← :recipe-scale (继承)
│                   │
│                   ├─ ⚠️ RecipeDialogPrintPreferences.vue  ← :recipe (但不传递 scale！)
│                   │   └─ RecipePrintView.vue    ← **无 :scale prop → 使用默认 scale=1**
│                   │
│                   └─ RecipeDialogAddToShoppingList.vue
│                       └─ :recipes=[{ ...recipeRef, scale: recipeScale }]
│                           ├─ 前端显示: RecipeIngredientListItem ← :scale="recipeSection.recipeScale"
│                           └─ 提交后端: recipeIncrementQuantity = recipeSection.recipeScale
│
├─ RecipePageIngredientToolsView.vue  ← :scale
│   └─ RecipeIngredients.vue          ← :scale
│       ├─ RecipeIngredientListItem.vue  ← :scale
│       │   └─ parseIngredientText(ingredient, scale)
│       └─ ingredientCopyText         ← parseIngredientText(ingredient, scale, false)
│
├─ RecipePageInstructions.vue         ← :scale
│   └─ RecipeIngredientHtml.vue       ← :scale
│       └─ useParsedIngredientText(ingredient, scale)
│
├─ RecipePageParseDialog.vue          ← 未接收 scale
│   └─ ingredientToParserString(ingredient)
│       └─ parseIngredientText(ingredient, **1**, false)   ← 硬编码 scale=1
│
└─ RecipePrintContainer.vue           ← :scale="scale"
    └─ RecipePrintView.vue            ← :scale="scale", :density="'compact'"
        ├─ parseIngredientText(ingredient, scale)
        ├─ useScaledAmount(yieldQuantity, scale)
        └─ useScaledAmount(recipeServings, scale)

另外两条独立路径（不经过 RecipePage）：

1. use-extract-ingredient-references.ts (步骤原料自动关联)
   └─ parseIngredientText(ingredient)  ← 默认 scale=1

2. mealplan/planner.vue (食谱计划添加购物清单)
   └─ RecipeDialogAddToShoppingList.vue
       └─ scale 取决于 meal plan 中的 recipe 引用数量
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
- **始终使用分数格式化**（不受 `unit.fraction` 影响），因为用于 servings/yield 数量

#### 原料数量缩放

位于 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L95-L124)

```typescript
if (quantity && Number(quantity) !== 0) {
  const scaledQuantity = Number((quantity * scale));
  // 根据 unit.fraction 决定走小数分支还是分数分支
  if (unit && !unit.fraction) {
    // 小数模式
  } else {
    // 分数模式
  }
}
```

**注意**：原料缩放**不先 `toFixed(3)`**，而是直接用 `quantity * scale` 的结果送入格式化函数。这与 `useScaledAmount` 略有差异（useScaledAmount 先 toFixed 再格式化）。

#### 购物清单后端缩放

位于 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L323-L385)

```python
def get_shopping_list_items_from_recipe(self, list_id, recipe_id, scale=1, recipe_ingredients=None):
    for ingredient in recipe_ingredients:
        if isinstance(ingredient.referenced_recipe, Recipe):
            # 子配方递归：sub_scale = 原料数量 * 父 scale
            sub_scale = (ingredient.quantity or 1) * scale
            sub_items = self.get_shopping_list_items_from_recipe(
                list_id, sub_recipe.id, sub_scale, ...
            )
            continue

        new_item = ShoppingListItemCreate(
            quantity=ingredient.quantity * scale if ingredient.quantity else 0,
            recipe_references=[
                ShoppingListItemRecipeRefCreate(
                    recipe_id=recipe_id,
                    recipe_quantity=ingredient.quantity,  # 原始数量
                    recipe_scale=scale,                   # 记录 scale
                    ...
                )
            ],
        )
```

**关键点**：
- 后端直接做 `quantity * scale`，存储到购物清单项
- 同时在 `recipe_reference` 中分别记录 `recipe_quantity`（原始数量）和 `recipe_scale`（缩放倍数），用于后续移除时精确计算

---

## 3. 数量格式化：小数 vs 分数 & HTML vs 纯文本

数量格式化有三个独立维度的决策：
1. **小数模式 vs 分数模式**：由 `unit.fraction` 决定
2. **HTML 格式化 vs 纯文本格式化**：由 `includeFormating` 参数决定
3. **精度/分母上限**：前端原料用分母 10，后端用分母 32；`useScaledAmount` 用分母 10

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

**前端特有行为**：小于 `0.001` 的数量显示为 `< 0.001`（后端无此逻辑）。

### 3.3 分数模式（`unit.fraction === true` 或无单位）

#### 后端实现

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L242-L256)

使用 Python 标准库 `fractions.Fraction`，限制最大分母为 **32**：

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

const fraction = !isUnderMinVal
  ? frac(scaledQuantity, FRAC_MIN_DENOM, true)   // 最大分母 = 10
  : [0, 1, FRAC_MIN_DENOM];

// 拼接整数 + 分数
if (fraction[0] !== undefined && fraction[0] > 0)
  returnQty += fraction[0];
if (fraction[1] > 0)
  returnQty += includeFormating
    ? `<sup>${fraction[1]}</sup><span>&frasl;</span><sub>${fraction[2]}</sub>`
    : ` ${fraction[1]}/${fraction[2]}`;

if (isUnderMinVal)
  returnQty = `< ${returnQty}`;
```

### 3.4 HTML 格式化 vs 纯文本格式化的输出差异

`includeFormating` 参数控制分数的渲染方式：

| `includeFormating` | 输出示例 (1/2) | 输出示例 (1 1/2) | 使用场景 |
|---|---|---|---|
| `true` (默认) | `<sup>1</sup><span>⁄</span><sub>2</sub>` | `1<sup>1</sup><span>⁄</span><sub>2</sub>` | 页面渲染：列表项、步骤内联、打印视图、yield/servings 显示 |
| `false` | ` 1/2` (注意前置空格) | `1 1/2` | 复制文本、重新解析 (`ingredientToParserString`)、购物清单后端提交 |

**注意纯文本分数的前置空格**：`includeFormating=false` 时，分数前有一个空格（` 1/2`），而整数和分数之间有空格（`1 1/2`）。纯整数时没有空格问题。

### 3.5 前端 vs 后端格式化差异汇总

| 维度 | 后端 Python | 前端 TypeScript |
|------|-------------|-----------------|
| 小数精度 | `round(,3)` 后判断整数 | `toPrecision(3)` 后 `toString()` |
| 分数最大分母 | 32 | 10（原料）、10（useScaledAmount） |
| 分数渲染 | Unicode 上下标 (`¹⁄₂`) | HTML `<sup>/<sub>` 或纯文本 `x/y` |
| 极小值处理 | 由 Fraction 自动收敛到最接近分数 | `< 1/10`（分数）或 `< 0.001`（小数） |
| HTML 净化 | — | `DOMPurify.sanitize`, 只允许 `strong`、`sup` 标签 |

### 3.6 useScaledAmount 的格式化（用于 servings/yield）

[use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.ts#L3-L22)

```typescript
function formatQuantity(val: number): string {
  if (Number.isInteger(val)) return val.toString();
  const { frac } = useFraction();
  const fraction = frac(val, 10, true);   // 最大分母 10
  // 输出 HTML: "1<sup>1</sup><span>&frasl;</span><sub>2</sub>"
}
```

用于 servings 和 yield 数量的显示，**始终使用分数模式 + HTML 格式化**（不受 `unit.fraction` 影响）。

### 3.7 测试用例验证

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

## 4. 原料（Ingredient）的显示合成与输出路径

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

`santizeIngredientHTML` 使用 DOMPurify，只允许 `strong` 和 `sup` 标签通过。

### 4.3 所有输出路径对照表

| 路径 | 调用方式 | scale | includeFormating | 分数输出格式 |
|------|---------|-------|-----------------|-------------|
| 页面原料列表 | `parseIngredientText(ing, scale)` | 当前 scale | `true` (默认) | HTML `<sup>/<sub>` |
| 步骤内联原料 | `useParsedIngredientText(ing, scale)` | 当前 scale | `true` (默认) | HTML `<sup>/<sub>` |
| 打印视图（实际打印） | `parseIngredientText(ing, scale)` | 当前 scale | `true` (默认) | HTML `<sup>/<sub>` |
| 打印预览（偏好对话框） | `parseIngredientText(ing, 1)` | **默认 1** | `true` (默认) | HTML `<sup>/<sub>` |
| 复制按钮 | `parseIngredientText(ing, scale, false)` | 当前 scale | `false` | 纯文本 `x/y` |
| 购物清单对话框显示 | `RecipeIngredientListItem :scale="recipeScale"` | 当前 scale | `true` (默认) | HTML `<sup>/<sub>` |
| 购物清单后端提交 | 直接提交 ingredient 对象，后端 `quantity * scale` | 当前 scale | — | 不格式化，提交原始数值 |
| 批量解析对话框重建 | `ingredientToParserString()` → `parseIngredientText(ing, 1, false)` | **硬编码 1** | `false` | 纯文本 `x/y` |
| 步骤原料自动关联匹配 | `parseIngredientText(ing)` | **默认 1** | `true` (默认) | HTML，但仅用于字符串匹配 |
| 后端 display 字段 | `self._format_display()` | — | — | Unicode `¹⁄₂` |

### 4.4 ingredientToParserString（用于重新解析）

[use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L145-L158)

```typescript
function ingredientToParserString(ingredient: RecipeIngredient): string {
  if (ingredient.originalText) return ingredient.originalText;
  if (!ingredient.unit && !ingredient.food) return ingredient.note || "";
  return parseIngredientText(ingredient, 1, false) ?? "";  // ← 硬编码 scale=1
}
```

**注意**：此函数硬编码 `scale=1` 且 `includeFormating=false`，用于将原料对象转成可重新解析的文本。

### 4.5 步骤原料自动关联（use-extract-ingredient-references）

[use-extract-ingredient-references.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipe-page/use-extract-ingredient-references.ts#L37-L65)

```typescript
function extractIngredientReferences(recipeIngredients, activeRefs, text): Set<string> {
  function ingredientMatchesWord(ingredient, word) {
    const searchText = parseIngredientText(ingredient);  // ← 默认 scale=1
    return searchText.toLowerCase().includes(word.toLowerCase());
  }
  // ...
}
```

此处 `parseIngredientText` 未传 scale，使用默认值 `1`。这是为了匹配步骤文本中的原始描述（步骤文本通常不随 scale 变化）。

---

## 5. 单位换算与合并的边界

### 5.1 单位换算的触发边界：**仅发生在购物清单后端合并**

| 场景 | 是否做单位换算 | 是否做数量缩放 | 说明 |
|------|--------------|--------------|------|
| 配方页面显示 | ❌ 否 | ✅ 是 (`* scale`) | 仅乘 scale，不改变单位 |
| 打印视图显示 | ❌ 否 | ✅ 是 (`* scale`) | 仅乘 scale，不改变单位 |
| 打印预览 | ❌ 否 | ❌ 否 (scale=1) | 原样显示 |
| 复制文本 | ❌ 否 | ✅ 是 (`* scale`) | 仅乘 scale，不改变单位 |
| 购物清单前端对话框 | ❌ 否 | ✅ 是 (`* scale`) | 仅乘 scale，不改变单位 |
| 购物清单后端创建 | ✅ 是（同食材合并时） | ✅ 是 (`* scale`) | 用 pint 库换算后合并 |
| 购物清单后端移除 | ✅ 是（已有合并结果） | ✅ 是（按比例减少） | 按 recipe_scale 精确扣减 |

**结论**：用户在页面上看到的原料单位，永远与配方中存储的单位一致，不会自动换算成更合适的单位（例如 1000 g 不会自动显示为 1 kg）。只有当同一食材的多个项在购物清单中合并时，才会在后端进行单位换算合并。

### 5.2 UnitConverter 核心

基于 Python `pint` 库实现，位于 [unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L20-L105)

```python
class UnitConverter:
    def __init__(self):
        self.ureg = UnitRegistry()

    def can_convert(self, unit, to_unit) -> bool: ...
    def convert(self, quantity, unit, to_unit) -> tuple[float, Unit]: ...
    def merge(self, qty1, unit1, qty2, unit2) -> tuple[float, Unit]: ...
```

### 5.3 Ounce 特殊处理

[unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L24-L40)

当 "ounce"（盎司，重量单位）与体积单位一起使用时，自动视为 "fluid_ounce"（液盎司）。这仅在**购物清单合并**时生效。

### 5.4 自定义单位的标准化换算

[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L158-L167) 定义了校验：
- `standard_unit` 和 `standard_quantity` 必须同时设置
- `standard_quantity` 必须 > 0，否则视为未设置

标准化单位列表（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L38-L57)）：

| 英制 | 公制 |
|------|------|
| `fluid_ounce`, `cup` | `milliliter`, `liter` |
| `ounce`, `pound` | `gram`, `kilogram` |

### 5.5 购物清单中的合并逻辑

[shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L45-L128)

```python
def can_merge(self, item1, item2) -> bool:
    # food_id 必须相同
    # 若 unit_id 不同，则两个单位都必须有 standard_unit 且可互相转换
    ...

def merge_items(self, from_item, to_item):
    # 通过 merge_quantity_and_unit() 换算后合并
```

[unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L107-L146) 中 `merge_quantity_and_unit` 的策略：
1. 将两个自定义单位通过 `standard_quantity * standard_unit` 注册到 pint
2. 合并后先转换到较大单位
3. 若结果 < 1，则回退到较小单位（避免 "0.5 kg"，优先显示 "500 g"）

### 5.6 单位换算的理解边界

用户在前端看到的"缩放"是简单乘法 `quantity * scale`，不涉及单位换算。因此：
- 如果配方中某原料是 "500 g 面粉"，scale=2 时显示为 "1000 g 面粉"（不会自动转成 "1 kg 面粉"）
- 但加入购物清单后，如果该购物清单中已有 "1 kg 面粉"，后端会将两者合并显示为 "2 kg 面粉"

这个行为在购物清单中是正确的，但如果用户从配方页面复制文本（`1000 g 面粉`）后手动记录，可能与购物清单自动换算的结果（`1 kg 面粉`）不一致。

---

## 6. 营养值（Nutrition）的处理与理解边界

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

### 6.2 显示与缩放：营养值**永不缩放**

**关键事实：营养值在所有路径中均不随 scale 自动缩放，始终原样显示。**

前端 [use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts) 仅提供标签和单位后缀映射，不含任何缩放逻辑。

所有显示路径的营养值处理一致：

| 路径 | 是否缩放 | 处理方式 |
|------|---------|---------|
| 配方页面营养标签 | ❌ 否 | 直接显示原始值 + 单位后缀 |
| 实际打印视图 | ❌ 否 | `value + " " + suffix` (如 "500 kcal") |
| 打印预览对话框 | ❌ 否 | 同上（scale=1 时也不缩放，因为根本不做缩放） |
| 购物清单 | ❌ 否 | 不传递营养信息 |
| 购物清单后端 | ❌ 否 | 不存储营养信息 |
| 复制功能 | ❌ 否 | 复制原料列表，不包含营养 |

[RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue#L171-L179) 直接显示：

```html
<td>{{ value ? (labels[key].suffix ? `${value} ${labels[key].suffix}` : value) : '-' }}</td>
```

各营养字段的后缀（单位）：
- 卡路里：kcal
- 脂肪/蛋白质/碳水/糖/纤维/饱和脂肪/反式脂肪/不饱和脂肪：g（克）
- 胆固醇/钠：mg（毫克）

### 6.3 营养值的理解边界

由于营养值从不缩放，存在以下理解边界：

1. **每份量 vs 总份的认知错位**：
   - 如果配方营养是按"每份"录入的（例如 recipeServings=4，显示 500 kcal 表示每人份 500 kcal），当用户将 scale 调到 2（8 人份）时，营养表仍显示 500 kcal，用户可能误以为总热量仍是 500 kcal
   - 如果配方营养是按"整份"录入的（4 人份总共 500 kcal），scale=2 时仍显示 500 kcal 也是错误的
   - 系统不做任何营养缩放，也不提供"按每份/按总量"的标注，完全依赖用户自行理解录入方式

2. **打印预览 vs 实际打印的误导**：
   - 由于打印预览 scale=1，实际打印 scale=当前值，而营养值在两者中都一样——这本身是一致的
   - 但原料数量：预览显示 "1 cup rice"（scale=1），实际打印显示 "2 cup rice"（scale=2）
   - 营养值两者都显示 "500 kcal"，用户可能产生"原料翻倍但营养不变"的困惑

3. **子配方展开的营养缺失**：
   - 打印视图的 `expandChildRecipes` 选项会展开子配方的原料列表
   - 但营养值只显示主配方数据，不含子配方营养
   - 可能出现"展开了 3 个子配方共 20 项原料，但营养表还是只有主配方的 500 kcal"

4. **字符串精度**：
   - 由于以字符串存储，如果录入的是 "12.3456" 这样的值，显示时也原样输出 "12.3456 g"
   - 不会做任何格式化或四舍五入，也不会与原料数量联动

### 6.4 可见性控制

通过 `recipe.settings.showNutrition` 控制页面是否显示，在打印视图中额外由 `preferences.showNutrition`（用户打印偏好）独立控制。

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

**注意**：这里的 quantity 已经是缩放后的数量（`quantity * scale`），所以复数判断随 scale 变化。

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

**注意**：传入的 `quantity` 是缩放后的数量（`quantity * scale`），即缩放后的数量影响食材复数形式。

### 7.3 单位缩写 vs 全名

显示优先级（前后端逻辑一致）：
1. 如果 `useAbbreviation === true`：
   - 用 `pluralAbbreviation`（复数）或 `abbreviation`（单数）
   - 若缩写为空，回退到全名
2. 否则用 `pluralName`（复数）或 `name`（单数）
3. 若 `pluralName` / `pluralAbbreviation` 未设置，回退到单数形式

---

## 8. 打印视图的两条路径与差异

打印系统存在两条独立路径，scale 行为不同：

### 8.1 路径一：打印偏好预览（RecipeDialogPrintPreferences）

[RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue#L83-L91)

```html
<v-card ... class="print-preview" ...>
  <RecipePrintView :recipe="recipe" />   <!-- ⚠️ 无 :scale prop -->
</v-card>
```

**关键发现**：打印偏好对话框内嵌的 `RecipePrintView` **没有传递 `:scale` prop**，因此使用组件的默认值 `scale = 1`。

同时也没有传递 `:density` prop，使用默认值 `dense = false`。

**完整路径来源**：
```
RecipePage → RecipePageHeader → RecipeActionMenu → RecipeContextMenu
  → RecipeContextMenuContent  (:recipe-scale 已传入，但在打开打印偏好对话框时未使用)
    → RecipeDialogPrintPreferences
      → 内部直接 <RecipePrintView :recipe="recipe" />，只传了 recipe，没传 scale
```

[RecipeContextMenuContent.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeContextMenu/RecipeContextMenuContent.vue#L3)：

```html
<RecipeDialogPrintPreferences v-model="printPreferencesDialog" :recipe="recipeRef" />
```

这里只传递了 `:recipe`，没有传递 `:scale`。上下文菜单虽然接收到了 `recipeScale` prop（默认值 1，或来自 RecipePage 的当前值），但打印偏好对话框组件的 props 接口根本没有 `scale` 参数。

### 8.2 路径二：实际打印容器（RecipePrintContainer）

[RecipePrintContainer.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintContainer.vue#L1-L23)

```html
<template>
  <div class="print-container">
    <RecipePrintView
      :recipe="recipe"
      :scale="scale"           <!-- ✅ 传递了当前 scale -->
      :density="'compact'"     <!-- ✅ 传递了 compact 密度 -->
    />
  </div>
</template>

<script setup lang="ts">
interface Props {
  recipe: Recipe;
  scale?: number;
}
withDefaults(defineProps<Props>(), {
  scale: 1,
});
</script>
```

**路径来源**：
```
RecipePage.vue
  → <RecipePrintContainer :recipe="recipe" :scale="scale" />
```

由 [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L27) 直接传递当前的 `scale` ref。

### 8.3 两条路径的差异汇总

| 维度 | 打印偏好预览（对话框内） | 实际打印（window.print()） |
|------|----------------------|------------------------|
| 组件 | RecipeDialogPrintPreferences → RecipePrintView | RecipePrintContainer → RecipePrintView |
| scale 值 | **固定 1**（未传递 prop，使用默认值） | **当前用户设置的 scale**（从 RecipePage 传入） |
| dense 值 | `false`（默认） | `true`（`'compact'`） |
| servings 显示 | "Serves: 4"（原始份数） | "Serves: 8"（当前份数，如 scale=2） |
| 原料数量显示 | 原始数量 | 缩放后数量 |
| yield 显示 | 原始产出 | 缩放后产出 |
| 营养值显示 | 原始值（不变） | 原始值（不变） |
| 关联配方原料展开 | 按 preferences.expandChildRecipes | 按 preferences.expandChildRecipes |
| 样式 | 在对话框卡片中，带滚动条，padding 更大 | 全屏打印，双栏网格，强制黑白，布局更紧凑 |
| 触发方式 | 右键菜单 → 打印偏好 | 右键菜单 → 打印 / 直接 Ctrl+P |

### 8.4 打印用户偏好

两条打印路径共享同一份偏好数据 [useUserPrintPreferences](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/use-users/preferences.ts)：

| 偏好项 | 作用 |
|--------|------|
| `imagePosition` | left / right / hidden |
| `showDescription` | 是否显示描述 |
| `showNotes` | 是否显示备注 |
| `showNutrition` | 是否显示营养信息（独立于 recipe.settings） |
| `expandChildRecipes` | 是否展开关联配方的原料 |

### 8.5 RecipePrintView 中 scale 的实际影响范围

[RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue)

| 受影响元素 | 使用方式 |
|-----------|---------|
| `servingsDisplay`（份数显示） | `useScaledAmount(recipe.recipeYieldQuantity, props.scale)` |
| `yieldDisplay`（产出显示） | `useScaledAmount(recipe.recipeServings, props.scale)` |
| `parseText(ingredient)`（原料文本） | `parseIngredientText(ingredient, props.scale)` |
| 营养值表格 | ❌ 不受影响 |
| 步骤文本（SafeMarkdown） | ❌ 不受影响 |
| 备注、描述、图片 | ❌ 不受影响 |

### 8.6 CSS 媒体查询与实际打印触发

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
    position: absolute; top: 0; left: 0;
  }
}
```

当用户调用 `window.print()`（由 RecipePageHeader 的打印按钮触发）时：
- 屏幕上的正常页面内容大部分被 `.d-print-none` 隐藏
- `.print-container` 从 `display: none` 变为 `display: block`，占据打印页
- RecipePrintContainer 中的 scale 是 RecipePage 当前的 scale 值

**重要**：打印偏好对话框的"预览"不影响实际打印内容。预览只是对话框中的卡片展示，真正打印的是 RecipePrintContainer。

---

## 9. 全路径 Scale 使用对照表

| 路径/功能 | 文件位置 | Scale 来源 | Scale 值 | includeFormating | 备注 |
|---------|---------|-----------|---------|-----------------|------|
| **配方页面 - scale 定义** | [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L449) | `ref(1)` | 用户可调整 | — | 唯一真实源 |
| **配方页面 - Yield 显示** | [RecipeYield.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeYield.vue#L54) | RecipePage prop | 当前 scale | N/A（用 useScaledAmount，始终 HTML 分数） | |
| **配方页面 - 原料列表** | [RecipeIngredientListItem.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue#L51) | RecipePage prop | 当前 scale | `true` | HTML 分数 |
| **配方页面 - 复制按钮** | [RecipeIngredients.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredients.vue#L89) | RecipePage prop | 当前 scale | `false` | 纯文本分数 |
| **配方页面 - 步骤内联原料** | [RecipeIngredientHtml.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue#L25) | RecipePage prop | 当前 scale | `true` | HTML 分数 |
| **步骤原料自动关联匹配** | [use-extract-ingredient-references.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipe-page/use-extract-ingredient-references.ts#L42) | 函数默认值 | **固定 1** | `true` | 仅用于字符串匹配，scale 无实际影响 |
| **批量解析对话框重建** | [RecipePageParseDialog.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageParseDialog.vue#L376) → `ingredientToParserString` | 硬编码 | **固定 1** | `false` | 输出可重新解析的纯文本 |
| **打印偏好对话框预览** | [RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue#L90) | RecipePrintView 默认值 | **固定 1** | `true` | ⚠️ 关键不一致：预览永远是 scale=1 |
| **实际打印输出** | [RecipePrintContainer.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintContainer.vue#L5) | RecipePage prop | 当前 scale | `true` | |
| **购物清单对话框显示** | [RecipeDialogAddToShoppingList.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogAddToShoppingList.vue#L158) | RecipeContextMenu prop → RecipeWithScale | 当前 scale（来自 RecipePage） | `true` | |
| **购物清单后端存储** | [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L373) | API 参数 `recipe_increment_quantity` | 当前 scale（前端提交） | — | `quantity = ingredient.quantity * scale` |
| **子配方递归缩放** | [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L347) | `(ingredient.quantity or 1) * scale` | 累积乘积 | — | 子配方 scale = 引用数量 × 父配方 scale |
| **子配方对话框显示** | [RecipeDialogAddToShoppingList.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogAddToShoppingList.vue#L337) | `parentQuantity * parentScale` | 累积乘积 | `true` | 与后端一致 |
| **后端 ingredient.display 字段** | [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py#L205-L323) | — | **无 scale 概念** | — | 存储时格式化原始数量，不感知前端缩放 |
| **营养值（所有路径）** | 多处 | — | **永不缩放** | — | 始终原样输出字符串 |

---

## 10. 关键文件速查表

### 后端 Python

| 文件 | 职责 |
|------|------|
| [mealie/schema/recipe/recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe.py) | Recipe 数据模型、servings/yield 字段校验 |
| [mealie/schema/recipe/recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_ingredient.py) | Ingredient 模型、数量格式化（小数/分数 Unicode）、单位/食材显示 |
| [mealie/schema/recipe/recipe_nutrition.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/schema/recipe/recipe_nutrition.py) | Nutrition 模型（全字符串字段，永不缩放） |
| [mealie/services/parser_services/parser_utils/unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/parser_services/parser_utils/unit_utils.py) | 单位换算（pint）、购物清单合并 |
| [mealie/services/household_services/shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py) | 购物清单原料合并（含 scale 累加、子配方递归缩放、单位换算） |
| [mealie/services/recipe/recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/recipe/recipe_service.py) | 配方 CRUD 服务 |
| [mealie/lang/locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/lang/locale_config.py) | 本地化复数策略配置 |

### 前端 TypeScript / Vue

| 文件 | 职责 |
|------|------|
| [frontend/app/composables/recipes/use-scaled-amount.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.ts) | 通用数量缩放 + 分数 HTML 格式化（用于 servings/yield） |
| [frontend/app/composables/recipes/use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts) | 原料文本解析、缩放、小数/分数格式化（HTML/纯文本）、复数策略、ingredientToParserString |
| [frontend/app/composables/recipes/use-fraction.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-fraction.ts) | 分数算法（mediant） |
| [frontend/app/composables/recipes/use-recipe-nutrition.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-nutrition.ts) | 营养标签与单位后缀映射（无缩放逻辑） |
| [frontend/app/composables/recipe-page/use-extract-ingredient-references.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipe-page/use-extract-ingredient-references.ts) | 步骤原料自动关联（使用 scale=1 做文本匹配） |
| [frontend/app/composables/recipe-page/shared-state.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipe-page/shared-state.ts) | 配方页面共享状态（不含 scale） |
| [frontend/app/composables/store/use-unit-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/store/use-unit-store.ts) | 单位数据 store |
| [frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) | scale 定义位置，向子组件传播 |
| [frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageScale.vue) | scale 基准值计算（servings/yield 回退逻辑） |
| [frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeScaleEditButton.vue) | scale 交互（用户调整份数） |
| [frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientListItem.vue) | 原料列表项渲染（带 scale，HTML 分数） |
| [frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeIngredientHtml.vue) | 步骤内联原料引用渲染（带 scale） |
| [frontend/app/components/Domain/Recipe/RecipePrintContainer.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintContainer.vue) | 实际打印容器（@media print CSS，传递当前 scale） |
| [frontend/app/components/Domain/Recipe/RecipePrintView.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipePrintView.vue) | 打印视图内容（双栏原料、营养表，默认 scale=1） |
| [frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue) | 打印偏好对话框（内嵌 RecipePrintView，⚠️ 不传 scale） |
| [frontend/app/components/Domain/Recipe/RecipeContextMenu/RecipeContextMenu.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeContextMenu/RecipeContextMenu.vue) | 右键菜单（contentProps 展开传递 recipeScale） |
| [frontend/app/components/Domain/Recipe/RecipeContextMenu/RecipeContextMenuContent.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeContextMenu/RecipeContextMenuContent.vue) | 菜单内容（向购物清单传递 scale，向打印偏好不传） |
| [frontend/app/components/Domain/Recipe/RecipeDialogAddToShoppingList.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogAddToShoppingList.vue) | 购物清单对话框（前端显示 scale，后端提交 recipeIncrementQuantity） |
| [frontend/app/components/Domain/Recipe/RecipeYield.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeYield.vue) | 产出数量显示（useScaledAmount + scale） |
| [frontend/app/lib/api/types/recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/lib/api/types/recipe.ts) | 前端 TypeScript 类型定义 |

### 测试文件

| 文件 | 覆盖内容 |
|------|----------|
| [frontend/app/composables/recipes/use-scaled-amount.test.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-scaled-amount.test.ts) | useScaledAmount 的整数、分数、带分数、缩放、极小值测试 |
| [frontend/app/composables/recipes/use-recipe-ingredients.test.ts](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/composables/recipes/use-recipe-ingredients.test.ts) | 原料文本解析、小数/分数格式化、复数策略、极小值显示、HTML 净化、ingredientToParserString |
| [tests/unit_tests/schema_tests/test_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/tests/unit_tests/schema_tests/test_recipe.py) | Recipe servings/yield 字段校验 |

---

## 11. 差异总结与一致性边界

### 11.1 一致性：哪些路径保持一致

1. **共享当前 scale 的路径**：
   - 配方页面所有显示组件（原料列表、步骤内联、Yield 显示、复制文本）
   - 实际打印（RecipePrintContainer）
   - 购物清单对话框显示及后端提交
   - 这些路径都从 RecipePage.vue 的 `scale = ref(1)` 获取当前值

2. **一致的格式化函数**：
   - 所有前端显示路径都使用 `useIngredientTextParser` → `parseIngredientText` → `useParsedIngredientText` 同一条格式化链
   - 小数/分数模式决策统一由 `unit.fraction` 控制
   - 复数策略统一由 locale 配置和 `quantity * scale` 值控制

3. **营养值完全独立一致**：
   - 所有路径都不缩放营养值，始终输出原始字符串

### 11.2 不一致：哪些路径存在差异

| 不一致点 | 影响路径 | 影响程度 |
|---------|---------|---------|
| **打印预览 scale=1 vs 实际打印 scale=当前值** | 打印预览中 servings、yield、原料数量永远显示原始值，实际打印输出缩放后的值 | ⚠️ 高：用户可能以为预览=最终打印结果 |
| **前端分数最大分母=10 vs 后端=32** | 后端 `display` 字段与前端显示可能有细微差异（如 3/32 前端会收敛到 1/10） | 低：大部分常见分数一致 |
| **极小值前端 `< 0.001` / `< 1/10` vs 后端自动收敛** | 极小数量显示不一致 | 低：极少遇到 |
| **ingredientToParserString 硬编码 scale=1** | 批量解析对话框重建的文本是原始数量 | 中：符合"重新解析原始数据"的语义 |
| **步骤原料自动关联 scale=1** | 匹配逻辑使用原始数量文本 | 低：步骤文本也不随 scale 变化 |
| **前端不做单位换算 vs 后端购物清单合并做换算** | 页面显示 "1000 g"，加入购物清单后可能合并显示为 "1 kg" | 中：用户复制文本 vs 购物清单可能不一致 |
| **Unicode 分数（后端） vs HTML/纯文本分数（前端）** | 后端 display 字段用 `¹⁄₂`，前端用 `<sup>1</sup>⁄<sub>2</sub>` 或 `1/2` | 低：展示形式差异，语义一致 |
| **useScaledAmount 先 toFixed(3) vs 原料直接乘** | 精度处理略有不同 | 低：3 位精度足够，差异在可忽略范围 |
| **打印预览 dense=false vs 实际打印 dense=true** | 预览 padding 更大，实际打印更紧凑 | 低：仅影响视觉布局密度 |

### 11.3 对营养值理解的边界影响

1. **"份数对应"的认知错位**：
   - 用户看到 servings=4 对应 nutrition=500 kcal，可能理解为"每份 500 kcal"或"整份 4 人份总共 500 kcal"
   - 当 scale=2（servings 变为 8）时，nutrition 仍显示 500 kcal，用户会产生三种误解：
     - 误解 A："8 人份总共 500 kcal"（认为营养已随 servings 自动缩放，实际未缩放）
     - 误解 B："营养数据是每份的，所以 8 人份=1000 kcal"（假设数据库存的是每份营养，但无法确认）
     - 误解 C："系统出 bug 了，营养数据没更新"（预期跟随 scale 变化，但代码永不缩放）
   - **边界**：Mealie 的 Nutrition 字段是**自由文本字符串**，不做语义解析，也不承诺"每份"或"整份"的语义，完全由数据录入者自行定义字段含义。

2. **打印预览的误导放大**：
   - 由于 [RecipeDialogPrintPreferences.vue](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/frontend/app/components/Domain/Recipe/RecipeDialogPrintPreferences.vue#L90) 不传 scale，打印预览中 servings=4、原料显示原始量，同时 nutrition=500 kcal——这三者在预览中**看起来是匹配的**（符合用户对"4 人份对应 500 kcal"的直觉）
   - 但用户点击"打印"后，实际打印纸上 servings=8、原料量翻倍、nutrition 仍=500 kcal——三者关系突然"断裂"，用户会认为**打印功能出错**
   - **边界**：打印预览 ≠ 打印输出，预览使用默认 scale=1，实际打印使用当前 scale，而营养值在两条路径中均保持不变，造成 servings/原料 与 营养值的视觉关联断裂。

3. **子配方展开的营养缺失**：
   - 购物清单后端 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py#L347) 对子配方做 `sub_scale = (ingredient.quantity or 1) * scale` 递归缩放，原料量正确传播
   - 但子配方的 Nutrition **永远不会被展开或累加**到父配方中——父配方 Nutrition 字段完全独立
   - **边界**：scale 仅作用于原料数量的数值乘法；营养值体系完全独立，不做子配方聚合，不随 scale 做任何数学运算。

4. **字符串精度边界**：
   - Nutrition 字段经 Pydantic `coerce_numbers_to_str=True` 转为字符串，如 `500.0` → `"500.0"`，`1/3` 可能存为 `"0.3333333"`
   - 前端直接输出原始字符串，不做格式化或精度截断
   - **边界**：营养值显示的精度完全取决于数据库存储时的字符串形式，与 scale、原料格式化的精度策略（toFixed(3)、mediant 分母上限 10）完全无关。

---

### 11.4 对单位换算理解的边界影响

1. **"页面显示量" ≠ "购物清单合并量"**：
   - 配方页面、打印视图、复制文本等路径的数量 = `quantity * scale`，**不做任何单位换算**。例如 1000 g × 2 = 2000 g，显示为 "2000 g"
   - 加入购物清单后，后端 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/services/household_services/shopping_lists.py) 使用 pint 库合并，可能将 2000 g 换算为 "2 kg"
   - 同时 fluid_ounce 会做上下文推断（与体积单位共存时视为 fl oz，否则视为重量盎司）
   - **边界**：单位换算仅发生在**购物清单后端合并逻辑**中，配方显示体系（页面/打印/复制/API 返回）永远不做换算。

2. **小数/分数模式不影响单位换算决策**：
   - 单位换算的判断基于 pint 的维度分析（质量、体积、长度等），与数值格式（小数 0.5 还是分数 ½）无关
   - 即使前端以分数 ½ cup 显示，后端购物清单合并时仍会解析为 0.5 cup 后参与 pint 换算
   - **边界**：格式化（小数 vs 分数、HTML vs 纯文本）是**展示层策略**，不影响语义层的数值解析和单位换算。

3. **复数本地化与单位换算的独立边界**：
   - 复数判断（"1 cup" vs "2 cups"）基于 `quantity * scale > 1`，由 [locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/78-mealie/mealie/lang/locale_config.py) 的策略控制
   - 单位换算（"1000 g" → "1 kg"）是独立逻辑，发生在购物清单合并后
   - 两者可能冲突：购物清单合并为 "1 kg" 后单位是单数 kg，但如果换算前是 1000 g（复数 grams），最终复数形式取决于换算后的数值
   - **边界**：复数判断在**格式化阶段**基于当前数值做，单位换算在**合并阶段**做，二者独立，可能出现"显示 g 时是复数，显示 kg 时是单数"的情况。

---

## 12. 最终总结：Scale 传播全景与边界地图

```
┌─────────────────────────────────────────────────────────────────┐
│                    RecipePage.vue scale = ref(1)                │
│                    (唯一真实源，浮点乘数，不入库)                   │
└──────────────────────────────────┬──────────────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
         ▼                         ▼                         ▼
  共享当前 scale                  硬编码 scale=1          完全不受 scale 影响
  ────────────────               ────────────────          ────────────────────
  ✅ 配方页面原料列表显示         ❌ 打印预览               ⛔ Nutrition 所有字段
  ✅ 配方页面步骤内联原料         ❌ ingredientToParserString ⛔ 步骤文本（不做替换）
  ✅ 配方页面复制文本             ❌ 步骤原料自动关联匹配     ⛔ 备注/描述/图片
  ✅ RecipeYield 显示            ❌（后端 display 字段默认）  ⛔ 复数策略规则本身
  ✅ 实际打印 RecipePrintContainer                          ⛔ locale 配置
  ✅ 购物清单对话框显示
  ✅ 购物清单后端提交（含子配方递归）
```

**核心一致性原则**：
1. **单一数据源**：所有使用当前 scale 的路径都从 RecipePage.vue 的 `scale = ref(1)` 传递，没有多份 scale 状态
2. **格式化统一**：`parseIngredientText(ingredient, scale, includeFormating)` 是所有前端显示的唯一格式化入口
3. **营养值独立**：Nutrition 在所有路径中均不缩放，始终是原始字符串
4. **单位换算边界**：仅购物清单后端做 pint 换算，其余路径纯数值乘法

**关键不一致风险**：
- ⚠️ **打印预览 vs 实际打印**：预览 scale=1 与实际打印 scale=当前值 可能造成用户对打印结果的误判（尤其 servings/原料/营养值三者的视觉关联）
- ⚠️ **页面显示 vs 购物清单**：页面显示"2000 g"，购物清单合并后变成"2 kg"，复制的文本与购物清单内容不一致
- ⚠️ **前后端分数精度**：前端分母上限 10 vs 后端 32，极端情况下显示略有差异

本分析覆盖了 Mealie v1.x 中 Recipe scaling 与 serving 相关的所有前端显示路径、后端处理逻辑、格式化策略、单位换算边界和营养值处理规则。