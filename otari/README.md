# Otari in place of Zuplo and LiteLLM (prototype)

This directory runs the Public AI Inference Utility's API path on [Otari](https://github.com/mozilla-ai/otari)
instead of Zuplo in front of LiteLLM. One Otari process takes over both:

| Today | With Otari |
|---|---|
| Zuplo API keys, Auth0 sign-in | Otari signup (open signup: one organization per developer) and keys from `/api/v1/organizations/me/keys` or the dashboard |
| Zuplo per-plan rate limits (`user-rate-limiter.ts`) | The overlay's plans: free 100, plus 200, pro 300, enterprise 10,000 requests a minute per user, refused with `429` |
| Zuplo wallet gate on Lago (`set-litellm-headers.ts`), `402` at $0.10 | The overlay: Otari's `BillingPort` bound to a Lago wallet, `402 insufficient_funds` with the top-up link |
| Zuplo top-ups (`wallet-topup*.ts`, `customer-checkout-url-handler.ts`) | The overlay's wallet routes: save a card through Lago's Stripe checkout, buy credit, Lago invoices it |
| LiteLLM `model_list` across Infomaniak, Phoeniqs, Featherless, Mistral, ... | Otari hosted providers. Here every model comes through otari.ai on one key; the Helm chart in the chat repo lists Public AI's own hosts |
| LiteLLM cost per request, read back for billing (`billing-handler.ts`) | Otari prices each request (`usage.cost_usd`, `Otari-Cost-USD`) and charges the wallet as it settles |
| LiteLLM `custom_lago_callback.py` (token events per model) | Not needed: the wallet is charged in dollars, so Lago keeps no per-model price list |
| LiteLLM `custom_auth.py` (OpenWebUI users get the `public_ai_free` budget) | One Otari service key for OpenWebUI; each chat user is an end user of it on the `public_ai_free` budget. Needs the `otari_end_user` filter from the chat repo's `community/owui_functions/` |

The overlay is loaded with `OTARI_BOOTSTRAP=otari_publicai:register` and needs no change to Otari
itself. It is the same extension point otari.ai's own wallet uses. Its state shared between Otari
replicas (wallet holds, charges not yet sent to Lago, a 20-second balance cache) is in Redis.

## Overlay routes

| Route | Who | What |
|---|---|---|
| `GET /api/v1/wallet` | developer | balance, held amount, plan and its limit |
| `GET /api/v1/wallet/checkout` | developer | Lago's Stripe link to save a card (`null` without Stripe) |
| `POST /api/v1/wallet/top-ups` | developer | buy $5 to $1,000 of credit; lands when Lago's invoice is paid |
| `POST /api/v1/wallet/grants` | operator | add free credit |
| `PUT /api/v1/wallet/plan` | operator | move an organization to `free`, `plus`, `pro` or `enterprise` |

## Run it

Lago runs from its own compose file:

```bash
git clone https://github.com/getlago/lago.git && cd lago
# .env: LAGO_RSA_PRIVATE_KEY (openssl genrsa 2048 | openssl base64 -A), FRONT_PORT=8081,
# LAGO_CREATE_ORG=true, LAGO_ORG_API_KEY=lago_prototype_key, LAGO_ORG_USER_EMAIL/PASSWORD
docker compose up -d
```

Then, from this directory:

```bash
cp .env.example .env            # fill in OTARI_SECRET_KEY and OTARI_AI_API_KEY (an otari.ai key)
docker compose up -d otari
python3 scripts/bootstrap.py    # operator org, otari.ai models, chat budget and service key
docker compose up -d open-webui # http://localhost:8180; install the otari_end_user filter as a global filter
docker compose run -d --name otari-replica-2 -p 8101:8000 otari   # optional second replica
python3 scripts/smoke.py
```

`Dockerfile` builds the image the Helm chart runs: Otari with the overlay in it.

| Service | URL |
|---|---|
| Otari API and dashboard | http://localhost:8100 |
| OpenWebUI | http://localhost:8180 |
| Mailpit (signup mail) | http://localhost:8025 |
| Lago | http://localhost:8081 (API on :3000) |

`scripts/smoke.py` walks a developer through the whole path and checks each step: sign up and
verify by mail, create a key, see the $2 starter credit, chat (served through otari.ai, charged to
the wallet), drain the wallet and get `402` with the top-up link, get credit and be served again,
buy credit (the Lago invoice is marked paid by hand, as Stripe's webhook would), share the wallet
with a second replica, and hit the plan's limit (`429`).

## Models

Model ids are Public AI's (`mistralai/mistral-small-4`, `z-ai/glm-5.2`, ...): Otari picks the
provider for a `vendor/model` id. Apertus (`swiss-ai/apertus-v1.5-70b`) is not on otari.ai yet,
so the smoke script skips it. Public AI can serve it today by adding its Infomaniak account as a
hosted provider, as the commented block in `config.yml` shows.

Right after `bootstrap.py` loads otari.ai's models, bare `vendor/model` ids can take up to a
minute to resolve (`otari-ai:vendor/model` works at once). A fix in Otari is ready on the branch
`fix/hosted-refresh-catalog-ids`.

## Known gaps

- **Usage invoices.** A charge voids wallet credits rather than sending a Lago usage event, so Lago
  invoices top-ups but not usage. Otari keeps the per-request usage rows.
- **One write to Otari's tables.** Putting a user on their plan sets `users.budget_id` directly,
  because Otari's operator user routes only reach the operator's own organization. Otari limits
  requests per minute on a user's own budget and not on an organization ceiling.
- **Plans and Zuplo plans.** Plans are set by an operator (`PUT /api/v1/wallet/plan`). The Plus
  upgrade through OpenCollective is not wired up.
- **Developer portal.** The Zudoku portal in this repository still talks to Zuplo's key service;
  pointing its API key and billing pages at Otari's routes is not done here.
