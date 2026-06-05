# API Schema 与前端消费契约边界

## 一、整体架构概述

### 1.1 技术栈

| 层级 | 技术栈 | 核心文件 |
|------|--------|----------|
| 后端 Schema | Python + Pydantic v2 + FastAPI | [mealie/schema/](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema) |
| 后端路由 | FastAPI + 类视图控制器 | [mealie/routes/](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes) |
| 前端类型 | TypeScript（自动生成） | [frontend/app/lib/api/types/](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types) |
| 前端客户端 | Axios + 分层 API 类 | [frontend/app/lib/api/](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api) |
| 代码生成 | pydantic2ts + 自定义脚本 | [dev/code-generation/gen_ts_types.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/dev/code-generation/gen_ts_types.py) |

### 1.2 契约流转流程

```
后端 Pydantic Schema 定义
        ↓
  gen_ts_types.py 自动生成
        ↓
前端 TypeScript 类型定义
        ↓
  前端 API 客户端类封装
        ↓
    Store 状态管理层
        ↓
  Vue 组件数据消费
```

---

## 二、字段定义规则

### 2.1 基础模型结构

所有 Schema 继承自 `MealieModel`，定义在 [mealie_model.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/_mealie/mealie_model.py#L45-L90)：

```python
class MealieModel(BaseModel):
    model_config = ConfigDict(alias_generator=camelize, populate_by_name=True)
    
    # 自动时区修正验证器
    @model_validator(mode="before")
    def fix_hour_only_tz(cls, data: T) -> T: ...
    
    @model_validator(mode="after")
    def set_tz_info(self) -> Self: ...
```

**关键配置：**
- `alias_generator=camelize`：自动将 Python 蛇形命名转为驼峰命名
- `populate_by_name=True`：允许同时使用字段名和别名进行赋值

### 2.2 字段命名约定

| 场景 | Python 后端 | TypeScript 前端 | 转换方式 |
|------|------------|----------------|----------|
| 字段定义 | `group_id` | `groupId` | 自动 camelize |
| 序列化输出 | `updated_at` | `updatedAt` | 自动别名 |
| 常量/枚举 | `API_VERSION` | `API_VERSION` | 保持不变 |

**特殊字段别名处理**（见 [UpdatedAtField](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/_mealie/mealie_model.py#L20-L37)）：

```python
def UpdatedAtField(*args, **kwargs):
    kwargs["validation_alias"] = AliasChoices("update_at", "updateAt", "updated_at", "updatedAt")
    kwargs["serialization_alias"] = "updatedAt"
    return Field(*args, **kwargs)
```

### 2.3 DTO 分层模式

Schema 按操作语义分层定义（以 [multi_purpose_label.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/labels/multi_purpose_label.py) 为例）：

```python
class MultiPurposeLabelCreate(MealieModel):        # 创建输入
    name: str
    color: str = "#959595"

class MultiPurposeLabelSave(MultiPurposeLabelCreate):  # 数据库保存
    group_id: UUID4

class MultiPurposeLabelUpdate(MultiPurposeLabelSave):  # 更新输入
    id: UUID4

class MultiPurposeLabelSummary(MultiPurposeLabelUpdate):  # 列表返回
    model_config = ConfigDict(from_attributes=True)

class MultiPurposeLabelOut(MultiPurposeLabelUpdate):     # 详情返回
    model_config = ConfigDict(from_attributes=True)

class MultiPurposeLabelPagination(PaginationBase):       # 分页返回
    items: list[MultiPurposeLabelSummary]
```

### 2.4 字段可选性与默认值

```python
# 必填字段（无默认值）
name: str

# 可选字段（允许 null）
description: str | None = None

# 带默认值的字段
color: str = "#959595"

# 自动生成字段
id: UUID4 | None = None
user_id: Annotated[UUID4, Field(default_factory=uuid4)]
```

### 2.5 字段验证器

```python
# 字段级验证
@field_validator("households_with_tool", mode="before")
def convert_households_to_slugs(cls, v):
    if not v:
        return []
    try:
        return [household.slug for household in v]
    except AttributeError:
        return v

# 模型级验证（before：验证前处理）
@model_validator(mode="before")
def fix_hour_only_tz(cls, data: T) -> T: ...

# 模型级验证（after：验证后处理）
@model_validator(mode="after")
def set_tz_info(self) -> Self: ...
```

---

## 三、序列化规则

### 3.1 序列化/反序列化配置

| 配置项 | 作用 | 位置 |
|--------|------|------|
| `alias_generator=camelize` | 自动生成驼峰别名 | [MealieModel](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/_mealie/mealie_model.py#L53) |
| `populate_by_name=True` | 允许通过原始字段名赋值 | [MealieModel](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/_mealie/mealie_model.py#L53) |
| `from_attributes=True` | 支持从 ORM 模型直接转换 | 各 Out/Summary 模型 |
| `coerce_numbers_to_str=True` | 自动将数字转为字符串 | [Nutrition](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/recipe/recipe_nutrition.py#L22) |

### 3.2 特殊字段序列化

**日期时间字段：**
- 数据库存储：UTC 无时区
- 序列化输出：自动添加 UTC 时区标记
- 反序列化：兼容 `+HH` 和 `+HH:MM` 两种时区格式

```python
# 自动修正时区格式（fix_hour_only_tz）
# 自动添加 UTC 时区（set_tz_info）
```

### 3.3 PATCH 操作序列化

PATCH 请求使用 `exclude_unset=True` 和 `exclude_defaults=True`，只序列化用户明确修改的字段：

```python
# [mixins.py:113](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/_base/mixins.py#L113)
item = self.repo.patch(item_id, data.model_dump(exclude_unset=True, exclude_defaults=True))
```

Repository 层 patch 实现（[repository_generic.py:246-254](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/repos/repository_generic.py#L246-L254)）：

```python
def patch(self, match_value, new_data):
    new_data = new_data if isinstance(new_data, dict) else new_data.model_dump()
    entry = self._query_one(match_value=match_value)
    entry_as_dict = self.schema.model_validate(entry).model_dump()
    entry_as_dict.update(new_data)  # 合并新旧数据
    return self.update(match_value, entry_as_dict)
```

### 3.4 分页响应序列化

分页响应自动生成 `next` 和 `previous` 链接（[pagination.py:60-84](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/response/pagination.py#L60-L84)）：

```python
def set_pagination_guides(self, route: str, query_params: dict[str, Any] | None) -> None:
    valid_dict: dict[str, Any] = camelize(query_params) if query_params else {}
    self._set_next(route, valid_dict)
    self._set_prev(route, valid_dict)
```

---

## 四、API 调用方式

### 4.1 后端路由分层

| 路由类型 | 基类 | 权限 | 前缀 |
|---------|------|------|------|
| 管理员路由 | `AdminAPIRouter` | 管理员权限 | `/api/admin/*` |
| 用户路由 | `UserAPIRouter` | 登录用户 | `/api/*` |
| 公开路由 | `APIRouter` | 无权限 | `/api/*` |

路由示例（[controller_labels.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/groups/controller_labels.py)）：

```python
router = APIRouter(prefix="/groups/labels", tags=["Groups: Multi Purpose Labels"], route_class=MealieCrudRoute)

@controller(router)
class MultiPurposeLabelsController(BaseCrudController):
    @router.get("", response_model=MultiPurposeLabelPagination)
    def get_all(self, q: PaginationQuery = Depends(), search: str | None = None): ...
    
    @router.post("", response_model=MultiPurposeLabelOut)
    def create_one(self, data: MultiPurposeLabelCreate): ...
    
    @router.put("/{item_id}", response_model=MultiPurposeLabelOut)
    def update_one(self, item_id: UUID4, data: MultiPurposeLabelUpdate): ...
    
    @router.patch("/{item_id}", response_model=MultiPurposeLabelOut)
    def patch_one(self, item_id: UUID4, data: MultiPurposeLabelUpdate): ...
    
    @router.delete("/{item_id}", response_model=MultiPurposeLabelOut)
    def delete_one(self, item_id: UUID4): ...
```

### 4.2 前端 API 客户端架构

**请求封装层**（[api-client.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/api/api-client.ts)）：

```typescript
export interface RequestResponse<T> {
  response: AxiosResponse<T> | null;
  data: T | null;
  error: any;
}

export const useUserApi = function (): UserApi {
  const requests = useRequests();
  return new UserApi(requests);
};
```

**CRUD 基类**（[base-clients.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/base/base-clients.ts)）：

```typescript
export abstract class BaseCRUDAPI<CreateType, ReadType, UpdateType = CreateType>
  extends BaseCRUDAPIReadOnly<ReadType> {
  
  async createOne(payload: CreateType) {
    return await this.requests.post<ReadType>(this.baseRoute, payload);
  }

  async updateOne(itemId: string | number, payload: UpdateType) {
    return await this.requests.put<ReadType, UpdateType>(this.itemRoute(itemId), payload);
  }

  async patchOne(itemId: string, payload: Partial<UpdateType>) {
    return await this.requests.patch<ReadType, Partial<UpdateType>>(this.itemRoute(itemId), payload);
  }

  async deleteOne(itemId: string | number) {
    return await this.requests.delete<ReadType>(this.itemRoute(itemId));
  }
}
```

**具体 API 实现**（[group-multiple-purpose-labels.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/user/group-multiple-purpose-labels.ts)）：

```typescript
export class MultiPurposeLabelsApi extends BaseCRUDAPI<
  MultiPurposeLabelCreate,
  MultiPurposeLabelOut,
  MultiPurposeLabelUpdate
> {
  baseRoute = "/api/groups/labels";
  itemRoute = (id) => `/api/groups/labels/${id}`;
}
```

**API 客户端聚合**（[client-user.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/client-user.ts)）：

```typescript
export class UserApiClient {
  public multiPurposeLabels: MultiPurposeLabelsApi;
  public categories: CategoriesAPI;
  public recipes: RecipeAPI;
  // ... 其他 API

  constructor(requests: ApiRequestInstance) {
    this.multiPurposeLabels = new MultiPurposeLabelsApi(requests);
    // ... 初始化其他 API
    Object.freeze(this);
  }
}
```

### 4.3 Store 状态管理层

**Store 工厂**（[use-store-factory.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/partials/use-store-factory.ts)）：

```typescript
export const useStore = function <T>(
  storeKey: string,
  store: Ref<T[]>,
  loading: Ref<boolean>,
  initialized: Ref<boolean>,
  api: BaseCRUDAPI<unknown, T, unknown>,
) {
  const storeActions = useStoreActions(storeKey, api, store, loading, initialized);
  const actions = {
    ...storeActions,
    async refresh() { ... },
    flushStore() { ... },
  };
  return { store, actions };
};
```

**具体 Store 使用**（[use-label-store.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/store/use-label-store.ts)）：

```typescript
export const useLabelStore = function () {
  const api = useUserApi();
  return useStore<MultiPurposeLabelOut>(
    "label", store, loading, initialized, api.multiPurposeLabels
  );
};
```

### 4.4 组件调用示例

（[labels.vue](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/pages/group/data/labels.vue)）：

```typescript
<script setup lang="ts">
const userApi = useUserApi();
const labelStore = useLabelStore();

async function handleCreate(createFormData: MultiPurposeLabelSummary) {
  await labelStore.actions.createOne(createFormData);
}

async function handleEdit(editFormData: MultiPurposeLabelSummary) {
  await labelStore.actions.updateOne(editFormData);
}
</script>
```

---

## 五、代码生成机制

### 5.1 类型生成流程

1. **扫描后端 Schema**：遍历 `mealie/schema/` 下的所有模块
2. **pydantic2ts 转换**：将 Pydantic 模型转为 JSON Schema，再转为 TypeScript
3. **后处理**：清理重复类型、修复枚举命名
4. **ESLint 格式化**：自动格式化生成的代码

关键代码在 [gen_ts_types.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/dev/code-generation/gen_ts_types.py#L69-L233)：

```python
def generate_typescript_types() -> None:
    schema_path = PROJECT_DIR / "mealie" / "schema"
    types_dir = PROJECT_DIR / "frontend" / "app" / "lib" / "api" / "types"
    
    for module in schema_path.iterdir():
        ts_out_name = module.name.replace("_", "-") + ".ts"
        out_path = types_dir.joinpath(ts_out_name)
        generate_typescript_defs(path_as_module, str(out_path), exclude=("MealieModel"))
        clean_output_file(out_path)  # 清理重复类型
```

### 5.2 生成的类型示例

（[recipe.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/recipe.ts)）：

```typescript
/* This file was automatically generated from pydantic models by running pydantic2ts. */

export interface RecipeTag {
  id?: string | null;
  groupId?: string | null;
  name: string;
  slug: string;
}

export interface MultiPurposeLabelOut {
  name: string;
  color?: string;
  groupId: string;
  id: string;
}
```

### 5.3 手动补充类型

非生成的类型定义在 [non-generated.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/non-generated.ts)：

```typescript
export interface PaginationData<T> {
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  items: T[];
}

export type RecipeOrganizer = "categories" | "tags" | "tools" | "foods" | "households" | "users";
```

---

## 六、兼容处理机制

### 6.1 字段级兼容

**新增字段策略**：
- 后端新增字段必须提供默认值或标记为可选 (`| None = None`)
- 前端无需修改即可正常工作（TypeScript 可选字段）

**修改字段策略**：
- 使用 `AliasChoices` 支持新旧字段名同时使用（参考 `UpdatedAtField`）
- 旧字段保留但标记为可选，逐步迁移

**删除字段策略**：
- 先标记为可选并添加弃用注释
- 至少一个版本周期后再删除

### 6.2 API 版本控制

当前实现**无 URL 级版本控制**，版本信息通过 `/api/about` 接口暴露：

```python
# [app_about.py:34-47](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/app/app_about.py#L34-L47)
return AppInfo(
    version=APP_VERSION,
    # ...
)
```

### 6.3 路由废弃标记

废弃路由使用 `deprecated=True` 标记，会在 OpenAPI 文档中显示：

```python
# [controller_shopping_lists.py:263](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/households/controller_shopping_lists.py#L263)
@router.post("/{item_id}/recipe/{recipe_id}", response_model=ShoppingListOut, deprecated=True)
```

### 6.4 缓存与 Last-Modified

CRUD 路由自动添加 `Last-Modified` 和 `Cache-Control` 头（[routers.py:27-50](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/_base/routers.py#L27-L50)）：

```python
class MealieCrudRoute(APIRoute):
    async def custom_route_handler(request: Request) -> Response:
        response = await original_route_handler(request)
        if last_modified := response_body.get("updatedAt"):
            response.headers["last-modified"] = last_modified
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
```

### 6.5 PATCH 语义兼容

PATCH 操作遵循以下原则：
1. 只发送用户修改的字段（前端 `Partial<UpdateType>`）
2. 后端 `exclude_unset=True` 只序列化明确设置的字段
3. Repository 层合并新旧数据后更新

---

## 七、边界规则总结

### 7.1 后端 → 前端 约束

| 约束项 | 规则 |
|--------|------|
| 字段命名 | Python snake_case → 自动 camelize → TS camelCase |
| 可选字段 | 后端 `\| None = None` → TS `?: \| null` |
| 必填字段 | 后端无默认值 → TS 无 `?` 标记 |
| 枚举值 | Python `StrEnum` → TS 字符串字面量联合类型 |
| 日期时间 | 统一 UTC 时区，ISO 8601 格式 |

### 7.2 前端 → 后端 约束

| 约束项 | 规则 |
|--------|------|
| 请求字段 | 支持 camelCase 或 snake_case（`populate_by_name=True`） |
| PATCH 请求 | 使用 `Partial<T>`，只发送修改字段 |
| PUT 请求 | 必须发送完整对象 |
| 分页参数 | `page`、`perPage`、`queryFilter` 等驼峰参数 |

### 7.3 变更影响范围

| 变更类型 | 影响范围 | 迁移策略 |
|----------|----------|----------|
| 新增可选字段 | 无 | 直接发布 |
| 新增必填字段 | 前端需更新 | 先给默认值过渡 |
| 删除字段 | 前端需清理 | 先废弃后删除 |
| 字段重命名 | 前后端同步 | AliasChoices 过渡 |
| 修改字段类型 | 前后端同步 | 新增字段并行 |
| 路由路径变更 | 前端路由配置 | 旧路由保留标记废弃 |

---

## 八、关键文件索引

| 类别 | 文件路径 |
|------|----------|
| 基础模型 | [mealie/schema/_mealie/mealie_model.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/_mealie/mealie_model.py) |
| 控制器基类 | [mealie/routes/_base/base_controllers.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/_base/base_controllers.py) |
| CRUD Mixin | [mealie/routes/_base/mixins.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/_base/mixins.py) |
| 分页 Schema | [mealie/schema/response/pagination.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/response/pagination.py) |
| 代码生成 | [dev/code-generation/gen_ts_types.py](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/dev/code-generation/gen_ts_types.py) |
| 前端请求封装 | [frontend/app/composables/api/api-client.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/api/api-client.ts) |
| 前端 CRUD 基类 | [frontend/app/lib/api/base/base-clients.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/base/base-clients.ts) |
| 前端类型定义 | [frontend/app/lib/api/types/](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types) |
| Store 工厂 | [frontend/app/composables/partials/use-store-factory.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/partials/use-store-factory.ts) |
