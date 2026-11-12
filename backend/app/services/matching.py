"""两层人工匹配工作台。

职责边界：
  - 自动引擎（reconciliation.py）只产出 PROPOSED 建议与 OPEN 差异；
  - 本服务承载人工动作：确认/拒收建议、手工建立匹配、比例或明细拆分挂账、
    未匹配余额结转（MatchCarryForward）、跨主体人工核销留痕。
关键规则：
  - “总额相等”不等于逐笔匹配完成：完成态按每条结算行/流水的有效 CONFIRMED
    分配逐笔判定，PROPOSED/REJECTED 与未结转余额都会阻断完成标记；
  - 跨主体（legal_entity 不一致）禁止自动核销，人工也必须显式 ack 并留痕；
  - 人工确认/建立的匹配置信度固定 100，并记录匹配依据（match_basis/match_detail）。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..enums import (
    CarryForwardStatus, MatchLevel, MatchMethod, MatchStatus,
    SettlementLineType, SplitRule,
)
from ..models import (
    BankTransaction, BusinessEvent, MatchCarryForward, SettlementBatch,
    SettlementLine, SettlementMatch,
)
from .money import quantize
from .reconciliation import TOLERANCE


class MatchingError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _d(value) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------- 汇总查询
@dataclass
class _Alloc:
    amount: Decimal
    status: str
    id: int


async def _line_level2_allocs(db: AsyncSession) -> dict[int, list[_Alloc]]:
    rows = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.settlement_line_id.is_not(None),
            )
        )
    ).scalars().all()
    out: dict[int, list[_Alloc]] = {}
    for m in rows:
        out.setdefault(m.settlement_line_id, []).append(
            _Alloc(m.allocated_amount, m.status, m.id)
        )
    return out


async def _bank_allocs(db: AsyncSession) -> dict[int, list[_Alloc]]:
    rows = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.bank_transaction_id.is_not(None),
            )
        )
    ).scalars().all()
    out: dict[int, list[_Alloc]] = {}
    for m in rows:
        out.setdefault(m.bank_transaction_id, []).append(
            _Alloc(m.allocated_amount, m.status, m.id)
        )
    return out


def _active_sum(allocs: list[_Alloc] | None) -> Decimal:
    """有效分配：拒收建议不占额度。"""
    if not allocs:
        return Decimal("0")
    return quantize(sum(
        (a.amount for a in allocs if a.status != MatchStatus.REJECTED),
        Decimal("0"),
    ))


async def get_workspace(db: AsyncSession, batch_id: int) -> dict:
    """聚合两层匹配工作台视图。"""
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None:
        raise MatchingError("结算批次不存在")

    lines = (
        await db.execute(
            select(SettlementLine).where(SettlementLine.batch_id == batch_id)
        )
    ).scalars().all()
    line_allocs = await _line_level2_allocs(db)
    bank_allocs = await _bank_allocs(db)

    # ---- LEVEL1：订单事件 -> 结算行
    l1_matches = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL1,
                SettlementMatch.settlement_line_id.in_([l.id for l in lines] or [0]),
            )
        )
    ).scalars().all()
    l1_by_line: dict[int, list[SettlementMatch]] = {}
    for m in l1_matches:
        l1_by_line.setdefault(m.settlement_line_id, []).append(m)

    event_ids = {m.business_event_id for m in l1_matches if m.business_event_id}
    events = {
        e.id: e
        for e in (await db.execute(
            select(BusinessEvent).where(BusinessEvent.id.in_(event_ids or [0]))
        )).scalars().all()
    }

    level1_rows = []
    l1_confirmed_count = l1_order_line_count = 0
    for line in lines:
        matches = l1_by_line.get(line.id, [])
        active = [m for m in matches if m.status != MatchStatus.REJECTED]
        is_non_order = line.line_type in SettlementLineType.NON_ORDER_TYPES
        if not is_non_order:
            l1_order_line_count += 1
            if active and all(m.status == MatchStatus.CONFIRMED for m in active):
                l1_confirmed_count += 1
        row_matches = []
        for m in matches:
            ev = events.get(m.business_event_id) if m.business_event_id else None
            row_matches.append({
                "id": m.id,
                "status": m.status,
                "match_method": m.match_method,
                "confidence": str(m.confidence) if m.confidence is not None else None,
                "match_basis": m.match_basis,
                "match_detail": m.match_detail,
                "split_rule": m.split_rule,
                "confirmed_by": m.confirmed_by,
                "confirm_comment": m.confirm_comment,
                "confirmed_at": m.confirmed_at,
                "allocated_amount": str(m.allocated_amount),
                "business_event_id": m.business_event_id,
                "event_ref": ev.entity_ref if ev else None,
                "event_type": ev.event_type if ev else None,
                "order_ref": ev.order_ref if ev else None,
                "channel_ref": ev.channel_ref if ev else None,
            })
        level1_rows.append({
            "line_id": line.id,
            "line_ref": line.line_ref,
            "line_type": line.line_type,
            "order_ref": line.order_ref,
            "channel_ref": line.channel_ref,
            "gross_amount": str(line.gross_amount),
            "fee_amount": str(line.fee_amount),
            "net_amount": str(line.net_amount),
            "currency": line.currency,
            "is_non_order_line": is_non_order,
            "component_kind": _component_kind(line.line_type),
            "matches": row_matches,
            "active_match_count": len(active),
            "fully_confirmed": bool(active) and all(
                m.status == MatchStatus.CONFIRMED for m in active
            ),
        })

    # ---- LEVEL2：结算行 -> 银行流水
    banks = (
        await db.execute(
            select(BankTransaction)
            .where(
                BankTransaction.channel == batch.channel,
                BankTransaction.currency == batch.currency,
            )
            .order_by(BankTransaction.booked_at)
        )
    ).scalars().all()

    level2_lines = []
    l2_line_complete = True
    for line in lines:
        allocs = line_allocs.get(line.id, [])
        allocated = _active_sum(allocs)
        remaining = quantize(_d(line.net_amount) - allocated)
        confirmed_amt = quantize(sum(
            (a.amount for a in allocs if a.status == MatchStatus.CONFIRMED),
            Decimal("0"),
        ))
        # 退款/拒付/保证金/调整项等净非正行通过批次轧差结算，不单独占用银行流水
        is_netted = _d(line.net_amount) <= 0
        if is_netted:
            complete = True
            remaining = Decimal("0.00")
        else:
            complete = abs(remaining) <= TOLERANCE and confirmed_amt == allocated and allocated != 0
        if not complete:
            l2_line_complete = False
        level2_lines.append({
            "line_id": line.id,
            "line_ref": line.line_ref,
            "line_type": line.line_type,
            "net_amount": str(line.net_amount),
            "allocated_amount": str(allocated),
            "confirmed_amount": str(confirmed_amt),
            "remaining_amount": str(remaining),
            "is_non_order_line": line.line_type in SettlementLineType.NON_ORDER_TYPES,
            "is_netted_line": is_netted,
            "complete": complete,
        })

    level2_banks = []
    l2_bank_clean = True
    for bank in banks:
        allocs = bank_allocs.get(bank.id, [])
        allocated = _active_sum(allocs)
        residual = quantize(bank.amount - allocated)
        cross_entity = bank.legal_entity != batch.legal_entity
        if abs(residual) > TOLERANCE or cross_entity:
            l2_bank_clean = False
        level2_banks.append({
            "bank_transaction_id": bank.id,
            "bank_ref": bank.bank_ref,
            "legal_entity": bank.legal_entity,
            "cross_entity": cross_entity,
            "booked_at": bank.booked_at,
            "amount": str(bank.amount),
            "allocated_amount": str(allocated),
            "residual_amount": str(residual),
            "memo": bank.memo,
        })

    # 批次级 LEVEL2 匹配（自动引擎只挂批次+流水，未落到行；手工拆分匹配同时带结算行）
    batch_matches = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.settlement_batch_id == batch_id,
            ).order_by(SettlementMatch.id)
        )
    ).scalars().all()
    bank_map = {b.id: b for b in banks}
    line_map = {l.id: l for l in lines}
    level2_matches = []
    l2_confirmed_total = l2_active_total = Decimal("0")
    for m in batch_matches:
        if m.status != MatchStatus.REJECTED:
            l2_active_total += m.allocated_amount
        if m.status == MatchStatus.CONFIRMED:
            l2_confirmed_total += m.allocated_amount
        bk = bank_map.get(m.bank_transaction_id)
        ln = line_map.get(m.settlement_line_id) if m.settlement_line_id else None
        level2_matches.append(_match_out(m, bank=bk, line=ln))

    # ---- 结转挂账
    carry_forwards = await _carry_forward_rows(db, batch_id=batch_id)

    # ---- 完成态判定：总额相等 ≠ 逐笔匹配完成
    level1_complete = (
        l1_order_line_count > 0 and l1_confirmed_count == l1_order_line_count
    )
    level2_complete = (
        l2_line_complete and l2_bank_clean
        and l2_active_total > 0
        and quantize(l2_active_total - l2_confirmed_total) <= TOLERANCE
    )
    # 口径一：有效分配合计 == 批次净额
    totals_equal = quantize(l2_active_total - batch.net_amount) <= TOLERANCE
    # 口径二：窗口内银行入账合计 == 批次净额（含跨主体/未匹配流水，最容易被误判的陷阱）
    bank_inflow_total = quantize(sum((_d(b.amount) for b in banks), Decimal("0")))
    bank_inflow_equals_net = quantize(bank_inflow_total - batch.net_amount) <= TOLERANCE
    totals_equal_but_incomplete = (
        totals_equal or bank_inflow_equals_net
    ) and not level2_complete

    return {
        "batch": {
            "id": batch.id,
            "batch_ref": batch.batch_ref,
            "version": batch.version,
            "status": batch.status,
            "channel": batch.channel,
            "legal_entity": batch.legal_entity,
            "currency": batch.currency,
            "period_start": batch.period_start,
            "period_end": batch.period_end,
            "gross_amount": str(batch.gross_amount),
            "fee_amount": str(batch.fee_amount),
            "net_amount": str(batch.net_amount),
        },
        "level1": {
            "rows": level1_rows,
            "order_line_count": l1_order_line_count,
            "confirmed_count": l1_confirmed_count,
            "complete": level1_complete,
        },
        "level2": {
            "lines": level2_lines,
            "banks": level2_banks,
            "matches": level2_matches,
            "active_total": str(quantize(l2_active_total)),
            "confirmed_total": str(quantize(l2_confirmed_total)),
            "bank_inflow_total": str(bank_inflow_total),
            "bank_inflow_equals_net": bank_inflow_equals_net,
            "complete": level2_complete,
            "totals_equal": totals_equal,
            "totals_equal_but_incomplete": totals_equal_but_incomplete,
            "completion_rule": (
                "逐笔完成要求每条结算行/流水的有效分配全部 CONFIRMED 且无未结转余额；"
                "仅批次总额相等不构成逐笔匹配完成"
            ),
        },
        "carry_forwards": carry_forwards,
    }


def _component_kind(line_type: str) -> str | None:
    return {
        SettlementLineType.FEE: "FEE",
        SettlementLineType.RESERVE: "RESERVE",
        SettlementLineType.RESERVE_RELEASE: "RESERVE_RELEASE",
        SettlementLineType.ADJUSTMENT: "ADJUSTMENT",
    }.get(line_type)


def _match_out(m: SettlementMatch, bank: BankTransaction | None = None,
                line: SettlementLine | None = None) -> dict:
    return {
        "id": m.id,
        "match_level": m.match_level,
        "settlement_batch_id": m.settlement_batch_id,
        "settlement_line_id": m.settlement_line_id,
        "line_ref": line.line_ref if line else None,
        "bank_transaction_id": m.bank_transaction_id,
        "bank_ref": bank.bank_ref if bank else None,
        "bank_legal_entity": bank.legal_entity if bank else None,
        "allocated_amount": str(m.allocated_amount),
        "currency": m.currency,
        "status": m.status,
        "match_method": m.match_method,
        "confidence": str(m.confidence) if m.confidence is not None else None,
        "match_basis": m.match_basis,
        "match_detail": m.match_detail,
        "split_rule": m.split_rule,
        "parent_match_id": m.parent_match_id,
        "carry_forward_id": m.carry_forward_id,
        "confirmed_by": m.confirmed_by,
        "confirm_comment": m.confirm_comment,
        "confirmed_at": m.confirmed_at,
        "created_at": m.created_at,
    }


async def _carry_forward_rows(db: AsyncSession, batch_id: int | None = None,
                              status: str | None = None) -> list[dict]:
    stmt = select(MatchCarryForward).order_by(MatchCarryForward.id)
    if batch_id is not None:
        stmt = stmt.where(MatchCarryForward.batch_id == batch_id)
    if status:
        stmt = stmt.where(MatchCarryForward.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    line_ids = [r.settlement_line_id for r in rows if r.settlement_line_id]
    bank_ids = [r.bank_transaction_id for r in rows if r.bank_transaction_id]
    lines = {
        x.id: x for x in (await db.execute(
            select(SettlementLine).where(SettlementLine.id.in_(line_ids or [0]))
        )).scalars().all()
    }
    banks = {
        x.id: x for x in (await db.execute(
            select(BankTransaction).where(BankTransaction.id.in_(bank_ids or [0]))
        )).scalars().all()
    }
    return [
        {
            "id": r.id,
            "match_level": r.match_level,
            "batch_id": r.batch_id,
            "source_type": r.source_type,
            "reason": r.reason,
            "status": r.status,
            "amount": str(r.amount),
            "currency": r.currency,
            "detail": r.detail,
            "created_by": r.created_by,
            "settlement_line_id": r.settlement_line_id,
            "line_ref": lines[r.settlement_line_id].line_ref
            if r.settlement_line_id in lines else None,
            "bank_transaction_id": r.bank_transaction_id,
            "bank_ref": banks[r.bank_transaction_id].bank_ref
            if r.bank_transaction_id in banks else None,
            "resolved_match_id": r.resolved_match_id,
            "resolved_at": r.resolved_at,
            "created_at": r.created_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 确认 / 拒收
async def confirm_match(db: AsyncSession, match_id: int, operator: str,
                        comment: str | None = None,
                        acknowledge_cross_entity: bool = False) -> SettlementMatch:
    """人工确认匹配。跨主体必须显式 ack；确认后置信度 100、依据留痕。"""
    m = await db.get(SettlementMatch, match_id)
    if m is None:
        raise MatchingError("匹配不存在")
    if m.status == MatchStatus.REJECTED:
        raise MatchingError("该建议已被人工拒收，请直接手工建立匹配关系")
    if m.status == MatchStatus.CONFIRMED:
        raise MatchingError("该匹配已确认，请勿重复确认")

    cross_entity = False
    if m.match_level == MatchLevel.LEVEL2 and m.bank_transaction_id:
        bank = await db.get(BankTransaction, m.bank_transaction_id)
        batch = await db.get(SettlementBatch, m.settlement_batch_id)
        if bank and batch and bank.legal_entity != batch.legal_entity:
            cross_entity = True
            if not acknowledge_cross_entity:
                raise MatchingError(
                    f"跨主体匹配（流水 {bank.legal_entity} vs 结算 {batch.legal_entity}）"
                    "必须勾选跨主体人工核销确认，系统不会自动核销"
                )

    m.status = MatchStatus.CONFIRMED
    m.match_method = MatchMethod.MANUAL
    m.confidence = Decimal("100.00")
    m.confirmed_by = operator
    m.confirm_comment = comment
    m.confirmed_at = _now()
    detail = dict(m.match_detail or {})
    detail.update({
        "confirmed_by": operator,
        "confirm_comment": comment,
        "confirmed_at": m.confirmed_at.isoformat(),
        "auto_basis": m.match_basis,
    })
    if cross_entity:
        m.match_basis = "CROSS_ENTITY_MANUAL"
        detail["cross_entity_acknowledged"] = True
    m.match_detail = detail
    await db.flush()
    return m


async def reject_match(db: AsyncSession, match_id: int, operator: str,
                       comment: str) -> SettlementMatch:
    """拒收自动建议：只追加状态轨迹，不物理删除；拒收后额度立即释放。"""
    m = await db.get(SettlementMatch, match_id)
    if m is None:
        raise MatchingError("匹配不存在")
    if m.status == MatchStatus.CONFIRMED:
        raise MatchingError("已确认匹配不可拒收；如需更正请走结算单修订新版本")
    if m.status == MatchStatus.REJECTED:
        raise MatchingError("该建议已拒收")
    m.status = MatchStatus.REJECTED
    m.confirmed_by = operator
    m.confirm_comment = comment
    m.confirmed_at = _now()
    detail = dict(m.match_detail or {})
    detail["rejected_by"] = operator
    detail["reject_comment"] = comment
    m.match_detail = detail
    await db.flush()
    return m


# ---------------------------------------------------------------- LEVEL1 手工匹配
async def create_manual_level1(db: AsyncSession, batch_id: int,
                               business_event_id: int, settlement_line_id: int,
                               operator: str, comment: str | None) -> SettlementMatch:
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None or batch.status == "CLOSED":
        raise MatchingError("批次不存在或已关闭")
    line = await db.get(SettlementLine, settlement_line_id)
    if line is None or line.batch_id != batch_id:
        raise MatchingError("结算行不属于该批次")
    if line.line_type in SettlementLineType.NON_ORDER_TYPES:
        raise MatchingError(
            f"{line.line_ref} 为{_component_kind(line.line_type)}成分行，"
            "不对应单笔订单，不能建立订单匹配"
        )
    event = await db.get(BusinessEvent, business_event_id)
    if event is None:
        raise MatchingError("业务事件不存在")

    # 同一行的旧建议（含引擎指向其他事件的建议）拒收留痕，保留审计轨迹
    old = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL1,
                SettlementMatch.settlement_line_id == line.id,
                SettlementMatch.status != MatchStatus.REJECTED,
            )
        )
    ).scalars().all()
    for m in old:
        if m.business_event_id == business_event_id and m.status == MatchStatus.PROPOSED:
            # 同目标建议直接转为人工确认
            return await confirm_match(db, m.id, operator, comment)
        m.status = MatchStatus.REJECTED
        m.confirmed_by = operator
        m.confirm_comment = f"被手工匹配替代：{comment or ''}"
        m.confirmed_at = _now()

    match = SettlementMatch(
        match_level=MatchLevel.LEVEL1,
        business_event_id=business_event_id,
        settlement_line_id=line.id,
        allocated_amount=line.net_amount,
        currency=line.currency,
        status=MatchStatus.CONFIRMED,
        match_method=MatchMethod.MANUAL,
        confidence=Decimal("100.00"),
        match_basis="MANUAL_SELECTION",
        split_rule=SplitRule.FULL,
        confirmed_by=operator,
        confirm_comment=comment,
        confirmed_at=_now(),
        match_detail={
            "manual": True,
            "line_ref": line.line_ref,
            "event_ref": event.entity_ref,
            "order_ref": event.order_ref,
            "channel_ref": event.channel_ref,
            "comment": comment,
        },
    )
    db.add(match)
    line.matched = True
    await db.flush()
    return match


# ---------------------------------------------------------------- LEVEL2 手工拆分匹配
async def _bank_remaining(db: AsyncSession, bank: BankTransaction) -> Decimal:
    allocs = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.bank_transaction_id == bank.id,
                SettlementMatch.status != MatchStatus.REJECTED,
            )
        )
    ).scalars().all()
    used = quantize(sum((m.allocated_amount for m in allocs), Decimal("0")))
    return quantize(bank.amount - used)


async def _line_remaining(db: AsyncSession, line: SettlementLine) -> Decimal:
    allocs = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.settlement_line_id == line.id,
                SettlementMatch.status != MatchStatus.REJECTED,
            )
        )
    ).scalars().all()
    used = quantize(sum((m.allocated_amount for m in allocs), Decimal("0")))
    return quantize(line.net_amount - used)


async def _open_bank_carry_forwards(
    db: AsyncSession, batch_id: int, bank_transaction_id: int,
) -> list[MatchCarryForward]:
    """该流水在本批次下全部 OPEN 的流水级结转挂账（按登记时间排序）。"""
    return list(
        (
            await db.execute(
                select(MatchCarryForward)
                .where(
                    MatchCarryForward.batch_id == batch_id,
                    MatchCarryForward.source_type == "BANK_TRANSACTION",
                    MatchCarryForward.bank_transaction_id == bank_transaction_id,
                    MatchCarryForward.status == CarryForwardStatus.OPEN,
                )
                .order_by(MatchCarryForward.id)
            )
        ).scalars().all()
    )


async def _open_line_carry_forwards(
    db: AsyncSession, batch_id: int, settlement_line_id: int,
) -> list[MatchCarryForward]:
    """该结算行在本批次下全部 OPEN 的行级结转挂账（按登记时间排序）。"""
    return list(
        (
            await db.execute(
                select(MatchCarryForward)
                .where(
                    MatchCarryForward.batch_id == batch_id,
                    MatchCarryForward.source_type == "SETTLEMENT_LINE",
                    MatchCarryForward.settlement_line_id == settlement_line_id,
                    MatchCarryForward.status == CarryForwardStatus.OPEN,
                )
                .order_by(MatchCarryForward.id)
            )
        ).scalars().all()
    )


async def create_manual_level2(
    db: AsyncSession, batch_id: int, bank_transaction_id: int,
    items: list[dict], split_rule: str, operator: str, comment: str | None,
    acknowledge_cross_entity: bool = False,
    carry_forward_id: int | None = None,
) -> list[SettlementMatch]:
    """手工把一笔银行流水分配到一条或多条结算行。

    items: [{settlement_line_id, amount?（DETAIL 必填）, ratio?（RATIO 必填）}]
    支持按比例拆分（尾差并入最后一行）与指定明细金额；未覆盖余额单独结转。
    """
    batch = await db.get(SettlementBatch, batch_id)
    if batch is None or batch.status == "CLOSED":
        raise MatchingError("批次不存在或已关闭")
    bank = await db.get(BankTransaction, bank_transaction_id)
    if bank is None:
        raise MatchingError("银行流水不存在")
    if bank.channel != batch.channel or bank.currency != batch.currency:
        raise MatchingError("银行流水渠道/币种与结算批次不一致")
    cross_entity = bank.legal_entity != batch.legal_entity
    if cross_entity and not acknowledge_cross_entity:
        raise MatchingError(
            f"跨主体匹配（流水 {bank.legal_entity} vs 结算 {batch.legal_entity}）"
            "必须勾选跨主体人工核销确认"
        )
    if split_rule not in (SplitRule.FULL, SplitRule.RATIO, SplitRule.DETAIL):
        raise MatchingError(f"不支持的拆分方式 {split_rule}")
    if not items:
        raise MatchingError("至少选择一条结算行")

    # 可选：消化既有结转挂账
    cf = None
    if carry_forward_id is not None:
        cf = await db.get(MatchCarryForward, carry_forward_id)
        if cf is None or cf.status != CarryForwardStatus.OPEN:
            raise MatchingError("结转挂账不存在或已结清")
        if cf.batch_id != batch_id:
            raise MatchingError("结转挂账不属于当前结算批次")
        if cf.currency != bank.currency:
            raise MatchingError("结转挂账币种与流水不一致")

    # 人工决策优先：同批次+同流水的引擎 PROPOSED 建议被本次手工分配替代（拒收并留痕）；
    # 已 CONFIRMED 的分配保持有效，继续占用额度
    stale = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.settlement_batch_id == batch_id,
                SettlementMatch.bank_transaction_id == bank.id,
                SettlementMatch.status == MatchStatus.PROPOSED,
            )
        )
    ).scalars().all()
    for m in stale:
        m.status = MatchStatus.REJECTED
        m.confirmed_by = operator
        m.confirm_comment = f"被手工拆分匹配替代：{comment or ''}"
        m.confirmed_at = _now()

    lines = []
    for it in items:
        line = await db.get(SettlementLine, it["settlement_line_id"])
        if line is None or line.batch_id != batch_id:
            raise MatchingError("结算行不属于该批次")
        lines.append((line, it))

    # 预先取各行未匹配余额（避免在同步 sum 里触发 async generator）
    line_remaining_map: dict[int, Decimal] = {}
    for ln, _ in lines:
        line_remaining_map[ln.id] = await _line_remaining(db, ln)

    # 计算每条明细承担金额
    bank_remaining = await _bank_remaining(db, bank)
    # 结转挂账描述的是已存在的未解释余额：BANK_TRANSACTION 挂账金额本身就等于
    # 流水剩余额度，不能再加一次；挂账仅做留痕与“分配不得超过挂账金额”的校验
    if bank_remaining <= TOLERANCE:
        raise MatchingError("该银行流水已无可用余额，不能再分配")

    allocations: list[tuple[SettlementLine, Decimal]] = []
    if split_rule == SplitRule.FULL:
        if len(lines) != 1:
            raise MatchingError("FULL 方式只能选择一条结算行；多行为拆分请用 RATIO/DETAIL")
        line, _ = lines[0]
        take = min(line_remaining_map[line.id], bank_remaining)
        if take <= TOLERANCE:
            raise MatchingError(f"行 {line.line_ref} 已无未匹配余额，不能再分配")
        allocations = [(line, quantize(take))]
    elif split_rule == SplitRule.RATIO:
        ratios = []
        for _, it in lines:
            r = _d(it.get("ratio", 0))
            if r <= 0:
                raise MatchingError("拆分比例必须为正数")
            ratios.append(r)
        ratio_total = sum(ratios, Decimal("0"))
        total_take = min(
            sum(line_remaining_map.values(), Decimal("0")),
            bank_remaining,
        )
        if total_take <= TOLERANCE:
            raise MatchingError("所选结算行均已无未匹配余额")
        running = Decimal("0")
        for idx, ((line, _), r) in enumerate(zip(lines, ratios)):
            if idx == len(lines) - 1:
                amt = quantize(total_take - running)  # 尾差并入最后一行
            else:
                amt = quantize(total_take * r / ratio_total)
                running += amt
            line_rem = line_remaining_map[line.id]
            if amt - line_rem > TOLERANCE:
                raise MatchingError(f"行 {line.line_ref} 分配金额超过其未匹配余额")
            allocations.append((line, amt))
    else:  # DETAIL
        total = Decimal("0")
        for line, it in lines:
            amt = quantize(_d(it.get("amount")))
            if amt <= 0:
                raise MatchingError(f"行 {line.line_ref} 明细金额必须为正数")
            if amt - line_remaining_map[line.id] > TOLERANCE:
                raise MatchingError(f"行 {line.line_ref} 分配金额超过其未匹配余额")
            allocations.append((line, amt))
            total += amt
        if total - bank_remaining > TOLERANCE:
            raise MatchingError(
                f"分配合计 {quantize(total)} 超过流水可用余额 {bank_remaining}"
            )

    # 统一校验：任何拆分方式下，单行分配都不得超过该行未匹配余额
    for line, amt in allocations:
        if amt - line_remaining_map[line.id] > TOLERANCE:
            raise MatchingError(
                f"行 {line.line_ref} 分配 {amt} 超过其未匹配余额 "
                f"{line_remaining_map[line.id]}"
            )

    basis = {
        SplitRule.FULL: "MANUAL_SELECTION",
        SplitRule.RATIO: "RATIO_SPLIT",
        SplitRule.DETAIL: "DETAIL_SPLIT",
    }[split_rule]
    if cross_entity:
        basis = "CROSS_ENTITY_MANUAL"

    # 同一拆分组：第一条作为父记录，其余挂 parent_match_id
    created: list[SettlementMatch] = []
    group_ref = f"MANUAL-L2-B{batch_id}-BK{bank.id}-{_now().strftime('%H%M%S%f')}"
    parent_id: int | None = None
    group_total = Decimal("0")
    for line, amt in allocations:
        m = SettlementMatch(
            match_level=MatchLevel.LEVEL2,
            settlement_batch_id=batch_id,
            settlement_line_id=line.id,
            bank_transaction_id=bank.id,
            allocated_amount=amt,
            currency=bank.currency,
            status=MatchStatus.CONFIRMED,
            match_method=MatchMethod.MANUAL,
            confidence=Decimal("100.00"),
            match_basis=basis,
            split_rule=split_rule,
            parent_match_id=parent_id,
            carry_forward_id=cf.id if cf else None,
            confirmed_by=operator,
            confirm_comment=comment,
            confirmed_at=_now(),
            match_detail={
                "manual": True,
                "group_ref": group_ref,
                "bank_ref": bank.bank_ref,
                "line_ref": line.line_ref,
                "split_rule": split_rule,
                "cross_entity": cross_entity,
                "carry_forward_id": cf.id if cf else None,
                "comment": comment,
            },
        )
        db.add(m)
        await db.flush()
        if parent_id is None:
            parent_id = m.id
        else:
            m.parent_match_id = parent_id
        created.append(m)
        group_total += amt

    # 结转挂账被消化：
    # 显式选中的挂账优先，连同该流水其它 OPEN 挂账（如引擎登记的跨主体/残差挂账）
    # 一并按本次核销金额顺序扣减，避免“原挂账不扣减、又新增一笔挂账”的重复列示；
    # 单笔挂账全额覆盖才 RESOLVED，部分核销保持 OPEN 并只保留实际未结余额。
    if cf is not None or group_total > TOLERANCE:
        linked = await _open_bank_carry_forwards(db, batch_id, bank.id)
        ordered = ([cf] if cf is not None else []) + [
            c for c in linked if cf is None or c.id != cf.id
        ]
        if cf is not None and group_total - cf.amount > TOLERANCE:
            raise MatchingError(
                f"分配金额 {group_total} 超过结转挂账金额 {cf.amount}"
            )
        to_consume = group_total
        for carry in ordered:
            if to_consume <= TOLERANCE:
                break
            take = min(carry.amount, to_consume)
            new_amount = quantize(carry.amount - take)
            carry.detail = {
                **carry.detail,
                "partially_consumed_by": group_ref,
                "consumed_amount": str(take),
                "remaining_amount": str(new_amount),
            }
            if new_amount <= TOLERANCE:
                carry.status = CarryForwardStatus.RESOLVED
                carry.resolved_match_id = parent_id
                carry.resolved_at = _now()
            else:
                carry.amount = new_amount
            to_consume = quantize(to_consume - take)

    # 未匹配余额单独结转：流水残差 / 结算行缺口。
    # 该流水已存在 OPEN 挂账时（含被部分核销后剩余的挂账），残差已由原挂账表达，
    # 不再新增同来源挂账，只在没有任何挂账时登记一笔；残差归零则结清挂账。
    new_bank_remaining = await _bank_remaining(db, bank)
    existing_bank_carries = await _open_bank_carry_forwards(db, batch_id, bank.id)
    if new_bank_remaining <= TOLERANCE:
        for carry in existing_bank_carries:
            carry.status = CarryForwardStatus.RESOLVED
            carry.resolved_match_id = parent_id
            carry.resolved_at = _now()
    elif not existing_bank_carries:
        db.add(MatchCarryForward(
            match_level=MatchLevel.LEVEL2, batch_id=batch_id,
            source_type="BANK_TRANSACTION", reason="PARTIAL_SPLIT",
            bank_transaction_id=bank.id,
            amount=new_bank_remaining, currency=bank.currency,
            status=CarryForwardStatus.OPEN,
            detail={"bank_ref": bank.bank_ref, "group_ref": group_ref,
                    "residual": str(new_bank_remaining)},
            created_by=operator,
        ))
    for line, amt in allocations:
        rem = await _line_remaining(db, line)
        line_carries = await _open_line_carry_forwards(db, batch_id, line.id)
        if rem <= TOLERANCE:
            # 后续核销已覆盖该行全部缺口：历史挂账结清，不留 stale OPEN
            for carry in line_carries:
                carry.status = CarryForwardStatus.RESOLVED
                carry.resolved_match_id = parent_id
                carry.resolved_at = _now()
            continue
        # 同一结算行已有 OPEN 挂账时，仅把挂账金额更新为实际未结余额，不重复登记
        if line_carries:
            primary = line_carries[0]
            primary.amount = rem
            primary.detail = {
                **primary.detail, "group_ref": group_ref,
                "remaining": str(rem),
                "partially_consumed_by": group_ref,
            }
            for extra in line_carries[1:]:
                extra.status = CarryForwardStatus.RESOLVED
                extra.resolved_match_id = parent_id
                extra.resolved_at = _now()
            continue
        db.add(MatchCarryForward(
            match_level=MatchLevel.LEVEL2, batch_id=batch_id,
            source_type="SETTLEMENT_LINE", reason="PARTIAL_SPLIT",
            settlement_line_id=line.id,
            amount=rem, currency=line.currency,
            status=CarryForwardStatus.OPEN,
            detail={"line_ref": line.line_ref, "group_ref": group_ref,
                    "remaining": str(rem)},
            created_by=operator,
        ))

    # 批次级缺口挂账（SHORT_FALL，不绑定具体行）即时对齐：
    # 手工核销也占用批次额度，刷新工作台（不重跑引擎）时挂账金额即等于真实缺口
    batch_rows = (
        await db.execute(
            select(SettlementMatch).where(
                SettlementMatch.match_level == MatchLevel.LEVEL2,
                SettlementMatch.settlement_batch_id == batch_id,
                SettlementMatch.status != MatchStatus.REJECTED,
            )
        )
    ).scalars().all()
    batch_allocated = quantize(sum(
        (m.allocated_amount for m in batch_rows), Decimal("0")
    ))
    batch_remaining = quantize(batch.net_amount - batch_allocated)
    shortfall_carries = list(
        (
            await db.execute(
                select(MatchCarryForward)
                .where(
                    MatchCarryForward.batch_id == batch_id,
                    MatchCarryForward.source_type == "SETTLEMENT_LINE",
                    MatchCarryForward.settlement_line_id.is_(None),
                    MatchCarryForward.status == CarryForwardStatus.OPEN,
                )
                .order_by(MatchCarryForward.id)
            )
        ).scalars().all()
    )
    for idx, carry in enumerate(shortfall_carries):
        if batch_remaining <= TOLERANCE or idx > 0:
            carry.status = CarryForwardStatus.RESOLVED
            carry.resolved_match_id = parent_id
            carry.resolved_at = _now()
        elif abs(quantize(carry.amount - batch_remaining)) > TOLERANCE:
            carry.amount = batch_remaining
            carry.detail = {
                **carry.detail, "group_ref": group_ref,
                "remaining": str(batch_remaining),
                "partially_consumed_by": group_ref,
            }
    await db.flush()
    return created
