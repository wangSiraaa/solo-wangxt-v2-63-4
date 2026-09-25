# 街道环卫考核 API

Django REST Framework + Pillow(pHash) + PostgreSQL/PostGIS 实现的纯 API 服务（无界面）。

## 设计原则（对应考核规则）

| 考核要求 | 实现方式 |
| --- | --- |
| 同一现场问题不能多次扣分 | 一个事件 `ProblemEvent` 对应唯一处罚单元 `PenaltyUnit`（1:1）；不同角度照片经**人工判定**后挂接同一事件，不新增扣分 |
| 相似照片不能自动合并不同地点 | pHash 只生成 `DuplicateCandidate`（pending 候选）；**候选生成阶段刻意不用位置/时间**；位置、时间只在人工 `decide` 时作为依据。误传同图可判 `different` 后分别立案 |
| 整改后复发是新事件 | 整改后复发照片判定 `create_new` → 新建事件、新建处罚单元；候选标记为 `recurrence`；向已整改事件挂接会被拒绝（400） |
| 扣分归属按发生时合同 | 立案时按 `occurred_at ∈ [valid_from, valid_to)` 且点在网格内解析合同，快照到事件/处罚单；与录入时间无关。无合同 422、区间重叠 409 |
| 逾期升级基于可注入时钟 | `POST /api/escalations/run/` body 传 `now`（或服务层传 `Clock`）；`FixedClock`/`OffsetClock` 支持重放，同刻重放幂等 |
| 复核锁定、更正只能追加 | 复核通过把 `locked_version` 指向当前版本；更正/升级一律 `PenaltyVersion` append-only，历史行不可改，追加后重新待复核 |
| 每笔扣分可追溯 | `penalty_no` 唯一 → 事件、责任合同/承包商、版本链、升级记录、复核记录、全部证据照片（含经纬度/拍摄时间/pHash） |
| 证据保全 | 照片只可上传/查询，不提供修改、删除（405）；处罚/版本只读 + 专用动作端点 |
| 网格/合同登记错误可追溯修订 | 修订**提案**带基准快照+有效时间，经冲突校验/影响预览/确认发布；历史 append-only（`GridHistory`/`ContractHistory`），不就地覆盖“按发生时归属” |
| 同一时空唯一责任归属 | 网格面不重叠、同网格合同半开区间不重叠；重叠/断档提案被拒（409），无半套数据 |
| 命中未锁定事件 | 确认前只形成**待确认调整**（`RevisionImpactItem`），归属一律不改；确认后单事务精确改挂事件与处罚 |
| 命中已锁定处罚 | 原合同/承包商快照与锁定版本指针**保留**，只追加 `attribution` 版本（重新待复核）+ `AttributionCorrection` 审计链，不改写历史 |

pHash：Pillow 实现的 64 位 DCT 感知哈希（`assessment/services/phash.py`，仅依赖 Pillow）。

## 快速启动

### 方式 A：docker compose（PostGIS 镜像）

```bash
docker compose up --build
# OpenAPI: http://localhost:8000/api/schema/
```

### 方式 B：本地用户态（无 root，脚本自动装 PostgreSQL+PostGIS+GDAL）

```bash
./run_local.sh        # 起库、迁移、生成模拟图片、runserver
python manage.py seed_demo   # 另开终端：写演示网格/合同/照片
```

### 方式 C：已有 PostgreSQL/PostGIS

```bash
pip install -r requirements.txt
export DB_HOST=... DB_PORT=5432 DB_NAME=... DB_USER=... DB_PASSWORD=...
python manage.py migrate
python manage.py generate_mock_images   # 模拟图片到 media/mock_images/，并打印 pHash 距离矩阵
python manage.py runserver
```

## OpenAPI

* 在线：`GET /api/schema/`（JSON；加 `?format=yaml` 得 YAML）
* 静态导出：[`docs/openapi.json`](docs/openapi.json)、[`docs/openapi.yml`](docs/openapi.yml)
* 重新导出：`python manage.py spectacular --file docs/openapi.yml`

## 主要端点

| 方法 路径 | 说明 |
| --- | --- |
| `POST /api/photos/` | multipart 上传：`image` + `lng/lat` + `captured_at`；服务端算 pHash 并生成疑似候选 |
| `GET /api/candidates/?status=pending` | 疑似重复候选（含双方位置、拍摄时间、汉明距离） |
| `POST /api/candidates/{id}/decide/` | `attach`（同问题挂接，不扣分）/ `create_new`（复发或不同，另立案）/ `different` / `rejected` |
| `POST /api/photos/{id}/create_event/` | 对照片直接立案（无候选或判 different 后） |
| `POST /api/events/{id}/rectify/` | 整改回调（可注入 `now`）；重复回调 409 |
| `POST /api/escalations/run/` | 逾期扫描（可注入 `now`），返回新建升级数；幂等 |
| `POST /api/penalties/{id}/review/` | 复核，`approved=true` 锁定当前版本 |
| `POST /api/penalties/{id}/correct/` | 人工更正：只追加一个 correction 版本 |
| `GET /api/penalties/{id}/` | 完整追溯：事件、承包商、版本链、升级、复核、证据、归属更正审计链 |
| `/api/grids/` `/api/contracts/` `/api/events/` `/api/penalty-versions/` `/api/rectifications/` | 基础数据只读/维护 |

### 责任区 / 合同修订（登记错误追溯更正）

| 方法 路径 | 说明 |
| --- | --- |
| `POST /api/revisions/contracts/{id}/propose/` | 合同修订提案：新承包商/区间/改挂网格 + 修订有效时间；创建即计算影响项（待确认调整） |
| `POST /api/revisions/grids/{id}/propose/` | 网格边界/名称修订提案（GeoJSON 新几何 + 有效时间） |
| `POST /api/revisions/contracts/{id}/preview/` | 无状态合同影响预览（不落库）：受影响照片/事件/处罚与修订前后归属 |
| `POST /api/revisions/grids/{id}/preview/` | 无状态网格影响预览 |
| `GET /api/revisions/` `/api/revisions/{id}/` | 提案检索（含基准快照、`impact_items`、`attribution_corrections`、影响摘要） |
| `POST /api/revisions/{id}/confirm/` | 确认发布：重新时空校验 + 基准乐观锁，全部命中后单事务应用；失败整体回滚 |
| `POST /api/revisions/{id}/withdraw/` | 撤回待确认提案（待确认调整随提案作废，不改任何归属） |
| `POST /api/revisions/{id}/replace/` | 以新内容替代：旧提案 `superseded`，新提案继承基准并重算影响 |
| `POST /api/revisions/{id}/refresh/` | 基准漂移后刷新恢复（重新锚定基准、重算待确认调整） |
| `/api/revision-impacts/` | 影响项（待确认调整）只读：按 `proposal/disposition/state/event/penalty/photo` 过滤 |
| `/api/attribution-corrections/` | 命中锁定处罚的归属更正审计链（从/到承包商、提案、追加版本）只读 |
| `/api/grid-history/` `/api/contract-history/` | 网格/合同 append-only 历史快照（迁移基线 `revision=null`，发布行带提案）只读 |

修订影响项 `disposition`：`unlocked_attribution`（确认后改挂）、`locked_correction`（保留快照+追加版本复核）、
`unresolved`（修订后断档/重叠，硬冲突禁止发布）、`future`（未来事件，只影响新事件不改历史）、`photo_only`（证据信息项）。

## 典型流程（三个关键例子）

模拟图片（`python manage.py generate_mock_images`）及 pHash 距离：

```
scene_a_angle1（首报）
scene_a_angle2    距离 0   —— 不同角度
scene_a_repost    距离 2   —— 整改后复发
scene_a_elsewhere_copy 距离 0 —— 与首报逐像素相同，但坐标在 3km 外
scene_c_bins      距离 28  —— 明显不同现场（不产生候选）
```

1. **同图跨地点误传**：上传远处同图 → 出现候选 → 监督员核对坐标 `121.51,31.26` 与首报不符 →
   `decide=different` → 现场核实属实后 `create_event` → 归远处网格的丙公司，独立处罚单号。
2. **同地点复发**：首报事件 `rectify` → 复发照上传 → 候选 `decide=create_new`
   → 候选变 `recurrence`、产生第二事件与第二处罚单；向已整改事件 attach 会被拒绝。
3. **重复整改回调**：对同一事件第二次 `rectify` → 409，扣分不变。

## 测试

```bash
python manage.py test assessment -v 2
```

真实 PostGIS 测试库（迁移自动 `CREATE EXTENSION postgis`），共 20 个用例：

* `test_api.py`（5 个）：pHash 距离、完整业务流（误传/复发/挂接/重复整改/历史归属/无合同/证据保全/追溯）、
  注入时钟升级 + 复核锁定 + 追加更正、合同重叠 409、OpenAPI schema。
* `test_revisions.py`（15 个）：未来修订只影响新事件；追溯命中未锁定事件确认前后归属；
  命中锁定处罚保留快照+追加 attribution 版本+审计链；网格/合同重叠与断档被拒；
  重复提交/同目标多 draft/并发发布拒绝且无半套数据；失败整体回滚；撤回/替代/基准漂移刷新恢复；
  旧网格/合同迁移后去重、整改、归属不回归。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚单元/版本/升级/复核
                                # + 修订提案/影响项/归属更正/网格·合同历史快照
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    revision_world.py           # 修订“模拟世界”：提案生效后的时空归属解析（只读）
    revisions.py                # 提案/冲突校验/影响预览/确认发布/撤回/替代（单事务）
    clock.py                    # SystemClock / FixedClock / OffsetClock
  migrations/0003_*.py 0004_*.py # 修订表结构 + 旧网格/合同历史回填（revision=null 基线）
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo
  tests/test_api.py             # 端到端测试
  tests/test_revisions.py       # 修订子系统验收测试（含并发/回滚）
docs/openapi.{json,yml}
```
