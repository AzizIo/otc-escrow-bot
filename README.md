# OTC Escrow Bot

Telegram OTC deal coordinator with an internal accounting ledger.

> **Important:** balances shown by this bot are internal bookkeeping units denominated in USDT. They are not USDT tokens, are not backed by on-chain assets, and cannot be deposited or withdrawn as cryptocurrency. The bot does not verify external payments.

## Planned stack

- Python 3.12+
- aiogram 3
- PostgreSQL
- SQLAlchemy 2 async + Alembic
- Docker Compose

## Core rules

- All balance changes are recorded as append-only ledger entries.
- Deal funding, release, refunds, and admin adjustments run in database transactions.
- Operations must be idempotent and protected against concurrent double-spending.
- Disputes freeze the deal until an administrator resolves them.
- A single Super Admin is configured through `SUPERADMIN_ID` (numeric Telegram ID).
- No blockchain integration, wallets, crypto APIs, or real deposits/withdrawals.

## Initial setup

1. Copy `.env.example` to `.env`.
2. Set `BOT_TOKEN`, `DATABASE_URL`, and `SUPERADMIN_ID`.
3. Start services with `docker compose up --build`.
4. Apply migrations with `alembic upgrade head`.

The bot is not ready for production until migrations, tests, deployment secrets, and operational controls are reviewed.

## Current status

Project scaffold is being built. See the repository issues/README updates for implementation progress.
