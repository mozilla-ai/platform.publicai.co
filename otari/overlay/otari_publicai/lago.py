"""A small async client for the parts of Lago's API the wallet uses."""

import os
from decimal import Decimal
from typing import Any

import httpx

# Lago keeps wallet credits to five decimal places.
CREDIT_PLACES = Decimal("0.00001")


class LagoError(Exception):
    """Raised when Lago answers a call with an unexpected status."""


class Lago:
    """Customers, wallets and wallet transactions, keyed by the customer's external id."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=(base_url or os.environ["LAGO_API_BASE"]).rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {api_key or os.environ['LAGO_API_KEY']}"},
            timeout=httpx.Timeout(5.0, connect=2.0),
        )

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise LagoError(f"{method} {path} answered {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    async def ensure_customer(self, external_id: str, name: str) -> None:
        if await self._call("GET", f"/customers/{external_id}") is not None:
            return
        customer: dict[str, Any] = {"external_id": external_id, "name": name}
        if provider_code := os.environ.get("STRIPE_PAYMENT_PROVIDER_CODE"):
            # As Public AI's customer-provisioning.ts does: Lago creates the Stripe customer.
            customer["billing_configuration"] = {
                "payment_provider": "stripe",
                "payment_provider_code": provider_code,
                "sync_with_provider": True,
            }
        await self._call("POST", "/customers", json={"customer": customer})

    async def checkout_url(self, external_id: str) -> str | None:
        """Lago's link for saving a card with the customer's payment provider."""
        try:
            body = await self._call("GET", f"/customers/{external_id}/checkout_url")
        except LagoError:
            # Answered when the customer has no payment provider.
            return None
        return ((body or {}).get("customer") or {}).get("checkout_url")

    async def active_wallet(self, external_id: str) -> dict[str, Any] | None:
        body = await self._call("GET", "/wallets", params={"external_customer_id": external_id})
        wallets = (body or {}).get("wallets", [])
        return next((w for w in wallets if w.get("status") == "active"), None)

    async def create_wallet(self, external_id: str, granted: Decimal) -> dict[str, Any]:
        body = await self._call(
            "POST",
            "/wallets",
            json={
                "wallet": {
                    "external_customer_id": external_id,
                    "name": "Credits",
                    "currency": "USD",
                    "rate_amount": "1",
                    "granted_credits": str(granted),
                }
            },
        )
        return (body or {})["wallet"]

    async def transact(
        self,
        wallet_id: str,
        *,
        granted: Decimal = Decimal(0),
        paid: Decimal = Decimal(0),
        voided: Decimal = Decimal(0),
        name: str | None = None,
    ) -> dict[str, Any]:
        """Add or remove credits. Paid credits are invoiced and land once the invoice is paid."""
        transaction: dict[str, str] = {"wallet_id": wallet_id}
        for field_name, amount in (("granted_credits", granted), ("paid_credits", paid), ("voided_credits", voided)):
            if amount:
                transaction[field_name] = str(amount.quantize(CREDIT_PLACES))
        if name:
            transaction["name"] = name
        body = await self._call("POST", "/wallet_transactions", json={"wallet_transaction": transaction})
        return ((body or {}).get("wallet_transactions") or [{}])[0]


def balance_of(wallet: dict[str, Any]) -> Decimal:
    """The credits a wallet holds, one credit to the dollar."""
    return Decimal(str(wallet.get("credits_balance") or "0"))
