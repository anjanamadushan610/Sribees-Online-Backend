"""
Referral API Endpoints

  GET  /referrals/me                    — my code, totals and invited friends
  GET  /referrals/friends/{user_id}     — per-order commissions from one friend
  POST /referrals/redeem                — attach myself to someone's code
  POST /referrals/claim                 — move claimable commission to my wallet

**Authorization model:** every query is scoped by the authenticated user id
taken from the token. No endpoint accepts a beneficiary id from the client, so
there is nothing to tamper with to read someone else's earnings.

**Privacy model:** a referrer sees their friend's name, a short member id, the
date and their own commission. Never the friend's phone, email, or what an
order was worth. Those are simply not selected — the exclusion is in the
service query, not left to a serializer to remember.

Prefix "/referrals" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_user
from app.schemas.referral import RedeemReferralRequest
from app.services.referral_commission import CommissionRateResolver
from app.services.referral_commission.presenter import offer_text
from app.services.referral_service import ReferralError, ReferralService

router = APIRouter(tags=["Referrals"])


def _referral_error(exc: ReferralError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "success": False,
            "error": {"message": str(exc), "code": exc.code},
        },
    )


# ============================================================================
# GET /me
# ============================================================================

@router.get("/me", response_model=dict)
async def get_my_referrals(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Everything the Invite Friends screen needs in one call.

    The code is generated on first view rather than at signup, so accounts that
    never open this screen do not consume codes.
    """
    code = await ReferralService.get_or_create_code(db, current_user)
    totals = await ReferralService.get_summary(db, current_user.user_id)
    friends = await ReferralService.list_friends(db, current_user.user_id)
    highest = await CommissionRateResolver.highest_live_rate(db)

    return {
        "success": True,
        "data": {
            "referralCode": code,
            # The best rate on offer today: rates are per product now. Kept
            # for app builds that still word the sentence themselves; newer
            # ones show commissionText, in the Accept-Language language.
            "commissionPercent": float(highest),
            "commissionText": offer_text(
                highest,
                audience="friend",
                accept_language=request.headers.get("accept-language"),
            ),
            "currency": settings.wallet_currency,
            "totals": {
                # Accrued but not yet delivered — shown so the referrer can see
                # earnings coming, but it is not spendable.
                "pending": float(totals["pending"]),
                # Delivered. This is what the claim button moves.
                "claimable": float(totals["claimable"]),
                "paid": float(totals["paid"]),
            },
            "friendCount": len(friends),
            "friends": friends,
        },
    }


# ============================================================================
# GET /friends/{friend_user_id}
# ============================================================================

@router.get("/friends/{friend_user_id}", response_model=dict)
async def get_commissions_from_friend(
    friend_user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Per-order commissions this user earned from one invited friend.

    Returns an empty list rather than 404 for a friend that is not theirs: a
    404-vs-empty distinction would confirm whether a given user id exists.
    """
    commissions = await ReferralService.list_commissions_from_friend(
        db, current_user.user_id, friend_user_id
    )
    return {
        "success": True,
        "data": {
            "commissions": commissions,
            "total": len(commissions),
        },
    }


# ============================================================================
# POST /redeem
# ============================================================================

@router.post("/redeem", response_model=dict)
async def redeem_referral_code(
    body: RedeemReferralRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Attach the caller to the owner of a code.

    Sign-up (phone OTP, Google or Apple) takes no code, so this is how a new
    customer -- or one who signed up before being given a code -- is
    attached to whoever invited them.

    Write-once: a second call always fails, whatever code is supplied.
    """
    try:
        await ReferralService.bind_referrer(db, current_user, body.referral_code)
    except ReferralError as exc:
        raise _referral_error(exc)

    return {
        "success": True,
        "message": "Invite code applied.",
    }


# ============================================================================
# POST /claim
# ============================================================================

@router.post("/claim", response_model=dict)
async def claim_commission(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Move every claimable commission into the caller's wallet.

    Idempotent by construction: the rows are locked before crediting, so a
    double-tap cannot pay twice — the second request simply finds nothing
    claimable and returns zero.
    """
    result = await ReferralService.claim(db, current_user.user_id)

    if result["claimed_count"] == 0:
        return {
            "success": True,
            "data": {"claimedCount": 0, "amount": 0.0},
            "message": "Nothing to claim right now.",
        }

    logger.info(
        f"Referral claim: {result['amount']} to user {current_user.user_id}"
    )
    return {
        "success": True,
        "data": {
            "claimedCount": result["claimed_count"],
            "amount": float(result["amount"]),
        },
        "message": f"{result['amount']} added to your wallet.",
    }
