"""两层人工匹配工作台：
总额相等不等于逐笔完成、跨主体禁止自动核销、比例/明细拆分、结转挂账、依据与置信度留痕。
"""
import pytest
from decimal import Decimal

from sqlalchemy import select

from app.demo.loader import load_m2_batch
from app.enums import MatchLevel, MatchStatus, SettlementLineType, SplitRule
from app.models import MatchCarryForward, SettlementLine, SettlementMatch
from app.services import matching as svc
from app.services.reconciliation import run_reconciliation

OPERATOR = "finance.chen"


async def _workspace(db):
    batch = await load_m2_batch(db)
    return batch, await svc.get_workspace(db, batch.id)


async def test_totals_equal_does_not_mean_line_by_line_complete(db):
    """S9：银行入账合计 280 == 批次净额 280，但逐笔匹配未完成。"""
    batch, ws = await _workspace(db)

    assert ws["level2"]["bank_inflow_equals_net"] is True
    assert ws["level2"]["totals_equal_but_incomplete"] is True
    assert ws["level2"]["complete"] is False
    # LEVEL1 全部只是 PROPOSED，未人工确认前同样不算完成
    assert ws["level1"]["complete"] is False
    assert ws["level1"]["confirmed_count"] == 0
    assert ws["level1"]["order_line_count"] == 3

    # 保证金/调整项作为成分行单独列示，不参与订单匹配、未被误报为幽灵订单差
    component_rows = [r for r in ws["level1"]["rows"] if r["is_non_order_line"]]
    assert {r["line_type"] for r in component_rows} == {
        SettlementLineType.RESERVE, SettlementLineType.ADJUSTMENT
    }
    for r in component_rows:
        assert r["matches"] == []

    # 未匹配余额已单独结转：入账不足 + 跨主体各一条
    reasons = {cf["reason"] for cf in ws["carry_forwards"]}
    assert "SHORT_FALL" in reasons
    assert "CROSS_ENTITY" in reasons


async def test_auto_suggestions_carry_confidence_and_basis_but_stay_proposed(db):
    _, ws = await _workspace(db)
    l1 = [m for r in ws["level1"]["rows"] for m in r["matches"]]
    assert l1, "应生成 LEVEL1 自动建议"
    for m in l1:
        assert m["status"] == MatchStatus.PROPOSED
        assert m["match_method"] == "AUTO"
        assert m["match_basis"] == "ORDER_REF_EXACT"
        assert Decimal(m["confidence"]) == Decimal("95.00")


async def test_manual_confirm_records_basis_confidence_and_operator(db):
    _, ws = await _workspace(db)
    proposal = next(m for r in ws["level1"]["rows"] for m in r["matches"])
    confirmed = await svc.confirm_match(
        db, proposal["id"], OPERATOR, comment="订单号与渠道参考号双重核对一致"
    )
    await db.commit()

    assert confirmed.status == MatchStatus.CONFIRMED
    assert confirmed.match_method == "MANUAL"
    assert confirmed.confidence == Decimal("100.00")
    assert confirmed.confirmed_by == OPERATOR
    assert confirmed.match_basis == "ORDER_REF_EXACT"  # 保留原始自动依据
    assert confirmed.match_detail["confirm_comment"] == (
        "订单号与渠道参考号双重核对一致"
    )

    # 重复确认被拒绝
    with pytest.raises(Exception, match="重复确认"):
        await svc.confirm_match(db, proposal["id"], OPERATOR)
    await db.rollback()


async def test_cross_entity_bank_not_auto_matched_and_manual_needs_ack(db):
    batch, ws = await _workspace(db)
    batch_id = batch.id
    cross_bank = next(b for b in ws["level2"]["banks"] if b["cross_entity"])
    assert cross_bank["bank_ref"] == "BK-M2-2"

    # 引擎没有为跨主体流水生成任何匹配
    auto = (await db.execute(
        select(SettlementMatch)
        .join(SettlementLine, SettlementLine.id == SettlementMatch.settlement_line_id,
              isouter=True)
        .where(SettlementMatch.match_level == MatchLevel.LEVEL2)
    )).scalars().all()
    assert all(m.bank_transaction_id != cross_bank["bank_transaction_id"] for m in auto)

    async def _line():
        return (await db.execute(
            select(SettlementLine).where(
                SettlementLine.batch_id == batch_id,
                SettlementLine.line_ref == "L-M2-003",
            )
        )).scalar_one()

    line = await _line()
    cross_bank_id = cross_bank["bank_transaction_id"]

    # 未勾选跨主体确认 -> 拒绝
    with pytest.raises(Exception, match="跨主体"):
        await svc.create_manual_level2(
            db, batch_id, cross_bank_id,
            [{"settlement_line_id": line.id}], SplitRule.FULL,
            OPERATOR, "尝试跨主体挂账", acknowledge_cross_entity=False,
        )
    await db.rollback()

    # 显式 ack 后允许人工核销，留痕 CROSS_ENTITY_MANUAL 且置信度 100
    # （rollback 会使 ORM 对象过期，全部使用标量 ID 并重新查询）
    line = await _line()
    created = await svc.create_manual_level2(
        db, batch_id, cross_bank_id,
        [{"settlement_line_id": line.id}], SplitRule.FULL,
        OPERATOR, "主体间资金调拨已核实", acknowledge_cross_entity=True,
    )
    await db.commit()
    assert len(created) == 1
    m = created[0]
    assert m.match_basis == "CROSS_ENTITY_MANUAL"
    assert m.confidence == Decimal("100.00")
    assert m.match_detail["cross_entity"] is True


async def test_ratio_split_allocates_and_carries_unmatched_balance(db):
    batch, ws = await _workspace(db)
    bank = next(b for b in ws["level2"]["banks"] if not b["cross_entity"])
    assert bank["bank_ref"] == "BK-M2-1"  # 部分到账 200
    line1 = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-001",
        )
    )).scalar_one()
    line2 = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-002",
        )
    )).scalar_one()

    # 按两行净额权重 117 : 97.5 拆分 200，尾差并入最后一行
    created = await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [
            {"settlement_line_id": line1.id, "ratio": "117"},
            {"settlement_line_id": line2.id, "ratio": "97.5"},
        ],
        SplitRule.RATIO, OPERATOR, "按订单金额比例拆分部分到账",
    )
    await db.commit()

    assert len(created) == 2
    assert created[0].parent_match_id is None
    assert created[1].parent_match_id == created[0].id
    assert created[0].match_basis == "RATIO_SPLIT"
    total = sum((m.allocated_amount for m in created), Decimal("0"))
    assert total == Decimal("200.00")
    assert created[1].allocated_amount == Decimal("90.91")  # 尾差并入末行

    # 两行未覆盖余额各自结转挂账
    carries = (await db.execute(
        select(MatchCarryForward).where(
            MatchCarryForward.batch_id == batch.id,
            MatchCarryForward.reason == "PARTIAL_SPLIT",
            MatchCarryForward.status == "OPEN",
        )
    )).scalars().all()
    assert len(carries) == 2
    by_line = {c.settlement_line_id: c.amount for c in carries}
    assert by_line[line1.id] == Decimal("7.91")
    assert by_line[line2.id] == Decimal("6.59")


async def test_detail_split_rejects_overallocation(db):
    batch, ws = await _workspace(db)
    bank = next(b for b in ws["level2"]["banks"] if not b["cross_entity"])
    line1 = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-001",
        )
    )).scalar_one()
    line2 = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-002",
        )
    )).scalar_one()
    # 两行各自未超自身余额，但合计 214.50 > 流水可用 200 -> 拒绝
    with pytest.raises(Exception, match="超过流水可用余额"):
        await svc.create_manual_level2(
            db, batch.id, bank["bank_transaction_id"],
            [
                {"settlement_line_id": line1.id, "amount": "117.00"},
                {"settlement_line_id": line2.id, "amount": "97.50"},
            ],
            SplitRule.DETAIL, OPERATOR, None,
        )
    await db.rollback()


async def test_reject_proposal_releases_capacity_and_is_idempotent(db):
    batch, ws = await _workspace(db)
    proposal = next(m for r in ws["level1"]["rows"] for m in r["matches"])
    await svc.reject_match(db, proposal["id"], OPERATOR, "订单事实不符，拒收建议")
    await db.commit()

    rejected = await db.get(SettlementMatch, proposal["id"])
    assert rejected.status == MatchStatus.REJECTED
    assert rejected.confirm_comment == "订单事实不符，拒收建议"

    # 重跑引擎不得复活/重建该建议（拒收轨迹保留，额度释放）
    await run_reconciliation(db, batch.id)
    await db.commit()
    active = (await db.execute(
        select(SettlementMatch).where(
            SettlementMatch.match_level == MatchLevel.LEVEL1,
            SettlementMatch.settlement_line_id == rejected.settlement_line_id,
            SettlementMatch.status != MatchStatus.REJECTED,
        )
    )).scalars().all()
    assert active == []


async def test_rejected_level2_proposal_not_revived_on_rerun(db):
    """拒收 LEVEL2 自动建议后重跑引擎：不复活建议、不占用流水额度。"""
    batch, ws = await _workspace(db)
    proposal = next(
        m for m in ws["level2"]["matches"]
        if m["status"] == MatchStatus.PROPOSED and not m["match_detail"].get("cross_entity")
    )
    await svc.reject_match(db, proposal["id"], OPERATOR, "贪心建议不采信")
    await db.commit()

    await run_reconciliation(db, batch.id)
    await db.commit()

    rows = (await db.execute(
        select(SettlementMatch).where(
            SettlementMatch.match_level == MatchLevel.LEVEL2,
            SettlementMatch.bank_transaction_id == proposal["bank_transaction_id"],
        )
    )).scalars().all()
    assert all(m.status == MatchStatus.REJECTED for m in rows)


async def test_carry_forward_must_belong_to_batch(db):
    """跨批次消化结转挂账被拒绝。"""
    from app.demo.loader import load_base_events
    base = await load_base_events(db)
    await run_reconciliation(db, base["batch"].id)
    await db.commit()
    other_cf = (await db.execute(
        select(MatchCarryForward).limit(1)
    )).scalar_one()

    batch, ws = await _workspace(db)
    bank_id = ws["level2"]["banks"][0]["bank_transaction_id"]
    line = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-001",
        )
    )).scalar_one()
    with pytest.raises(Exception, match="不属于当前结算批次"):
        await svc.create_manual_level2(
            db, batch.id, bank_id,
            [{"settlement_line_id": line.id}], SplitRule.FULL,
            OPERATOR, None, carry_forward_id=other_cf.id,
        )
    await db.rollback()


async def test_manual_level1_rejected_for_component_line(db):
    batch, ws = await _workspace(db)
    reserve_line_id = next(
        r["line_id"] for r in ws["level1"]["rows"]
        if r["line_type"] == SettlementLineType.RESERVE
    )
    # 任取一个订单业务事件
    event_id = next(
        m["business_event_id"]
        for r in ws["level1"]["rows"] for m in r["matches"]
    )
    with pytest.raises(Exception, match="成分行"):
        await svc.create_manual_level1(
            db, batch.id, event_id, reserve_line_id, OPERATOR, None
        )
    await db.rollback()


async def test_carry_forward_consumed_by_later_manual_match(db):
    """结转挂账可被后续人工匹配消化并标记 RESOLVED。"""
    batch, ws = await _workspace(db)
    cross_cf = next(cf for cf in ws["carry_forwards"] if cf["reason"] == "CROSS_ENTITY")
    line = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-003",
        )
    )).scalar_one()
    created = await svc.create_manual_level2(
        db, batch.id,
        next(b["bank_transaction_id"] for b in ws["level2"]["banks"]
             if b["bank_ref"] == "BK-M2-2"),
        [{"settlement_line_id": line.id}], SplitRule.FULL,
        OPERATOR, "跨主体调拨核实后消化挂账",
        acknowledge_cross_entity=True, carry_forward_id=cross_cf["id"],
    )
    await db.commit()
    cf = await db.get(MatchCarryForward, cross_cf["id"])
    assert cf.status == "RESOLVED"
    assert cf.resolved_match_id == created[0].id


async def _bank_carries(db, batch_id: int, bank_transaction_id: int):
    return list(
        (
            await db.execute(
                select(MatchCarryForward)
                .where(
                    MatchCarryForward.batch_id == batch_id,
                    MatchCarryForward.source_type == "BANK_TRANSACTION",
                    MatchCarryForward.bank_transaction_id == bank_transaction_id,
                )
                .order_by(MatchCarryForward.id)
            )
        ).scalars().all()
    )


async def test_partial_cross_entity_writeoff_deducts_existing_carry_forward(db):
    """跨主体流水部分核销后刷新：原挂账按核销额扣减，不新增同余额挂账。

    复现路径：工作台对 BK-M2-2（引擎已登记 80 的 CROSS_ENTITY 挂账）只核销 30，
    不关联既有挂账（前端默认“不关联”），随后刷新工作台、重跑自动对账。
    """
    batch, ws = await _workspace(db)
    bank = next(b for b in ws["level2"]["banks"] if b["bank_ref"] == "BK-M2-2")
    original_cf = next(cf for cf in ws["carry_forwards"] if cf["reason"] == "CROSS_ENTITY")
    assert Decimal(original_cf["amount"]) == Decimal("80.00")
    line = (await db.execute(
        select(SettlementLine).where(
            SettlementLine.batch_id == batch.id,
            SettlementLine.line_ref == "L-M2-003",
        )
    )).scalar_one()

    await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [{"settlement_line_id": line.id, "amount": "30.00"}],
        SplitRule.DETAIL, OPERATOR, "跨主体流水先核销一部分",
        acknowledge_cross_entity=True,
    )
    await db.commit()

    # 刷新工作台（不重跑引擎）：同一流水只剩一笔 OPEN 挂账，金额=实际残差 50
    ws = await svc.get_workspace(db, batch.id)
    bank_view = next(b for b in ws["level2"]["banks"]
                     if b["bank_transaction_id"] == bank["bank_transaction_id"])
    assert Decimal(bank_view["residual_amount"]) == Decimal("50.00")
    open_rows = [c for c in ws["carry_forwards"]
                 if c["status"] == "OPEN" and c["bank_ref"] == "BK-M2-2"]
    assert len(open_rows) == 1
    assert Decimal(open_rows[0]["amount"]) == Decimal("50.00")
    # 原挂账被原地扣减而非新增：ID 不变、原因与登记人保留、状态仍 OPEN
    assert open_rows[0]["id"] == original_cf["id"]
    assert open_rows[0]["reason"] == "CROSS_ENTITY"
    assert open_rows[0]["created_by"] == "reconcile-engine"

    rows = await _bank_carries(db, batch.id, bank["bank_transaction_id"])
    assert len(rows) == 1
    assert rows[0].id == original_cf["id"]
    assert rows[0].status == "OPEN"
    assert Decimal(rows[0].amount) == Decimal("50.00")

    # 再次刷新结果稳定（不随读取产生新挂账）
    ws_again = await svc.get_workspace(db, batch.id)
    again_rows = [c for c in ws_again["carry_forwards"]
                  if c["status"] == "OPEN" and c["bank_ref"] == "BK-M2-2"]
    assert [c["id"] for c in again_rows] == [original_cf["id"]]

    # 重跑自动对账后仍只有一笔 50 的挂账，引擎不得复活或新增
    await run_reconciliation(db, batch.id)
    await db.commit()
    rows = await _bank_carries(db, batch.id, bank["bank_transaction_id"])
    open_rows = [r for r in rows if r.status == "OPEN"]
    assert len(open_rows) == 1
    assert open_rows[0].id == original_cf["id"]
    assert Decimal(open_rows[0].amount) == Decimal("50.00")


async def test_remaining_writeoff_resolves_deducted_carry_forward(db):
    """扣减后的挂账被后续核销全额覆盖时结清，未匹配列表不再展示该流水。"""
    batch, ws = await _workspace(db)
    bank = next(b for b in ws["level2"]["banks"] if b["bank_ref"] == "BK-M2-2")

    async def _line():
        return (await db.execute(
            select(SettlementLine).where(
                SettlementLine.batch_id == batch.id,
                SettlementLine.line_ref == "L-M2-003",
            )
        )).scalar_one()

    await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [{"settlement_line_id": (await _line()).id, "amount": "30.00"}],
        SplitRule.DETAIL, OPERATOR, "第一次部分核销",
        acknowledge_cross_entity=True,
    )
    await db.commit()
    await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [{"settlement_line_id": (await _line()).id, "amount": "50.00"}],
        SplitRule.DETAIL, OPERATOR, "核销剩余全部残差",
        acknowledge_cross_entity=True,
    )
    await db.commit()

    rows = await _bank_carries(db, batch.id, bank["bank_transaction_id"])
    assert len(rows) == 1
    assert rows[0].status == "RESOLVED"
    ws = await svc.get_workspace(db, batch.id)
    bank_view = next(b for b in ws["level2"]["banks"]
                     if b["bank_transaction_id"] == bank["bank_transaction_id"])
    assert Decimal(bank_view["residual_amount"]) == Decimal("0.00")
    assert [c for c in ws["carry_forwards"]
            if c["status"] == "OPEN" and c["bank_ref"] == "BK-M2-2"] == []


async def test_repeated_partial_writeoffs_update_single_carry_forward(db):
    """同主体流水多次部分核销：挂账随每次核销原地更新，不重复登记。"""
    batch, ws = await _workspace(db)
    bank = next(b for b in ws["level2"]["banks"] if b["bank_ref"] == "BK-M2-1")

    async def _line():
        return (await db.execute(
            select(SettlementLine).where(
                SettlementLine.batch_id == batch.id,
                SettlementLine.line_ref == "L-M2-001",
            )
        )).scalar_one()

    await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [{"settlement_line_id": (await _line()).id, "amount": "50.00"}],
        SplitRule.DETAIL, OPERATOR, "同主体首次部分核销",
    )
    await db.commit()
    first = await _bank_carries(db, batch.id, bank["bank_transaction_id"])
    assert len(first) == 1
    assert first[0].reason == "PARTIAL_SPLIT"
    assert Decimal(first[0].amount) == Decimal("150.00")

    await svc.create_manual_level2(
        db, batch.id, bank["bank_transaction_id"],
        [{"settlement_line_id": (await _line()).id, "amount": "50.00"}],
        SplitRule.DETAIL, OPERATOR, "同主体二次部分核销",
    )
    await db.commit()
    second = await _bank_carries(db, batch.id, bank["bank_transaction_id"])
    assert len(second) == 1
    assert second[0].id == first[0].id
    assert second[0].status == "OPEN"
    assert Decimal(second[0].amount) == Decimal("100.00")

    ws = await svc.get_workspace(db, batch.id)
    bank_view = next(b for b in ws["level2"]["banks"]
                     if b["bank_transaction_id"] == bank["bank_transaction_id"])
    open_rows = [c for c in ws["carry_forwards"]
                 if c["status"] == "OPEN" and c["bank_ref"] == "BK-M2-1"]
    assert len(open_rows) == 1
    assert Decimal(open_rows[0]["amount"]) == Decimal(bank_view["residual_amount"]) == Decimal("100.00")
