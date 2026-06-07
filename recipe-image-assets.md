# Recipe Image & Asset 处理流程全链路解析

## 目录结构总览

每个 Recipe 的数据按 UUID 存储在磁盘上：

```
{RECIPE_DATA_DIR}/{recipe_id}/
├── images/
│   ├── original.webp          # 原图 (max 2048x2048, quality 80)
│   ├── min-original.webp      # 缩略图 (max 1024x1024, quality 80)
│   ├── tiny-original.webp     # 方形裁剪缩略图 (600x600 retina / 300x300 逻辑, quality 80)
│   └── timeline/
│       └── {timeline_event_id}/
│           ├── original.webp
│           ├── min-original.webp
│           └── tiny-original.webp
└── assets/
    └── {slugified_name}.{ext}  # 用户上传的附件文件
```

相关代码文件：
- 磁盘目录计算：[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/schema/recipe/recipe.py#L202-L237)（`directory_from_id`, `image_dir_from_id`, `asset_dir_from_id`, `timeline_image_dir_from_id`）
- 图片类型枚举：[recipe_image_types.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/schema/recipe/recipe_image_types.py)
- 资产数据模型：[assets.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/db/models/recipe/assets.py)

---

## 1. 图片上传流程

### 1.1 本地文件上传（PUT /api/recipes/{slug}/image）

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L635-L642)

**写入顺序（先鉴权后落盘，安全）**：

```
① RecipeService.get_one(slug)           # 读取 Recipe（仅 household 过滤，无权限校验）
② RecipeService.can_update([slug])      # ★ 权限校验（在此处拦截）
   → 失败：抛 PermissionDenied → 403，磁盘不写任何东西
③ RecipeDataService(recipe.id)           # 创建 {recipe_id}/images/ 等目录
④ RecipeDataService.write_image(bytes, extension)
   ├─ 写 images/original.{ext}
   ├─ PillowMinifier.minify() 生成三种 webp
   └─ purge=True → 删除 original.{ext}，只留 .webp
⑤ RepositoryRecipes.update_image(slug)
   → DB: recipes.image = randint(0, 255)  （仅作缓存版本号）
```

关键实现：
- [RecipeService.update_recipe_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L530-L538)
- [RecipeDataService.write_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L85-L109)
- [RepositoryRecipes.update_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/repos/repository_recipes.py#L155-L160)

前端组件：[RecipeImageUploadBtn.vue](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/components/Domain/Recipe/RecipeImageUploadBtn.vue)
前端 API：[recipe.ts updateImage](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/lib/api/user/recipes/recipe.ts#L133-L139)

---

### 1.2 外链 URL 抓取图片（POST /api/recipes/{slug}/image）

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L614-L633)

**路由核心代码（控制流依据）**：
```python
recipe = self.mixins.get_one(slug)                 # ① 仅 household 过滤，无权限校验
data_service = RecipeDataService(recipe.id)         # ② 创建 {recipe_id}/images/ 等目录（磁盘已变化）

try:
    await data_service.scrape_image(url.url)        # ③ 三种可能结果（见下）
except NotAnImageError:
    raise HTTPException(400, ...)                   # ③b：捕获 → 400，函数提前返回
except InvalidDomainError:
    raise HTTPException(400, ...)                   # ③b：捕获 → 400，函数提前返回
                                                      # ValueError/PIL 异常未捕获 → 500，提前返回

recipe.image = cache.cache_key.new_key()            # ④ 仅在 ③ 无异常时执行
self.service.update_one(recipe.slug, recipe)        # ⑤ 仅在 ③ 无异常时执行
                                                     #   → _pre_update_check → can_update([slug])
```

**设计缺陷**：写磁盘（步骤②③）发生在权限校验（步骤⑤）之前，存在 TOCTOU 漏洞。

---

#### 三种抓图结果 × 两种权限结果的分支矩阵

`scrape_image` 的返回/抛异常决定了步骤④⑤是否被执行，进而决定 `can_update` 是否被触发、DB 是否变化、磁盘留下什么。

##### 情况 A：抓图成功（scrape_image 正常返回 None）

| 权限结果 | can_update 是否触发 | DB `recipes.image` | 磁盘状态 |
|---------|-------------------|-------------------|---------|
| ✅ can_update 通过（权限合法） | ✅ 是（在步骤⑤ update_one → _pre_update_check 中） | 更新为新的 4 位随机缓存键 | `{recipe_id}/images/` 下三种 webp 齐全（original / min / tiny） |
| ❌ can_update 失败（PermissionDenied） | ✅ 是（触发后抛异常） | **保持旧值**（update_one 回滚） | 三种 webp 已写入，成为**孤儿文件** ❌ 无自动清理机制（`check_assets` 只清 assets/；`clean/images` 只删非 webp） |

##### 情况 B：抓图静默失败（网络异常 / 非 200 状态码 → scrape_image `return None`，不抛异常）

此种情况步骤④⑤**仍会照常执行**，因为没有异常。

| 权限结果 | can_update 是否触发 | DB `recipes.image` | 磁盘状态 |
|---------|-------------------|-------------------|---------|
| ✅ can_update 通过 | ✅ 是 | 更新为新的 4 位随机缓存键（**无意义的缓存键刷新**：实际图片根本没变化/不存在） | 仅空目录 `{recipe_id}/images/` 和 `{recipe_id}/assets/`（scrape_image 在 GET 失败时不写任何文件） |
| ❌ can_update 失败 | ✅ 是（触发后抛异常） | **保持旧值** | 仅空目录（无 webp 文件，残留最少） |

**无意义缓存刷新的副作用**：用户发起一次网络失败的抓图请求，若权限合法，前端拿到新的 version 参数会绕过浏览器缓存重新请求图片，但图片和旧图完全一样（或根本不存在）。

##### 情况 C：抓图抛异常（NotAnImageError / InvalidDomainError / ValueError / PIL 解码失败等）

路由在步骤③提前 `raise HTTPException(400)` 或异常冒泡为 500，**步骤④⑤永远不执行**。

| 子情况 | can_update 是否触发 | DB `recipes.image` | 磁盘状态 |
|--------|-------------------|-------------------|---------|
| Content-Type 非 image（NotAnImageError） | ❌ 否 | **完全不变**（未触及 update_one） | 仅空目录（异常在 write_image 之前抛出） |
| 非法域名（InvalidDomainError） | ❌ 否 | **完全不变** | 仅空目录（异常在写磁盘之前） |
| URL 解析为空（ValueError） | ❌ 否 | **完全不变** | 仅空目录（异常在 write_image 之前） |
| PIL 解码失败（损坏文件/截断/HEIC 缺依赖等） | ❌ 否 | **完全不变** | `write_image` 内部已 `unlink(missing_ok=True)` 删除了临时 `original.{ext}`；若 minify 已生成了部分 webp 后才失败（如 original.webp 生成成功但 tiny 时磁盘满），已生成的 webp 会**残留为孤儿文件** |

**情况 C 小结**：抓图抛异常时，权限校验 `can_update` 根本不会被触发——即使调用者完全没有权限，磁盘上仍可能留下空目录或部分 webp 文件。

---

#### 未授权遗留状态总览

| 抓图结果 | 权限合法 | 权限非法（PermissionDenied） |
|---------|---------|--------------------------|
| **A. 成功** | 正常，三 webp + DB 更新 | 三 webp 孤儿 + DB 未变 |
| **B. 静默失败（网络/非 200）** | 空目录 + DB 无意义更新 | 空目录 + DB 未变 |
| **C. 抛异常** | 空目录（或部分 webp 孤儿）+ DB 未变 | 与左列完全相同（can_update 甚至没被调用） |

**关键发现**：只要调用者能通过身份认证（拿到合法 session/token）并找到一个存在的 recipe slug，即使该调用者对这个 recipe 没有编辑权限，也能通过 POST `/{slug}/image` 在服务器磁盘上创建目录并写入图片文件（情况 A 权限非法分支、情况 C 全部子情况）。

**关键实现引用**：
- [scrape_image_url 路由](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L614-L633) —— 无前置 can_update
- [RecipeDataService.scrape_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L119-L169)
- [RecipeService.update_one → _pre_update_check](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L445-L478)

---

### 1.3 资产文件上传（POST /api/recipes/{slug}/assets）

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L653-L699)

**写入顺序（先落盘后鉴权，同样存在 TOCTOU 漏洞）**：

```
① 扩展名白名单校验 / 文件名 slugify / 路径穿越校验         # 轻量前置校验
② recipe = self.service.get_one(slug)                    # 读取 Recipe（无权限校验）
③ dest = recipe.asset_dir / file_name
④ with dest.open("wb") as buffer: copyfileobj(file, buf) # ★ 写入磁盘
⑤ if not dest.is_file(): raise 500
⑥ recipe.assets.append(asset_in)                          # 修改内存对象
⑦ self.service.update_one(slug, recipe)                   # ★ 权限校验在此处（写之后）
   → _pre_update_check → can_update([slug])
   → 失败：抛 PermissionDenied（此路由没有 try/except 包 handle_exceptions，通常会 500）
   → 磁盘上资产文件已经写入！
```

**未授权遗留状态分析**：
- 磁盘上 `assets/{file_name}` 已存在
- DB 的 `recipe_assets` 表无记录，`recipe.assets` 内存追加被回滚
- **有自动清理机制**：下次对该 recipe 执行 `update_one` 或 `patch_one` 时，`check_assets()` 会遍历 `assets/` 下所有文件，凡不在 `recipe.assets[*].file_name` 中的一律 `unlink()` 删除（[recipe_service.py#L150-L156](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L150-L156)）

**安全测试覆盖**：[test_recipe_image_assets.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/tests/integration_tests/user_recipe_tests/test_recipe_image_assets.py)（路径穿越、脚本扩展名拦截、attachment 下载头、nosniff 头验证）

---

## 2. 外链导入时的图片下载流程

从 URL 创建 Recipe 的完整链路：

### 2.1 创建流程总览

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L173-L275)（`/create/url` 及 `/create/url/stream`）

```
POST /api/recipes/create/url  {url: "https://example.com/recipe"}
  → RecipeController._create_recipe_from_web()
    → create_from_html(url, repos, translator)
      ├─ RecipeScraper.scrape(url)
      │   ├─ safe_scrape_html(url)  下载 HTML（模拟 Chrome/Firefox/Safari/Edge TLS 指纹）
      │   └─ 依次尝试各解析策略：
      │       1. RecipeScraperPackage (recipe_scrapers 库解析 schema.org JSON-LD)
      │       2. RecipeScraperOpenAITranscription (yt-dlp 下载视频 + AI 转写)
      │       3. RecipeScraperOpenAI (OpenAI 从 HTML 提取)
      │       4. RecipeScraperOpenGraph (Open Graph 标签兜底)
      │   └─ 返回 Recipe 对象（含 image 字段为 URL 字符串或 URL 列表）
      │
      ├─ 生成 new_recipe.id = uuid4()                          # 预先分配 UUID
      │
      ├─ RecipeDataService(new_recipe.id)                      # 创建磁盘目录
      │   └─ await recipe_data_service.scrape_image(new_recipe.image)
      │       ├─ (URL 解析与下载)
      │       ├─ write_image(bytes, ext)  生成三尺寸 webp
      │       └─ 删除临时下载文件
      │
      ├─ new_recipe.slug = slugify(name)
      └─ new_recipe.image = cache.new_key(4)    # 成功：4位随机字符串作为缓存版本
      └─ (异常捕获) new_recipe.image = "no image"  # 失败：硬编码标记
    │
    → RecipeController._finish_recipe_from_web()
      └─ RecipeService.create_one(recipe)  写入 DB
```

关键入口：[scraper.py create_from_html](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/scraper.py#L26-L85)

### 2.2 各策略中的图片来源

| 策略 | 图片字段来源 |
|------|------------|
| RecipeScraperPackage | schema.org `image` 字段（可能是字符串、列表或对象），通过 [cleaner.clean_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/cleaner.py) 清洗 |
| RecipeScraperOpenAI | 同上（先用 OpenAI 重写 HTML 中的 LD-JSON，再用 recipe_scrapers 解析），或 [find_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/scraper_strategies.py#L364-L390) 从 og:image 或最大 img 标签提取 |
| RecipeScraperOpenAITranscription | yt-dlp 返回的 `thumbnail_url` |
| RecipeScraperOpenGraph | `<meta property="og:image">` 标签 |

### 2.3 列表图片 URL 的选择规则（四层架构逐层说明）

schema.org 的 `image` 字段在数据源中可能是 `str`、`list[str]`、`dict` 三种形式，但这些类型在流经不同层次时被逐步收敛。整个链路分为四层，每层的类型处理方式不同：

#### 第 1 层：请求 Schema —— ScrapeRecipe 只接受 `str`

**定义**：[recipe_scraper.py#L16-L26](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/schema/recipe/recipe_scraper.py#L16-L26)
```python
class ScrapeRecipe(ScrapeRecipeBase):
    url: str   # ★ 仅支持单个字符串 URL，不支持数组
```

前端 API 对应：[recipe.ts#L141-L142](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/lib/api/user/recipes/recipe.ts#L141-L142)
```ts
updateImagebyURL(slug: string, url: string) {
    return this.requests.post(routes.recipesRecipeSlugImage(slug), { url });
}
```

**结论**：HTTP 抓图接口 `POST /{slug}/image` **在协议层面就不支持数组传输**，调用方只能传单个字符串 URL。

#### 第 2 层：HTTP 路由 —— scrape_image_url 接收单值并透传

**路由实现**：[recipe_crud_routes.py#L614-L633](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L614-L633)
```python
@router.post("/{slug}/image")
async def scrape_image_url(self, slug: str, url: ScrapeRecipe):   # url: ScrapeRecipe → url.url: str
    recipe = self.mixins.get_one(slug)
    data_service = RecipeDataService(recipe.id)
    ...
    await data_service.scrape_image(url.url)   # ★ 传入的是单个字符串
```

**结论**：HTTP 路由层透传单个字符串给内部函数，`scrape_image` 的 `list[str]` 分支在这条路径上**永远不会被触发**。

#### 第 3 层：URL 导入内部流程 —— 列表取第一张

在 `create_from_html`（URL 创建 Recipe）路径中，在调用 `scrape_image` 之前有一段预处理：

[scraper.py#L64-L66](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/scraper.py#L64-L66)
```python
if new_recipe.image:
    if isinstance(new_recipe.image, list):
        new_recipe.image = new_recipe.image[0]   # ★ 直接取索引 0，不比较大小
    await recipe_data_service.scrape_image(new_recipe.image)
```

**结论**：
- scraper 策略从 schema.org 解析出的多分辨率 URL 列表，在导入时被**直接取第一张**（schema.org 通常按「小 → 大」排序，因此实际被选中的往往是**分辨率最低**的那张）
- 多分辨率信息在此被丢弃，`scrape_image` 收到的只是单个字符串 URL

#### 第 4 层：内部函数 scrape_image —— list 分支按 Content-Length 选最大

虽然 HTTP 层无法触发，但 `RecipeDataService.scrape_image` 的函数签名**理论上**支持 `list[str]`：

[recipe_data_service.py#L119-L139](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L119-L139)
```python
async def scrape_image(self, image_url: str | dict[str, str] | list[str]) -> None:
    if isinstance(image_url, str):
        image_url_str = image_url
    elif isinstance(image_url, list):                          # ★ 内部 list 分支
        image_url_str, _ = await largest_content_len(image_url) # 并发 HEAD 选 Content-Length 最大
    elif isinstance(image_url, dict):
        image_url_str = image_url.get("url", "")
```

`largest_content_len` 实现（[recipe_data_service.py#L28-L46](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L28-L46)）：
- Semaphore(10) 并发 HEAD 各 URL
- `ignore_exceptions=True` 过滤掉抛异常的响应（超时、DNS、TLS 失败等）
- 取 `Content-Length` 响应头数值最大的那个 URL
- 若所有 HEAD 均失败 → `largest_url = ""` → 回到 scrape_image 抛 `ValueError`（进入 except 分支，DB 写入 `"no image"`）

**结论**：按 Content-Length 选最大图片是**内部函数接到 list 时的兜底行为**，在当前代码中**没有任何外部调用路径能触发它**——HTTP 层只传 str，导入层在传参前已把 list 截断为 str。

#### 四层汇总

| 层级 | 接受类型 | list 处理策略 | 最大分辨率是否被利用 |
|------|---------|--------------|-------------------|
| ScrapeRecipe Schema / HTTP 接口 | 仅 `str` | N/A（协议层不支持数组） | N/A |
| URL 创建导入（`create_from_html`） | `str \| list \| dict` → 预处理后只传 `str` | 取 `[0]`（通常最小） | ❌ 否 |
| 内部函数 `scrape_image` | `str \| list[str] \| dict` | 选 Content-Length 最大 | ✅ 是（但无调用方） |

---

### 2.4 URL 导入中各失败场景的精确状态

`create_from_html` 函数的完整控制流（[scraper.py#L26-L85](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/scraper.py#L26-L85)）分为三段：

```python
# ── 第一段：HTML 抓取与 Recipe 解析（图片 try/except 之外） ──
extracted_url = regex_search(...)           # L47-50：URL 不合法 → raise HTTP 400 → 中断整个函数
new_recipe, extras = await scraper.scrape(...)  # L52：抓取/解析失败 → 向上抛异常 → 中断
if not new_recipe:                              # L54-55：解析为空 → raise HTTP 400 → 中断
new_recipe.id = uuid4()
recipe_data_service = RecipeDataService(new_recipe.id)

# ── 第二段：图片处理分支（try/except 包裹，仅此段保证不中断） ──
try:                                              # L63
    if new_recipe.image:
        if isinstance(new_recipe.image, list):
            new_recipe.image = new_recipe.image[0]
        await recipe_data_service.scrape_image(new_recipe.image)
    if new_recipe.name is None:
        new_recipe.name = "Untitled"
    new_recipe.slug = slugify(new_recipe.name)
    new_recipe.image = cache.new_key(4)           # L76：无异常才走这里
except Exception as e:                            # L77
    new_recipe.image = "no image"                 # L79：抛异常才走这里

# ── 第三段：名称兜底（图片 try/except 之外） ──
if new_recipe.name is None or new_recipe.name == "":   # L81-83：不会抛异常
    new_recipe.name = f"No Recipe Name Found - {uuid4()!s}"
    new_recipe.slug = slugify(new_recipe.name)

return new_recipe, extras
```

**结论限定范围**：「图片处理不会中断」**仅适用于第二段（L63–L79 的 try/except 块内部）**。第一段（L46–L55）在进入图片处理之前仍可能因 URL 非法、scraper 返回空等原因抛 `HTTPException` 中断整个函数，此时 Recipe 不会被创建，也不会在磁盘上留下任何目录。

---

以下失败场景均发生在**第二段图片处理分支**（`try/except` 范围内）。此段内部分两条路径：

| 路径 | 触发条件 | `new_recipe.image` 赋值 |
|------|---------|----------------------|
| **静默 `return None`（不进 except）** | 无 image 字段、网络故障、非 200 状态码 | `cache.new_key(4)`（合法缓存键） |
| **抛异常（进 except）** | Content-Type 非 image、PIL 解码失败、URL 解析失败、后续逻辑异常 | `"no image"`（硬编码标记） |

详细场景表：

| # | 失败场景 | scrape_image 行为 | 最终 `recipes.image` 值 | 缓存键 | 磁盘状态 | 后续是否可自动清理 |
|---|---------|------------------|----------------------|--------|---------|-----------------|
| **A** | 新 recipe 本身无 image 字段（`new_recipe.image` 为 None/空/False） | `if new_recipe.image:` 为 False，**scrape_image 根本不被调用** | `cache.new_key(4)`（4 位随机串） | ✅ 合法缓存键 | 仅空目录 `{uuid}/images/`、`{uuid}/assets/`（RecipeDataService `__init__` 创建） | ❌ 无自动；`clean/recipe-folders` 不删（合法 UUID）；`clean/images` 也不删（无非 webp 文件） |
| **B** | 网络层故障（DNS 解析失败、连接超时、TLS 握手失败、连接被重置等） | [recipe_data_service.py#L151-L154](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L151-L154)：`except Exception: return None` **（不抛异常）** | `cache.new_key(4)`（4 位随机串） | ✅ 合法缓存键 | 仅空目录（GET 请求失败，不写文件） | ❌ 同上 |
| **C** | 远程返回非 200 状态码（403、404、500、429 等） | [recipe_data_service.py#L156-L159](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L156-L159)：`if r.status_code != 200: return None` **（不抛异常）** | `cache.new_key(4)`（4 位随机串） | ✅ 合法缓存键 | 仅空目录（状态码不对，不写文件） | ❌ 同上 |
| **D** | 200 但 Content-Type 不含 `"image"`（如 text/html、application/pdf 等） | [recipe_data_service.py#L161-L165](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L161-L165)：`raise NotAnImageError` **（抛异常）** | `"no image"`（硬编码字符串） | ❌ 非缓存键 | 仅空目录（异常在 write_image 之前抛出） | ❌ 同上 |
| **E** | 图片下载成功但 PIL 解码失败（损坏文件、HEIC 缺少依赖、截断的 JPEG、零字节等） | [recipe_data_service.py#L168](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L168)：`self.write_image(...)` 内部 minify 抛异常 → [write_image#L102-L107](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L102-L107)：`image_path.unlink()` 后 re-raise **（抛异常）** | `"no image"`（硬编码字符串） | ❌ 非缓存键 | write_image 已 `unlink(missing_ok=True)` 删除了临时 `original.{ext}`；若 minify 生成了部分 webp 后才失败（如生成 original.webp 成功但生成 tiny 时磁盘满），已生成的 webp 会**残留在磁盘上** | ❌ 残留 webp 无法自动清理 |
| **F** | image URL 解析失败（dict 无 `url` 键、dict 的 `url` 值为空串等） | [recipe_data_service.py#L133-L139](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L133-L139)：`image_url_str = image_url.get("url", "")` 为空 → `raise ValueError` **（抛异常）**。**注**：在 URL 导入路径中，list 已被提前取 `[0]`，因此 list 分支的 HEAD 全失败不会在此触发；仅内部函数直接接 list 时才会走那条路径（当前无调用方） | `"no image"`（硬编码字符串） | ❌ 非缓存键 | 仅空目录（异常在 write_image 之前抛出） | ❌ 同上 |
| **G** | 图片下载 + minify 全部成功，但后续 slugify 或 name 为空兜底逻辑抛异常（极少） | scrape_image 正常返回；异常在 try 块后续逻辑触发 | `"no image"`（硬编码字符串） | ❌ 非缓存键 | 三尺寸 webp 完整存在于 `{uuid}/images/` | ❌ 永久孤儿 webp，无自动清理 |
| **H** | ✅ 图片处理分支全流程成功 | scrape_image 正常返回 | `cache.new_key(4)`（4 位随机串） | ✅ 合法缓存键 | `{uuid}/images/` 下三尺寸 webp 齐全 | N/A |

**前端表现差异**：
- **A/B/C 场景**（有合法缓存键）：前端请求 `/api/media/recipes/{id}/images/tiny-original.webp?version={4位随机串}` → 文件不存在 → 404 → 前端 fallback 占位图
- **D/E/F/G 场景**（`"no image"`）：前端请求 `...?version=no+image` → 同样 404 → 同样 fallback 占位图
- 两种路径在用户视觉上无差异，但 DB 中存储的字段值不同

**第二段范围结论**：在图片处理分支（L63–L79 try/except 内），所有异常都会被捕获并降级为 `"no image"` 或静默赋值缓存键，**函数不会中断**，仍然返回 Recipe 对象给调用方继续写入 DB。

---

## 3. 裁剪与缓存逻辑

### 3.1 PillowMinifier 三种尺寸

核心代码：[minify.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/pkgs/img/minify.py#L156-L206)

| 输出文件 | 尺寸规则 | 质量 | 用途 |
|---------|---------|------|------|
| `original.webp` | thumbnail(2048, 2048)，保持宽高比 | 80 | 详情页大图 |
| `min-original.webp` | thumbnail(1024, 1024)，保持宽高比 | 80 | 中等预览 |
| `tiny-original.webp` | **crop_center(300, 300, high_res=True)** → 600x600 居中裁剪 | 80 | 列表卡片缩略图 |

**crop_center 算法**（[minify.py#L121-L154](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/pkgs/img/minify.py#L121-L154)）：
1. Retina 加倍 → 目标 size * 2
2. 按较小边等比缩放（LANCZOS 重采样）
3. 居中裁剪到目标尺寸
4. 原图小于目标时不做放大

### 3.2 格式转换与 EXIF 处理

- 统一转为 WEBP（RGB/RGBA 模式）
- `ImageOps.exif_transpose()` 处理手机拍照方向
- 支持输入格式：`.jpg, .jpeg, .png, .webp, .heic, .avif`（含 HEIC 通过 pillow-heif）

### 3.3 数据库 image 字段作为缓存版本号

数据库 `recipes.image` 列**不存图片数据**，仅存一个随机字符串作为缓存 bust 键，共 4 种可能取值：

| 取值 | 来源 | 含义 |
|------|------|------|
| `cache.new_key(4)`（4 位随机字母数字，如 `"a7f2"`） | 上传/URL 抓取**成功**；或导入中无 image 字段/网络失败/非 200（scrape_image `return None`，不抛异常） | ✅ 合法缓存键，前端据此拼 URL |
| `randint(0, 255)`（整数） | 老代码路径（如 RepositoryRecipes.update_image） | ✅ 合法缓存键 |
| `None` | DELETE /{slug}/image 显式删除 | ❌ 无图 |
| `"no image"`（硬编码字符串） | 导入中 Content-Type 非 image / PIL 失败 / URL 解析失败（scrape_image 抛异常） | ❌ 非缓存键，仅作标记 |

前端构造 URL 时将此值作为 query param（[static-routes.ts](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/composables/api/static-routes.ts#L10-L24)）：
```
/api/media/recipes/{recipe_id}/images/original.webp?rnd={key}&version={image}
```

**注意**：`cache.new_key(4)` **不等于**图片真实存在——A/B/C 三种失败场景（无图字段/网络故障/非 200）也会写入合法缓存键，但磁盘上实际没有 webp 文件，返回 404 由前端 fallback。

### 3.4 purge 机制清理源文件

`RecipeDataService` 使用 `PillowMinifier(purge=True)`：
- 三尺寸 webp 全部生成成功后，删除 images 目录下所有 `.webp` 以外的文件（[minify.py#L57-L63](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/pkgs/img/minify.py#L57-L63)）
- 即下载的原始 jpg/png/heic 等会被清理，只保留 webp

---

## 4. 列表与详情展示

### 4.1 媒体读取路由（无需登录，公开访问）

**文件**：[media_recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/media/media_recipe.py)

| 路由 | 说明 |
|------|------|
| `GET /api/media/recipes/{recipe_id}/images/{file_name}` | 返回三尺寸图片之一（original.webp / min-original.webp / tiny-original.webp），Content-Type: image/webp |
| `GET /api/media/recipes/{recipe_id}/images/timeline/{event_id}/{file_name}` | 时间线事件图片 |
| `GET /api/media/recipes/{recipe_id}/assets/{file_name}` | 返回资产文件，强制 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`，并校验路径不越界 |

**注意**：media 路由**不做权限校验**。任何知道 recipe_id 的人都可以访问图片。Docker 部署下此路由由 nginx 直接代理静态文件，不经过 Python。

### 4.2 前端渲染

列表页卡片使用 tiny 图：[RecipeCardImage.vue](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/components/Domain/Recipe/RecipeCardImage.vue)
```vue
<v-img :src="recipeTinyImage(recipeId, imageVersion)" @error="fallBackImage = true" />
```

详情页 header 使用 original / min 图：[RecipePageHeader.vue](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageHeader.vue)、[RecipePageInfoCardImage.vue](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/components/Domain/Recipe/RecipePage/RecipePageParts/RecipePageInfoCardImage.vue)

### 4.3 公开食谱权限过滤

Explore（公开浏览）接口在 [controller_public_recipes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/explore/controller_public_recipes.py)：

```python
public_filter = "(household.preferences.privateHousehold = FALSE AND settings.public = TRUE)"
```

共享食谱通过 token 访问：[shared_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/shared_routes.py#L22-L39)，token 过期自动删除。

---

## 5. 旧图清理机制

### 5.1 Recipe 删除时级联清理

**RecipeService.delete_many**（[recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L573-L588)）：

```
先 can_delete 校验（仅 admin 或所有者）
  → DB 删除 recipe 记录（级联删除 recipe_assets 行）
  → 对每个 recipe 调用 delete_assets(recipe)
    → shutil.rmtree(recipe.directory, ignore_errors=True)
      → 完整删除 {RECIPE_DATA_DIR}/{recipe_id}/ 整个目录
      → 包含 images/ 和 assets/ 全部文件
```

DB 层：`RecipeModel.assets` 配置 `cascade="all, delete-orphan"`，删除 recipe 行时自动清理 recipe_assets 表记录（[recipe.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/db/models/recipe/recipe.py#L96)）。

### 5.2 Recipe 更新时清理无效资产

**RecipeService.check_assets**（[recipe_service.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L136-L156)）：

```
每次 update / patch recipe 后执行：
  1. 若 slug 变更 → copytree(old_dir, new_dir) 迁移整个目录（遗留 slug 目录兼容代码）
  2. 遍历 assets/ 目录下所有文件
     → 若文件名不在 recipe.assets[x].file_name 列表中 → unlink() 删除
```

这保证了 DB 中已解除关联的资产文件不会残留在磁盘上。**但 images/ 目录不做类似校验**。

### 5.3 图片显式删除

**DELETE /api/recipes/{slug}/image** → [RecipeService.delete_recipe_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L540-L549)：
```python
① can_update() 校验  （先鉴权后操作，安全）
② data_service.delete_image()  # 删除 images/ 下三种 webp 文件
③ self.group_recipes.delete_image(slug)  # DB recipes.image = None
```

### 5.4 Admin 维护清理工具（清理能力边界）

**POST /api/admin/maintenance/clean/images**（[admin_maintenance.py#L16-L35](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/admin/admin_maintenance.py#L16-L35)）：
- 遍历所有 recipe 目录下 images/
- **只删除后缀 != `.webp` 的文件**（即 purge 未清理干净的原始 jpg/png/heic 等）
- 对孤儿 webp 文件无效

**POST /api/admin/maintenance/clean/recipe-folders**（[admin_maintenance.py#L38-L52](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/admin/admin_maintenance.py#L38-L52)）：

```python
for recipe_dir in root_dir.iterdir():
    try:
        uuid.UUID(recipe_dir.name)
        continue                # ★ 合法 UUID 直接跳过，不查 DB
    except ValueError:
        shutil.rmtree(recipe_dir)
```

- **仅删除目录名不是合法 UUID 的目录**（旧版本按 slug 建的遗留目录）
- **不与数据库 recipes 表做 JOIN 比对**，因此：
  - 旧 slug 格式遗留目录 ✅ 可清
  - 合法 UUID 但 DB 中不存在对应 recipe（孤儿目录） ❌ **无法被清理**
  - 合法 UUID 且 DB 中存在对应 recipe ✅ 正确跳过

**POST /api/admin/maintenance/clean/temp**：
- 暴力删除并重建 TEMP_DIR

### 5.5 重新处理旧图脚本

[reprocess_images.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/scripts/reprocess_images.py)：
- 检测 tiny-original.webp 是否为旧版 300x300（新版为 600x600 retina）
- 需要时删除 min/tiny 并从 original.webp 重新 minify
- 支持多线程并发处理

---

## 6. 权限访问控制

### 6.1 can_update 精确触发条件

**SQL 逻辑**（[recipe_service.py#L87-L131](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L87-L131)），按优先级判断：

| # | 条件 | can_update 结果 | 说明 |
|---|------|----------------|------|
| 1 | `r.user_id == :user_id` | **1（允许）** | Recipe 所有者 |
| 2 | 不满足 #1 **且** `COALESCE(rs.locked, TRUE) = TRUE` | **0（拒绝）** | 非所有者 + recipe 被锁定（NULL 默认视为锁定） |
| 3 | 不满足 #1、#2 **且** `u.household_id != :household_id AND COALESCE(hp.lock_recipe_edits_from_other_households, TRUE) = TRUE` | **0（拒绝）** | 非所有者 + 跨 Household + Household 策略禁止外组编辑 |
| 4 | 不满足以上全部 | **1（允许）** | 非所有者 + 未锁定 + 同 Household（或跨 Household 但其策略允许） |

**补充**：
- 判断范围是全部传入的 slugs，只要有一条不可更新，整体返回 0
- 查询加了 `r.group_id = :group_id` 过滤，跨 Group 的 recipe 根本看不到
- `can_delete` 更严格：admin 或 `r.user_id == :user_id`（协作编辑不允许删除）

### 6.2 各写操作的鉴权位置与遗留风险对比

| 操作 | HTTP | 鉴权位置 | 鉴权时机 | 鉴权失败磁盘遗留 |
|------|------|---------|---------|----------------|
| 本地上传图片 | PUT /{slug}/image | `RecipeService.update_recipe_image` L532 | **写之前** ✅ | 无 |
| 外链抓图 | POST /{slug}/image | `RecipeService.update_one` → `_pre_update_check`（仅抓图成功/静默失败路径触发；抛异常路径完全跳过） | **写之后** ❌ | 成功+权限非法：三种 webp 孤儿；抛异常：空目录或部分 webp 孤儿；全部无自动清理 |
| 删除图片 | DELETE /{slug}/image | `RecipeService.delete_recipe_image` L542 | **写之前** ✅ | 无 |
| 上传资产 | POST /{slug}/assets | `RecipeService.update_one` → `_pre_update_check` | **写之后** ❌ | assets/ 下文件名，下次 update/patch 时 check_assets 会清 |
| 更新 recipe 本体 | PATCH /{slug} | `RecipeService.patch_one` → `_pre_update_check` | 写 DB 之前 | 无（不碰磁盘） |
| 删除 recipe | DELETE /{slug} | `RecipeService.delete_many` → `can_delete` | 写 DB 之前 | 无 |

### 6.3 RecipeSettings 对前端展示的影响

[recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/schema/recipe/recipe_settings.py)：

| 字段 | 作用 |
|------|------|
| `public` | 公开浏览时（Explore 页面、未登录用户）只有 public=True 且所在 Household 非 private 才可见 |
| `show_nutrition` | 控制营养信息展示 |
| `show_assets` | 控制资产列表展示（前端根据此字段决定是否渲染资产区域） |
| `locked` | 锁定后非所有者不可编辑（见 can_update 条件 #2，NULL 视为 TRUE=锁定） |
| `disable_comments` | 控制评论功能 |

**注意**：`show_assets` 仅控制 UI 显示，资产文件仍可通过 `/api/media/recipes/{id}/assets/{name}` 直接下载（只要知道文件名和 recipe_id）。

### 6.4 读权限设计（obscurity 模式）

Media 路由（`/api/media/recipes/*`）**没有任何认证/授权**。原因：
- 生产环境 Docker 部署中，此路径由 nginx 直接 serve 静态文件，请求不经过 Python
- 通过 obscurity 保护：需要知道 recipe 的 UUID（而非 slug），UUID 不可枚举
- `recipe_id` 是 UUIDv4，2^122 熵，无法暴力枚举

---

## 7. 导入失败时的资源状态

### 7.1 单 URL 导入（create_from_html）中的图片处理分支

详见 **2.4 节 A–H 表格**。汇总：

- 两类失败路径（均发生在 L63–L79 的图片处理 try/except 分支内）：

| 路径 | 触发场景 | `recipes.image` 值 | 占比（估计） |
|------|---------|------------------|-----------|
| **静默 `return None`（不进 except）** | 无 image 字段、网络故障、非 200 状态码 | `cache.new_key(4)`（合法缓存键） | 大多数图片抓取失败 |
| **抛异常（进 except）** | Content-Type 非 image、PIL 解码失败、URL 解析失败、后续逻辑异常 | `"no image"` | 少数情况（目标站点返回 200 但内容不对） |

- 在图片处理 try/except 分支范围内：无论哪条路径，Recipe **仍会创建**并写入 DB，磁盘上至少存在空目录 `{uuid}/images/` 和 `{uuid}/assets/`
- 注意：**图片处理分支之外的代码（L46–L55 的 URL 校验和 scraper 解析）可能抛 HTTPException 中断整个函数**，此时 Recipe 不会被创建，磁盘也不会留下任何目录
- PIL 处理在生成部分 webp 后失败（如 original.webp 生成成功但 tiny-original.webp 因磁盘满失败），已生成的 webp 无法被自动清理
- 两种路径在前端表现一致：图片 URL 均返回 404，由 fallback 占位图兜底

### 7.2 write_image / minify 失败回滚

[recipe_data_service.py#L102-L107](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L102-L107)：

```python
try:
    self.minifier.minify(image_path)    # 生成三尺寸 webp，期间可能抛 PIL 异常
except Exception:
    image_path.unlink(missing_ok=True)  # ★ 只删除原始 original.{ext}
    raise                               # ★ 不删除已生成的 webp
```

**回滚不完整**：若 `original.webp` 生成成功但生成 `tiny-original.webp` 时磁盘满，`original.webp` 和 `min-original.webp` 已在磁盘上，不会被清理。

### 7.3 批量导入（Bulk URL Import）DB 写入失败的孤儿目录

[recipe_bulk_scraper.py#L82-L127](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/recipe_bulk_scraper.py#L82-L127)：

```
并发（Semaphore 3）执行每个 URL:
  _do(url):
    ① recipe, _ = await create_from_html(url, ...)
       → 成功：recipe.id 是一个全新 UUID，{uuid}/ 目录已创建，可能有图片
       → 失败：记录错误 entry，recipe=None，continue
  ② 对有 recipe 的，调用 self.service.create_one(recipe) 写入 DB
     → 成功：记录 success entry
     → 失败（如 slug 冲突、DB 连接中断等）：记录 error entry，recipe 内存对象丢弃
```

**孤儿目录产生条件**：步骤 ① create_from_html 成功（创建了 `{uuid}/` 目录）但步骤 ② DB `create_one` 抛异常。

**孤儿目录特征**：
- 目录名是合法 UUIDv4
- DB `recipes` 表无对应 `id = {uuid}` 的行
- 目录下可能为空、可能有三种 webp 图片、可能有临时下载文件

**维护接口能否清除？—— 不能**：
- `clean/recipe-folders` 只删除目录名不是合法 UUID 的目录（见 5.4 节），孤儿目录名是合法 UUID，会被 `continue` 跳过
- `clean/images` 只删非 webp 文件
- 唯一方法是管理员手工遍历 `RECIPE_DATA_DIR` 与 DB recipes.id 做差集后手动删除，或自行开发额外脚本

### 7.4 Recipe 复制失败的资源状态

[RecipeService.duplicate_one](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L358-L412)：

```python
① new_recipe = _recipe_creation_factory(...)   # DB 先创建新 recipe（带新 UUID）
② try:
    copytree(old_service.dir_data, new_service.dir_data, dirs_exist_ok=True)
  except Exception as e:
    self.logger.error(...)   # ★ 仅记日志，不抛异常，不回滚 DB
```

- 复制失败不会回滚已创建的 DB 记录，新 recipe 在 DB 中存在但无图片/资产
- 复制中断（复制了部分文件后失败）可能导致不完整的 assets/ 目录内容

---

## 8. 其他创建方式中的图片处理

### 8.1 从图片创建 Recipe（AI OCR）

**POST /api/recipes/create/image**（[recipe_crud_routes.py#L309-L335](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L309-L335)）：
1. 上传图片保存到临时目录
2. OpenAI 多模态识别提取 Recipe 字段
3. cleaner.clean() 清洗
4. `service.create_one(recipe_data)` 入库
5. 第一张图作为 recipe 主图：`data_service.write_image(local_images[0].read_bytes(), "webp")`

### 8.2 从 Zip 导入

**POST /api/recipes/create/zip** → [RecipeService.create_from_zip](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L302-L333)：
1. 解压 zip，读 .json（recipe 元数据）和 .webp（图片）
2. `service.create_one(Recipe(...))` 入库
3. `data_service.write_image(recipe_image_bytes, "webp")` 写图片并裁剪

### 8.3 时间线事件图片

RecipeTimelineEvent 也支持图片，路径：`images/timeline/{event_id}/`
- 读写路由见 [media_recipe.py#L33-L48](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/media/media_recipe.py#L33-L48)
- 前端 API 见 [recipe.ts updateTimelineEventImage](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/lib/api/user/recipes/recipe.ts#L276-L282)
- 同样由 PillowMinifier 生成三尺寸 webp
