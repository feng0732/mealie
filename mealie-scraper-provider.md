# Mealie Recipe Scraper Provider 链路分析

## 整体架构概览

Mealie 的食谱抓取系统采用 **策略模式 + 责任链** 的组合设计，通过多层抽象屏蔽不同来源的差异，最终将异构数据统一落库为标准 Recipe 对象。

```
用户请求 → API路由层 → 服务入口层 → 策略执行层(责任链) → 字段清理层 → 数据落库层
```

---

## 一、抓取入口（API 层 → 服务入口层）

### 1.1 API 路由入口

所有抓取入口集中在 [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py)，提供 4 种抓取方式：

| 路由 | 方法 | 说明 |
|------|------|------|
| `/recipes/create/url` | POST | 根据 URL 抓取食谱（阻塞返回） |
| `/recipes/create/url/stream` | POST | 根据 URL 抓取食谱（SSE 流式返回进度） |
| `/recipes/create/html-or-json` | POST | 直接传入 HTML 或 schema.org JSON 解析 |
| `/recipes/create/url/bulk` | POST | 批量 URL 异步后台抓取 |

核心处理方法是私有方法 `_create_recipe_from_web()`（[recipe_crud_routes.py#L196-L248](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L196-L248)），它通过 SSE（Server-Sent Events）机制向客户端推送抓取进度。

### 1.2 服务统一入口

所有 API 路由最终调用服务层统一入口函数 `create_from_html()`，位于 [scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper.py#L26-L85)。

```python
async def create_from_html(
    url: str,
    repos: AllRepositories,
    translator: Translator,
    html: str | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Recipe, ScrapedExtras | None]:
```

**职责：**
1. URL 格式校验与提取（正则匹配 `http(s)://` 或 `www.`）
2. 实例化 `RecipeScraper` 调度器
3. 调用 `scraper.scrape()` 获取原始 Recipe 对象
4. 图片下载与处理（`RecipeDataService.scrape_image()`）
5. 生成 UUID、slug 等标识字段
6. 兜底处理（名称为空时设为 "Untitled" 等）

---

## 二、来源差异处理（策略执行层）

### 2.1 调度器：RecipeScraper

位于 [recipe_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_scraper.py#L27-L90)，是责任链模式的核心调度器。

**默认策略优先级顺序（`DEFAULT_SCRAPER_STRATEGIES`）：**

```python
DEFAULT_SCRAPER_STRATEGIES = [
    RecipeScraperPackage,          # 优先级1: 基于 recipe-scrapers 库解析 schema.org
    RecipeScraperOpenAITranscription,  # 优先级2: 视频转录 + AI 解析（需启用）
    RecipeScraperOpenAI,           # 优先级3: 网页全文 + AI 解析（需启用）
    RecipeScraperOpenGraph,        # 优先级4: Open Graph 元数据（保底方案）
]
```

**执行逻辑（[recipe_scraper.py#L46-L90](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_scraper.py#L46-L90)）：**

1. 如未提供 HTML，先用 `safe_scrape_html()` 抓取网页内容（含浏览器 TLS 指纹模拟绕过反爬）
2. **按顺序遍历**策略列表，依次调用：
   - `scraper.can_scrape()`：判断该策略是否适用（如 AI 策略需检查配置是否启用）
   - `scraper.parse()`：实际执行解析
3. 第一个成功返回非空结果的策略即终止链条
4. 调用 `cleaner.clean()` 对结果做字段标准化

### 2.2 策略抽象基类：ABCScraperStrategy

位于 [scraper_strategies.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L137-L175)，定义了所有策略必须实现的接口：

```python
class ABCScraperStrategy(ABC):
    @abstractmethod
    def can_scrape(self) -> bool: ...

    @abstractmethod
    async def get_html(self, url: str) -> str: ...

    @abstractmethod
    async def parse(self, on_progress=...) -> tuple[Recipe, ScrapedExtras] | tuple[None, None]: ...
```

### 2.3 四个具体策略详解

#### 策略 1：RecipeScraperPackage（schema.org 结构化数据）

位置：[scraper_strategies.py#L178-L340](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L178-L340)

- **适用场景**：网页包含标准 schema.org Recipe 结构化数据（嵌入 `<script type="application/ld+json">`）
- **依赖库**：第三方 `recipe-scrapers` Python 包
- **核心方法**：
  - `scrape_url()`：调用 `scrape_html()` 解析 schema 数据，验证 ingredients/instructions 非空
  - `clean_scraper()`：通过 `try_get_default()` 辅助函数**双层兜底取值**：
    1. 先调用 `recipe-scrapers` 库的方法（如 `scraped_data.title()`）
    2. 失败则直接从原始 schema dict 读取（如 `scraped_data.schema.data.get("name")`）
- **字段映射**：直接将 schema.org 字段映射到 Recipe 对象（name、image、description、nutrition、recipeYield、recipeIngredient、recipeInstructions、totalTime、prepTime、perform_time、org_url、notes）

#### 策略 2：RecipeScraperOpenAITranscription（视频转录）

位置：[scraper_strategies.py#L442-L610](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L442-L610)

- **适用场景**：URL 是视频网站（YouTube 等，由 `yt-dlp` 的 extractor 判断）且启用了音频 AI 服务
- **执行流程**：
  1. `can_scrape()`：检查 `audio_provider_enabled` 配置 + yt-dlp extractor 匹配
  2. `_download_audio()`：用 yt-dlp 下载音频 MP3 + 字幕（优先英/法/西/德/意字幕）
  3. 如字幕存在，直接解析 VTT 字幕文本；否则调用 OpenAI `transcribe_audio()` 做语音转文字
  4. 将视频标题、描述、转录文本拼接后发送给 AI，按 `OpenAIRecipe` schema 返回结构化数据
  5. 手动构造 Recipe 对象，从 AI 返回字段映射
- **特点**：唯一不需要 HTML 的策略，完全基于音频/视频内容

#### 策略 3：RecipeScraperOpenAI（AI 解析网页全文）

位置：[scraper_strategies.py#L342-L430](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L342-L430)

- **适用场景**：网页无标准 schema.org 数据，但启用了 AI 服务
- **继承关系**：继承自 `RecipeScraperPackage`，复用其 `clean_scraper()` 和 `parse()`
- **差异化逻辑**（重写 `get_html()`）：
  1. 抓取原始 HTML
  2. `format_html_to_text()`：提取纯文本 + JSON-LD 数据 + 最大尺寸图片 URL
  3. 调用 AI 服务（prompt: `recipes.scrape-recipe`），要求返回 JSON 格式的 schema.org Recipe
  4. 将 AI 返回的 JSON 包装成假 HTML（嵌入 ld+json script 标签）
  5. 后续流程与 RecipeScraperPackage 完全一致（用 `recipe-scrapers` 解析假 HTML）

#### 策略 4：RecipeScraperOpenGraph（OG 元数据保底）

位置：[scraper_strategies.py#L613-L672](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L613-L672)

- **适用场景**：所有其他策略都失败时的最后兜底
- **数据来源**：HTML `<meta>` 标签的 Open Graph 属性（og:title、og:description、og:image 等）
- **特点**：只能提取非常有限的字段（名称、描述、封面图、标签），食材和步骤会填充占位文本（"Could not detect ingredients/instructions"）

### 2.4 反爬处理：safe_scrape_html

位于 [scraper_strategies.py#L58-L134](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraper_strategies.py#L58-L134)

核心机制：
- **TLS 指纹模拟**：依次尝试 impersonate chrome/firefox/safari/edge 四种浏览器指纹（基于 httpx-curl-cffi），绕过 Cloudflare 等 JA3/JA4 指纹检测
- **超时保护**：总超时 15 秒，流式读取时也做二次超时检测，防止恶意大文件攻击
- **编码自适应**：优先使用响应头 charset，fallback 到 auto-detected encoding

---

## 三、字段清理层（cleaner.py）

位于 [cleaner.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py)

### 3.1 总入口：clean()

[cleaner.py#L39-L75](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/cleaner.py#L39-L75)

无论哪个策略返回的 Recipe，都要经过统一清理流程，确保数据符合数据库 schema：

```python
def clean(recipe_data: Recipe | dict, translator: Translator, url=None) -> Recipe:
    # 1. 统一转为 dict 处理
    # 2. 调用各字段专用 clean_* 函数
    recipe_data["slug"] = slugify(recipe_data.get("name", ""))
    recipe_data["description"] = clean_string(...)
    recipe_data["prepTime"] = clean_time(...)
    recipe_data["performTime"] = clean_time(...)
    recipe_data["totalTime"] = clean_time(...)
    recipe_data["recipeServings"], recipe_data["recipeYieldQuantity"], recipe_data["recipeYield"] = clean_yield(...)
    recipe_data["recipeCategory"] = clean_categories(...)
    recipe_data["recipeIngredient"] = clean_ingredients(...)
    recipe_data["recipeInstructions"] = clean_instructions(...)
    recipe_data["image"] = clean_image(...)[0]
    recipe_data["notes"] = clean_notes(...)
    recipe_data["rating"] = clean_int(...)
    return Recipe(**recipe_data)
```

### 3.2 各字段清理函数的多态处理

每个清理函数都使用 Python `match` 语句处理多种可能的输入格式（因为不同网站返回的数据结构差异极大）：

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

**关键点**：`clean_instructions()` 还会递归调用自身处理嵌套结构（如 HowToSection → HowToStep），并循环调用 `clean_string()` 直到稳定（处理多级 HTML 转义）。

---

## 四、食谱字段落库流程

### 4.1 ScrapedExtras：标签与分类的特殊处理

位于 [scraped_extras.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/scraped_extras.py#L19-L82)

标签（tags）和分类（categories）**不直接存储在 Recipe 表中**，而是独立表通过外键关联，因此需要单独处理：

```python
class ScrapedExtras:
    def set_tags(self, tags: list[str]) -> None: ...
    def set_categories(self, categories: list[str]) -> None: ...

    def use_tags(self, ctx: ScraperContext) -> list[TagOut]:
        # 对每个标签名：
        #   1. slugify 后去重
        #   2. 查询数据库是否已存在（按 slug）
        #   3. 不存在则创建新 Tag
        #   4. 返回 TagOut 对象列表

    def use_categories(self, ctx: ScraperContext) -> list[TagOut]:
        # 逻辑同 use_tags，但操作 categories 表
```

### 4.2 最终落库：_finish_recipe_from_web()

位于 [recipe_crud_routes.py#L250-L274](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/routes/recipe/recipe_crud_routes.py#L250-L274)

```python
def _finish_recipe_from_web(self, req, recipe, extras):
    # 1. 根据请求参数决定是否保存标签和分类
    if req.include_tags:
        ctx = ScraperContext(self.repos)
        recipe.tags = extras.use_tags(ctx)

    if req.include_categories:
        ctx = ScraperContext(self.repos)
        recipe.recipe_category = extras.use_categories(ctx)

    # 2. 核心落库：调用 RecipeService 写入数据库
    new_recipe = self.service.create_one(recipe)

    # 3. 发布领域事件（recipe_created），触发通知和后续处理
    self.publish_event(event_type=EventTypes.recipe_created, ...)

    return new_recipe.slug
```

### 4.3 批量导入落库

批量场景在 [recipe_bulk_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/119-mealie/mealie/services/scraper/recipe_bulk_scraper.py) 中处理：

- 并发控制：`asyncio.Semaphore(3)` 限制同时最多 3 个抓取任务
- 每条结果独立写入：`self.service.create_one(recipe)`
- 生成导入报告（Report），记录每条 URL 的成功/失败状态和异常信息
- 标签/分类直接取自批量请求参数（`b.tags` / `b.categories`），不走 ScrapedExtras

---

## 五、完整链路时序图

```
用户
  │
  ▼
[recipe_crud_routes.py] parse_recipe_url()
  │
  ├─► _create_recipe_from_web()
  │     │
  │     ├─► on_progress() ──── SSE 进度推送
  │     │
  │     └─► [scraper.py] create_from_html()
  │           │
  │           ├─► URL 正则校验
  │           │
  │           └─► [recipe_scraper.py] RecipeScraper.scrape()
  │                 │
  │                 ├─► safe_scrape_html()  ──── 抓取 HTML（TLS指纹模拟+超时保护）
  │                 │
  │                 ├─► [责任链遍历]
  │                 │     ├─► RecipeScraperPackage.can_scrape()? → parse()
  │                 │     │     └─► recipe-scrapers 库解析 schema.org
  │                 │     ├─► RecipeScraperOpenAITranscription.can_scrape()? → parse()
  │                 │     │     └─► yt-dlp 下载 → 字幕/AI转录 → AI 结构化解析
  │                 │     ├─► RecipeScraperOpenAI.can_scrape()? → parse()
  │                 │     │     └─► HTML 提取文本 → AI 生成 schema JSON → 复用 Package 解析
  │                 │     └─► RecipeScraperOpenGraph.can_scrape()? → parse()
  │                 │           └─► 解析 og:* meta 标签（保底）
  │                 │
  │                 └─► [cleaner.py] clean()  ──── 所有字段标准化清理
  │
  ├─► RecipeDataService.scrape_image()  ──── 下载封面图
  │
  └─► _finish_recipe_from_web()
        │
        ├─► ScrapedExtras.use_tags() / use_categories()  ──── 查找或创建标签/分类
        │
        ├─► RecipeService.create_one()  ──── 写入数据库（Recipe 表）
        │
        └─► publish_event(recipe_created)  ──── 发布领域事件
```

---

## 六、关键设计模式总结

| 设计模式 | 应用位置 | 作用 |
|----------|---------|------|
| **策略模式** | `ABCScraperStrategy` + 4 个实现类 | 封装不同来源的解析算法 |
| **责任链模式** | `RecipeScraper.scrape()` 遍历策略列表 | 按优先级降级尝试，首个成功即终止 |
| **模板方法模式** | `RecipeScraperOpenAI` 继承 `RecipeScraperPackage`，只重写 `get_html()` | 复用解析流程，只差异化 HTML 获取方式 |
| **多态匹配** | `cleaner.py` 中大量 `match` 语句 | 统一处理异构输入格式 |
| **数据传输对象** | `ScrapedExtras` 暂存标签/分类，与 Recipe 主体分离 | 延迟处理关联数据，支持可选落库 |
