# Mealie Recipe Scraper Provider 链路分析

## 整体架构概览

Mealie 的食谱抓取系统采用 **策略模式 + 责任链** 的组合设计，通过多层抽象屏蔽不同来源的差异，最终将异构数据统一落库为标准 Recipe 对象。

```
用户请求 → API路由层(URL预处理分支) → 服务入口层(调度器前置网页抓取)
    → 策略执行层(责任链) → 字段清理层 → 标签分类独立处理 → 数据落库层
```

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

这是之前容易被忽略的重要事实——即使是视频转录策略，在 URL 入口下也已经先做了一次网页抓取。

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
- **tags/categories 处理**：不写入 Recipe 对象，而是存入 `ScrapedExtras.set_tags()` / `set_categories()`，延迟到落库前再处理

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
- **重要澄清**：此策略**不依赖 HTML 内容**做解析，但在 **URL 入口场景下，调度器已经在策略遍历之前执行了 `safe_scrape_html(url)`**（即已经发过一次网页 GET 请求），只是这个策略忽略了那个 HTML 结果。HTML/JSON 入口则不会有这次额外请求。

#### 策略 3：RecipeScraperOpenAI（AI 解析网页全文）

位置：[scraper_strategies.py#L342-L430](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L342-L430)

- **can_scrape()**：检查群组 `ai_enabled` 且父类条件满足
- **继承关系**：继承 `RecipeScraperPackage`，只重写 `get_html()`，复用 `clean_scraper()` 和 `parse()`
- **差异化的 get_html()**：
  1. 取 `self.raw_html`（前置已抓取）或再抓一次
  2. 用 BeautifulSoup 提取纯文本 + JSON-LD 数据 + 最大尺寸图片 URL
  3. 调用 AI（prompt: `recipes.scrape-recipe`）要求返回 JSON 格式的 schema.org Recipe
  4. 将 AI 返回的 JSON 用 `ld_json_to_html()` 包装成假 HTML
  5. 后续父类 `parse()` 流程与策略 1 完全一致

#### 策略 4：RecipeScraperOpenGraph（OG 元数据保底）

位置：[scraper_strategies.py#L613-L672](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L613-L672)

- **can_scrape()**：`bool(self.url or self.raw_html)` → 几乎总是 True
- **get_html()**：返回 `self.raw_html` 或抓取
- **get_recipe_fields()**：用 `extruct` 库提取 Open Graph 属性，填充最基本字段（name、description、image、tags），ingredients 和 instructions 写死为占位文本

### 2.4 反爬处理：safe_scrape_html

位于 [scraper_strategies.py#L58-L134](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L58-L134)。

核心机制：
- **TLS 指纹模拟**：依次尝试 impersonate chrome/firefox/safari/edge 四种浏览器指纹（基于 httpx-curl-cffi），绕过 Cloudflare 等 JA3/JA4 指纹检测
- **超时保护**：总超时 15 秒，流式读取时也做二次超时检测，防止恶意大文件攻击
- **编码自适应**：优先使用响应头 charset，fallback 到 auto-detected encoding

---

## 三、字段清理层（cleaner.py）

位于 [cleaner.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py)。

### 3.1 总入口：clean()

[cleaner.py#L39-L75](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L39-L75)

无论哪个策略返回的 Recipe，都要经过统一清理：

```python
def clean(recipe_data: Recipe | dict, translator: Translator, url=None) -> Recipe:
    # 转为 dict → 各字段 clean_* → 重新构造 Recipe
```

**重要**：`clean()` **不处理 tags 和 recipe_category**——这两个字段在策略层被存到了独立的 `ScrapedExtras` 对象中，不在 Recipe 对象上流转，延迟到落库前的 `_finish_recipe_from_web()` 中才处理。

### 3.2 各字段清理函数的多态处理

每个清理函数都使用 Python `match` 语句处理多种可能的输入格式：

| 清理函数 | 支持的输入格式 |
|----------|---------------|
| `clean_string()` | str / list / int / float / None |
| `clean_image()` | str / list[str] / list[dict{url}] / dict{url} / list[dict{@id}] |
| `clean_instructions()` | list[dict{text}] / dict（数字索引）/ str（含换行或 JSON）/ list[str] / list[HowToSection] |
| `clean_ingredients()` | list[str] / list[dict] / str / None |
| `clean_time()` | ISO 8601 字符串(PT1H30M) / timedelta / int(分钟) / dict{minValue} / list[str] / datetime |
| `clean_yield()` | str（如 "4 servings"）/ list[str] |
| `clean_categories()` | str（逗号分隔）/ list[str] / list[dict{name,slug}] |
| `clean_nutrition()` | dict（提取数字，钠/胆固醇自动处理克→毫克转换） |

**关键点**：`clean_instructions()` 会递归调用自身处理嵌套结构（如 HowToSection → HowToStep），并循环调用 `clean_string()` 直到稳定（处理多级 HTML 转义）。

---

## 四、标签与分类到食谱落库的完整链路

标签（tags）和分类（recipe_category）是唯一**不经过 cleaner.clean()** 的字段，它们走一条独立链路，涉及三个阶段：

### 阶段 1：策略层 → 暂存到 ScrapedExtras

以 `RecipeScraperPackage.clean_scraper()` 为例（[scraper_strategies.py#L264-L268](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L264-L268)）：

```python
extras = ScrapedExtras()
extras.set_tags(try_get_default(scraped_data.keywords, "keywords", "", cleaner.clean_tags))
extras.set_categories(try_get_default(scraped_data.category, "recipeCategory", "", cleaner.clean_categories))
```

- 从 schema.org 数据提取 keywords 和 recipeCategory 原始字符串
- 分别经过 `cleaner.clean_tags()` 和 `cleaner.clean_categories()` 转为 `list[str]`
- 存入 `ScrapedExtras._tags` 和 `ScrapedExtras._categories` 私有属性
- **此时：未写数据库，也未赋值给 Recipe 对象**，Recipe 对象上的 tags 和 recipe_category 字段是空的

### 阶段 2：路由层 → 按需查库或创建，并赋值到 Recipe

在 `_finish_recipe_from_web()`（[recipe_crud_routes.py#L250-L274](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L250-L274)）：

```python
def _finish_recipe_from_web(self, req, recipe, extras):
    if req.include_tags:                        # 请求参数开关，默认 False
        ctx = ScraperContext(self.repos)
        recipe.tags = extras.use_tags(ctx)      # type: ignore

    if req.include_categories:                  # 请求参数开关，默认 False
        ctx = ScraperContext(self.repos)
        recipe.recipe_category = extras.use_categories(ctx)  # type: ignore

    new_recipe = self.service.create_one(recipe)
    ...
```

`ScrapedExtras.use_tags()` 的逻辑（[scraped_extras.py#L30-L55](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraped_extras.py#L30-L55)）：

```
对每个标签名：
  1. slugify 后去重（seen_tag_slugs 集合）
  2. repo.get_one(slugify_tag, "slug") 查数据库是否已存在
     ├─ 存在 → 直接用返回的 TagOut
     └─ 不存在 → TagSave(name=tag, group_id=...) → repo.create() → 返回新建的 TagOut
  3. 汇总为 list[TagOut] 返回
```

`use_categories()` 逻辑完全相同，只是操作 `self.repos.categories` 表。

**此时**：tags 和 recipe_category 已经是完整的数据库对象列表（带 id、slug、group_id），被赋值到 recipe 对象上。

### 阶段 3：RecipeService.create_one() → ORM 级联写入

在 `RecipeService.create_one()`（[recipe_service.py#L202-L245](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L202-L245)）：

1. **_recipe_creation_factory()**（[recipe_service.py#L163-L187](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L163-L187)）：
   ```python
   if additional_attrs.get("tags"):
       for i in range(len(additional_attrs.get("tags", []))):
           additional_attrs["tags"][i]["group_id"] = self.user.group_id
   ```
   给每个 tag 补上 group_id，确保归属正确的群组。categories 则不需要这一步（因为 `CategorySave` 创建时已经带了 group_id）。

2. **self.repos.recipes.create(data)**：SQLAlchemy ORM 执行 INSERT。由于 Recipe 模型上 tags 和 recipe_category 定义为关系字段（`list[RecipeTag] | None` 和 `list[RecipeCategory] | None`，见 [recipe.py#L105](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/recipe/recipe.py#L105) 和 [recipe.py#L137-L138](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/schema/recipe/recipe.py#L137-L138)），ORM 会自动处理关联表的级联写入。

3. 之后写入用户评分、创建时间线事件等附属数据。

### 注意：_process_recipe_data 不在抓取落库链路上

`RecipeService` 中还有 `_transform_category_or_tag()` 和 `_process_recipe_data()` 方法（[recipe_service.py#L255-L297](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/recipe/recipe_service.py#L255-L297)），但它们是给 `create_from_zip()`（zip 导入）等其他入口使用的。**URL/HTML/JSON 抓取落库不走这条路径**——标签分类的查库或创建逻辑在 ScrapedExtras 中已经完成了。

---

## 五、完整链路时序图（以 URL 入口 + 命中视频策略为例）

```
用户 POST /recipes/create/url {url: "https://youtube.com/...", includeTags: true}
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
                    │     └─► 【关键】即使是视频 URL，这里也先抓取了一次网页！
                    │         （TLS 指纹模拟 → 超时保护 → 编码自适应）
                    │
                    ├─► 遍历策略（raw_html=刚才抓到的网页内容）
                    │     ├─► RecipeScraperPackage.can_scrape() → True
                    │     │     parse() → recipe-scrapers 库解析失败 → 返回 None
                    │     │
                    │     ├─► RecipeScraperOpenAITranscription.can_scrape()
                    │     │     ├─► 检查 audio_provider_enabled ✓
                    │     │     └─► yt-dlp extractor.suitable(url) ✓
                    │     │
                    │     └─► parse()
                    │           ├─► get_html() → 返回空字符串（完全忽略前置抓取的 HTML）
                    │           ├─► yt-dlp 下载音频+字幕
                    │           ├─► 解析字幕 / 调用 AI 转写
                    │           ├─► AI 结构化解析 → OpenAIRecipe
                    │           └─► 构造 Recipe(name, ingredients, instructions, ...)
                    │                 构造 ScrapedExtras（tags=[], categories=[]）← 视频策略不提取标签
                    │
                    └─► cleaner.clean(recipe_result, translator)
                          └─► 字段标准化（注意：tags/categories 不在 Recipe 上，不经过这里）
              │
              ├─► RecipeDataService.scrape_image(thumbnail_url) → 下载视频封面
              ├─► 生成 uuid4() id
              ├─► slugify(name) → slug
              └─► return (new_recipe, extras)
        │
        └─► _finish_recipe_from_web(req, recipe, extras)
              │
              ├─► req.include_tags == True
              │     └─► extras.use_tags(ScraperContext(repos))
              │           └─► 视频策略 tags 为空列表，不操作数据库
              │
              ├─► recipe.recipe_category = ...（同样为空）
              │
              └─► [recipe_service.py] RecipeService.create_one(recipe)
                    ├─► _recipe_creation_factory() → 补 group_id 等默认值
                    ├─► repos.recipes.create(data) → ORM INSERT Recipe + 关联表
                    ├─► 创建 timeline 事件
                    └─► 返回 new_recipe
  │
  └─► publish_event(recipe_created) → 通知系统

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
| **数据传输对象（延迟处理）** | `ScrapedExtras` 暂存标签/分类字符串，与 Recipe 主体分离 | ① 标签分类不经过 cleaner 清理；② 由请求参数 `include_tags`/`include_categories` 决定是否真正落库；③ 落库前做 get_or_create 避免重复 |
