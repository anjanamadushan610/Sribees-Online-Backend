"""
Wallet & Cashback Service - Business Logic

Manages the per-user cashback wallet and its append-only transaction ledger.
Balance mutations always go through `_apply_transaction` so the ledger and the
running balance stay consistent.
"""
from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.wallet import Wallet, WalletTransaction, WalletTransactionType
from app.services import finance_outbox


class WalletService:
    """Service class for wallet operations."""

    @staticmethod
    async def get_or_create_wallet(db: AsyncSession, user_id: UUID) -> Wallet:
        """Return the user's wallet, creating an empty one on first access."""
        # All shopping-credit writers share this lock, including Partner transfers.
        # UPSERT avoids duplicate first-wallet creation under concurrent requests.
        await db.execute(insert(Wallet).values(user_id=user_id, balance=Decimal("0"),
            currency=settings.wallet_currency).on_conflict_do_nothing(index_elements=["user_id"]))
        result = await db.execute(select(Wallet).where(Wallet.user_id == user_id)
            .with_for_update().execution_options(populate_existing=True))
        wallet = result.scalar_one()

        return wallet

    @staticmethod
    async def apply_transaction(
        db: AsyncSession,
        user_id: UUID,
        tx_type: WalletTransactionType,
        amount: Decimal,
        title: str,
        order_id: Optional[UUID] = None,
        order_number: Optional[str] = None,
        notes: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> WalletTransaction:
        """
        Apply a movement to the wallet and record it in the ledger.

        `amount` is always positive; `tx_type` decides the direction.
        Does NOT commit — the caller owns the transaction boundary so wallet
        movements can be atomic with the order that produced them.

        ``reason`` (a ``finance_outbox.WalletReason``) says why, which decides
        how Sribees Finance books it; the finance event is staged with it.
        """
        wallet = await WalletService.get_or_create_wallet(db, user_id)

        amount = Decimal(str(amount)).quantize(Decimal("0.01"))

        if tx_type == WalletTransactionType.SPENT:
            wallet.balance = (wallet.balance or Decimal("0")) - amount
        else:  # EARNED or REFUND
            wallet.balance = (wallet.balance or Decimal("0")) + amount

        transaction = WalletTransaction(
            wallet_id=wallet.wallet_id,
            user_id=user_id,
            order_id=order_id,
            order_number=order_number,
            type=tx_type.value,
            title=title,
            amount=amount,
            balance_after=wallet.balance,
            notes=notes,
            reason=reason,
        )
        db.add(transaction)
        await db.flush()
        await finance_outbox.wallet_movement(db, transaction, reason)
        return transaction

    @staticmethod
    async def get_balance(db: AsyncSession, user_id: UUID) -> Decimal:
        """Return the current wallet balance (0 if no wallet yet)."""
        result = await db.execute(
            select(Wallet.balance).where(Wallet.user_id == user_id)
        )
        balance = result.scalar_one_or_none()
        return balance if balance is not None else Decimal("0")

    @staticmethod
    async def get_transactions(
        db: AsyncSession,
        user_id: UUID,
        page: int = 1,
        page_size: int = 20,
        tx_type: Optional[str] = None,
    ) -> Tuple[List[WalletTransaction], int]:
        """Return a page of the user's transactions plus the total count."""
        base = select(WalletTransaction).where(
            WalletTransaction.user_id == user_id
        )
        count_query = select(func.count(WalletTransaction.transaction_id)).where(
            WalletTransaction.user_id == user_id
        )

        if tx_type:
            base = base.where(WalletTransaction.type == tx_type)
            count_query = count_query.where(WalletTransaction.type == tx_type)

        total = (await db.execute(count_query)).scalar_one()

        offset = (page - 1) * page_size
        result = await db.execute(
            base.order_by(WalletTransaction.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        transactions = list(result.scalars().all())
        return transactions, total
