# 单位转换与食材规范化代码路径分析

本文档深入分析 Mealie 项目中单位转换、食材规范化、份量缩放和购物清单汇总之间的协作关系。

---

## 一、整体架构总览

系统的核心协作链路如下：

```
原始食材字符串
    │
    ▼
┌─────────────────────┐
│  食材解析 (Parser)   │── brute / nlp / openai 三种解析器
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  数据匹配 (DataMatcher) │── 规范化 + 模糊匹配
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  单位族 & 换算系统    │── Pint + 标准化单位
└─────────┬───────────┘
          │
     ┌────┴────┐
     ▼         ▼
  份量缩放   购物清单汇总
  (前端+后端)  (单位合并 + 食材聚合)
```

涉及的关键文件：

| 模块 | 文件路径 |
|------|----------|
| 单位转换核心 | [unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/parser_utils/unit_utils.py) |
| 数据匹配与规范化 | [_base.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/_base.py) |
| 规范化算法 | [_model_base.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/db/models/_model_base.py#L29-L33) |
| 食材/单位数据库模型 | [ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/db/models/recipe/ingredient.py) |
| Schema 与显示格式化 | [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py) |
| 单位标准化注入 | [repository_units.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/repository_units.py) |
| 购物清单合并服务 | [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py) |
| 前端份量缩放与显示 | [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts) |
| 本地化配置 | [locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/lang/locale_config.py) |
| 单位国际化种子数据 | [en-US.json](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/seed/resources/units/locales/en-US.json) |

---

## 二、文本规范化 (Normalization)

规范化是整个系统的基础，所有食材名称、单位名称、别名等都会通过统一的规范化函数处理，以便匹配和搜索。

### 2.1 规范化算法

规范化函数定义在 [SqlAlchemyBase.normalize](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/db/models/_model_base.py#L29-L33)：

```python
@classmethod
def normalize(cls, val: str) -> str:
    return unidecode(val).translate(_NORMALIZE_PUNCTUATION_TABLE).lower().strip()[:255]
```

规范化步骤（顺序执行）：
1. **Unicode 转写** (`unidecode`)：将非 ASCII 字符转为最接近的 ASCII 形式（如 `café` → `cafe`，`中文` → 拼音或空串）
2. **标点符号替换**：将标点符号转为空格，但保留单引号和双引号（用于搜索的精确匹配）
3. **小写化** (`.lower()`)
4. **去除首尾空白** (`.strip()`)
5. **长度截断**：截断到 255 字符，防止 PostgreSQL 索引过长

### 2.2 规范化字段的自动维护

系统通过 SQLAlchemy 的事件监听器（`@event.listens_for`）自动维护规范化字段，定义在 [ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/db/models/recipe/ingredient.py#L429-L499)：

| 模型 | 原字段 | 自动生成的规范化字段 |
|------|--------|---------------------|
| `IngredientUnitModel` | `name` | `name_normalized` |
| `IngredientUnitModel` | `plural_name` | `plural_name_normalized` |
| `IngredientUnitModel` | `abbreviation` | `abbreviation_normalized` |
| `IngredientUnitModel` | `plural_abbreviation` | `plural_abbreviation_normalized` |
| `IngredientFoodModel` | `name` | `name_normalized` |
| `IngredientFoodModel` | `plural_name` | `plural_name_normalized` |
| `IngredientUnitAliasModel` | `name` | `name_normalized` |
| `IngredientFoodAliasModel` | `name` | `name_normalized` |
| `RecipeIngredientModel` | `note` | `note_normalized` |
| `RecipeIngredientModel` | `original_text` | `original_text_normalized` |

### 2.3 规范化数据迁移

迁移脚本 [c7427796f7b6_more_aggresive_normalization.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/alembic/versions/2026-05-10-18.44.53_c7427796f7b6_more_aggresive_normalization.py) 会对已有数据批量重新执行规范化，确保历史数据也符合最新的规范化规则。

---

## 三、单位族 (Unit Family) 系统

单位族并非用显式的枚举定义，而是通过 **Pint 库的量纲 (dimensionality)** 来判定。

### 3.1 Pint 量纲系统与单位识别边界

核心类是 [UnitConverter](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L20-L104)，它封装了 Pint 的 `UnitRegistry`：

```python
class UnitConverter:
    def __init__(self):
        self.ureg = UnitRegistry()
```

两个单位是否属于同一"单位族"，由 `can_convert` 方法判定，其本质是检查量纲兼容性：

```python
def can_convert(self, unit, to_unit) -> bool:
    unit = self.parse(unit)
    to_unit = self.parse(to_unit)
    if not (isinstance(unit, Unit) and isinstance(to_unit, Unit)):
        return False
    unit, to_unit = self._resolve_ounce(unit, to_unit)
    return unit.is_compatible_with(to_unit)
```

#### 3.1.1 Pint 的三级单位识别

Pint 对单位字符串有三种截然不同的响应，直接决定了后续所有行为：

| 级别 | 特征 | `uc.parse()` 返回值 | `isinstance(x, pint.Unit)` | 示例 |
|------|------|-------------------|---------------------------|------|
| **T1：Pint 原生可识别** | Pint 内置定义的物理/化学单位 | `pint.Unit` 对象 | `True` | `cup`、`pint`、`pound`、`gram`、`milliliter`、`ounce`、`fluid_ounce` |
| **T2：dimensionless（无量纲）** | 合法的 Pint 单位，量纲为 `{}` | `pint.Unit` 对象，`dimensionality == dimensionless` | `True` | 空字符串 `""`、`count`（若已定义） |
| **T3：Pint 完全不认识** | Pint 注册表中不存在该单位 | 原始输入字符串（非 `Unit`） | `False` | `"pinch"`、`"dash"`、`"splash"`、`"serving"`、`"head"`、`"clove"`、`"can"`、`"bunch"`、`"pack"`、`"sprig"` |

#### 3.1.2 dimensionless vs T3（未定义单位）的关键区别

这是两个完全不同的概念：

| 维度 | dimensionless (T2) | 未定义单位 (T3) |
|------|-------------------|----------------|
| Pint 响应 | 返回合法 `pint.Unit`，量纲 `{}` | 抛异常后 `parse()` 回退返回字符串 |
| `can_convert(自身, 自身)` | `True`（与自己兼容） | `False`（返回非 Unit，提前短路） |
| `can_convert(自身, cup)` | `False`（量纲不兼容） | `False`（返回非 Unit，提前短路） |
| `can_convert(自身, pound)` | `False`（量纲不兼容） | `False`（返回非 Unit，提前短路） |

#### 3.1.3 实际单位族映射

根据 Pint 实际识别结果：

| 量纲 (Dimensionality) | 单位族 | 实际 Pint 可识别单位 (T1) |
|----------------------|--------|-------------------------|
| `[length] ** 3` | 体积 (Volume) | cup, pint, quart, gallon, fluid_ounce, milliliter, liter, teaspoon, tablespoon |
| `[mass]` | 质量 (Mass/Weight) | pound, ounce, gram, kilogram, milligram |
| `dimensionless` | 无量纲 | 空字符串（无单位时的隐式默认） |

> ⚠️ **重要更正**：`pinch`、`dash`、`splash`、`serving`、`head`、`clove`、`can`、`bunch`、`pack`、`sprig` 这些单位虽然存在于 Mealie 的种子数据（[en-US.json](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/seed/resources/units/locales/en-US.json)）中，但 **Pint 并不认识它们**，属于 T3 级别。在 [RepositoryUnit._add_standardized_unit](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/repository_units.py#L43-L107) 的 match 语句中，它们落入 `case _: pass` 分支，`standard_quantity` 和 `standard_unit` 均为 `None`。因此它们既不能被 Pint 识别，也没有标准化数据，无法参与任何跨单位换算。

### 3.2 特殊处理：盎司 (Ounce) 的歧义消除

盎司是唯一存在歧义的单位——既可表示重量 (ounce)，又可在食谱语境中表示体积 (fluid_ounce)。系统通过 [`_resolve_ounce`](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L24-L40) 解决：

```python
def _resolve_ounce(self, unit_1: Unit, unit_2: Unit) -> tuple[Unit, Unit]:
    VOLUME = "[length] ** 3"
    if unit_1 == OUNCE and unit_2.dimensionality == VOLUME:
        return FL_OUNCE, unit_2
    if unit_2 == OUNCE and unit_1.dimensionality == VOLUME:
        return unit_1, FL_OUNCE
    return unit_1, unit_2
```

**规则**：当 `ounce` 与体积单位进行换算或合并时，自动将 `ounce` 视为 `fluid_ounce`。

### 3.3 标准化单位 (Standardized Unit)

为了让用户自定义单位也能参与换算，系统为每个单位定义了两个字段（见 [CreateIngredientUnit](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L148-L167)）：

- `standard_quantity: float | None`：标准化系数
- `standard_unit: str | None`：指向 Pint 可识别的标准单位名

标准化类型枚举 `StandardizedUnitType` 定义了支持的标准单位（见 [recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L38-L58)）：

```python
class StandardizedUnitType(StrEnum):
    # Imperial
    FLUID_OUNCE = "fluid_ounce"
    CUP = "cup"
    OUNCE = "ounce"
    POUND = "pound"
    # Metric
    MILLILITER = "milliliter"
    LITER = "liter"
    GRAM = "gram"
    KILOGRAM = "kilogram"
```

### 3.4 标准化单位的自动注入

[RepositoryUnit._add_standardized_unit](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/repository_units.py#L43-L107) 在创建单位时，通过匹配本地化种子文件中的单位名称，自动注入标准化信息：

```python
def _add_standardized_unit(self, data):
    # ...
    for prop in ["name", "plural_name", "abbreviation", "plural_abbreviation"]:
        standardized_unit_key = self.standardized_unit_map.get(val.strip().lower())
        match standardized_unit_key:
            case "teaspoon":
                data["standard_quantity"] = 1 / 6
                data["standard_unit"] = StandardizedUnitType.FLUID_OUNCE
            case "tablespoon":
                data["standard_quantity"] = 1 / 2
                data["standard_unit"] = StandardizedUnitType.FLUID_OUNCE
            case "cup":
                data["standard_quantity"] = 1
                data["standard_unit"] = StandardizedUnitType.CUP
            # ... 更多映射
```

`standardized_unit_map` 构建时会调用 [IngredientUnitsSeeder.get_file](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/seed/seeders.py#L56-L59) 读取种子文件，其回退逻辑为：

```python
@classmethod
def get_file(cls, locale: str | None = None) -> pathlib.Path:
    locale_path = cls.resources / "units" / "locales" / f"{locale}.json"
    return locale_path if locale_path.exists() else units.en_US
```

即：当 locale 为 `None`、或当前 locale 没有对应的种子文件时，**自动回退到英文 `en-US.json`**（默认值见 [units/__init__.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/seed/resources/units/__init__.py)）。因此，匹配表中始终至少包含英文单位名。

每个单位的 `name`、`plural_name`、`abbreviation` 都会被规范化后映射到标准单位 key。

---

## 四、数量换算逻辑

### 4.1 基础单位换算

`UnitConverter.convert` 使用 Pint 直接处理已知单位的换算：

```python
def convert(self, quantity: float, unit, to_unit) -> tuple[float, Unit]:
    unit = self.parse(unit, strict=True)
    to_unit = self.parse(to_unit, strict=True)
    unit, to_unit = self._resolve_ounce(unit, to_unit)
    qty = quantity * unit
    converted = qty.to(to_unit)
    return float(converted.magnitude), converted.units
```

### 4.2 带标准化数据的自定义单位合并

[`merge_quantity_and_unit`](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L107-L146) 是购物清单合并的核心函数，用于合并两个带标准化数据的 Mealie 自定义单位：

```python
def merge_quantity_and_unit[T: CreateIngredientUnit](
    qty_1: float, unit_1: T, qty_2: float, unit_2: T
) -> tuple[float, T]:
    # 1. 校验两个单位都有标准化数据
    if not (unit_1.standard_quantity and unit_1.standard_unit and ...):
        raise ValueError("Both units must contain standardized unit data")

    # 2. 为 Pint 动态注册自定义单位
    #    e.g. "_mealie_unit_1 = 0.5 * fluid_ounce" (1 tablespoon)
    uc.ureg.define(f"{PINT_UNIT_1_TXT} = {unit_1.standard_quantity} * {unit_1_standard}")
    uc.ureg.define(f"{PINT_UNIT_2_TXT} = {unit_2.standard_quantity} * {unit_2_standard}")

    # 3. 合并两个数量
    merged_q, merged_u = uc.merge(qty_1, pint_unit_1, qty_2, pint_unit_2)

    # 4. 智能选择输出单位：
    #    - 如果合并后数量 >= 1，选择较大的单位
    #    - 否则选择较小的单位
    merged_q, merged_u = uc.convert(merged_q, merged_u, max(pint_unit_1, pint_unit_2))
    if abs(merged_q) < 1:
        merged_q, merged_u = uc.convert(merged_q, merged_u, min(pint_unit_1, pint_unit_2))
```

**示例**：
- 1 tablespoon + 1 teaspoon = 4 teaspoons（但因为数量 >= 1，会转为 tablespoon = 1.333... tablespoon）
- 0.125 pint + 0.5 cup = 0.75 cup（数量 < 1，选择较小单位 cup）

---

## 五、食材名称处理与规范化流程

### 5.1 解析阶段

原始字符串如 `"2 1/2 cups diced onions, finely chopped"` 会先被解析器拆分为结构化数据。以 Brute Force 解析器为例（[brute/process.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/brute/process.py)）：

1. `parse_amount`：提取数值（支持整数、小数、分数如 `½` 或 `1/2`、混合分数 `2 1/2`）
2. 提取单位（第 2-3 个 token）
3. `parse_ingredient`：提取食材名称和备注（通过逗号或括号分割）

### 5.2 数据匹配阶段

[DataMatcher](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/_base.py#L21-L138) 类负责将解析出的自由文本与数据库中的食材/单位进行匹配。

#### 5.2.1 别名索引构建

DataMatcher 在首次使用时会构建两个倒排索引：

- `foods_by_alias`：将所有食材的 `name`、`plural_name`、以及每个 `alias.name` 规范化后映射到食材对象
- `units_by_alias`：将所有单位的 `name`、`plural_name`、`abbreviation`、`plural_abbreviation`、以及每个 `alias.name` 规范化后映射到单位对象

#### 5.2.2 匹配算法

[find_match](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/_base.py#L99-L114) 采用两级匹配：

```python
@classmethod
def find_match[T: BaseModel](cls, match_value, *, store_map, fuzzy_match_threshold=0):
    # 第一级：精确匹配（规范化后的值完全相等）
    if match_value in store_map:
        return store_map[match_value]

    # 第二级：模糊匹配（rapidfuzz 比率 >= 阈值）
    fuzz_result = process.extractOne(
        match_value, store_map.keys(), scorer=fuzz.ratio, score_cutoff=fuzzy_match_threshold
    )
    return store_map[fuzz_result[0]] if fuzz_result else None
```

**默认阈值**：
- 食材：`85`（要求较高，避免误匹配）
- 单位：`70`（稍微宽松）
- 购物清单匹配食材：`80`（在购物清单中使用）

#### 5.2.3 解析错误修复

[find_ingredient_match](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/_base.py#L176-L202) 有一个智能回退逻辑：当解析器把一个整体食材名错误地拆成"单位+食材"（且两者都匹配失败），会尝试把"单位名+食材名"拼接后重新匹配食材。

---

## 六、份量缩放 (Scaling)

份量缩放在前端和后端都有实现，分别负责显示和数据处理。

### 6.1 前端缩放

定义在 [use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts) 的 `useParsedIngredientText` 函数。

缩放规则：
- **数值计算**：`scaledQuantity = quantity * scale`
- **显示格式选择**：
  - 若 `unit.fraction === false`（十进制单位如克、毫升）：使用 `toPrecision(3)` 保留 3 位有效数字
  - 若 `unit.fraction === true`（分数单位如杯、勺）：转换为分数显示（上标/下标 HTML 格式），最大分母 10
- **极小值处理**：缩放后数量小于 `10^{-3}`（十进制）或 `1/10`（分数）时，显示为 `< 最小值`

### 6.2 后端缩放（购物清单场景）

在 [ShoppingListService.get_shopping_list_items_from_recipe](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L323-L411) 中，每个食材生成购物清单项时：

```python
new_item = ShoppingListItemCreate(
    quantity=ingredient.quantity * scale if ingredient.quantity else 0,
    recipe_references=[
        ShoppingListItemRecipeRefCreate(
            recipe_id=recipe_id,
            recipe_quantity=ingredient.quantity,   # 单份原始量（未缩放）
            recipe_scale=scale,                    # 份数/缩放因子
            recipe_note=ingredient.note or None,
        )
    ],
)
```

核心字段关系：
- `quantity` = **缩放后** 的实际数量（= `ingredient.quantity * scale`）
- `recipe_quantity` = 单份食谱中的原始量（不随缩放改变）
- `recipe_scale` = 该食谱被添加的份数（缩放因子）
- 子食谱（referenced_recipe）会递归处理，缩放因子相乘：`sub_scale = ingredient.quantity * scale`

#### 6.2.1 同食谱内重复食材的数量累加（误差来源）

当同一食谱中多次出现相同食材（如两个条目都是 "cup flour"），在 `get_shopping_list_items_from_recipe` 内部会提前合并（[shopping_lists.py L387-L406](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L387-L406)）：

```python
# since this is the same recipe, we combine the quanities, rather than the scales
# all items will have exactly one recipe reference
if ingredient.quantity:
    existing_item.quantity += ingredient.quantity
    existing_item.recipe_references[0].recipe_quantity += ingredient.quantity
```

注意关键细节：第一个条目的 `quantity` 被正确设置为 `ingredient.quantity * scale`（已缩放），但合并时累加的是 **未缩放** 的 `ingredient.quantity`，而不是 `new_item.quantity`。

**误差产生推演**（以 scale=3、食谱内有 "1 cup flour" 和 "2 cup flour" 为例）：

| 步骤 | 操作 | `quantity` | `recipe_quantity` | `recipe_scale` | 期望值 `(recipe_quantity * recipe_scale)` |
|------|------|-----------|-------------------|---------------|-----------------------------------------|
| 处理第 1 条 "1 cup flour" | 新建条目 | `1 * 3 = 3` | `1` | `3` | 3 |
| 处理第 2 条 "2 cup flour" | 合并累加 | `3 + 2 = 5` ❌ | `1 + 2 = 3` ✅ | `3` | 9 |
| **误差** | | **少了 4** | — | — | |

此时：
- `quantity = 5`，但 `recipe_quantity * recipe_scale = 3 * 3 = 9`
- 两者已经不一致。后续所有使用 `quantity` 的地方都偏小。

#### 6.2.2 场景一：减少食谱份数（remove_recipe_ingredients_from_list）

减少份数的逻辑在 [shopping_lists.py L457-L539](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L457-L539)，核心代码：

```python
if ref.recipe_scale > recipe_decrement:
    # remove only part of the reference
    item.quantity -= recipe_decrement * ref.recipe_quantity
else:
    # remove everything that's left on the reference
    item.quantity -= ref.recipe_scale * ref.recipe_quantity

ref.recipe_scale -= recipe_decrement
```

注意：扣除数量时使用的是 **正确公式** `recipe_decrement * ref.recipe_quantity`（或 `recipe_scale * recipe_quantity`），这个公式假设了 `quantity == recipe_quantity * recipe_scale`。但由于同食谱累加时的误差，这个等式不成立。

**延续上例，逐步减少份数**：

| 操作 | 扣除量计算 | `quantity` 变化 | `recipe_scale` 变化 | 实际剩余 `quantity` | 应有剩余 `recipe_quantity * recipe_scale` |
|------|----------|----------------|--------------------|-------------------|-----------------------------------------|
| 初始状态 | — | 5 | 3 | 5 | 9 |
| 减少 1 份 (`recipe_decrement=1`) | `1 * 3 = 3` | `5 - 3 = 2` | `3 - 1 = 2` | 2 | `3 * 2 = 6` |
| 再减少 1 份 | `1 * 3 = 3` | `2 - 3 = -1` ❌ | `2 - 1 = 1` | -1 | `3 * 1 = 3` |
| 再减少 1 份（全部移除） | `1 * 3 = 3`（走 else 分支） | `-1 - 3 = -4` | `1 - 1 = 0`（引用被删） | -4 | 0（无引用） |

**误差影响**：
- 第一次减份后，`quantity` 从 **偏小 4** 变成 **偏小 4**（2 vs 6），差距仍然是 4
- 第二次减份时，`quantity` 已经变成 **负数**（-1），但 `recipe_scale` 仍为 1，理论上还应该有 3 cup flour
- 第三次减份后，`recipe_scale` 归零、引用被删除，但 `quantity` 为 -4，由于 `quantity < 0`，条目会被 `delete_items` 删除（[L506](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L506)）——虽然最终会删除，但 `quantity` 的值完全失真
- 如果该条目同时还有其他食谱的引用（见 6.2.4），负数 `quantity` 会继续污染跨食谱合并结果

#### 6.2.3 场景二：引用被删除

当 `recipe_scale` 被减到 0 或以下时，该食谱引用会从 `recipe_references` 列表中移除：

```python
ref.recipe_scale -= recipe_decrement
if ref.recipe_scale <= 0:
    item.recipe_references.remove(ref)
```

条目是否被删除的判断逻辑（[L505-L507](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L505-L507)）：

```python
# only remove a 0 quantity item if we removed its last recipe reference
if item.quantity < 0 or (item.quantity == 0 and not item.recipe_references):
    delete_items.append(item.id)
```

**删除判定矩阵**：

| `quantity` | 是否还有其他引用 | 结果 |
|-----------|----------------|------|
| `< 0` | 无论有无 | **被删除**（即使还有其他食谱引用！） |
| `== 0` | 无 | **被删除** |
| `== 0` | 有 | 保留（还有其他食谱） |
| `> 0` | 无论有无 | 保留 |

**由累加误差引发的异常**：
- 在上例中，减到第 2 份时 `quantity = -1`（但 `recipe_scale = 1`，还有引用）
- `quantity < 0` 直接触发删除，导致该购物清单项连同**可能存在的其他食谱引用**一起被误删
- 如果只有这一个食谱引用，最终虽然删除了条目，但过程中 `quantity` 已经过多次负值

#### 6.2.4 场景三：跨食谱合并

当存在同食谱累加误差的条目与另一个食谱的相同食材在购物清单中合并时，误差通过 `merge_items` 传播。

**推演**：已有条目 A（来自食谱 R1，有累加误差：`quantity=5, recipe_quantity=3, recipe_scale=3`，"cup flour"），再添加食谱 R2 的 "2 cup flour"（scale=2）：

1. R2 的新条目 B 被创建：`quantity = 2 * 2 = 4`, `recipe_quantity = 2`, `recipe_scale = 2`
2. `bulk_create_items` 中 A 与 B 调用 `merge_items`（[L191-L201](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L191-L201)）
3. `merge_items` 对数量的处理（[L86-L96](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L86-L96)）：

```python
if to_item_unit and to_item_unit.standard_unit and from_item_unit and from_item_unit.standard_unit:
    merged_qty, merged_unit = merge_quantity_and_unit(
        from_item.quantity or 0, from_item_unit, to_item.quantity or 0, to_item_unit
    )
    to_item.quantity = merged_qty
else:
    # No conversion needed, just sum the quantities
    to_item.quantity += from_item.quantity
```

由于 cup 是 T1（有 standard_unit），走 `merge_quantity_and_unit` 分支：

| 项目 | `quantity` | 对合并结果的贡献 |
|------|-----------|----------------|
| A（有误差） | 5（应为 9） | 5 |
| B（无误差） | 4 | 4 |
| **合并结果** | **5 + 4 = 9** | **应为 9 + 4 = 13** |

食谱引用的合并（[L109-L127](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L109-L127)）：
- 不同 recipe_id 的引用各自保留
- 因此合并后：`recipe_references` 包含 R1 (`recipe_quantity=3, recipe_scale=3`) 和 R2 (`recipe_quantity=2, recipe_scale=2`)
- 引用合计理论量：`3*3 + 2*2 = 13`，但 `quantity = 9`，**差距为 4**（与原误差一致）

**结论**：跨食谱合并时，误差被**原样继承**到合并后的条目中，不会放大也不会缩小。后续对 R1 或 R2 的份数增减都会继续以错误的 `quantity` 为基础，而扣除时仍使用正确的 `recipe_decrement * recipe_quantity` 公式，使 `quantity` 越来越偏离真实值。

备注合并逻辑：用 `" | "` 分隔符连接去重后的备注集合。

### 6.3 复数形式决策

单位和食材的单复数显示由以下规则决定：

**单位复数**（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L258-L276)）：
```python
use_plural = self.quantity and self.quantity > 1
```

**食材复数**（[recipe_ingredient.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L278-L299)）：
取决于 locale 的 `plural_food_handling` 配置（[locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/lang/locale_config.py#L10-L13)）：

| 策略 | 含义 | 适用语言示例 |
|------|------|-------------|
| `ALWAYS` | 数量 > 1 时使用复数 | 法语、西班牙语、德语等 |
| `WITHOUT_UNIT` | 数量 > 1 **且没有单位**时使用复数 | 英语 (en-US, en-GB) |
| `NEVER` | 始终使用单数 | 中文、日语、韩语、越南语、土耳其语 |

---

## 七、本地化 (Localization) 影响

### 7.1 单位种子数据的本地化

[IngredientUnitsSeeder](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/repos/seed/seeders.py#L55-L88) 会根据当前 locale 从对应 JSON 文件加载单位名称。例如英语单位名：

```json
// en-US.json
{
  "teaspoon": { "name": "teaspoon", "plural_name": "teaspoons", "abbreviation": "tsp" },
  "tablespoon": { "name": "tablespoon", "plural_name": "tablespoons", "abbreviation": "tbsp" },
  "cup": { "name": "cup", "plural_name": "cups", "abbreviation": "c" },
  // ...
}
```

这些本地化名通过 `RepositoryUnit.standardized_unit_map` 反查，将用户输入的本地单位名映射到标准单位 key，从而注入正确的 `standard_quantity` 和 `standard_unit`。

### 7.2 对标准化映射的影响

由于 `standardized_unit_map` 的构建依赖当前 locale 的种子文件（不存在时回退英文），实际影响分三种情况：

| 场景 | 标准化注入结果 |
|------|--------------|
| **当前 locale 有种子文件，且用户输入名匹配** | ✅ 正确注入。例如德语 locale 下输入 "Teelöffel" → 匹配 `standard_quantity=1/6, standard_unit=fluid_ounce` |
| **当前 locale 无种子文件（回退英文），用户输入英文名匹配** | ✅ 仍能注入。例如任何 locale 下输入 "teaspoon" 都能匹配到英文种子数据 |
| **用户输入了自定义/本地语言名，且在种子（含回退英文）中无匹配** | ❌ 无法自动注入，`standard_quantity` 和 `standard_unit` 均为 `None`，该单位将无法参与跨单位换算。例如德语 locale 无种子文件时，输入 "Teelöffel" 无法匹配 |

**注**：即便自动注入失败，用户仍可在单位管理界面手动填写 `standard_quantity` 和 `standard_unit`，手动配置后即可正常参与换算。

---

## 八、未知单位与边界情况的影响

结合第 3.1 节的三级单位分类，"未知单位"在 Mealie 中实际对应 **T2（dimensionless）** 和 **T3（Pint 不认识且无标准化数据）** 两种情况：

| 分类 | 典型代表 | Pint 识别 | `standard_unit` | 数据库中存在 |
|------|---------|----------|----------------|-------------|
| T1（已知） | cup, gram, pound | ✅ `pint.Unit` | ✅ 有 | ✅ |
| T2（dimensionless） | 空字符串 / 无单位 | ✅ `pint.Unit`（量纲`{}`） | ❌ 无 | ✅（作为隐式状态） |
| T3-A（种子但无换算） | pinch, dash, splash, serving, head, clove, can, bunch, pack, sprig | ❌ 返回字符串 | ❌ 无 | ✅（来自种子数据） |
| T3-B（真正自定义） | 用户创建的 "handful"、"sprinkle" 等 | ❌ 返回字符串 | ❌ 无（除非手动配置） | ✅（用户创建） |

### 8.1 单位转换行为

- `UnitConverter.can_convert()`：
  - T1 + T1 且量纲兼容 → `True`
  - T1 + T2 → `False`（无量纲与体积/质量不兼容）
  - T2 + T2 → `True`（dimensionless 与自己兼容）
  - 任何含 T3 → `False`（T3 返回字符串，非 `pint.Unit`，在 [`can_convert` L72](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/parser_services/parser_utils/unit_utils.py#L72) 被直接短路返回 False）
- `UnitConverter.convert()` / `merge()`：任一单位为 T3 时抛出 `UnitNotFound` 异常
- `merge_quantity_and_unit()`：任一单位缺少 `standard_quantity` 或 `standard_unit` 时抛出 `ValueError`

### 8.2 对显示 (Display) 的影响

T2（无单位/dimensionless）与 T3（不认识）在前后端的显示行为：

**后端 Schema 层**（[RecipeIngredientBase._format_*](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L228-L323)）：
- **数量格式**：`CreateIngredientUnit.fraction` 默认为 `True`，因此 T3 类单位（pinch、dash 等种子单位及用户自定义单位）的数量默认按**分数**格式显示。若单位对象不存在（T2 无单位），则走无单位分支，仍按分数格式化
- **单位名称**：
  - T3 类单位（有单位对象）：直接使用 `name` / `plural_name` / `abbreviation`，是否缩写取决于 `use_abbreviation`（默认 `False`）
  - T2（无单位，`unit_id is None`）：单位名部分完全不显示（见 [_format_display L298](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/schema/recipe/recipe_ingredient.py#L298-L323)，`use_unit` 为 `False` 时 unit 传空字符串）
- **单复数**：
  - T3：`quantity > 1` 时使用复数形式，无 `plural_name` 则回退 `name`
  - T2：不涉及

**前端显示层**（[use-recipe-ingredients.ts](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L82-L143)）：
- 同样按 `unit.fraction` 决定分数/十进制显示（分数分母最大 10，十进制 3 位有效数字）
- 单位名仅在 `unitName && quantity` 同时为真时显示（[L131](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/frontend/app/composables/recipes/use-recipe-ingredients.ts#L131)），因此 T2（无 unit 对象）和零数量都不显示单位名
- `ingredientToParserString` 中若 `!parsed.unit && !parsed.food` 会直接返回 `note`

### 8.3 对购物清单汇总 (Aggregation) 的影响

[ShoppingListService.can_merge](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L45-L71) 中对单位的检查：

```python
if item1.unit_id != item2.unit_id:
    # 如果单位不同，两个单位都必须有 standard_unit 且可换算
    if not (item1_unit and item1_unit.standard_unit):
        return False
    if not (item2_unit and item2_unit.standard_unit):
        return False
    uc = UnitConverter()
    if not uc.can_convert(item1_unit.standard_unit, item2_unit.standard_unit):
        return False
```

各分类的实际汇总结果对照：

| 场景 | 能否合并 | 原因 | 结果 |
|------|---------|------|------|
| 相同 `unit_id`（无论 T1/T3-A/T3-B） | ✅ 可以 | `unit_id == unit_id` 跳过单位检查 | 数量直接相加，单位不变 |
| 两个不同的 T1 单位，量纲兼容（如 cup + pint） | ✅ 可以 | 都有 `standard_unit`，且 Pint 可换算 | 调用 `merge_quantity_and_unit`，换算后智能合并 |
| 两个不同的 T1 单位，量纲不兼容（cup + pound） | ❌ 不行 | `can_convert` 返回 False | 相同食材显示为两行 |
| T1 + T3（任意子类型），不同 `unit_id` | ❌ 不行 | T3 无 `standard_unit`，在 L61-L64 被短路 | 相同食材显示为两行 |
| T3-A + T3-A，不同 `unit_id`（如 pinch + dash） | ❌ 不行 | 都无 `standard_unit` | 相同食材显示为两行 |
| T3-A + T3-B，不同 `unit_id`（如 pinch + handful） | ❌ 不行 | 都无 `standard_unit` | 相同食材显示为两行 |
| T2（无单位，`unit_id=None`）+ T2，相同备注 | ✅ 可以 | 走备注完全相同分支 | 数量直接相加 |
| T2（无单位，`unit_id=None`）+ T2，不同备注 | ❌ 不行 | 备注不同 | 相同食材显示为两行 |

**典型案例**：一个食谱含有 "1 pinch salt" 和 "2 dashes salt"。两者 `food_id` 相同（盐），但 `unit_id` 不同（pinch vs dash），且两者都是 T3-A（无 `standard_unit`）。加入购物清单后会显示为两条独立记录，无法自动合并。

---

## 九、购物清单汇总 (Shopping List Aggregation)

### 9.1 合并判定逻辑

两个购物清单项可以合并的条件（[can_merge](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L45-L71)）：

1. 两个条目都未勾选 (`checked == false`)
2. 食材 ID 相同 (`food_id == food_id`)，或者（无食材 ID 时备注完全相同）
3. 单位 ID 相同，或者（单位 ID 不同但两个单位都有标准化数据且量纲兼容）

### 9.2 合并执行逻辑

[merge_items](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L73-L128)：

1. **数量合并**：
   - 若两个单位都有标准化数据：调用 `merge_quantity_and_unit` 进行单位换算后智能合并
   - 否则：直接相加数量（此时要求单位 ID 相同）
2. **备注合并**：用 `" | "` 连接去重后的备注
3. **食谱引用合并**：相同 recipe_id 的引用合并 `recipe_scale`（份数相加）

### 9.3 批量创建/更新流程

以 `bulk_create_items` 为例，分三步：

1. **输入项内合并**：先在待创建的列表内部两两尝试合并
2. **与已有项合并**：遍历购物清单中已存在的未勾选项，尝试合并到已有项
3. **创建剩余项**：未被合并的项作为新条目创建，同时自动查找标签

### 9.4 同食谱内食材去重

在从食谱生成购物清单项时（[get_shopping_list_items_from_recipe](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L387-L406)），同一食谱内重复的食材会在生成阶段就合并，而不是等到后续的批量创建阶段。详细累加逻辑见 [6.2.1 节](#621-同食谱内重复食材的数量累加)。

### 9.5 跨食谱（不同 recipe_id）合并行为

当**不同食谱**的相同食材被添加到同一购物清单时（经过 `bulk_create_items` → `merge_items`），合并行为与同食谱内不同：

1. **数量合并**：若单位有标准化数据，调用 `merge_quantity_and_unit` 做单位换算后合并；否则直接相加
2. **食谱引用合并**：不同 `recipe_id` 的引用各自保留，相同 `recipe_id` 的引用累加 `recipe_scale`

```python
# merge_items 中对 recipe_references 的处理
updated_refs = {ref.recipe_id: ref for ref in from_item.recipe_references}
for to_ref in to_item.recipe_references:
    if to_ref.recipe_id not in updated_refs:
        updated_refs[to_ref.recipe_id] = to_ref
        continue

    # merge recipe scales
    base_ref = updated_refs[to_ref.recipe_id]
    if base_ref.recipe_scale is None:
        base_ref.recipe_scale = 1
    if to_ref.recipe_scale is None:
        to_ref.recipe_scale = 1
    base_ref.recipe_scale += to_ref.recipe_scale
```

此时 `quantity` 是两个条目的缩放后数量直接合并，而 `recipe_scale` 按 recipe_id 分别累加。关于同食谱累加误差如何在跨食谱合并中传播的详细分析见 [6.2.4 节](#624-场景三跨食谱合并)。

---

## 十、端到端协作示例

以 `"2 cups flour"` + `"1 pint flour"` 加入购物清单为例，追踪完整链路：

1. **解析阶段**：Brute Parser 解析为 `{amount: 2, unit: "cups", food: "flour"}` 和 `{amount: 1, unit: "pint", food: "flour"}`
2. **规范化匹配**：
   - `"cups"` 规范化为 `"cups"` → 匹配单位 ID `cup`，`standard_quantity=1, standard_unit="cup"`
   - `"flour"` 规范化为 `"flour"` → 匹配食材 ID
3. **购物清单合并判定**：
   - `food_id` 相同 ✓
   - 单位不同但量纲兼容（都是体积）✓
4. **数量合并**（调用 `merge_quantity_and_unit`）：
   - `2 cup + 1 pint = 2 cup + 2 cup = 4 cup`
   - 数量 >= 1，选择较大单位 → `2 pint`
5. **结果**：购物清单中出现一条 `"2 pints flour"`

---

## 十一、关键约束与潜在风险

| 场景 | 限制 / 行为 | 影响 |
|------|------------|------|
| **T3 级单位（pinch/dash/splash/serving 等）** | Pint 不认识，无 `standard_unit`，种子数据中落入 `case _: pass` | 不同 T3 单位间**无法跨单位换算/合并**；同食材但不同 T3 单位（如 pinch vs dash）在购物清单中显示为多行；只有相同 `unit_id` 才能合并 |
| **T2 dimensionless vs T3 未定义单位** | T2 是合法的 `pint.Unit`（量纲 `{}`），T3 是非 Unit 字符串 | T2 与自己兼容（`can_convert(T2, T2)=True`），但 T3 与任何单位（包括自己）都不兼容（非 Unit 提前短路返回 False） |
| **T3 单位显示** | `fraction` 默认为 `True`，`use_abbreviation` 默认为 `False` | 数量默认以分数格式显示，单位默认使用全名而非缩写 |
| **盎司歧义消除** | 仅在与体积单位合用时才自动转液盎司 | 纯盎司合并（如 8 oz + 1 lb）按重量处理；单独的盎司条目保持不变 |
| **模糊匹配阈值** | 食材 85 / 单位 70 / 购物清单标签匹配 80 | 拼写差异过大可能匹配失败，需要手动纠正或添加别名 |
| **非 ASCII 字符** | 规范化时使用 unidecode 转写 | 中文等表意文字转写后可能完全丢失语义，匹配严重依赖于用户创建的别名 |
| **本地化种子缺失** | 缺失 locale 文件时自动回退英文 `en-US.json` | 英文单位名始终可匹配标准化；纯本地语言名在无对应 locale 种子时无法自动注入标准化 |
| **同食谱重复食材累加误差** | 累加的是未缩放的 `ingredient.quantity` 而非已缩放的 `new_item.quantity` | `quantity` 字段值偏小，但 `recipe_quantity` 被正确累计 |
| **减少份数时误差延续** | 扣除使用正确公式 `recipe_decrement * recipe_quantity`，但基数 `quantity` 已有误差 | `quantity` 可能提前变负；减到 0 之前条目就可能因 `quantity < 0` 被误删（连同其他食谱引用一起） |
| **引用删除判定** | `quantity < 0` 时无条件删除 | 有累加误差的条目在减份时可能提前触发此条件，导致仍有有效引用时条目被删除 |
| **跨食谱合并误差传播** | 数量直接按 `quantity` 字段合并，误差原样继承 | 误差不会放大也不会缩小，但后续对任一食谱的份数增减会让 `quantity` 进一步偏离 |
| **分数显示精度** | 最大分母 32（后端 `limit_denominator`）/ 10（前端 `frac`） | 某些精确小数无法以分数准确表示，会有舍入误差 |
| **已勾选条目** | `checked=true` 的条目永不参与合并 | 购物清单中手动勾选过的相同食材会保持为独立行 |
