# OTC Escrow Bot

Telegram OTC deal coordinator with an internal USDT-denominated accounting ledger and optional Crypto Pay deposits.

> **Important:** the balance inside this bot is an internal accounting balance, not a personal on-chain wallet. Users can add funds through the administrator-configured Crypto Pay app; the bot confirms invoices through the Crypto Pay API and credits the internal ledger. The bot does not provide user withdrawals or on-chain transfers. Do not describe an internal balance as a self-custodied wallet or as a trustless escrow contract.

## Stack

- Python 3.12+
- aiogram 3
- PostgreSQL 16
- SQLAlchemy 2 async + Alembic
- Docker Compose
- Crypto Pay API (optional deposits)

## Implemented

- User registration and inline main menu.
- Deal creation wizard with seller selection/search, amount and conditions.
- Seller acceptance or cancellation of an offer.
- Buyer funding from available internal balance; funds move to a frozen balance.
- Seller delivery confirmation and buyer completion confirmation.
- Disputes for funded deals, with Super Admin resolution (release to seller or refund to buyer).
- Internal balance ledger for deposits, deal freezes/releases/refunds and administrator adjustments.
- Crypto Pay invoice creation for USDT deposits, periodic invoice polling and one-time ledger crediting.
- Admin panel with user lookup, account adjustments, profile moderation fields, analytics, broadcast and subscription settings.
- Mandatory channel subscription check (when enabled by the Super Admin).

## Deal flow

1. Both participants open the bot with `/start`.
2. The buyer creates a deal and chooses a registered seller.
3. The seller accepts the offer.
4. The buyer reserves the agreed amount from their available internal balance.
5. The seller marks the order as delivered.
6. The buyer confirms receipt; the reserved amount is credited to the seller's internal balance.
7. If there is a problem after funding, either participant can open a dispute. The Super Admin chooses payout or refund.

Deals cannot be cancelled after funds have been reserved. A dispute is required instead.

## Balance and payment model

- Balances are internal bookkeeping entries denominated in USDT.
- Crypto Pay invoices are created in the administrator's Crypto Pay app and polled by the bot.
- A paid invoice is matched to a locally stored invoice record and checked for asset, currency type and exact amount before crediting.
- Invoice status and ledger credit are updated in a database transaction; a repeated poll must not credit the same invoice twice.
- The app does not expose user withdrawals, private keys, or direct on-chain transfers.
- The Crypto Pay API token is stored in the database as a secret setting and must be protected with database access controls and backups.

This architecture is not a trustless smart-contract escrow. The administrator controls the platform and can resolve disputes and adjust internal balances. Any real-money launch requires an independent security review and jurisdiction-specific legal/compliance review.

## Core accounting rules

- All balance changes must be recorded in the ledger.
- Funding, payout, refund and admin adjustments run in database transactions.
- Deal transitions use row-level locking and strict state checks to prevent duplicate actions and double spending.
- Available and frozen balances have database-level non-negative constraints.
- Disputes remain unresolved until the Super Admin decides.
- One Super Admin is configured through `SUPERADMIN_ID` (numeric Telegram ID).

## Setup

1. Copy `.env.example` to `.env`.
2. Set `BOT_TOKEN`, numeric `SUPERADMIN_ID`, `DATABASE_URL` and `POSTGRES_PASSWORD`.
3. If you change `POSTGRES_PASSWORD`, update the password in `DATABASE_URL` too.
4. Start the stack:

   ```bash
   docker compose up --build -d
   docker compose logs -f bot
   ```

The container applies Alembic migrations before starting the bot. PostgreSQL data is stored in the named `postgres_data` volume. **Do not use `docker compose down -v` on a live installation** unless you intentionally want to delete the database volume.

## Configure Crypto Pay

1. In @CryptoBot, open Crypto Pay → My Apps → Create App and copy the API token.
2. Open the bot as the Super Admin and send `/admin`.
3. Open the Crypto Pay settings and submit the token through the private admin flow.
4. The bot validates the token with `getMe` and stores it in the database. Do not commit the token to Git or place it in source code.
5. For testnet, configure `CRYPTO_PAY_API_BASE=https://testnet-pay.crypt.bot/api` and use a testnet app token.

The polling worker checks active invoices periodically. Keep the bot process running and monitor its logs. If an invoice is paid but the user is not credited, preserve the invoice ID and check the database and logs before making any manual balance adjustment.

## Admin access

- `/admin` — open the administrator panel.
- Administrative callbacks and commands are restricted to the configured `SUPERADMIN_ID`.
- Manual balance adjustments require a reason and are written to the ledger/audit trail.

Never send your bot token, Crypto Pay token, database password or server credentials in chat. Keep secrets out of GitHub and restrict VPS and database access.

## Production readiness status

This repository is an MVP and is **not yet approved for production use with customer funds**. Before a commercial launch, complete and independently verify at least:

- Automated unit and integration tests for every deal transition and ledger operation.
- Payment-provider contract tests, invoice edge cases and recovery after process/database/network failures.
- Concurrent-action and duplicate-callback tests.
- Database backup and restore drills.
- Admin action audit review and least-privilege access controls.
- Rate limiting, abuse controls, monitoring and alerting.
- Security review, deployment documentation and jurisdiction-specific legal/compliance review.

Do not use this software to custody customer funds until these controls have been implemented, tested and reviewed.
