"""Shared desktop model-access and proactive quota policy primitives."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import Depends, HTTPException

from database import redis_db, users as users_db
from models.users import PlanType, Subscription
from utils.executors import critical_executor, db_executor, run_blocking
from utils.other.endpoints import get_current_user_uid
from utils.subscription import (
    DESKTOP_ACCESS_TIER_ARCHITECT,
    DESKTOP_ACCESS_TIER_FREE,
    DESKTOP_ACCESS_TIER_FULL,
    effective_desktop_access_tier,
    is_desktop_trial_paywalled,
)

logger = logging.getLogger(__name__)

DESKTOP_PROACTIVE_QUOTA_WINDOW_SECONDS = 24 * 60 * 60
REALTIME_RELAY_CONNECT_WINDOW_SECONDS = 60
REALTIME_RELAY_CONNECT_BURST_LIMIT = 6
REALTIME_RELAY_LEASE_GRACE_SECONDS = 30
DESKTOP_PROACTIVE_DAILY_LIMITS: dict[str, dict[str, int]] = {
    DESKTOP_ACCESS_TIER_FREE: {'proactive_extraction': 150, 'proactive_reasoning': 60},
    DESKTOP_ACCESS_TIER_FULL: {'proactive_extraction': 1000, 'proactive_reasoning': 500},
    DESKTOP_ACCESS_TIER_ARCHITECT: {'proactive_extraction': 2000, 'proactive_reasoning': 1000},
}


@dataclass(frozen=True)
class DesktopModelQuotaReservation:
    operation: str
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        return {
            'X-Proactive-Quota-Limit': str(self.limit),
            'X-Proactive-Quota-Remaining': str(self.remaining),
            'X-Proactive-Quota-Reset': str(self.reset_seconds),
        }


@dataclass(frozen=True)
class RealtimeRelayAdmission:
    token: str
    lease_ttl_seconds: int


def _customer_subscription(uid: str) -> Subscription | None:
    return users_db.get_user_valid_subscription(uid)


def desktop_proactive_quota_limit(
    operation: str,
    subscription: Subscription | None,
    *,
    tier_resolver: Callable[[Any, Subscription | None], str] = effective_desktop_access_tier,
) -> int:
    plan = subscription.plan if subscription is not None else PlanType.basic
    tier = tier_resolver(plan, subscription)
    limits = DESKTOP_PROACTIVE_DAILY_LIMITS.get(tier, DESKTOP_PROACTIVE_DAILY_LIMITS[DESKTOP_ACCESS_TIER_FREE])
    try:
        return limits[operation]
    except KeyError as error:
        raise ValueError(f'unsupported desktop model quota operation {operation!r}') from error


async def enforce_desktop_model_access(
    uid: str,
    *,
    runner: Callable[..., Awaitable[Any]] = run_blocking,
    paywall_checker: Callable[..., bool] = is_desktop_trial_paywalled,
) -> str:
    if await runner(db_executor, paywall_checker, uid, 'desktop'):
        raise HTTPException(status_code=402, detail='trial_expired')
    return uid


async def authorized_desktop_model_user(uid: str = Depends(get_current_user_uid)) -> str:
    return await enforce_desktop_model_access(uid)


async def reserve_desktop_proactive_quota(
    uid: str,
    operation: str,
    *,
    runner: Callable[..., Awaitable[Any]] = run_blocking,
    subscription_loader: Callable[[str], Subscription | None] = _customer_subscription,
    reserve: Callable[..., tuple[bool, int, int]] = redis_db.reserve_rate_limit,
    tier_resolver: Callable[[Any, Subscription | None], str] = effective_desktop_access_tier,
) -> DesktopModelQuotaReservation:
    try:
        subscription = await runner(db_executor, subscription_loader, uid)
        limit = desktop_proactive_quota_limit(operation, subscription, tier_resolver=tier_resolver)
        allowed, remaining, reset_seconds = await runner(
            critical_executor,
            reserve,
            uid,
            f'desktop_{operation}',
            limit,
            DESKTOP_PROACTIVE_QUOTA_WINDOW_SECONDS,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=503, detail='Proactive metering is temporarily unavailable') from error
    reservation = DesktopModelQuotaReservation(
        operation=operation,
        limit=limit,
        remaining=remaining,
        reset_seconds=reset_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail='Proactive request limit exceeded',
            headers={**reservation.headers(), 'Retry-After': str(reset_seconds)},
        )
    return reservation


async def release_desktop_proactive_quota(
    uid: str,
    operation: str,
    *,
    runner: Callable[..., Awaitable[Any]] = run_blocking,
    release: Callable[..., Any] = redis_db.release_rate_limit,
) -> None:
    try:
        await runner(critical_executor, release, uid, f'desktop_{operation}')
    except Exception:
        logger.exception('failed to release desktop model quota uid=%s operation=%s', uid, operation)


async def admit_realtime_relay(
    uid: str,
    max_session_seconds: int,
    *,
    runner: Callable[..., Awaitable[Any]] = run_blocking,
    connect_limiter: Callable[..., tuple[bool, int, int]] = redis_db.check_rate_limit,
    lease_acquirer: Callable[..., bool] = redis_db.try_acquire_realtime_relay_lease,
) -> RealtimeRelayAdmission:
    """Enforce a burst limit plus one cross-instance upstream socket per user."""

    lease_ttl_seconds = max_session_seconds + REALTIME_RELAY_LEASE_GRACE_SECONDS
    token = secrets.token_urlsafe(24)
    try:
        allowed, _remaining, retry_after = await runner(
            critical_executor,
            connect_limiter,
            uid,
            'desktop_realtime_relay_connect',
            REALTIME_RELAY_CONNECT_BURST_LIMIT,
            REALTIME_RELAY_CONNECT_WINDOW_SECONDS,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail='Realtime relay connection rate exceeded',
                headers={'Retry-After': str(retry_after)},
            )
        acquired = await runner(
            critical_executor,
            lease_acquirer,
            uid,
            token,
            lease_ttl_seconds,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=503, detail='Realtime relay admission is temporarily unavailable') from error
    if not acquired:
        raise HTTPException(
            status_code=429,
            detail='Realtime relay concurrent session limit exceeded',
            headers={'Retry-After': str(lease_ttl_seconds)},
        )
    return RealtimeRelayAdmission(token=token, lease_ttl_seconds=lease_ttl_seconds)


async def release_realtime_relay(
    uid: str,
    admission: RealtimeRelayAdmission,
    *,
    runner: Callable[..., Awaitable[Any]] = run_blocking,
    lease_releaser: Callable[..., bool] = redis_db.release_realtime_relay_lease,
) -> None:
    try:
        await runner(critical_executor, lease_releaser, uid, admission.token)
    except Exception:
        logger.exception('failed to release realtime relay lease uid=%s', uid)
