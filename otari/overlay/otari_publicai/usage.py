"""Token events to Lago for each request Otari served, as LiteLLM's ``custom_lago_callback.py`` sent.

Lago prices them from Public AI's catalog (``charts/platform/lago-catalog.yaml``) and deducts the
wallet, so the portal's usage per model, its wallet transactions and its invoices read as they do
today.

The model is the catalog name: ``PUBLICAI_LAGO_MODELS`` maps a served ``provider:model`` or
``model`` to it (``a=b,c=d``), and an unmapped model goes as served, with the callback's clean-up.
"""

import asyncio
import logging
import os
import uuid

from gateway.core.database import create_session
from gateway.ports.usage_signal_port import RecordedUsage, UsageSignalPort
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.billing import lago

logger = logging.getLogger(__name__)

MODEL_NAMES: dict[str, str] = dict(
    pair.split("=", 1) for pair in os.environ.get("PUBLICAI_LAGO_MODELS", "").split(",") if "=" in pair
)

_organizations: dict[uuid.UUID, uuid.UUID] = {}
_tasks: set[asyncio.Task] = set()


def catalog_model(provider: str | None, model: str) -> str:
    for key in (f"{provider}:{model}", model):
        if key in MODEL_NAMES:
            return MODEL_NAMES[key]
    clean = model.removeprefix("openai/")
    return clean.split(":")[0] if ":" in clean else clean


async def _organization_of(workspace_id: uuid.UUID) -> uuid.UUID | None:
    if workspace_id not in _organizations:
        # A read of Otari's own table: the usage facts carry no organization.
        async with create_session() as session:
            row = await session.execute(
                text("SELECT organization_id FROM workspace WHERE id = :id"), {"id": workspace_id}
            )
            organization_id = row.scalar_one_or_none()
        if organization_id is None:
            return None
        _organizations[workspace_id] = organization_id
    return _organizations[workspace_id]


async def _send(usage: RecordedUsage) -> None:
    try:
        organization_id = await _organization_of(usage.workspace_id)
        if organization_id is None:
            return
        model = catalog_model(usage.provider, usage.model)
        for tokens, kind in ((usage.prompt_tokens, "input"), (usage.completion_tokens, "output")):
            if tokens:
                await lago().send_tokens(str(organization_id), model, tokens, kind)
    except Exception as exc:  # noqa: BLE001  never reaches the request
        logger.warning("Lago usage events failed for %s: %s", usage.usage_id, exc)


class LagoUsageSignal(UsageSignalPort):
    """Reports each request the deployment paid for to Lago, off the request path."""

    def __init__(self, session: AsyncSession | None) -> None:
        del session

    async def usage_recorded(self, usage: RecordedUsage) -> None:
        # Only what the deployment pays for is billed, and only an attempt that served.
        if not usage.deployment_paid or usage.status != "success":
            return
        task = asyncio.create_task(_send(usage))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
