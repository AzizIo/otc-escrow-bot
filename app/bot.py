from decimal import Decimal, InvalidOperation

import aiohttp

from aiogram import F, Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionFactory
from app.models import Account, Deal, User
from app.services.deals import (
    accept_deal,
    admin_resolve,
    cancel_deal,
    confirm_deal,
    create_deal,
    fund_deal,
    mark_delivered,
    open_dispute,
)
from app.services.ledger import admin_adjust
from app.services.crypto_pay import create_deposit_invoice

dp = Dispatcher()


class TopUp(StatesGroup):
    amount = State()


class DealCreation(StatesGroup):
    seller_id = State()
    amount = State()
    description = State()
    confirm = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Создать сделку", callback_data="menu:create")],
            [
                InlineKeyboardButton(text="💰 Мой баланс", callback_data="menu:balance"),
                InlineKeyboardButton(text="📋 Мои сделки", callback_data="menu:deals"),
            ],
            [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="menu:topup")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="menu:help")],
        ]
    )


def deal_creation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать сделку", callback_data="create:confirm")],
            [InlineKeyboardButton(text="✏️ Изменить описание", callback_data="create:edit")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="create:cancel")],
        ]
    )


def deal_keyboard(deal_id: int, *actions: str) -> InlineKeyboardMarkup:
    labels = {
        "accept": "🤝 Принять сделку",
        "fund": "🔒 Зарезервировать средства",
        "delivered": "📦 Я выполнил обязательства",
        "confirm": "✅ Подтвердить получение",
        "dispute": "⚠️ Открыть спор",
        "cancel": "✖️ Отменить сделку",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels[action], callback_data=f"deal:{action}:{deal_id}")]
            for action in actions
        ]
    )


def admin_deal_keyboard(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Выплатить продавцу", callback_data=f"admin:release:{deal_id}")],
            [InlineKeyboardButton(text="↩️ Вернуть покупателю", callback_data=f"admin:refund:{deal_id}")],
        ]
    )


async def participant_ids(session, deal: Deal) -> tuple[int, int]:
    buyer = await session.get(User, deal.buyer_id)
    seller = await session.get(User, deal.seller_id)
    if buyer is None or seller is None:
        raise ValueError("Не удалось найти участников сделки.")
    return buyer.telegram_id, seller.telegram_id


@dp.message(Command("start"))
async def start(message: Message) -> None:
    async with SessionFactory() as session, session.begin():
        await session.execute(
            pg_insert(User)
            .values(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
            )
            .on_conflict_do_nothing(index_elements=[User.telegram_id])
        )
        user = await session.scalar(
            select(User).where(User.telegram_id == message.from_user.id).with_for_update()
        )
        if user is None:
            raise RuntimeError("Could not register Telegram user.")
        user.username = message.from_user.username
        user.full_name = message.from_user.full_name
        await session.execute(
            pg_insert(Account)
            .values(user_id=user.id, currency=settings.currency)
            .on_conflict_do_nothing(index_elements=[Account.user_id])
        )

    await message.answer(
        "Добро пожаловать в OTC Escrow 👋\n\n"
        "Здесь можно безопасно координировать OTC-сделки и отслеживать их этапы.\n"
        "Баланс — внутренняя учётная запись в условных USDT, не реальные токены "
        "и не криптокошелёк.\n\n"
        "Выбери нужное действие:",
        reply_markup=main_menu(),
    )


@dp.message(Command("menu"))
async def show_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())


@dp.message(Command("balance"))
async def balance(message: Message) -> None:
    async with SessionFactory() as session:
        account = await session.scalar(
            select(Account).join(User, User.id == Account.user_id)
            .where(User.telegram_id == message.from_user.id)
        )
        if account is None:
            await message.answer("Сначала отправь /start.")
            return
        await message.answer(
            f"Доступно: {account.available:.2f} {account.currency}\n"
            f"В резерве: {account.frozen:.2f} {account.currency}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="menu:topup")]
            ]),
        )


@dp.message(Command("my_deals"))
async def my_deals(message: Message) -> None:
    status_labels = {
        "awaiting_seller": "Ожидает продавца",
        "awaiting_funding": "Ожидает резерва",
        "funded": "Средства в резерве",
        "delivered": "Ожидает подтверждения",
        "completed": "Завершена",
        "cancelled": "Отменена",
        "disputed": "Спор",
        "refunded": "Возвращена",
    }
    async with SessionFactory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        if user is None:
            await message.answer("Сначала отправь /start.")
            return
        deals = list(
            (
                await session.scalars(
                    select(Deal)
                    .where(or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
                    .order_by(Deal.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
    if not deals:
        await message.answer("У тебя пока нет сделок.")
        return
    lines = ["Последние сделки:"]
    for deal in deals:
        role = "Покупатель" if deal.buyer_id == user.id else "Продавец"
        status = status_labels.get(deal.status, deal.status)
        lines.append(
            f"#{deal.id} · {deal.amount:.2f} USDT · {role}\n"
            f"{status} · {deal.description[:80]}"
        )
    await message.answer("\n\n".join(lines))


async def _adjust_balance(message: Message, *, credit: bool) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Эта команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) != 4:
        action = "credit" if credit else "debit"
        await message.answer(f"Формат: /{action} TELEGRAM_ID СУММА ПРИЧИНА")
        return
    try:
        target_id = int(parts[1])
        amount = Decimal(parts[2])
        if target_id <= 0 or not amount.is_finite() or amount <= 0:
            raise ValueError("Проверьте Telegram ID и сумму.")
        async with SessionFactory() as session, session.begin():
            await admin_adjust(
                session, target_telegram_id=target_id,
                actor_telegram_id=message.from_user.id, amount=amount,
                credit=credit, reason=parts[3],
            )
    except (ValueError, InvalidOperation) as exc:
        await message.answer(f"Не удалось выполнить операцию: {exc}")
        return
    verb = "начислено" if credit else "списано"
    await message.answer(f"✅ {verb.capitalize()} {amount:.2f} USDT пользователю {target_id}.")


@dp.message(Command("credit"))
async def credit(message: Message) -> None:
    await _adjust_balance(message, credit=True)


@dp.message(Command("debit"))
async def debit(message: Message) -> None:
    await _adjust_balance(message, credit=False)


async def begin_deal_creation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DealCreation.seller_id)
    await message.answer(
        "🤝 Создание сделки\n\n"
        "Шаг 1 из 3 — отправь Telegram ID продавца.\n"
        "Продавец должен предварительно запустить бота через /start.\n\n"
        "Для отмены отправь /cancel."
    )


@dp.callback_query(F.data == "menu:create")
async def menu_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(DealCreation.seller_id)
    await callback.message.answer(
        "🤝 Создание сделки\n\n"
        "Шаг 1 из 3 — отправь Telegram ID продавца.\n"
        "Продавец должен предварительно запустить бота через /start.\n\n"
        "Для отмены отправь /cancel."
    )


@dp.callback_query(F.data == "menu:balance")
async def menu_balance(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    async with SessionFactory() as session:
        account = await session.scalar(
            select(Account).join(User, User.id == Account.user_id)
            .where(User.telegram_id == callback.from_user.id)
        )
    if account is None:
        await callback.message.answer("Сначала отправь /start.")
        return
    await callback.message.answer(
        f"💰 Твой баланс\n\n"
        f"Доступно: {account.available:.2f} {account.currency}\n"
        f"В резерве: {account.frozen:.2f} {account.currency}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить баланс", callback_data="menu:topup")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:help")],
        ]),
    )


@dp.callback_query(F.data == "menu:topup")
async def menu_topup(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not settings.crypto_pay_api_token:
        await callback.message.answer(
            "Пополнение пока не подключено. Администратору нужно добавить "
            "CRYPTO_PAY_API_TOKEN в .env."
        )
        return
    await state.set_state(TopUp.amount)
    await callback.message.answer(
        "➕ Пополнение баланса через Crypto Bot\n\n"
        "Введи сумму в USDT (от 1 до 100 000). После оплаты она автоматически "
        "поступит на твой внутренний баланс.\n\nДля отмены отправь /cancel."
    )


@dp.message(TopUp.amount)
async def receive_topup_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal((message.text or "").strip().replace(",", "."))
        if (
            not amount.is_finite()
            or amount < Decimal("1.00")
            or amount > Decimal("100000.00")
            or amount.as_tuple().exponent < -2
        ):
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.answer("Введи сумму от 1 до 100 000 USDT, максимум 2 знака после запятой.")
        return

    try:
        invoice = await create_deposit_invoice(
            telegram_id=message.from_user.id,
            amount=amount,
        )
    except (aiohttp.ClientError, RuntimeError, SQLAlchemyError) as exc:
        await message.answer("Не удалось создать счёт Crypto Bot. Попробуй позже.")
        logging.getLogger(__name__).warning("Crypto Pay invoice creation failed: %s", exc)
        await state.clear()
        return

    await state.clear()
    invoice_url = invoice.get("bot_invoice_url")
    if not invoice_url:
        await message.answer("Crypto Bot не вернул ссылку на оплату. Обратись к администратору.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить через Crypto Bot", url=invoice_url)],
        [InlineKeyboardButton(text="💰 Проверить баланс", callback_data="menu:balance")],
    ])
    await message.answer(
        f"Счёт на {amount:.2f} USDT создан.\n"
        "Оплати его по кнопке ниже. После подтверждения Crypto Bot баланс "
        "зачислится автоматически (обычно в течение нескольких секунд).\n\n"
        f"Номер счёта: {invoice['invoice_id']}",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "menu:deals")
async def menu_deals(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    status_labels = {
        "awaiting_seller": "Ожидает продавца",
        "awaiting_funding": "Ожидает резерва",
        "funded": "Средства в резерве",
        "delivered": "Ожидает подтверждения",
        "completed": "Завершена",
        "cancelled": "Отменена",
        "disputed": "Спор",
        "refunded": "Возвращена",
    }
    async with SessionFactory() as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        if user is None:
            await callback.message.answer("Сначала отправь /start.")
            return
        deals = list(
            (
                await session.scalars(
                    select(Deal)
                    .where(or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
                    .order_by(Deal.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
    if not deals:
        await callback.message.answer("У тебя пока нет сделок.", reply_markup=main_menu())
        return
    lines = ["📋 Последние сделки:"]
    for deal in deals:
        role = "Покупатель" if deal.buyer_id == user.id else "Продавец"
        lines.append(
            f"#{deal.id} · {deal.amount:.2f} USDT · {role}\n"
            f"{status_labels.get(deal.status, deal.status)}\n{deal.description[:80]}"
        )
    await callback.message.answer("\n\n".join(lines), reply_markup=main_menu())


@dp.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "Как проходит сделка:\n"
        "1. Покупатель указывает продавца, сумму и описание.\n"
        "2. Продавец принимает предложение.\n"
        "3. Покупатель резервирует внутренний баланс.\n"
        "4. Продавец отмечает выполнение.\n"
        "5. Покупатель подтверждает получение.\n"
        "6. Сумма зачисляется продавцу.\n\n"
        "Если возникла проблема после резервирования, можно открыть спор.\n\n"
        "Важно: баланс в USDT — внутренняя единица учёта, не реальные криптоактивы.",
        reply_markup=main_menu(),
    )


@dp.message(Command("cancel"))
async def cancel_creation(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Сейчас нечего отменять.", reply_markup=main_menu())
        return
    await state.clear()
    await message.answer("Создание сделки отменено.", reply_markup=main_menu())


@dp.message(Command("deal"))
async def new_deal(message: Message, state: FSMContext) -> None:
    await begin_deal_creation(message, state)


@dp.message(DealCreation.seller_id)
async def receive_seller_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужен положительный числовой Telegram ID. Попробуй ещё раз.")
        return
    if int(raw) == message.from_user.id:
        await message.answer("Нельзя создать сделку с самим собой. Укажи ID другого пользователя.")
        return
    await state.update_data(seller_id=int(raw))
    await state.set_state(DealCreation.amount)
    await message.answer("Шаг 2 из 3 — введи сумму сделки в USDT, например: 25.00")


@dp.message(DealCreation.amount)
async def receive_deal_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal((message.text or "").strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError
        if amount.adjusted() >= 16:
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.answer("Введи корректную сумму больше нуля, максимум 2 знака после запятой.")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(DealCreation.description)
    await message.answer("Шаг 3 из 3 — кратко опиши товар или услугу (до 1000 символов).")


@dp.message(DealCreation.description)
async def receive_deal_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if not description or len(description) > 1000:
        await message.answer("Описание должно содержать от 1 до 1000 символов. Попробуй ещё раз.")
        return
    await state.update_data(description=description)
    data = await state.get_data()
    await state.set_state(DealCreation.confirm)
    await message.answer(
        "Проверь условия перед созданием:\n\n"
        f"Продавец: {data['seller_id']}\n"
        f"Сумма: {Decimal(data['amount']):.2f} USDT\n"
        f"Описание: {description}\n\n"
        "Создать сделку?",
        reply_markup=deal_creation_keyboard(),
    )


@dp.callback_query(F.data == "create:edit", DealCreation.confirm)
async def edit_deal_description(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(DealCreation.description)
    await callback.message.answer("Отправь новое описание сделки (до 1000 символов).")


@dp.callback_query(F.data == "create:cancel")
async def cancel_deal_creation_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Создание отменено.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Создание сделки отменено.", reply_markup=main_menu())


@dp.callback_query(F.data == "create:confirm", DealCreation.confirm)
async def confirm_deal_creation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        async with SessionFactory() as session, session.begin():
            deal = await create_deal(
                session,
                buyer_telegram_id=callback.from_user.id,
                seller_telegram_id=int(data["seller_id"]),
                amount=Decimal(data["amount"]),
                description=data["description"],
            )
            deal_id, amount, description = deal.id, deal.amount, deal.description
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        await state.clear()
        return
    except SQLAlchemyError:
        await callback.answer("Ошибка базы данных. Попробуй позже.", show_alert=True)
        await state.clear()
        return

    await state.clear()
    await callback.answer("Сделка создана.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ Сделка #{deal_id} создана.\n"
        f"Сумма: {amount:.2f} USDT\nОписание: {description}\n\n"
        "Ожидаем ответа продавца.",
        reply_markup=main_menu(),
    )
    try:
        await callback.bot.send_message(
            int(data["seller_id"]),
            f"🤝 Вам предложена сделка #{deal_id}.\n"
            f"Сумма: {amount:.2f} USDT\nОписание: {description}\n\n"
            "Примите или отклоните предложение.",
            reply_markup=deal_keyboard(deal_id, "accept", "cancel"),
        )
    except TelegramAPIError:
        await callback.message.answer(
            "⚠️ Сделка создана, но уведомление продавцу не доставлено. "
            "Убедитесь, что продавец запускал бота через /start."
        )


@dp.callback_query(lambda c: c.data and c.data.startswith("deal:"))
async def deal_action(callback: CallbackQuery) -> None:
    _, action, raw_id = callback.data.split(":")
    try:
        deal_id = int(raw_id)
        async with SessionFactory() as session, session.begin():
            if action == "accept":
                deal = await accept_deal(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            elif action == "fund":
                deal = await fund_deal(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            elif action == "delivered":
                deal = await mark_delivered(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            elif action == "confirm":
                deal = await confirm_deal(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            elif action == "dispute":
                deal = await open_dispute(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            elif action == "cancel":
                deal = await cancel_deal(
                    session, deal_id=deal_id, actor_telegram_id=callback.from_user.id
                )
            else:
                raise ValueError("Неизвестное действие.")
            deal_id = deal.id
            buyer_id, seller_id = await participant_ids(session, deal)

        await callback.answer("Готово.")
        if action == "accept":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                buyer_id, f"Продавец принял сделку #{deal_id}. Теперь зарезервируйте сумму.",
                reply_markup=deal_keyboard(deal_id, "fund", "cancel"),
            )
        elif action == "fund":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                seller_id, f"Средства по сделке #{deal_id} зарезервированы. Выполните обязательства.",
                reply_markup=deal_keyboard(deal_id, "delivered", "dispute"),
            )
            await callback.bot.send_message(
                buyer_id, f"Сумма по сделке #{deal_id} находится в резерве."
            )
        elif action == "delivered":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                buyer_id, f"Продавец отметил выполнение сделки #{deal_id}. Подтвердите получение.",
                reply_markup=deal_keyboard(deal_id, "confirm", "dispute"),
            )
        elif action == "confirm":
            await callback.message.edit_reply_markup(reply_markup=None)
            for telegram_id in {buyer_id, seller_id}:
                await callback.bot.send_message(
                    telegram_id,
                    f"✅ Сделка #{deal_id} завершена. Средства зачислены продавцу."
                )
        elif action in ("dispute", "cancel"):
            await callback.message.edit_reply_markup(reply_markup=None)
            status_text = "отправлена на разбор администратору" if action == "dispute" else "отменена"
            await callback.bot.send_message(
                buyer_id, f"Сделка #{deal_id} {status_text}."
            )
            await callback.bot.send_message(
                seller_id, f"Сделка #{deal_id} {status_text}."
            )
            if action == "dispute":
                await callback.bot.send_message(
                    settings.superadmin_id,
                    f"⚠️ Спор по сделке #{deal_id}.\n"
                    "Выберите решение:",
                    reply_markup=admin_deal_keyboard(deal_id),
                )
    except (ValueError, InvalidOperation) as exc:
        await callback.answer(str(exc), show_alert=True)
    except (SQLAlchemyError, TelegramAPIError):
        await callback.answer("Не удалось выполнить действие. Попробуйте позже.", show_alert=True)


async def _admin_resolve(message: Message, *, release: bool) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Нет доступа.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /resolve_release ID" if release else "Формат: /resolve_refund ID")
        return
    deal_id = int(parts[1])
    try:
        async with SessionFactory() as session, session.begin():
            deal = await admin_resolve(
                session, deal_id=deal_id, actor_telegram_id=message.from_user.id,
                release_to_seller=release,
            )
            buyer_id, seller_id = await participant_ids(session, deal)
        result = "выплачена продавцу" if release else "возвращена покупателю"
        await message.answer(f"✅ Сделка #{deal_id}: сумма {result}.")
        for telegram_id in {buyer_id, seller_id}:
            await message.bot.send_message(telegram_id, f"Решение администратора по сделке #{deal_id}: сумма {result}.")
    except ValueError as exc:
        await message.answer(f"Не удалось разрешить сделку: {exc}")



@dp.callback_query(lambda c: c.data and c.data.startswith("admin:"))
async def admin_deal_action(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        if action not in ("release", "refund"):
            raise ValueError("Неизвестное действие.")
        deal_id = int(raw_id)
        async with SessionFactory() as session, session.begin():
            deal = await admin_resolve(
                session,
                deal_id=deal_id,
                actor_telegram_id=callback.from_user.id,
                release_to_seller=(action == "release"),
            )
            buyer_id, seller_id = await participant_ids(session, deal)
        result = "выплачена продавцу" if action == "release" else "возвращена покупателю"
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Решение применено.")
        for telegram_id in {buyer_id, seller_id}:
            await callback.bot.send_message(
                telegram_id,
                f"Решение администратора по сделке #{deal_id}: сумма {result}.",
            )
    except (ValueError, InvalidOperation) as exc:
        await callback.answer(str(exc), show_alert=True)
    except (SQLAlchemyError, TelegramAPIError):
        await callback.answer("Не удалось применить решение.", show_alert=True)


@dp.message(Command("resolve_release"))
async def resolve_release(message: Message) -> None:
    await _admin_resolve(message, release=True)


@dp.message(Command("resolve_refund"))
async def resolve_refund(message: Message) -> None:
    await _admin_resolve(message, release=False)


@dp.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(
        "Администрирование:\n"
        "/credit ID СУММА ПРИЧИНА — начислить баланс\n"
        "/debit ID СУММА ПРИЧИНА — списать баланс\n"
        "/resolve_release ID — выплатить продавцу\n"
        "/resolve_refund ID — вернуть покупателю\n\n"
        "Все движения средств записываются в неизменяемый журнал."
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Покупатель создаёт сделку: /deal ID_ПРОДАВЦА СУММА ОПИСАНИЕ\n"
        "2. Продавец принимает предложение кнопкой.\n"
        "3. Покупатель резервирует сумму кнопкой.\n"
        "4. Продавец отмечает выполнение.\n"
        "5. Покупатель подтверждает получение — сумма зачисляется продавцу.\n\n"
        "При проблеме участник открывает спор кнопкой. Решение принимает администратор.\n"
        "/my_deals — последние 10 твоих сделок.\n\n"
        "Важно: USDT здесь — только внутренняя единица учёта, не реальные токены."
    )


def create_bot() -> Bot:
    return Bot(token=settings.bot_token)
