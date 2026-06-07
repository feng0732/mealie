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

虽然 Cookie 是共享的，但认证状态的内存表示（`authStatus` ref）不跨 Tab 同步。下面以"Tab A 登出 → Tab B 保存 Recipe 失败"为场景，逐步拆解完整代码链路。

#### 阶段一：Tab A 执行 signOut()

调用链：`useMealieAuth().signOut()` → `useAuthBackend().signOut()`

```typescript
// use-auth-backend.ts signOut()
async function signOut(callbackUrl: string = ""): Promise<void> {
  try {
    await $axios.post("/api/auth/logout");   // 可选调用后端 logout API
  }
  finally {
    setToken(null);                          // 1. 清 Cookie
    authUser.value = null;                   // 2. 清 Tab A 的内存 ref
    authStatus.value = "unauthenticated";    // 3. 更新 Tab A 的内存状态
    clearAllStores();
    clearNuxtData();
    await router.push(callbackUrl || "/login"); // 4. Tab A 跳转 /login
  }
}
```

关键副作用：
- `setToken(null)` 通过 Nuxt 的 `useCookie()` 将 Cookie 设置为 `null` → 浏览器 Cookie Jar 被更新
- 由于 HTTP Cookie 是浏览器级资源，**同源下所有 Tab 共享同一个 Cookie Jar**，因此 Tab B 的 Cookie 也被同步清除（这是浏览器行为，不是 JS 主动通知）
- 但 `authUser` 和 `authStatus` 是 **ES 模块顶层 ref**，仅存在于 Tab A 的 JS 运行时内存中，对 Tab B 不可见

#### 阶段二：Tab B 点击保存，发起 API 请求

```typescript
// RecipePage.vue saveRecipe()
async function saveRecipe() {
  const { data, error } = await api.recipes.updateOne(recipe.value.slug, recipe.value);
  // ... 后续处理
}
```

完整调用链：
```
saveRecipe()
  → RecipeAPI.updateOne()                        [frontend/app/lib/api/user/recipes/recipe.ts]
    → BaseCRUDAPI.updateOne()                    [frontend/app/lib/api/base/base-clients.ts]
      → requests.put()                           [frontend/app/composables/api/api-client.ts]
        → request.safe(axiosInstance.put, ...)   [frontend/app/composables/api/api-client.ts]
          → axiosInstance.put(url, payload)      [触发 axios 拦截器链]
```

#### 阶段三：axios 请求拦截器读取 Cookie

`frontend/app/plugins/axios.ts` 的请求拦截器在每个请求发出前执行：

```typescript
axiosInstance.interceptors.request.use((config) => {
  const token = useCookie(tokenName).value;  // 从浏览器 Cookie 读取
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});
```

此时由于 Tab A 已经清除了 Cookie，`useCookie(tokenName).value` 返回 `null` / `undefined` → **请求不携带 Authorization header** 直接发出。

#### 阶段四：后端返回 401，axios 响应拦截器处理

后端 FastAPI 依赖检查 token 缺失或无效 → 返回 `401 Unauthorized`。

`frontend/app/plugins/axios.ts` 的响应拦截器处理错误分支：

```typescript
axiosInstance.interceptors.response.use(
  (response) => { ... },
  (error) => {
    // 优先 toast 显示后端消息
    if (error?.response?.data?.detail?.message) {
      alert.error(error.response.data.detail.message as string);
    }

    // 401 处理
    if (error?.response?.status === 401) {
      const tokenCookie = useCookie(tokenName);
      // 关键判断：只有当本地 Cookie 仍有值时才执行跳转
      if (tokenCookie.value) {        // ← 漏洞点：Tab A 已清 Cookie，此处为 false！
        tokenCookie.value = null;
        window.onbeforeunload = null;
        window.location.href = "/login";
      }
    }

    return Promise.reject(error);  // 继续抛出错误给上层
  },
);
```

**关键判断漏洞**：`if (tokenCookie.value)` 这个条件在跨 Tab 登出场景下为 `false`（因为 Cookie 已被 Tab A 清除）。因此：
- ❌ 不执行 `window.location.href = "/login"` 跳转
- ❌ 不清除本地任何内存状态
- ✅ 仅执行 `return Promise.reject(error)` 把错误抛给上层

（如果是本 Tab 自己 token 过期导致的 401，则 `tokenCookie.value` 仍有值，会正常跳转到 `/login`）

#### 阶段五：API client 层吞掉异常

`frontend/app/composables/api/api-client.ts` 中 `request.safe()` 捕获 Promise rejection：

```typescript
const request = {
  async safe<T, U>(funcCall, url, data, config) {
    let error = null;
    const response = await funcCall(url, data, config).catch(function (e) {
      console.log(e);             // 仅打印到控制台
      error = e;                  // 存到 error 变量
      return null;                // 返回 null 而非继续抛异常
    });
    return { response, error, data: response?.data ?? null };
  },
};
```

**异常不再向上冒泡**，而是被包装成 `{ response: null, error: e, data: null }` 的正常返回值。

#### 阶段六：saveRecipe() 静默失败

回到 `RecipePage.vue`：

```typescript
async function saveRecipe() {
  const { data, error } = await api.recipes.updateOne(recipe.value.slug, recipe.value);
  if (!error) {                   // error 不为 null → 跳过
    setMode(PageMode.VIEW);
  }
  if (data?.slug) {               // data 为 null → 跳过
    recipe.value = data;
    originalRecipe.value = deepCopy(recipe.value);
    // router.replace(...)
  }
  // 函数结束，无任何 UI 反馈给用户
}
```

两个 `if` 分支均不执行，编辑器仍停留在 EDIT 模式，用户完全不知道保存失败。

#### 阶段七：authStatus ref 不同步的根因

整个链路中有两处地方会更新 `authStatus`，但跨 Tab 登出场景都未触发：

| 更新 authStatus 的位置 | 触发条件 | 本场景是否触发 |
|----------------------|---------|-------------|
| `use-auth-backend.ts` → `signOut()` | 本 Tab 主动调用登出 | ❌ 是 Tab A 调用，不是 Tab B |
| `use-auth-backend.ts` → `handleAuthError()` | auth 相关 API（`signIn`、`getSession`、`refresh`）返回 401 | ❌ `saveRecipe()` 走的是通用 axios 拦截器，不经过 `handleAuthError()` |
| `axios.ts` 响应拦截器 401 分支 | Cookie 仍有值时跳转 `/login` | ❌ Cookie 已被清除，条件不满足 |

因此 Tab B 的内存中：
- `authStatus.value` 仍为 `"authenticated"`
- `authUser.value` 仍保留旧用户对象
- UI 上的头像、用户名显示正常
- `isOwnGroup` 权限判断仍返回 `true`
- 编辑器继续允许修改，但所有需要认证的 API 调用都会失败

#### 小结：跨 Tab 登出的失效链路

```
Tab A signOut()
    │
    ├─ setToken(null) ──── 浏览器 Cookie Jar 同步清除
    │                           │
    │                           ▼
    │                   Tab B 请求拦截器读 Cookie → null → 无 Authorization header
    │                           │
    │                           ▼
    │                   后端返回 401 Unauthorized
    │                           │
    │                           ▼
    │                   Tab B 响应拦截器: if (tokenCookie.value) → false → 不跳转
    │                           │
    │                           ▼
    │                   request.safe() catch → { response:null, error:e, data:null }
    │                           │
    │                           ▼
    └─ authStatus 仅更新 Tab A 内存    saveRecipe() 静默失败，Tab B authStatus 仍为 authenticated
```

### 6.9 跨 Tab 认证链路的关键前提

前述 6.8 节的链路分析依赖几个容易被忽略的底层前提。本节逐一澄清。

#### 6.9.1 Nuxt Cookie 同步机制与跨 Tab 感知边界

##### 项目配置确认

从 `frontend/nuxt.config.ts` 提取关键配置：

| 配置项 | 值 | 含义 |
|-------|---|------|
| `ssr` | `false` | 纯 SPA 模式，没有服务端渲染，所有代码在浏览器执行 |
| `future.compatibilityVersion` | `4` | Nuxt 4 兼容模式 |
| `compatibilityDate` | `"2026-04-08"` | 最新兼容日期 |
| `experimental.cookieStore` | **未配置**（默认 `false`） | 不启用浏览器 Cookie Store API |
| `refreshCookie` | **未使用**（全局搜索无结果） | — |
| `cookieStore` / `CookieStore` / `cookie.onchange` | **未使用**（全局搜索无结果） | — |

Nuxt 版本：`nuxt@^4.4.2`（`frontend/package.json`）。

##### `useCookie` 的两种底层实现

Nuxt 的 `useCookie()` 有两套底层实现，由 `experimental.cookieStore` 配置开关控制：

---

**模式 A：document.cookie（本项目默认，未启用 cookieStore）**

所有读写都走浏览器传统的同步 `document.cookie` API：

```typescript
// 读取：每次 .value 访问都会重新解析整串 document.cookie
const raw = document.cookie;  // "foo=bar; mealie.access_token=eyJhbGc...; other=1"
// 按分号分割，按 name 匹配，解码值

// 写入：通过赋值 document.cookie 追加/覆盖单条 cookie
document.cookie = "mealie.access_token=; max-age=0; path=/; secure";
```

特性：
- **同步 API**：读写都阻塞 JS 线程
- **无事件通知**：`document.cookie` 没有任何变更事件（早期 `cookiechange` 草案已废弃）
- **整串解析**：每次读取都要解析完整的 cookie 字符串，性能随 cookie 数量增多而下降
- **受 HttpOnly 限制**：HttpOnly 的 cookie 无法通过 `document.cookie` 读取

本项目中 `tokenCookie` 的配置（`frontend/app/composables/use-auth-backend.ts`）：
```typescript
const tokenCookie = useCookie(tokenName, {
  maxAge: $appInfo.tokenTime * 60 * 60,     // 有效期 = TOKEN_TIME 小时
  secure: $appInfo.production                 // 生产环境 HTTPS 时才标记 Secure
            && window?.location?.protocol === "https:",
  // 未设置 httpOnly → 默认为 false，JS 可读可写
  // 未设置 sameSite → 使用 Nuxt 默认值
  // 未设置 path → 默认为 "/"
});
```

**重要**：项目未配置 `httpOnly: true`，因此 `mealie.access_token` 是一个**非 HttpOnly cookie**，JS 可以自由读写。这是 axios 拦截器能把 token 从 cookie 取出来放到 Authorization header 的前提。

---

**模式 B：Cookie Store API（本项目未启用）**

如果配置了 `experimental.cookieStore: true`，Nuxt 会优先使用浏览器现代的 [Cookie Store API](https://developer.mozilla.org/en-US/docs/Web/API/Cookie_Store_API)：

```javascript
// 读取：异步 Promise API
const cookie = await cookieStore.get("mealie.access_token");

// 写入：异步 Promise API
await cookieStore.set({
  name: "mealie.access_token",
  value: "...",
  expires: ...,
});

// 监听变更（包括跨 Tab 的变更！）
cookieStore.addEventListener("change", (event) => {
  console.log("Cookie changed:", event.changed, event.deleted);
});
```

特性对比：

| 特性 | document.cookie（项目实际使用） | Cookie Store API（项目未启用） |
|-----|--------------------------------|-------------------------------|
| API 风格 | 同步 | 异步 Promise |
| 跨 Tab 变更通知 | ❌ 无任何机制 | ✅ `cookieStore.onchange` 事件 |
| 性能 | 每次读整串解析 | 浏览器内部优化 |
| 浏览器支持 | 100% | Chrome 87+ / Edge / Safari 17+ / **Firefox 不支持** |
| HttpOnly 可读 | ❌ | ❌（安全限制相同） |

**关键结论**：即使本项目启用了 `experimental.cookieStore`，代码中也**没有注册 `cookieStore.onchange` 事件监听器**，所以仍然不会自动感知跨 Tab 的 cookie 清除。Firefox 不支持也是一个实际障碍。

---

##### `refreshCookie` 的作用

`refreshCookie()` 是 Nuxt 提供的一个 composable，主要用于 **SSR/ISR 场景**：
- 在 SSR 模式下，服务端渲染时读取的 cookie 来自 HTTP 请求头
- 客户端 hydration 后，cookie 可能被 JS 修改过
- `refreshCookie(name)` 强制让客户端的 `useCookie` ref 重新从浏览器读取值，同步服务端和客户端的状态

但本项目 `ssr: false`（纯 SPA），没有服务端渲染阶段，因此：
- `refreshCookie()` 对本项目**无实际意义**
- 项目代码中确实没有任何 `refreshCookie` 调用

##### Tab B 感知 cookie 清除的精确条件

Tab B **绝对不会自动感知** Tab A 的登出操作。感知仅在 Tab B 代码主动读取 cookie 时发生：

| Tab B 中的代码位置 | 何时执行 | 是否触发重新读取 `document.cookie` |
|------------------|---------|----------------------------------|
| axios 请求拦截器 | 每次发起 API 请求前 | ✅ `useCookie(tokenName).value` 每次都会重新解析 |
| `useAuthBackend().token` computed 被访问 | 组件模板渲染或 JS 显式访问 | ✅ `computed(() => tokenCookie.value)` 每次 getter 都会重新解析 |
| `getSession()` 函数 | 应用初始化、登录后、refresh 后 | ✅ 开头有 `if (!tokenCookie.value)` 检查 |
| `refresh()` 函数 | 显式调用刷新 token | ✅ 开头有 `if (!tokenCookie.value)` 检查 |
| `authStatus.value` 被访问 | 任何时刻 | ❌ 这是模块级 ref，与 cookie 无绑定，只在 `signOut/getSession/handleAuthError/refresh` 中显式更新 |
| `authUser.value` 被访问 | 任何时刻 | ❌ 同上，无响应式绑定 |

**实际体感**：Tab B 用户在登出后如果不触发任何 API 请求，界面上的用户名、头像、登录状态看起来一切正常——因为 `authStatus` 和 `authUser` 还停留在旧值。直到用户点击保存等操作触发 API 请求，才会通过 401 间接感知到登出。

##### Tab B 仍然可能携带旧 JWT 的场景（基于代码分析）

在 cookie 已从浏览器 Cookie Jar 清除的前提下，代码逻辑上 Tab B **不会**再携带有效的 JWT。因为：
1. axios 请求拦截器每次都会**重新解析** `document.cookie`，不使用缓存
2. `useCookie()` ref 的 `.value` getter 每次都会重新解析，不使用缓存
3. 项目代码中没有任何地方把 JWT 单独缓存到 localStorage/sessionStorage/内存变量里再读出来

但在**时间竞态**场景下，Tab B 仍然可能携带并提交有效的旧 JWT：

**场景 1：请求发出竞态（最常见）**

Tab B 的保存请求在 Tab A 的 `setToken(null)` 执行之前就已经携带 Authorization header 进入网络层：

```
时间线：
T1: Tab B 用户点击保存 → axios 拦截器读 cookie → 读到有效 JWT → 设置 Authorization: Bearer <jwt> → 请求进入浏览器网络栈
T2: Tab A 用户点击登出 → setToken(null) → document.cookie 清除 → Cookie Jar 更新
T3: Tab B 的请求到达后端 → jwt.decode() 验证通过 → Recipe 保存成功
```

此时 cookie 虽然已被清除，但请求已经带着有效的 JWT header 发出，后端不吊销 JWT，保存成功。

**场景 2：refresh() 竞态（少见，但真实存在）**

Tab B 恰好在 Tab A 登出的同时触发了 token 刷新流程：

```
时间线：
T1: Tab B 某逻辑调用 auth.refresh() → 先检查 tokenCookie.value → 读到有效 JWT → 发起 GET /api/auth/refresh
T2: Tab A 执行 signOut() → setToken(null) → cookie 被清除 → 跳转 /login
T3: 后端收到 Tab B 的 /api/auth/refresh → JWT 仍有效 → 返回新的 access_token
T4: Tab B 收到 refresh 响应 → setToken(new_access_token) → cookie 被**重新写回**！
T5: Tab B 用户点击保存 → 请求携带新的 JWT → 保存成功
```

这种场景下，Tab A 的登出操作实际上被 Tab B 的 refresh 「撤销」了——cookie 虽然被删过一次，但 refresh 流程又把新 token 写了回去。

#### 6.9.2 后端 logout 不吊销短期 JWT（JWT 无状态）

**后端 logout 实现**（`mealie/routes/auth/auth.py`）：
```python
@user_router.post("/logout")
async def logout(response: Response, accept_language: ... = None):
    response.delete_cookie("mealie.access_token")
    return {"message": translator.t("notifications.logged-out")}
```

后端 logout 只做一件事：在 HTTP 响应中附带 `Set-Cookie: mealie.access_token=; expires=...`，通知浏览器删除 cookie。**没有任何 JWT 吊销逻辑**：
- 没有 Token Blacklist / Redis 吊销表
- 没有数据库记录短期 Token
- 没有 Token version 机制

**JWT 验证逻辑**（`mealie/core/dependencies/dependencies.py` `get_current_user()`）：
```python
# 后端有双来源读取：优先 header，fallback 到 cookie
if token is None and "mealie.access_token" in request.cookies:
    token = request.cookies.get("mealie.access_token", "")

payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])  # 仅验证签名和 exp
```

`jwt.decode()` 只验证两件事：
1. 签名是否正确（用服务端 `SECRET`）
2. `exp` claim 是否已过期

只要这两个条件满足，**无论前端是否调用过 logout，JWT 都会被接受**。这意味着：
- 登出本质上是「让浏览器丢掉 token」，不是「让 token 失效」
- 如果攻击者从某处窃取了一个未过期的 JWT，即使合法用户已登出，该 JWT 仍可被使用（直至自然过期）
- JWT 有效期由 `settings.TOKEN_TIME`（小时）控制，默认值取决于部署配置

#### 6.9.3 后端 token 的双来源读取

`get_current_user()` 有两条获取 token 的路径（`mealie/core/dependencies/dependencies.py`）：

| 来源 | 触发条件 | 代码 |
|-----|---------|------|
| Authorization header | `OAuth2PasswordBearer` 从 header 解析 `Bearer <token>` | `token: str \| None = Depends(oauth2_scheme_soft_fail)` |
| Cookie | header 没取到且 `request.cookies` 中存在 `mealie.access_token` | `token = request.cookies.get("mealie.access_token", "")` |

**双来源的意义**：
- 常规 API 请求：前端 axios 拦截器把 token 从 cookie 读出来放进 `Authorization: Bearer` header → 走第一条路径
- 浏览器直接发起的请求（如 `<img src>`、`<a href>` 下载）：浏览器自动带 Cookie，但不会加 Authorization header → 走第二条路径
- 跨 Tab 登出场景：两条路径都可能拿不到 token（cookie 被清除 + 拦截器没设置 header）

#### 6.9.4 Tab B 保存 Recipe 的两条路径

基于以上前提，Tab A 登出后 Tab B 保存 Recipe 存在**成功**和**失败**两条路径，取决于请求发出的时间点。

---

**路径一：保存失败（绝大多数正常时序）**

```
Tab A 用户点击登出
    │
    ▼
signOut() 执行 setToken(null)
    │  → document.cookie 删除 mealie.access_token
    │  → 浏览器 Cookie Jar 同步更新
    │
    ▼ （若干毫秒后）
Tab B 用户点击保存
    │
    ▼
axios 请求拦截器执行:
  const token = useCookie(tokenName).value
    │  → 重新解析 document.cookie → null
    │  → 不设置 Authorization header
    │
    ▼
浏览器自动附带 Cookie: （Cookie Jar 中已无 mealie.access_token）
    │
    ▼
后端 get_current_user():
  ├─ oauth2_scheme_soft_fail → None（无 header）
  └─ request.cookies.get("mealie.access_token") → None（无 cookie）
    │
    ▼
抛出 HTTPException 401
    │
    ▼
axios 响应拦截器:
  error.response.status === 401 → true
  tokenCookie.value → 已是 null → if 条件 false → 不跳转 /login
    │
    ▼
request.safe() catch: { response:null, error:e, data:null }
    │
    ▼
saveRecipe() 静默失败
```

这是 6.8 节详细描述的路径，也是最常发生的路径。

---

**路径二：保存成功（时间窗口竞态）**

Tab B 保存请求在「Tab A 登出导致 cookie 被清除」之前就已经携带有效 token 发出，且在后端被处理时 JWT 仍未过期。具体有两种子场景：

**子场景 A：Tab B 请求先于 Tab A 登出发出**
```
Tab B 用户点击保存（时间点 T1）
    │
    ▼
axios 请求拦截器读到有效 token → 设置 Authorization header
    │
    ▼
HTTP 请求已进入网络层（携带着有效的 JWT）

Tab A 用户点击登出（时间点 T2 > T1，但请求还未返回）
    │
    ▼
signOut() 清 cookie + 跳转 /login
    │
    ▼
Tab B 的保存请求到达后端（时间点 T3）
    │
    ▼
jwt.decode() 验证：签名正确、exp 未过期 → 通过
    │
    ▼
Recipe 正常更新，返回 200
    │
    ▼
Tab B saveRecipe() 成功，退出编辑模式
    │
    ▼
（Tab B 后续的其他请求才会因 cookie 被清除而失败）
```

**子场景 B：Tab B 请求在 Tab A logout API 处理完成前到达后端**

Tab A 的 logout 请求和 Tab B 的保存请求同时在网络上传输，存在后端先处理 Tab B 保存请求的可能（取决于网络路由和后端调度顺序）。此时：
- Cookie Jar 尚未被 logout 响应的 Set-Cookie 更新
- Tab B 拦截器能读到 token，或浏览器自动附带了有效 cookie
- JWT 未被吊销（也无法被吊销）
- 保存成功

**路径二的触发概率**：
- 在用户手动操作场景下，子场景 A 偶发（两人同时操作）
- 子场景 B 是纯粹的网络竞态，概率极低但理论上存在
- 路径二一旦发生，Tab B 会误以为自己已登出（实际上 Tab B 的 authStatus 仍为 authenticated，但后续操作又会失败），造成更混乱的用户体验

---

**两条路径的根本原因总结**：

| 要素 | 路径一（失败） | 路径二（成功） |
|-----|--------------|--------------|
| Tab B 拦截器读到 token | ❌ null | ✅ 有效字符串 |
| 请求携带 Authorization header | ❌ 无 | ✅ Bearer <jwt> |
| 请求附带有效 cookie | ❌ 无 | ✅ 有 |
| 后端 jwt.decode() 结果 | —（未执行，无 token） | ✅ 签名正确、未过期 |
| saveRecipe() 结果 | 静默失败 | 正常保存 |
| 后续 Tab B 行为 | 仍在编辑模式，可继续改但都失败 | 退出编辑，回到 VIEW，下次请求才会失败 |

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
| axios 插件（请求/响应拦截器 + 401 分支） | `frontend/app/plugins/axios.ts` | 1-64 |
| API 请求包装层（request.safe 吞异常） | `frontend/app/composables/api/api-client.ts` | 8-66 |
| API 入口导出 | `frontend/app/composables/api/index.ts` | 1-2 |
| BaseCRUDAPI 抽象类 | `frontend/app/lib/api/base/base-clients.ts` | 16-82 |
| 后端 auth 路由（token/logout/refresh） | `mealie/routes/auth/auth.py` | 64-162 |
| JWT 创建（create_access_token） | `mealie/core/security/security.py` | 31-40 |
| 后端认证依赖（get_current_user + 双来源 token 读取） | `mealie/core/dependencies/dependencies.py` | 88-123 |
| Nuxt 主配置（ssr/experimental/cookie 相关） | `frontend/nuxt.config.ts` | 1-273 |
| 前端依赖版本（Nuxt 4.4.2 等） | `frontend/package.json` | 1-74 |
