"""Otari's ``BillingPort`` on a Lago prepaid wallet, one wallet per Otari organization.

This replaces the wallet gate in Zuplo's ``set-litellm-headers.ts`` and the token events of
LiteLLM's ``custom_lago_callback.py``. Otari prices each request itself and hands the settled
dollar amount to :meth:`LagoBillingAdapter.charge`, so Lago no longer needs a per-model price
list: a charge voids that many credits from the wallet.

Prototype limits, each named where it applies: holds and the balance cache live in this
process, so the adapter is correct for one Otari replica only, and a charge voids credits
rather than raising a Lago usage event, so Lago invoices top-ups but not usage.
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.lago import CREDIT_PLACES, Lago, LagoError, balance_of

logger = logging.getLogger(__name__)

# Every new organization's starter credit, as Public AI grants each new account today.
STARTER_CREDIT = Decimal(os.environ.get("PUBLICAI_STARTER_CREDIT", "2.00"))
# Zuplo refused at or below $0.10, so a request never starts on a nearly empty wallet.
MINIMUM_BALANCE = Decimal(os.environ.get("PUBLICAI_MINIMUM_BALANCE", "0.10"))
TOPUP_URL = os.environ.get("PUBLICAI_TOPUP_URL") or None
BALANCE_TTL_SEC = 20.0


@dataclass
class _Ledger:
    """What this process knows about each organization's wallet between Lago calls."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    provisioned: set[uuid.UUID] = field(default_factory=set)
    wallet_ids: dict[uuid.UUID, str] = field(default_factory=dict)
    balances: dict[uuid.UUID, tuple[Decimal, float]] = field(default_factory=dict)
    held: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    # Charged but not yet voided in Lago: an amount under Lago's smallest credit step, or one a
    # failed Lago call left behind. Counted against the balance until it is voided.
    owed: dict[uuid.UUID, Decimal] = field(default_factory=dict)


_ledger = _Ledger()
_lago: Lago | None = None


def lago() -> Lago:
    global _lago
    if _lago is None:
        _lago = Lago()
    return _lago


async def _lago_balance(organization_id: uuid.UUID, *, fresh: bool = False) -> Decimal:
    cached = _ledger.balances.get(organization_id)
    if cached and not fresh and time.monotonic() - cached[1] < BALANCE_TTL_SEC:
        return cached[0]
    wallet = await lago().active_wallet(str(organization_id))
    if wallet is None:
        # Not cached, so the wallet provisioning creates is read on the next call.
        return Decimal(0)
    _ledger.wallet_ids[organization_id] = wallet["lago_id"]
    balance = balance_of(wallet)
    _ledger.balances[organization_id] = (balance, time.monotonic())
    return balance


async def available(organization_id: uuid.UUID) -> Decimal:
    """The wallet's balance less what this process has charged and not yet voided."""
    return await _lago_balance(organization_id) - _ledger.owed.get(organization_id, Decimal(0))


def held(organization_id: uuid.UUID) -> Decimal:
    return _ledger.held.get(organization_id, Decimal(0))


async def grant(organization_id: uuid.UUID, amount: Decimal) -> Decimal:
    """Add ``amount`` of free credit to an organization's wallet and return the new balance."""
    await LagoBillingAdapter(None).apply_due_credit(organization_id=organization_id)
    async with _ledger.lock:
        balance = await _lago_balance(organization_id)
        await lago().transact(_ledger.wallet_ids[organization_id], granted=amount)
        _note_granted(organization_id, balance + amount)
    return balance + amount


def _note_granted(organization_id: uuid.UUID, balance: Decimal) -> None:
    # Lago settles a grant in a background job, so a read right after it still shows the old
    # balance. Hold the known one until the cache would expire anyway.
    _ledger.balances[organization_id] = (balance, time.monotonic())


class LagoBillingAdapter(BillingPort):
    """``BillingPort`` adapter that funds deployment-paid requests from a Lago wallet."""

    def __init__(self, session: AsyncSession | None) -> None:
        # The ledger lives in Lago and in this process, never in Otari's database.
        del session

    async def _refuse_below(self, organization_id: uuid.UUID, floor: Decimal) -> None:
        try:
            balance = await available(organization_id)
        except (LagoError, OSError) as exc:
            logger.warning("Lago balance read failed for %s: %s", organization_id, exc)
            raise InsufficientFundsError(Decimal(0), "This organization's funds could not be checked.") from exc
        if balance - held(organization_id) <= floor:
            raise InsufficientFundsError(max(balance, Decimal(0)), action_url=TOPUP_URL)

    async def apply_due_credit(self, *, organization_id: uuid.UUID) -> None:
        if organization_id in _ledger.provisioned:
            return
        external_id = str(organization_id)
        try:
            await lago().ensure_customer(external_id, name=f"Otari organization {external_id}")
            if await lago().active_wallet(external_id) is None:
                wallet = await lago().create_wallet(external_id, granted=STARTER_CREDIT)
                _ledger.wallet_ids[organization_id] = wallet["lago_id"]
                _note_granted(organization_id, STARTER_CREDIT)
        except (LagoError, OSError) as exc:
            # Provisioning is retried on the next request; the funds gate refuses meanwhile.
            logger.warning("Lago provisioning failed for %s: %s", organization_id, exc)
            return
        _ledger.provisioned.add(organization_id)

    async def require_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> None:
        await self._refuse_below(organization_id, MINIMUM_BALANCE)

    async def has_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> bool:
        try:
            await self._refuse_below(organization_id, MINIMUM_BALANCE)
        except InsufficientFundsError:
            return False
        return True

    async def concurrency_limit(
        self, *, organization_id: uuid.UUID, billed: BilledModel, actor_user_id: uuid.UUID | None
    ) -> int | None:
        return None

    async def require_unheld_funds(self, *, organization_id: uuid.UUID) -> None:
        await self._refuse_below(organization_id, Decimal(0))

    async def hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        async with _ledger.lock:
            balance = await available(organization_id)
            if balance - held(organization_id) < amount:
                raise InsufficientFundsError(max(balance, Decimal(0)), action_url=TOPUP_URL)
            _ledger.held[organization_id] = held(organization_id) + amount

    async def release_hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        async with _ledger.lock:
            _ledger.held[organization_id] = max(held(organization_id) - amount, Decimal(0))

    async def charge(
        self,
        *,
        organization_id: uuid.UUID,
        amount: Decimal,
        billed: BilledModel | None,
        tools: Mapping[str, ToolUse],
        description: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        api_key_id: str | None = None,
    ) -> None:
        if amount <= 0:
            return
        async with _ledger.lock:
            owed = _ledger.owed.get(organization_id, Decimal(0)) + amount
            _ledger.owed[organization_id] = owed
            try:
                lago_balance = await _lago_balance(organization_id)
                wallet_id = _ledger.wallet_ids.get(organization_id)
                # Void what Lago can take: whole credit steps, and no more than the wallet holds.
                voided = min(owed, lago_balance).quantize(CREDIT_PLACES, rounding="ROUND_DOWN")
                if wallet_id and voided > 0:
                    await lago().transact(wallet_id, voided=voided)
                    _ledger.owed[organization_id] = owed - voided
                    _ledger.balances[organization_id] = (lago_balance - voided, time.monotonic())
            except (LagoError, OSError) as exc:
                # Kept as owed, so the next charge voids it and the gates count it meanwhile.
                logger.warning("Lago charge of %s failed for %s: %s", amount, organization_id, exc)
        logger.info(
            "charged %s to %s for %s (%s)",
            amount,
            organization_id,
            f"{billed.provider}:{billed.model}" if billed else "tools",
            description or "",
        )
