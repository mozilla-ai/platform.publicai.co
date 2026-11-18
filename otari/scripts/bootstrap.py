"""Set up a fresh prototype as Public AI's operator, before any developer signs up.

- provisions the operator's organization, which pays for chat.publicai.co;
- loads otari.ai's model list onto the ``otari-ai`` hosted provider;
- creates the free budget every chat user starts on (LiteLLM's ``public_ai_free`` today) and the
  service key OpenWebUI calls Otari with, and writes that key to ``.env``;
- funds the operator's wallet so chat users can be served.

Standard library only:  python3 scripts/bootstrap.py
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke import ENV, OTARI, call  # noqa: E402

CHAT_DAILY_BUDGET_USD = float(os.environ.get("CHAT_DAILY_BUDGET_USD", "0.50"))
OPERATOR_CREDIT_USD = os.environ.get("OPERATOR_CREDIT_USD", "100.00")


def ok(status: int, body: object, what: str) -> object:
    if status >= 300:
        raise SystemExit(f"{what} failed ({status}): {body}")
    print(f"ok   {what}")
    return body


def main() -> None:
    master = {"Authorization": f"Bearer {ENV['OTARI_MASTER_KEY']}"}

    org = ok(*call("GET", f"{OTARI}/api/v1/organizations/me", headers=master)[::2], "operator organization")
    ok(*call("POST", f"{OTARI}/api/v1/hosted-providers/otari-ai/models/refresh", headers=master)[::2], "otari.ai models loaded")

    budget = ok(
        *call(
            "POST",
            f"{OTARI}/api/v1/budgets",
            {"name": "public_ai_free", "max_budget": CHAT_DAILY_BUDGET_USD, "budget_duration_sec": 86400},
            master,
        )[::2],
        f"chat users' budget, ${CHAT_DAILY_BUDGET_USD:.2f} a day",
    )
    key = ok(
        *call(
            "POST",
            f"{OTARI}/api/v1/keys",
            {"key_name": "chat.publicai.co", "is_service_key": True, "end_user_budget_id": budget["budget_id"]},
            master,
        )[::2],
        "service key for OpenWebUI",
    )
    ok(
        *call("POST", f"{OTARI}/api/v1/wallet/grants", {"organization_id": org["organization"]["id"], "amount_usd": OPERATOR_CREDIT_USD}, master)[::2],
        f"operator wallet funded with ${OPERATOR_CREDIT_USD}",
    )

    env_path = Path(__file__).resolve().parent.parent / ".env"
    text = env_path.read_text()
    line = f"OTARI_CHAT_SERVICE_KEY={key['key']}"
    text = re.sub(r"^OTARI_CHAT_SERVICE_KEY=.*$", line, text, flags=re.M) if "OTARI_CHAT_SERVICE_KEY=" in text else text + line + "\n"
    env_path.write_text(text)
    print("ok   OTARI_CHAT_SERVICE_KEY written to .env; run `docker compose up -d open-webui`")


if __name__ == "__main__":
    main()
