# OTC Escrow Bot

Telegram OTC deal coordinator with an internal accounting ledger.

> **Important:** balances shown by this bot are internal bookkeeping units denominated in USDT. They are not USDT tokens, are not backed by on-chain assets, and cannot be deposited or withdrawn as cryptocurrency. The bot does not verify external payments.

## Stack

- Python 3.12+
- aiogram 3
- PostgreSQL 16
- SQLAlchemy 2 async + Alembic
- Docker Compose

## Implemented

- User registration with `/start` and internal balance view with `/balance`.
- Create a deal: `/deal SELLER_TELEGRAM_ID AMOUNT DESCRIPTION`.
- Seller accepts or cancels the offer with Telegram buttons.
- Buyer reserves the agreed amount; the amount moves from available to frozen balance.
- Seller marks the order as delivered; buyer confirms receipt and the amount is credited to the seller.
- Buyer or seller can open a dispute after funds are reserved.
- Super Admin resolves a dispute using inline buttons or `/resolve_release ID` / `/resolve_refund ID`.
- Super Admin-only `/credit ID AMOUNT REASON` and `/debit ID AMOUNT REASON`.
- Balance changes are written to an append-only ledger.
- Deal state transitions lock the deal row and verify the acting participant before changing balances.

## Deal flow

1. Both participants must start the bot with `/start`.
2. Buyer creates a deal using `/deal SELLER_TELEGRAM_ID AMOUNT DESCRIPTION`.
3. Seller accepts the offer.
4. Buyer reserves the amount from their available internal balance.
5. Seller marks the service/item as delivered.
6. Buyer confirms receipt; the reserved amount is credited to the seller.
7. If there is a problem after funding, either participant can open a dispute. The Super Admin chooses payout or refund.

Deals cannot be cancelled after funds have been reserved. A dispute is required instead.

## Core rules

- Internal USDT is a unit of account only; no blockchain, wallets, crypto APIs, deposits, or withdrawals.
- All balance changes must be recorded in the ledger.
- Funding, payout, refund, and admin adjustments run in database transactions.
- Deal transitions use row-level locking and strict state checks to prevent duplicate actions and double spending.
- Disputes remain unresolved until the Super Admin decides.
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
- `/resolve_release DEAL_ID` — release reserved funds to the seller.
- `/resolve_refund DEAL_ID` — return reserved funds to the buyer.

Never send your bot token or server credentials in chat. Keep secrets in the server-side `.env` file.

## Current limitations

This is an MVP implementation, **not production-ready escrow software**. It has not yet been run against a live Telegram bot or a PostgreSQL deployment in this development session. Automated integration tests, deployment hardening, rate limiting, richer transaction history, and operational monitoring remain to be completed. Do not use it to custody or represent real customer funds.


## Пополнение через Crypto Bot

1. В @CryptoBot открой Crypto Pay → My Apps → Create App и скопируй API Token.
2. Добавь токен на сервере в .env как CRYPTO_PAY_API_TOKEN=... Не публикуй его в GitHub и не отправляй в чат.
3. Перезапусти контейнеры командой docker compose up -d --build.

Пользователь выбирает «Пополнить баланс», вводит сумму в USDT и оплачивает созданный Crypto Pay invoice. Приложение опрашивает getInvoices, проверяет оплаченный счёт и зачисляет его один раз в журнал баланса. Для работы не нужен публичный webhook URL; проверка выполняется фоновым опросом API. В тестовом режиме используй @CryptoTestnetBot и базовый URL https://testnet-pay.crypt.bot/api.

Пополнение зачисляется как внутренний учётный баланс приложения. Само приложение не хранит приватные ключи и не выполняет on-chain переводы пользователям.
