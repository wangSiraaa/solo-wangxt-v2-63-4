"""
责任区/合同修订 端到端验收测试（真实 PostgreSQL/PostGIS）。

覆盖验收标准：
1. 未来修订只影响新事件；
2. 追溯修订命中未锁定事件时，确认前不改归属，确认后改归属并留版本痕；
3. 命中已锁定处罚时保留原合同/承包商快照，追加审计链，可再复核锁定；
4. 重叠区间（合同时间重叠 / 网格面积重叠）被拒绝；
5. 重复提交（幂等键）被拒绝；
6. 并发/冲突发布被拒绝且不留半套数据；
7. 确认失败整体回滚，刷新后状态可恢复（再预览/替代/撤回）；
8. 空间/时间校验与迁移：边界修订后新事件按新边界归属；
9. 撤回 / 替代流程与状态机约束；
10. OpenAPI 包含修订端点；处罚详情输出修订审计链（处罚追溯）。
"""
import json
from datetime import timedelta

from django.contrib.gis.geos import GEOSGeometry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    ResponsibilityRevision,
    RevisionImpactItem,
    RoadGrid,
)

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
GRID2 = {
    "type": "Polygon",
    "coordinates": [[
        [121.5000, 31.2500], [121.5200, 31.2500],
        [121.5200, 31.2700], [121.5000, 31.2700],
        [121.5000, 31.2500],
    ]],
}
# 与 GRID1 之间留有 [121.480,121.490] 未登记地带
GRID3 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4900, 31.2300], [121.5000, 31.2300],
        [121.5000, 31.2400], [121.4900, 31.2400],
        [121.4900, 31.2300],
    ]],
}
# GRID1 向东扩展到 121.490（与 GRID3 仅边界相接，面积重叠为 0）
GRID1_EXPANDED = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4900, 31.2300],
        [121.4900, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
# GRID1 收缩到西半幅（东半幅变成无网格地带）
GRID1_SHRUNK = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4750, 31.2300],
        [121.4750, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
# 与 GRID2/GRID3 存在面积重叠的非法边界
GRID1_OVERLAPPING = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.5100, 31.2300],
        [121.5100, 31.2700], [121.4700, 31.2700],
        [121.4700, 31.2300],
    ]],
}
IN_LNG, IN_LAT = 121.4770, 31.2370       # GRID1 内（收缩后仍在）
OUT_LNG, OUT_LAT = 121.4845, 31.2350     # 未登记地带（扩展后入 GRID1）


class RevisionFlowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="人民东路一段", geom=json.dumps(GRID1))
        cls.grid2 = RoadGrid.objects.create(code="G2", name="人民东路二段(远)", geom=json.dumps(GRID2))
        cls.grid3 = RoadGrid.objects.create(code="G3", name="人民东路三段", geom=json.dumps(GRID3))
        CleaningContract.objects.create(
            code="A-OLD", grid=cls.grid1, contractor_name="甲保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now - timedelta(days=1),
        )
        CleaningContract.objects.create(
            code="B-NEW", grid=cls.grid1, contractor_name="乙保洁公司",
            valid_from=cls.now - timedelta(days=1), valid_to=cls.now + timedelta(days=10),
        )
        CleaningContract.objects.create(
            code="C-FAR", grid=cls.grid2, contractor_name="丙保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=300),
        )
        CleaningContract.objects.create(
            code="E-ADJ", grid=cls.grid3, contractor_name="庚保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now + timedelta(days=300),
        )

    # ---------- 辅助 ----------
    def upload_photo(self, lng, lat, captured_at, note=""):
        upload = SimpleUploadedFile("scene.png", scene_png_bytes("scene_c_bins"), content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": lng, "lat": lat,
             "captured_at": captured_at.isoformat(), "note": note},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def create_event(self, photo_id, **extra):
        payload = {"category": "litter"}
        payload.update(extra)
        resp = self.client.post(f"/api/photos/{photo_id}/create_event/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def create_revision(self, payload, expect=201):
        resp = self.client.post("/api/revisions/", payload, format="json")
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp.json() if expect == 201 else resp

    def contract_revision_payload(self, **over):
        t = self.now
        payload = {
            "target_kind": "contract_interval",
            "grid": self.grid1.id,
            "target_contract": CleaningContract.objects.get(code="A-OLD").id,
            "proposed_payload": {"contractor_name": "戊保洁公司"},
            "effective_from": (t - timedelta(days=400)).isoformat(),
            "effective_to": (t - timedelta(days=1)).isoformat(),
            "reason": "合同登记错误，实际承包商为戊",
            "actor": "数据管理员-赵",
        }
        payload.update(over)
        return payload

    def test_future_revision_only_affects_new_events(self):
        t = self.now
        # 已存在事件：发生在乙的合同区间
        p1 = self.upload_photo(IN_LNG, IN_LAT, t - timedelta(hours=2), "存量事件")
        e1 = self.create_event(p1["id"])
        self.assertEqual(e1["contractor_name"], "乙保洁公司")

        # 未来生效的新合同区间（与 B-NEW 首尾相接，不重叠）
        rev = self.create_revision({
            "target_kind": "contract_interval",
            "grid": self.grid1.id,
            "proposed_payload": {"contractor_name": "丁保洁公司", "code": "D-FUT"},
            "effective_from": (t + timedelta(days=10)).isoformat(),
            "effective_to": (t + timedelta(days=300)).isoformat(),
            "reason": "新增下一轮保洁合同",
            "idempotency_key": "future-1",
        })
        self.assertEqual(rev["status"], "pending")
        # 预览：窗口内没有存量事件，影响为空
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["impact_summary"]["events"], 0)
        # 确认发布
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/",
                                {"actor": "数据管理员-赵"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "published")
        self.assertTrue(CleaningContract.objects.filter(code="D-FUT").exists())

        # 存量事件归属不变，处罚仍只有初版
        e1_after = self.client.get(f"/api/events/{e1['id']}/").json()
        self.assertEqual(e1_after["contractor_name"], "乙保洁公司")
        self.assertEqual(len(e1_after["penalty"]["versions"]), 1)

        # 生效窗口内的新事件按新合同归属
        p2 = self.upload_photo(IN_LNG, IN_LAT, t + timedelta(days=100), "未来事件")
        e2 = self.create_event(p2["id"])
        self.assertEqual(e2["contractor_name"], "丁保洁公司")

    def test_retroactive_revision_pending_until_confirm(self):
        t = self.now
        # 200 天前的事件归甲公司，处罚未锁定
        p1 = self.upload_photo(IN_LNG, IN_LAT, t - timedelta(days=200), "历史事件")
        e1 = self.create_event(p1["id"])
        self.assertEqual(e1["contractor_name"], "甲保洁公司")
        penalty_id = e1["penalty"]["id"]

        rev = self.create_revision(self.contract_revision_payload(idempotency_key="retro-1"))
        # 基准快照记录了修订前登记状态
        self.assertEqual(rev["baseline_snapshot"]["contractor_name"], "甲保洁公司")
        self.assertEqual(rev["baseline_snapshot"]["code"], "A-OLD")

        # 影响预览：精确命中 事件/处罚/照片，且只是待确认调整
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["impact_summary"]["events"], 1)
        self.assertEqual(body["impact_summary"]["reassign_pending"], 2)  # 事件+处罚各一条
        kinds = {(i["item_kind"], i["disposition"]) for i in body["impact_items"]}
        self.assertIn(("event", "reassign_pending"), kinds)
        self.assertIn(("penalty", "reassign_pending"), kinds)
        self.assertIn(("photo", "photo_context"), kinds)
        event_item = next(i for i in body["impact_items"] if i["item_kind"] == "event")
        self.assertEqual(event_item["old_contractor_name"], "甲保洁公司")
        self.assertEqual(event_item["new_contractor_name"], "戊保洁公司")
        self.assertFalse(event_item["applied"])

        # 关键：确认前归属不变
        e1_mid = self.client.get(f"/api/events/{e1['id']}/").json()
        self.assertEqual(e1_mid["contractor_name"], "甲保洁公司")
        self.assertEqual(len(e1_mid["penalty"]["versions"]), 1)

        # 确认发布：归属改为戊，追加 revision 版本，影响明细生效
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["status"], "published")
        self.assertTrue(all(i["applied"] for i in body["impact_items"]))

        e1_after = self.client.get(f"/api/events/{e1['id']}/").json()
        self.assertEqual(e1_after["contractor_name"], "戊保洁公司")
        penalty = e1_after["penalty"]
        self.assertEqual(penalty["contractor_name"], "戊保洁公司")
        self.assertEqual(penalty["versions"][-1]["kind"], "revision")
        self.assertEqual(len(penalty["versions"]), 2)
        # 处罚追溯：详情输出修订审计链
        self.assertEqual(len(penalty["revision_impacts"]), 1)
        self.assertEqual(penalty["revision_impacts"][0]["new_contractor_name"], "戊保洁公司")
        # 重复发布被拒绝
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(PenaltyVersion.objects.filter(penalty_id=penalty_id).count(), 2)

    def test_locked_penalty_keeps_snapshot_and_gets_audit_chain(self):
        t = self.now
        p1 = self.upload_photo(IN_LNG, IN_LAT, t - timedelta(days=200), "锁定处罚事件")
        e1 = self.create_event(p1["id"])
        penalty_id = e1["penalty"]["id"]
        # 复核通过 → 锁定 v1
        resp = self.client.post(f"/api/penalties/{penalty_id}/review/",
                                {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(resp.json()["status"], "locked")
        self.assertEqual(resp.json()["locked_version_no"], 1)

        rev = self.create_revision(self.contract_revision_payload(idempotency_key="locked-1"))
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        self.assertEqual(resp.json()["impact_summary"]["locked_audit"], 2)  # 事件+处罚
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        # 已锁定处罚：原合同/承包商快照保留，历史不改写
        penalty = PenaltyUnit.objects.get(pk=penalty_id)
        self.assertEqual(penalty.contractor_name, "甲保洁公司")
        self.assertEqual(penalty.contract.code, "A-OLD")
        self.assertEqual(penalty.locked_version.version_no, 1)
        # 事件归属快照同样保留
        self.assertEqual(ProblemEvent.objects.get(pk=e1["id"]).contractor_name, "甲保洁公司")
        # 审计链：追加 revision 版本（处罚回到待复核），复核后可重新锁定
        versions = list(penalty.versions.order_by("version_no"))
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1].kind, "revision")
        self.assertIn("戊保洁公司", versions[-1].reason)
        self.assertEqual(penalty.status, PenaltyUnit.Status.DRAFT)
        resp = self.client.post(f"/api/penalties/{penalty_id}/review/",
                                {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(resp.json()["locked_version_no"], 2)
        # 处罚详情可追溯修订影响
        detail = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(detail["revision_impacts"][0]["disposition"], "locked_audit")
        self.assertEqual(detail["revision_impacts"][0]["old_contractor_name"], "甲保洁公司")
        self.assertEqual(detail["revision_impacts"][0]["new_contractor_name"], "戊保洁公司")

    def test_overlapping_revisions_rejected(self):
        t = self.now
        # 合同区间与 B-NEW 重叠 → 409
        self.create_revision({
            "target_kind": "contract_interval",
            "grid": self.grid1.id,
            "proposed_payload": {"contractor_name": "丁保洁公司", "code": "D-OVERLAP"},
            "effective_from": t.isoformat(),
            "effective_to": (t + timedelta(days=5)).isoformat(),
        }, expect=409)
        # 修订已有合同但新区间与 B-NEW 重叠 → 409
        self.create_revision(self.contract_revision_payload(
            effective_from=(t - timedelta(days=2)).isoformat(),
            effective_to=(t + timedelta(days=2)).isoformat(),
        ), expect=409)
        # 网格新边界与 GRID2/GRID3 面积重叠 → 409
        self.create_revision({
            "target_kind": "grid_boundary",
            "grid": self.grid1.id,
            "proposed_payload": {"geom": GRID1_OVERLAPPING},
            "effective_from": (t - timedelta(days=1)).isoformat(),
        }, expect=409)
        # 非法多边形 → 400
        self.create_revision({
            "target_kind": "grid_boundary",
            "grid": self.grid1.id,
            "proposed_payload": {"geom": {"type": "Point", "coordinates": [121.47, 31.23]}},
            "effective_from": (t - timedelta(days=1)).isoformat(),
        }, expect=400)
        # 合同修订缺 contractor_name → 400；effective_to 早于 effective_from → 400
        self.create_revision({
            "target_kind": "contract_interval",
            "grid": self.grid1.id,
            "proposed_payload": {"code": "D-X"},
            "effective_from": t.isoformat(),
            "effective_to": (t + timedelta(days=5)).isoformat(),
        }, expect=400)
        self.create_revision(self.contract_revision_payload(
            effective_from=t.isoformat(), effective_to=(t - timedelta(days=2)).isoformat(),
        ), expect=400)
        # 数据库中没有任何半成品
        self.assertFalse(CleaningContract.objects.filter(code__startswith="D-").exists())
        self.assertEqual(ResponsibilityRevision.objects.count(), 0)

    def test_duplicate_submission_rejected(self):
        payload = self.contract_revision_payload(idempotency_key="dup-1")
        rev = self.create_revision(payload)
        self.assertEqual(rev["status"], "pending")
        # 相同幂等键重复提交 → 409，且不产生新提案
        self.create_revision(payload, expect=409)
        self.assertEqual(ResponsibilityRevision.objects.count(), 1)
        # 不同幂等键的相同内容允许另提（人工确认不是重复）
        payload2 = self.contract_revision_payload(idempotency_key="dup-2")
        self.create_revision(payload2)
        self.assertEqual(ResponsibilityRevision.objects.count(), 2)

    def test_conflicting_publish_rejected_no_partial_data(self):
        t = self.now
        window = {
            "effective_from": (t + timedelta(days=400)).isoformat(),
            "effective_to": (t + timedelta(days=500)).isoformat(),
        }
        rev1 = self.create_revision({
            "target_kind": "contract_interval", "grid": self.grid1.id,
            "proposed_payload": {"contractor_name": "丁保洁公司", "code": "D-1"},
            "reason": "先提出的下一轮合同", **window,
        })
        rev2 = self.create_revision({
            "target_kind": "contract_interval", "grid": self.grid1.id,
            "proposed_payload": {"contractor_name": "辛保洁公司", "code": "D-2"},
            "reason": "后提出的冲突合同", **window,
        })
        # 先发布者成功
        resp = self.client.post(f"/api/revisions/{rev1['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        # 后发布者（模拟并发到达）在锁内复查发现冲突 → 409
        resp = self.client.post(f"/api/revisions/{rev2['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        # 无半套数据：D-2 合同未创建，rev2 仍为待确认、无影响明细
        self.assertTrue(CleaningContract.objects.filter(code="D-1").exists())
        self.assertFalse(CleaningContract.objects.filter(code="D-2").exists())
        rev2_after = ResponsibilityRevision.objects.get(pk=rev2["id"])
        self.assertEqual(rev2_after.status, "pending")
        self.assertEqual(rev2_after.impact_items.count(), 0)

    def test_failed_confirm_rolls_back_and_recovers(self):
        t = self.now
        # 未锁定事件位于将被收缩出去的地带
        p2 = self.upload_photo(IN_LNG, IN_LAT, t - timedelta(hours=2), "网格内事件")
        e1 = self.create_event(p2["id"])
        self.assertEqual(e1["contractor_name"], "乙保洁公司")
        penalty_id = e1["penalty"]["id"]

        # 边界收缩：e1 落出所有网格 → 修订后无法归属
        rev = self.create_revision({
            "target_kind": "grid_boundary",
            "grid": self.grid1.id,
            "proposed_payload": {"geom": GRID1_SHRUNK},
            "effective_from": (t - timedelta(days=1)).isoformat(),
            "reason": "边界登记过大，需收缩",
        })
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        self.assertEqual(resp.json()["impact_summary"]["unresolved"], 2)  # 事件+处罚
        # 确认 → 422，且整体回滚
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 422)
        # 刷新恢复：网格未变、归属未变、版本未增、修订仍待确认、影响明细未生效
        self.grid1.refresh_from_db()
        self.assertTrue(self.grid1.geom.equals_exact(GEOSGeometry(json.dumps(GRID1)), 0.0))
        e1_after = self.client.get(f"/api/events/{e1['id']}/").json()
        self.assertEqual(e1_after["contractor_name"], "乙保洁公司")
        self.assertEqual(len(e1_after["penalty"]["versions"]), 1)
        rev_after = self.client.get(f"/api/revisions/{rev['id']}/").json()
        self.assertEqual(rev_after["status"], "pending")
        self.assertTrue(all(not i["applied"] for i in rev_after["impact_items"]))
        # 重复预览幂等：明细不翻倍
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        count1 = len(resp.json()["impact_items"])
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        self.assertEqual(len(resp.json()["impact_items"]), count1)
        self.assertEqual(
            RevisionImpactItem.objects.filter(revision_id=rev["id"]).count(), count1)
        # 恢复路径：撤回旧提案
        resp = self.client.post(f"/api/revisions/{rev['id']}/withdraw/",
                                {"actor": "数据管理员-赵"}, format="json")
        self.assertEqual(resp.json()["status"], "withdrawn")
        # 撤回后不可再确认
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(PenaltyVersion.objects.filter(penalty_id=penalty_id).count(), 1)

    def test_grid_boundary_revision_migration(self):
        t = self.now
        # 未登记地带的照片：暂无法立案
        p1 = self.upload_photo(OUT_LNG, OUT_LAT, t - timedelta(hours=2), "边界外照片")
        resp = self.client.post(f"/api/photos/{p1['id']}/create_event/",
                                {"category": "litter"}, format="json")
        self.assertEqual(resp.status_code, 422)

        # 边界向东扩展（覆盖该地带），追溯生效
        rev = self.create_revision({
            "target_kind": "grid_boundary",
            "grid": self.grid1.id,
            "proposed_payload": {"geom": GRID1_EXPANDED},
            "effective_from": (t - timedelta(days=1)).isoformat(),
            "reason": "边界登记遗漏东侧地带",
        })
        resp = self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        body = resp.json()
        # 精确命中该照片（归属上下文变化），无存量事件受影响
        self.assertEqual(body["impact_summary"]["events"], 0)
        self.assertEqual(body["impact_summary"]["photos"], 1)
        photo_item = body["impact_items"][0]
        self.assertEqual(photo_item["item_kind"], "photo")
        self.assertEqual(photo_item["photo"], p1["id"])
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

        # 迁移后：同一照片可按新边界立案并归属乙公司
        e1 = self.create_event(p1["id"])
        self.assertEqual(e1["contractor_name"], "乙保洁公司")
        self.assertEqual(e1["grid"], self.grid1.id)

    def test_locked_event_in_vacated_area_gets_audit_not_block(self):
        t = self.now
        p1 = self.upload_photo(IN_LNG, IN_LAT, t - timedelta(hours=2), "将被划出地带的事件")
        e1 = self.create_event(p1["id"])
        penalty_id = e1["penalty"]["id"]
        self.client.post(f"/api/penalties/{penalty_id}/review/",
                         {"approved": True, "actor": "复核员-李"}, format="json")

        # 收缩边界使事件落出所有网格：已锁定 → 不阻断，记审计链
        rev = self.create_revision({
            "target_kind": "grid_boundary",
            "grid": self.grid1.id,
            "proposed_payload": {"geom": GRID1_SHRUNK},
            "effective_from": (t - timedelta(days=1)).isoformat(),
        })
        resp = self.client.post(f"/api/revisions/{rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        item = next(i for i in resp.json()["impact_items"] if i["item_kind"] == "event")
        self.assertEqual(item["disposition"], "locked_audit")
        self.assertEqual(item["new_contractor_name"], "")
        # 快照保留、审计版本追加
        penalty = PenaltyUnit.objects.get(pk=penalty_id)
        self.assertEqual(penalty.contractor_name, "乙保洁公司")
        self.assertEqual(penalty.versions.order_by("-version_no").first().kind, "revision")

    def test_supersede_flow(self):
        rev = self.create_revision(self.contract_revision_payload(idempotency_key="sup-1"))
        # 以新提案替代：旧提案变为已被替代，新提案待确认并链接旧提案
        resp = self.client.post(
            f"/api/revisions/{rev['id']}/supersede/",
            self.contract_revision_payload(idempotency_key="sup-2", reason="修正后的替代方案"),
            format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        new_rev = resp.json()
        self.assertEqual(new_rev["status"], "pending")
        self.assertEqual(new_rev["supersedes"], rev["id"])
        old = self.client.get(f"/api/revisions/{rev['id']}/").json()
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], new_rev["id"])
        # 旧提案不得再发布/撤回/再替代
        self.assertEqual(self.client.post(
            f"/api/revisions/{rev['id']}/confirm/", {}, format="json").status_code, 409)
        self.assertEqual(self.client.post(
            f"/api/revisions/{rev['id']}/withdraw/", {}, format="json").status_code, 409)
        # 新提案可正常发布
        resp = self.client.post(f"/api/revisions/{new_rev['id']}/confirm/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        # 已发布的修订可以被新修订替代（效果保留、审计链延续）
        resp = self.client.post(
            f"/api/revisions/{new_rev['id']}/supersede/",
            self.contract_revision_payload(idempotency_key="sup-3",
                                           proposed_payload={"contractor_name": "己保洁公司"}),
            format="json")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_revision_openapi_and_read_endpoints(self):
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        for path in ["/api/revisions/", "/api/revisions/{id}/preview/",
                     "/api/revisions/{id}/confirm/", "/api/revisions/{id}/withdraw/",
                     "/api/revisions/{id}/supersede/", "/api/revision-items/"]:
            self.assertIn(path, schema["paths"], path)
        # 影响明细只读过滤
        rev = self.create_revision(self.contract_revision_payload(idempotency_key="ro-1"))
        self.client.post(f"/api/revisions/{rev['id']}/preview/", {}, format="json")
        resp = self.client.get(f"/api/revision-items/?revision={rev['id']}&item_kind=event")
        self.assertEqual(resp.status_code, 200)
        # 写方法 405
        self.assertEqual(self.client.delete(f"/api/revision-items/1/").status_code, 405)
