"""Otari's ``BillingPort`` on a Lago prepaid wallet, one wallet per Otari organization.

This replaces the wallet gate in Zuplo's ``set-litellm-headers.ts``: it provisions the same
customer, wallet and pay-as-you-go subscription, and refuses on the same ongoing balance. Lago
prices the request from the token events ``usage.py`` sends, as LiteLLM's callback did, and
deducts the wallet itself.

Holds and the cached balance are in Redis (``ledger.py``), so any number of Otari replicas share
one view of each wallet.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Mapping
from decimal import Decimal

from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.lago import Lago, LagoError, balance_of
from otari_publicai.ledger import Ledger
from otari_publicai import plans

logger = logging.getLogger(__name__)

# Every new organization's starter credit, as Public AI grants each new account today.
STARTER_CREDIT = Decimal(os.environ.get("PUBLICAI_STARTER_CREDIT", "2.00"))
# Zuplo refused at or below $0.10, so a request never starts on a nearly empty wallet.
MINIMUM_BALANCE = Decimal(os.environ.get("PUBLICAI_MINIMUM_BALANCE", "0.10"))
TOPUP_URL = os.environ.get("PUBLICAI_TOPUP_URL") or None

_lago: Lago | None = None
_ledger: Ledger | None = None
_plan_tasks: set[asyncio.Task] = set()


def lago() -> Lago:
    global _lago
    if _lago is None:
        _lago = Lago()
    return _lago


def ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger


async def _lago_balance(organization_id: uuid.UUID) -> Decimal:
    """The wallet's balance, from Redis when a replica read it in the last few seconds."""
    cached = await ledger().cached_balance(organization_id)
    if cached is not None:
        return cached
    wallet = await lago().active_wallet(str(organization_id))
    if wallet is None:
        # Not cached, so the wallet provisioning creates is read on the next call.
        return Decimal(0)
    await ledger().set_wallet_id(organization_id, wallet["lago_id"])
    balance = balance_of(wallet)
    await ledger().set_balance(organization_id, balance)
    return balance


async def available(organization_id: uuid.UUID) -> Decimal:
    """The wallet's ongoing balance, lowered by charges Lago has not counted yet."""
    return await _lago_balance(organization_id)


async def held(organization_id: uuid.UUID) -> Decimal:
    return await ledger().held(organization_id)


async def _wallet_id(organization_id: uuid.UUID) -> str:
    await LagoBillingAdapter(None).apply_due_credit(organization_id=organization_id)
    wallet_id = await ledger().wallet_id(organization_id)
    if wallet_id is None:
        await _lago_balance(organization_id)
        wallet_id = await ledger().wallet_id(organization_id)
    if wallet_id is None:
        raise LagoError(f"organization {organization_id} has no wallet")
    return wallet_id


async def grant(organization_id: uuid.UUID, amount: Decimal) -> Decimal:
    """Add ``amount`` of free credit to an organization's wallet and return the new balance."""
    wallet_id = await _wallet_id(organization_id)
    balance = await _lago_balance(organization_id)
    await lago().transact(wallet_id, granted=amount, name="Credit granted")
    # Lago settles a grant in a background job, so a read right after it still shows the old
    # balance. Cache the known one until the next read from Lago.
    await ledger().set_balance(organization_id, balance + amount)
    return balance + amount


async def buy(organization_id: uuid.UUID, amount: Decimal) -> dict:
    """Buy ``amount`` of credit. Lago invoices it and charges the card saved through checkout.

    The credit lands once the invoice is paid, which is Lago's and Stripe's business; the next
    balance read from Lago picks it up.
    """
    return await lago().transact(await _wallet_id(organization_id), paid=amount, name="Prepaid top-up")


async def checkout_url(organization_id: uuid.UUID) -> str | None:
    """Where the organization saves a card with the payment provider, or None without one."""
    await LagoBillingAdapter(None).apply_due_credit(organization_id=organization_id)
    return await lago().checkout_url(str(organization_id))


async def _ensure_plan_budget(organization_id: uuid.UUID) -> None:
    try:
        await plans.ensure_budget(ledger(), organization_id)
    except Exception as exc:  # retried on the organization's next request
        logger.warning("Plan setup failed for %s: %s", organization_id, exc)


class LagoBillingAdapter(BillingPort):
    """``BillingPort`` adapter that funds deployment-paid requests from a Lago wallet."""

    def __init__(self, session: AsyncSession | None) -> None:
        # The ledger lives in Lago and Redis; the session only puts users on their plan.
        self._session = session

    async def _apply_plan(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if not await plans.has_budget(ledger(), organization_id):
            # Off the request path: it calls Otari's own API, which must not wait on this request.
            task = asyncio.create_task(_ensure_plan_budget(organization_id))
            _plan_tasks.add(task)
            task.add_done_callback(_plan_tasks.discard)
        elif self._session is not None:
            await plans.assign(ledger(), self._session, organization_id, user_id)

    async def _refuse_below(self, organization_id: uuid.UUID, floor: Decimal) -> None:
        try:
            balance = await available(organization_id)
            in_flight = await held(organization_id)
        except (LagoError, OSError) as exc:
            logger.warning("Lago balance read failed for %s: %s", organization_id, exc)
            raise InsufficientFundsError(Decimal(0), "This organization's funds could not be checked.") from exc
        if balance - in_flight <= floor:
            raise InsufficientFundsError(max(balance, Decimal(0)), action_url=TOPUP_URL)

    async def apply_due_credit(self, *, organization_id: uuid.UUID) -> None:
        if await ledger().wallet_id(organization_id):
            return
        external_id = str(organization_id)
        try:
            await lago().ensure_customer(external_id, name=f"Otari organization {external_id}")
            await lago().ensure_subscription(external_id)
            wallet = await lago().active_wallet(external_id)
            if wallet is None:
                wallet = await lago().create_wallet(external_id, granted=STARTER_CREDIT)
                await ledger().set_balance(organization_id, STARTER_CREDIT)
            await ledger().set_wallet_id(organization_id, wallet["lago_id"])
        except (LagoError, OSError) as exc:
            # Provisioning is retried on the next request; the funds gate refuses meanwhile.
            logger.warning("Lago provisioning failed for %s: %s", organization_id, exc)

    async def require_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> None:
        if actor_user_id:
            await self._apply_plan(organization_id, actor_user_id)
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
        result = await ledger().hold(organization_id, amount)
        if result == -1:
            await _lago_balance(organization_id)
            result = await ledger().hold(organization_id, amount)
        if result != 1:
            raise InsufficientFundsError(max(await available(organization_id), Decimal(0)), action_url=TOPUP_URL)

    async def release_hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        await ledger().release(organization_id, amount)

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
        # Lago deducts it once it has priced the token events; until the balance is read again,
        # count it here so back-to-back requests do not all pass on the same cached balance.
        await ledger().lower_cached_balance(organization_id, amount)
