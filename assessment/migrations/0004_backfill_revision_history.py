# -*- coding: utf-8 -*-
"""
旧网格 / 旧合同迁移：把迁移前已存在的 RoadGrid / CleaningContract 当前值
作为基线（revision=NULL 的 append-only 历史行）写入历史表。

这样后续任何修订发布都能在完整的历史快照链上进行“按发生时归属”解析；
旧数据的事件归属、去重与整改逻辑不受影响（本迁移只新增行，不改任何业务表）。
"""
from django.db import migrations


def backfill_history(apps, schema_editor):
    RoadGrid = apps.get_model("assessment", "RoadGrid")
    CleaningContract = apps.get_model("assessment", "CleaningContract")
    GridHistory = apps.get_model("assessment", "GridHistory")
    ContractHistory = apps.get_model("assessment", "ContractHistory")

    grid_rows = [
        GridHistory(
            grid_id=g.id, code=g.code, name=g.name, geom=g.geom,
            effective_from=g.effective_from, effective_to=g.effective_to,
            revision=None,
        )
        for g in RoadGrid.objects.all()
    ]
    if grid_rows:
        GridHistory.objects.bulk_create(grid_rows, batch_size=500)

    contract_rows = [
        ContractHistory(
            contract_id=c.id, grid_id=c.grid_id, code=c.code,
            contractor_name=c.contractor_name,
            valid_from=c.valid_from, valid_to=c.valid_to, revision=None,
        )
        for c in CleaningContract.objects.all()
    ]
    if contract_rows:
        ContractHistory.objects.bulk_create(contract_rows, batch_size=500)


def noop_reverse(apps, schema_editor):
    # 仅删除基线快照行；历史回填不可精确逆向，但删除这些行可安全回滚到无历史状态
    apps.get_model("assessment", "GridHistory").objects.filter(revision__isnull=True).delete()
    apps.get_model("assessment", "ContractHistory").objects.filter(revision__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assessment", "0003_roadgrid_effective_from_roadgrid_effective_to_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_history, noop_reverse),
    ]
