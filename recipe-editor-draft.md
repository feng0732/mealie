# Recipe Editor 与草稿状态前后端协作细节

## 1. 架构总览

Recipe Editor 采用 Nuxt 3 + Vue 3 前端，FastAPI + SQLAlchemy 后端的前后端分离架构。

- **前端页面**：[r/[slug]/index.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/pages/g/%5BgroupSlug%5D/r/%5Bslug%5D/index.vue) → [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue)
- **后端控制器**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/routes/recipe/recipe_crud_routes.py) (RecipeController)
- **后端服务层**：[recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/services/recipe/recipe_service.py) (RecipeService)
- **前端 API 客户端**：[recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/lib/api/user/recipes/recipe.ts) (RecipeAPI)

**重要提示**：Mealie 当前**没有独立的草稿（Draft）状态机制**。所有编辑操作直接作用于 Recipe 实体本身，保存即持久化到数据库。所谓"草稿"仅指编辑过程中尚未保存到后端的本地内存状态。

---

## 2. 前端编辑器本地状态管理

### 2.1 页面模式共享状态

状态通过 [shared-state.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/composables/recipe-page/shared-state.ts) 中的 `usePageState(slug)` 管理，采用 **Memo 单例模式**（以 slug 为 key 缓存）。

```typescript
// 核心状态枚举
enum PageMode { EDIT = "EDIT", VIEW = "VIEW", COOK = "COOK" }
enum EditorMode { JSON = "JSON", FORM = "FORM" }

// 导出的 ComputedRef 状态
isEditForm    // EDIT + FORM 同时满足
isEditJSON    // EDIT + JSON 同时满足
isEditMode    // 处于 EDIT 模式
isCookMode    // 处于 COOK 模式
isParsing     // 正在解析食材
imageKey      // 图片刷新缓存键（每次图片变更自增）
```

**模式切换副作用**：
- 进入 `EDIT` 模式 → `activateNavigationWarning()` 激活 `onbeforeunload` 浏览器提示
- 离开 `EDIT` 模式 → `deactivateNavigationWarning()` 关闭提示，并重置 EditorMode 为 FORM

### 2.2 Recipe 对象的状态共享

RecipePage 组件注释明确说明了数据流转方式：

> The global recipe object is shared down the tree of components and _is_ mutated by child components. This is some-what of a hack of the system and goes against the principles of Vue...

- 使用 Vue 3 `defineModel<NoUndefinedField<Recipe>>()` 在根组件 [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L224) 接收数据
- 所有子组件（[RecipePageInfoEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageInfoEditor.vue)、[RecipePageIngredientEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageIngredientEditor.vue)、[RecipePageInstructions.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageInstructions.vue) 等）通过 props 双向绑定直接**原地 mutate** 同一个 Recipe 对象引用
- 这违反了 Vue 的单向数据流原则，但简化了大量 prop 透传

### 2.3 变更快照检测（脏检查）

```typescript
// RecipePage.vue L251-L265
const originalRecipe = ref<Recipe | null>(null);

// 挂载后深拷贝一份作为基线
onMounted: originalRecipe.value = deepCopy(recipe.value);

// 通过 JSON 序列化比较判断是否有未保存变更
function hasUnsavedChanges(): boolean {
  return JSON.stringify(recipe.value) !== JSON.stringify(originalRecipe.value);
}
```

### 2.4 未保存变更的导航拦截

两层保护机制：

**第一层：浏览器级**（[use-navigation-warning.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/composables/use-navigation-warning.ts)）
- `window.onbeforeunload = () => true` — 刷新/关闭标签页时浏览器原生弹窗

**第二层：Vue Router 级**（[RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L302-L308)）
```typescript
onBeforeRouteLeave((to) => {
  if (isEditMode.value && hasUnsavedChanges()) {
    pendingRoute.value = to;
    discardDialog.value = true;  // 弹出"放弃变更"确认对话框
    return false;  // 阻止导航
  }
});
```

用户确认后：`restoreOriginalRecipe()` 将 recipe 恢复为深拷贝的基线值，然后执行导航。

---

## 3. 保存流程

### 3.1 创建新 Recipe（两步走）

**第一步**：[new.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/pages/g/%5BgroupSlug%5D/r/create/new.vue) 创建最小骨架
```typescript
// 前端仅传 { name }
await api.recipes.createOne({ name });
// 后端返回 201 + slug
router.push(`/g/${groupSlug}/r/${slug}?edit=true`);
```

后端 [recipe_service.py#L202-L245](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/services/recipe/recipe_service.py#L202-L245) `_recipe_creation_factory` 填充默认值：
- 注入 `user_id`、`household_id`、`group_id`
- 创建一条默认食材 `RecipeIngredient(note="默认食材提示")`
- 创建一条默认步骤 `RecipeStep(text="默认步骤提示")`
- 从 household preferences 继承 RecipeSettings（公开性、营养显示等）
- 创建 RecipeTimelineEvent（创建事件）

**第二步**：跳转到详情页并自动进入编辑模式（URL query `?edit=true` 触发）

### 3.2 更新已有 Recipe

调用链：
1. 前端 [RecipePage.vue#L363-L378](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L363-L378) `saveRecipe()`
2. `api.recipes.updateOne(slug, recipe.value)` → PUT `/api/recipes/{slug}`
3. 后端 [recipe_crud_routes.py#L472-L493](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/routes/recipe/recipe_crud_routes.py#L472-L493) `RecipeController.update_one()`
4. [recipe_service.py#L520-L528](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/services/recipe/recipe_service.py#L520-L528) `RecipeService.update_one()`

#### 后端更新流程分解：

1. **`_pre_update_check`** 权限与一致性校验
   - 校验 recipe 存在性
   - `can_update([slug])` 检查权限（owner / 未锁定 / household 策略）
   - 若变更 locked 字段，校验是否为 owner
   - `has_recursive_recipe_link()` 检查食材引用是否形成环

2. **数据清洗**
   - `_remove_non_existent_ingredient_references()`：清理步骤中引用了已删除食材的 ingredientReferences
   - `_resolve_ingredient_sub_recipes()`：将子食谱的 slug 解析为 id

3. **持久化**：`self.group_recipes.update(recipe.slug, update_data)`

4. **资产处理**：`check_assets(new_data, recipe.slug)`
   - 若 slug 变更，复制旧目录到新目录
   - 清理 assets 列表中未引用的文件

5. **前端成功处理**：
   - 若 slug 变更（重命名导致），`router.replace()` 跳转新 URL
   - 更新 `originalRecipe` 快照为最新数据
   - `setMode(PageMode.VIEW)` 退出编辑模式

### 3.3 解析食材保存

[RecipePage.vue#L380-L384](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L380-L384) `saveParsedIngredients()` 先替换 `recipe.recipeIngredient` 再调用 `saveRecipe()`。

---

## 4. 错误校验机制

### 4.1 前端表单校验

基于 Vuetify rules，在 [use-validators.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/composables/use-validators.ts) 中导出：

| 校验器 | 说明 | 使用位置 |
|-------|------|---------|
| `required` | 非空 | recipe.name (RecipePageInfoEditor) |
| `email` | 邮箱格式 | 用户相关 |
| `whitespace` | 不含空格 | 标识类字段 |
| `url` / `urlOptional` | URL 格式 | 来源链接等 |
| `minLength(n)` / `maxLength(n)` | 长度约束 | - |

**重要**：前端仅对 `recipe.name` 做了必填校验，其他字段（食材、步骤等）均无前端阻塞性校验。

### 4.2 后端 Pydantic Schema 校验

[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/schema/recipe/recipe.py) 中通过 `@field_validator` 实现：

- **数字字段零值兜底**：`recipe_servings`、`recipe_yield_quantity` 空值转 0
- **字符串化**：时间字段、yield 字段等 Number 转 String
- **Slug 生成**：slug 为空时用 name 通过 `slugify()` 自动生成
- **食材兼容**：`recipe_ingredient` 为 `string[]` 时自动转为 `RecipeIngredient[]`
- **标签/分类兼容**：tags/categories 为 `string[]` 时自动创建实体
- **关联数据规范化**：group_id、household_id、user_id 从 int 转为 UUID
- **extras 转换**：从 list 形式转为 dict 形式

### 4.3 后端业务异常处理

[recipe_crud_routes.py#L90-L125](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/routes/recipe/recipe_crud_routes.py#L90-L125) `handle_exceptions()` 统一映射：

| 异常类型 | HTTP 状态码 | 消息 |
|---------|------------|------|
| `PermissionDenied` | 403 | Permission Denied |
| `NoEntryFound` | 404 | No Entry Found |
| `sqlalchemy.exc.IntegrityError` | 400 | Recipe already exists（唯一约束冲突） |
| `RecursiveRecipe` | 400 | 递归食谱链接 |
| `SlugError` | 400 | 无法生成合法 slug |
| 其他 | 500 | Unknown Error |

**注意**：前端 `saveRecipe()` **未对 error 做任何用户友好提示**，仅静默失败（不退出编辑模式）。

---

## 5. 图片字段交互

### 5.1 组件协作链

```
RecipeImageUploadBtn.vue  (上传按钮 + 删除确认弹窗 + URL 输入)
        |
        v (emit upload/delete)
RecipePageEditorToolbar.vue  (处理事件，调用 API)
        |
        v (更新 recipe.image, imageKey++)
RecipePageHeader.vue  (通过 imageKey 触发图片 URL 刷新)
```

### 5.2 三种图片操作

**1. 本地上传**（PUT `/api/recipes/{slug}/image`，multipart/form-data）
```typescript
// RecipePageEditorToolbar.vue L72-L81
async function uploadImage(fileObject: File) {
  const newVersion = await api.recipes.updateImage(recipe.value.slug, fileObject);
  if (newVersion?.data?.image) {
    recipe.value.image = newVersion.data.image;  // 更新后端返回的新 image key
  }
  imageKey.value++;  // 触发浏览器缓存失效
}
```

**2. URL 抓取**（POST `/api/recipes/{slug}/image`，JSON body `{ url }`）
- 后端 [recipe_crud_routes.py#L614-L633](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/routes/recipe/recipe_crud_routes.py#L614-L633) 通过 `RecipeDataService.scrape_image(url)` 下载，异常：
  - `NotAnImageError` → 400 "Url is not an image"
  - `InvalidDomainError` → 400 "Url is not from an allowed domain"

**3. 删除图片**（DELETE `/api/recipes/{slug}/image`）
- 前端本地立即 `recipe.value.image = ""`，再 `imageKey.value++` 乐观更新

### 5.3 后端图片处理

[recipe_service.py#L530-L548](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/services/recipe/recipe_service.py#L530-L548):
- `update_recipe_image()`：`RecipeDataService.writeImage(bytes, extension)` 写入磁盘，然后 DB 更新 image 字段为新 cache key
- `delete_recipe_image()`：删除磁盘文件 + DB 置空

### 5.4 图片字段与 Recipe 保存的解耦

**关键风险点**：图片操作是**独立 API 调用**，不经过 `saveRecipe()`。意味着：
- 上传图片**立即生效**，不等同于 Recipe 整体保存
- 用户若上传图片后放弃 Recipe 其他变更，图片仍然保留在后端
- 删除图片同样立即生效，无法通过"放弃变更"回滚

---

## 6. 并发覆盖的风险点

### 6.1 Last-Write-Wins（无乐观锁）

后端 `update_one()` 流程：
```python
recipe = self.get_one(slug_or_id)       # 读
# ... 校验 ...
new_data = self.group_recipes.update(recipe.slug, update_data)  # 写
```

**无任何版本号、ETag、updated_at 比较机制**。并发场景下后提交者直接覆盖先提交者的数据，无任何警告。

### 6.2 前端本地状态陈旧

前端仅在**首次挂载**时通过 `useRecipe(slug)` 拉取一次数据。之后：
- 没有 WebSocket 推送
- 没有轮询刷新
- 没有重新拉取机制（除非手动刷新页面）

用户 A 打开 Recipe 编辑页，用户 B 同时修改并保存，用户 A 后续保存时直接覆盖 B 的修改。

### 6.3 图片操作的竞态

图片操作与 Recipe 更新是独立 API：
- `updateImage` + `saveRecipe` 同时发起时，DB 的 image 字段谁后写入谁生效
- 但磁盘上的图片文件以 `updateImage` 实际写入的为准，可能出现 DB 记录与磁盘不一致

### 6.4 Slug 变更的竞态

重命名 recipe 会触发 slug 变更（若 slug 基于 name）。若两人同时修改 name 并保存：
- 后保存者可能使用已存在的 slug → 触发 IntegrityError → 返回 400 "Recipe already exists"
- 前端不会自动重试或追加后缀

### 6.5 子资源引用的竞态

`_resolve_ingredient_sub_recipes()` 将 slug 解析为 id。若解析后保存前，目标 recipe 被删除，则保存时可能出现外键问题（取决于 DB 约束配置）。

### 6.6 多 Tab 编辑同一 Recipe

`usePageState(slug)` 使用全局 memo，同一浏览器多 Tab 编辑同一 recipe：
- 共享同一份 PageMode 状态（一个 Tab 进入编辑，另一个也会受影响）
- 但 Recipe 对象是各 Tab 独立的 ref → 各 Tab 的本地变更互不通知
- 各自保存时按 6.1 的 LWW 规则覆盖

---

## 7. 关键代码索引

| 模块 | 文件 | 关键行 |
|-----|------|-------|
| 页面入口 | [r/[slug]/index.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/pages/g/%5BgroupSlug%5D/r/%5Bslug%5D/index.vue) | 28-56 |
| 编辑器主组件 | [RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) | 224-378 |
| 共享状态管理 | [shared-state.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/composables/recipe-page/shared-state.ts) | 4-165 |
| 导航警告 | [use-navigation-warning.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/composables/use-navigation-warning.ts) | 1-20 |
| 前端 API | [recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/lib/api/user/recipes/recipe.ts) | 93-283 |
| 后端控制器 | [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/routes/recipe/recipe_crud_routes.py) | 88-699 |
| 后端服务层 | [recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/services/recipe/recipe_service.py) | 63-589 |
| Recipe Schema | [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/mealie/schema/recipe/recipe.py) | 40-348 |
| 图片上传按钮 | [RecipeImageUploadBtn.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipeImageUploadBtn.vue) | 1-138 |
| 图片工具栏 | [RecipePageEditorToolbar.vue](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageEditorToolbar.vue) | 1-88 |
| 表单校验 | [inputs.ts](file:///d:/fz/0601/solo-dogfeeding/code/84-mealie/frontend/app/lib/validators/inputs.ts) | 1-48 |
