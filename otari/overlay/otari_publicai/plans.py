"""Public AI's plans: requests per minute per user, as Zuplo's ``user-rate-limiter.ts`` sets them.

Each organization has a budget, ``plan-<organization id>``, that holds only an ``rpm_limit`` and
belongs to the operator. Every user who makes a request in the organization gets it as their own
budget, which is where Otari enforces a per-minute limit (it does not on an organization
ceiling). Changing plan rewrites the budget's limit, for all of the organization's users at once.

The budget is made through Otari's operator API on this process, with the master key. Assigning
it to a user is the one place the overlay writes an Otari table (``users.budget_id``), because the
operator's user routes only reach users of the operator's own organization.
"""

import logging
import os
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.ledger import Ledger

logger = logging.getLogger(__name__)

PLANS: dict[str, int] = {"free": 100, "plus": 200, "pro": 300, "enterprise": 10_000}
DEFAULT_PLAN = "free"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{os.environ.get('OTARI_PORT', '8000')}/api/v1",
        headers={"Authorization": f"Bearer {os.environ['OTARI_MASTER_KEY']}"},
        timeout=10.0,
    )


def _budget_id(organization_id: uuid.UUID) -> str:
    return f"plan-{organization_id}"


def _budget_flag(organization_id: uuid.UUID) -> str:
    return f"publicai:plan-budget:{organization_id}"


def _user_flag(organization_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"publicai:plan-user:{organization_id}:{user_id}"


async def _put_budget(client: httpx.AsyncClient, organization_id: uuid.UUID, plan: str) -> None:
    response = await client.put(
        f"/budgets/{_budget_id(organization_id)}", json={"name": f"Plan: {plan}", "rpm_limit": PLANS[plan]}
    )
    response.raise_for_status()


async def _ensure_budget(client: httpx.AsyncClient, organization_id: uuid.UUID) -> None:
    if (await client.get(f"/budgets/{_budget_id(organization_id)}")).status_code == 404:
        await _put_budget(client, organization_id, DEFAULT_PLAN)


async def has_budget(ledger: Ledger, organization_id: uuid.UUID) -> bool:
    return bool(await ledger.redis.exists(_budget_flag(organization_id)))


async def ensure_budget(ledger: Ledger, organization_id: uuid.UUID) -> None:
    """Give a new organization its plan budget, on the default plan."""
    async with _client() as client:
        await _ensure_budget(client, organization_id)
    await ledger.redis.set(_budget_flag(organization_id), 1)


async def assign(ledger: Ledger, session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Make the organization's plan budget the user's own, in the request's transaction.

    It binds from the user's next request. The flag expires, so a write the request rolled back
    is made again.
    """
    if await ledger.redis.exists(_user_flag(organization_id, user_id)):
        return
    await session.execute(
        text("UPDATE users SET budget_id = :budget WHERE user_id = :user AND budget_id IS DISTINCT FROM :budget"),
        {"budget": _budget_id(organization_id), "user": str(user_id)},
    )
    await ledger.redis.set(_user_flag(organization_id, user_id), 1, ex=300)


async def set_plan(organization_id: uuid.UUID, plan: str) -> None:
    async with _client() as client:
        await _ensure_budget(client, organization_id)
        await _put_budget(client, organization_id, plan)


async def get_plan(organization_id: uuid.UUID) -> str | None:
    async with _client() as client:
        response = await client.get(f"/budgets/{_budget_id(organization_id)}")
    if response.status_code != 200:
        return None
    name = response.json().get("name") or ""
    return name.removeprefix("Plan: ") or None
