# 用户偏好与前端状态优先级关系梳理

## 一、架构概览：偏好层级结构

Mealie 的偏好系统采用**多层级优先级**设计，从高到低依次为：

```
食谱自身设置 (Recipe.settings)
    ↓
用户本地浏览器偏好 (LocalStorage / SessionStorage)
    ↓
家庭级别偏好 (HouseholdPreferences)
    ↓
组级别偏好 (GroupPreferences) — 已废弃，仅作为创建家庭时的默认值来源
    ↓
代码硬编码默认值 (Pydantic/DB column defaults)
```

> **关键结论**：不存在独立的「用户偏好」后端数据表。用户个人偏好仅存储在前端浏览器 LocalStorage 中，而共享的食谱默认值存储在「家庭」层。

---

## 二、各层偏好详细说明

### 2.1 食谱自身设置 (Recipe.settings) — 最高优先级

**存储位置**：后端数据库 `recipes.settings`（JSON 字段）

**涉及代码**：
- 后端模型：[recipe/settings.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/recipe/settings.py)
- 前端类型：[recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/lib/api/types/recipe.ts)

**包含字段**：
| 字段 | 类型 | 说明 |
|------|------|------|
| `public` | boolean | 是否公开可见 |
| `showNutrition` | boolean | 是否显示营养信息 |
| `showAssets` | boolean | 是否显示附件资源 |
| `landscapeView` | boolean | 是否使用横屏布局 |
| `disableComments` | boolean | 是否禁用评论 |
| `locked` | boolean | 是否锁定（防止编辑） |

**读取逻辑**（食谱详情页）：
见 [RecipePage.vue#L396-L408](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L396-L408)
```typescript
const landscape = computed(() => {
  const preferLandscape = recipe.value.settings?.landscapeView;
  const smallScreen = !display.smAndUp.value;
  if (preferLandscape) return true;      // 优先使用食谱自身设置
  else if (smallScreen) return true;     // 其次响应式判断
  return false;
});
```

**创建时的默认值来源**：
创建新食谱时，若未显式指定 settings，则自动从**家庭偏好**继承。
见 [recipe_service.py#L208-L218](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/recipe/recipe_service.py#L208-L218)
```python
if isinstance(create_data, CreateRecipe) or create_data.settings is None:
    if self.household.preferences is not None:
        data.settings = RecipeSettings(
            public=self.household.preferences.recipe_public,
            show_nutrition=self.household.preferences.recipe_show_nutrition,
            show_assets=self.household.preferences.recipe_show_assets,
            landscape_view=self.household.preferences.recipe_landscape_view,
            disable_comments=self.household.preferences.recipe_disable_comments,
        )
    else:
        data.settings = RecipeSettings()  # 空态回退到代码默认值
```

---

### 2.2 用户本地浏览器偏好 (LocalStorage / SessionStorage)

**存储位置**：浏览器 `localStorage` 和 `sessionStorage`

**统一入口文件**：[preferences.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-users/preferences.ts)

所有偏好均使用 `@vueuse/core` 的 `useLocalStorage` / `useSessionStorage`，并设置 `{ mergeDefaults: true }` 选项（确保旧数据不会丢失新增字段）。

#### 完整偏好清单：

| Composable 函数 | Storage Key | 存储介质 | 字段 | 默认值 |
|-----------------|-------------|----------|------|--------|
| `useUserMealPlanPreferences()` | `meal-planner-preferences` | LocalStorage | `numberOfDaysPast` <br> `numberOfDays` | `0` <br> `7` |
| `useUserPrintPreferences()` | `recipe-print-preferences` | LocalStorage | `imagePosition` <br> `showDescription` <br> `showNotes` <br> `showNutrition` <br> `expandChildRecipes` | `"left"` <br> `true` <br> `true` <br> `false` <br> `false` |
| **`useUserSortPreferences()`** | **`recipe-section-preferences`** | **LocalStorage** | **`orderBy`** <br> **`orderDirection`** <br> **`filterNull`** <br> **`sortIcon`** <br> **`useMobileCards`** | **`"created_at"`** <br> **`"desc"`** <br> **`false`** <br> **图标引用** <br> **`false`** |
| `useUserActivityPreferences()` | `activity-preferences` | LocalStorage | `defaultActivity` | `ActivityKey.RECIPES` |
| `useUserSearchQuerySession()` | `search-query` | **SessionStorage** | `recipe` | `""` |
| `useShoppingListPreferences()` | `shopping-list-preferences` | LocalStorage | `viewAllLists` | `false` |
| `useTimelinePreferences()` | `timeline-preferences` | LocalStorage | `orderDirection` <br> `types` | `"asc"` <br> `["info","system","comment"]` |
| `useParsingPreferences()` | `parsing-preferences` | LocalStorage | `parser` | `"nlp"` |
| `useCookbookPreferences()` | `cookbook-preferences` | LocalStorage | `hideOtherHouseholds` | `false` |
| `useRecipeFinderPreferences()` | `recipe-finder-preferences` | LocalStorage | `foodIds` <br> `toolIds` <br> `queryFilter` <br> `queryFilterJSON` <br> `maxMissingFoods` <br> `maxMissingTools` <br> `includeFoodsOnHand` <br> `includeToolsOnHand` | `[]` <br> `[]` <br> `""` <br> `{parts:[]}` <br> `20` <br> `20` <br> `true` <br> `true` |
| `useRecipeCreatePreferences()` | `recipe-create-preferences` | LocalStorage | `importKeywordsAsTags` <br> `importCategories` <br> `stayInEditMode` <br> `parseRecipe` | `false` <br> `false` <br> `false` <br> `true` |
| `useUserExperiencePreferences()` | `user-experience-preferences` | LocalStorage | `lockScreen` | `true` |

#### 列表默认视图的实际使用：

在 [RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue) 中：

- **排序字段/方向**：`preferences.value.orderBy`、`preferences.value.orderDirection`
  - 见 [RecipeCardSection.vue#L261-L263](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue#L261-L263)

- **卡片视图切换（紧凑/卡片）**：`preferences.value.useMobileCards`
  - 见 [RecipeCardSection.vue#L219-L221](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue#L219-L221)
  ```typescript
  const useMobileCards = computed(() => {
    return display.smAndDown.value || preferences.value.useMobileCards;
  });
  ```
  > 注意：小屏设备会**强制**使用紧凑视图，覆盖用户偏好

- **切换函数**：`toggleMobileCards()` 直接写入 LocalStorage
  - 见 [RecipeCardSection.vue#L456-L458](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue#L456-L458)

---

### 2.3 家庭级别偏好 (HouseholdPreferences)

**存储位置**：后端数据库 `household_preferences` 表

**后端模型**：[preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/household/preferences.py)

**后端 Schema**：[household_preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/schema/household/household_preferences.py)

**前端类型**：[household.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/lib/api/types/household.ts)

**包含字段**：
| 字段（camelCase） | 字段（snake_case） | 类型 | 默认值 | 说明 |
|-------------------|---------------------|------|--------|------|
| `privateHousehold` | `private_household` | boolean | `True` | 家庭是否私有 |
| `showAnnouncements` | `show_announcements` | boolean | `True` | 是否显示系统公告 |
| `lockRecipeEditsFromOtherHouseholds` | `lock_recipe_edits_from_other_households` | boolean | `True` | 锁定其他家庭编辑本家庭食谱 |
| `firstDayOfWeek` | `first_day_of_week` | int | `0` (周日) | 周起始日 |
| `recipePublic` | `recipe_public` | boolean | `True` | 新食谱默认是否公开 |
| `recipeShowNutrition` | `recipe_show_nutrition` | boolean | `False` | 新食谱默认显示营养 |
| `recipeShowAssets` | `recipe_show_assets` | boolean | `False` | 新食谱默认显示附件 |
| `recipeLandscapeView` | `recipe_landscape_view` | boolean | `False` | 新食谱默认横屏视图 |
| `recipeDisableComments` | `recipe_disable_comments` | boolean | `False` | 新食谱默认禁用评论 |

#### 读取和保存 API：

**路由**（后端）：[controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/households/controller_household_self_service.py)
- `GET /api/households/preferences` → `get_household_preferences()`
- `PUT /api/households/preferences` → `update_household_preferences()`（需要 `can_manage_household` 权限）

**前端 API 封装**：[households.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/lib/api/user/households.ts#L46-L49)
```typescript
async setPreferences(payload: UpdateHouseholdPreferences) {
  return await this.requests.put<ReadHouseholdPreferences, UpdateHouseholdPreferences>(
    routes.preferences, payload
  );
}
```

**前端状态管理**：[use-households.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L7-L51)
- `useHouseholdSelf()` 返回响应式的 `household` 对象（包含 `preferences`）
- `actions.updatePreferences()` 调用 API 保存并更新本地状态

**UI 编辑组件**：
- [HouseholdPreferencesEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue)
- [household/index.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/household/index.vue)（管理页面，需 `can-manage-household-only` 中间件）

#### 缓存处理（修正版）：

家庭当前用户家庭数据通过 `useHouseholdSelf()` 进行**模块级单例缓存**：

```typescript
// use-households.ts 第4行 — 模块作用域 ref，单例缓存
const householdSelfRef = ref<HouseholdInDB | null>(null);
```

- **首次调用**时懒加载（`refreshHouseholdSelf()`）
- **清理时机**：⚠️ `clearAllStores()` **不包含** `householdSelfRef` 的清理。它仅在以下情况被清空：
  1. `refreshHouseholdSelf()` 被调用且检测到 `!auth.user.value`（见 [use-households.ts#L11-L14](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L11-L14)）
  2. 下次调用 `useHouseholdSelf()` 时触发懒加载前的检查

> **重要区分**：`useHouseholdStore()`（[use-household-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/store/use-household-store.ts)）是家庭**列表**缓存，属于 `clearAllStores()` 管理范畴；而 `useHouseholdSelf()` 返回的是**当前登录用户所属家庭**的单例数据，与前者是两套独立缓存。

#### 空态处理：

家庭创建时，如果父组存在偏好，则从组偏好继承部分字段：
见 [household_service.py#L43-L49](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/household_services/household_service.py#L43-L49)
```python
if group and group.preferences:
    prefs = CreateHouseholdPreferences(
        private_household=group.preferences.private_group,
        recipe_public=not group.preferences.private_group,
    )
else:
    prefs = CreateHouseholdPreferences()  # 回退到代码默认值
```

---

### 2.4 组级别偏好 (GroupPreferences) — 已废弃

**存储位置**：后端数据库 `group_preferences` 表

**后端模型**：[group/preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/group/preferences.py)

**前端状态管理**：[use-groups.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts#L7-L69)

> ⚠️ 与 `householdSelfRef` 相同的缓存结构：`groupSelfRef` 是模块级单例，**不在 `clearAllStores()` 清理范围内**。

> ⚠️ **注意**：除 `private_group` 和 `show_announcements` 外，其余字段（食谱相关默认值）已标注 **Deprecated**，代码注释明确指出「see household preferences」。这些字段目前仅作为创建新家庭时的默认值来源（见上一节）。

**包含字段**：
| 字段 | 类型 | 默认值 | 状态 |
|------|------|--------|------|
| `private_group` | boolean | `True` | ✅ 有效 |
| `show_announcements` | boolean | `True` | ✅ 有效 |
| `first_day_of_week` | int | `0` | ❌ Deprecated |
| `recipe_public` | boolean | `True` | ❌ Deprecated |
| `recipe_show_nutrition` | boolean | `False` | ❌ Deprecated |
| `recipe_show_assets` | boolean | `False` | ❌ Deprecated |
| `recipe_landscape_view` | boolean | `False` | ❌ Deprecated |
| `recipe_disable_comments` | boolean | `False` | ❌ Deprecated |
| `recipe_disable_amount` | boolean | `True` | ❌ Deprecated |

**API 路由**：[controller_group_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/groups/controller_group_self_service.py#L54-L60)
- `GET /api/groups/preferences`
- `PUT /api/groups/preferences`

---

### 2.5 User 模型自身的偏好字段

User 模型上存在少量直接存储的偏好字段（非独立 preference 表）：

见 [users.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/users/users.py)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `show_announcements` | boolean | `True` | 用户级公告开关（独立于家庭/组） |
| `last_read_announcement` | string | `null` | 已读公告 ID |
| `advanced` | boolean | `False` | 是否启用高级模式 |
| `cache_key` | string | `"1234"` | 用户数据缓存键（用于前端缓存失效） |

前端编辑入口：[user/profile/edit.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/user/profile/edit.vue#L169-L201)

---

## 三、主题 (Theme) 与语言 (Language/Locale)

### 3.1 主题 (Theme / Dark Mode)

#### 亮暗模式与主题色的关系

亮暗模式（dark/light）与主题色（primary/accent 等具体色值）是**两套独立机制**：

| 维度 | 亮暗模式切换 | 主题色配置 |
|------|-------------|-----------|
| 控制内容 | Vuetify 的 `theme.global.name`（`"dark"` 或 `"light"`） | 每个主题（dark/light）下的 `colors.primary` 等具体色值 |
| 存储位置 | `localStorage['vueuse-color-scheme']` | 后端 API + 前端 `__cachedTheme` 内存缓存 + HTTP 7 天缓存 |
| 实现插件 | [dark-mode.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts) | [theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts) |
| 切换方式 | `useDark()` 自动管理，`onChanged` 时调用 `vuetify.theme.toggle()` | 页面加载时一次性读取（需刷新页面生效） |
| 登出行为 | **不清理** localStorage | **不清理** `__cachedTheme` 与 HTTP 缓存 |

**亮暗模式优先级**：
```
localStorage['vueuse-color-scheme'] ('dark' | 'light' | 'auto')
    ↓  (当 localStorage 非 'dark' 且非 'light' 时)
系统偏好 (prefers-color-scheme: dark)
    ↓  (Vuetify 初始化时的默认主题)
nuxt.config.ts runtimeConfig.public.useDark
    (来源: Boolean(process.env.THEME_USE_DARK) || false，见 [nuxt.config.ts#L86](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L86))
```

**亮暗模式初始化流程**：
1. [theme.ts#L41](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L41)：`vuetify:before-create` 钩子中根据 `$config.public.useDark` 设置 `defaultTheme`
2. [dark-mode.client.ts#L13-L15](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts#L13-L15)：`vuetify:ready` 钩子中根据 `useDark()` 当前值调用 `vuetify.theme.change()` 覆盖默认值
3. [dark-mode.client.ts#L5-L10](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts#L5-L10)：用户切换时 `onChanged` 回调调用 `vuetify.theme.toggle()`

**页面加载时的防闪烁**：
在 [nuxt.config.ts#L53-L57](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L53-L57)，通过注入内联脚本在渲染前读取 `vueuse-color-scheme` 并设置 `document.documentElement.style.backgroundColor`（`#1E1E1E` 或 `#FFFFFF`）。

---

#### 主题颜色的完整取值链（校准版）

##### 第一层：`nuxt.config.ts` 的 runtimeConfig.public.themes（存在但未被使用）

**确认存在**：[nuxt.config.ts#L87-L107](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L87-L107) 中确实定义了完整的颜色对象：

```typescript
// nuxt.config.ts — runtimeConfig.public
themes: {
  dark: {
    primary: process.env.THEME_DARK_PRIMARY || "#E58325",
    accent: process.env.THEME_DARK_ACCENT || "#007A99",
    secondary: process.env.THEME_DARK_SECONDARY || "#973542",
    success: process.env.THEME_DARK_SUCCESS || "#43A047",
    info: process.env.THEME_DARK_INFO || "#1976d2",
    warning: process.env.THEME_DARK_WARNING || "#FF6D00",
    error: process.env.THEME_DARK_ERROR || "#EF5350",
    background: "#1E1E1E",
  },
  light: {
    primary: process.env.THEME_LIGHT_PRIMARY || "#E58325",
    accent: process.env.THEME_LIGHT_ACCENT || "#007A99",
    // ... 其他颜色
  },
},
```

> ⚠️ **关键事实**：经全局搜索（`config.public.themes`、`runtimeConfig.public.themes`、`themes.dark.`、`themes.light.`）确认，**整个 frontend 目录没有任何代码读取该对象**。它是**死代码**——定义了环境变量 fallback 链，但 theme 插件完全未使用。

##### 第二层：后端配置与 API（实际被读取）

**后端 Theme 类**：[themes.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/core/settings/themes.py)
```python
class Theme(BaseSettings):
    light_primary: str = "#E58325"
    light_accent: str = "#007A99"
    # ... 共 14 个字段（light_* × 7 + dark_* × 7）
    model_config = SettingsConfigDict(env_prefix="theme_", extra="allow")
```

后端取值优先级：
```
环境变量 (THEME_LIGHT_PRIMARY, THEME_DARK_PRIMARY, ...)
    ↓  (通过 pydantic SettingsConfigDict env_prefix="theme_" 自动读取)
Theme 类硬编码默认值（如 light_primary="#E58325"）
```

**API 暴露**：[app_about.py#L66-L72](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/app/app_about.py#L66-L72)
- 路由：`GET /api/app/about/theme`
- 返回值：`AppTheme(**settings.theme.model_dump())`
- HTTP 响应头：`Cache-Control: public, max-age=604800`（7 天浏览器/CDN 缓存）

##### 第三层：前端 theme.ts 插件的读取与回退（实际生效链路）

[theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts)

取值优先级（前端实际生效链路）：
```
模块级变量 __cachedTheme (前端插件生命周期内的内存缓存，[theme.ts#L18](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L18))
    ↓  (首次加载或缓存失效时)
fetch("/api/app/about/theme") + HTTP 7 天缓存 ([theme.ts#L20-L31](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L31))
    ↓  (fetch 失败 / 返回 undefined 时)
theme.ts 内联硬编码十六进制颜色值（如 "#E58325"、"#007A99" 等，[theme.ts#L51-L69](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L51-L69)）
```

**theme.ts 回退代码片段**：
```typescript
// theme.ts 第 51-57 行 — light 主题颜色
colors: {
  primary:   theme?.lightPrimary   ?? "#E58325",   // 硬编码 fallback
  accent:    theme?.lightAccent    ?? "#007A99",   // 硬编码 fallback
  secondary: theme?.lightSecondary ?? "#973542",   // 硬编码 fallback
  // ...
}
```

> ⚠️ **再次确认**：theme.ts 仅读取了 `nuxtApp.$config.public.useDark`（用于设置默认亮暗主题），**完全没有读取** `nuxtApp.$config.public.themes` 颜色对象。

---

#### 亮暗模式、主题色缓存、语言请求头三者的关系校准

| 特性 | 亮暗模式 (Dark Mode) | 主题色 (Theme Colors) | 语言请求头 (Accept-Language) |
|------|---------------------|----------------------|------------------------------|
| **核心功能** | 切换 light/dark 主题 | 设置每个主题下的色板 | 告知后端用户偏好语言 |
| **存储介质** | localStorage (`vueuse-color-scheme`) | ① 模块级变量 `__cachedTheme` <br> ② HTTP `Cache-Control: max-age=604800` | ① i18n Cookie (由 `@nuxtjs/i18n` 管理) <br> ② `$axios.defaults.headers.common` |
| **写入时机** | 用户切换时自动写入 | 页面首次加载时 fetch 并缓存 | ① 用户切换 locale 时 i18n 自动写 Cookie <br> ② **每次**调用 `useUserApi()`/`usePublicApi()` 等时更新 axios 默认头 |
| **设置机制** | `@vueuse/core` 的 `useDark()` + Vuetify `theme.change()`/`toggle()` | Nuxt 插件 `vuetify:before-create` 钩子一次性注入 | `useRequests()` 中修改 `$axios.defaults.headers.common`（非拦截器） |
| **与后端 API 关系** | 纯前端，无 API | 依赖 `/api/app/about/theme`（GET，公开） | 依赖每个请求的 `Accept-Language` 头，后端中间件读取 |
| **登出行为** | ❌ **不清理** localStorage | ❌ **不清理** `__cachedTheme` 与 HTTP 缓存 | ❌ **不清理** i18n Cookie 与 axios 默认头 |
| **互相依赖** | — | 依赖亮暗模式决定最终使用 light 还是 dark 色板 | 独立，与主题系统无耦合 |

**语言请求头的完整传递链路**：
```
用户切换语言 (useLocales().locale.value = "zh-CN")
    → i18n.setLocale("zh-CN")
    → @nuxtjs/i18n 自动写入 i18n Cookie (detectBrowserLanguage.useCookie=true)
    → 后续任意组件调用 useUserApi() / usePublicApi() / useAdminApi() / usePublicExploreApi()
        → useRequests() 执行:
            $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value
            ([api-client.ts#L63](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/api/api-client.ts#L63))
    → 后端 LocaleContextMiddleware 从 request.headers["accept-language"] 读取
        → 传给带 @lru_cache 的 ProviderFactory
        → 若语言不支持，fallback 到 en-US
```

> **重要区分**：[axios.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/axios.ts) 中的 axios 拦截器**仅处理 Authorization token 和 401 响应**，不涉及 Accept-Language。

---

### 3.2 语言 / Locale

#### 前端语言管理

**配置**：[nuxt.config.ts#L152-L214](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L152-L214)
- 默认 locale：`en-US`
- fallback locale：`en-US`
- 语言策略：`strategy: "no_prefix"`（URL 中不包含语言代码）
- 懒加载：`lazy: true`

**Cookie 持久化（自动）**：
```typescript
// nuxt.config.ts 第 204-208 行
detectBrowserLanguage: {
  useCookie: true,           // 将检测/切换结果自动持久化到 Cookie
  alwaysRedirect: true,
  fallbackLocale: "en-US",
}
```

**语言切换与请求头传递（修正版）**：

语言切换调用链：

```
用户选择语言
    → useLocales().locale.value = "zh-CN"
    → i18n.setLocale("zh-CN")  (use-locales.ts#L12)
    → @nuxtjs/i18n 自动写入 i18n Cookie
    → 后续任意组件调用 useUserApi() / usePublicApi() 时：
        $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value
        (见 api-client.ts#L63)
```

**关键代码细节**：

见 [api-client.ts#L57-L66](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/api/api-client.ts#L57-L66)
```typescript
export const useRequests = function (i18n?: Composer): ApiRequestInstance {
  const { $axios } = useNuxtApp();
  if (!i18n) {
    i18n = useGlobalI18n();
  }
  // 直接修改 axios 全局实例的默认请求头
  $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value;
  return getRequests($axios);
};
```

> **重要说明**：`Accept-Language` 请求头**不是通过 axios 拦截器设置**的（[axios.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/axios.ts) 拦截器仅处理 Authorization token 和 401 响应）。它通过修改 `$axios.defaults.headers.common` 全局生效，每次调用 `useUserApi()` / `usePublicApi()` / `useAdminApi()` / `usePublicExploreApi()` 时都会用**当前** `i18n.locale.value` 更新该默认头。

**前端语言存储优先级**：
```
i18n Cookie (由 detectBrowserLanguage.useCookie=true 自动管理)
    ↓
浏览器 Accept-Language header (首次访问时检测)
    ↓
默认 'en-US'
```

**Composable**：[use-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/use-locales.ts)
- 同时监听 `locale` 变化，同步更新 Vuetify 的 locale

**i18n 实例缓存**：
见 [use-global-i18n.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-global-i18n.ts#L3-L9)
```typescript
let i18n: Composer | null = null;
export function useGlobalI18n() {
  if (!i18n) {
    i18n = useI18n();   // 模块级单例缓存
  }
  return i18n;
}
```
> 该单例在页面生命周期内不随登出被清理。

#### 后端语言管理

**中间件**：[locale_context.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/middleware/locale_context.py)
- 从请求头 `Accept-Language` 读取语言
- 通过 ContextVar 注入到当前请求上下文

**语言 Provider**：[providers.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py)
- 使用 `@lru_cache` 缓存翻译 Provider 工厂（进程生命周期内）
- fallback locale：`en-US`
- 若请求的语言不在支持列表中，回退到 `en-US`

```python
@lru_cache
def _load_factory() -> i18n.ProviderFactory:
    return i18n.ProviderFactory(
        directory=TRANSLATIONS,
        fallback_locale="en-US",
    )
```

#### 多语言格式的影响

语言代码格式统一为 `xx-YY`（如 `zh-CN`、`en-US`），对大小写敏感。

**受 locale 影响的功能**：
| 功能 | 说明 | 相关文件 |
|------|------|----------|
| 前端 UI 文本翻译 | i18n 消息 JSON | [lang/messages/](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/lang/messages/) |
| 日期时间格式 | 各 locale 独立的 datetimeFormats | [i18n.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/i18n.config.ts) |
| 食材/单位/标签种子数据 | 导入时选择 locale | [group/data/foods.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/group/data/foods.vue) |
| 食谱名称翻译 | OCR/AI 翻译时使用当前 locale | [r/create/image.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/g/[groupSlug]/r/create/image.vue#L147) |
| 后端邮件/通知文本 | 根据请求头 Accept-Language | [auth.py#L157-L161](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/auth/auth.py#L157-L161) |
| 名词复数处理 | 不同 locale 有不同规则（`pluralFoodHandling`） | [available-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/available-locales.ts) |
| 文本方向 (LTR/RTL) | 如阿拉伯语 `ar-SA` 为 RTL | [available-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/available-locales.ts#L284-L289) |

---

## 四、缓存处理与登出状态保留风险

### 4.1 模块级单例缓存总览

前端存在多处**模块作用域**的变量（在 ES 模块顶层 `const xxx = ref(...)`），它们是独立于 Vue 组件树之外的单例缓存：

| 数据 | 定义位置 | 类型 | 登出时是否被 `clearAllStores()` 清理 |
|------|----------|------|--------------------------------------|
| `householdSelfRef` | [use-households.ts#L4](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L4) | `Ref<HouseholdInDB \| null>` | ❌ **否** |
| `groupSelfRef` | [use-groups.ts#L4](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts#L4) | `Ref<GroupSummary \| null>` | ❌ **否** |
| `__cachedTheme` | [theme.ts#L18](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L18) | `ThemeConfig \| undefined` | ❌ **否** |
| `i18n` | [use-global-i18n.ts#L3](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-global-i18n.ts#L3) | `Composer \| null` | ❌ **否** |
| `authUser` | [use-auth-backend.ts#L24](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L24) | `Ref<UserOut \| null>` | ✅ 是（`signOut()` 中直接赋值 null） |
| `authStatus` | [use-auth-backend.ts#L25](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L25) | `Ref<string>` | ✅ 是 |
| 9 个列表 store（`useCategoryStore` 等） | [composables/store/](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/store/) | 多个 `Ref<T[]>` 等 | ✅ 是（`clearAllStores()` 逐个调用 `resetXxxStore()`） |

### 4.2 登出流程全解析

见 [use-auth-backend.ts#L94-L115](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L94-L115)

```typescript
async function signOut(callbackUrl: string = ""): Promise<void> {
  try {
    await $axios.post("/api/auth/logout");   // 通知后端失效 token
  } catch (error) {
    // API 失败仍继续登出
  } finally {
    setToken(null);                          // ✅ 1. 清空 auth token Cookie
    authUser.value = null;                   // ✅ 2. 清空用户信息 ref
    authStatus.value = "unauthenticated";    // ✅ 3. 更新状态

    clearAllStores();                        // ✅ 4. 清空 9 个列表 store（不包含 householdSelfRef / groupSelfRef）
    clearNuxtData();                         // ✅ 5. 清空 Nuxt useAsyncData 缓存

    await router.push(callbackUrl || "/login");  // 跳转登录页
  }
}
```

### 4.3 登出后的状态保留风险（关键修正）

| 数据类型 | 登出后是否残留 | 说明 / 风险 |
|----------|----------------|-------------|
| **Auth Token Cookie** | ❌ 清理 | `setToken(null)` 直接清空，不存在残留 |
| **authUser / authStatus** | ❌ 清理 | 直接赋值 null / unauthenticated |
| **9 个列表 store** | ❌ 清理 | `clearAllStores()` 逐个重置 |
| **Nuxt useAsyncData 缓存** | ❌ 清理 | `clearNuxtData()` 清空 |
| **`householdSelfRef`** | ⚠️ **可能残留** | 仅当 `refreshHouseholdSelf()` 被调用且检测到 `!auth.user.value` 时才会设为 null。若登出后不再触发该函数，则保留上一个用户的家庭数据（含 preferences）。 |
| **`groupSelfRef`** | ⚠️ **可能残留** | 同上，仅在 `refreshGroupSelf()` 调用时清理。 |
| **LocalStorage 用户偏好（12 个 composable）** | ⚠️ **永久残留** | 存储于浏览器 LocalStorage，登出流程**不清理**任何 LocalStorage 数据。切换用户后，新用户将继承前一用户的排序、视图、打印等偏好。 |
| **主题色缓存 `__cachedTheme`** | ⚠️ 残留 | 插件生命周期内不清理，但属于全局配置，无敏感数据风险。 |
| **暗模式设置 `vueuse-color-scheme`** | ⚠️ 永久残留 | 存储于 LocalStorage，登出不清理。 |
| **i18n Cookie（语言偏好）** | ⚠️ 永久残留 | `@nuxtjs/i18n` 管理的 Cookie，登出不清理。 |
| **`useGlobalI18n()` 单例** | ⚠️ 残留 | 模块级缓存，登出不清理，但为只读配置对象，无风险。 |

> **潜在数据泄露场景**：A 用户登出后，若 B 用户在同一浏览器标签页（未刷新）登录，在 B 用户的 `useHouseholdSelf()` / `useGroupSelf()` 首次懒加载触发之前，代码中任何对 `householdSelfRef.value` / `groupSelfRef.value` 的直接访问仍会读到 A 用户的数据。

---

### 4.3.1 householdSelfRef / groupSelfRef 缓存失效路径详解

#### 一、核心代码结构

两个模块的实现逻辑完全对称（[use-households.ts#L4-L51](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L4-L51)、[use-groups.ts#L4-L69](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts#L4-L69)）：

```typescript
// 模块级单例 ref
const householdSelfRef = ref<HouseholdInDB | null>(null);
const loading = ref(false);

export const useHouseholdSelf = function () {
  const auth = useMealieAuth();

  async function refreshHouseholdSelf() {
    if (!auth.user.value) {           // [短路分支] 无登录用户 → 清空 ref 并返回
      householdSelfRef.value = null;
      return;
    }
    loading.value = true;
    const { data } = await api.households.getCurrentUserHousehold();
    householdSelfRef.value = data;    // 写入新数据
    loading.value = false;
  }

  const actions = {
    get() {
      // [关键逻辑] 只有当 ref 为空且不在加载中时，才触发 refresh
      if (!(householdSelfRef.value || loading.value)) {
        refreshHouseholdSelf();
      }
      return householdSelfRef;        // 同步返回 ref（可能是旧值）
    },
    async updatePreferences() { /* ... */ },
    // ❌ 注意：useHouseholdSelf 未暴露 refresh() 方法
  };

  const household = actions.get();    // 组件调用 useHouseholdSelf() 时立即触发 get()
  return { actions, household };
};
```

> **不对称点**：`useGroupSelf` 在 [use-groups.ts#L61-L63](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts#L61-L63) 暴露了 `actions.refresh()`，但 `useHouseholdSelf` 的 actions 中**没有暴露** refresh 方法。

#### 二、get() 短路返回真值表

`get()` 的判断条件：`if (!(householdSelfRef.value || loading.value))`

| `householdSelfRef.value` | `loading.value` | 条件求值 | 是否触发 `refreshHouseholdSelf()` |
|--------------------------|-----------------|----------|-----------------------------------|
| `null`                   | `false`         | `!false` → `true` | ✅ **是**（首次调用、页面刷新后） |
| `null`                   | `true`          | `!true` → `false` | ❌ 否（加载中，避免重复请求） |
| `{data}`（非 null）      | `false`         | `!true` → `false` | ❌ **否**（已有数据，短路返回旧值） |
| `{data}`（非 null）      | `true`          | `!true` → `false` | ❌ 否 |

> **关键结论**：一旦 ref 被赋过一次非 null 值，后续所有 `get()` 调用都会**短路返回旧值**，永远不会主动重新请求 API，除非外部显式调用 `refreshHouseholdSelf()`（但 `useHouseholdSelf` 并未暴露此方法）。

---

#### 三、各场景下的失效行为

##### 场景 1：用户登出（signOut）

**调用链**：
1. [use-auth-backend.ts#L94-L115](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L94-L115) → `signOut()`
2. 执行：`setToken(null)` → `authUser.value = null` → `authStatus.value = "unauthenticated"` → `clearAllStores()` → `clearNuxtData()` → `router.push("/login")`

**对 householdSelfRef / groupSelfRef 的影响**：

| 步骤 | 影响 |
|------|------|
| `authUser.value = null` | 通过 [use-mealie-auth.ts#L13-L24](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-mealie-auth.ts#L13-L24) 的 watcher → `auth.user.value` 变为 null |
| `clearAllStores()` | 清理 9 个列表 store，**不包含** householdSelfRef / groupSelfRef |
| `signOut()` 中 | ❌ **不调用** `refreshHouseholdSelf()` 或 `refreshGroupSelf()` |

**结果**：
- 如果登出前 `householdSelfRef.value` 已有值（非 null）
- 登出后没有任何代码触发 `refreshHouseholdSelf()`
- `get()` 的条件 `!(householdSelfRef.value || loading.value)` 为 false（因为 ref 有值）
- **旧数据保留在内存中**，不会被自动清空
- 只有当某个组件后续再次调用 `useHouseholdSelf()` → `get()`，且在此之前 ref 被其他路径置为 null，才会触发 refresh

---

##### 场景 2：切换用户（A 登出 → B 登录，同标签页未刷新）

**完整时序**：

| 步骤 | 操作 | `auth.user.value` | `householdSelfRef.value` | `get()` 是否触发 refresh |
|------|------|-------------------|--------------------------|--------------------------|
| T0 | 用户 A 正常使用，已调用过 `useHouseholdSelf()` | A 用户对象 | **A 家庭数据**（非 null） | — |
| T1 | 用户 A 点击登出，执行 `signOut()` | `null` | **仍为 A 家庭数据**（未被清空） | ❌ 不触发（ref 仍有值） |
| T2 | 跳转到 `/login` 页，页面未刷新 | `null` | **仍为 A 家庭数据** | — |
| T3 | 用户 B 输入凭据登录，`signIn()` → `getSession()` | **B 用户对象** | **仍为 A 家庭数据** | — |
| T4 | 跳转到主页，某个组件挂载时调用 `useHouseholdSelf()` → `actions.get()` | B 用户对象 | **仍为 A 家庭数据** | ❌ **不触发**（因为 `householdSelfRef.value` 非 null，条件短路） |
| T5 | 组件渲染时访问 `household.value.preferences` | B 用户对象 | **返回 A 的家庭偏好数据** | — |

> **实际 Bug 场景**：T4-T5 之间，新登录的用户 B 会读取到用户 A 的家庭数据（含 preferences、privateHousehold 等敏感设置），直到页面刷新或有人显式调用 `refreshHouseholdSelf()`。

**风险加重因素**：
- `useHouseholdSelf` 的 actions **未暴露 refresh() 方法**，外部无法主动刷新
- `get()` 是同步返回的，组件不会等待异步 refresh 完成

---

##### 场景 3：get() 短路返回（已有缓存值）

一旦 `householdSelfRef.value` 被赋过一次值：

```typescript
// 组件 A 首次调用
const { household } = useHouseholdSelf();
// → get() → ref=null → 触发 refresh → 写入 A 的家庭数据

// 组件 B 在同生命周期内调用
const { household } = useHouseholdSelf();
// → get() → ref=A家庭数据 → 条件不满足 → 短路返回旧值
// → ✅ 正常，避免重复请求

// 用户登出 → B 登录后，组件 C 调用
const { household } = useHouseholdSelf();
// → get() → ref=A家庭数据 → 条件不满足 → 短路返回旧值
// → ❌ Bug：返回 A 的家庭数据给 B 用户
```

---

##### 场景 4：手动刷新（UI 层面）

| 缓存类型 | 是否提供手动刷新方法 | 能否主动清除/刷新 |
|-----------|---------------------|-------------------|
| householdSelfRef | ❌ actions 中无 refresh() | ❌ 无法手动刷新，除非刷新页面 |
| groupSelfRef | ✅ `actions.refresh()`（[use-groups.ts#L61-L63](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts#L61-L63)） | ✅ 可调用，但当前代码中无任何调用点 |
| 9 个列表 store | ✅ 各 store 自有的 `flushStore()` | ✅ 可通过 `clearAllStores()` 统一清空 |
| LocalStorage 用户偏好 | 无需（用户修改时自动写入） | 需浏览器手动清除 |

---

##### 场景 5：页面刷新（F5 / Ctrl+R）

**行为**：
1. 浏览器重新加载，整个 JS 运行时销毁重建
2. 所有模块级 ref 重新初始化为 `null`：
   - `householdSelfRef = ref(null)`
   - `groupSelfRef = ref(null)`
   - `authUser = ref(null)`
   - `__cachedTheme = undefined`
   - `i18n = null`
3. 组件重新挂载 → 调用 `useHouseholdSelf()` → `actions.get()`：
   - `householdSelfRef.value` 为 `null`
   - `loading.value` 为 `false`
   - 条件满足 → 触发 `refreshHouseholdSelf()`
   - 检查 `auth.user.value`：
     - 若有有效 cookie：调用 API 获取当前用户家庭数据
     - 若无有效 cookie：`!auth.user.value` → 置 `householdSelfRef.value = null` 并返回

> ✅ **页面刷新是唯一可靠的缓存失效方式**，能确保 householdSelfRef / groupSelfRef 与当前登录用户完全同步。

---

#### 四、各缓存风险关系校准

| 缓存类型 | 存储介质 | 登出后是否残留 | 切换用户（同标签页）风险 | 泄露数据敏感度 |
|----------|----------|----------------|-------------------------|---------------|
| **householdSelfRef** | 模块级 ref（JS 内存） | ⚠️ **可能残留** | 🔴 **高**：返回 A 用户家庭数据给 B 用户（实际 Bug） | 高（preferences、私有设置） |
| **groupSelfRef** | 模块级 ref（JS 内存） | ⚠️ 可能残留 | 🔴 高：同上 | 中（preferences、AI Provider 设置） |
| LocalStorage 用户偏好（12 类） | 浏览器 LocalStorage | ⚠️ **永久残留** | 🟡 中：B 用户继承 A 用户的 UI 偏好（排序、视图、打印等） | 低（仅 UI 偏好） |
| 主题色 `__cachedTheme` | 模块级变量（JS 内存） | ⚠️ 残留 | 🟢 低：全局配置，无用户区分 | 无 |
| 主题色 HTTP 缓存 | 浏览器 HTTP Cache（`Cache-Control: max-age=604800`） | ⚠️ 永久残留（7 天） | 🟢 低：全局配置 | 无 |
| 暗模式 `vueuse-color-scheme` | 浏览器 LocalStorage | ⚠️ 永久残留 | 🟡 低：B 用户继承 A 的亮/暗主题选择 | 无 |
| i18n Cookie | 浏览器 Cookie（`@nuxtjs/i18n` 管理） | ⚠️ 永久残留 | 🟡 低：B 用户继承 A 的语言选择 | 无 |
| `$axios.defaults.headers.common["Accept-Language"]` | axios 全局实例（JS 内存） | ⚠️ 残留上次设置 | 🟢 低：下次调用 `useXxxApi()` 时自动更新为当前 locale | 无 |
| Auth Token Cookie | 浏览器 Cookie | ❌ 清理（`setToken(null)`） | ✅ 无 | — |
| authUser / authStatus | 模块级 ref（JS 内存） | ❌ 清理（直接赋值 null） | ✅ 无（`getSession()` 重新获取） | — |
| 9 个列表 store | 模块级 ref（JS 内存） | ❌ 清理（`clearAllStores()`） | ✅ 无（首次访问时重新请求） | — |

---

### 4.4 各类数据的缓存方式与失效触发汇总

| 数据 | 缓存方式 | 失效触发 |
|------|----------|----------|
| 用户数据 (UserOut) | 内存级 `authUser` ref | 登出、`auth.refresh()`、401 响应 |
| 当前用户家庭数据 (含 preferences) | 模块级 `householdSelfRef` ref | `refreshHouseholdSelf()` 调用且无登录用户时、页面刷新 |
| 当前用户组数据 (含 preferences) | 模块级 `groupSelfRef` ref | `refreshGroupSelf()` 调用且无登录用户时、页面刷新 |
| 家庭/组/分类/食材等**列表**缓存 | 9 个 store 模块 ref | `clearAllStores()`（登出时调用）、各 store 自有的 `flushStore()` |
| 用户 UI 偏好（排序、视图、打印等） | `localStorage`（12 个 key） | 用户手动修改（自动同步）、浏览器手动清除、**登出不清理** |
| 主题色配置 | ① 模块级 `__cachedTheme` 变量（前端内存缓存）<br>② HTTP `Cache-Control: public, max-age=604800`（浏览器 7 天缓存）<br>③ theme.ts 内联硬编码 fallback（未使用 nuxt.config.ts 中的 `public.themes`） | 页面刷新（JS 变量重置）、7 天后 HTTP 缓存失效、API 失败时自动回退到硬编码色值 |
| 亮/暗模式 | `localStorage['vueuse-color-scheme']` | 用户切换主题、浏览器手动清除、**登出不清理** |
| 语言偏好 | i18n Cookie + `$axios.defaults.headers.common["Accept-Language"]` | 用户切换语言（写 Cookie 和默认请求头）、浏览器清除 Cookie、**登出不清理** |
| 前端语言翻译文件 | `@nuxtjs/i18n` 内部缓存（懒加载） | 切换 locale 时按需重新加载 |
| 后端翻译 Provider | Python `@lru_cache` | 进程生命周期（服务重启才失效） |

---

### 4.5 空态（无数据）情况处理

| 场景 | 处理方式 | 代码位置 |
|------|----------|----------|
| `household.preferences` 为 null | 前端渲染 `HouseholdPreferencesEditor` 前有 `v-if` 保护 | [HouseholdPreferencesEditor.vue#L2](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue#L2) |
| 创建食谱时 `household.preferences` 为 null | 回退到 `RecipeSettings()` 无参构造（即代码默认值） | [recipe_service.py#L217-L218](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/recipe/recipe_service.py#L217-L218) |
| `localStorage` 中无用户偏好 | `useLocalStorage(..., defaults, { mergeDefaults: true })` 自动填充默认值 | [preferences.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-users/preferences.ts) |
| API `/api/app/about/theme` 获取失败 | 回退到 theme.ts 内联硬编码颜色值（如 `"#E58325"`）。注：`nuxt.config.ts` 的 `runtimeConfig.public.themes` 虽定义了环境变量 fallback 链，但 theme 插件并未读取它，属于死代码。 | [theme.ts#L20-L31](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L31)、[theme.ts#L51-L69](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L51-L69) |
| 浏览器语言不在支持列表 | 前后端均回退到 `en-US` | [providers.py#L49-L53](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py#L49-L53)、[i18n.config.ts#L97](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/i18n.config.ts#L97) |
| 未登录访问 | `useHouseholdSelf()` / `useGroupSelf()` 的 `refreshXxxSelf()` 检测 `!auth.user.value` 时立即返回 null，不发起请求 | [use-households.ts#L11-L15](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L11-L15) |
| `recipe.settings` 为 undefined | 详情页中使用可选链 `recipe.value.settings?.landscapeView`，undefined 视为 false | [RecipePage.vue#L397](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue#L397) |

---

## 五、关键代码文件索引

| 类别 | 文件路径 |
|------|----------|
| **后端模型** | |
| User 模型 | [mealie/db/models/users/users.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/users/users.py) |
| Household 偏好模型 | [mealie/db/models/household/preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/household/preferences.py) |
| Group 偏好模型 | [mealie/db/models/group/preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/db/models/group/preferences.py) |
| **后端 Schema** | |
| Household 偏好 Schema | [mealie/schema/household/household_preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/schema/household/household_preferences.py) |
| Group 偏好 Schema | [mealie/schema/group/group_preferences.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/schema/group/group_preferences.py) |
| AppTheme Schema | [mealie/schema/admin/about.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/schema/admin/about.py) |
| **后端设置** | |
| Theme 配置类 | [mealie/core/settings/themes.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/core/settings/themes.py) |
| **后端路由** | |
| Household 偏好 API | [mealie/routes/households/controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/households/controller_household_self_service.py) |
| Group 偏好 API | [mealie/routes/groups/controller_group_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/groups/controller_group_self_service.py) |
| App 关于/主题 API | [mealie/routes/app/app_about.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/app/app_about.py) |
| **后端服务层** | |
| 食谱服务（默认值注入） | [mealie/services/recipe/recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/recipe/recipe_service.py) |
| 家庭服务（创建默认值） | [mealie/services/household_services/household_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/household_services/household_service.py) |
| **后端语言** | |
| Locale Provider | [mealie/lang/providers.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py) |
| Locale 中间件 | [mealie/middleware/locale_context.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/middleware/locale_context.py) |
| **前端用户偏好** | |
| 本地偏好 composables | [frontend/app/composables/use-users/preferences.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-users/preferences.ts) |
| 当前家庭状态 composable（`householdSelfRef`） | [frontend/app/composables/use-households.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts) |
| 当前组状态 composable（`groupSelfRef`） | [frontend/app/composables/use-groups.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts) |
| 认证后端（登出逻辑） | [frontend/app/composables/use-auth-backend.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts) |
| 认证包装（useMealieAuth） | [frontend/app/composables/use-mealie-auth.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-mealie-auth.ts) |
| Store 统一入口（`clearAllStores`） | [frontend/app/composables/store/index.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/store/index.ts) |
| 家庭列表 store | [frontend/app/composables/store/use-household-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/store/use-household-store.ts) |
| API 客户端（Accept-Language 设置） | [frontend/app/composables/api/api-client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/api/api-client.ts) |
| Axios 拦截器（仅 Authorization/401） | [frontend/app/plugins/axios.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/axios.ts) |
| **前端主题/语言** | |
| 主题颜色插件（`__cachedTheme`） | [frontend/app/plugins/theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts) |
| 暗色模式插件 | [frontend/app/plugins/dark-mode.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts) |
| 全局 i18n 单例 | [frontend/app/composables/use-global-i18n.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-global-i18n.ts) |
| Locale composable | [frontend/app/composables/use-locales/use-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/use-locales.ts) |
| 可用语言列表 | [frontend/app/composables/use-locales/available-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/available-locales.ts) |
| i18n 运行时配置 | [frontend/app/i18n.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/i18n.config.ts) |
| Nuxt 构建配置（i18n、useDark） | [frontend/nuxt.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts) |
| **前端 UI 组件** | |
| 食谱卡片列表（视图切换） | [frontend/app/components/Domain/Recipe/RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue) |
| 食谱详情页（Landscape 视图） | [frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) |
| 家庭偏好编辑器 | [frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue) |
| **前端页面** | |
| 家庭设置页 | [frontend/app/pages/household/index.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/household/index.vue) |
| 用户设置页 | [frontend/app/pages/user/profile/edit.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/user/profile/edit.vue) |
