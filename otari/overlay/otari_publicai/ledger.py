"""What every Otari replica must agree on between Lago calls, kept in Redis.

Per organization: the wallet's balance as last read from Lago (cached briefly), what in-flight
requests hold against it, and what has been charged but not yet voided in Lago. Amounts are
stored as integer nano-dollars so Redis can add them atomically.
"""

import os
import uuid
from decimal import Decimal

from redis.asyncio import Redis

NANO = Decimal(1_000_000_000)
BALANCE_TTL_SEC = 20
# A hold a crashed replica never released stops counting once nothing has touched it this long.
HELD_TTL_SEC = 900
VOID_LOCK_SEC = 10

# Claim ``amount`` if the cached balance, less what is owed and held, covers it.
# Returns -1 when no balance is cached (the caller reads Lago and retries), 0 when refused, 1 when held.
_HOLD = """
local balance = redis.call('GET', KEYS[1])
if not balance then return -1 end
local held = tonumber(redis.call('GET', KEYS[2]) or '0')
local owed = tonumber(redis.call('GET', KEYS[3]) or '0')
if tonumber(balance) - owed - held < tonumber(ARGV[1]) then return 0 end
redis.call('INCRBY', KEYS[2], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return 1
"""

_RELEASE = """
local held = tonumber(redis.call('GET', KEYS[1]) or '0') - tonumber(ARGV[1])
if held <= 0 then redis.call('DEL', KEYS[1]) else redis.call('SET', KEYS[1], held, 'KEEPTTL') end
return 1
"""


def to_nano(amount: Decimal) -> int:
    return int((amount * NANO).to_integral_value())


def from_nano(value: int | bytes | str | None) -> Decimal:
    return Decimal(int(value or 0)) / NANO


class Ledger:
    def __init__(self, redis: Redis | None = None) -> None:
        self.redis = redis or Redis.from_url(os.environ["PUBLICAI_REDIS_URL"])
        self._hold = self.redis.register_script(_HOLD)
        self._release = self.redis.register_script(_RELEASE)

    @staticmethod
    def _key(organization_id: uuid.UUID, name: str) -> str:
        return f"publicai:wallet:{organization_id}:{name}"

    async def cached_balance(self, organization_id: uuid.UUID) -> Decimal | None:
        value = await self.redis.get(self._key(organization_id, "balance"))
        return None if value is None else from_nano(value)

    async def set_balance(self, organization_id: uuid.UUID, balance: Decimal) -> None:
        await self.redis.set(self._key(organization_id, "balance"), to_nano(balance), ex=BALANCE_TTL_SEC)

    async def lower_cached_balance(self, organization_id: uuid.UUID, amount: Decimal) -> None:
        key = self._key(organization_id, "balance")
        if await self.redis.exists(key):
            await self.redis.decrby(key, to_nano(amount))

    async def held(self, organization_id: uuid.UUID) -> Decimal:
        return from_nano(await self.redis.get(self._key(organization_id, "held")))

    async def owed(self, organization_id: uuid.UUID) -> Decimal:
        return from_nano(await self.redis.get(self._key(organization_id, "owed")))

    async def hold(self, organization_id: uuid.UUID, amount: Decimal) -> int:
        keys = [self._key(organization_id, name) for name in ("balance", "held", "owed")]
        return int(await self._hold(keys=keys, args=[to_nano(amount), HELD_TTL_SEC]))

    async def release(self, organization_id: uuid.UUID, amount: Decimal) -> None:
        await self._release(keys=[self._key(organization_id, "held")], args=[to_nano(amount)])

    async def add_owed(self, organization_id: uuid.UUID, amount: Decimal) -> None:
        await self.redis.incrby(self._key(organization_id, "owed"), to_nano(amount))

    async def settle_owed(self, organization_id: uuid.UUID, amount: Decimal) -> None:
        await self.redis.decrby(self._key(organization_id, "owed"), to_nano(amount))

    async def lock_voiding(self, organization_id: uuid.UUID) -> bool:
        """One replica at a time voids an organization's owed amount, so none is voided twice."""
        return bool(await self.redis.set(self._key(organization_id, "void-lock"), 1, nx=True, ex=VOID_LOCK_SEC))

    async def unlock_voiding(self, organization_id: uuid.UUID) -> None:
        await self.redis.delete(self._key(organization_id, "void-lock"))

    async def wallet_id(self, organization_id: uuid.UUID) -> str | None:
        value = await self.redis.get(self._key(organization_id, "wallet-id"))
        return value.decode() if value else None

    async def set_wallet_id(self, organization_id: uuid.UUID, wallet_id: str) -> None:
        await self.redis.set(self._key(organization_id, "wallet-id"), wallet_id)
