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

#### 缓存处理：

家庭偏好通过 `useHouseholdSelf()` 进行**模块级单例缓存**（`householdSelfRef` 是模块作用域的 ref），首次调用时懒加载。登出时通过 `clearAllStores()` 清空（见 [use-auth-backend.ts#L108](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L108)）。

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

**存储位置**：浏览器 `localStorage`，key = `vueuse-color-scheme`

**实现插件**：
- 主题配置加载：[theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts)
- 暗色模式切换：[dark-mode.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts)

**优先级**：
```
localStorage 中的 'vueuse-color-scheme' ('dark' | 'light')
    ↓
系统偏好 (prefers-color-scheme: dark)  — 当 localStorage 非 'dark' 且非 'light' 时
    ↓
nuxt.config.ts 中 runtimeConfig.public.useDark (默认 false)
```

**主题颜色来源**：
- 首先尝试从 API `/api/app/about/theme` 获取服务器配置的主题色
- 缓存于模块级变量 `__cachedTheme`
- 失败回退到 `nuxt.config.ts` 中的 `runtimeConfig.public.themes` 环境变量
- 最后使用硬编码默认值（如 `primary: "#E58325"`）

见 [theme.ts#L20-L75](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L75)

**页面加载时的防闪烁**：
在 [nuxt.config.ts#L53-L57](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L53-L57)，通过注入内联脚本在渲染前读取 `vueuse-color-scheme` 并设置背景色。

---

### 3.2 语言 / Locale

#### 前端语言管理

**配置**：[nuxt.config.ts#L152-L214](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts#L152-L214)
- 默认 locale：`en-US`
- fallback locale：`en-US`
- 语言策略：`strategy: "no_prefix"`（URL 中不包含语言代码）
- 懒加载：`lazy: true`

**浏览器语言检测**：
```typescript
detectBrowserLanguage: {
  useCookie: true,           // 将检测结果持久化到 Cookie
  alwaysRedirect: true,
  fallbackLocale: "en-US",
}
```

**Composable**：[use-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/use-locales.ts)
```typescript
const locale = computed({
  get: () => i18n.locale.value,
  set(value) { i18n.setLocale(value); },
});
```
同时监听 `locale` 变化，同步更新 Vuetify 的 locale。

**存储优先级**：
```
i18n Cookie (由 detectBrowserLanguage 管理)
    ↓
浏览器 Accept-Language header (首次访问时)
    ↓
默认 'en-US'
```

#### 后端语言管理

**中间件**：[locale_context.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/middleware/locale_context.py)
- 从请求头 `Accept-Language` 读取语言
- 通过 ContextVar 注入到当前请求上下文

**语言 Provider**：[providers.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py)
- 使用 `@lru_cache` 缓存翻译 Provider 工厂
- fallback locale：`en-US`
- 若请求的语言不在支持列表中，回退到 `en-US`

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

## 四、缓存处理与空态情况

### 4.1 缓存处理汇总

| 数据 | 缓存方式 | 失效触发 |
|------|----------|----------|
| 用户数据 (UserOut) | 内存级 `authUser` ref | 登出、`auth.refresh()`、401 响应 |
| 家庭数据 (含 preferences) | 模块级 `householdSelfRef` ref | 登出、手动 `refreshHouseholdSelf()` |
| 组数据 (含 preferences) | 模块级 `groupSelfRef` ref | 登出、手动 `refreshGroupSelf()` |
| 用户 UI 偏好 | `localStorage` | 用户手动修改（自动同步） |
| 主题色配置 | 模块级 `__cachedTheme` 变量 | 页面刷新（生命周期内不失效） |
| 语言翻译 | `@nuxtjs/i18n` 内部缓存 | 切换 locale 时按需懒加载 |
| 后端翻译 Provider | Python `@lru_cache` | 进程生命周期 |

**登出时统一清理**：
见 [use-auth-backend.ts#L102-L112](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts#L102-L112)
```typescript
finally {
  setToken(null);
  authUser.value = null;
  authStatus.value = "unauthenticated";
  clearAllStores();   // 清空所有 store 缓存
  clearNuxtData();    // 清空 Nuxt useAsyncData 缓存
  await router.push(callbackUrl || "/login");
}
```

### 4.2 空态（无数据）情况处理

| 场景 | 处理方式 | 代码位置 |
|------|----------|----------|
| `household.preferences` 为 null | 前端渲染 `HouseholdPreferencesEditor` 前有 `v-if` 保护 | [HouseholdPreferencesEditor.vue#L2](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue#L2) |
| 创建食谱时 `household.preferences` 为 null | 回退到 `RecipeSettings()` 无参构造（即代码默认值） | [recipe_service.py#L217-L218](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/recipe/recipe_service.py#L217-L218) |
| `localStorage` 中无用户偏好 | `useLocalStorage(..., defaults, { mergeDefaults: true })` 自动填充默认值 | [preferences.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-users/preferences.ts) |
| API `/api/app/about/theme` 获取失败 | 回退到 nuxt.config.ts 中的环境变量 → 硬编码默认值 | [theme.ts#L20-L31](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts#L20-L31) |
| 浏览器语言不在支持列表 | 前后端均回退到 `en-US` | [providers.py#L49-L53](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py#L49-L53)、[i18n.config.ts#L97](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/i18n.config.ts#L97) |
| 未登录访问 | `useHouseholdSelf()` / `useGroupSelf()` 立即返回 null，不发起请求 | [use-households.ts#L11-L15](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts#L11-L15) |
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
| **后端路由** | |
| Household 偏好 API | [mealie/routes/households/controller_household_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/households/controller_household_self_service.py) |
| Group 偏好 API | [mealie/routes/groups/controller_group_self_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/routes/groups/controller_group_self_service.py) |
| **后端服务层** | |
| 食谱服务（默认值注入） | [mealie/services/recipe/recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/recipe/recipe_service.py) |
| 家庭服务（创建默认值） | [mealie/services/household_services/household_service.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/services/household_services/household_service.py) |
| **后端语言** | |
| Locale Provider | [mealie/lang/providers.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/lang/providers.py) |
| Locale 中间件 | [mealie/middleware/locale_context.py](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/mealie/middleware/locale_context.py) |
| **前端用户偏好** | |
| 本地偏好 composables | [frontend/app/composables/use-users/preferences.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-users/preferences.ts) |
| 家庭状态 composable | [frontend/app/composables/use-households.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-households.ts) |
| 组状态 composable | [frontend/app/composables/use-groups.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-groups.ts) |
| 认证状态 composable | [frontend/app/composables/use-auth-backend.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-auth-backend.ts) |
| **前端主题/语言** | |
| 主题插件 | [frontend/app/plugins/theme.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/theme.ts) |
| 暗色模式插件 | [frontend/app/plugins/dark-mode.client.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/plugins/dark-mode.client.ts) |
| Locale composable | [frontend/app/composables/use-locales/use-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/use-locales.ts) |
| 可用语言列表 | [frontend/app/composables/use-locales/available-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/composables/use-locales/available-locales.ts) |
| i18n 配置 | [frontend/app/i18n.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/i18n.config.ts) |
| Nuxt 配置 | [frontend/nuxt.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/nuxt.config.ts) |
| **前端 UI 组件** | |
| 食谱卡片列表（视图切换） | [frontend/app/components/Domain/Recipe/RecipeCardSection.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeCardSection.vue) |
| 食谱详情页（Landscape 视图） | [frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePage.vue) |
| 家庭偏好编辑器 | [frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Household/HouseholdPreferencesEditor.vue) |
| 食谱设置开关 | [frontend/app/components/Domain/Recipe/RecipeSettingsSwitches.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/components/Domain/Recipe/RecipeSettingsSwitches.vue) |
| **前端页面** | |
| 家庭设置页 | [frontend/app/pages/household/index.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/household/index.vue) |
| 用户设置页 | [frontend/app/pages/user/profile/edit.vue](file:///d:/fz/0601/solo-dogfeeding/code/81-mealie/frontend/app/pages/user/profile/edit.vue) |
