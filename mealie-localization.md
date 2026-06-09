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

[locale_config.py](mealie/lang/locale_config.py)

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

[json_provider.py](mealie/pkgs/i18n/json_provider.py)

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

[provider_factory.py](mealie/pkgs/i18n/provider_factory.py)

`ProviderFactory` 负责：

- **文件加载** `_load(locale)`：按 `{locale}.json` 查找；不存在则使用 `fallback_locale`（默认 `"en-US"`）的文件
- **内存缓存** `_store`：使用 `InUseProvider(provider, locks)` 引用计数管理已加载的翻译器，避免重复 I/O
- **支持的语言列表** `supported_locales`：扫描目录下所有 JSON 文件自动得出

### 2.5 翻译器获取与依赖注入

[providers.py](mealie/lang/providers.py)

关键函数：

1. **`_load_factory()`**：`@lru_cache` 单例工厂，固定 fallback 为 `"en-US"`，翻译目录为 `mealie/lang/messages/`
2. **`get_locale_provider(accept_language)`**：FastAPI 可注入依赖（`Header(None)`），从请求的 `Accept-Language` 头获取 locale 字符串，返回对应 `JsonProvider`
3. **`get_locale_config(accept_language)`**：同理返回 `LocaleConfig`；locale 不在 `LOCALE_CONFIG` 中时回退到 `"en-US"`
4. **`set_locale_context(translator, locale_config)` / `get_locale_context()`**：使用 Python `contextvars.ContextVar` 存储请求级别的翻译器，供非路由层代码（如 Schema、Service）在 HTTP 请求上下文中使用

### 2.6 HTTP 中间件

[locale_context.py](mealie/middleware/locale_context.py)

`LocaleContextMiddleware` 在每个请求进入时：

1. 从 `request.headers.get("accept-language")` 读语言偏好
2. 获取 translator 和 locale_config
3. 调用 `set_locale_context()` 存入 ContextVar
4. 调用后续处理器

这使得在请求生命周期内的任何代码都可以通过 `get_locale_context()` 访问当前语言环境。

### 2.7 路由层使用方式

[base_controllers.py](mealie/routes/_base/base_controllers.py)

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

## 三、前端本地化实现与 Accept-Language 请求头深度分析

### 3.1 ⭐ Accept-Language 设置时机：useRequests 与 axios 全局单例

**核心结论：前端通过全局共享的 axios 单例的 defaults.headers.common 设置 Accept-Language，但设置时机存在潜在同步问题。**

#### 3.1.1 axios 实例的全局单例生命周期

[axios.ts#L10-L63](frontend/app/plugins/axios.ts#L10-L63)

```ts
export default defineNuxtPlugin(() => {
  const tokenName = useRuntimeConfig().public.AUTH_TOKEN;
  const axiosInstance = axios.create({   // ⭐ Nuxt 插件仅执行一次，全局只有这一个实例
    baseURL: "/",
    withCredentials: true,
  });

  axiosInstance.interceptors.request.use(
    (config) => {
      const token = useCookie(tokenName).value;
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;  // 拦截器只处理 Authorization
      }
      return config;
    },
    // ...
  );

  return {
    provide: {
      axios: axiosInstance,  // ⭐ 作为 $axios 全局提供，所有组件共享同一个实例
    },
  };
});
```

**关键点**：
- `$axios` 是 Nuxt 插件创建的**全局单例**，整个应用生命周期只有一个实例
- 请求拦截器只动态设置 `Authorization`，**不处理 `Accept-Language`**
- `defaults.headers.common` 是这个全局单例的共享属性，所有请求都会带上

#### 3.1.2 useRequests 的设置逻辑

[api-client.ts#L57-L66](frontend/app/composables/api/api-client.ts#L57-L66)

```ts
export const useRequests = function (i18n?: Composer): ApiRequestInstance {
  const { $axios } = useNuxtApp();       // 取全局单例
  if (!i18n) {
    i18n = useGlobalI18n();
  }

  // ⭐ 关键：每次调用 useRequests 都向全局单例的 defaults 写入当前 locale
  $axios.defaults.headers.common["Accept-Language"] = i18n.locale.value;

  return getRequests($axios);  // 返回的 requests 对象直接使用这个全局 $axios
};

// 所有 API 客户端工厂都走 useRequests
export const useAdminApi = function (i18n?: Composer): AdminAPI {
  const requests = useRequests(i18n);
  return new AdminAPI(requests);
};
export const useUserApi = function (i18n?: Composer): UserAPI { /* 同理 */ };
export const usePublicApi = function (i18n?: Composer): PublicApi { /* 同理 */ };
```

**设置时机分析**：

| 场景 | Accept-Language 是否更新 | 原因 |
|------|------------------------|------|
| 组件首次挂载，`<script setup>` 中调用 `useUserApi()` | ✅ 更新 | setup 执行时 `i18n.locale.value` 已由 `@nuxtjs/i18n` 初始化 |
| 用户切换语言后，**当前已挂载组件**发起请求（没有重新调用 useUserApi） | ⚠️ **取决于全局是否有其他组件重新调用了 useRequests** | 当前组件的 `const api = useUserApi()` 只在 setup 时执行一次，不重新执行就不会触发 defaults 更新 |
| 用户切换语言后，**路由切换到新页面**，新页面 setup 调用 `useUserApi()` | ✅ 更新 | 新页面 setup 重新执行，写入新的 locale |
| 用户切换语言后，**同一组件内的事件处理函数**（如按钮点击）调用已保存的 `api` 引用 | ⚠️ **可能不更新** | `api` 引用持有的 `requests` 对象在创建时使用了 $axios，但 $axios.defaults 是全局共享的——只要任何地方重新调用了 useRequests，所有请求都会使用新值 |
| `useAuthBackend` 中的认证请求（signIn/getSession/refresh/signOut） | ⚠️ **取决于其他组件是否已调用过 useRequests** | useAuthBackend 直接使用 `$axios.get/post`，**从不经过 useRequests**，因此从不主动设置 Accept-Language，完全依赖 defaults 的已有值 |

#### 3.1.3 各组件的使用模式

**典型模式：在 setup 顶层一次性持有 API 客户端引用**

绝大多数页面（如 [edit.vue#L256](frontend/app/pages/user/profile/edit.vue#L256)、[index.vue#L315](frontend/app/pages/user/profile/index.vue#L315)、[shopping-lists/index.vue#L138](frontend/app/pages/shopping-lists/index.vue#L138)、[api-tokens.vue#L131](frontend/app/pages/user/profile/api-tokens.vue#L131)、[reset-password.vue#L119](frontend/app/pages/reset-password.vue#L119)、[register/index.vue#L466](frontend/app/pages/register/index.vue#L466)）都采用：

```ts
const api = useUserApi();  // 只在 setup 时执行一次
```

Composable 层（如 [use-user.ts#L11](frontend/app/composables/use-user.ts#L11)、[use-user.ts#L27](frontend/app/composables/use-user.ts#L27)）也在函数体内调用，但每次调用该 composable 时才会重新执行。

**例外：useAuthBackend 不经过 useRequests**

[use-auth-backend.ts](frontend/app/composables/use-auth-backend.ts) 中的所有请求（`$axios.get("/api/users/self")`、`$axios.post("/api/auth/token")` 等）直接使用全局 `$axios`，**从不调用 `useRequests()`**。这些请求的 Accept-Language 完全依赖于 defaults 中已有的值。

### 3.2 ⭐ use-locales 切换语言时的同步机制

[use-locales.ts](frontend/app/composables/use-locales/use-locales.ts)

```ts
export const useLocales = () => {
  const i18n = useGlobalI18n();
  const { current: vuetifyLocale } = useLocale();

  const locale = computed<LocaleObject["code"]>({
    get: () => i18n.locale.value,
    set(value) {
      i18n.setLocale(value);  // 触发 @nuxtjs/i18n 内部：更新 cookie、懒加载语言文件、更新 i18n.locale
    },
  });

  function updateLocale(lc: LocaleObject["code"]) {
    vuetifyLocale.value = lc;  // 只同步 Vuetify 组件的 locale
  }

  // ⚠️ watch 只同步 Vuetify，不同步 axios Accept-Language
  watch(locale, (lc) => {
    updateLocale(lc);
  });

  if (i18n.locale.value) {
    updateLocale(i18n.locale.value);
  }

  return { locale, locales: LOCALES, i18n };
};
```

**语言切换时的同步覆盖范围**：

| 同步对象 | 是否同步 | 实现方式 |
|---------|---------|---------|
| vue-i18n 的 `i18n.locale` | ✅ | `i18n.setLocale(value)` 内部处理 |
| `i18n_redirected` cookie | ✅ | `@nuxtjs/i18n` 内部 `detectBrowserLanguage` 处理 |
| 新语言文件懒加载 | ✅ | `@nuxtjs/i18n` 内部 lazy loading |
| Vuetify 组件 locale | ✅ | `watch(locale, ...)` 中 `vuetifyLocale.value = lc` |
| **axios `Accept-Language` 请求头** | ❌ **不同步** | **没有任何 watch 或逻辑更新 `$axios.defaults.headers.common["Accept-Language"]`** |

### 3.3 ⭐ 复用 API 客户端可能出现的旧 locale 问题

#### 3.3.1 问题场景全景

由于 `$axios` 是全局单例，而 Accept-Language 仅在调用 `useRequests()` 时被写入 defaults，可能出现以下问题：

**场景一：单页内切换语言后立即发起请求**

```
用户处于页面 A（已挂载，setup 中执行过 const api = useUserApi()，Accept-Language=en-US）
    │
    ▼
用户打开 LanguageDialog，切换为 zh-CN
    │
    ├─ i18n.setLocale("zh-CN") → vue-i18n 更新、cookie 更新、Vuetify 更新
    │  ✅ 界面文字立即变成中文
    │
    ├─ ❌ axios.defaults.headers.common["Accept-Language"] 仍然是 "en-US"
    │     （没有任何 watch 更新它）
    │
    ▼
用户在当前页面点击某个按钮，触发 api.recipes.getAll()
    │
    ▼
请求发出，Header 中的 Accept-Language 仍是 "en-US"  ← ⚠️ 旧语言
    │
    ▼
后端返回英文的错误提示/通知消息
```

**场景二：认证请求不保证有 Accept-Language**

```
用户首次访问应用（未登录），直接进入 /login
    │
    ▼
init-auth.client.ts 插件执行 → auth.getSession()
    │
    ▼
useAuthBackend 中直接调用 $axios.get("/api/users/self")
    │
    ├─ 此时尚未有任何组件调用过 useUserApi()
    │  $axios.defaults.headers.common["Accept-Language"] 未设置
    │
    ▼
请求发出，Header 中没有 Accept-Language（或为浏览器默认值）
    │
    ▼
后端按默认 en-US 处理
```

**场景三：间接修复（由其他组件触发）**

```
用户在页面 A 切换语言（场景一的状态，Accept-Language 仍为旧值）
    │
    ▼
用户点击链接跳转到页面 B
    │
    ▼
页面 B 的 <script setup> 执行 const api = useUserApi()
    │
    ▼
useRequests() 被调用 → $axios.defaults.headers.common["Accept-Language"] = 新 locale
    │
    ▼
此时全局 $axios 的 defaults 已更新
    │
    ▼
用户再返回页面 A，点击按钮发起请求 → 使用新的 Accept-Language ✅
```

#### 3.3.2 问题的根本原因

| 设计问题 | 说明 |
|---------|------|
| **设置点与变更点分离** | Accept-Language 仅在 `useRequests()`（API 客户端创建时）写入，而语言变更发生在 `use-locales.ts` 的 `locale.set()`，两者之间没有任何同步机制 |
| **依赖隐式的全局副作用** | 依赖"新页面 setup 会重新调用 `useUserApi()`"这一副作用来间接更新 Accept-Language，而非显式 watch locale 变化 |
| **useAuthBackend 绕过 useRequests** | 认证相关请求不经过 useRequests，可能在任何 Accept-Language 状态下发起 |
| **写入的是全局 defaults 而非 per-request config** | 如果改为在 axios 请求拦截器中动态读取当前 locale，就能保证每个请求都使用最新值 |

### 3.4 浏览器语言检测与 Cookie 持久化

`@nuxtjs/i18n` 的 `detectBrowserLanguage` 配置（[nuxt.config.ts#L204-L208](frontend/nuxt.config.ts#L204-L208)）：

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

### 3.5 界面文本选择

**模板中**（全局 `$t` 函数）：

```vue
<v-toolbar-title>{{ $t('user.sign-in') }}</v-toolbar-title>
<v-text-field :label="$t('user.email-or-username')" />
```

**脚本中**（Composition API）：

```ts
const i18n = useI18n();
alert.error(i18n.t("user.please-enter-your-email-and-password"));
```

**运行时标签/食材名称的显示**：数据库中已存储了种子时使用的语言对应的名称（如中文的"大蒜"），前端直接通过 `{{ item.label.name }}` 或 `{{ food.name }}` 显示，**不再经过 i18n 翻译**。

---

## 四、多语言种子数据代码深度分析

### 4.1 seed_labels / MultiPurposeLabelSeeder 实现

[seeders.py#L18-L53](mealie/repos/seed/seeders.py#L18-L53)

**关键发现：`MultiPurposeLabelSeeder` 实际上读取的是 `foods/locales/{locale}.json`，而非 `labels/locales/{locale}.json`！**

```python
class MultiPurposeLabelSeeder(AbstractSeeder):
    @classmethod
    def get_file(cls, locale: str | None = None) -> pathlib.Path:
        # ⭐ 注释明确写着：从 foods 种子文件中获取 labels
        locale_path = cls.resources / "foods" / "locales" / f"{locale}.json"
        return locale_path if locale_path.exists() else foods.en_US

    def load_data(self, locale: str | None = None) -> Generator[MultiPurposeLabelSave, None, None]:
        file = self.get_file(locale)
        current_label_names = {label.name for label in self.get_all_labels()}
        # ⭐ 加载 foods locale 文件，取 JSON 的所有顶级 keys 作为标签名
        seed_label_names = set(filter(None, self.load_file(file).keys()))
        to_seed_labels = seed_label_names - current_label_names
        for label in to_seed_labels:
            yield MultiPurposeLabelSave(name=label, group_id=self.repos.group_id)
```

**调用链**：
1. 注册服务：[registration_service.py#L118](mealie/services/user_services/registration_service.py#L118) → `seeder_service.seed_labels(registration.locale)`
2. SeederService：[seeder_service.py#L15-L17](mealie/services/seeder/seeder_service.py#L15-L17)
3. 路由接口：[controller_seeder.py#L32-L34](mealie/routes/groups/controller_seeder.py#L32-L34)

### 4.2 foods locale 与 labels locale 的实际关联

foods JSON 文件结构（[en-US.json](mealie/repos/seed/resources/foods/locales/en-US.json)）：

```json
{
    "Vegetables & Greens": {        // ⭐ 这个顶级 key 就是标签名
        "foods": {                  // 该标签下的所有食材
            "garlic": { "name": "garlic", "plural_name": "garlic" },
            "onion": { "name": "onion", "plural_name": "onions" }
        }
    },
    "Dairy & Eggs": { "foods": { /* ... */ } }
}
```

**两种子的关联机制（共享同一份 foods 文件）**：

| 阶段 | MultiPurposeLabelSeeder.seed_labels() | IngredientFoodsSeeder.seed_foods() |
|------|--------------------------------------|-----------------------------------|
| 读取文件 | `foods/locales/{locale}.json` | `foods/locales/{locale}.json`（同一份） |
| 解析方式 | `self.load_file(file).keys()` → 取所有顶级 keys 作为标签名 | `for label, values in self.load_file(file).items()` → 遍历每个 key-value |
| 数据用途 | 创建 `MultiPurposeLabelSave` 标签记录 | 按标签名查找已有 Label，关联 `label_id` 后创建食材记录 |
| 代码位置 | [seeders.py#L32-L44](mealie/repos/seed/seeders.py#L32-L44) | [seeders.py#L103-L122](mealie/repos/seed/seeders.py#L103-L122) |
| 理想执行顺序 | **先执行**（创建标签） | **后执行**（引用已存在的 label_id） |

注册服务中的实际执行顺序（[registration_service.py#L115-L119](mealie/services/user_services/registration_service.py#L115-L119)）：

```python
seeder_service.seed_foods(registration.locale)   # ① foods 在前
seeder_service.seed_labels(registration.locale)  # ② labels 在后
seeder_service.seed_units(registration.locale)   # ③
```

⚠️ 注意：实际代码顺序 foods 在前、labels 在后。当 foods 找不到对应 label 时，`label_id` 设为 `None`（[seeders.py#L121](mealie/repos/seed/seeders.py#L121)），后续可手动编辑关联。

### 4.3 Crowdin labels 资源是否在运行时加载？—— 否，已废弃

**结论：Crowdin 中同步的 `mealie/repos/seed/resources/labels/locales/*.json` 文件完全不被运行时代码使用，是旧格式的遗留废弃资源。**

证据：

1. **代码层面零引用**：全局搜索 `resources/labels` 或 `labels/locales`，仅两个位置：
   - [crowdin.yml#L15-L16](crowdin.yml#L15-L16) —— Crowdin 同步配置
   - [convert_seed_files_to_new_format.py](dev/scripts/convert_seed_files_to_new_format.py) —— 一次性历史迁移脚本

2. **迁移脚本说明格式变更**：旧版本 foods 文件是 `{food_name: attrs}` 扁平字典，labels 单独一个数组文件 `[{"name": "Produce"}, ...]`；新版本将 label 名作为 foods 文件的顶级 key，因此 labels 文件不再需要。

3. **运行时无标签名翻译**：`mealie/lang/messages/*.json` 中没有任何与食材标签名称相关的 key。标签名称一旦被 seeds 写入数据库，就以该 locale 的文字直接存储，前端直接显示 `label.name`，不经过 i18n 运行时翻译。

**操作指南**：新增标签名应直接修改 `foods/locales/en-US.json` 的顶级 keys，而非修改 `labels/locales/` 下的文件。

### 4.4 注册 locale 的完整链路

注册请求中 locale 的完整传递路径：

```
前端 register/index.vue
    │  const { locale } = useLocales();         // 当前 UI 语言，如 "zh-CN"
    │  payload.locale = locale.value;
    │  payload.seedData = true;
    ▼
POST /api/users/register
    Body: { "email": "...", "locale": "zh-CN", "seedData": true, ... }
    Header: Accept-Language: zh-CN （同时存在，用于响应消息翻译）
    │
    ▼
后端 CreateUserRegistration Schema 校验
    │  [registration.py#L22-L29]
    │  locale: str = "en-US" （默认值）
    │  @field_validator → validate_locale() 白名单校验
    │
    ▼
RegistrationService.register_user()
    │  [registration_service.py#L115-L119]
    │  if seedData:
    │      seeder_service.seed_foods("zh-CN")   // 读取 foods/locales/zh-CN.json
    │      seeder_service.seed_labels("zh-CN")  // 读取 foods/locales/zh-CN.json 的 keys
    │      seeder_service.seed_units("zh-CN")   // 读取 units/locales/zh-CN.json
    │
    ▼
食材名（"大蒜"）、标签名（"蔬菜"）、单位名（"克"）等以中文写入数据库
    │
    ▼
后续界面无论切换到什么语言，数据库中存储的中文名称直接显示，不做运行时翻译
```

---

## 五、回退机制与语言来源综合对比

### 5.1 两条独立语言来源路径

| 对比维度 | 普通接口语言（Accept-Language） | 注册/种子数据 locale |
|---------|------------------------------|-------------------|
| **传输载体** | HTTP Header: `Accept-Language` | HTTP Request Body: `{"locale": "zh-CN"}` |
| **前端写入位置** | `useRequests()` 中 `$axios.defaults.headers.common["Accept-Language"]` | 各页面手动放入 API payload（register、units/foods/labels 管理页） |
| **前端设置时机** | 调用 `useUserApi()` / `useAdminApi()` 时（组件 setup 或 composable 调用） | 用户提交表单/点击按钮时 |
| **语言切换时同步** | ❌ 无显式同步，依赖重新调用 useRequests 的副作用 | ✅ 每次请求都传入最新选择 |
| **后端读取方式** | `Header(None)` 依赖注入 + `LocaleContextMiddleware` | Pydantic Model 字段解析（`CreateUserRegistration` / `SeederConfig`） |
| **默认值** | Header 为空 → `"en-US"` | Schema `locale: str = "en-US"` |
| **校验机制** | 无显式校验（不存在的 locale 由 ProviderFactory 自动回退） | `validate_locale()` 白名单校验，非法值抛 422 |
| **影响范围** | 接口响应消息、邮件通知、错误文本、Schema 显示格式化 | **持久化到数据库**（foods/units/labels 表的 name 字段） |
| **翻译资源目录** | `mealie/lang/messages/{locale}.json` | `mealie/repos/seed/resources/{foods,units}/locales/{locale}.json`（labels 共享 foods 文件） |
| **是否持久化** | 不持久化，每次请求动态决定 | 持久化到数据库，后续不再改变 |
| **能否与 UI 语言不同** | 不能，始终等于当前 i18n locale | **能**，种子页用户可单独选择不同语言导入 |

### 5.2 完整回退链总览

**后端回退链**：

| 层级 | 场景 | 回退行为 | 代码位置 |
|------|------|----------|---------|
| ProviderFactory | locale JSON 文件不存在于 `mealie/lang/messages/` | 加载 `en-US.json` | [provider_factory.py#L30-L34](mealie/pkgs/i18n/provider_factory.py#L30-L34) |
| JsonProvider.t() | key 在翻译字典中不存在 | 返回 `default` 参数；未指定则返回 key 本身 | [json_provider.py#L58](mealie/pkgs/i18n/json_provider.py#L58) |
| get_locale_config() | locale 不在 LOCALE_CONFIG | 返回 `en-US` 的 LocaleConfig | [providers.py#L49-L53](mealie/lang/providers.py#L49-L53) |
| get_locale_provider() | Accept-Language Header 为空 | 默认为 `"en-US"` | [providers.py#L43-L46](mealie/lang/providers.py#L43-L46) |
| CreateUserRegistration Schema | 前端未传 locale 字段 | 默认为 `"en-US"` | [registration.py#L23](mealie/schema/user/registration.py#L23) |
| MultiPurposeLabelSeeder.get_file() | foods locale 文件不存在 | 回退到 `foods.en_US` | [seeders.py#L26-L27](mealie/repos/seed/seeders.py#L26-L27) |
| IngredientFoodsSeeder.get_file() | foods locale 文件不存在 | 回退到 `foods.en_US` | [seeders.py#L94-L95](mealie/repos/seed/seeders.py#L94-L95) |
| IngredientUnitsSeeder.get_file() | units locale 文件不存在 | 回退到 `units.en_US` | [seeders.py#L58-L59](mealie/repos/seed/seeders.py#L58-L59) |
| IngredientFoodsSeeder.load_data() | 食材找不到对应 label | `label_id` 设为 `None` | [seeders.py#L121](mealie/repos/seed/seeders.py#L121) |

**前端回退链**：

| 层级 | 场景 | 回退行为 | 代码位置 |
|------|------|----------|---------|
| @nuxtjs/i18n detectBrowserLanguage | 浏览器语言不匹配 / cookie 不存在 | `fallbackLocale: "en-US"` | [nuxt.config.ts#L207](frontend/nuxt.config.ts#L207) |
| vue-i18n | key 不存在于当前语言文件 | `fallbackLocale: "en-US"`（控制台输出警告） | [i18n.config.ts#L97-L98](frontend/app/i18n.config.ts#L97-L98) |
| Vuetify locale | locale 切换 | 同步切换，默认 fallback `"en-US"` | [nuxt.config.ts#L250-L253](frontend/nuxt.config.ts#L250-L253) |

### 5.3 界面文本选择的完整决策流

```
用户访问页面
    │
    ├─ 1. 读取 cookie i18n_redirected → 确定当前 locale（默认 en-US）
    │      ↓
    ├─ 2. 懒加载对应语言文件，合并 messages JSON + Vuetify locale
    │
    ├─ 3. 界面框架层文字（按钮、标签、菜单等）：
    │      {{ $t('user.sign-in') }} → vue-i18n 查当前语言
    │                                    ↓ 找不到 → fallback en-US
    │                                    ↓ 还找不到 → 返回 key 本身
    │
    ├─ 4. API 调用（Accept-Language 可能存在同步问题）：
    │      │
    │      ├─ 4a. 组件 setup 中调用 useUserApi()
    │      │       → $axios.defaults.headers.common["Accept-Language"] = 当前 locale  ✅
    │      │
    │      ├─ 4b. 用户切换语言后当前组件不重新挂载
    │      │       → use-locales watch 只同步 Vuetify，不同步 axios defaults  ❌
    │      │       → Accept-Language 仍为旧值（直到其他组件重新调用 useRequests）
    │      │
    │      └─ 4c. useAuthBackend 认证请求
    │              → 直接使用 $axios，不经过 useRequests
    │              → Accept-Language 取决于 defaults 是否被其他组件设置过
    │
    ├─ 5. 后端处理：
    │      LocaleContextMiddleware 读 Accept-Language
    │         ↓
    │      接口响应消息（错误提示、成功通知）：
    │         translator.t("exceptions.permission_denied")
    │         ↓ 查 mealie/lang/messages/{locale}.json
    │         ↓ 找不到 → fallback en-US
    │
    └─ 6. 业务数据层文字（食材名、标签名、单位名）：
           直接显示数据库中存储的 name 字段
              ↓ 该名称是注册/种子时用 seed locale 写入的，固定不变
              ↓ 不经过任何 i18n 运行时翻译
              ↓ 如种子时选 zh-CN → 存"大蒜"；无论界面切什么语言都显示"大蒜"
```

---

## 六、翻译资源管理与 Crowdin 同步

### 6.1 Crowdin 配置中的 5 组资源（含已废弃的 labels）

[crowdin.yml](crowdin.yml)

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

[gen_ts_locales.py](dev/code-generation/gen_ts_locales.py) 是关键的前后端协作工具：

1. 从 Crowdin API 获取所有目标语言的元数据和翻译进度
2. 合并 `LOCALE_CONFIG` 的语言名称、方向、复数策略
3. 生成 `frontend/app/composables/use-locales/available-locales.ts`
4. 自动注入 `nuxt.config.ts` 的 `i18n.locales` 数组
5. 自动注入 `i18n.config.ts` 的 `datetimeFormats`
6. 注入 `mealie/schema/_mealie/validators.py` 的 `validate_locale()` 白名单

---

## 七、综合结论

| 问题 | 结论 |
|------|------|
| useRequests 中 Accept-Language 的设置时机？ | 每次调用 `useUserApi()` / `useAdminApi()` / `useRequests()` 时，向**全局共享的 `$axios` 单例**的 `defaults.headers.common["Accept-Language"]` 写入当前 `i18n.locale.value`。设置仅发生在调用时刻，不动态跟随 locale 变化。代码位置：[api-client.ts#L63](frontend/app/composables/api/api-client.ts#L63) |
| use-locales 切换语言时是否同步请求头？ | **不同步**。`watch(locale, ...)` 只同步更新 Vuetify locale，没有任何逻辑更新 `$axios.defaults.headers.common["Accept-Language"]`。切换语言后，只有当新页面 setup 重新调用了 `useUserApi()`，Accept-Language 才会被间接更新。代码位置：[use-locales.ts#L21-L23](frontend/app/composables/use-locales/use-locales.ts#L21-L23) |
| 复用 API 客户端可能出现什么旧 locale 问题？ | ①单页内切换语言后立即发请求 → Accept-Language 可能仍是旧值；②`useAuthBackend` 的认证请求从不经过 useRequests → Accept-Language 完全依赖 defaults 是否被其他组件设置过；③根本原因是"设置点（useRequests）与变更点（use-locales set）"分离，且未使用请求拦截器动态读取。 |
| seed_labels / MultiPurposeLabelSeeder 读取哪个文件？ | **读取 `foods/locales/{locale}.json`**，取 JSON 顶级 keys 作为标签名。代码注释明确写着 "Get the labels from the foods seed file now"。代码位置：[seeders.py#L24-L27](mealie/repos/seed/seeders.py#L24-L27) |
| foods locale 与 labels locale 的实际关联？ | **两者共享同一份 foods JSON 文件**。labels seeder 取 `keys()` 创建标签记录；foods seeder 遍历 `items()`，通过标签名查找对应记录并关联 `label_id` 后创建食材。 |
| Crowdin 的 labels 资源是否在运行时加载？ | **否**。`mealie/repos/seed/resources/labels/locales/*.json` 是旧格式遗留，完全不被运行时代码使用。仅 Crowdin 配置和一次性迁移脚本引用它。新增标签名应直接修改 foods JSON 的顶级 keys。 |
| 注册 locale 与普通接口语言来源是否相同？ | **不同**。前者来自 Request Body 的 `locale` 字段（用户显式选择，持久化到数据库的食材/标签/单位名）；后者来自 HTTP Header 的 `Accept-Language`（跟随当前 UI 语言，用于翻译响应消息/邮件，不持久化）。 |
| 默认回退机制的最终兜底语言？ | **全部为 `en-US`**。无论是前端 vue-i18n、ProviderFactory 文件加载、种子数据文件查找、Schema 默认值，所有回退链路最终都指向 en-US。 |
| 界面文本如何选择语言？ | 分三层：① UI 框架文字 → vue-i18n 实时翻译（`$t()` / `i18n.t()`）；② API 响应消息 → 后端 JsonProvider 按 Accept-Language 翻译；③ 业务数据（食材/标签/单位名）→ 直接显示数据库中种子时写入的固定名称，不做运行时翻译。 |
