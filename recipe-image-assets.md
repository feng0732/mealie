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

```
前端 RecipeImageUploadBtn.vue
  → RecipeAPI.updateImage(slug, fileObject)
    → PUT /api/recipes/{slug}/image  (FormData: image=bytes, extension=jpg/png/...)
      → RecipeController.update_recipe_image()
        → RecipeService.update_recipe_image()
          → 权限校验 can_update()
          → RecipeDataService(recipe.id).write_image(image_bytes, extension)
            → 写入 original.{ext}
            → PillowMinifier.minify() 生成 original.webp / min-original.webp / tiny-original.webp
          → RepositoryRecipes.update_image(slug)
            → DB recipes.image = randint(0, 255)  （仅作为缓存版本号）
```

关键实现：
- [RecipeService.update_recipe_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L530-L538)
- [RecipeDataService.write_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L85-L109)
- [RepositoryRecipes.update_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/repos/repository_recipes.py#L155-L160)

前端组件：[RecipeImageUploadBtn.vue](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/components/Domain/Recipe/RecipeImageUploadBtn.vue)
前端 API：[recipe.ts updateImage](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/lib/api/user/recipes/recipe.ts#L133-L139)

### 1.2 外链 URL 抓取图片（POST /api/recipes/{slug}/image）

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L614-L634)

```
前端输入 URL → RecipeAPI.updateImagebyURL(slug, url)
  → POST /api/recipes/{slug}/image  (Body: {url: "https://..."})
    → RecipeController.scrape_image_url()
      → RecipeDataService.scrape_image(url)
        → URL 类型解析（str / list[str] / dict）
        → 若为 list，并发 HEAD 请求选 Content-Length 最大的图片
        → httpx GET 下载图片字节（AsyncSafeTransport 模拟浏览器 TLS）
        → 校验 Content-Type 包含 "image"，否则抛 NotAnImageError
        → write_image(bytes, extension) 写入并裁剪
      → DB: recipe.image = cache.new_key(4)
      → RecipeService.update_one() 保存
```

关键实现：
- [RecipeDataService.scrape_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L119-L169)
- [largest_content_len](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L28-L46)（多分辨率列表选最大图）

### 1.3 资产文件上传（POST /api/recipes/{slug}/assets）

**入口路由**：[recipe_crud_routes.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/recipe/recipe_crud_routes.py#L653-L699)

```
前端 FormData: name, icon, extension, file
  → 扩展名白名单校验: {pdf, jpg, jpeg, png, gif, webp, bmp, avif, txt, md, csv, json}
    → 黑名单（被拦截）: html, svg, js, htm, xhtml  （防止 XSS）
  → 文件名 = slugify(name) + "." + extension
  → 路径安全校验: dest.absolute().parent == recipe.asset_dir  （防止路径穿越）
  → 写入文件到 assets/{file_name}
  → recipe.assets 追加 RecipeAsset(name, icon, file_name)
  → RecipeService.update_one() 保存到 DB
```

安全参考测试：[test_recipe_image_assets.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/tests/integration_tests/user_recipe_tests/test_recipe_image_assets.py)（包含路径穿越、脚本扩展名拦截、attachment 下载头验证）

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
      ├─ 生成 new_recipe.id = uuid4()
      │
      ├─ RecipeDataService(new_recipe.id)
      │   └─ scrape_image(new_recipe.image)
      │       ├─ 下载外链图片到 images/{recipe_id}.{ext} （临时文件）
      │       ├─ write_image(bytes, ext)  生成三尺寸 webp
      │       └─ 删除临时下载文件
      │
      ├─ new_recipe.slug = slugify(name)
      └─ new_recipe.image = cache.new_key(4)  （4位随机字符串作为缓存版本）
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

数据库 `recipes.image` 列**不存图片数据**，仅存一个随机字符串作为缓存 bust 键：

- 上传/URL 抓取成功时：`cache.new_key(4)` → 4 位随机字母数字（[cache_key.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/pkgs/cache/cache_key.py)）
- 老代码路径：`randint(0, 255)` → 整数
- 删除图片时：`entry.image = None`

前端构造 URL 时将此值作为 query param：
```
/api/media/recipes/{recipe_id}/images/original.webp?rnd={key}&version={image}
```

前端实现：[static-routes.ts](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/frontend/app/composables/api/static-routes.ts#L10-L24)

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
DB 删除 recipe 记录后
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
  1. 若 slug 变更 → copytree(old_dir, new_dir) 迁移整个目录
  2. 遍历 assets/ 目录下所有文件
     → 若文件名不在 recipe.assets[x].file_name 列表中 → unlink() 删除
```

这保证了 DB 中已解除关联的资产文件不会残留在磁盘上。

### 5.3 图片显式删除

**DELETE /api/recipes/{slug}/image** → [RecipeService.delete_recipe_image](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L540-L549)：
```python
data_service.delete_image()  # 删除 images/ 下三种 webp 文件
self.group_recipes.delete_image(slug)  # DB recipes.image = None
```

### 5.4 Admin 维护清理工具

**POST /api/admin/maintenance/clean/images**（[admin_maintenance.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/admin/admin_maintenance.py#L16-L35)）：
- 遍历所有 recipe 目录下 images/
- 删除所有后缀 != `.webp` 的文件（即 purge 未清理干净的原始 jpg/png 等）

**POST /api/admin/maintenance/clean/recipe-folders**（[admin_maintenance.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/routes/admin/admin_maintenance.py#L38-L52)）：
- 删除 RECIPE_DATA_DIR 下文件夹名不是合法 UUID 的目录（旧版本按 slug 建的遗留目录）

### 5.5 重新处理旧图脚本

[reprocess_images.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/scripts/reprocess_images.py)：
- 检测 tiny-original.webp 是否为旧版 300x300（新版为 600x600 retina）
- 需要时删除 min/tiny 并从 original.webp 重新 minify
- 支持多线程并发处理

---

## 6. 权限访问控制

### 6.1 Recipe 图片/资产的写权限

所有修改操作（上传图片、URL 抓取、删除图片、上传资产）都在 RecipeService 层通过 `can_update` 校验：

```python
# recipe_service.py can_update()
SELECT CASE
  WHEN r.user_id = :user_id THEN 1                          -- 所有者
  WHEN COALESCE(rs.locked, TRUE) = TRUE THEN 0              -- 被锁定则不可改
  WHEN u.household_id != :household_id                      -- 跨 Household
       AND COALESCE(hp.lock_recipe_edits_from_other_households, TRUE) = TRUE THEN 0
  ELSE 1
END
```

删除权限 `can_delete` 更严格：仅 admin 或 recipe.user_id == 当前用户。

### 6.2 RecipeSettings 对前端展示的影响

[recipe_settings.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/schema/recipe/recipe_settings.py)：

| 字段 | 作用 |
|------|------|
| `public` | 公开浏览时（Explore 页面、未登录用户）只有 public=True 且所在 Household 非 private 才可见 |
| `show_nutrition` | 控制营养信息展示 |
| `show_assets` | 控制资产列表展示（前端根据此字段决定是否渲染资产区域） |
| `locked` | 锁定后非所有者不可编辑（见 can_update 逻辑） |
| `disable_comments` | 控制评论功能 |

**注意**：`show_assets` 仅控制 UI 显示，资产文件仍可通过 `/api/media/recipes/{id}/assets/{name}` 直接下载（只要知道文件名和 recipe_id）。

### 6.3 读权限漏洞（特性）

Media 路由（`/api/media/recipes/*`）**没有任何认证/授权**。原因：
- 生产环境 Docker 部署中，此路径由 nginx 直接 serve 静态文件，请求不经过 Python
- 通过 obscurity 保护：需要知道 recipe 的 UUID（而非 slug），UUID 不可枚举

---

## 7. 导入失败时的资源状态

### 7.1 URL 抓取过程中图片下载失败

在 [create_from_html](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/scraper.py#L63-L79) 中：

```python
try:
    if new_recipe.image:
        await recipe_data_service.scrape_image(new_recipe.image)
    new_recipe.image = cache.new_key(4)
except Exception as e:
    recipe_data_service.logger.exception(f"Error Scraping Image: {e}")
    new_recipe.image = "no image"   # 标记失败，不中断导入
```

- 图片下载异常（网络失败、非图片 Content-Type、PIL 解码失败等）**被吞掉**，仅记录日志
- Recipe 仍然会被创建并保存到 DB，只是 `recipes.image = "no image"`
- 磁盘状态：`RecipeDataService.scrape_image` 内部 `write_image` 失败时会 `image_path.unlink(missing_ok=True)` 回滚，不会残留半成品

### 7.2 write_image / minify 失败回滚

[recipe_data_service.py#L102-L107](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_data_service.py#L102-L107)：

```python
try:
    self.minifier.minify(image_path)
except Exception:
    image_path.unlink(missing_ok=True)   # 删除不完整的 original.{ext}
    raise                                 # 向上抛出
```

若 minify 中 PIL 解码失败、磁盘写满等，原始上传文件会被删除，不会残留在 images/ 目录。

### 7.3 批量导入（Bulk URL Import）失败

[recipe_bulk_scraper.py](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/scraper/recipe_bulk_scraper.py#L82-L127)：

```
并发（Semaphore 3）执行每个 URL:
  → create_from_html() 失败 → 记录 ReportEntryCreate(success=False)，跳过
  → create_from_html() 成功但 DB create_one() 失败 → 记录错误，跳过
  → 全部成功 → ReportEntryCreate(success=True)
```

- 单个 URL 失败不会中断整个批量任务
- Report 表中记录每条的成败详情，最终 report.status 为 success / failure / partial
- 因 create_from_html 会预创建 recipe_id 目录并写图片，若后续 DB create_one 失败：**磁盘上可能残留孤立的 {recipe_id}/ 目录**，需要通过 admin maintenance 的 clean/recipe-folders 来清理（若 recipe_id 仍在 DB 则不会被清，产生孤儿数据）

### 7.4 Recipe 复制失败的资源状态

[RecipeService.duplicate_one](file:///d:/fz/0601/solo-dogfeeding/code/76-mealie/mealie/services/recipe/recipe_service.py#L358-L412)：

```python
# DB 先创建新 recipe（带新 UUID）
# 再 copytree 旧目录到新目录，失败仅记日志不抛出
try:
    copytree(old_service.dir_data, new_service.dir_data, dirs_exist_ok=True)
except Exception as e:
    self.logger.error(f"Failed to copy assets from ...")
```

- 复制失败不会回滚已创建的 DB 记录，新 recipe 在 DB 中存在但无图片/资产

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
