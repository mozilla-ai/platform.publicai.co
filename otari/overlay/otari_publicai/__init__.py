"""Public AI's Otari overlay: Lago billing as Zuplo and LiteLLM ran it.

Load it with ``OTARI_BOOTSTRAP=otari_publicai:register``.
"""

from gateway.container import Container, RouterContribution
from gateway.ports.billing_port import BillingPort
from gateway.ports.entitlement_port import EntitlementPort
from gateway.ports.usage_signal_port import UsageSignalPort
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.billing import LagoBillingAdapter
from otari_publicai.usage import LagoUsageSignal
from otari_publicai.wallet import wallet_router

BILLING = "billing"


class _PublicAIEntitlements(EntitlementPort):
    def __init__(self, session: AsyncSession | None) -> None:
        del session

    async def entitlements(self) -> set[str]:
        return {BILLING}


def register(container: Container) -> None:
    """Bind the Lago wallet and its usage events, and mount the wallet routes."""
    container.bind(BillingPort, LagoBillingAdapter)
    container.bind(UsageSignalPort, LagoUsageSignal)
    container.bind(EntitlementPort, _PublicAIEntitlements)
    container.contribute_router(RouterContribution(capability=BILLING, router=wallet_router()))
