# Mealie 本地化与多语言资源代码协作分析

## 一、总体架构概览

Mealie 采用前后端分离的本地化架构：

- **后端**：Python/FastAPI，自建轻量 i18n 框架（`mealie/pkgs/i18n/`）
- **前端**：Nuxt 3 + Vue 3，使用 `@nuxtjs/i18n`（vue-i18n 桥接）+ Vuetify 内置国际化
- **翻译资源管理**：通过 [Crowdin](https://crowdin.com) 平台协作，CI/CD 自动同步

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
└── middleware/
    └── locale_context.py         # HTTP 中间件：基于请求头注入上下文
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
    translator: Translator = Depends(get_locale_provider)   # ← 注入
    locale_config: LocaleConfig = Depends(get_locale_config)

    @property
    def t(self):
        return self.translator.t if self.translator else get_locale_provider().t
```

控制器子类（如 `RegistrationController`）直接用 `self.t("key")` 或 `self.translator`：

```python
# registration_service.py
raise HTTPException(409, {"message": self.t("exceptions.username-conflict-error")})
```

服务层使用模式：

```python
# email_service.py
class EmailService(BaseService):
    def __init__(self, sender=None, locale=None):
        self.translator = get_locale_provider(locale)  # 可显式传入 locale

    def send_forgot_password(self, address, url):
        EmailTemplate(
            subject=self.translator.t("emails.password.subject"),
            ...
        )
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
│   └── globals.ts                    # 全局注入
├── composables/
│   ├── use-global-i18n.ts            # 全局单例 i18n
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

### 3.3 语言文件懒加载入口

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

### 3.4 文本选择：模板与脚本中的使用

**模板中**（全局 `$t` 函数）：

```vue
<!-- login.vue -->
<v-toolbar-title>{{ $t('user.sign-in') }}</v-toolbar-title>
<v-text-field :label="$t('user.email-or-username')" />
```

**脚本中**（Composition API）：

```ts
// login.vue
const i18n = useI18n();
// ...
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

### 3.5 语言切换机制

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

### 3.6 浏览器语言检测与持久化

`detectBrowserLanguage` 配置了：
- 首次访问根据 `navigator.language` 匹配
- 选择后写入 `i18n_redirected` cookie
- 后续访问优先读 cookie

### 3.7 前后端同步语言选择

后端完全依赖 HTTP `Accept-Language` 请求头。前端通过浏览器机制（以及 `@nuxtjs/i18n` 的内部实现）在切换语言时自动更新该请求头，axios 请求会携带浏览器当前语言。

对于注册、种子数据导入等场景，前端会在 payload 中显式传递 `locale` 字段：

```ts
// register/index.vue
const { locale } = useLocales();
const payload = { ..., locale: locale.value };
```

后端 `RegistrationService` 接收后将其传给 `SeederService`，用于加载对应语言的内置食材/单位/标签。

---

## 四、缺省回退机制总结

| 层级 | 场景 | 回退行为 | 位置 |
|------|------|----------|------|
| 后端 ProviderFactory | 请求的 locale JSON 文件不存在 | 加载 `en-US.json` | [provider_factory.py#L30-L34](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/provider_factory.py#L30-L34) |
| 后端 JsonProvider.t() | key 在翻译字典中不存在 | 返回 `default` 参数；未指定则返回 key 本身 | [json_provider.py#L58](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/json_provider.py#L58) |
| 后端 get_locale_config() | locale 不在 LOCALE_CONFIG | 返回 `en-US` 的 LocaleConfig | [providers.py#L49-L53](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L49-L53) |
| 后端 get_locale_provider() | Accept-Language 为空 | 默认为 `"en-US"` | [providers.py#L43-L46](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L43-L46) |
| 前端 vue-i18n | key 不存在 | `fallbackLocale: "en-US"`（`fallbackWarn: true` 时输出警告） | [i18n.config.ts#L97-L98](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/i18n.config.ts#L97-L98) |
| 前端 @nuxtjs/i18n | 浏览器语言不匹配 | `fallbackLocale: "en-US"` | [nuxt.config.ts#L207](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L207) |
| 前端 Vuetify | locale 切换 | 同步切换，默认 fallback `"en-US"` | [nuxt.config.ts#L250-L253](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L250-L253) |

---

## 五、翻译资源管理与代码协作

### 5.1 Crowdin 同步

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

### 5.2 代码生成器

[gen_ts_locales.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/dev/code-generation/gen_ts_locales.py) 是关键的前后端协作工具：

**功能**：
1. 从 Crowdin API（或本地回退）获取所有目标语言的元数据和翻译进度
2. 合并 `LOCALE_CONFIG` 中定义的语言名称、方向、复数策略
3. 生成 `frontend/app/composables/use-locales/available-locales.ts`（语言选择器数据）
4. 自动扫描 JSON 文件并注入 `nuxt.config.ts` 的 `i18n.locales` 数组（使用 `CODE_GEN_ID: MESSAGE_LOCALES` 标记块）
5. 自动扫描日期格式 JSON 并注入 `i18n.config.ts` 的 `datetimeFormats`（使用 `CODE_GEN_ID: DATE_LOCALES` 标记块）
6. 注入用户注册验证器的语言白名单

---

## 六、端到端数据流

```
用户选择语言
    │
    ▼
┌──────────────────────────┐
│ 前端 LanguageDialog.vue  │─── locale.value = "zh-CN"
└──────────────────────────┘
    │
    ▼
┌────────────────────────────┐
│ useLocales.setLocale()     │─── i18n.setLocale("zh-CN")
│                            │─── 写 cookie: i18n_redirected
│                            │─── vuetifyLocale.value = "zh-CN"
└────────────────────────────┘
    │
    ▼
┌────────────────────────────┐
│ 浏览器发 HTTP 请求         │─── Accept-Language: zh-CN
│ (axios / fetch)            │─── Cookie: i18n_redirected=zh-CN
└────────────────────────────┘
    │
    ▼
┌────────────────────────────┐
│ LocaleContextMiddleware    │─── 读 accept-language
│ (FastAPI)                  │─── get_locale_provider("zh-CN")
│                            │─── set_locale_context()
└────────────────────────────┘
    │
    ▼
┌────────────────────────────┐
│ ProviderFactory.get()      │─── 命中 _store 缓存或加载 zh-CN.json
│                            │─── 若文件缺失 → 回退 en-US.json
└────────────────────────────┘
    │
    ▼
┌────────────────────────────┐
│ 业务代码调用 .t("key")     │─── 逐层查 JSON 字典
│ (Controller/Service)       │─── 变量替换 {name}
│                            │─── 复数处理 | 分隔
│                            │─── key 缺失 → 返回 default 或 key
└────────────────────────────┘
```
