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

### 3.1 Pint 量纲系统

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

常见单位族及示例：

| 量纲 (Dimensionality) | 单位族 | 示例单位 |
|----------------------|--------|---------|
| `[length] ** 3` | 体积 (Volume) | cup, pint, quart, gallon, fluid_ounce, milliliter, liter |
| `[mass]` | 质量 (Mass/Weight) | pound, ounce, gram, kilogram, milligram |
| `dimensionless` | 计数/无单位 | serving, pinch, dash, splash, head, clove, can |

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

`standardized_unit_map` 构建时会读取当前 locale 的单位种子文件，将每个单位的 `name`、`plural_name`、`abbreviation` 都映射到标准单位 key。

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

在 [ShoppingListService.get_shopping_list_items_from_recipe](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L323-L411) 中：

```python
new_item = ShoppingListItemCreate(
    quantity=ingredient.quantity * scale if ingredient.quantity else 0,
    recipe_references=[
        ShoppingListItemRecipeRefCreate(
            recipe_id=recipe_id,
            recipe_quantity=ingredient.quantity,   # 原始单份量
            recipe_scale=scale,                    # 份数
            recipe_note=ingredient.note or None,
        )
    ],
)
```

关键点：
1. `quantity` 存储的是 **缩放后** 的实际数量
2. 同时保留 `recipe_quantity`（单份原始量）和 `recipe_scale`（份数），方便后续按份数增减
3. 子食谱（referenced_recipe）会递归处理，缩放因子相乘：`sub_scale = ingredient.quantity * scale`

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

由于 `standardized_unit_map` 的构建依赖当前 locale 的种子文件：
- 如果用户输入的是德语 "Teelöffel"（茶匙），系统能正确匹配并注入 `standard_quantity=1/6, standard_unit=fluid_ounce`
- 如果用户使用的 locale 没有对应的种子文件（或用了自定义单位名），则无法自动注入标准化数据，该单位将无法参与跨单位换算

---

## 八、未知单位 (Unknown Units) 的影响

当一个单位无法被 Pint 识别（`uc.parse()` 返回原始字符串而非 `pint.Unit`），或者没有标准化数据时：

### 8.1 单位转换

- `UnitConverter.can_convert()` 返回 `False`
- `UnitConverter.convert()` 和 `merge()` 抛出 `UnitNotFound` 异常
- `merge_quantity_and_unit()` 抛出 `ValueError: "Both units must contain standardized unit data"`

### 8.2 购物清单合并

[ShoppingListService.can_merge](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L45-L71) 中的检查：

```python
def can_merge(self, item1, item2) -> bool:
    # ... food_id 必须相同，且都未勾选
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

**结论**：未知单位（无 `standard_unit`）**只能与完全相同 `unit_id` 的条目合并**。如果两个相同食材的条目使用了不同的未知单位，则无法合并，会在购物清单中显示为两行。

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

在从食谱生成购物清单项时（[get_shopping_list_items_from_recipe](file:///d:/fz/0601/solo-dogfeeding/code/74-mealie/mealie/services/household_services/shopping_lists.py#L387-L406)），同一食谱内重复的食材会在生成阶段就合并，而不是等到后续的批量创建阶段。

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

| 场景 | 限制 | 影响 |
|------|------|------|
| 未知/自定义单位 | 无 `standard_unit` 则无法跨单位换算 | 购物清单中相同食材不同单位会显示多行 |
| 盎司歧义 | 仅在与体积单位合用时才自动转液盎司 | 纯盎司合并（如 8 oz + 1 lb）按重量处理 |
| 模糊匹配阈值 | 食材 85/单位 70/购物清单 80 | 拼写差异过大可能匹配失败 |
| 非 ASCII 字符 | 规范化时使用 unidecode 转写 | 某些语言（如中文）转写后可能完全丢失语义，匹配依赖于别名 |
| 本地化种子 | 部分 locale 可能没有单位种子数据 | 无法自动注入标准化信息，用户需手动配置 |
| 分数显示 | 最大分母 32（后端）/ 10（前端） | 某些精确小数无法以分数准确表示 |
