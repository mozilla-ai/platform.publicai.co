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
        if await self._call("GET", f"/customers/{external_id}") is None:
            await self._call("POST", "/customers", json={"customer": {"external_id": external_id, "name": name}})

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

    async def transact(self, wallet_id: str, *, granted: Decimal = Decimal(0), voided: Decimal = Decimal(0)) -> None:
        transaction: dict[str, str] = {"wallet_id": wallet_id}
        if granted:
            transaction["granted_credits"] = str(granted.quantize(CREDIT_PLACES))
        if voided:
            transaction["voided_credits"] = str(voided.quantize(CREDIT_PLACES))
        await self._call("POST", "/wallet_transactions", json={"wallet_transaction": transaction})


def balance_of(wallet: dict[str, Any]) -> Decimal:
    """The credits a wallet holds, one credit to the dollar."""
    return Decimal(str(wallet.get("credits_balance") or "0"))
