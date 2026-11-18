"""Public AI's Otari overlay: a Lago prepaid wallet behind Otari's billing port.

Load it with ``OTARI_BOOTSTRAP=otari_publicai:register``.
"""

from gateway.container import Container, RouterContribution
from gateway.ports.billing_port import BillingPort
from gateway.ports.entitlement_port import EntitlementPort
from sqlalchemy.ext.asyncio import AsyncSession

from otari_publicai.billing import LagoBillingAdapter
from otari_publicai.wallet import wallet_router

BILLING = "billing"


class _PublicAIEntitlements(EntitlementPort):
    def __init__(self, session: AsyncSession | None) -> None:
        del session

    async def entitlements(self) -> set[str]:
        return {BILLING}


def register(container: Container) -> None:
    """Bind the Lago wallet and mount its routes."""
    container.bind(BillingPort, LagoBillingAdapter)
    container.bind(EntitlementPort, _PublicAIEntitlements)
    container.contribute_router(RouterContribution(capability=BILLING, router=wallet_router()))
