"""Otari's ``BillingPort`` on a Lago prepaid wallet, one wallet per Otari organization.

This replaces the wallet gate in Zuplo's ``set-litellm-headers.ts`` and the token events of
LiteLLM's ``custom_lago_callback.py``. Otari prices each request itself and hands the settled
dollar amount to :meth:`LagoBillingAdapter.charge`, so Lago no longer needs a per-model price
list: a charge voids that many credits from the wallet. Lago therefore invoices top-ups, and
Otari's usage rows say what each request cost.

Holds, unvoided charges and the cached balance are in Redis (``ledger.py``), so any number of
Otari replicas share one view of each wallet.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal

from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.lago import CREDIT_PLACES, Lago, LagoError, balance_of
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
    """The wallet's balance less what has been charged and not yet voided in Lago."""
    return await _lago_balance(organization_id) - await ledger().owed(organization_id)


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
        # Counted against the balance at once; voided in Lago below, or by a later charge.
        await ledger().add_owed(organization_id, amount)
        if await ledger().lock_voiding(organization_id):
            try:
                await self._void_owed(organization_id)
            except (LagoError, OSError) as exc:
                logger.warning("Lago charge of %s failed for %s: %s", amount, organization_id, exc)
            finally:
                await ledger().unlock_voiding(organization_id)
        logger.info(
            "charged %s to %s for %s (%s)",
            amount,
            organization_id,
            f"{billed.provider}:{billed.model}" if billed else "tools",
            description or "",
        )

    async def _void_owed(self, organization_id: uuid.UUID) -> None:
        wallet_id = await ledger().wallet_id(organization_id)
        if wallet_id is None:
            return
        owed = await ledger().owed(organization_id)
        # Lago takes whole credit steps, and no more than the wallet holds.
        voided = min(owed, await _lago_balance(organization_id)).quantize(CREDIT_PLACES, rounding=ROUND_DOWN)
        if voided <= 0:
            return
        await lago().transact(wallet_id, voided=voided, name="Usage")
        await ledger().settle_owed(organization_id, voided)
        await ledger().lower_cached_balance(organization_id, voided)
