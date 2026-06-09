# Mealie 本地化与多语言资源代码协作分析

## 一、总体架构概览

Mealie 采用前后端分离的本地化架构：

- **后端**：Python/FastAPI，自建轻量 i18n 框架（`mealie/pkgs/i18n/`）
- **前端**：Nuxt 3 + Vue 3，使用 `@nuxtjs/i18n`（vue-i18n 桥接）+ Vuetify 内置国际化
- **翻译资源管理**：通过 Crowdin 平台协作，CI/CD 自动同步
- **前后端语言同步**：前端在 API 调用层**显式写入 `Accept-Language` 请求头**，后端通过 Header 读取

---

## 二、后端本地化实现（Python/FastAPI）

### 2.1 核心模块结构

```
mealie/
├── lang/
│   ├── __init__.py               # 导出 providers
│   ├── locale_config.py          # 语言元数据配置（名称、LTR/RTL、复数策略）
│   ├── providers.py              # 翻译器工厂、上下文变量、依赖注入
│   └── messages/                 # JSON 翻译文件（en-US.json 等 40+ 语言）
├── pkgs/i18n/
│   ├── __init__.py
│   ├── json_provider.py          # JSON 翻译提供器（解析、复数、变量替换）
│   └── provider_factory.py       # 提供器工厂（加载、缓存、回退）
├── middleware/
│   └── locale_context.py         # HTTP 中间件：基于请求头注入上下文
└── repos/seed/
    ├── _abstract_seeder.py       # 种子数据基类（含 locale 参数）
    ├── seeders.py                # 食材/单位/标签 seeder 实现
    └── resources/                # 种子数据 JSON（按 locale 分目录）
        ├── foods/locales/*.json
        ├── units/locales/*.json
        └── labels/locales/*.json
```

### 2.2 语言配置元数据

[locale_config.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/locale_config.py)

使用 `LocaleConfig` dataclass 定义每个语言的元数据：

```python
@dataclass
class LocaleConfig:
    key: str                                   # 语言代码，如 "zh-CN"
    name: str                                  # 显示名
    dir: LocaleTextDirection = LTR             # 文本方向 LTR/RTL
    plural_food_handling: LocalePluralFoodHandling = ALWAYS  # 食物名词复数策略
```

支持 40+ 种语言，其中阿拉伯语（ar-SA）和希伯来语（he-IL）为 RTL 方向；日语、韩语、中文、土耳其语、越南语等采用 `NEVER` 复数策略（不使用复数形式）。

### 2.3 JSON 翻译提供器

[json_provider.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/json_provider.py)

核心类 `JsonProvider`：

- **加载**：构造函数接受 `Path` 或 `dict`，从 JSON 文件加载翻译字典
- **键层级查找**：支持点分隔的嵌套键（如 `"emails.password.subject"` 通过 `split(".")` 逐层遍历字典）
- **变量插值**：支持 `{key}` 占位符替换，例如 `"generic-created": "{name} was created"`
- **复数解析** `_parse_plurals()`：基于 vue-i18n 规范，使用 `|` 分隔多复数形式
  - 1 段：原样返回
  - 2 段：`count==1` 取第 1 段，否则第 2 段
  - 3 段：`0` 取第 1 段，`1` 取第 2 段，其他取第 3 段
- **缺省回退**：当键不存在时，返回 `default` 参数（若有）或原始 key 字符串

```python
def t(self, key: str, default=None, **kwargs) -> str:
    keys = key.split(".")
    translation_value = self.translations
    # ... 逐层查找 ...
    # 找到后做变量替换和复数处理
    # 找不到则 return default or key
```

### 2.4 提供器工厂与缓存

[provider_factory.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/provider_factory.py)

`ProviderFactory` 负责：

- **文件加载** `_load(locale)`：按 `{locale}.json` 查找；不存在则使用 `fallback_locale`（默认 `"en-US"`）的文件
- **内存缓存** `_store`：使用 `InUseProvider(provider, locks)` 引用计数管理已加载的翻译器，避免重复 I/O
- **支持的语言列表** `supported_locales`：扫描目录下所有 JSON 文件自动得出

### 2.5 翻译器获取与依赖注入

[providers.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py)

关键函数：

1. **`_load_factory()`**：`@lru_cache` 单例工厂，固定 fallback 为 `"en-US"`，翻译目录为 `mealie/lang/messages/`
2. **`get_locale_provider(accept_language)`**：FastAPI 可注入依赖（`Header(None)`），从请求的 `Accept-Language` 头获取 locale 字符串，返回对应 `JsonProvider`
3. **`get_locale_config(accept_language)`**：同理返回 `LocaleConfig`；locale 不在 `LOCALE_CONFIG` 中时回退到 `"en-US"`
4. **`set_locale_context(translator, locale_config)` / `get_locale_context()`**：使用 Python `contextvars.ContextVar` 存储请求级别的翻译器，供非路由层代码（如 Schema、Service）在 HTTP 请求上下文中使用

### 2.6 HTTP 中间件

[locale_context.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/middleware/locale_context.py)

`LocaleContextMiddleware` 在每个请求进入时：

1. 从 `request.headers.get("accept-language")` 读语言偏好
2. 获取 translator 和 locale_config
3. 调用 `set_locale_context()` 存入 ContextVar
4. 调用后续处理器

这使得在请求生命周期内的任何代码都可以通过 `get_locale_context()` 访问当前语言环境。

### 2.7 路由层使用方式

[base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/routes/_base/base_controllers.py)

所有控制器基类 `_BaseController` 通过 FastAPI `Depends` 注入：

```python
class _BaseController(ABC):
    session: Session = Depends(generate_session)
    translator: Translator = Depends(get_locale_provider)   # ← 从 Accept-Language Header 注入
    locale_config: LocaleConfig = Depends(get_locale_config)

    @property
    def t(self):
        return self.translator.t if self.translator else get_locale_provider().t
```

控制器子类（如 `RegistrationController`）直接用 `self.t("key")` 或 `self.translator`：

```python
raise HTTPException(409, {"message": self.t("exceptions.username-conflict-error")})
```

服务层使用模式：

```python
# email_service.py
class EmailService(BaseService):
    def __init__(self, sender=None, locale=None):
        self.translator = get_locale_provider(locale)  # 可显式传入 locale 字符串
```

Schema 层在请求上下文中通过 ContextVar 获取：

```python
# recipe_ingredient.py
def _format_display(self) -> str:
    locale_context = get_locale_context()
    if locale_context:
        _, locale_cfg = locale_context
        plural_food_handling = locale_cfg.plural_food_handling
        ...
```

---

## 三、前端本地化实现（Nuxt 3 + Vue 3）

### 3.1 核心模块结构

```
frontend/app/
├── i18n.config.ts                    # vue-i18n 全局配置（datetimeFormats、fallback）
├── plugins/
│   └── axios.ts                      # axios 实例与拦截器（仅处理 Authorization）
├── composables/
│   ├── use-global-i18n.ts            # 全局单例 i18n
│   ├── api/
│   │   └── api-client.ts             # ⭐ API 调用入口，显式写入 Accept-Language
│   └── use-locales/
│       ├── index.ts
│       ├── use-locales.ts            # 语言切换 composable
│       └── available-locales.ts      # 自动生成的语言列表（含翻译进度）
├── components/global/
│   └── LanguageDialog.vue            # 语言切换对话框
└── lang/
    ├── locales/                      # 懒加载入口 TS 文件（en-US.ts 等）
    ├── messages/                     # JSON 翻译文件（en-US.json 等）
    └── dateTimeFormats/              # 日期时间格式 JSON
```

### 3.2 Nuxt i18n 配置

[nuxt.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L152-L214)

```ts
i18n: {
  locales: [
    { code: "zh-CN", file: "zh-CN.ts", dir: "ltr" },
    // ... 40+ 种语言，由 gen_ts_locales.py 自动注入
  ],
  strategy: "no_prefix",                    // URL 不使用语言前缀
  lazy: true,                               // 按需懒加载语言文件
  types: "composition",                     // 使用 Vue 3 Composition API
  langDir: "./../app/lang/locales",         // 懒加载入口目录
  defaultLocale: "en-US",
  detectBrowserLanguage: {
    useCookie: true,                        // 持久化语言选择到 cookie
    alwaysRedirect: true,
    fallbackLocale: "en-US",
  },
  compilation: { strictMessage: false, escapeHtml: true },
  vueI18n: "./../app/i18n.config.ts",
}
```

[i18n.config.ts](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/i18n.config.ts)

```ts
export default defineI18nConfig(() => ({
  legacy: false,
  locale: "en-US",
  fallbackLocale: "en-US",
  fallbackWarn: true,
  datetimeFormats: { /* ... 40+ 种语言的日期格式 */ },
}));
```

### 3.3 ⭐ 前端是否在请求头写入 Accept-Language？——是，显式写入

**结论：前端在 API 调用层显式将当前 locale 写入 `Accept-Language` 请求头。**

关键代码位置：[api-client.ts#L63](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/composables/api/api-client.ts#L57-L66)

```ts
export const useRequests = function (i18n?: Composer): ApiRequestInstance {
  const { $axios } = useNuxtApp();
  if (!i18n) {
    i18n = useGlobalI18n();
  }

  // ⭐ 每次创建 API 请求实例时，都将当前 i18n locale 写入 axios 默认请求头
  $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value;

  return getRequests($axios);
};

// 所有 API 客户端都通过 useRequests() 创建，因此都会带上 Accept-Language
export const useAdminApi = function (i18n?: Composer): AdminAPI {
  const requests = useRequests(i18n);
  return new AdminAPI(requests);
};
export const useUserApi = function (i18n?: Composer): UserAPI { /* ... */ };
export const usePublicApi = function (i18n?: Composer): PublicApi { /* ... */ };
```

对比 [axios.ts](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/plugins/axios.ts#L18-L29) 的请求拦截器，后者**仅处理 Authorization Token**，不处理 Accept-Language：

```ts
axiosInstance.interceptors.request.use(
  (config) => {
    const token = useCookie(tokenName).value;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;  // 只加 Authorization
    }
    return config;
  },
  // ...
);
```

**Accept-Language 的设置时机**：
- 不是拦截器动态读取，而是在**每次调用 `useUserApi()` / `useAdminApi()` / `usePublicApi()`** 时静态写入 `$axios.defaults.headers.common`
- 由于这些 composable 在组件 setup 阶段调用，而 i18n locale 在该阶段已由 `@nuxtjs/i18n` 的 `detectBrowserLanguage` 初始化完成，所以能正确获得当前语言
- 用户切换语言后，后续重新调用这些 composable 的组件会自动带上新的 Accept-Language

### 3.4 浏览器语言检测与 Cookie 持久化

`@nuxtjs/i18n` 的 `detectBrowserLanguage` 配置（[nuxt.config.ts#L204-L208](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L204-L208)）：

```ts
detectBrowserLanguage: {
  useCookie: true,          // 使用 cookie 持久化用户选择
  alwaysRedirect: true,     // 始终重定向以应用检测结果
  fallbackLocale: "en-US",  // 检测失败时回退
}
```

工作流程：
1. **首次访问**：读取 `navigator.language`，匹配 `locales` 数组中的 code；不匹配则回退 `"en-US"`
2. **写入 Cookie**：将检测结果写入名为 `i18n_redirected` 的 cookie
3. **用户手动切换**：调用 `i18n.setLocale(value)`（由 [use-locales.ts#L12](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/composables/use-locales/use-locales.ts#L12) 触发），`@nuxtjs/i18n` 内部自动更新 cookie
4. **再次访问**：优先读取 `i18n_redirected` cookie，跳过浏览器检测

### 3.5 语言文件懒加载入口

每个语言有一个 TS 入口，如 [en-US.ts](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/lang/locales/en-US.ts)：

```ts
export default defineI18nLocale(async () => {
  const { en: $vuetify } = await import("vuetify/locale");
  const { default: enUS } = await import("../messages/en-US.json");
  return {
    ...enUS,         // 业务翻译
    $vuetify,        // Vuetify 组件翻译（合并）
  };
});
```

### 3.6 界面文本选择：模板与脚本中的使用

**模板中**（全局 `$t` 函数）：

```vue
<!-- login.vue -->
<v-toolbar-title>{{ $t('user.sign-in') }}</v-toolbar-title>
<v-text-field :label="$t('user.email-or-username')" />
```

**脚本中**（Composition API）：

```ts
const i18n = useI18n();
alert.error(i18n.t("user.please-enter-your-email-and-password"));
useSeoMeta({ title: i18n.t("user.login") });
```

**组件中使用插值和插槽**（LanguageDialog.vue）：

```vue
<i18n-t keypath="language-dialog.how-to-contribute-description">
  <template #read-the-docs-link>
    <a href="https://docs.mealie.io/contributors/translating/">
      {{ $t("language-dialog.read-the-docs") }}
    </a>
  </template>
</i18n-t>
```

### 3.7 语言切换机制

[use-locales.ts](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/composables/use-locales/use-locales.ts)

```ts
export const useLocales = () => {
  const i18n = useGlobalI18n();            // 单例 i18n
  const { current: vuetifyLocale } = useLocale();  // Vuetify locale

  const locale = computed({
    get: () => i18n.locale.value,
    set(value) { i18n.setLocale(value); }, // 切换同时触发 cookie 持久化
  });

  // 自动同步 Vuetify 组件的语言
  watch(locale, (lc) => { vuetifyLocale.value = lc; });

  return { locale, locales: LOCALES, i18n };
};
```

[LanguageDialog.vue](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/components/global/LanguageDialog.vue) 提供 UI 入口，用户选择后写回 `locale.value`。

---

## 四、两种语言来源对比：种子数据 locale vs 普通接口语言

Mealie 后端存在**两条独立的语言来源路径**，分别服务于不同场景。

### 4.1 路径一：普通接口的语言来源（Accept-Language Header）

**适用场景**：绝大多数 API 接口，包括返回翻译后的错误消息、通知文本、邮件内容、食材显示格式化等。

**完整链路**：

```
前端组件调用 useUserApi() / useAdminApi() / usePublicApi()
    │
    ▼
api-client.ts: $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value
    │  （如 "zh-CN"）
    ▼
浏览器发出 HTTP 请求，Header 携带：Accept-Language: zh-CN
    │
    ▼
后端 LocaleContextMiddleware.dispatch()
    │  读 request.headers.get("accept-language")
    ▼
get_locale_provider(accept_language="zh-CN")
    │  ProviderFactory.get("zh-CN")
    │  → 命中缓存 或 加载 mealie/lang/messages/zh-CN.json
    │  → 若文件不存在 → 回退 en-US.json
    ▼
存入 ContextVar：set_locale_context(translator, locale_config)
    │
    ▼
路由层 BaseController 通过 Depends(get_locale_provider) 注入 translator
    │  self.t("key") → JsonProvider.t("key")
    ▼
Service / Schema 层通过 get_locale_context() 获取
    │
    ▼
JsonProvider.t() 逐层查字典 → 变量替换 → 复数处理 → 返回文本
    │  key 不存在 → 返回 default 或 原始 key
```

**涉及的关键代码位置**：
- 前端 Header 写入：[api-client.ts#L63](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/composables/api/api-client.ts#L63)
- 后端中间件读取：[locale_context.py#L13-L21](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/middleware/locale_context.py#L13-L21)
- 路由层注入：[base_controllers.py#L32-L44](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/routes/_base/base_controllers.py#L32-L44)
- 提供器回退逻辑：[provider_factory.py#L30-L34](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/provider_factory.py#L30-L34)

### 4.2 路径二：种子数据的 locale 来源（请求体 payload 显式字段）

**适用场景**：注册新用户时初始化食材/单位/标签，以及管理员在后台手动触发 "Seed Data" 导入。

**完整链路（注册场景）**：

```
前端 register/index.vue
    │
    ├─ const { locale } = useLocales();          // 当前 UI 语言
    │  locale.value = "zh-CN"
    │
    ▼
构造 payload:
    {
      email: "...",
      password: "...",
      locale: locale.value,    // ⭐ 显式放入请求体
      seedData: true,
      ...
    }
    │
    ▼
POST /api/users/register  (JSON Body)
    │
    ▼
后端 schema 校验：CreateUserRegistration
    │  [registration.py#L22-L29]
    │  locale: str = "en-US"（默认值）
    │  @field_validator 调用 validate_locale()
    │  → 白名单校验通过（白名单由 gen_ts_locales.py 自动生成）
    │
    ▼
RegistrationController.register_new_user()
    │  registration_service = RegistrationService(..., self.translator, ...)
    │  result = registration_service.register_user(data)
    │
    ▼
RegistrationService.register_user()
    │  [registration_service.py#L115-L119]
    │  if new_group and registration.seed_data:
    │      seeder_service = SeederService(self.repos)
    │      seeder_service.seed_foods(registration.locale)    // ⭐ 使用 payload 中的 locale
    │      seeder_service.seed_labels(registration.locale)
    │      seeder_service.seed_units(registration.locale)
    │
    ▼
SeederService.seed_foods() → IngredientFoodsSeeder.seed(locale)
    │
    ▼
IngredientFoodsSeeder.get_file(locale)
    │  [seeders.py#L91-L95]
    │  locale_path = resources/foods/locales/{locale}.json
    │  return locale_path if locale_path.exists() else foods.en_US  // ⭐ 独立回退
    │
    ▼
从对应 locale 的 JSON 加载食材名称，写入数据库
```

**管理员手动触发种子（/groups/seeders 接口）链路类似**：

```
前端 units.vue / foods.vue / labels.vue
    │  const { locale: currentLocale } = useLocales();
    │  locale.value = currentLocale.value;  // 用户可在 UI 上选择不同语言
    │  await userApi.seeders.units({ locale: locale.value });
    │
    ▼
POST /api/groups/seeders/units  Body: { "locale": "zh-CN" }
    │
    ▼
SeederConfig schema 校验（同白名单 validate_locale）
    │
    ▼
DataSeederController.seed_units()
    │  → self.service.seed_units(data.locale)
    │
    ▼
（同上 seeder 流程）
```

**涉及的关键代码位置**：
- 前端注册页放入 payload：[register/index.vue#L475](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/pages/register/index.vue#L475)
- 前端种子页放入 payload：[units.vue#L443](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/pages/group/data/units.vue#L443)
- 后端 Schema 定义与默认值：[registration.py#L22-L29](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/schema/user/registration.py#L22-L29)
- 后端 locale 白名单校验：[validators.py#L1-L49](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/schema/_mealie/validators.py#L1-L49)
- 注册服务调用 seeder：[registration_service.py#L115-L119](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/services/user_services/registration_service.py#L115-L119)
- 种子数据文件查找与回退：[seeders.py#L56-L59](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L56-L59)、[seeders.py#L91-L95](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L91-L95)
- 种子数据默认回退文件：[foods/__init__.py#L5](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/resources/foods/__init__.py#L5)

### 4.3 两条路径的核心差异对比

| 对比维度 | 普通接口语言 | 种子数据 locale |
|---------|------------|--------------|
| **传输载体** | HTTP Header: `Accept-Language` | HTTP Request Body: `{"locale": "zh-CN"}` |
| **前端写入位置** | `useRequests()` 中 `$axios.defaults.headers.common["Accept-Language"]` | 各页面手动放入 API payload |
| **后端读取方式** | `Header(None)` 依赖注入 / 中间件 `request.headers.get()` | Pydantic Model 字段解析 |
| **默认值** | Header 为空时默认为 `"en-US"` | Schema 定义 `locale: str = "en-US"` |
| **校验机制** | 无显式校验（不存在的 locale 由 ProviderFactory 回退） | `validate_locale()` 白名单校验，非法值抛 422 |
| **影响范围** | 接口响应消息、邮件通知、错误文本、Schema 显示格式化 | 写入数据库的预置食材/单位/标签名称 |
| **回退机制** | ProviderFactory → 加载 `en-US.json`；JsonProvider.t() → 返回 default 或 key | Seeder.get_file() → 文件不存在返回 `en-US.json` |
| **翻译资源目录** | `mealie/lang/messages/{locale}.json` | `mealie/repos/seed/resources/{foods,units,labels}/locales/{locale}.json` |
| **是否持久化** | 不持久化，每次请求动态决定 | 持久化到数据库（foods/units/labels 表），后续不再改变 |
| **能否与 UI 语言不同** | 不能，始终等于当前 i18n locale | 能，种子页用户可手动选择不同语言导入 |

---

## 五、缺省回退机制总结

### 5.1 后端回退链

| 层级 | 场景 | 回退行为 | 位置 |
|------|------|----------|------|
| ProviderFactory | 请求的 locale JSON 文件不存在 | 加载 `en-US.json` | [provider_factory.py#L30-L34](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/provider_factory.py#L30-L34) |
| JsonProvider.t() | key 在翻译字典中不存在 | 返回 `default` 参数；未指定则返回 key 本身 | [json_provider.py#L58](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/json_provider.py#L58) |
| get_locale_config() | locale 不在 LOCALE_CONFIG | 返回 `en-US` 的 LocaleConfig | [providers.py#L49-L53](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L49-L53) |
| get_locale_provider() | Accept-Language 为空 | 默认为 `"en-US"` | [providers.py#L43-L46](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L43-L46) |
| CreateUserRegistration Schema | 前端未传 locale 字段 | 默认为 `"en-US"` | [registration.py#L23](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/schema/user/registration.py#L23) |
| Seeder.get_file() | 种子数据 locale 文件不存在 | 回退到 `en-US.json` | [seeders.py#L24-L27](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L24-L27) |

### 5.2 前端回退链

| 层级 | 场景 | 回退行为 | 位置 |
|------|------|----------|------|
| @nuxtjs/i18n detectBrowserLanguage | 浏览器语言不匹配 / cookie 不存在 | `fallbackLocale: "en-US"` | [nuxt.config.ts#L207](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L207) |
| vue-i18n | key 不存在于当前语言 | `fallbackLocale: "en-US"`（`fallbackWarn: true` 时控制台输出警告） | [i18n.config.ts#L97-L98](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/i18n.config.ts#L97-L98) |
| Vuetify locale | locale 切换 | 同步切换，默认 fallback `"en-US"` | [nuxt.config.ts#L250-L253](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L250-L253) |

---

## 六、翻译资源管理与代码协作

### 6.1 Crowdin 同步

[crowdin.yml](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/crowdin.yml) 定义了 5 组需要翻译的资源：

```yaml
files:
  - source: /frontend/app/lang/messages/en-US.json    # 前端界面文本
    translation: /frontend/app/lang/messages/%locale%.json
  - source: /mealie/lang/messages/en-US.json          # 后端异常、邮件、通知文本
    translation: /mealie/lang/messages/%locale%.json
  - source: /mealie/repos/seed/resources/foods/locales/en-US.json   # 内置食材名称
    translation: /mealie/repos/seed/resources/foods/locales/%locale%.json
  - source: /mealie/repos/seed/resources/units/locales/en-US.json   # 内置单位名称
    translation: /mealie/repos/seed/resources/units/locales/%locale%.json
  - source: /mealie/repos/seed/resources/labels/locales/en-US.json  # 内置标签名称
    translation: /mealie/repos/seed/resources/labels/locales/%locale%.json
```

GitHub Actions（`.github/workflows/auto-merge-l10n.yml`、`locale-sync.yml`）负责：
- 推送英文源文件到 Crowdin
- 从 Crowdin 拉取翻译结果并自动创建 PR

### 6.2 代码生成器

[gen_ts_locales.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/dev/code-generation/gen_ts_locales.py) 是关键的前后端协作工具：

**功能**：
1. 从 Crowdin API（或本地回退）获取所有目标语言的元数据和翻译进度
2. 合并 `LOCALE_CONFIG` 中定义的语言名称、方向、复数策略
3. 生成 `frontend/app/composables/use-locales/available-locales.ts`（语言选择器数据）
4. 自动扫描 JSON 文件并注入 `nuxt.config.ts` 的 `i18n.locales` 数组（使用 `CODE_GEN_ID: MESSAGE_LOCALES` 标记块）
5. 自动扫描日期格式 JSON 并注入 `i18n.config.ts` 的 `datetimeFormats`（使用 `CODE_GEN_ID: DATE_LOCALES` 标记块）
6. 注入 `mealie/schema/_mealie/validators.py` 的 `validate_locale()` 白名单（使用相同的 `CODE_GEN_ID: MESSAGE_LOCALES` 标记块）

---

## 七、端到端数据流总图

### 7.1 普通接口语言流

```
用户浏览器（cookie: i18n_redirected=zh-CN）
    │
    ▼
前端组件 setup() 调用 useUserApi()
    │
    ▼
useRequests() → $axios.defaults.headers.common["Accept-Language"] = "zh-CN"
    │
    ▼
HTTP 请求发出
    Header:
      Accept-Language: zh-CN
      Authorization: Bearer <token>
      Cookie: i18n_redirected=zh-CN; mealie.access_token=...
    │
    ▼
后端 LocaleContextMiddleware
    │  accept_language = request.headers["accept-language"] → "zh-CN"
    │  translator = get_locale_provider("zh-CN")
    │  set_locale_context(translator, locale_config)
    │
    ▼
路由 BaseUserController
    │  translator: Translator = Depends(get_locale_provider)
    │  self.t("exceptions.permission_denied")
    │
    ▼
ProviderFactory.get("zh-CN")
    │  → _store 缓存命中 或 加载 mealie/lang/messages/zh-CN.json
    │  → 若文件缺失 → 回退 en-US.json
    │
    ▼
JsonProvider.t("exceptions.permission_denied")
    │  → 逐层查字典
    │  → 返回 "You do not have permission to perform this action"
    │  → key 缺失 → 返回 default 或 原始 key
```

### 7.2 注册种子数据语言流

```
用户填写注册表单，选择 "简体中文" 作为 UI 语言
    │
    ▼
前端 register/index.vue
    │  const { locale } = useLocales();  // "zh-CN"
    │  payload.locale = locale.value;
    │  payload.seedData = true;
    │
    ▼
POST /api/users/register
    Body: {
      "email": "user@example.com",
      "password": "...",
      "locale": "zh-CN",          // ⭐ 请求体字段
      "seedData": true,
      "group": "My Family"
    }
    Header: Accept-Language: zh-CN  （同时存在，但种子数据不使用它）
    │
    ▼
CreateUserRegistration Schema 校验
    │  locale: "zh-CN" ∈ valid locales ✅
    │
    ▼
RegistrationService.register_user()
    │  创建用户、组、家庭
    │  if seedData:
    │      seeder_service.seed_foods("zh-CN")     // ⭐ 使用 payload.locale
    │      seeder_service.seed_labels("zh-CN")
    │      seeder_service.seed_units("zh-CN")
    │
    ▼
IngredientFoodsSeeder.seed("zh-CN")
    │  get_file("zh-CN")
    │    → resources/foods/locales/zh-CN.json 存在 → 加载
    │    → 若不存在 → 回退 resources/foods/locales/en-US.json
    │
    ▼
解析 JSON，将中文食材名（"面粉"、"鸡蛋"、"牛奶"等）写入 ingredient_foods 表
    │
    ▼
后续 API 调用通过 Accept-Language 返回界面翻译文本，
但数据库中的食材名始终以种子时选择的语言存储（不再变更）
```
