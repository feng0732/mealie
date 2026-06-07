# Recipe Editor 与草稿状态前后端协作细节

## 1. 架构总览

Recipe Editor 采用 Nuxt 3 + Vue 3 前端，FastAPI + SQLAlchemy 后端的前后端分离架构。

- **前端页面**：`frontend/app/pages/g/[groupSlug]/r/[slug]/index.vue` → `frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue`
- **后端控制器**：`mealie/routes/recipe/recipe_crud_routes.py` (RecipeController)
- **后端服务层**：`mealie/services/recipe/recipe_service.py` (RecipeService)
- **前端 API 客户端**：`frontend/app/lib/api/user/recipes/recipe.ts` (RecipeAPI)

**重要提示**：Mealie 当前**没有独立的草稿（Draft）状态机制**。所有编辑操作直接作用于 Recipe 实体本身，保存即持久化到数据库。所谓"草稿"仅指编辑过程中尚未保存到后端的本地内存状态。

---

## 2. 前端编辑器本地状态管理

### 2.1 页面模式共享状态

状态通过 `frontend/app/composables/recipe-page/shared-state.ts` 中的 `usePageState(slug)` 管理。

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

**关键实现：Memo 的运行时边界**

```typescript
// shared-state.ts
const memo: Record<string, PageRefs> = {};  // ES 模块顶层变量

export function usePageState(slug: string): PageState {
  if (!memo[slug]) {
    memo[slug] = pageRefs(slug);
  }
  return pageState(memo[slug]);
}
```

`memo` 声明在 ES 模块的顶层作用域。根据 ES Module 规范，**每个 JavaScript 运行时（即每个浏览器标签页/Worker）拥有独立的模块实例**。因此：

| 场景 | memo 是否共享 | 说明 |
|-----|-------------|------|
| 同一 Tab 内不同组件调用 `usePageState("same-slug")` | ✅ 共享 | 保证同页面内 Header/Toolbar/IngredientEditor 等子组件状态一致 |
| 同一浏览器不同 Tab 打开同一 Recipe | ❌ 完全隔离 | 每个 Tab 有独立的 V8 实例、独立的模块作用域、独立的 memo 对象 |
| 同一 Tab 内不同 slug | ⚠️ 同 memo 对象，不同 key | 不同 Recipe 页面互不干扰 |

这意味着 `PageMode`、`EditorMode`、`imageKey` 等**只在单个 Tab 内共享**，跨 Tab 完全独立。

**模式切换副作用**：
- 进入 `EDIT` 模式 → `activateNavigationWarning()` 激活 `onbeforeunload` 浏览器提示
- 离开 `EDIT` 模式 → `deactivateNavigationWarning()` 关闭提示，并重置 EditorMode 为 FORM

### 2.2 Recipe 对象的状态共享

RecipePage 组件注释明确说明了数据流转方式：

> The global recipe object is shared down the tree of components and _is_ mutated by child components. This is some-what of a hack of the system and goes against the principles of Vue...

- 使用 Vue 3 `defineModel<NoUndefinedField<Recipe>>()` 在根组件 `frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue` 接收数据
- 所有子组件（`frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageInfoEditor.vue`、`RecipePageIngredientEditor.vue`、`RecipePageInstructions.vue` 等）通过 props 双向绑定直接**原地 mutate** 同一个 Recipe 对象引用
- 这违反了 Vue 的单向数据流原则，但简化了大量 prop 透传
- **跨 Tab 隔离**：每个 Tab 有自己独立的 `recipe` ref 和组件树，mutate 互不影响

### 2.3 变更快照检测（脏检查）

```typescript
// RecipePage.vue
const originalRecipe = ref<Recipe | null>(null);

// 挂载后深拷贝一份作为基线
onMounted: originalRecipe.value = deepCopy(recipe.value);

// 通过 JSON 序列化比较判断是否有未保存变更
function hasUnsavedChanges(): boolean {
  return JSON.stringify(recipe.value) !== JSON.stringify(originalRecipe.value);
}
```

`originalRecipe` 也是 Tab 内独立的 ref，每个 Tab 维护自己的快照基线。

### 2.4 未保存变更的导航拦截

两层保护机制，均为 Tab 级独立生效：

**第一层：浏览器级**（`frontend/app/composables/use-navigation-warning.ts`）
- `window.onbeforeunload = () => true` — 刷新/关闭标签页时浏览器原生弹窗
- 每个 Tab 独立注册，互不干扰

**第二层：Vue Router 级**（`frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue`）
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

**第一步**：`frontend/app/pages/g/[groupSlug]/r/create/new.vue` 创建最小骨架
```typescript
// 前端仅传 { name }
await api.recipes.createOne({ name });
// 后端返回 201 + slug
router.push(`/g/${groupSlug}/r/${slug}?edit=true`);
```

后端 `mealie/services/recipe/recipe_service.py` `_recipe_creation_factory` 填充默认值：
- 注入 `user_id`、`household_id`、`group_id`
- 创建一条默认食材 `RecipeIngredient(note="默认食材提示")`
- 创建一条默认步骤 `RecipeStep(text="默认步骤提示")`
- 从 household preferences 继承 RecipeSettings（公开性、营养显示等）
- 创建 RecipeTimelineEvent（创建事件）

**第二步**：跳转到详情页并自动进入编辑模式（URL query `?edit=true` 触发）

### 3.2 更新已有 Recipe

调用链：
1. 前端 `RecipePage.vue` `saveRecipe()`
2. `api.recipes.updateOne(slug, recipe.value)` → PUT `/api/recipes/{slug}`
3. 后端 `mealie/routes/recipe/recipe_crud_routes.py` `RecipeController.update_one()`
4. `mealie/services/recipe/recipe_service.py` `RecipeService.update_one()`

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

`RecipePage.vue` `saveParsedIngredients()` 先替换 `recipe.recipeIngredient` 再调用 `saveRecipe()`。

---

## 4. 错误校验机制

### 4.1 前端表单校验

基于 Vuetify rules，在 `frontend/app/composables/use-validators.ts` 中导出：

| 校验器 | 说明 | 使用位置 |
|-------|------|---------|
| `required` | 非空 | recipe.name (RecipePageInfoEditor) |
| `email` | 邮箱格式 | 用户相关 |
| `whitespace` | 不含空格 | 标识类字段 |
| `url` / `urlOptional` | URL 格式 | 来源链接等 |
| `minLength(n)` / `maxLength(n)` | 长度约束 | - |

**重要**：前端仅对 `recipe.name` 做了必填校验，其他字段（食材、步骤等）均无前端阻塞性校验。

### 4.2 后端 Pydantic Schema 校验

`mealie/schema/recipe/recipe.py` 中通过 `@field_validator` 实现：

- **数字字段零值兜底**：`recipe_servings`、`recipe_yield_quantity` 空值转 0
- **字符串化**：时间字段、yield 字段等 Number 转 String
- **Slug 生成**：slug 为空时用 name 通过 `slugify()` 自动生成
- **食材兼容**：`recipe_ingredient` 为 `string[]` 时自动转为 `RecipeIngredient[]`
- **标签/分类兼容**：tags/categories 为 `string[]` 时自动创建实体
- **关联数据规范化**：group_id、household_id、user_id 从 int 转为 UUID
- **extras 转换**：从 list 形式转为 dict 形式

### 4.3 后端业务异常处理

`mealie/routes/recipe/recipe_crud_routes.py` `handle_exceptions()` 统一映射：

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
// RecipePageEditorToolbar.vue
async function uploadImage(fileObject: File) {
  const newVersion = await api.recipes.updateImage(recipe.value.slug, fileObject);
  if (newVersion?.data?.image) {
    recipe.value.image = newVersion.data.image;  // 更新后端返回的新 image key
  }
  imageKey.value++;  // 触发浏览器缓存失效
}
```

**2. URL 抓取**（POST `/api/recipes/{slug}/image`，JSON body `{ url }`）
- 后端 `mealie/routes/recipe/recipe_crud_routes.py` 通过 `RecipeDataService.scrape_image(url)` 下载，异常：
  - `NotAnImageError` → 400 "Url is not an image"
  - `InvalidDomainError` → 400 "Url is not from an allowed domain"

**3. 删除图片**（DELETE `/api/recipes/{slug}/image`）
- 前端本地立即 `recipe.value.image = ""`，再 `imageKey.value++` 乐观更新

### 5.3 后端图片处理

`mealie/services/recipe/recipe_service.py`:
- `update_recipe_image()`：`RecipeDataService.writeImage(bytes, extension)` 写入磁盘，然后 DB 更新 image 字段为新 cache key
- `delete_recipe_image()`：删除磁盘文件 + DB 置空

### 5.4 图片字段与 Recipe 保存的解耦

**关键风险点**：图片操作是**独立 API 调用**，不经过 `saveRecipe()`。意味着：
- 上传图片**立即生效**，不等同于 Recipe 整体保存
- 用户若上传图片后放弃 Recipe 其他变更，图片仍然保留在后端
- 删除图片同样立即生效，无法通过"放弃变更"回滚

---

## 6. 并发覆盖的风险点

### 6.1 浏览器标签页的运行时隔离模型

在深入分析并发风险前，先明确浏览器多 Tab 的运行时边界：

| 资源 | 跨 Tab 共享？ | 存储/实现方式 |
|-----|-------------|-------------|
| HTTP Cookie（token） | ✅ 共享 | 浏览器 Cookie Jar，同源下所有 Tab 可见 |
| `memo`（usePageState） | ❌ 隔离 | ES 模块顶层变量，每个 Tab 独立模块实例 |
| `recipe` ref / `originalRecipe` 快照 | ❌ 隔离 | Vue 组件响应式数据，Tab 内独立 |
| `PageMode` / `EditorMode` | ❌ 隔离 | 基于 memo 的 ref，每个 Tab 独立 |
| `window.onbeforeunload` | ❌ 隔离 | 每个 Tab 的 window 对象独立 |
| Vue Router / `onBeforeRouteLeave` | ❌ 隔离 | 每个 Tab 有独立的 Vue 应用实例和 Router 实例 |
| `authUser` / `authStatus` ref | ❌ 隔离 | ES 模块顶层 ref，但初始化时从 Cookie 读 token → 调 API，最终状态一致 |
| Store（useUserStore 等） | ❌ 隔离 | ES 模块顶层 ref，Tab 内独立 |

**跨 Tab 通信机制检查结论**：代码中**未使用** `BroadcastChannel`、`window.postMessage`、`localStorage` 的 `storage` 事件、或 SharedWorker 等跨 Tab 同步方案。仅依赖 Cookie 实现隐式的认证状态共享。

### 6.2 Last-Write-Wins（无乐观锁）

后端 `update_one()` 流程：
```python
recipe = self.get_one(slug_or_id)       # 读
# ... 校验 ...
new_data = self.group_recipes.update(recipe.slug, update_data)  # 写
```

**无任何版本号、ETag、updated_at 比较机制**。并发场景下后提交者直接覆盖先提交者的数据，无任何警告。

### 6.3 多 Tab 编辑同一 Recipe 的具体并发场景

用户在同一浏览器打开两个 Tab 编辑同一份 Recipe（`/g/my-group/r/awesome-recipe?edit=true`）：

**阶段 1：各自拉取数据**
- Tab A 挂载 → `useRecipe(slug)` → `GET /api/recipes/awesome-recipe` → 得到数据 v1
- Tab B 挂载 → `useRecipe(slug)` → `GET /api/recipes/awesome-recipe` → 得到数据 v1
- 此时两个 Tab 内存中各有一份 v1 的独立深拷贝（`originalRecipe` 快照）

**阶段 2：各自编辑**
- Tab A 修改 `recipe.name` 为 "Awesome Recipe v2"，内存中 recipe ref 变更，`hasUnsavedChanges() === true`
- Tab B 修改 `recipe.description` 为 "A great recipe"，内存中 recipe ref 变更，`hasUnsavedChanges() === true`
- 两者完全不知道对方的存在，无任何 UI 提示

**阶段 3：Tab A 先保存**
- Tab A `PUT /api/recipes/awesome-recipe` → 后端将 name 更新为 v2，DB 中数据为 v2
- Tab A `originalRecipe` 快照更新为 v2，退出 EDIT 模式

**阶段 4：Tab B 后保存（覆盖发生）**
- Tab B `PUT /api/recipes/awesome-recipe` → payload 中 name 仍是旧值 "Awesome Recipe"，description 是新值
- 后端直接用 Tab B 的整份 payload 覆盖 DB → **name 被回滚为旧值**，description 更新
- Tab B 以为自己保存成功，但实际上覆盖/丢失了 Tab A 的修改

### 6.4 前端本地状态陈旧

前端仅在**首次挂载**时通过 `useRecipe(slug)` 拉取一次数据。之后：
- 没有 WebSocket 推送
- 没有轮询刷新
- 没有通过 `localStorage` storage 事件监听其他 Tab 的保存
- 没有重新拉取机制（除非手动刷新页面）

不仅是多 Tab，即使用户 A 和用户 B 在不同设备上编辑同一 Recipe，也存在相同的 LWW 覆盖问题。

### 6.5 图片操作的竞态

图片操作与 Recipe 更新是独立 API：
- `updateImage` + `saveRecipe` 同时发起时，DB 的 image 字段谁后写入谁生效
- 但磁盘上的图片文件以 `updateImage` 实际写入的为准，可能出现 DB 记录与磁盘不一致
- 两个 Tab 分别上传图片时：后写入磁盘的文件实际保留，但 DB 的 image cache key 以最后一个 API 响应为准

### 6.6 Slug 变更的竞态

重命名 recipe 会触发 slug 变更（若 slug 基于 name）。若两人同时修改 name 并保存：
- 后保存者可能使用已存在的 slug → 触发 IntegrityError → 返回 400 "Recipe already exists"
- 前端不会自动重试或追加后缀

### 6.7 子资源引用的竞态

`_resolve_ingredient_sub_recipes()` 将 slug 解析为 id。若解析后保存前，目标 recipe 被删除，则保存时可能出现外键问题（取决于 DB 约束配置）。

### 6.8 认证状态变化的跨 Tab 传播边界

虽然 Cookie 是共享的，但认证状态的内存表示（`authStatus` ref）不跨 Tab 同步：
- Tab A 执行 `signOut()` → 清 Cookie + `authStatus = "unauthenticated"` + 跳转 `/login`
- Tab B 的 `authStatus` ref 仍为 `"authenticated"`，直到下一次需要 token 的 API 调用返回 401 才会被 `handleAuthError` 重置
- 对 Recipe 编辑的直接影响：Tab A 登出后，Tab B 仍可继续编辑，但保存时会因 401 失败，且 `saveRecipe()` 没有友好错误提示

---

## 7. 关键代码索引

| 模块 | 仓库相对路径 | 关键行 |
|-----|------------|-------|
| 页面入口 | `frontend/app/pages/g/[groupSlug]/r/[slug]/index.vue` | 28-56 |
| 编辑器主组件 | `frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue` | 224-378 |
| 共享状态管理（含 memo 实现） | `frontend/app/composables/recipe-page/shared-state.ts` | 4-165 |
| 导航警告（onbeforeunload） | `frontend/app/composables/use-navigation-warning.ts` | 1-20 |
| 前端 API Client | `frontend/app/lib/api/user/recipes/recipe.ts` | 93-283 |
| 后端 CRUD 控制器 | `mealie/routes/recipe/recipe_crud_routes.py` | 88-699 |
| 后端控制器基类 | `mealie/routes/recipe/_base.py` | 37-56 |
| 后端服务层（含 update_one） | `mealie/services/recipe/recipe_service.py` | 63-589 |
| Recipe Schema + Pydantic 校验 | `mealie/schema/recipe/recipe.py` | 40-348 |
| 图片上传按钮组件 | `frontend/app/components/Domain/Recipe/RecipeImageUploadBtn.vue` | 1-138 |
| 图片工具栏（含 uploadImage） | `frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageEditorToolbar.vue` | 1-88 |
| 前端表单校验器 | `frontend/app/lib/validators/inputs.ts` | 1-48 |
| 校验器 composable | `frontend/app/composables/use-validators.ts` | 1-42 |
| 创建新 Recipe 页面 | `frontend/app/pages/g/[groupSlug]/r/create/new.vue` | 44-80 |
| 认证后端（Cookie + authStatus ref） | `frontend/app/composables/use-auth-backend.ts` | 24-142 |
| 认证插件初始化 | `frontend/app/plugins/init-auth.client.ts` | 1-9 |
| Mealie Auth 包装 | `frontend/app/composables/use-mealie-auth.ts` | 1-56 |
