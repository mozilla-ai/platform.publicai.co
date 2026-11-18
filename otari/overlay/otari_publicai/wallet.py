"""The wallet routes the developer portal calls, mounted behind the ``billing`` capability."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from gateway.api.deps import CallerOrganization, require_deployment_operator_or_absent, verify_master_key
from pydantic import BaseModel, Field

from otari_publicai import billing, plans
from otari_publicai.lago import LagoError


class Wallet(BaseModel):
    organization_id: uuid.UUID
    balance_usd: Decimal
    held_usd: Decimal
    top_up_url: str | None
    plan: str | None
    requests_per_minute: int | None


class PlanChange(BaseModel):
    organization_id: uuid.UUID
    plan: str = Field(pattern="^(" + "|".join(plans.PLANS) + ")$")


class Grant(BaseModel):
    organization_id: uuid.UUID
    amount_usd: Decimal = Field(gt=0, le=1000)


class TopUp(BaseModel):
    amount_usd: Decimal = Field(ge=5, le=1000)


class TopUpStarted(BaseModel):
    transaction_id: str | None
    status: str | None
    amount_usd: Decimal


class Checkout(BaseModel):
    checkout_url: str | None


async def _wallet(organization_id: uuid.UUID) -> Wallet:
    plan = await plans.get_plan(organization_id)
    return Wallet(
        organization_id=organization_id,
        balance_usd=await billing.available(organization_id),
        held_usd=await billing.held(organization_id),
        top_up_url=billing.TOPUP_URL,
        plan=plan,
        requests_per_minute=plans.PLANS.get(plan or ""),
    )


def wallet_router() -> APIRouter:
    router = APIRouter(prefix="/wallet", tags=["wallet"])

    @router.get("", response_model=Wallet)
    async def read_wallet(organization_id: CallerOrganization) -> Wallet:
        """The signed-in caller's organization's wallet."""
        await billing.LagoBillingAdapter(None).apply_due_credit(organization_id=organization_id)
        return await _wallet(organization_id)

    @router.get("/checkout", response_model=Checkout)
    async def card_checkout(organization_id: CallerOrganization) -> Checkout:
        """Where the caller saves a card for top-ups. ``null`` until a payment provider is connected."""
        return Checkout(checkout_url=await billing.checkout_url(organization_id))

    @router.post("/top-ups", response_model=TopUpStarted, status_code=status.HTTP_202_ACCEPTED)
    async def top_up(body: TopUp, organization_id: CallerOrganization) -> TopUpStarted:
        """Buy credit for the caller's organization, as Public AI's ``wallet-topup-handler.ts`` does.

        Lago invoices the amount and charges the saved card; the credit lands when the invoice
        is paid, so the answer is 202 with the pending transaction.
        """
        try:
            transaction = await billing.buy(organization_id, body.amount_usd)
        except LagoError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The top-up could not be started.") from exc
        return TopUpStarted(
            transaction_id=transaction.get("lago_id"), status=transaction.get("status"), amount_usd=body.amount_usd
        )

    @router.post(
        "/grants",
        response_model=Wallet,
        dependencies=[Depends(verify_master_key), Depends(require_deployment_operator_or_absent)],
    )
    async def grant_credit(body: Grant) -> Wallet:
        """Add free credit to an organization's wallet. Operators only."""
        await billing.grant(body.organization_id, body.amount_usd)
        return await _wallet(body.organization_id)

    @router.put(
        "/plan",
        response_model=Wallet,
        dependencies=[Depends(verify_master_key), Depends(require_deployment_operator_or_absent)],
    )
    async def change_plan(body: PlanChange) -> Wallet:
        """Move an organization to another plan. Operators only."""
        await plans.set_plan(body.organization_id, body.plan)
        return await _wallet(body.organization_id)

    return router
