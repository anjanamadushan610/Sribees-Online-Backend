"""
Wallet & Cashback API Endpoints

    GET /api/v1/wallet               - balance, pending cashback, points and level
    GET /api/v1/wallet/pending       - cashback on its way, order by order
    GET /api/v1/wallet/loyalty       - points, level, the next level, the ladder
    GET /api/v1/wallet/transactions  - paginated earned/spent history

Prefix "/wallet" is applied by app/api/v1/router.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_user
from app.schemas.wallet import (
    PendingCashbackItem,
    PendingCashbackResponse,
    WalletBalanceResponse,
    WalletTransactionResponse,
    WalletTransactionsPage,
    WalletTransactionsResponse,
)
from app.services.loyalty_service import LoyaltyService
from app.services.order_reward_service import OrderRewardService
from app.services.wallet_service import WalletService
from app.utils.i18n import resolve_language

router = APIRouter(tags=["Wallet"])


def _lang(request: Request) -> str:
    return resolve_language(request.headers.get("accept-language"))


@router.get("", response_model=WalletBalanceResponse)
async def get_wallet(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> WalletBalanceResponse:
    """The customer's wallet: spendable balance, cashback on its way, points."""
    try:
        balance = await WalletService.get_balance(db, current_user.user_id)
        pending = await OrderRewardService.pending_for_user(db, current_user.user_id)
        loyalty = await LoyaltyService.summary(db, current_user.user_id, _lang(request))
        return WalletBalanceResponse(
            balance=float(balance),
            currency=settings.wallet_currency,
            cashback_rate=settings.cashback_rate,
            pending_cashback=float(OrderRewardService.pending_total(pending)),
            points=loyalty["points"],
            tier=loyalty["tier"],
            next_tier=loyalty["next_tier"],
        )
    except Exception as e:
        logger.error(f"Error fetching wallet: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch wallet",
        )


@router.get("/pending", response_model=PendingCashbackResponse)
async def get_pending_cashback(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> PendingCashbackResponse:
    """Cashback held until each order's return window closes, newest first."""
    try:
        rows = await OrderRewardService.pending_for_user(db, current_user.user_id)
        return PendingCashbackResponse(
            data={
                "total": float(OrderRewardService.pending_total(rows)),
                "items": [PendingCashbackItem(**r).model_dump() for r in rows],
            }
        )
    except Exception as e:
        logger.error(f"Error fetching pending cashback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch pending cashback",
        )


@router.get("/loyalty", response_model=dict)
async def get_loyalty(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> dict:
    """Points, the current level, the next one, and every level on the ladder."""
    try:
        return {
            "success": True,
            "data": await LoyaltyService.summary(db, current_user.user_id, _lang(request)),
        }
    except Exception as e:
        logger.error(f"Error fetching loyalty summary: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch loyalty summary",
        )


@router.get("/transactions", response_model=WalletTransactionsResponse)
async def get_wallet_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(
        None,
        description="Filter by transaction type: 'earned', 'spent' or 'refund'",
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> WalletTransactionsResponse:
    """Get a paginated list of the user's wallet transactions (newest first)."""
    try:
        transactions, total = await WalletService.get_transactions(
            db,
            user_id=current_user.user_id,
            page=page,
            page_size=page_size,
            tx_type=type,
        )

        items = [
            WalletTransactionResponse(
                transaction_id=str(tx.transaction_id),
                type=tx.type,
                title=tx.title,
                amount=float(tx.amount),
                balance_after=float(tx.balance_after)
                if tx.balance_after is not None
                else None,
                order_id=str(tx.order_id) if tx.order_id else None,
                order_number=tx.order_number,
                created_at=tx.created_at,
            )
            for tx in transactions
        ]

        return WalletTransactionsResponse(
            data=WalletTransactionsPage(
                transactions=items,
                total=total,
                page=page,
                page_size=page_size,
            )
        )
    except Exception as e:
        logger.error(f"Error fetching wallet transactions: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch wallet transactions",
        )
