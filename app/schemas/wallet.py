"""
Wallet & Cashback Pydantic Schemas
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

# ============================================================================
# Response Schemas
# ============================================================================

class LoyaltyTierBrief(BaseModel):
    tier_id: str
    name: str
    min_points: int
    color: Optional[str] = None
    # Only on next_tier: points still to earn.
    points_needed: Optional[int] = None


class WalletBalanceResponse(BaseModel):
    """GET /wallet — balance, cashback on its way, and loyalty standing."""
    balance: float
    currency: str = "LKR"
    cashback_rate: float
    # Cashback held until each order's return window closes. Not spendable.
    pending_cashback: float = 0
    # Lifetime points; never go down.
    points: int = 0
    tier: Optional[LoyaltyTierBrief] = None
    next_tier: Optional[LoyaltyTierBrief] = None


class PendingCashbackItem(BaseModel):
    order_id: str
    order_number: Optional[str] = None
    amount: float
    # 'awaiting_delivery' | 'return_window' | 'return_in_progress'
    stage: str
    # When it reaches the wallet; only once the window is running.
    available_at: Optional[str] = None


class PendingCashbackResponse(BaseModel):
    success: bool = True
    data: dict


class WalletTransactionResponse(BaseModel):
    """A single wallet ledger entry."""
    transaction_id: str
    type: str  # 'earned' | 'spent' | 'refund'
    title: str
    amount: float
    balance_after: Optional[float] = None
    order_id: Optional[str] = None
    order_number: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WalletTransactionsPage(BaseModel):
    """Paginated wallet transaction list."""
    transactions: List[WalletTransactionResponse]
    total: int
    page: int
    page_size: int


class WalletTransactionsResponse(BaseModel):
    """Standard wrapper for the transactions list."""
    success: bool = True
    data: WalletTransactionsPage
