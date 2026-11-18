# Otari prototype

Runs Public AI's API on [Otari](https://github.com/mozilla-ai/otari) instead of Zuplo and LiteLLM.
Overview and status: [mozilla-ai/publicai-otari-pilot](https://github.com/mozilla-ai/publicai-otari-pilot).

- `overlay/`: plugin that connects Otari to Lago wallets and Public AI's plans. Loaded with
  `OTARI_BOOTSTRAP=otari_publicai:register`.
- `docker-compose.yml`: Otari, Postgres, Redis, Mailpit, OpenWebUI.
- `Dockerfile`: Otari image with the overlay, used by the Helm chart.
- `scripts/bootstrap.py`: first-time setup.
- `scripts/smoke.py`: end-to-end test.

## Run

Start Lago from its own repo:

```bash
git clone https://github.com/getlago/lago.git && cd lago
# .env: LAGO_RSA_PRIVATE_KEY (openssl genrsa 2048 | openssl base64 -A), FRONT_PORT=8081,
# LAGO_CREATE_ORG=true, LAGO_ORG_API_KEY=lago_prototype_key, LAGO_ORG_USER_EMAIL/PASSWORD
docker compose up -d
```

Then from this directory:

```bash
cp .env.example .env            # set OTARI_SECRET_KEY and OTARI_AI_API_KEY (an otari.ai key)
docker compose up -d otari
python3 scripts/bootstrap.py
docker compose up -d open-webui # add the otari_end_user filter as a global filter
docker compose run -d --name otari-replica-2 -p 8101:8000 otari   # optional second server
python3 scripts/smoke.py
```

| Service | URL |
|---|---|
| Otari | http://localhost:8100 |
| OpenWebUI | http://localhost:8180 |
| Mailpit | http://localhost:8025 |
| Lago | http://localhost:8081 (API :3000) |

## Wallet routes

| Route | Who | Does |
|---|---|---|
| `GET /api/v1/wallet` | developer | balance, plan, limit |
| `GET /api/v1/wallet/checkout` | developer | Stripe link to save a card |
| `POST /api/v1/wallet/top-ups` | developer | buy $5 to $1,000 of credit |
| `POST /api/v1/wallet/grants` | operator | add free credit |
| `PUT /api/v1/wallet/plan` | operator | set `free`, `plus`, `pro` or `enterprise` |

## Notes

- Models come from otari.ai. Apertus isn't there yet; `config.yml` has a commented Infomaniak
  block to add it directly.
- Setting a user's plan writes `users.budget_id` in Otari's database directly, because Otari's
  API can't do it for another organization's users.
