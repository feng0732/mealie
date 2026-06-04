# Mealie 菜谱导入流程：从抓取到落库

本文档追踪 Mealie 中「菜谱导入」的完整数据流，涵盖 API 入口处理、HTML 抓取、内容解析、字段映射和数据保存落库五个阶段。

---

## 一、总体流程概览

```
用户提交 URL / HTML / JSON-LD
        │
        ▼
┌─── API 路由层 ───┐
│ RecipeController  │  校验请求、选择入口
└───────┬──────────┘
        │
        ▼
┌─── 抓取调度层 ───┐
│  create_from_html │  获取 HTML、调用 RecipeScraper
└───────┬──────────┘
        │
        ▼
┌─── 策略解析层 ───┐
│  RecipeScraper    │  按优先级尝试 4 种解析策略
│  ┌─────────────┐ │
│  │ 1. RecipeScraperPackage  (recipe_scrapers 库)   │
│  │ 2. RecipeScraperOpenAITranscription (视频转录)  │
│  │ 3. RecipeScraperOpenAI    (AI 解析 HTML)        │
│  │ 4. RecipeScraperOpenGraph (OG 标签兜底)         │
│  └─────────────┘ │
└───────┬──────────┘
        │
        ▼
┌─── 数据清洗层 ───┐
│  cleaner.clean()  │  标准化各字段格式
└───────┬──────────┘
        │
        ▼
┌─── 后处理层 ─────┐
│ _finish_recipe_   │  处理 tags/categories、
│ from_web()        │  下载图片、生成 slug
└───────┬──────────┘
        │
        ▼
┌─── 服务 & 仓库层 ┐
│ RecipeService     │  create_one() → 填充默认值
│ RepositoryRecipes │  create() → 写入数据库
└──────────────────┘
```

---

## 二、API 入口处理

所有导入相关的路由定义在 [recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/routes/recipe/recipe_crud_routes.py#L88-L275) 的 `RecipeController` 中。

### 2.1 入口路由一览

| 路由 | 方法 | 请求体 | 说明 |
|------|------|--------|------|
| `/recipes/create/url` | POST | `ScrapeRecipe` | 通过 URL 导入（同步返回 slug） |
| `/recipes/create/url/stream` | POST | `ScrapeRecipe` | 通过 URL 导入（SSE 流式返回进度） |
| `/recipes/create/html-or-json` | POST | `ScrapeRecipeData` | 通过原始 HTML 或 JSON-LD 导入 |
| `/recipes/create/html-or-json/stream` | POST | `ScrapeRecipeData` | 同上，SSE 流式 |
| `/recipes/create/url/bulk` | POST | `CreateRecipeByUrlBulk` | 批量 URL 导入（后台任务） |
| `/recipes/test-scrape-url` | POST | `ScrapeRecipeTest` | 测试抓取，不落库 |

### 2.2 请求体 Schema

定义在 [recipe_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/schema/recipe/recipe_scraper.py#L1-L34)：

- **`ScrapeRecipe`** — 包含 `url: str`、`include_tags: bool`、`include_categories: bool`
- **`ScrapeRecipeData`** — 包含 `data: str`（HTML 或 JSON-LD 字符串）、`url: str | None`、同上 include 开关
- **`ScrapeRecipeTest`** — 包含 `url: str`、`use_openai: bool`

### 2.3 核心入口方法：`_create_recipe_from_web`

所有单条导入路由最终汇聚到 [\_create_recipe_from_web](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/routes/recipe/recipe_crud_routes.py#L196-L248)：

```python
async def _create_recipe_from_web(self, req: ScrapeRecipe | ScrapeRecipeData) -> AsyncIterable[ServerSentEvent]:
```

关键步骤：
1. 根据 `req` 类型判断：`ScrapeRecipeData` 有 `html`，`ScrapeRecipe` 只有 `url`
2. 创建异步队列用于 SSE 进度推送
3. 调用 `create_from_html(url, repos, translator, html, on_progress)` → 返回 `(Recipe, ScrapedExtras)`
4. 调用 `_finish_recipe_from_web(req, recipe, extras)` 完成后续处理

### 2.4 JSON-LD 预处理

当 `ScrapeRecipeData.data` 以 `{` 开头时，视为 JSON-LD，先包装成 HTML：

```python
req.data = RecipeScraperPackage.ld_json_to_html(req.data)
# 生成: <script type="application/ld+json">{...}</script>
```

见 [scraper_strategies.py#L182-L188](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L182-L188)。

---

## 三、HTML 抓取与内容解析

### 3.1 抓取入口：`create_from_html`

定义在 [scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper.py#L26-L85)：

```python
async def create_from_html(url, repos, translator, html=None, on_progress=None) -> tuple[Recipe, ScrapedExtras | None]
```

流程：
1. 若无 `html`，从 URL 中用正则提取有效链接
2. 调用 `RecipeScraper.scrape(url, html)` → 得到 `Recipe` + `ScrapedExtras`
3. 为 Recipe 分配 `uuid4()` 作为 `id`
4. 调用 `RecipeDataService.scrape_image()` 下载图片到本地
5. 设置 `slug`（由 `name` 生成）和 `image`（缓存 key）

### 3.2 HTML 网络抓取：`safe_scrape_html`

定义在 [scraper_strategies.py#L58-L134](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L58-L134)。

核心逻辑：
- 使用 `httpx-curl-cffi` 模拟浏览器 TLS 指纹（chrome → firefox → safari → edge 依次尝试），绕过 Cloudflare 等反爬
- 请求超时 15 秒，流式读取，超过超时则抛出 `ForceTimeoutException`
- 403 时切换下一个指纹重试，其他 4xx/5xx 直接失败

### 3.3 解析策略选择：`RecipeScraper.scrape`

定义在 [recipe_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/recipe_scraper.py#L27-L89)。

按固定优先级依次尝试 4 种策略，**首个成功的即返回**：

```python
DEFAULT_SCRAPER_STRATEGIES = [
    RecipeScraperPackage,               # 1. recipe_scrapers 库（最优先）
    RecipeScraperOpenAITranscription,    # 2. 视频音频转录
    RecipeScraperOpenAI,                 # 3. AI 解析
    RecipeScraperOpenGraph,              # 4. OG 标签兜底
]
```

每种策略先调用 `can_scrape()` 判断是否可用，再调用 `parse()` 执行解析。

#### 策略 1：`RecipeScraperPackage`

见 [scraper_strategies.py#L178-L339](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L178-L339)。

- 依赖第三方库 `recipe_scrapers`，调用 `scrape_html(html, org_url=url, supported_only=False)`
- 支持标准 schema.org/Recipe 和 wild 模式
- 解析后通过 `clean_scraper()` 方法将 scraped data 映射为 `Recipe` 对象

#### 策略 2：`RecipeScraperOpenAITranscription`

见 [scraper_strategies.py#L442-L610](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L442-L610)。

- 适用于视频 URL（通过 yt-dlp 检测）
- 使用 yt-dlp 下载音频和字幕
- 优先使用字幕；若无可读字幕，则用 OpenAI Whisper 转录
- 将转录文本提交给 OpenAI 提取菜谱结构

#### 策略 3：`RecipeScraperOpenAI`

见 [scraper_strategies.py#L342-L430](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L342-L430)。

- 需要群组启用 AI 功能
- 提取页面文本 + JSON-LD 数据 + OG 图片
- 将内容提交给 OpenAI，让其返回 JSON-LD 格式的菜谱
- 将 AI 返回的 JSON-LD 包装成 HTML，再走 `RecipeScraperPackage` 的 `parse()` 流程

#### 策略 4：`RecipeScraperOpenGraph`

见 [scraper_strategies.py#L613-L672](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L613-L672)。

- 兜底方案：从 Open Graph 元标签提取标题、描述、图片
- 食材和步骤只标记为 "Could not detect..."
- 使用 `extruct` 库提取 OG 数据

---

## 四、字段映射

### 4.1 `RecipeScraperPackage.clean_scraper` 映射表

这是最核心的映射逻辑，位于 [scraper_strategies.py#L193-L294](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraper_strategies.py#L193-L294)。

| schema.org / scraped 字段 | Recipe Schema 字段 | 清洗函数 | 说明 |
|---|---|---|---|
| `name` | `name` | `clean_string` | 菜谱名称 |
| `image` | `image` | `clean_image` | 图片 URL，支持 str/list/dict 多种格式 |
| `description` | `description` | `clean_string` | 描述文本 |
| `nutrition` / `nutrients` | `nutrition` | `clean_nutrition` | 营养信息字典 |
| `recipeYield` / `yields` | `recipe_yield` | `clean_string` | 产出描述（如 "4 servings"） |
| `recipeIngredient` / `ingredients` | `recipe_ingredient` | `clean_ingredients` | 食材列表 |
| `recipeInstructions` / `instructions` | `recipe_instructions` | `clean_instructions` → `[RecipeStep]` | 步骤列表 |
| `totalTime` / `total_time` | `total_time` | `clean_time` | 总时长 |
| `prepTime` / `prep_time` | `prep_time` | `clean_time` | 准备时长 |
| `performTime` / `cook_time` | `perform_time` | `clean_time` | 烹饪时长 |
| `url` | `org_url` | `clean_string` | 原始来源 URL |
| `keywords` | → `ScrapedExtras._tags` | `clean_tags` | 关键词映射为标签 |
| `recipeCategory` / `category` | → `ScrapedExtras._categories` | `clean_categories` | 分类 |
| `notes` | `notes` | — | 备注 |

> **注意**：tags 和 categories 不直接写入 Recipe 对象，而是存入 `ScrapedExtras`，后续在 `_finish_recipe_from_web` 中根据 `include_tags` / `include_categories` 选项决定是否附加。

### 4.2 `ScrapedExtras` — tags/categories 的延迟处理

定义在 [scraped_extras.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/scraped_extras.py#L19-L81)。

`ScrapedExtras` 暂存了 `_tags: list[str]` 和 `_categories: list[str]`，只有当用户勾选了 `include_tags` / `include_categories` 时，才通过 `use_tags()` / `use_categories()` 转换为数据库对象：

1. 对每个 tag/category 名字做 `slugify`
2. 查询数据库是否已存在（按 slug 匹配）
3. 若不存在，创建新记录（`TagSave` / `CategorySave`）
4. 返回完整的 `TagOut` / `CategoryOut` 列表，赋给 `recipe.tags` / `recipe.recipe_category`

### 4.3 `cleaner.clean` — 全局数据清洗

定义在 [cleaner.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/cleaner.py#L39-L75)。

无论哪种解析策略，解析结果都会经过 `cleaner.clean(recipe_data, translator)` 全局清洗。核心流程：

1. 将 `Recipe` 对象序列化为 dict（如果传入的是对象而非 dict）
2. 逐字段调用对应的 `clean_*` 函数
3. 最终用 `Recipe(**recipe_data)` 重新构建 Pydantic 模型

#### 各清洗函数一览

| 函数 | 作用 |
|------|------|
| `clean_string` | 去 HTML 标签、HTML 实体解码、压缩空白 |
| `clean_image` | 统一图片 URL 格式：str / list[str] / dict["url"] / list[dict] → `list[str]` |
| `clean_instructions` | 统一步骤格式：HowToSection / dict / str / list → `[{"text": "..."}]` |
| `clean_ingredients` | 统一食材格式：str / list[str] / list[dict] |
| `clean_time` | ISO 8601 duration (`PT1H30M`) / timedelta / 数字(分钟) → 人类可读字符串 |
| `clean_yield` | 产出描述 → `(servings, yield_quantity, yield_string)` 三元组 |
| `clean_categories` | 逗号分隔字符串 / list → `["Category1", "Category2"]` |
| `clean_tags` | 同上 |
| `clean_nutrition` | 提取数值，钠/胆固醇单位转换 |
| `clean_notes` | 标准化为 `[{"title": "", "text": "..."}]` |

### 4.4 Pydantic `Recipe` Schema 的字段校验器

定义在 [recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/schema/recipe/recipe.py#L182-L298)。

`Recipe` 模型本身也有一组 `field_validator` 在反序列化时自动做格式适配：

| 校验器 | 逻辑 |
|--------|------|
| `validate_slug` | 若无 slug 则从 `name` 自动生成 |
| `validate_ingredients` | 若食材列表元素是纯字符串，转为 `RecipeIngredient(note=x)` |
| `validate_tags` | 若元素是字符串，转为 `RecipeTag(id=uuid4(), name=c, slug=slugify(c))` |
| `validate_categories` | 同上，转为 `RecipeCategory` |
| `validate_group_id` / `validate_household_id` / `validate_user_id` | 若是 int（兼容旧数据）则替换为新 uuid4 |
| `validate_nutrition` | 空值 → `None` |

---

## 五、保存落库步骤

### 5.1 `_finish_recipe_from_web` — 控制器的后处理

定义在 [recipe_crud_routes.py#L250-L274](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/routes/recipe/recipe_crud_routes.py#L250-L274)。

```python
def _finish_recipe_from_web(self, req, recipe, extras) -> str:
    if req.include_tags:
        recipe.tags = extras.use_tags(ScraperContext(self.repos))
    if req.include_categories:
        recipe.recipe_category = extras.use_categories(ScraperContext(self.repos))
    new_recipe = self.service.create_one(recipe)    # 关键：调用 Service 落库
    # ... 发布事件 ...
    return new_recipe.slug
```

### 5.2 `RecipeService.create_one` — 服务层落库

定义在 [recipe_service.py#L202-L245](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/recipe/recipe_service.py#L202-L245)。

```python
def create_one(self, create_data: Recipe | CreateRecipe) -> Recipe:
```

关键步骤：

1. **填充默认值**：通过 `_recipe_creation_factory` 设置：
   - `user_id` = 当前用户 ID
   - `household_id` = 当前家庭 ID
   - `group_id` = 当前群组 ID
   - 若 `recipe_ingredient` 为空，填入默认占位食材
   - 若 `recipe_instructions` 为空，填入默认占位步骤

2. **设置 RecipeSettings**：根据家庭偏好（`household.preferences`）生成默认设置（公开/显示营养/显示资产/横屏/禁评论）

3. **写入数据库**：`self.repos.recipes.create(data)`

4. **处理评分**：若 `rating` 有值，创建 `UserRatingCreate` 记录

5. **创建时间线事件**：插入 `RecipeTimelineEventCreate`（记录菜谱创建）

### 5.3 `RepositoryRecipes.create` — 仓库层写入

定义在 [repository_recipes.py#L95-L108](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/repos/repository_recipes.py#L95-L108)。

```python
def create(self, document: Recipe) -> Recipe:
    max_retries = 10
    for i in range(1, 11):
        try:
            return super().create(document)
        except IntegrityError:          # slug 唯一约束冲突
            self.session.rollback()
            document.name = f"{original_name} ({i})"
            document.slug = create_recipe_slug(document.name)
```

- 如果 `slug + group_id` 唯一约束冲突，自动重命名（加后缀 `(1)`、`(2)` ...），最多重试 10 次
- 最终调用父类 `RepositoryGeneric.create`，将 Pydantic `Recipe` 序列化为 dict 后通过 `auto_init` 机制创建 `RecipeModel` ORM 对象并 commit

### 5.4 `RecipeModel` — SQLAlchemy 模型

定义在 [recipe.py (model)](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/db/models/recipe/recipe.py#L42-L179)。

关键字段与数据库列的映射：

| Recipe Schema 字段 | RecipeModel 列 | 数据库列类型 |
|---|---|---|
| `id` | `id` | GUID (UUID) |
| `slug` | `slug` | String, 唯一约束(slug+group_id) |
| `name` | `name` | String, NOT NULL |
| `description` | `description` | String |
| `image` | `image` | String |
| `total_time` | `total_time` | String |
| `prep_time` | `prep_time` | String |
| `perform_time` | `perform_time` | String |
| `recipe_yield` | `recipe_yield` | String |
| `recipe_yield_quantity` | `recipe_yield_quantity` | Float |
| `recipe_servings` | `recipe_servings` | Float |
| `org_url` | `org_url` | String |
| `group_id` | `group_id` | GUID, FK → groups.id |
| `user_id` | `user_id` | GUID, FK → users.id |
| `rating` | `rating` | Float |
| `recipe_ingredient` | `recipe_ingredient` | 关系 → RecipeIngredientModel (cascade) |
| `recipe_instructions` | `recipe_instructions` | 关系 → RecipeInstruction (cascade) |
| `recipe_category` | `recipe_category` | 多对多 → Category (通过 recipes_to_categories) |
| `tags` | `tags` | 多对多 → Tag (通过 recipes_to_tags) |
| `tools` | `tools` | 多对多 → Tool (通过 recipes_to_tools) |
| `nutrition` | `nutrition` | 一对一 → Nutrition (cascade) |
| `notes` | `notes` | 一对多 → Note (cascade) |
| `assets` | `assets` | 一对多 → RecipeAsset (cascade) |
| `settings` | `settings` | 一对一 → RecipeSettings (cascade) |

`RecipeModel.__init__` 中的 `@auto_init()` 装饰器自动将传入的 dict 参数分发到对应的关系字段，如：
- `nutrition: dict` → `Nutrition(**nutrition)`
- `recipe_instructions: list[dict]` → `[RecipeInstruction(**step)]`
- `recipe_ingredient: list[dict]` → `[RecipeIngredientModel(**ingr)]`

### 5.5 图片下载与存储

在 `create_from_html` 中，图片处理发生在解析之后、落库之前：

```python
recipe_data_service = RecipeDataService(new_recipe.id)
await recipe_data_service.scrape_image(new_recipe.image)
new_recipe.image = cache.new_key(4)   # 替换为缓存版本号
```

[RecipeDataService](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/recipe/recipe_data_service.py#L57-L169) 负责图片下载和存储：
1. 解析图片 URL（支持 str / list / dict 格式）
2. 若有多个 URL，选 `Content-Length` 最大的
3. 下载图片到 `{RECIPE_DATA_DIR}/{recipe_id}/images/original.{ext}`
4. 调用 `PillowMinifier.minify()` 生成缩略图
5. `recipe.image` 字段存的是缓存 key（随机数），而非 URL 本身

---

## 六、批量导入

定义在 [recipe_bulk_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/31-mealie/mealie/services/scraper/recipe_bulk_scraper.py#L21-L127)。

`RecipeBulkScraperService.scrape()` 作为后台任务执行：

1. 创建导入报告 `ReportCreate`
2. 使用 `asyncio.Semaphore(3)` 控制并发
3. 对每个 URL 调用 `create_from_html()` 获取 Recipe
4. 附加请求中的 tags / categories
5. 调用 `service.create_one(recipe)` 落库
6. 汇总成功/失败条目，更新报告状态

---

## 七、端到端数据流示例

以用户提交 URL `https://example.com/recipe` 为例，`include_tags=True`：

```
1. POST /recipes/create/url  { url: "https://example.com/recipe", includeTags: true }

2. RecipeController._create_recipe_from_web()
   → 判断 req 是 ScrapeRecipe，html=None

3. create_from_html(url, repos, translator, html=None)
   → RecipeScraper.scrape(url, html=None)

4. RecipeScraper: html 为空，先调用 safe_scrape_html(url) 获取 HTML
   → 轮流尝试 chrome/firefox/safari/edge TLS 指纹

5. 依次尝试策略：
   a. RecipeScraperPackage: scrape_html(html) 成功
      → clean_scraper() 将 schema.org 数据映射为 Recipe + ScrapedExtras
   b. cleaner.clean(recipe_result, translator) 全局清洗

6. create_from_html 后处理：
   → 分配 recipe.id = uuid4()
   → RecipeDataService.scrape_image(recipe.image) 下载图片
   → recipe.slug = slugify(recipe.name)
   → recipe.image = cache_key

7. _finish_recipe_from_web(req, recipe, extras):
   → include_tags=True → extras.use_tags(ScraperContext(repos))
     → 对每个 tag: 查 DB 是否存在，不存在则创建
     → recipe.tags = [TagOut, ...]
   → self.service.create_one(recipe)

8. RecipeService.create_one(recipe):
   → _recipe_creation_factory() 填充 user_id/household_id/group_id
   → 设置 RecipeSettings（从家庭偏好）
   → self.repos.recipes.create(data)

9. RepositoryRecipes.create(data):
   → super().create(document)
   → RecipeModel(session, **data.model_dump())
     → auto_init 分发字段到 Nutrition, RecipeInstruction, RecipeIngredientModel 等
   → session.commit()
   → 若 slug 冲突，自动重命名重试（最多10次）

10. 创建 UserRating 记录（如有 rating）
    创建 RecipeTimelineEvent 记录

11. 返回 new_recipe.slug → 响应 "chocolate-cake"
```
