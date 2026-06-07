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

#### 亮暗模式切换

**存储位置**：浏览器 `localStorage`，key = `vueuse-color-scheme`（由 `@vueuse/core` 的 `useDark()` 管理）

**实现插件**：[dark-mode.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts)

**亮暗模式优先级**：
```
localStorage['vueuse-color-scheme'] ('dark' | 'light' | 'auto')
    ↓  (当 localStorage 非 'dark' 且非 'light' 时)
系统偏好 (prefers-color-scheme: dark)
    ↓  (Vuetify 默认主题在 theme.ts 中初始化)
nuxt.config.ts runtimeConfig.public.useDark (默认 false，即 light)
```

**初始化**：
- 插件加载时读取 `useDark()` 返回值，调用 `vuetify.theme.change()` 设置初始主题
- 切换时通过 `onChanged` 回调同步到 Vuetify

**页面加载时的防闪烁**：
在 [nuxt.config.ts#L53-L57](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L53-L57)，通过注入内联脚本在渲染前读取 `vueuse-color-scheme` 并设置背景色。

#### 主题颜色的完整取值链（修正版）

**后端配置**：
见 [themes.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/core/settings/themes.py)
```python
class Theme(BaseSettings):
    light_primary: str = "#E58325"
    # ... 14 个颜色字段
    model_config = SettingsConfigDict(env_prefix="theme_", extra="allow")
```

取值优先级（后端）：
```
环境变量 (THEME_LIGHT_PRIMARY, THEME_DARK_PRIMARY, ...) — 通过 SettingsConfigDict env_prefix="theme_" 读取
    ↓
Theme 类硬编码默认值（如 light_primary="#E58325"）
```

**API 暴露**：
见 [app_about.py#L66-L72](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/app/app_about.py#L66-L72)
- `GET /api/app/about/theme` — 返回 `AppTheme(**settings.theme.model_dump())`
- 设置了 HTTP 响应头：`Cache-Control: public, max-age=604800`（7 天浏览器/CDN 缓存）

**前端加载逻辑**：
见 [theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L75)

取值优先级（前端）：
```
模块级变量 __cachedTheme (前端插件生命周期内的内存缓存)
    ↓  (首次加载或缓存失效时)
fetch("/api/app/about/theme") + HTTP 7天缓存
    ↓  (fetch 失败时)
前端硬编码默认值（如 primary: "#E58325", accent: "#007A99" 等）
```

> ⚠️ **之前的描述偏差**：前端并不存在「回退到 `nuxt.config.ts` 的 `runtimeConfig.public.themes`」这一层。`nuxt.config.ts` 中仅有 `runtimeConfig.public.useDark`（来源：`Boolean(process.env.THEME_USE_DARK) || false`，见 [nuxt.config.ts#L86](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L86)），用于控制 Vuetify 默认亮/暗主题，不包含颜色配置。

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

### 4.4 各类数据的缓存方式与失效触发汇总

| 数据 | 缓存方式 | 失效触发 |
|------|----------|----------|
| 用户数据 (UserOut) | 内存级 `authUser` ref | 登出、`auth.refresh()`、401 响应 |
| 当前用户家庭数据 (含 preferences) | 模块级 `householdSelfRef` ref | `refreshHouseholdSelf()` 调用且无登录用户时、页面刷新 |
| 当前用户组数据 (含 preferences) | 模块级 `groupSelfRef` ref | `refreshGroupSelf()` 调用且无登录用户时、页面刷新 |
| 家庭/组/分类/食材等**列表**缓存 | 9 个 store 模块 ref | `clearAllStores()`（登出时调用）、各 store 自有的 `flushStore()` |
| 用户 UI 偏好（排序、视图、打印等） | `localStorage`（12 个 key） | 用户手动修改（自动同步）、浏览器手动清除、**登出不清理** |
| 主题色配置 | 模块级 `__cachedTheme` 变量 + HTTP `Cache-Control: max-age=604800` | 页面刷新（JS 变量重置）、7 天后 HTTP 缓存失效 |
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
| API `/api/app/about/theme` 获取失败 | 直接回退到前端硬编码默认值（不存在 nuxt.config.ts 环境变量中间层） | [theme.ts#L20-L31](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L31) |
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
