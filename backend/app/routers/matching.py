"""两层匹配工作台 API。

  GET  /api/matching/workspace/{batch_id}   工作台聚合视图（两层逐笔状态、成分行、结转）
  POST /api/matching/matches/{id}/confirm   人工确认（跨主体须 ack，记录依据与置信度 100）
  POST /api/matching/matches/{id}/reject    拒收自动建议（释放占用额度，轨迹保留）
  POST /api/matching/level1/manual          手工订单事件 -> 结算行
  POST /api/matching/level2/manual          手工结算行 -> 银行流水（比例/明细拆分、结转）
  GET  /api/matching/carry-forwards         未匹配余额结转挂账列表
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    ManualLevel1In, ManualLevel2In, MatchConfirmIn, MatchRejectIn,
)
from ..services import matching as svc

router = APIRouter(prefix="/api/matching", tags=["matching-workspace"])


@router.get("/workspace/{batch_id}")
async def workspace(batch_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await svc.get_workspace(db, batch_id)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@router.post("/matches/{match_id}/confirm")
async def confirm(match_id: int, data: MatchConfirmIn,
                  db: AsyncSession = Depends(get_db)):
    try:
        m = await svc.confirm_match(
            db, match_id, data.operator,
            comment=data.comment,
            acknowledge_cross_entity=data.acknowledge_cross_entity,
        )
        await db.commit()
        return {
            "id": m.id, "status": m.status, "match_method": m.match_method,
            "confidence": str(m.confidence), "match_basis": m.match_basis,
            "confirmed_by": m.confirmed_by,
        }
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/matches/{match_id}/reject")
async def reject(match_id: int, data: MatchRejectIn,
                 db: AsyncSession = Depends(get_db)):
    try:
        m = await svc.reject_match(db, match_id, data.operator, data.comment)
        await db.commit()
        return {"id": m.id, "status": m.status}
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/level1/manual")
async def manual_level1(data: ManualLevel1In, db: AsyncSession = Depends(get_db)):
    try:
        m = await svc.create_manual_level1(
            db, batch_id=(await _line_batch(db, data.settlement_line_id)),
            business_event_id=data.business_event_id,
            settlement_line_id=data.settlement_line_id,
            operator=data.operator, comment=data.comment,
        )
        await db.commit()
        return {"id": m.id, "status": m.status, "match_basis": m.match_basis}
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.post("/level2/manual")
async def manual_level2(data: ManualLevel2In, db: AsyncSession = Depends(get_db)):
    try:
        # 从首条明细行定位批次，服务内还会再次校验归属
        from ..models import SettlementLine
        line = await db.get(SettlementLine, data.items[0].settlement_line_id)
        if line is None:
            raise HTTPException(400, "结算行不存在")
        created = await svc.create_manual_level2(
            db, batch_id=line.batch_id,
            bank_transaction_id=data.bank_transaction_id,
            items=[it.model_dump() for it in data.items],
            split_rule=data.split_rule,
            operator=data.operator, comment=data.comment,
            acknowledge_cross_entity=data.acknowledge_cross_entity,
            carry_forward_id=data.carry_forward_id,
        )
        await db.commit()
        return {
            "created": [
                {
                    "id": m.id, "settlement_line_id": m.settlement_line_id,
                    "bank_transaction_id": m.bank_transaction_id,
                    "allocated_amount": str(m.allocated_amount),
                    "split_rule": m.split_rule, "match_basis": m.match_basis,
                }
                for m in created
            ],
            "count": len(created),
        }
    except Exception as exc:
        await db.rollback()
        raise HTTPException(400, str(exc))


@router.get("/carry-forwards")
async def carry_forwards(batch_id: int | None = None, status: str | None = None,
                         db: AsyncSession = Depends(get_db)):
    return await svc._carry_forward_rows(db, batch_id=batch_id, status=status)


async def _line_batch(db: AsyncSession, line_id: int) -> int:
    from ..models import SettlementLine
    line = await db.get(SettlementLine, line_id)
    if line is None:
        raise HTTPException(400, "结算行不存在")
    return line.batch_id
