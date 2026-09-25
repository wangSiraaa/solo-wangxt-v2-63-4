# -*- coding: utf-8 -*-
"""
责任区 / 合同修订子系统验收测试（真实 PostgreSQL/PostGIS）。

覆盖验收点：
* 未来修订只影响新事件（已发生事件归属不动）；
* 追溯修订命中未锁定事件：确认前不改归属，确认后精确改挂；
* 命中锁定处罚：保留原合同/承包商快照与锁定版本，追加 attribution 版本 +
  AttributionCorrection 审计链，处罚回到待复核；
* 同一时空区间不得有两个责任归属：网格/合同重叠提案被拒；
* 重复提交（幂等键 / 同目标多个 draft）与并发发布被拒绝，且无半套数据；
* 失败整体回滚；基准漂移后刷新恢复；
* 受影响照片 / 事件 / 处罚精确命中；历史快照 append-only；
* 旧网格/合同迁移后，去重、整改、归属逻辑不回归（另见 test_api 全套）。
"""
import json
import threading
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections, transaction
from django.utils import timezone
from rest_framework.test import APITestCase, APITransactionTestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    AttributionCorrection,
    CleaningContract,
    ContractHistory,
    GridHistory,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    RevisionImpactItem,
    RevisionProposal,
    RoadGrid,
)
from assessment.services.clock import FixedClock
from assessment.services import revisions

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
# 修订后扩大的网格（向西南扩张，覆盖一个原本在网格外的点）
GRID1_EXPANDED = {
    "type": "Polygon",
    "coordinates": [[
        [121.4600, 31.2200], [121.4800, 31.2200],
        [121.4800, 31.2400], [121.4600, 31.2400],
        [121.4600, 31.2200],
    ]],
}
INSIDE_LNG, INSIDE_LAT = 121.4750, 31.2350
NEWLY_INSIDE_LNG, NEWLY_INSIDE_LAT = 121.4650, 31.2250  # 扩张后才落入网格


def grid(geojson):
    return json.dumps(geojson)


class RevisionTestBase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="网格一", geom=grid(GRID1))
        # 三段连续责任区间：甲(老) -> 乙(当前) -> 丙(未来)，同网格互不重叠
        cls.contract_a = CleaningContract.objects.create(
            code="A-OLD", grid=cls.grid1, contractor_name="甲公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now - timedelta(days=100),
        )
        cls.contract_b = CleaningContract.objects.create(
            code="B-CUR", grid=cls.grid1, contractor_name="乙公司",
            valid_from=cls.now - timedelta(days=100), valid_to=cls.now + timedelta(days=100),
        )
        cls.contract_c = CleaningContract.objects.create(
            code="C-FUT", grid=cls.grid1, contractor_name="丙公司",
            valid_from=cls.now + timedelta(days=100), valid_to=cls.now + timedelta(days=400),
        )

    def upload(self, scene, lng, lat, captured_at):
        upload = SimpleUploadedFile(f"{scene}.png", scene_png_bytes(scene), content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": lng, "lat": lat,
             "captured_at": captured_at.isoformat()},
            format="multipart",
        )
        assert resp.status_code == 201, resp.content
        return resp.json()

    def create_event(self, photo_id):
        resp = self.client.post(f"/api/photos/{photo_id}/create_event/",
                                {"category": "litter"}, format="json")
        assert resp.status_code == 201, resp.content
        return resp.json()

    def propose_contract(self, contract, **over):
        payload = {
            "new_contractor_name": "丁公司",
            "new_valid_from": (self.now - timedelta(days=400)).isoformat(),
            "new_valid_to": (self.now + timedelta(days=400)).isoformat(),
            "reason": "登记错误追溯更正",
            "actor": "监督员-王",
        }
        payload.update(over)
        return self.client.post(f"/api/revisions/contracts/{contract.id}/propose/",
                                payload, format="json")

    def confirm(self, proposal_id):
        return self.client.post(f"/api/revisions/{proposal_id}/confirm/",
                                {"actor": "主管-赵"}, format="json")

    def refresh_db(self, obj):
        obj.refresh_from_db()
        return obj


class FutureRevisionTests(RevisionTestBase):
    def test_future_revision_only_affects_new_events(self):
        # 历史事件（5 天前，乙公司区间）
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=5))
        event_json = self.create_event(photo["id"])
        self.assertEqual(event_json["contractor_name"], "乙公司")
        event = ProblemEvent.objects.get(pk=event_json["id"])
        original_contract_id = event.contract_id

        # “未来修订”：有效时间从明天开始 —— 把乙改派为丁（区间与 B-CUR 完全一致）
        resp = self.propose_contract(
            self.contract_b,
            new_valid_from=(self.now - timedelta(days=100)).isoformat(),
            new_valid_to=(self.now + timedelta(days=100)).isoformat(),
            effective_from=(self.now + timedelta(days=1)).isoformat(),
            effective_to=(self.now + timedelta(days=100)).isoformat(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        proposal_id = resp.json()["id"]

        # 确认前归属不变
        event = self.refresh_db(event)
        self.assertEqual(event.contract_id, original_contract_id)
        self.assertEqual(event.contractor_name, "乙公司")

        # 影响项：历史事件要么不命中、要么标记为 future（不改挂）
        items = RevisionImpactItem.objects.filter(proposal_id=proposal_id)
        self.assertFalse(items.exclude(
            disposition__in=[RevisionImpactItem.Disposition.FUTURE,
                             RevisionImpactItem.Disposition.PHOTO_ONLY]).exists())

        resp = self.confirm(proposal_id)
        self.assertEqual(resp.status_code, 200, resp.content)

        # 确认后历史事件归属仍然不变
        event = self.refresh_db(event)
        self.assertEqual(event.contract_id, original_contract_id)
        self.assertEqual(event.contractor_name, "乙公司")

        # 修订生效后新立案的事件（发生在未来、落入丁的区间）归丁
        future_photo = self.upload("scene_a_angle1", INSIDE_LNG, INSIDE_LAT,
                                   self.now + timedelta(days=30))
        future_event = self.create_event(future_photo["id"])
        self.assertEqual(future_event["contractor_name"], "丁公司")


class RetroactiveUnlockedTests(RevisionTestBase):
    def test_retroactive_hits_unlocked_event_changes_only_after_confirm(self):
        # 事件发生在 200 天前 → 原归甲公司
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=200))
        event_json = self.create_event(photo["id"])
        self.assertEqual(event_json["contractor_name"], "甲公司")
        penalty_id = event_json["penalty"]["id"]
        event = ProblemEvent.objects.get(pk=event_json["id"])

        # 把 A-OLD 的承包商追溯改派为丁公司（覆盖其老区间）
        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        proposal_id = body["id"]
        self.assertEqual(body["status"], "draft")

        # 影响摘要：恰好 1 个未锁定归属调整
        summary = body["impact_summary"]["unlocked_attribution"]
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["pending"], 1)

        # 确认前：归属、处罚快照一律不变
        event = self.refresh_db(event)
        penalty = PenaltyUnit.objects.get(pk=penalty_id)
        self.assertEqual(event.contract_id, self.contract_a.id)
        self.assertEqual(event.contractor_name, "甲公司")
        self.assertEqual(penalty.contractor_name, "甲公司")

        # 无状态预览也给出同样的命中
        preview = self.client.post(
            f"/api/revisions/contracts/{self.contract_a.id}/preview/",
            {
                "new_contractor_name": "丁公司",
                "new_valid_from": (self.now - timedelta(days=400)).isoformat(),
                "new_valid_to": (self.now - timedelta(days=100)).isoformat(),
            }, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["counts"]["unlocked_attribution"], 1)
        self.assertFalse(preview.json()["blocked"])

        # 确认发布
        resp = self.confirm(proposal_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "published")

        # 确认后：事件与未锁定处罚归属被精确改挂
        event = self.refresh_db(event)
        penalty = self.refresh_db(penalty)
        self.assertEqual(event.contractor_name, "丁公司")
        self.assertEqual(penalty.contractor_name, "丁公司")
        self.assertEqual(event.contract_id, self.contract_a.id)  # 合同实体不变，只改承包商
        item = RevisionImpactItem.objects.get(
            proposal_id=proposal_id, event_id=event.id,
            disposition=RevisionImpactItem.Disposition.UNLOCKED_ATTRIBUTION)
        self.assertEqual(item.state, RevisionImpactItem.State.APPLIED)
        self.assertEqual(item.from_contractor_name, "甲公司")
        self.assertEqual(item.to_contractor_name, "丁公司")

        # 合同历史 append-only：本次发布追加一行；旧值保留在提案基准快照中
        history = ContractHistory.objects.filter(contract_id=self.contract_a.id).order_by("id")
        self.assertEqual(history.count(), 1)
        self.assertEqual(history.last().contractor_name, "丁公司")
        self.assertEqual(history.last().revision_id, proposal_id)
        # 修订前的值由基准快照保全
        self.assertEqual(
            RevisionProposal.objects.get(pk=proposal_id).baseline_contractor_name, "甲公司")

        # 再次确认已发布提案 → 409（不重复应用）
        self.assertEqual(self.confirm(proposal_id).status_code, 409)

    def test_grid_boundary_revision_reaches_newly_covered_event_and_photos(self):
        # 点在旧网格外（无合同 -> 本来无法立案）；先直接造一个落在旧网格边缘内、
        # 修订后仍在网格内的事件，以及专门验证照片命中。
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=10))
        event_json = self.create_event(photo["id"])
        self.assertEqual(event_json["contractor_name"], "乙公司")

        resp = self.client.post(
            f"/api/revisions/grids/{self.grid1.id}/propose/",
            {"new_geom": GRID1_EXPANDED, "reason": "边界登记偏小，追溯更正",
             "actor": "监督员-王"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        proposal_id = resp.json()["id"]
        # 现有事件归属未变（仍乙），照片作为信息项被命中
        photo_items = RevisionImpactItem.objects.filter(
            proposal_id=proposal_id, disposition=RevisionImpactItem.Disposition.PHOTO_ONLY)
        self.assertTrue(photo_items.filter(photo_id=photo["id"]).exists())
        self.assertEqual(self.confirm(proposal_id).status_code, 200)

        # 网格历史追加一行（旧几何保留在提案基准快照中）
        gh = GridHistory.objects.filter(grid_id=self.grid1.id).order_by("id")
        self.assertEqual(gh.count(), 1)
        self.assertEqual(gh.last().revision_id, proposal_id)
        proposal = RevisionProposal.objects.get(pk=proposal_id)
        current_geom = RoadGrid.objects.get(id=self.grid1.id).geom
        # 基准快照保留旧几何，与发布后的当前几何不同
        self.assertNotEqual(proposal.baseline_grid_geom.ewkt, current_geom.ewkt)
        self.assertFalse(proposal.baseline_grid_geom.equals(current_geom))

        # 扩张后，原本在网格外的点现在落入网格 → 新事件可立案归乙
        p2 = self.upload("scene_a_angle2", NEWLY_INSIDE_LNG, NEWLY_INSIDE_LAT,
                         self.now - timedelta(hours=5))
        e2 = self.create_event(p2["id"])
        self.assertEqual(e2["contractor_name"], "乙公司")
        self.assertEqual(e2["grid"], self.grid1.id)


class LockedPenaltyTests(RevisionTestBase):
    def test_locked_penalty_keeps_snapshot_and_appends_audit_chain(self):
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=200))
        event_json = self.create_event(photo["id"])
        penalty_id = event_json["penalty"]["id"]

        # 复核锁定当前版本
        r = self.client.post(f"/api/penalties/{penalty_id}/review/",
                             {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(r.status_code, 200)
        locked_version_no = r.json()["locked_version_no"]

        penalty = PenaltyUnit.objects.get(pk=penalty_id)
        event = ProblemEvent.objects.get(pk=event_json["id"])
        self.assertEqual(penalty.status, PenaltyUnit.Status.LOCKED)
        snapshot_contract_id = penalty.contract_id
        snapshot_contractor = penalty.contractor_name
        snapshot_event_contractor = event.contractor_name

        # 追溯修订 A-OLD 甲 -> 丁，命中已锁定处罚
        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        proposal_id = resp.json()["id"]
        self.assertEqual(resp.json()["impact_summary"]["locked_correction"]["total"], 1)

        # 确认前处罚快照不变
        penalty = self.refresh_db(penalty)
        self.assertEqual(penalty.contract_id, snapshot_contract_id)
        self.assertEqual(penalty.contractor_name, snapshot_contractor)
        self.assertEqual(penalty.status, PenaltyUnit.Status.LOCKED)

        self.assertEqual(self.confirm(proposal_id).status_code, 200)

        # 确认后：原合同/承包商快照与锁定版本指针保留（事件与处罚都不改写历史）
        penalty = self.refresh_db(penalty)
        event = self.refresh_db(event)
        self.assertEqual(penalty.contract_id, snapshot_contract_id)
        self.assertEqual(penalty.contractor_name, snapshot_contractor)
        self.assertEqual(event.contractor_name, snapshot_event_contractor)
        self.assertEqual(event.contract_id, snapshot_contract_id)
        self.assertEqual(penalty.locked_version.version_no, locked_version_no)
        # 追加了 attribution 版本，处罚回到待复核
        versions = list(penalty.versions.order_by("version_no"))
        self.assertEqual(versions[-1].kind, PenaltyVersion.Kind.ATTRIBUTION)
        self.assertEqual(versions[-1].points, penalty.points)  # 扣分不变
        self.assertEqual(penalty.status, PenaltyUnit.Status.DRAFT)

        # 审计链：AttributionCorrection 记录从/到归属与提案、版本因果
        corr = AttributionCorrection.objects.get(penalty_id=penalty_id)
        self.assertEqual(corr.proposal_id, proposal_id)
        self.assertEqual(corr.from_contractor_name, "甲公司")
        self.assertEqual(corr.to_contractor_name, "丁公司")
        self.assertEqual(corr.version_id, versions[-1].id)

        # 追溯输出（处罚详情）含审计链
        detail = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(len(detail["attribution_corrections"]), 1)
        self.assertEqual(detail["attribution_corrections"][0]["to_contractor_name"], "丁公司")
        # 历史版本行不可变
        self.assertEqual(float(PenaltyVersion.objects.get(
            penalty_id=penalty_id, version_no=locked_version_no).points),
            float(versions[0].points))

        # 再次复核可锁定 attribution 版本
        r = self.client.post(f"/api/penalties/{penalty_id}/review/",
                             {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["locked_version_no"], versions[-1].version_no)


class ConflictAndConcurrencyTests(RevisionTestBase):
    def test_overlapping_contract_interval_rejected(self):
        # 让 A-OLD 修订后的区间与 B-CUR 重叠
        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=50)).isoformat(),  # 侵入乙区间
        )
        self.assertEqual(resp.status_code, 201)  # 提案仍创建（draft + 拒绝原因）
        proposal_id = resp.json()["id"]
        self.assertTrue(resp.json()["reject_reason"])
        # 确认被 409 拒绝
        r = self.confirm(proposal_id)
        self.assertEqual(r.status_code, 409)

    def test_overlapping_grid_revision_rejected(self):
        # 另一个已有网格与扩张网格重叠
        RoadGrid.objects.create(
            code="G2", name="网格二", geom=grid({
                "type": "Polygon",
                "coordinates": [[
                    [121.4620, 31.2220], [121.4700, 31.2220],
                    [121.4700, 31.2300], [121.4620, 31.2300],
                    [121.4620, 31.2220],
                ]],
            }))
        resp = self.client.post(
            f"/api/revisions/grids/{self.grid1.id}/propose/",
            {"new_geom": GRID1_EXPANDED, "reason": "扩张但与G2重叠"}, format="json")
        self.assertEqual(resp.status_code, 201)
        proposal_id = resp.json()["id"]
        self.assertTrue(resp.json()["reject_reason"])
        self.assertEqual(self.confirm(proposal_id).status_code, 409)

    def test_gap_after_revision_blocks_confirm(self):
        # 把 B-CUR 区间缩短，制造断档；事件发生在被切掉的区间
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=5))
        self.create_event(photo["id"])
        resp = self.propose_contract(
            self.contract_b,
            new_valid_from=(self.now - timedelta(days=100)).isoformat(),
            new_valid_to=(self.now - timedelta(days=30)).isoformat(),  # 5天前变成无合同
        )
        proposal_id = resp.json()["id"]
        self.assertTrue(
            RevisionImpactItem.objects.filter(
                proposal_id=proposal_id,
                disposition=RevisionImpactItem.Disposition.UNRESOLVED).exists())
        self.assertEqual(self.confirm(proposal_id).status_code, 409)

    def test_duplicate_submission_rejected(self):
        body = dict(
            new_contractor_name="丁公司",
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
            reason="x", idempotency_key="DEDUP-1",
        )
        r1 = self.client.post(f"/api/revisions/contracts/{self.contract_a.id}/propose/",
                              body, format="json")
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post(f"/api/revisions/contracts/{self.contract_a.id}/propose/",
                              body, format="json")
        self.assertEqual(r2.status_code, 409)
        # 同目标第二个 draft（不带幂等键）也被拒
        body.pop("idempotency_key")
        r3 = self.client.post(f"/api/revisions/contracts/{self.contract_a.id}/propose/",
                              body, format="json")
        self.assertEqual(r3.status_code, 409)
        # 只有一个待确认提案
        self.assertEqual(RevisionProposal.objects.filter(
            contract=self.contract_a, status="draft").count(), 1)

    def test_withdraw_allows_new_proposal(self):
        r1 = self.propose_contract(self.contract_a,
                                   new_valid_to=(self.now - timedelta(days=100)).isoformat())
        proposal_id = r1.json()["id"]
        w = self.client.post(f"/api/revisions/{proposal_id}/withdraw/",
                             {"actor": "主管-赵"}, format="json")
        self.assertEqual(w.status_code, 200)
        self.assertEqual(w.json()["status"], "withdrawn")
        # 撤回后可重新提交
        r2 = self.propose_contract(self.contract_a,
                                   new_valid_to=(self.now - timedelta(days=100)).isoformat())
        self.assertEqual(r2.status_code, 201)

    def test_replace_creates_new_proposal_and_marks_old_superseded(self):
        r1 = self.propose_contract(self.contract_a,
                                   new_contractor_name="丁公司",
                                   new_valid_to=(self.now - timedelta(days=100)).isoformat())
        old_id = r1.json()["id"]
        r2 = self.client.post(f"/api/revisions/{old_id}/replace/", {
            "new_contractor_name": "戊公司",
            "new_valid_from": (self.now - timedelta(days=400)).isoformat(),
            "new_valid_to": (self.now - timedelta(days=100)).isoformat(),
            "reason": "改派戊公司", "actor": "主管-赵",
        }, format="json")
        self.assertEqual(r2.status_code, 201, r2.content)
        new_id = r2.json()["id"]
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(RevisionProposal.objects.get(pk=old_id).status, "superseded")
        self.assertEqual(RevisionProposal.objects.get(pk=old_id).superseded_by_id, new_id)
        self.assertEqual(RevisionProposal.objects.get(pk=new_id).new_contractor_name, "戊公司")
        # 旧提案不可再确认
        self.assertEqual(self.confirm(old_id).status_code, 409)

    def test_failed_confirm_rolls_back_everything(self):
        photo = self.upload("scene_c_bins", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(days=200))
        event_json = self.create_event(photo["id"])
        event = ProblemEvent.objects.get(pk=event_json["id"])

        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
        )
        proposal_id = resp.json()["id"]
        history_before = ContractHistory.objects.count()

        # 在应用阶段注入故障（未锁定处罚改挂时 PenaltyUnit.save 抛错）
        with mock.patch("assessment.models.PenaltyUnit.save",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                revisions.confirm_proposal(
                    RevisionProposal.objects.get(pk=proposal_id), actor="x",
                    clock=FixedClock(self.now),
                )

        # 事务回滚：提案未发布、归属未变、无新增历史行、无半应用影响项
        self.assertEqual(RevisionProposal.objects.get(pk=proposal_id).status, "draft")
        event = self.refresh_db(event)
        self.assertEqual(event.contractor_name, "甲公司")
        self.assertEqual(ContractHistory.objects.count(), history_before)
        self.assertFalse(RevisionImpactItem.objects.filter(
            proposal_id=proposal_id,
            state__in=[RevisionImpactItem.State.APPLIED,
                      RevisionImpactItem.State.SKIPPED]).exists())

    def test_stale_baseline_blocks_until_refresh(self):
        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
        )
        proposal_id = resp.json()["id"]
        # 提案创建后，合同被外部直接改动（基准漂移）
        c = CleaningContract.objects.get(pk=self.contract_a.id)
        c.contractor_name = "甲公司-改名"
        c.save(update_fields=["contractor_name", "updated_at"])
        # 直接确认 -> 409 要求刷新
        self.assertEqual(self.confirm(proposal_id).status_code, 409)
        # 刷新恢复
        r = self.client.post(f"/api/revisions/{proposal_id}/refresh/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        # 刷新后再确认成功
        self.assertEqual(self.confirm(proposal_id).status_code, 200)
        self.assertEqual(
            RevisionProposal.objects.get(pk=proposal_id).status, "published")


class ConcurrentPublishTests(APITransactionTestCase):
    """两个线程并发发布同一提案：恰好一个成功，另一个被拒，无半套数据。"""

    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.grid1 = RoadGrid.objects.create(code="G1", name="网格一", geom=grid(GRID1))
        self.contract_a = CleaningContract.objects.create(
            code="A-OLD", grid=self.grid1, contractor_name="甲公司",
            valid_from=self.now - timedelta(days=400), valid_to=self.now - timedelta(days=100),
        )
        # 用服务层造提案（避免跨线程共享未提交数据）
        self.proposal = revisions.create_contract_revision(
            contract=self.contract_a,
            new_contractor_name="丁公司",
            new_valid_from=self.now - timedelta(days=400),
            new_valid_to=self.now - timedelta(days=100),
            reason="并发测试", actor="t",
            clock=FixedClock(self.now),
        )

    def test_concurrent_confirm_only_one_succeeds(self):
        errors = []
        results = []
        barrier = threading.Barrier(2)

        def worker():
            try:
                # 每个线程独立连接
                prop = RevisionProposal.objects.get(pk=self.proposal.id)
                barrier.wait(timeout=10)
                try:
                    with transaction.atomic():
                        revisions.confirm_proposal(prop, actor="w",
                                                   clock=FixedClock(self.now))
                    results.append("ok")
                except Exception as exc:  # noqa: BLE001
                    results.append(f"rejected:{type(exc).__name__}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(15); t2.join(15)
        self.assertFalse(t1.is_alive() or t2.is_alive(), "线程未在超时前结束")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["ok", "rejected:RevisionNotDraft"])
        # 只有一次发布生效
        self.assertEqual(RevisionProposal.objects.get(pk=self.proposal.id).status, "published")
        self.assertEqual(ContractHistory.objects.filter(
            revision_id=self.proposal.id).count(), 1)


class MigrationNonRegressionTests(RevisionTestBase):
    """旧网格/合同迁移后：历史链可用，去重/整改/归属逻辑不回归。"""

    def test_published_history_chains_append_only(self):
        # setUpTestData 的数据在迁移之后创建，故没有迁移基线行（revision=NULL）；
        # 任何修订发布都会追加 revision 行，且不覆盖旧行。
        resp = self.propose_contract(
            self.contract_a,
            new_valid_from=(self.now - timedelta(days=400)).isoformat(),
            new_valid_to=(self.now - timedelta(days=100)).isoformat(),
        )
        proposal_id = resp.json()["id"]
        self.assertEqual(
            ContractHistory.objects.filter(
                contract=self.contract_a, revision_id=proposal_id).count(), 0)
        self.confirm(proposal_id)
        rows = ContractHistory.objects.filter(contract=self.contract_a).order_by("id")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows[0].revision_id, proposal_id)
        self.assertEqual(rows[0].contractor_name, "丁公司")

    def test_dedup_rectification_attribution_still_work(self):
        # 归属
        p1 = self.upload("scene_a_angle1", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(hours=2))
        e1 = self.create_event(p1["id"])
        self.assertEqual(e1["contractor_name"], "乙公司")
        # 去重：不同角度挂接不新增扣分
        p2 = self.upload("scene_a_angle2", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(hours=2))
        cand = self.client.get(
            f"/api/candidates/?photo={p2['id']}&matched_photo={p1['id']}&status=pending"
        ).json()["results"][0]
        r = self.client.post(f"/api/candidates/{cand['id']}/decide/",
                             {"action": "attach"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PenaltyUnit.objects.count(), 1)
        # 整改 + 重复整改 409
        self.assertEqual(
            self.client.post(f"/api/events/{e1['id']}/rectify/", {}, format="json").status_code,
            201)
        self.assertEqual(
            self.client.post(f"/api/events/{e1['id']}/rectify/", {}, format="json").status_code,
            409)
        # 复发新事件
        p3 = self.upload("scene_a_repost", INSIDE_LNG, INSIDE_LAT, self.now - timedelta(minutes=5))
        cand = self.client.get(
            f"/api/candidates/?photo={p3['id']}&matched_photo={p1['id']}&status=pending"
        ).json()["results"][0]
        r = self.client.post(f"/api/candidates/{cand['id']}/decide/",
                             {"action": "create_new", "category": "litter"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "recurrence")
        self.assertEqual(PenaltyUnit.objects.count(), 2)
