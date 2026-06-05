# 采购清单聚合规则详解

本文档详细解析 Mealie 采购清单（Shopping List）的聚合规则，包括来源汇总、去重合并、数量换算和状态变更四个核心部分。

---

## 一、来源汇总

采购清单项的来源主要有两个途径：**从食谱添加** 和 **手动创建**。其中从食谱添加是最复杂的聚合场景。

### 1.1 食谱食材提取流程

核心逻辑位于 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/services/household_services/shopping_lists.py) 的 `get_shopping_list_items_from_recipe` 方法（L323-L411）。

**处理步骤：**

1. **递归处理子食谱**：如果食材引用了另一个食谱（`referenced_recipe`），则递归处理子食谱的食材，缩放比例为 `(ingredient.quantity or 1) * scale`

   ```python
   if isinstance(ingredient.referenced_recipe, Recipe):
       sub_recipe = ingredient.referenced_recipe
       sub_scale = (ingredient.quantity or 1) * scale
       sub_items = self.get_shopping_list_items_from_recipe(
           list_id, sub_recipe.id, sub_scale, sub_recipe.recipe_ingredient
       )
       list_items.extend(sub_items)
       continue
   ```

2. **提取食材属性**：为每个食材创建 `ShoppingListItemCreate` 对象，包含：
   - `quantity`：原数量 × 缩放比例（`ingredient.quantity * scale`）
   - `food_id` / `label_id`：从食材对象提取
   - `unit_id`：从食材对象提取
   - `recipe_references`：包含 `recipe_id`、`recipe_quantity`（单倍配方数量）、`recipe_scale`（添加次数倍数）

3. **同一食谱内合并**：在提取过程中，如果同一食谱内出现相同食材（可合并），直接合并数量和备注

   ```python
   for existing_item in list_items:
       if not self.can_merge(existing_item, new_item):
           continue
       # 同一食谱内：直接累加数量，而非累加 scale
       if ingredient.quantity:
           existing_item.quantity += ingredient.quantity
           existing_item.recipe_references[0].recipe_quantity += ingredient.quantity
       merged = True
       break
   ```

### 1.2 多食谱批量添加

`add_recipe_ingredients_to_list` 方法（L413-L455）支持批量添加多个食谱：

1. 遍历所有食谱，调用 `get_shopping_list_items_from_recipe` 生成待创建项
2. 调用 `bulk_create_items` 进行批量创建与合并
3. 更新清单级别的 `recipe_references`，累加同一食谱的添加次数

---

## 二、去重合并规则

去重合并是采购清单最核心的逻辑，由 `can_merge`（判断可否合并）和 `merge_items`（执行合并）两个方法配合完成。

### 2.1 可合并条件判断

`can_merge` 方法位于 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/services/household_services/shopping_lists.py#L45-L71)。

**不可合并的前置条件（满足任一即不可合并）：**

| 条件 | 说明 |
|------|------|
| `item1.checked == true` | 已勾选的项不参与合并 |
| `item2.checked == true` | 已勾选的项不参与合并 |
| `item1.food_id != item2.food_id` | 食物 ID 不同 |

**单位兼容性检查：**

如果 `unit_id` 不同，需要检查单位是否可转换：
- 两个单位都必须有 `standard_unit`（标准单位）
- 通过 `UnitConverter.can_convert()` 判断标准单位是否兼容（相同量纲）

**最终判断：**

```python
return bool(item1.food_id) or item1.note == item2.note
```

即：
- 如果有 `food_id`（且前面的检查都通过），则可合并
- 如果没有 `food_id`，则 `note` 必须完全相同才可合并

### 2.2 合并执行流程

`merge_items` 方法位于 [shopping_lists.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/services/household_services/shopping_lists.py#L73-L128)。

**合并优先级：** `to_item`（已有项）的属性优先于 `from_item`（新项）。

**合并内容：**

1. **数量与单位合并**：
   - 如果两个单位都有标准单位，调用 `merge_quantity_and_unit()` 进行智能换算合并
   - 否则直接累加数量（`to_item.quantity += from_item.quantity`）

2. **备注合并**：用 ` | ` 分隔符合并不同的备注，自动去重

   ```python
   notes: set[str] = set(to_item.note.split(" | ")) if to_item.note else set()
   notes.add(from_item.note)
   to_item.note = " | ".join([note for note in notes if note])
   ```

3. **Extras 合并**：字典合并，`from_item` 的键覆盖 `to_item` 的同名键

4. **食谱引用合并**：
   - 按 `recipe_id` 去重
   - 同一食谱多次添加时，累加 `recipe_scale`（添加倍数）

   ```python
   if base_ref.recipe_scale is None:
       base_ref.recipe_scale = 1  # 向后兼容
   if to_ref.recipe_scale is None:
       to_ref.recipe_scale = 1
   base_ref.recipe_scale += to_ref.recipe_scale
   ```

### 2.3 批量操作中的合并时机

合并发生在三个层级：

| 层级 | 发生时机 | 涉及方法 |
|------|----------|----------|
| 1 | 输入批次内部合并 | `bulk_create_items` L162-L177、`bulk_update_items` L226-L252 |
| 2 | 与数据库现有未勾选项合并 | `bulk_create_items` L180-L203、`bulk_update_items` L254-L282 |
| 3 | 同一食谱内预合并 | `get_shopping_list_items_from_recipe` L387-L407 |

---

## 三、数量换算逻辑

数量换算使用 [unit_utils.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/services/parser_services/parser_utils/unit_utils.py) 中的 `UnitConverter` 类和 `merge_quantity_and_unit` 函数。

### 3.1 单位转换基础

`UnitConverter` 基于 `pint` 库实现单位转换：

- **`can_convert(unit1, unit2)`**：判断两个单位是否可转换（相同量纲）
- **`convert(quantity, from_unit, to_unit)`**：执行单位转换
- **`merge(quantity1, unit1, quantity2, unit2)`**：合并两个数量

### 3.2 特殊处理：盎司歧义

`_resolve_ounce` 方法（L24-L40）解决盎司（ounce）的歧义问题：

> 食谱中常常用 "ounce" 指代 "fluid ounce"（液盎司）。当盎司与体积单位一起使用时，自动假定为液盎司。

```python
if unit_1 == OUNCE and unit_2.dimensionality == VOLUME:
    return FL_OUNCE, unit_2
```

### 3.3 智能单位选择

`merge_quantity_and_unit` 函数（L107-L146）在合并后会智能选择最合适的显示单位：

**算法：**

1. 将两个自定义单位（Mealie 定义的单位）转换为标准单位
2. 创建临时的 pint 单位定义进行合并运算
3. 先转换为**较大的单位**
4. 如果转换后数量 `< 1`，则改用**较小的单位**

**示例：**

```
合并 0.125 pint + 0.5 cup:
= 0.25 cup + 0.5 cup
= 0.75 cup
→ 0.75 < 1，所以用较小单位 cup 显示
```

```
合并 2 pint + 4 cup:
= 4 cups + 4 cups
= 8 cups = 4 pint
→ 4 >= 1，所以用较大单位 pint 显示
```

### 3.4 数量为负的处理

在 `bulk_create_items` L203 和 `bulk_update_items` L285 中：

```python
if merged or create_item.quantity < 0:
    continue  # 不创建，数量为负的项被丢弃
```

```python
if update_item.quantity < 0:
    delete_items.add(update_item.id)  # 标记为删除
    continue
```

---

## 四、状态变更逻辑

### 4.1 Checked 状态的影响

`checked` 是采购清单项的核心状态，对聚合规则有决定性影响：

**规则：**

1. **已勾选项不参与合并**：`can_merge` 方法首先检查 `item1.checked` 和 `item2.checked`，任一为 `true` 则不可合并
2. **已勾选项不参与匹配**：批量操作查询现有项时，只查询 `checked=false` 的项

   ```python
   query_filter = f"shopping_list_id={list_id} AND checked=false"
   ```

3. **勾选时清理食谱引用**：设置 `checked=true` 时，清空 `recipe_references`

   ```python
   if create_item.checked:
       create_item.recipe_references = []
   ```

### 4.2 前端状态管理

前端状态位于 [use-shopping-list-state.ts](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/frontend/app/composables/shopping-list-page/sub-composables/use-shopping-list-state.ts)。

**列表分组：**

```typescript
const listItems = reactive({
  unchecked: [] as ShoppingListItemOut[],
  checked: [] as ShoppingListItemOut[],
});
```

**已勾选项排序规则**（L31-L36）：
1. 优先按 `updatedAt` 降序（最近勾选的排在最前）
2. `updatedAt` 相同时按 `position` 降序

### 4.3 全选/取消全选

位于 [use-shopping-list-crud.ts](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/frontend/app/composables/shopping-list-page/sub-composables/use-shopping-list-crud.ts)。

**勾选流程：**
- 遍历所有项，设置 `checked=true`
- 更新 `updatedAt` 为当前时间（影响排序）
- 调用后端批量更新

**取消勾选流程：**
- 遍历所有项，设置 `checked=false`
- 将所有已勾选项移至未勾选列表
- 重新计算未勾选项的 `position`

### 4.4 删除逻辑

**删除已勾选项：**
1. 筛选所有 `checked=true` 的项
2. 调用 `bulk_delete_items` 批量删除
3. 定时任务自动清理：[delete_old_checked_shopping_list_items.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/services/scheduler/tasks/delete_old_checked_shopping_list_items.py)

**移除食谱时的递减逻辑**（`remove_recipe_ingredients_from_list` L457-L539）：

1. 遍历清单项，找到对应食谱的引用
2. 如果 `recipe_scale > recipe_decrement`，只减少部分：
   ```python
   item.quantity -= recipe_decrement * ref.recipe_quantity
   ref.recipe_scale -= recipe_decrement
   ```
3. 否则移除整个引用：
   ```python
   item.quantity -= ref.recipe_scale * ref.recipe_quantity
   item.recipe_references.remove(ref)
   ```
4. 数量为 0 且无其他食谱引用时，删除整个清单项：
   ```python
   if item.quantity < 0 or (item.quantity == 0 and not item.recipe_references):
       delete_items.append(item.id)
   ```

### 4.5 孤儿引用清理

每次批量操作后调用 `remove_unused_recipe_references`（L130-L143）：

1. 收集所有清单项中仍在使用的 `recipe_id`
2. 删除清单级别 `recipe_references` 中未被任何项引用的条目

---

## 五、完整聚合流程示例

**场景**：购物清单已有 "1 cup 牛奶"，现在添加食谱 A（含 "200ml 牛奶"）和食谱 B（含 "0.5 pint 牛奶"）。

**执行流程：**

1. 提取食谱 A 食材 → `{quantity: 200, unit: ml, food: 牛奶}`
2. 提取食谱 B 食材 → `{quantity: 0.5, unit: pint, food: 牛奶}`
3. **批次内合并**：判断可合并（food_id 相同，单位兼容）
   - 200ml + 0.5pint = 200ml + 236.59ml = 436.59ml ≈ 1.84 cup
   - 合并后：`{quantity: 1.84, unit: cup, food: 牛奶}`
4. **与现有项合并**：现有 "1 cup 牛奶"，判断可合并
   - 1.84 cup + 1 cup = 2.84 cup
   - 合并食谱引用：`[{recipe: A, scale: 1}, {recipe: B, scale: 1}]`
5. 最终结果：**"2.84 cup 牛奶"**，带两个食谱引用

---

## 六、关键数据结构

### ShoppingListItemBase

位于 [group_shopping_list.py](file:///d:/fz/0601/solo-dogfeeding/code/33-mealie/mealie/schema/household/group_shopping_list.py#L58-L76)。

```python
class ShoppingListItemBase(RecipeIngredientBase):
    shopping_list_id: UUID4
    checked: bool = False
    position: int = 0
    quantity: float = 1
    food_id: UUID4 | None = None
    label_id: UUID4 | None = None
    unit_id: UUID4 | None = None
    extras: dict | None = {}
```

### ShoppingListItemRecipeRef

```python
class ShoppingListItemRecipeRefCreate(MealieModel):
    recipe_id: UUID4
    recipe_quantity: float = 0      # 单倍配方中的数量
    recipe_scale: NoneFloat = 1     # 添加次数倍数
    recipe_note: str | None = None  # 原始备注
```

---

## 七、边界情况汇总

| 场景 | 处理方式 |
|------|----------|
| 已勾选的项 | 不参与合并，不参与匹配 |
| 无 `food_id` 且 `note` 不同 | 不可合并 |
| 单位不可转换（如 cup vs pound） | 不可合并 |
| 合并后数量 < 1 | 自动改用较小单位显示 |
| 数量为负的新项 | 丢弃不创建 |
| 数量为负的更新项 | 标记为删除 |
| 数量为 0 且无食谱引用 | 删除整个项 |
| ounce 与体积单位合并 | 自动视为 fluid ounce |
| 同一请求中相同 ID 的更新 | 只保留第一个 |
