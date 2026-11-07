import asyncio
import logging
from decimal import Decimal

import aiohttp
from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import SessionFactory
from app.models import Account, AppSetting, CryptoDeposit, LedgerEntry, User

logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 15
MAX_BATCH_SIZE = 100
TOKEN_SETTING_KEY = "crypto_pay_api_token"


async def get_crypto_pay_token() -> str | None:
    async with SessionFactory() as session:
        setting = await session.get(AppSetting, TOKEN_SETTING_KEY)
        return setting.value if setting and setting.value else None


async def has_crypto_pay_token() -> bool:
    return bool(await get_crypto_pay_token())


async def _api_request(
    http: aiohttp.ClientSession,
    token: str,
    method: str,
    payload: dict | None = None,
) -> dict | list:
    headers = {"Crypto-Pay-API-Token": token}
    url = f"{settings.crypto_pay_api_base.rstrip('/')}/{method}"
    async with http.post(url, json=payload or {}, headers=headers) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)

    if not data.get("ok"):
        raise RuntimeError(f"Crypto Pay API error: {data.get('error', 'unknown error')}")
    return data["result"]


async def validate_crypto_pay_token(token: str) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        result = await _api_request(http, token, "getMe")
    if not isinstance(result, dict):
        raise TypeError("Crypto Pay returned an invalid account response")


async def save_crypto_pay_token(token: str) -> None:
    async with SessionFactory() as session, session.begin():
        setting = await session.get(AppSetting, TOKEN_SETTING_KEY, with_for_update=True)
        if setting is None:
            session.add(AppSetting(key=TOKEN_SETTING_KEY, value=token))
        else:
            setting.value = token


async def delete_crypto_pay_token() -> None:
    async with SessionFactory() as session, session.begin():
        setting = await session.get(AppSetting, TOKEN_SETTING_KEY, with_for_update=True)
        if setting is not None:
            await session.delete(setting)


async def create_deposit_invoice(*, telegram_id: int, amount: Decimal) -> dict:
    token = await get_crypto_pay_token()
    if not token:
        raise RuntimeError("Crypto Pay is not configured by the administrator")

    amount = amount.quantize(Decimal("0.01"))
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20)
    ) as http:
        invoice = await _api_request(
            http,
            token,
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": "USDT",
                "amount": f"{amount:.2f}",
                "description": f"Пополнение внутреннего баланса пользователя {telegram_id}",
                "payload": f"escrow-deposit:{telegram_id}",
                "allow_comments": False,
                "allow_anonymous": False,
                "expires_in": 3600,
            },
        )

    if not isinstance(invoice, dict) or not invoice.get("invoice_id"):
        raise RuntimeError("Crypto Pay returned an invalid invoice")

    async with SessionFactory() as session, session.begin():
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id).with_for_update()
        )
        if user is None:
            raise RuntimeError("User is not registered; send /start first")
        session.add(
            CryptoDeposit(
                invoice_id=int(invoice["invoice_id"]),
                user_id=user.id,
                amount=amount,
                status="active",
            )
        )
    return invoice


async def _mark_invoice_expired(invoice_id: int) -> None:
    async with SessionFactory() as session, session.begin():
        deposit = await session.scalar(
            select(CryptoDeposit)
            .where(CryptoDeposit.invoice_id == invoice_id)
            .with_for_update()
        )
        if deposit is not None and deposit.status == "active":
            deposit.status = "expired"


async def _credit_paid_invoice(invoice: dict) -> tuple[int, Decimal, str] | None:
    invoice_id = int(invoice["invoice_id"])
    async with SessionFactory() as session, session.begin():
        deposit = await session.scalar(
            select(CryptoDeposit)
            .where(CryptoDeposit.invoice_id == invoice_id)
            .with_for_update()
        )
        if deposit is None or deposit.status != "active":
            return None

        expected_amount = deposit.amount
        received_amount = Decimal(str(invoice.get("amount", "0")))
        is_valid = (
            invoice.get("status") == "paid"
            and invoice.get("currency_type") == "crypto"
            and invoice.get("asset") == "USDT"
            and received_amount == expected_amount
        )
        user = await session.get(User, deposit.user_id)
        if user is None:
            raise RuntimeError(f"User for Crypto Pay invoice {invoice_id} was not found")

        if not is_valid:
            deposit.status = "manual_review"
            return user.telegram_id, expected_amount, "review"

        account = await session.scalar(
            select(Account)
            .where(Account.user_id == deposit.user_id)
            .with_for_update()
        )
        if account is None:
            raise RuntimeError(f"Account for Crypto Pay invoice {invoice_id} was not found")

        account.available += expected_amount
        deposit.status = "paid"
        session.add(
            LedgerEntry(
                account_id=account.id,
                kind="deposit_credit",
                amount=expected_amount,
                available_delta=expected_amount,
                frozen_delta=Decimal("0.00"),
                actor_telegram_id=0,
                reason=f"Crypto Pay invoice #{invoice_id}",
            )
        )
        return user.telegram_id, expected_amount, "paid"


async def poll_crypto_pay_invoices(bot: Bot) -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        while True:
            try:
                token = await get_crypto_pay_token()
                if not token:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                async with SessionFactory() as session:
                    deposits = list(
                        (
                            await session.scalars(
                                select(CryptoDeposit)
                                .where(CryptoDeposit.status == "active")
                                .order_by(CryptoDeposit.created_at)
                                .limit(MAX_BATCH_SIZE)
                            )
                        ).all()
                    )

                if deposits:
                    invoice_ids = ",".join(str(item.invoice_id) for item in deposits)
                    invoices = await _api_request(
                        http,
                        token,
                        "getInvoices",
                        {"invoice_ids": invoice_ids, "count": len(deposits)},
                    )
                    for invoice in invoices:
                        invoice_id = int(invoice["invoice_id"])
                        if invoice.get("status") == "paid":
                            result = await _credit_paid_invoice(invoice)
                            if result is not None:
                                telegram_id, amount, status = result
                                if status == "paid":
                                    await bot.send_message(
                                        telegram_id,
                                        f"✅ Оплата подтверждена! На баланс зачислено "
                                        f"{amount:.2f} USDT.",
                                    )
                                elif status == "review":
                                    await bot.send_message(
                                        telegram_id,
                                        "⚠️ Платёж получен, но параметры счёта не совпали. "
                                        "Средства не зачислены автоматически; администратор "
                                        "проверит операцию.",
                                    )
                                    await bot.send_message(
                                        settings.superadmin_id,
                                        f"⚠️ Требуется проверка Crypto Pay invoice #{invoice_id}.",
                                    )
                        elif invoice.get("status") == "expired":
                            await _mark_invoice_expired(invoice_id)
                            try:
                                async with SessionFactory() as session:
                                    deposit = await session.get(CryptoDeposit, invoice_id)
                                    if deposit is not None:
                                        user = await session.get(User, deposit.user_id)
                                        if user is not None:
                                            await bot.send_message(
                                                user.telegram_id,
                                                f"Счёт Crypto Pay #{invoice_id} истёк. "
                                                "Создай новый счёт для пополнения.",
                                            )
                            except Exception:
                                logger.exception("Could not notify user about expired invoice")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Crypto Pay invoice polling failed")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
