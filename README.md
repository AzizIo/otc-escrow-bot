# OTC Escrow Bot

Telegram OTC deal coordinator with an internal accounting ledger.

> **Important:** balances shown by this bot are internal bookkeeping units denominated in USDT. They are not USDT tokens, are not backed by on-chain assets, and cannot be deposited or withdrawn as cryptocurrency. The bot does not verify external payments.

## Stack

- Python 3.12+
- aiogram 3
- PostgreSQL 16
- SQLAlchemy 2 async + Alembic
- Docker Compose

## Implemented so far

- User registration via `/start` and balance view via `/balance`.
- Super Admin-only `/credit ID AMOUNT REASON` and `/debit ID AMOUNT REASON`.
- Every admin balance adjustment writes an append-only ledger entry.
- Initial PostgreSQL schema migration.
- Docker Compose setup.

## Core rules

- Internal USDT is a unit of account only; no blockchain, wallets, crypto APIs, deposits, or withdrawals.
- All balance changes must be recorded in the ledger.
- Deal funding, release, refunds, and admin adjustments must run in database transactions.
- Deal operations must be idempotent and protected against concurrent double-spending.
- Disputes freeze the deal until an administrator resolves them.
- One Super Admin is configured through `SUPERADMIN_ID` (numeric Telegram ID).

## Setup

1. Copy `.env.example` to `.env`.
2. Set `BOT_TOKEN` and your numeric `SUPERADMIN_ID`.
3. If you change `POSTGRES_PASSWORD`, update the password in `DATABASE_URL` too.
4. Start the stack:

   ```bash
   docker compose up --build -d
   docker compose logs -f bot
   ```

The container applies Alembic migrations before starting the bot.

## Admin commands

- `/admin` — show admin commands.
- `/credit TELEGRAM_ID AMOUNT REASON` — add internal balance.
- `/debit TELEGRAM_ID AMOUNT REASON` — subtract available internal balance.

Never send your bot token or server credentials in chat. Keep secrets in the server-side `.env` file.

## Status

This is an early scaffold, not production-ready escrow software. Deal creation and state transitions, dispute handling, audit protections, comprehensive tests, and deployment hardening are still pending. Do not use it to custody or represent real customer funds.
