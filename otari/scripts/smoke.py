"""Walk a developer through the prototype: sign up, get a key, chat, run out of credit, top up.

Standard library only. Run against the compose stack in ../docker-compose.yml with Lago up:

    python3 scripts/smoke.py
"""

import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

OTARI = os.environ.get("OTARI_URL", "http://localhost:8100")
MAILPIT = os.environ.get("MAILPIT_URL", "http://localhost:8025")
LAGO = os.environ.get("LAGO_URL", "http://localhost:3000")
MODEL = os.environ.get("SMOKE_MODEL", "mistralai/mistral-small-4")
APERTUS = os.environ.get("SMOKE_APERTUS_MODEL", "swiss-ai/apertus-v1.5-70b")


def env_file() -> dict[str, str]:
    path = Path(__file__).resolve().parent.parent / ".env"
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return {k.strip(): v.strip() for k, v in pairs}


ENV = env_file()
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method: str, url: str, body: object = None, headers: dict[str, str] | None = None):
    """Return (status, headers, parsed body) without raising on an HTTP error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=120) as res:
            raw = res.read()
            return res.status, res.headers, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw.decode(errors="replace")
        return err.code, err.headers, parsed


def step(title: str) -> None:
    print(f"\n== {title}")


def check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        sys.exit(1)


def chat(key: str, model: str, content: str = "Say hello in Swiss German, in five words or fewer."):
    return call(
        "POST",
        f"{OTARI}/api/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 40},
        {"Authorization": f"Bearer {key}"},
    )


def verification_token(email: str) -> str:
    for _ in range(30):
        _, _, listing = call("GET", f"{MAILPIT}/api/v1/search?query=to:{email}")
        for message in (listing or {}).get("messages", []):
            _, _, full = call("GET", f"{MAILPIT}/api/v1/message/{message['ID']}")
            match = re.search(r"token=([A-Za-z0-9_\-.]+)", full.get("Text", "") + full.get("HTML", ""))
            if match:
                return match.group(1)
        time.sleep(1)
    raise SystemExit(f"no verification mail for {email}")


def main() -> None:
    master = {"Authorization": f"Bearer {ENV['OTARI_MASTER_KEY']}"}
    email = f"dev-{secrets.token_hex(4)}@publicai.example"
    password = secrets.token_urlsafe(18)

    step("A developer signs up and verifies their address")
    status, _, body = call("POST", f"{OTARI}/api/v1/auth/signup", {"email": email, "password": password, "terms_accepted": True})
    check(status < 300, f"signup {status}: {body}")
    status, _, body = call("POST", f"{OTARI}/api/v1/auth/verify-email", {"token": verification_token(email), "password": password})
    check(status < 300, f"verify-email {status}")
    status, _, body = call("POST", f"{OTARI}/api/v1/auth/session", {"email": email, "password": password})
    check(status < 300, f"signed in ({status})")

    step("They create an API key, and their organization's wallet holds the starter credit")
    status, _, body = call("POST", f"{OTARI}/api/v1/organizations/me/keys", {"key_name": "smoke"})
    check(status < 300, f"key created ({status}) {'' if status < 300 else body}")
    key = body["key"]
    status, _, wallet = call("GET", f"{OTARI}/api/v1/wallet")
    check(status == 200 and Decimal(wallet["balance_usd"]) > 0, f"wallet {status}: {wallet}")
    organization_id, starting = wallet["organization_id"], Decimal(wallet["balance_usd"])

    step(f"A chat completion on {MODEL}, served through otari.ai")
    status, headers, body = chat(key, MODEL)
    check(status == 200, f"{status}: {body if status != 200 else body['choices'][0]['message']['content']!r}")
    print(f"       Otari-Provider={headers.get('Otari-Provider')} Otari-Model={headers.get('Otari-Model')} cost=${body['usage'].get('cost_usd')}")
    status, _, wallet = call("GET", f"{OTARI}/api/v1/wallet")
    check(Decimal(wallet["balance_usd"]) < starting, f"the wallet was charged: ${starting} -> ${wallet['balance_usd']}")

    step(f"Apertus ({APERTUS})")
    status, headers, body = chat(key, APERTUS)
    if status == 200:
        check(True, f"served by {headers.get('Otari-Provider')}:{headers.get('Otari-Model')}")
    else:
        print(f"  skip {status}: Apertus is not on otari.ai yet ({str(body)[:160]})")

    step("The wallet runs dry, and the next request is refused with 402")
    wallets = call("GET", f"{LAGO}/api/v1/wallets?external_customer_id={organization_id}", headers={"Authorization": f"Bearer {ENV['LAGO_API_KEY']}"})[2]
    lago_wallet = wallets["wallets"][0]
    call(
        "POST",
        f"{LAGO}/api/v1/wallet_transactions",
        {"wallet_transaction": {"wallet_id": lago_wallet["lago_id"], "voided_credits": lago_wallet["credits_balance"]}},
        {"Authorization": f"Bearer {ENV['LAGO_API_KEY']}"},
    )
    time.sleep(21)  # the adapter caches a balance for 20 seconds
    status, headers, body = chat(key, MODEL)
    check(status == 402, f"{status}: {body}")

    step("An operator tops the wallet up, and requests are served again")
    status, _, wallet = call("POST", f"{OTARI}/api/v1/wallet/grants", {"organization_id": organization_id, "amount_usd": "1.00"}, master)
    check(status == 200, f"granted: {wallet}")
    status, _, body = chat(key, MODEL)
    check(status == 200, f"served again ({status})")

    # The free-plan rule is in config.yml and so read-only at runtime; a stricter per-key rule
    # added for this step shows the same refusal without a hundred requests.
    step("A per-key rate limit refuses with 429 (a temporary rule of 2 a minute)")
    status, _, body = call("POST", f"{OTARI}/api/v1/organizations/me/keys", {"key_name": "smoke-rate-limit"})
    fresh_key = body["key"]
    status, _, body = call("POST", f"{OTARI}/api/v1/rate-limits", {"name": "smoke-tight", "per": "key", "rpm": 2}, master)
    check(status < 300, f"temporary rule added ({status})")
    try:
        results = [chat(fresh_key, MODEL, "Reply with one word.") for _ in range(3)]
    finally:
        call("DELETE", f"{OTARI}/api/v1/rate-limits/smoke-tight", headers=master)
    statuses = [status for status, _, _ in results]
    check(statuses == [200, 200, 429], f"statuses {statuses}, Retry-After={results[-1][1].get('Retry-After')}")

    print("\nAll steps passed.")


if __name__ == "__main__":
    main()
