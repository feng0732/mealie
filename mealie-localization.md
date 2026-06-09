# Mealie 本地化与多语言资源代码协作分析

## 一、总体架构概览

Mealie 采用前后端分离的本地化架构：

- **后端**：Python/FastAPI，自建轻量 i18n 框架（`mealie/pkgs/i18n/`）
- **前端**：Nuxt 3 + Vue 3，使用 `@nuxtjs/i18n`（vue-i18n 桥接）+ Vuetify 内置国际化
- **翻译资源管理**：通过 Crowdin 平台协作，CI/CD 自动同步
- **前后端语言同步**：前端在 API 调用层**显式写入 `Accept-Language` 请求头**，后端通过 Header 读取
- **种子数据**：foods/units/labels 三类多语言种子数据，其中 labels 资源存在特殊的代码复用机制

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
        ├── foods/locales/*.json     # ⭐ foods 与 labels 共享此文件的 keys
        ├── units/locales/*.json
        └── labels/locales/*.json   # ⚠️ 遗留废弃，代码不使用此目录
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

---

## 三、前端本地化实现（Nuxt 3 + Vue 3）

### 3.1 ⭐ 前端在请求头写入 Accept-Language —— 显式写入

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

### 3.2 浏览器语言检测与 Cookie 持久化

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
3. **用户手动切换**：调用 `i18n.setLocale(value)`，`@nuxtjs/i18n` 内部自动更新 cookie
4. **再次访问**：优先读取 `i18n_redirected` cookie，跳过浏览器检测

### 3.3 界面文本选择

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

**运行时标签/食材名称的显示**：数据库中已存储了种子时使用的语言对应的名称（如中文的"大蒜"），前端直接通过 `{{ item.label.name }}` 或 `{{ food.name }}` 显示，**不再经过 i18n 翻译**。标签的名称在种子数据导入时就已决定，不会随界面语言切换而改变。

---

## 四、多语言种子数据代码深度分析

### 4.1 seed_labels 与 MultiPurposeLabelSeeder 实现

[seeders.py#L18-L53](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L18-L53)

**关键发现：`MultiPurposeLabelSeeder` 实际上读取的是 `foods/locales/{locale}.json`，而非 `labels/locales/{locale}.json`！**

```python
class MultiPurposeLabelSeeder(AbstractSeeder):
    @cached_property
    def service(self):
        return MultiPurposeLabelService(self.repos)

    @classmethod
    def get_file(cls, locale: str | None = None) -> pathlib.Path:
        # ⭐ 注释明确写着：从 foods 种子文件中获取 labels
        locale_path = cls.resources / "foods" / "locales" / f"{locale}.json"
        return locale_path if locale_path.exists() else foods.en_US

    def get_all_labels(self) -> list[MultiPurposeLabelOut]:
        return self.repos.group_multi_purpose_labels.get_all()

    def load_data(self, locale: str | None = None) -> Generator[MultiPurposeLabelSave, None, None]:
        file = self.get_file(locale)

        current_label_names = {label.name for label in self.get_all_labels()}
        # ⭐ 加载 foods locale 文件，取 JSON 的所有顶级 keys 作为标签名
        seed_label_names = set(filter(None, self.load_file(file).keys()))
        to_seed_labels = seed_label_names - current_label_names
        for label in to_seed_labels:
            yield MultiPurposeLabelSave(
                name=label,
                group_id=self.repos.group_id,
            )
```

**调用链**：
1. 注册服务：[registration_service.py#L118](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/services/user_services/registration_service.py#L118)
   ```python
   seeder_service.seed_labels(registration.locale)
   ```
2. SeederService：[seeder_service.py#L15-L17](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/services/seeder/seeder_service.py#L15-L17)
   ```python
   def seed_labels(self, locale: str) -> None:
       seeder = MultiPurposeLabelSeeder(self.repos, self.logger)
       seeder.seed(locale)
   ```
3. 路由接口：[controller_seeder.py#L32-L34](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/routes/groups/controller_seeder.py#L32-L34)
   ```python
   @router.post("/labels", response_model=SuccessResponse)
   def seed_labels(self, data: SeederConfig) -> dict:
       return self._wrap(lambda: self.service.seed_labels(data.locale))
   ```

### 4.2 foods locale 与 labels locale 的实际关联

foods JSON 文件结构（[en-US.json](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/resources/foods/locales/en-US.json)）：

```json
{
    "Vegetables & Greens": {        // ⭐ 这个顶级 key 就是标签名
        "foods": {                  // 标签下的所有食材
            "garlic": {
                "aliases": [],
                "description": "",
                "name": "garlic",
                "plural_name": "garlic"
            },
            "onion": {
                "name": "onion",
                "plural_name": "onions"
            }
            // ...
        }
    },
    "Dairy & Eggs": {              // ⭐ 另一个标签名
        "foods": { /* ... */ }
    }
    // ...
}
```

**关联机制**（两种子共享同一份 foods 文件）：

| 阶段 | MultiPurposeLabelSeeder.seed_labels() | IngredientFoodsSeeder.seed_foods() |
|------|--------------------------------------|-----------------------------------|
| 读取文件 | `foods/locales/{locale}.json` | `foods/locales/{locale}.json`（同一份） |
| 解析方式 | 取所有顶级 keys → 作为标签名 | 遍历每个顶级 key → 获取对应标签对象 → 创建食材并关联 label_id |
| 关键代码 | `self.load_file(file).keys()` | `for label, values in self.load_file(file).items()` |
| 执行顺序 | **必须先执行**（创建标签） | **必须后执行**（引用已存在的 label_id） |

注册服务中的执行顺序（[registration_service.py#L115-L119](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/services/user_services/registration_service.py#L115-L119)）：

```python
if new_group and registration.seed_data:
    seeder_service = SeederService(self.repos)
    seeder_service.seed_foods(registration.locale)   # ①
    seeder_service.seed_labels(registration.locale)  # ② —— 注意：实际代码中 foods 在前 labels 在后
    seeder_service.seed_units(registration.locale)   # ③
```

⚠️ 注意：虽然从依赖关系上 labels 应该先执行（foods 需要关联 label_id），但实际代码顺序是 foods 在前、labels 在后。这是因为 foods 创建时如果找不到对应 label，会将 `label_id` 设为 `None`（[seeders.py#L121](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L121)），后续可以通过手动编辑关联标签。

### 4.3 Crowdin labels 资源是否在运行时加载？—— 否，已废弃

**结论：Crowdin 中同步的 `mealie/repos/seed/resources/labels/locales/*.json` 文件完全不被运行时代码使用，是旧格式的遗留废弃资源。**

证据：

1. **代码层面**：全局搜索 `resources/labels` 或 `labels/locales`，仅两个引用：
   - `crowdin.yml`（配置同步路径，[crowdin.yml#L15-L16](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/crowdin.yml#L15-L16)）
   - `dev/scripts/convert_seed_files_to_new_format.py`（一次性历史迁移脚本）

2. **迁移脚本说明**：[convert_seed_files_to_new_format.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/dev/scripts/convert_seed_files_to_new_format.py) 的注释解释了格式变更：

   ```python
   """
   Convert the current food seed file for a locale into a new format which maps each food to a label

   Old format: foods/{locale}.json 是 {food_name: {attrs...}} 的扁平字典
               labels/{locale}.json 是 [{"name": "Produce"}, ...] 的数组

   New format: foods/{locale}.json 的顶级 key 就是 label 名，
               value 是 {"foods": {food_key: {attrs...}}}
               因此 labels 文件不再需要
   """

   def transform_foods(locale: str):
       # Seeding for labels now pulls from the foods file and parses the labels from there
       #   (as top-level keys), thus we need to add all of the existing labels to the new
       #   food seed file and give them an empty foods dictionary
       label_names = get_labels_from_file(locale)  # 从旧 labels 文件读取
       for label in label_names:
           transformed_data[label] = {"foods": {}}  # 作为顶级 key 放入 foods 文件
   ```

3. **新 labels 文件内容**：新格式下 `labels/locales/*.json` 仍保留旧格式（数组 `[{"name": "Produce"}, ...]`），但 `MultiPurposeLabelSeeder.get_file()` 完全不读取它。

4. **运行时无额外翻译**：`mealie/lang/messages/*.json`（接口响应翻译资源）中没有任何与食材标签名称相关的 key。标签名称一旦被 seeds 写入数据库，就以该 locale 的文字直接存储，前端直接显示 `label.name`，不经过 i18n 运行时翻译。

**总结**：Crowdin 上的 labels 资源目录仅作为历史遗留存在，实际代码读取的是 foods 文件的顶级 keys。如果新增标签名，需要直接在 foods JSON 文件中添加顶级 key。

---

## 五、三种语言来源与回退机制综合对比

### 5.1 两条独立语言来源路径

| 对比维度 | 普通接口语言 | 注册/种子数据 locale |
|---------|------------|-------------------|
| **传输载体** | HTTP Header: `Accept-Language` | HTTP Request Body: `{"locale": "zh-CN"}` |
| **前端写入位置** | `useRequests()` 中 `$axios.defaults.headers.common["Accept-Language"]` | 各页面手动放入 API payload（register、units/foods/labels 管理页） |
| **后端读取方式** | `Header(None)` 依赖注入 + `LocaleContextMiddleware` | Pydantic Model 字段解析（`CreateUserRegistration` / `SeederConfig`） |
| **默认值** | Header 为空 → `"en-US"` | Schema `locale: str = "en-US"` |
| **校验机制** | 无显式校验（不存在的 locale 由 ProviderFactory 自动回退） | `validate_locale()` 白名单校验，非法值抛 422 |
| **影响范围** | 接口响应消息、邮件通知、错误文本、Schema 显示格式化 | **持久化到数据库**（foods/units/labels 表的 name 字段） |
| **翻译资源目录** | `mealie/lang/messages/{locale}.json` | `mealie/repos/seed/resources/{foods,units}/locales/{locale}.json`（labels 共享 foods 文件） |
| **是否持久化** | 不持久化，每次请求动态决定 | 持久化到数据库，后续不再改变 |
| **能否与 UI 语言不同** | 不能，始终等于当前 i18n locale | **能**，种子页用户可单独选择不同语言导入 |

### 5.2 完整回退链

**后端回退链**：

| 层级 | 场景 | 回退行为 | 位置 |
|------|------|----------|------|
| ProviderFactory | 请求的 locale JSON 文件不存在于 `mealie/lang/messages/` | 加载 `en-US.json` | [provider_factory.py#L30-L34](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/provider_factory.py#L30-L34) |
| JsonProvider.t() | key 在翻译字典中不存在 | 返回 `default` 参数；未指定则返回 key 本身 | [json_provider.py#L58](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/pkgs/i18n/json_provider.py#L58) |
| get_locale_config() | locale 不在 LOCALE_CONFIG | 返回 `en-US` 的 LocaleConfig | [providers.py#L49-L53](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L49-L53) |
| get_locale_provider() | Accept-Language Header 为空 | 默认为 `"en-US"` | [providers.py#L43-L46](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/lang/providers.py#L43-L46) |
| CreateUserRegistration Schema | 前端未传 locale 字段 | 默认为 `"en-US"` | [registration.py#L23](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/schema/user/registration.py#L23) |
| MultiPurposeLabelSeeder.get_file() | foods locale 文件不存在 | 回退到 `foods.en_US`（即 `en-US.json`） | [seeders.py#L26-L27](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L26-L27) |
| IngredientFoodsSeeder.get_file() | foods locale 文件不存在 | 回退到 `foods.en_US` | [seeders.py#L94-L95](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L94-L95) |
| IngredientUnitsSeeder.get_file() | units locale 文件不存在 | 回退到 `units.en_US` | [seeders.py#L58-L59](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L58-L59) |
| IngredientFoodsSeeder.load_data() | 食材找不到对应 label | `label_id` 设为 `None` | [seeders.py#L121](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/mealie/repos/seed/seeders.py#L121) |

**前端回退链**：

| 层级 | 场景 | 回退行为 | 位置 |
|------|------|----------|------|
| @nuxtjs/i18n detectBrowserLanguage | 浏览器语言不匹配 / cookie 不存在 | `fallbackLocale: "en-US"` | [nuxt.config.ts#L207](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L207) |
| vue-i18n | key 不存在于当前语言文件 | `fallbackLocale: "en-US"`（`fallbackWarn: true` 时控制台输出警告） | [i18n.config.ts#L97-L98](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/i18n.config.ts#L97-L98) |
| Vuetify locale | locale 切换 | 同步切换，默认 fallback `"en-US"` | [nuxt.config.ts#L250-L253](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/nuxt.config.ts#L250-L253) |

### 5.3 界面文本选择的完整决策流

```
用户访问页面
    │
    ├─ 1. 读取 cookie i18n_redirected → 确定当前 locale（默认 en-US）
    │      ↓
    ├─ 2. 懒加载对应语言文件（en-US.ts）
    │      ↓ 合并 messages JSON + Vuetify locale
    │
    ├─ 3. 界面框架层文字（按钮、标签、菜单等）：
    │      {{ $t('user.sign-in') }} → vue-i18n 查当前语言
    │                                    ↓ 找不到 → fallback en-US
    │                                    ↓ 还找不到 → 返回 key 本身
    │
    ├─ 4. API 调用：
    │      useUserApi() → $axios.defaults.headers.common["Accept-Language"] = 当前 locale
    │         ↓
    │      后端 LocaleContextMiddleware 读 Accept-Language
    │         ↓
    │      接口响应消息（错误提示、成功通知）：
    │         translator.t("exceptions.permission_denied")
    │         ↓ 查 mealie/lang/messages/{locale}.json
    │         ↓ 找不到 → fallback en-US
    │         ↓ 还找不到 → 返回 default 或 key
    │
    └─ 5. 业务数据层文字（食材名、标签名、单位名）：
           直接显示数据库中存储的 name 字段
              ↓ 该名称是注册/种子时用 seed locale 写入的，固定不变
              ↓ 不经过任何 i18n 运行时翻译
              ↓ 如种子时选 zh-CN → 存"大蒜"；无论界面切什么语言都显示"大蒜"
```

---

## 六、翻译资源管理与 Crowdin 同步

### 6.1 Crowdin 配置中的 5 组资源（含已废弃的 labels）

[crowdin.yml](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/crowdin.yml)

```yaml
files:
  - source: /frontend/app/lang/messages/en-US.json    # 前端界面文本 ✅ 有效
    translation: /frontend/app/lang/messages/%locale%.json
  - source: /mealie/lang/messages/en-US.json          # 后端异常、邮件、通知文本 ✅ 有效
    translation: /mealie/lang/messages/%locale%.json
  - source: /mealie/repos/seed/resources/foods/locales/en-US.json   # 内置食材/标签名称 ✅ 有效
    translation: /mealie/repos/seed/resources/foods/locales/%locale%.json
  - source: /mealie/repos/seed/resources/units/locales/en-US.json   # 内置单位名称 ✅ 有效
    translation: /mealie/repos/seed/resources/units/locales/%locale%.json
  - source: /mealie/repos/seed/resources/labels/locales/en-US.json  # ⚠️ 废弃！代码不使用
    translation: /mealie/repos/seed/resources/labels/locales/%locale%.json
```

### 6.2 代码生成器

[gen_ts_locales.py](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/dev/code-generation/gen_ts_locales.py) 是关键的前后端协作工具：

**功能**：
1. 从 Crowdin API（或本地回退）获取所有目标语言的元数据和翻译进度
2. 合并 `LOCALE_CONFIG` 中定义的语言名称、方向、复数策略
3. 生成 `frontend/app/composables/use-locales/available-locales.ts`（语言选择器数据）
4. 自动扫描 JSON 文件并注入 `nuxt.config.ts` 的 `i18n.locales` 数组
5. 自动扫描日期格式 JSON 并注入 `i18n.config.ts` 的 `datetimeFormats`
6. 注入 `mealie/schema/_mealie/validators.py` 的 `validate_locale()` 白名单

---

## 七、综合结论

| 问题 | 结论 |
|------|------|
| 前端是否在请求头中写入 Accept-Language？ | **是**。在 [api-client.ts#L63](file:///d:/fz/0601/solo-dogfeeding/code/123-mealie/frontend/app/composables/api/api-client.ts#L63) 通过 `$axios.defaults.headers.common["Accept-Language"] = i18n.locale.value` 显式写入，每次调用 `useUserApi()` / `useAdminApi()` / `usePublicApi()` 时设置。 |
| seed_labels / MultiPurposeLabelSeeder 读取哪个文件？ | **读取 `foods/locales/{locale}.json`**，取 JSON 顶级 keys 作为标签名。代码注释明确写着 "Get the labels from the foods seed file now"。 |
| foods locale 与 labels locale 的实际关联？ | **两者共享同一份 foods JSON 文件**。labels seeder 取 keys 创建标签记录；foods seeder 遍历 key-value 对，通过 label 名查找对应记录并关联 `label_id`。 |
| Crowdin 的 labels 资源是否在运行时加载？ | **否**。`mealie/repos/seed/resources/labels/locales/*.json` 是旧格式遗留，完全不被代码使用。仅 Crowdin 配置和一次性迁移脚本引用它。新增标签名应直接修改 foods JSON 的顶级 keys。 |
| 注册种子 locale 与普通接口语言来源是否相同？ | **不同**。前者来自 Request Body 的 `locale` 字段（用户显式选择，持久化到数据库）；后者来自 HTTP Header 的 `Accept-Language`（跟随当前 UI 语言，不持久化）。 |
| 默认回退机制的最终兜底语言？ | **全部为 `en-US`**。无论是前端 vue-i18n、ProviderFactory 文件加载、种子数据文件查找、Schema 默认值，所有回退链路最终都指向 en-US。 |
| 界面文本如何选择语言？ | 分三层：① UI 框架文字 → vue-i18n 实时翻译；② API 响应消息 → 后端 JsonProvider 按 Accept-Language 翻译；③ 业务数据（食材/标签/单位名）→ 直接显示数据库中种子时写入的固定名称，不做运行时翻译。 |
