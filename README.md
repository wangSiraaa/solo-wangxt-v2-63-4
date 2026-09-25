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
| 责任区/合同错误可追溯修订 | 修订提案 `ResponsibilityRevision`（基准快照+有效时间）→ 冲突校验 → 影响预览 → 确认发布/撤回/替代；确认前不改归属；同一时空区间不得发布两个责任归属；已锁定处罚保留原合同/承包商快照，只追加 `revision` 版本（审计链）待复核 |

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
| `GET /api/penalties/{id}/` | 完整追溯：事件、承包商、版本链、升级、复核、修订审计链、证据 |
| `POST /api/revisions/` | 责任区/合同修订提案（基准快照+有效时间+幂等键）；重叠区间/重复提交 409 |
| `POST /api/revisions/{id}/preview/` | 影响预览：精确计算受影响照片/事件/处罚归属，生成待确认调整（不改归属） |
| `POST /api/revisions/{id}/confirm/` | 确认发布：锁内复查冲突+重算影响，单事务应用；冲突 409、无法归属 422、失败回滚 |
| `POST /api/revisions/{id}/withdraw/` | 撤回待确认修订 |
| `POST /api/revisions/{id}/supersede/` | 以新提案替代本修订（旧修订标记已被替代） |
| `GET /api/revision-items/?revision=&penalty=` | 修订影响明细（待确认调整/审计链记录，只读） |
| `/api/grids/` `/api/contracts/` `/api/events/` `/api/penalty-versions/` `/api/rectifications/` | 基础数据只读/维护 |

## 责任区/合同修订流程

网格边界或保洁合同登记错误时，历史归属（按发生时解析）不能被直接覆盖，必须走修订流程：

1. **提案** `POST /api/revisions/`：`target_kind=grid_boundary`（`proposed_payload={"geom": <GeoJSON>}`）
   或 `contract_interval`（`proposed_payload={"contractor_name": ..., "code": 新增时必填}`，
   `target_contract` 留空表示新增合同区间）。服务端登记**基准快照**与**有效时间**
   `[effective_from, effective_to)`，状态为 `pending`；`idempotency_key` 重复提交 409。
2. **预览** `POST /api/revisions/{id}/preview/`：按时空足迹精确计算受影响的照片、事件、
   处罚归属，落库为**待确认调整**（`RevisionImpactItem`，`applied=false`）；可重复执行，明细整体替换。
3. **确认** `POST /api/revisions/{id}/confirm/`：行锁内复查时空冲突并重算影响，单事务发布：
   * 未来生效的修订：只改登记（边界/合同），只影响之后发生的新事件；
   * 追溯修订命中**未锁定**处罚：确认后改事件/处罚归属快照，并追加 `revision` 版本留痕；
   * 命中**已锁定**处罚：保留原合同/承包商快照与锁定指针，只追加 `revision` 版本
     （审计链），处罚回到待复核，由复核重新锁定——历史行不改写；
   * 修订后无法归属的未锁定事件 → 422 整体回滚；重叠区间/重复发布/并发冲突 → 409，不留半套数据。
4. **撤回/替代**：`withdraw` 仅对待确认有效；`supersede` 以新提案取代旧修订（审计链延续）。

处罚详情 `GET /api/penalties/{id}/` 的 `revision_impacts` 输出该处罚的全部修订审计记录。

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

16 个用例（真实 PostGIS 测试库，迁移自动 `CREATE EXTENSION postgis`）：
pHash 距离、完整业务流（误传/复发/挂接/重复整改/历史归属/无合同/证据保全/追溯）、
注入时钟升级 + 复核锁定 + 追加更正、合同重叠 409、OpenAPI schema；
修订流程（未来修订只影响新事件、追溯修订确认前不改归属、锁定处罚保留快照+审计链、
重叠/重复提交/并发发布拒绝且无半套数据、失败回滚与刷新恢复、边界迁移、撤回/替代）。

## 目录

```
sanitation/settings.py          # PostGIS、drf-spectacular、业务阈值
assessment/
  models.py                     # 网格/合同/事件/照片/候选/整改/处罚单元/版本/升级/复核/修订提案/影响明细
  services/
    phash.py                    # Pillow 感知哈希
    duplicates.py               # 仅按 pHash 生成候选
    attribution.py              # 发生时合同归属（PostGIS 空间查询）
    events.py / rectification.py / penalties.py / escalation.py / decisions.py
    revisions.py                # 修订提案/冲突校验/影响预览/确认发布/撤回/替代
    clock.py                    # SystemClock / FixedClock / OffsetClock
  mockimages.py                 # 5 张确定性模拟图片
  management/commands/          # generate_mock_images / seed_demo
  tests/test_api.py             # 端到端测试
  tests/test_revisions.py       # 修订流程验收测试
docs/openapi.{json,yml}
```
