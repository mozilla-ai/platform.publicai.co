# Otari in place of Zuplo and LiteLLM (prototype)

This directory runs the Public AI Inference Utility's API path on [Otari](https://github.com/mozilla-ai/otari)
instead of Zuplo in front of LiteLLM. One Otari process takes over both:

| Today | With Otari |
|---|---|
| Zuplo API keys, Auth0 sign-in | Otari signup (open signup: one organization per developer) and keys from `/api/v1/organizations/me/keys` or the dashboard |
| Zuplo per-plan rate limits (`user-rate-limiter.ts`) | Otari rate-limit rules (`config.yml`), refused with `429` and `Retry-After` |
| Zuplo wallet gate on Lago (`set-litellm-headers.ts`), `402` at $0.10 | The overlay in `overlay/`: Otari's `BillingPort` bound to a Lago wallet, `402 insufficient_funds` with the top-up link |
| LiteLLM `model_list` across Infomaniak, Phoeniqs, Featherless, Mistral, ... | Otari hosted providers. Here every model comes through otari.ai on one key; a host Public AI contracts with directly is one more entry (see `config.yml`) |
| LiteLLM cost per request, read back for billing (`billing-handler.ts`) | Otari prices each request (`usage.cost_usd`, `Otari-Cost-USD`) and charges the wallet as it settles |
| LiteLLM `custom_lago_callback.py` (token events per model) | Not needed: the wallet is charged in dollars, so Lago keeps no per-model price list |
| LiteLLM `custom_auth.py` (OpenWebUI users get the `public_ai_free` budget) | One Otari service key for OpenWebUI; each chat user is an end user of it on the `public_ai_free` budget. Needs the `otari_end_user` filter from the chat repo's `community/owui_functions/` |

The overlay is loaded with `OTARI_BOOTSTRAP=otari_publicai:register` and needs no change to Otari
itself. It is the same extension point otari.ai's own wallet uses.

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
docker compose restart otari    # bare catalog ids resolve only after a restart (see Known gaps)
docker compose up -d open-webui # http://localhost:8180; install the otari_end_user filter as a global filter
python3 scripts/smoke.py
```

| Service | URL |
|---|---|
| Otari API and dashboard | http://localhost:8100 |
| OpenWebUI | http://localhost:8180 |
| Mailpit (signup mail) | http://localhost:8025 |
| Lago | http://localhost:8081 (API on :3000) |

`scripts/smoke.py` walks a developer through the whole path and checks each step: sign up and
verify by mail, create a key, see the $2 starter credit, chat (served through otari.ai, charged to
the wallet), drain the wallet and get `402` with the top-up link, get topped up and be served
again, and hit a per-key rate limit (`429`).

## Models

Model ids are Public AI's (`mistralai/mistral-small-4`, `z-ai/glm-5.2`, ...): Otari picks the
provider for a `vendor/model` id. Apertus (`swiss-ai/apertus-v1.5-70b`) is not on otari.ai yet;
the smoke script skips it until otari.ai serves it through Infomaniak. Until then, add
Infomaniak as a hosted provider here, as the commented block in `config.yml` shows.

## Known gaps

- **Plan tiers.** Rate-limit rules apply per key, user, budget, model or deployment, not per
  plan. Free, Plus, Pro and Enterprise at 100/200/300/10,000 a minute need either a rule per plan
  scoped to that plan's organizations, or the overlay deciding the limit. Not done here: every
  key is on the free plan's 100 a minute.
- **Paid top-ups.** `POST /api/v1/wallet/grants` (operator only) grants free credit. A paid
  top-up is Lago charging Stripe for `paid_credits`, which Public AI's `wallet-topup.ts` already
  does; porting it is a route in the overlay.
- **One replica.** Holds and the short balance cache live in the Otari process. More replicas need
  them in Redis or Postgres.
- **Usage invoices.** A charge voids wallet credits rather than sending a Lago usage event, so Lago
  invoices top-ups but not usage. Otari keeps the per-request usage rows.
- **Model list refresh.** After `bootstrap.py` loads otari.ai's models, bare `vendor/model` ids are
  refused until Otari restarts, while `otari-ai:vendor/model` works at once. Worth fixing in Otari.
- **Developer portal.** The Zudoku portal in this repository still talks to Zuplo's key service;
  pointing its API key and billing pages at Otari's routes is not done here.
