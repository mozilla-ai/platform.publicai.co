"""The wallet routes the developer portal calls, mounted behind the ``billing`` capability."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from gateway.api.deps import CallerOrganization, require_deployment_operator_or_absent, verify_master_key
from pydantic import BaseModel, Field

from otari_publicai import billing


class Wallet(BaseModel):
    organization_id: uuid.UUID
    balance_usd: Decimal
    held_usd: Decimal
    top_up_url: str | None


class Grant(BaseModel):
    organization_id: uuid.UUID
    amount_usd: Decimal = Field(gt=0, le=1000)


def wallet_router() -> APIRouter:
    router = APIRouter(prefix="/wallet", tags=["wallet"])

    @router.get("", response_model=Wallet)
    async def read_wallet(organization_id: CallerOrganization) -> Wallet:
        """The signed-in caller's organization's wallet."""
        await billing.LagoBillingAdapter(None).apply_due_credit(organization_id=organization_id)
        return Wallet(
            organization_id=organization_id,
            balance_usd=await billing.available(organization_id),
            held_usd=billing.held(organization_id),
            top_up_url=billing.TOPUP_URL,
        )

    @router.post(
        "/grants",
        response_model=Wallet,
        dependencies=[Depends(verify_master_key), Depends(require_deployment_operator_or_absent)],
    )
    async def grant_credit(body: Grant) -> Wallet:
        """Add free credit to an organization's wallet. Operators only.

        Stands in for a paid top-up, which Lago charges through Stripe once a payment provider is
        connected; Public AI's ``wallet-topup.ts`` is that flow today.
        """
        await billing.grant(body.organization_id, body.amount_usd)
        return Wallet(
            organization_id=body.organization_id,
            balance_usd=await billing.available(body.organization_id),
            held_usd=billing.held(body.organization_id),
            top_up_url=billing.TOPUP_URL,
        )

    return router
