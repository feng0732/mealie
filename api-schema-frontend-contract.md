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

### 3.5 分页响应 snake_case 例外

**重要例外：分页响应字段不遵循 camelCase 命名约定。**

#### 根因分析

`PaginationBase` 继承自 `BaseModel` 而非 `MealieModel`，因此不具备自动驼峰转换能力：

```python
# [pagination.py:32, 46, 51](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/response/pagination.py#L32-L51)

class RequestQuery(MealieModel):           # ✅ 继承 MealieModel，自动 camelize
    order_by: str | None = None            # → orderBy
    query_filter: str | None = None        # → queryFilter

class PaginationQuery(RequestQuery):       # ✅ 间接继承 MealieModel
    page: int = 1                          # → page
    per_page: int = 50                     # → perPage（自动转换）

class PaginationBase[DataT: BaseModel](BaseModel):  # ❌ 继承 BaseModel，无 camelize
    page: int = 1                          # → page（不变）
    per_page: int = 10                     # → per_page（❌ 不转换！）
    total: int = 0
    total_pages: int = 0                   # → total_pages（❌ 不转换！）
    items: list[DataT]
    next: str | None = None
    previous: str | None = None
```

#### 代码证据对比

**后端生成的类型**（[response.ts:19-27](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/response.ts#L19-L27)）：
```typescript
// PaginationQuery 继承 MealieModel → 正确 camelize
export interface PaginationQuery {
  orderBy?: string | null;
  queryFilter?: string | null;
  page?: number;
  perPage?: number;  // ✅ 正确转换为 perPage
}
```

**前端手写类型**（[non-generated.ts:19-25](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/non-generated.ts#L19-L25)）：
```typescript
// PaginationData 必须手写匹配后端实际输出
export interface PaginationData<T> {
  page: number;
  per_page: number;      // ❌ 保持 snake_case，与实际响应一致
  total: number;
  total_pages: number;   // ❌ 保持 snake_case
  items: T[];
}
```

**前端实际消费**（[use-actions-factory.ts:40-48](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/composables/partials/use-actions-factory.ts#L40-L48)）：
```typescript
const { data } = await api.getAll(page, perPage, params);
if (data && allRef) {
  allRef.value = data.items;  // ✅ items 字段名一致，无问题
}
```

#### PaginationBase 不继承 MealieModel 的可能原因（推测）

> **⚠️ 以下为代码推断，无明确设计文档证据**

根据代码上下文推测，可能的原因包括：
1. **泛型参数约束**：`PaginationBase[DataT: BaseModel]` 的泛型约束要求 `DataT` 是 `BaseModel` 子类，使用 `MealieModel` 会引入不必要的依赖
2. **保持分页字段稳定**：`per_page`、`total_pages` 作为 API 契约的一部分，可能有意保持 snake_case 以兼容早期客户端
3. **代码生成工具限制**：`pydantic2ts` 对泛型类的支持不完善，继承 `BaseModel` 可减少生成复杂度

#### 影响范围

| 影响项 | 说明 |
|--------|------|
| 前端类型安全 | 必须手动维护 `PaginationData<T>` 与后端一致 |
| 命名一致性 | 分页字段 `per_page`、`total_pages` 与其他字段 `groupId` 风格不一致 |
| 代码生成 | `PaginationBase` 及其子类（如 `MultiPurposeLabelPagination`）均无法自动生成 |

#### 分页响应例外：最终总结

| 对比项 | PaginationQuery（继承 MealieModel） | PaginationBase（继承 BaseModel） |
|--------|-----------------------------------|--------------------------------|
| 字段示例 | `per_page` → `perPage`（✅ 转换） | `per_page` → `per_page`（❌ 不转换） |
| 代码生成 | ✅ 生成 `PaginationQuery` 接口 | ❌ 无生成类型，需手写 `PaginationData<T>` |
| 前端使用 | `api.getAll(page, perPage, params)` | `data.per_page`、`data.total_pages` |
| 类型来源 | 生成类型 `types/response.ts` | 手写类型 `types/non-generated.ts` |

> **关键事实**：所有 `*Pagination` 子类（如 `MultiPurposeLabelPagination`、`RecipePagination`）均未在生成类型中出现，前端统一使用手写的 `PaginationData<T>` 作为所有分页响应的类型。

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

### 5.4 生成类型 vs 手写类型：完整差异对比

#### 识别标记

**生成类型**的文件头有明确的自动生成标记（[recipe.ts:1-6](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/recipe.ts#L1-L6)）：
```typescript
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/
```

**手写类型**集中在 [non-generated.ts](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/non-generated.ts)，无自动生成标记。

#### 完整差异表（修正后）

| 维度 | 生成类型（auto-generated） | 手写类型（non-generated） |
|------|---------------------------|---------------------------|
| **来源** | 从 Pydantic Schema 自动生成 | 前端团队手动维护 |
| **文件位置** | `types/*.ts`（如 `recipe.ts`、`response.ts`） | `types/non-generated.ts` |
| **可修改性** | ❌ 禁止手动修改，重新生成会被覆盖 | ✅ 可手动编辑 |
| **支持 interface** | ✅ 大多数数据接口 | ✅ 泛型接口如 `PaginationData<T>` |
| **支持 type 联合** | ✅ `export type RegisteredParser = "nlp" \| "brute" \| "openai"` | ✅ `export type RecipeOrganizer = "categories" \| ...` |
| **支持泛型** | ❌ pydantic2ts 不支持泛型类 | ✅ `PaginationData<T>` |
| **函数类型** | ❌ | ✅ `ApiRequestInstance` 包含方法签名 |
| **工具类型** | ❌ | ✅ `NoUndefinedField<T>` 条件类型 |
| **TypeScript enum** | ❌ 仅生成字符串字面量联合 | ✅ `Organizer`、`SSEDataEventStatus` |
| **外部依赖** | ❌ 不引用外部类型 | ✅ 引用 `AxiosRequestConfig`、`AxiosResponse` |

#### 代码证据：生成类型的真实能力

**生成类型不仅有 interface，也有 type 联合**（[recipe.ts:8-13](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/recipe.ts#L8-L13)）：
```typescript
// ✅ 生成类型也包含字符串字面量联合类型
export type ExportTypes = "json";
export type RegisteredParser = "nlp" | "brute" | "openai";
export type TimelineEventType = "system" | "info" | "comment";

// ✅ 生成简单接口
export interface PaginationQuery {
  orderBy?: string | null;
  perPage?: number;
}
```

**手写类型也包含数据结构，不仅是封装**（[non-generated.ts:19-25, 68-76](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/frontend/app/lib/api/types/non-generated.ts#L19-L25)）：
```typescript
// ✅ 数据结构类型：后端有定义但代码生成失败，需手动同步
export interface PaginationData<T> {  // 对应后端 PaginationBase
  page: number;
  per_page: number;
  total: number;
  total_pages: number;
  items: T[];
}

// ✅ 数据结构类型：后端有 StandardizedUnitType(StrEnum)，但未生成
export type StandardizedUnitType
  = | "fluid_ounce"
    | "cup"
    | "ounce"
    // ...
```

#### 代码证据：哪些类型本该生成但实际未生成？

后端有定义但前端生成类型中缺失的类型（代码生成工具限制导致）：

| 后端类型 | 前端状态 | 原因推测 |
|---------|---------|---------|
| `PaginationBase<T>` | ❌ 缺失，需手写 `PaginationData<T>` | 泛型类 pydantic2ts 不支持 |
| `MultiPurposeLabelPagination` | ❌ 缺失，复用 `PaginationData<T>` | 继承泛型基类导致生成失败 |
| `RecipePagination` | ❌ 缺失，复用 `PaginationData<T>` | 继承泛型基类导致生成失败 |
| `StandardizedUnitType(StrEnum)` | ❌ 缺失，需手动重定义 | 可能是模块导出或清理逻辑问题 |

> **代码证据**：`labels.ts` 中只有 `MultiPurposeLabelCreate/Out/Save/Summary/Update`，没有 `MultiPurposeLabelPagination`（后端 [multi_purpose_label.py:29](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/schema/labels/multi_purpose_label.py#L29) 有定义）。

#### 为什么需要手写类型？

1. **泛型分页数据**：`PaginationData<T>` 无法自动生成，因为 `PaginationBase` 是泛型类，且所有 `*Pagination` 子类均无法生成
2. **请求封装接口**：`ApiRequestInstance`、`RequestResponse<T>` 是前端架构需要，后端无对应 Schema
3. **运行时枚举**：`enum Organizer` 用于运行时逻辑判断，字符串字面量类型仅用于编译时
4. **类型体操工具**：`NoUndefinedField<T>` 是 TypeScript 高级类型，Pydantic 无对应概念
5. **外部库依赖**：引用 `axios` 类型，不属于后端契约
6. **代码生成遗漏**：`StandardizedUnitType` 等后端有定义但生成失败的类型，需手动同步

#### 边界规则：修正后的实际约定

```typescript
// ✅ 数据结构：优先使用生成类型
import type { MultiPurposeLabelOut } from "~/lib/api/types/labels";  // 生成类型

// ✅ 分页数据：只能用手写类型（生成失败）
import type { PaginationData } from "~/lib/api/types/non-generated";  // 手写类型

// ✅ API 调用封装：使用手写类型
import type { RequestResponse } from "~/lib/api/types/non-generated";  // 手写类型

// ✅ 运行时逻辑：使用手写 enum
import { Organizer } from "~/lib/api/types/non-generated";
if (type === Organizer.Category) { /* 运行时判断 */ }
```

> **⚠️ 实际约定**：
> 1. 后端 Pydantic Schema 是数据结构的唯一真值源
> 2. 优先使用自动生成的类型
> 3. 当生成类型缺失时（如泛型、枚举），前端可手动定义对应类型，但**必须与后端 Schema 保持一致**
> 4. 禁止前端定义后端不存在的数据字段
> 5. 手写类型仅限：前端架构封装 + 生成失败的数据类型补全

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

### 6.3.1 重要：`deprecated` 仅用于路由级标记

**证据结论：`deprecated=True` 仅用于路由装饰器，**不用于**字段级或 Schema 级废弃。**

#### 完整代码搜索证据

对整个 `mealie/` 目录全量搜索 `deprecated` 关键字：

| 文件 | 用法 | 级别 |
|------|------|------|
| [controller_shopping_lists.py:263](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/routes/households/controller_shopping_lists.py#L263) | `@router.post(..., deprecated=True)` | ✅ 路由级 |
| [task.py:18](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/db/models/server/task.py#L18) | `# Server Tasks are deprecated...` | ❌ 仅代码注释 |
| [mealplan.py:45](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/db/models/household/mealplan.py#L45) | `# Old filters - deprecated in favor of...` | ❌ 仅代码注释 |
| [cookbook.py:38](file:///d:/fz/0601/solo-dogfeeding/code/34-mealie/mealie/db/models/household/cookbook.py#L38) | `# Old filters - deprecated in favor of...` | ❌ 仅代码注释 |

#### Schema 字段级废弃搜索结果为空：
```bash
# 搜索 Field(deprecated=True) 或 Field(..., deprecated=True)
# → 0 匹配结果
```

#### 为什么不支持字段级 deprecated？

1. **Pydantic v2 支持但未使用**：Pydantic v2 支持 `Field(..., deprecated=True)` 语法，可在 JSON Schema 中标记弃用字段，但本项目未采用此机制

2. **向后兼容优先**：字段级废弃通过"可选字段 + 默认值"实现，而非显式 deprecated 标记

3. **代码生成限制**：pydantic2ts 可能无法正确传递 deprecated 标记到 TypeScript

#### 当前字段级兼容机制对比

| 机制 | 本项目采用 | Pydantic 标准 |
|------|----------|----------------|
| 字段废弃 | 标记为可选 + 注释 | `Field(..., deprecated=True)` |
| 路由废弃 | `@router(..., deprecated=True)` | 同左 |
| OpenAPI 可见性 | 仅路由级可见 | 字段级也可见 |
| 前端类型警告 | 无 | 可生成 `@deprecated` JSDoc |

#### 字段级废弃的实际做法：

```python
# 本项目的做法：标记为可选，不使用 deprecated
class SomeSchema(MealieModel):
    old_field: str | None = None  # 不使用 Field(deprecated=True)
    new_field: str | None = None
```

> **⚠️ 注意**：如果需要在 OpenAPI 文档中明确标记字段废弃，需要手动添加 `Field(..., json_schema_extra={"deprecated": True})` 并确保代码生成支持，当前无此实践。

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
