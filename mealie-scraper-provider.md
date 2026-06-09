# Mealie Recipe Scraper Provider 链路分析

## 整体架构概览

Mealie 的食谱抓取系统采用 **策略模式 + 责任链** 的组合设计，通过多层抽象屏蔽不同来源的差异，最终将异构数据统一落库为标准 Recipe 对象。

```
用户请求 → API路由层(URL预处理分支) → 服务入口层(调度器前置网页抓取)
    → 策略执行层(责任链) → 字段清理层 → 标签分类双通道汇合 → 数据落库层
```

**前置知识：MealieModel 的 alias 机制**

所有 Recipe 相关模型继承自 [MealieModel](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/_mealie/mealie_model.py#L45-L53)：

```python
model_config = ConfigDict(alias_generator=camelize, populate_by_name=True)
```

- `alias_generator=camelize`：所有蛇形字段名**自动生成驼峰别名**，如 `recipe_category` → `recipeCategory`，`org_url` → `orgURL`
- `populate_by_name=True`：构造对象时字段名和别名都可使用

这个机制是理解字段清理层和 Recipe 对象构造的关键前提。

---

## 一、抓取入口：URL 与 HTML/JSON 两条路径的真实差异

### 1.1 API 路由总览

所有抓取入口集中在 [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py)，共 **6 个路由**，按输入类型分为两大类，每类各有阻塞和流式两种返回方式：

| 路由 | 方法 | 请求 Schema | 说明 |
|------|------|-------------|------|
| `/recipes/create/url` | POST | `ScrapeRecipe` | URL → 阻塞返回 slug |
| `/recipes/create/url/stream` | POST | `ScrapeRecipe` | URL → SSE 流式返回 |
| `/recipes/create/html-or-json` | POST | `ScrapeRecipeData` | HTML/JSON → 阻塞返回 slug |
| `/recipes/create/html-or-json/stream` | POST | `ScrapeRecipeData` | HTML/JSON → SSE 流式返回 |
| `/recipes/create/url/bulk` | POST | `CreateRecipeByUrlBulk` | 批量 URL 后台异步 |
| `/recipes/test-scrape-url` | POST | `ScrapeRecipeTest` | 调试用，返回原始 schema 数据 |

两种请求 Schema 定义在 [recipe_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/recipe/recipe_scraper.py#L11-L34)：

```python
class ScrapeRecipe(ScrapeRecipeBase):
    url: str                    # 必填 URL

class ScrapeRecipeData(ScrapeRecipeBase):
    data: str                   # 必填 HTML 或 schema.org JSON 字符串
    url: str | None = None      # 可选，仅作为 org_url 记录
```

两者共同继承 `ScrapeRecipeBase`，带有 `include_tags` / `include_categories` 两个布尔开关（默认均为 False）。

### 1.2 路由层的预处理分支（关键差异点）

**URL 入口路径**（[recipe_crud_routes.py#L173-L194](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L173-L194)）：
- 阻塞版和流式版都**直接**把 `req` 传给 `_create_recipe_from_web(req)`，不做任何预处理
- 在 `_create_recipe_from_web` 内部分派：`html = None`, `url = req.url`

**HTML/JSON 入口路径**（[recipe_crud_routes.py#L144-L171](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L144-L171)）：
- 在调用 `_create_recipe_from_web` **之前**，路由层先做一步预处理：
  ```python
  if req.data.startswith("{"):
      req.data = RecipeScraperPackage.ld_json_to_html(req.data)
  ```
  如果传入的是纯 JSON（schema.org Recipe 对象），会被包装成一个包含 `<script type="application/ld+json">` 的**假 HTML 文档**。
- 在 `_create_recipe_from_web` 内部分派：`html = req.data`, `url = req.url or ""`

**阻塞 vs 流式的差异**：仅在于 SSE 事件的消费方式——阻塞版内部消费 SSE 事件直到收到 DONE，提取 slug 返回；流式版直接 yield 所有 SSE 事件给客户端。两条路径的业务逻辑完全一致，都走 `_create_recipe_from_web`。

### 1.3 服务统一入口：create_from_html()

位于 [scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper.py#L26-L85)。

这里有一个**与输入类型强相关的分支**（[scraper.py#L46-L50](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper.py#L46-L50)）：

```python
if not html:
    extracted_url = regex_search(r"(https?://|www\.)[^\s]+", url)
    if not extracted_url:
        raise HTTPException(...)
    url = extracted_url.group(0)
```

- **URL 入口**（`html=None`）：执行 URL 正则校验和提取
- **HTML/JSON 入口**（`html` 已有值）：**跳过** URL 正则校验，直接使用传入的 url（可能为空字符串）

之后两条路径汇合：调用 `scraper.scrape(url, html, on_progress)` → 图片下载 → 生成 UUID/slug → 兜底处理。

---

## 二、调度器前置网页抓取与策略责任链

### 2.1 调度器：RecipeScraper.scrape()

位于 [recipe_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_scraper.py#L46-L90)。

**关键细节：网页抓取发生在策略遍历之前**（[recipe_scraper.py#L58-L64](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_scraper.py#L58-L64)）：

```python
if not html:
    if on_progress:
        await on_progress(self.translator.t("recipe.create-progress.fetching-webpage"))
    html = await safe_scrape_html(url)
    if not html:
        return None, None
```

这意味着：
- **URL 入口**（`html=None`）：无论最终命中哪个策略，**必先执行一次 `safe_scrape_html(url)` 抓取网页**，抓不到直接返回失败
- **HTML/JSON 入口**（`html` 已有值）：跳过此步，直接进入策略遍历

即使是视频转录策略，在 URL 入口下也已经先做了一次网页抓取。

### 2.2 策略优先级与责任链遍历

默认策略顺序（[recipe_scraper.py#L19-L24](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_scraper.py#L19-L24)）：

```python
DEFAULT_SCRAPER_STRATEGIES = [
    RecipeScraperPackage,          # 优先级1
    RecipeScraperOpenAITranscription,  # 优先级2
    RecipeScraperOpenAI,           # 优先级3
    RecipeScraperOpenGraph,        # 优先级4
]
```

遍历逻辑：
1. 按顺序实例化每个策略，传入 `raw_html=html`（就是前置抓取到的 HTML，或用户传入的 HTML/JSON）
2. `can_scrape()` 返回 False 则跳过
3. 调用 `parse()`，第一个返回非空 `(Recipe, ScrapedExtras)` 的策略即终止链条
4. 对结果执行 `cleaner.clean()` 标准化

### 2.3 四个具体策略详解

#### 策略 1：RecipeScraperPackage（schema.org 结构化数据）

位置：[scraper_strategies.py#L178-L340](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L178-L340)

- **can_scrape()**：`bool(self.url or self.raw_html)` → 几乎总是 True
- **get_html()**：返回 `self.raw_html`（前置抓取的结果，或用户传入的 HTML/JSON），不再发网络请求
- **scrape_url()**：调用第三方库 `recipe-scrapers` 的 `scrape_html()` 解析 schema.org 数据
- **clean_scraper()**：双层兜底取值（先调库方法，失败则直接读原始 schema dict），然后构造 Recipe 对象
- **tags/categories 处理**：
  - 在构造 Recipe **之前**先存入 `ScrapedExtras`（见下文「标签分类双通道」）
  - 构造 Recipe 对象时**不传** `tags` 和 `recipe_category` 参数，取默认值空列表

#### 策略 2：RecipeScraperOpenAITranscription（视频转录）

位置：[scraper_strategies.py#L442-L610](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L442-L610)

- **can_scrape()**（[scraper_strategies.py#L445-L454](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L445-L454)）：检查三件事
  1. 有 `self.url`
  2. 群组启用了 `audio_provider_enabled`
  3. URL 被 yt-dlp 的任一 extractor 匹配（`any(ie.suitable(self.url) for ie in _get_yt_dlp_extractors())`）
- **get_html()**（[scraper_strategies.py#L521-L522](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L521-L522)）：直接返回空字符串——**完全不使用 HTML 内容**
- **parse() 执行流程**：
  1. yt-dlp 下载音频 MP3 + 字幕（VTT 格式，优先英/法/西/德/意）
  2. 有字幕则解析 VTT 文本，无字幕则调用 OpenAI `transcribe_audio()` 语音转文字
  3. 将标题 + 描述 + 转录文本发送给 AI（prompt: `recipes.parse-recipe-video`），按 `OpenAIRecipe` schema 返回结构化数据
  4. 手动构造 Recipe 对象，图片取视频缩略图 URL
  5. 返回空的 `ScrapedExtras()`（视频策略不提取标签分类）
- **重要澄清**：此策略**不依赖 HTML 内容**做解析，但在 **URL 入口场景下，调度器已经在策略遍历之前执行了 `safe_scrape_html(url)`**，只是这个策略忽略了那个 HTML 结果。

#### 策略 3：RecipeScraperOpenAI（AI 解析网页全文）

位置：[scraper_strategies.py#L342-L430](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L342-L430)

- **can_scrape()**：检查群组 `ai_enabled` 且父类条件满足
- **继承关系**：继承 `RecipeScraperPackage`，只重写 `get_html()`，复用 `clean_scraper()` 和 `parse()`
- **差异化的 get_html()**：
  1. 取 `self.raw_html`（前置已抓取）或再抓一次
  2. 用 BeautifulSoup 提取纯文本 + JSON-LD 数据 + 最大尺寸图片 URL
  3. 调用 AI（prompt: `recipes.scrape-recipe`）要求返回 JSON 格式的 schema.org Recipe
  4. 将 AI 返回的 JSON 用 `ld_json_to_html()` 包装成假 HTML
  5. 后续父类 `parse()` 流程与策略 1 完全一致（含 ScrapedExtras 标签分类处理）

#### 策略 4：RecipeScraperOpenGraph（OG 元数据保底）

位置：[scraper_strategies.py#L613-L672](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L613-L672)

- **can_scrape()**：`bool(self.url or self.raw_html)` → 几乎总是 True
- **get_recipe_fields()**（[scraper_strategies.py#L620-L652](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L620-L652)）：提取 Open Graph 属性，返回 dict：
  ```python
  {
      "name": ..., "description": ..., "image": ...,
      "categories": [],                       # 注意：key 是 "categories"，不是 "recipeCategory"
      "tags": og_fields(properties, "og:article:tag"),  # tags 有值
      ...
  }
  ```
- **parse()**：直接 `return Recipe(**og_data), ScrapedExtras()`
  - 返回空的 `ScrapedExtras()`，不走 ScrapedExtras 通道
  - 但 **tags 和 categories 直接传入 Recipe 构造器**

### 2.4 反爬处理：safe_scrape_html

位于 [scraper_strategies.py#L58-L134](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L58-L134)。

核心机制：
- **TLS 指纹模拟**：依次尝试 impersonate chrome/firefox/safari/edge 四种浏览器指纹（基于 httpx-curl-cffi），绕过 Cloudflare 等 JA3/JA4 指纹检测
- **超时保护**：总超时 15 秒，流式读取时也做二次超时检测，防止恶意大文件攻击
- **编码自适应**：优先使用响应头 charset，fallback 到 auto-detected encoding

---

## 三、字段清理层（cleaner.py）对分类的真实处理

### 3.1 前置知识：Recipe 模型的字段与别名

[RecipeSummary](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/recipe/recipe.py#L116-L144) 中定义：

```python
recipe_category: Annotated[list[RecipeCategory] | None, Field(validate_default=True)] = []
tags: Annotated[list[RecipeTag] | None, Field(validate_default=True)] = []
org_url: str | None = Field(None, alias="orgURL")
```

由于 `alias_generator=camelize`：
- `recipe_category` 的别名是自动生成的 **`recipeCategory`**（驼峰）
- `tags` 的别名就是 **`tags`**（已是单字）
- `org_url` 同时也显式指定了 alias="orgURL"

此外，Recipe 模型有两个 field_validator（[recipe.py#L258-L268](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/recipe/recipe.py#L258-L268)）：

```python
@field_validator("tags", mode="before")
def validate_tags(cats: list[Any]):
    if isinstance(cats, list) and cats and isinstance(cats[0], str):
        return [RecipeTag(id=uuid4(), name=c, slug=slugify(c)) for c in cats]
    return cats

@field_validator("recipe_category", mode="before")
def validate_categories(cats: list[Any]):
    if isinstance(cats, list) and cats and isinstance(cats[0], str):
        return [RecipeCategory(id=uuid4(), name=c, slug=slugify(c)) for c in cats]
    return cats
```

**当传入 `list[str]` 时，自动构造临时 `RecipeTag`/`RecipeCategory` 对象**（生成临时 uuid，没有 group_id）。

### 3.2 cleaner.clean() 的分类字段处理

[cleaner.py#L39-L75](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L39-L75) 总入口：

```python
def clean(recipe_data: Recipe | dict, translator: Translator, url=None) -> Recipe:
    if not isinstance(recipe_data, dict):
        recipe_data_dict = recipe_data.model_dump(by_alias=True)   # 用别名导出 dict
        recipe_data_dict["recipeIngredient"] = [ing.display for ing in recipe_data.recipe_ingredient]
        recipe_data = recipe_data_dict

    recipe_data["slug"] = slugify(recipe_data.get("name", ""))
    recipe_data["description"] = clean_string(recipe_data.get("description", ""))
    recipe_data["prepTime"] = clean_time(recipe_data.get("prepTime"), translator)
    recipe_data["performTime"] = clean_time(recipe_data.get("performTime"), translator)
    recipe_data["totalTime"] = clean_time(recipe_data.get("totalTime"), translator)
    recipe_data["recipeServings"], recipe_data["recipeYieldQuantity"], recipe_data["recipeYield"] = clean_yield(...)
    recipe_data["recipeCategory"] = clean_categories(recipe_data.get("recipeCategory", []))  # ← 用别名！
    recipe_data["recipeIngredient"] = clean_ingredients(recipe_data.get("recipeIngredient", []))
    recipe_data["recipeInstructions"] = clean_instructions(recipe_data.get("recipeInstructions", []))
    recipe_data["image"] = clean_image(recipe_data.get("image"))[0]
    recipe_data["orgURL"] = url or recipe_data.get("orgURL")
    recipe_data["notes"] = clean_notes(recipe_data.get("notes"))
    recipe_data["rating"] = clean_int(recipe_data.get("rating"))

    return Recipe(**recipe_data)   # ← 关键字参数可用别名（populate_by_name=True）
```

**关键修正：cleaner.clean() 明确处理了 recipeCategory，而且处理方式非常精巧：**

1. `model_dump(by_alias=True)`：将 Recipe 对象转为 dict 时，**所有 key 用驼峰别名**，如 `recipe_category` → key 变为 `"recipeCategory"`
2. `recipe_data.get("recipeCategory", [])`：读取时也用别名 key
3. `clean_categories()` 返回 `list[str]`（清理后的分类名）
4. `recipe_data["recipeCategory"] = ...`：写回时也用别名 key
5. `Recipe(**recipe_data)`：因为 `populate_by_name=True`，`recipeCategory` 关键字参数能正确匹配到 `recipe_category` 字段
6. field_validator `validate_categories` 自动将 `list[str]` 转为 `list[RecipeCategory]`（临时 uuid 对象）

**标签 tags 的处理类似**：cleaner.clean() 总入口中没有专门的 `recipe_data["tags"] = ...` 行，是因为 `model_dump(by_alias=True)` 已经把 tags 作为 key `"tags"` 带入了 dict，而 `Recipe(**recipe_data)` 构造时 field_validator `validate_tags` 会自动处理。

### 3.3 clean_categories() 与 clean_tags() 的实现

[cleaner.py#L516-L559](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L516-L559)

两个函数签名和行为一致：输入 `str | list` → 输出 `list[str]`（每个元素 `.title()` 首字母大写）。

```python
def clean_categories(category: str | list) -> list[str]:
    match category:
        case str(category):      # "Dinner, Vegan" → ["Dinner", "Vegan"]
            return [cat.strip().title() for cat in category.split(",") if cat.strip()]
        case [str(), *_]:        # ["dinner", "vegan"] → ["Dinner", "Vegan"]
            return [cat.strip().title() for cat in category if cat.strip()]
        case [{"name": str(), "slug": str()}, *_]:   # 迁移专用格式
            return [cat["name"] for cat in category if "name" in cat]
        case int() | float():
            return []
        case _:
            raise TypeError(...)

def clean_tags(data: str | list[str]) -> list[str]:
    match data:
        case [str(), *_]:
            return [tag.strip().title() for tag in data if tag.strip()]
        case str(data):
            return clean_tags(data.split(","))
        case _:
            return []
```

---

## 四、标签与分类的双通道汇合机制

标签（tags）和分类（recipe_category）存在**两条完全独立的数据流**，它们在 `_finish_recipe_from_web()` 中才最终汇合。

### 4.1 通道 A：Recipe 对象内置通道

数据跟随 Recipe 对象本身流转，经过 cleaner.clean() 统一清理。

**各策略在通道 A 中的产出：**

| 策略 | Recipe.tags | Recipe.recipe_category | 说明 |
|------|-------------|----------------------|------|
| RecipeScraperPackage | `[]`（空列表） | `[]`（空列表） | 构造 Recipe 时不传这两个参数，取默认值 |
| RecipeScraperOpenAI | `[]` | `[]` | 复用 Package 的 clean_scraper() |
| RecipeScraperOpenAITranscription | `[]` | `[]` | 手动构造 Recipe 时不传 |
| RecipeScraperOpenGraph | `list[str]`（og:article:tag） | `[]`（空列表） | og_data 里的 key 是 `"tags"`（匹配字段名），但 key `"categories"` **不匹配** `recipeCategory` 别名，被 Pydantic 忽略 |

**通道 A 在 cleaner.clean() 中的处理：**
- `model_dump(by_alias=True)` 导出 → `clean_categories()` 清理字符串 → `Recipe(**recipe_data)` 重新构造
- field_validator 自动将 `list[str]` 转为 `list[RecipeTag/RecipeCategory]`（临时 uuid 对象，无 group_id）

### 4.2 通道 B：ScrapedExtras 独立通道

数据存储在独立的 `ScrapedExtras` 对象中，**不跟随 Recipe 对象流转**，也**不经过 cleaner.clean() 总入口**。

**接管时机：策略层构造 Recipe 之前**

以 RecipeScraperPackage.clean_scraper() 为例（[scraper_strategies.py#L264-L268](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L264-L268)）：

```python
extras = ScrapedExtras()

extras.set_tags(
    try_get_default(
        scraped_data.keywords,   # 先调用 recipe-scrapers 库的 keywords()
        "keywords",              # 失败则从 schema dict 读取 keywords 字段
        "",                      # 默认值
        cleaner.clean_tags       # ← 字符串清理在这里就做了！不是在 cleaner.clean() 总入口
    )
)

extras.set_categories(
    try_get_default(
        scraped_data.category,   # 先调用 recipe-scrapers 库的 category()
        "recipeCategory",        # 失败则从 schema dict 读取 recipeCategory 字段
        "",                      # 默认值
        cleaner.clean_categories # ← 字符串清理在这里就做了！
    )
)
```

**关键修正：标签分类的字符串清理（clean_tags/clean_categories）在策略层存入 ScrapedExtras 时就已执行**，输出为 `list[str]`。之后 `ScrapedExtras._tags` 和 `ScrapedExtras._categories` 存储的就是清理好的字符串列表。

**各策略在通道 B 中的产出：**

| 策略 | extras._tags | extras._categories | 说明 |
|------|-------------|------------------|------|
| RecipeScraperPackage | `list[str]`（schema keywords） | `list[str]`（schema recipeCategory） | 有值 |
| RecipeScraperOpenAI | `list[str]` | `list[str]` | 复用 Package，有值 |
| RecipeScraperOpenAITranscription | `[]` | `[]` | 返回空 ScrapedExtras() |
| RecipeScraperOpenGraph | `[]` | `[]` | 返回空 ScrapedExtras() |

### 4.3 汇合点：_finish_recipe_from_web() —— 无条件覆盖

位于 [recipe_crud_routes.py#L250-L274](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L250-L274)：

```python
def _finish_recipe_from_web(self, req, recipe, extras):
    if req.include_tags:                        # 请求参数开关，默认 False
        ctx = ScraperContext(self.repos)
        recipe.tags = extras.use_tags(ctx)      # ← 无条件覆盖！不管 extras 是不是空

    if req.include_categories:                  # 请求参数开关，默认 False
        ctx = ScraperContext(self.repos)
        recipe.recipe_category = extras.use_categories(ctx)  # ← 无条件覆盖！不管 extras 是不是空

    new_recipe = self.service.create_one(recipe)
    ...
```

**关键代码事实**：`if` 判断的是**请求开关**，不是 `extras` 是否有数据。只要开关为 True，就一定执行赋值覆盖——哪怕 `extras.use_tags()` 返回空列表。

而 `ScrapedExtras.use_tags()` / `use_categories()` 的实现（[scraped_extras.py#L30-L32](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraped_extras.py#L30-L32) 和 [L57-L59](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraped_extras.py#L57-L59)）：

```python
def use_tags(self, ctx: ScraperContext) -> list[TagOut]:
    if not self._tags:
        return []   # ← 空列表直接返回 []，不会抛出异常
    ...
```

### 4.4 来源为空时的数据链路推演

先梳理 Package/OpenAI 策略在 schema 缺少 keywords 或 recipeCategory 时的完整数据链路：

**try_get_default 到 clean_tags/clean_categories 的空值传递链**（[scraper_strategies.py#L194-L218](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L194-L218) 与 [cleaner.py#L516-L559](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L516-L559)）：

```
try_get_default(scraped_data.keywords, "keywords", "", cleaner.clean_tags)
        │
        ▼
 ① value = default = ""              ← 初始值是空字符串
        │
        ▼
 ② scraped_data.keywords()           ← 先调 recipe-scrapers 库方法
    ├─ 有值 → value = 返回值
    └─ 抛异常 / 返回空 → value 保持 ""
        │
        ▼
 ③ value == default? ("" == "") → True
    └─ scraped_data.schema.data.get("keywords")  ← 再从原始 schema 读
        ├─ 有值 → value = 返回值
        └─ 缺失 / None / "" → value 保持 ""
        │
        ▼
 ④ cleaner.clean_tags(value) = cleaner.clean_tags("")
    └─ if not data: return []        ← [cleaner.py#L548-L549] 空字符串直接返回空列表
        │
        ▼
 ⑤ extras.set_tags([])               ← 通道 B 的 _tags 是空列表，不是 None，不是空字符串
```

**clean_categories("")** 路径完全相同（[cleaner.py#L517-L518](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L517-L518)）：
```python
def clean_categories(category: str | list) -> list[str]:
    if not category:   # "" 为 falsy，直接进入
        return []      # 返回空列表
```

**进入 _finish_recipe_from_web 后的覆盖结果**：

```python
if req.include_tags:
    recipe.tags = extras.use_tags(ctx)  # extras._tags = []
                                        # use_tags() 内 if not self._tags: return []
                                        # → 覆盖后 recipe.tags = []
else:
    # 保留通道 A：recipe.tags = []（构造 Recipe 时未传，取默认值）
```

**结论：当 schema 缺少 keywords/recipeCategory 时，include_tags 和 include_categories 的 True/False 结果完全相同——都是空列表。** 开关失去区分作用。

### 4.5 3 种策略 × 来源有无 × 2 个开关 = 完整结果矩阵

#### 策略 1：RecipeScraperPackage / RecipeScraperOpenAI — 分两子情况

**子情况 A：schema 有 keywords/recipeCategory**

| 双通道产出 | 通道 A：Recipe 对象 | 通道 B：ScrapedExtras |
|-----------|-------------------|----------------------|
| tags | `[]`（空列表） | `["Dinner", "Vegan"]`（有值） |
| recipe_category | `[]`（空列表） | `["Main Course"]`（有值） |

| 开关组合 | 最终 tags | 最终 recipe_category | 说明 |
|---------|----------|---------------------|------|
| include_tags=False | `[]`（保留 A） | - | 通道 A 空，无标签 |
| include_tags=True | `[TagOut]`（B 覆盖 A） | - | 从 schema keywords 查库/创建，**有值** ✅ |
| include_categories=False | - | `[]`（保留 A） | 通道 A 空，无分类 |
| include_categories=True | - | `[TagOut]`（B 覆盖 A） | 从 schema recipeCategory 查库/创建，**有值** ✅ |

**子情况 B：schema 无 keywords/recipeCategory（缺失）**

| 双通道产出 | 通道 A：Recipe 对象 | 通道 B：ScrapedExtras |
|-----------|-------------------|----------------------|
| tags | `[]`（空列表） | `[]`（"" → clean_tags → 空列表） |
| recipe_category | `[]`（空列表） | `[]`（"" → clean_categories → 空列表） |

| 开关组合 | 最终 tags | 最终 recipe_category | 说明 |
|---------|----------|---------------------|------|
| include_tags=False | `[]`（保留 A） | - | 空列表 |
| include_tags=True | `[]`（B 覆盖 A） | - | 空列表覆盖空列表，**无变化** |
| include_categories=False | - | `[]`（保留 A） | 空列表 |
| include_categories=True | - | `[]`（B 覆盖 A） | 空列表覆盖空列表，**无变化** |

**子情况 B 下，开关 True/False 结果完全相同，都为空。**

#### 策略 2：RecipeScraperOpenAITranscription（视频转录）

| 双通道产出 | 通道 A：Recipe 对象 | 通道 B：ScrapedExtras（空对象） |
|-----------|-------------------|-------------------------------|
| tags | `[]`（空列表） | `[]`（构造 ScrapedExtras 时默认空） |
| recipe_category | `[]`（空列表） | `[]`（构造 ScrapedExtras 时默认空） |

| 开关组合 | 最终 tags | 最终 recipe_category | 说明 |
|---------|----------|---------------------|------|
| include_tags=False | `[]`（保留 A） | - | 空列表，结果与开关 True 相同 |
| include_tags=True | `[]`（B 覆盖 A） | - | 空 ScrapedExtras 返回 `[]`，覆盖后仍为空 |
| include_categories=False | - | `[]`（保留 A） | 空列表，结果与开关 True 相同 |
| include_categories=True | - | `[]`（B 覆盖 A） | 空 ScrapedExtras 返回 `[]`，覆盖后仍为空 |

**视频策略下，两个开关 True/False 结果完全相同**——因为通道 A 和通道 B 都是空。

#### 策略 3：RecipeScraperOpenGraph（OG 元数据保底）

| 双通道产出 | 通道 A：Recipe 对象 | 通道 B：ScrapedExtras（空对象） |
|-----------|-------------------|-------------------------------|
| tags | `[RecipeTag("Easy"), RecipeTag("Quick")]`（og:article:tag，**有值**） | `[]`（构造 ScrapedExtras 时默认空） |
| recipe_category | `[]`（og_data["categories"] key 不匹配 alias，被忽略） | `[]`（构造 ScrapedExtras 时默认空） |

| 开关组合 | 最终 tags | 最终 recipe_category | 说明 |
|---------|----------|---------------------|------|
| include_tags=False | `[RecipeTag]`（保留 A） | - | 保留 OpenGraph 提取的标签，**有值** ✅ |
| include_tags=True | `[]`（B 覆盖 A）⚠️ | - | **空 ScrapedExtras 覆盖了通道 A，原本有值的标签被丢弃为空** |
| include_categories=False | - | `[]`（保留 A） | 空列表，结果与开关 True 相同 |
| include_categories=True | - | `[]`（B 覆盖 A） | 空 ScrapedExtras 返回 `[]`，覆盖后仍为空 |

**⚠️ OpenGraph 策略的隐藏陷阱：include_tags=True 会把原本从 og:article:tag 提取到的标签清空为 `[]`。** 因为 OpenGraph 返回的是空 `ScrapedExtras()`，而开关 True 触发无条件覆盖。

### 4.6 覆盖行为汇总表（含来源缺失子情况）

| 策略 | 数据源 | include_tags=False | include_tags=True | include_categories=False | include_categories=True |
|------|--------|--------------------|--------------------|--------------------------|--------------------------|
| Package / OpenAI | schema 有值 | `[]`（空） | `[TagOut]`（有值）✅ | `[]`（空） | `[TagOut]`（有值）✅ |
| Package / OpenAI | schema 缺失 | `[]`（空） | `[]`（空，无变化） | `[]`（空） | `[]`（空，无变化） |
| 视频转录 | （无标签分类） | `[]`（空） | `[]`（空，无变化） | `[]`（空） | `[]`（空，无变化） |
| OpenGraph | （tags 来自 og，其他空） | `[RecipeTag]`（有值）✅ | `[]`（空，覆盖丢值）⚠️ | `[]`（空） | `[]`（空，无变化） |

### 4.7 开关的有效作用域总结

- **include_tags 真正产生差异的唯一情形**：Package/OpenAI 策略 + schema keywords 有值
- **include_categories 真正产生差异的唯一情形**：Package/OpenAI 策略 + schema recipeCategory 有值
- **其余所有组合**：开关 True/False 结果相同，要么都空，要么（OpenGraph tags 场景）开关 True 反而丢值

### 4.8 ScrapedExtras.use_tags()/use_categories() 的数据库操作

[scraped_extras.py#L30-L82](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraped_extras.py#L30-L82)

```
对每个标签/分类名（list[str]）：
  1. slugify 后去重（seen_tag_slugs 集合）
  2. repo.get_one(slugify_tag, "slug") 查数据库是否已存在
     ├─ 存在 → 直接用返回的 TagOut（带 id、group_id）
     └─ 不存在 → TagSave/CategorySave(name=tag, group_id=...) → repo.create() → 返回新建的 TagOut
  3. 汇总为 list[TagOut] 返回
```

**此时**：返回的 tags 和 recipe_category 已经是完整的数据库对象列表（带 id、slug、group_id），被赋值到 recipe 对象上，**覆盖**通道 A 的临时 uuid 对象。

### 4.9 最终落库：RecipeService.create_one()

位于 [recipe_service.py#L202-L245](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L202-L245)

1. **_recipe_creation_factory()**（[recipe_service.py#L163-L187](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L163-L187)）：
   ```python
   if additional_attrs.get("tags"):
       for i in range(len(additional_attrs.get("tags", []))):
           additional_attrs["tags"][i]["group_id"] = self.user.group_id
   ```
   给每个 tag dict 补上 group_id。**注意：这里处理的是通道 A 的临时对象（没有 group_id）；通道 B 的对象在 ScrapedExtras.create() 时已经带了 group_id。**

2. **self.repos.recipes.create(data)**：SQLAlchemy ORM 执行 INSERT。Recipe 模型上 tags 和 recipe_category 定义为关系字段，ORM 自动处理关联表级联写入。

### 4.10 注意：_process_recipe_data 不在抓取落库链路上

`RecipeService` 中还有 `_transform_category_or_tag()` 和 `_process_recipe_data()` 方法（[recipe_service.py#L255-L297](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L255-L297)），但它们是给 `create_from_zip()`（zip 导入）等其他入口使用的。**URL/HTML/JSON 抓取落库不走这条路径**。

### 4.11 批量导入场景的第三来源

批量 URL 导入（[recipe_bulk_scraper.py#L104-L108](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_bulk_scraper.py#L104-L108)）还有第三个来源——用户在请求中直接指定：

```python
if b.tags:
    recipe.tags = b.tags         # 直接覆盖
if b.categories:
    recipe.recipe_category = b.categories  # 直接覆盖
```

批量场景不走 `_finish_recipe_from_web()`，也不使用 ScrapedExtras，直接用请求参数覆盖通道 A 的结果。

---

## 五、完整链路时序图（以 URL 入口 + RecipeScraperPackage + include_categories=True 为例）

```
用户 POST /recipes/create/url {url: "...", includeCategories: true}
  │
  ▼
[recipe_crud_routes.py] parse_recipe_url()
  │
  └─► _create_recipe_from_web(req)
        │
        ├─ html=None, url=req.url
        │
        └─► [scraper.py] create_from_html(url, repos, translator, html=None)
              │
              ├─► html is None → 执行 URL 正则校验
              │
              └─► [recipe_scraper.py] RecipeScraper.scrape(url, html=None)
                    │
                    ├─► html is None → safe_scrape_html(url)
                    │
                    ├─► 遍历策略，命中 RecipeScraperPackage
                    │     └─► parse()
                    │           ├─► clean_scraper()
                    │           │     ├─► extras = ScrapedExtras()
                    │           │     ├─► extras.set_tags(clean_tags(keywords))
                    │           │     │     └─ 通道 B: _tags = ["Dinner", "Vegan"]
                    │           │     ├─► extras.set_categories(clean_categories(recipeCategory))
                    │           │     │     └─ 通道 B: _categories = ["Main Course"]
                    │           │     └─► Recipe(name=..., ...)
                    │           │           └─ 通道 A: tags=[], recipe_category=[] (默认值)
                    │           └─► return (recipe, extras)
                    │
                    └─► cleaner.clean(recipe_result, translator)
                          ├─► model_dump(by_alias=True) → {"recipeCategory": [], "tags": [], ...}
                          ├─► recipe_data["recipeCategory"] = clean_categories([]) → []
                          └─► Recipe(**recipe_data)
                                └─ 通道 A: tags=[], recipe_category=[] (field_validator 生效但空)
              │
              ├─► 下载图片、生成 id/slug
              └─► return (recipe, extras)
        │
        └─► _finish_recipe_from_web(req, recipe, extras)
              │
              ├─► req.include_tags == False → 保留通道 A: recipe.tags = []
              │
              ├─► req.include_categories == True
              │     └─► extras.use_categories(ScraperContext(repos))
              │           ├─► "Main Course" → slugify → "main-course"
              │           ├─► repo.categories.get_one("main-course", "slug")
              │           │     ├─ 存在 → 返回 TagOut
              │           │     └─ 不存在 → CategorySave → create() → 返回 TagOut
              │           └─► return [TagOut(id=..., name="Main Course", slug="main-course", group_id=...)]
              │
              ├─► recipe.recipe_category = 上述列表（通道 B 覆盖通道 A）
              │
              └─► [recipe_service.py] RecipeService.create_one(recipe)
                    ├─► _recipe_creation_factory() → 给 tag dict 补 group_id
                    ├─► repos.recipes.create(data) → ORM INSERT Recipe + RecipeCategory 关联表
                    ├─► 创建 timeline 事件
                    └─► 返回 new_recipe
  │
  └─► publish_event(recipe_created)

返回 new_recipe.slug
```

---

## 六、关键设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|---------|------|
| **策略模式** | `ABCScraperStrategy` + 4 个实现类 | 封装不同来源的解析算法 |
| **责任链模式** | `RecipeScraper.scrape()` 遍历策略列表 | 按优先级降级尝试，首个成功即终止 |
| **模板方法模式** | `RecipeScraperOpenAI` 继承 `RecipeScraperPackage`，只重写 `get_html()` | 复用解析流程，只差异化 HTML 获取方式 |
| **多态匹配** | `cleaner.py` 中大量 `match` 语句 | 统一处理异构输入格式 |
| **别名自动生成** | `MealieModel.config alias_generator=camelize` | 让 snake_case 字段天然兼容 schema.org 的 camelCase 命名 |
| **双通道数据流** | Recipe 对象通道 A + ScrapedExtras 独立通道 B | ① 不同策略可选择不同通道传递标签分类；② 由请求参数决定最终使用哪一通道数据；③ 通道 B 延迟到落库前做 get_or_create 避免重复写入 |
