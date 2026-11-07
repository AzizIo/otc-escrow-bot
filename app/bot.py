from decimal import Decimal, InvalidOperation
from datetime import UTC, date, datetime, time
import html
import logging
import re

import aiohttp

from aiogram import F, Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionFactory
from app.models import Account, Deal, User, UserAdminAudit
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
from app.services.crypto_pay import (
    create_deposit_invoice,
    delete_crypto_pay_token,
    has_crypto_pay_token,
    save_crypto_pay_token,
    validate_crypto_pay_token,
)
dp = Dispatcher()

BRAND_BANNER_URL = (
    "https://images.unsplash.com/photo-1639762681485-074b7f938ba0"
    "?auto=format&fit=crop&w=1280&h=720&q=85"
)

# Custom emoji IDs from Telegram's public News Emoji pack.
# Unicode fallbacks are used when the bot owner does not have Telegram Premium.
NEWS_EMOJI_IDS = {
    "urgent": ("5224607267797606837", "⚡"),
    "warning": ("5447644880824181073", "⚠️"),
    "check": ("5206607081334906820", "✅"),
    "money": ("5409048419211682843", "💵"),
    "lock": ("5296369303661067030", "🔒"),
    "stats": ("5231200819986047254", "📊"),
    "info": ("5323442290708985472", "ℹ️"),
    "cancel": ("5240241223632954241", "❌"),
}


def news_emoji(name: str) -> str:
    emoji_id, fallback = NEWS_EMOJI_IDS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def ui_button(
    *,
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> TelegramInlineKeyboardButton:
    """Create consistently styled Telegram Bot API 9.4 buttons."""
    action = f"{text} {callback_data or ''}".lower()
    if style is None:
        danger_words = (
            "cancel", "refund", "remove", "block", "debit", "dispute",
            "отмен", "возврат", "отключ", "блок", "списать", "спор", "удал",
        )
        success_words = (
            "accept", "fund", "delivered", "confirm", "release", "credit",
            "принять", "зарезерв", "подтверд", "выплат", "начислить",
        )
        if any(word in action for word in danger_words):
            style = "danger"
        elif any(word in action for word in success_words):
            style = "success"
        else:
            style = "primary"
    return TelegramInlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style=style,
    )


async def send_hero(
    message: Message,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a branded image header, with fallbacks for older accounts/clients."""
    try:
        await message.answer_photo(
            photo=BRAND_BANNER_URL,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramAPIError:
        plain_caption = re.sub(
            r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>',
            r'\1',
            caption,
        )
        try:
            await message.answer_photo(
                photo=BRAND_BANNER_URL,
                caption=plain_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramAPIError:
            logging.getLogger(__name__).warning(
                "Could not load the branded banner; falling back to text."
            )
            await message.answer(
                plain_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )


class TopUp(StatesGroup):
    amount = State()


class CryptoPayAdmin(StatesGroup):
    token = State()


class AdminUserStates(StatesGroup):
    search = State()
    balance_amount = State()
    balance_reason = State()
    total_deals = State()
    successful_deals = State()
    since_date = State()
    stats_reason = State()


class DealCreation(StatesGroup):
    seller_id = State()
    amount = State()
    description = State()
    confirm = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ui_button(text="🤝 Создать сделку", callback_data="menu:create")],
            [
                ui_button(text="💰 Мой баланс", callback_data="menu:balance"),
                ui_button(text="📋 Мои сделки", callback_data="menu:deals"),
            ],
            [ui_button(text="➕ Пополнить баланс", callback_data="menu:topup")],
            [ui_button(text="ℹ️ Помощь", callback_data="menu:help")],
        ]
    )


def deal_creation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ui_button(text="✅ Создать сделку", callback_data="create:confirm")],
            [ui_button(text="✏️ Изменить описание", callback_data="create:edit")],
            [ui_button(text="✖️ Отмена", callback_data="create:cancel")],
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
            [ui_button(text=labels[action], callback_data=f"deal:{action}:{deal_id}")]
            for action in actions
        ]
    )


def admin_deal_keyboard(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ui_button(text="💸 Выплатить продавцу", callback_data=f"admin:release:{deal_id}")],
            [ui_button(text="↩️ Вернуть покупателю", callback_data=f"admin:refund:{deal_id}")],
        ]
    )


def user_label(user: User) -> str:
    name = re.sub(r"\s+", " ", (user.full_name or "").strip()) or "Без имени"
    username = f"@{user.username}" if user.username else "без username"
    return f"{name} ({username}, ID {user.telegram_id})"


async def participant_ids(session, deal: Deal) -> tuple[int, int]:
    buyer = await session.get(User, deal.buyer_id)
    seller = await session.get(User, deal.seller_id)
    if buyer is None or seller is None:
        raise ValueError("Не удалось найти участников сделки.")
    return buyer.telegram_id, seller.telegram_id


async def participant_labels(session, deal: Deal) -> tuple[str, str]:
    buyer = await session.get(User, deal.buyer_id)
    seller = await session.get(User, deal.seller_id)
    if buyer is None or seller is None:
        raise ValueError("Не удалось найти участников сделки.")
    return user_label(buyer), user_label(seller)


async def recent_users(exclude_telegram_id: int, limit: int = 8) -> list[User]:
    async with SessionFactory() as session:
        return list((await session.scalars(
            select(User)
            .where(User.telegram_id != exclude_telegram_id, User.is_blocked.is_(False))
            .order_by(User.created_at.desc())
            .limit(limit)
        )).all())


def seller_picker_keyboard(users: list[User]) -> InlineKeyboardMarkup:
    rows = []
    for user in users:
        title = (user.full_name or (f"@{user.username}" if user.username else str(user.telegram_id)))
        title = re.sub(r"\s+", " ", title).strip()[:28]
        suffix = f" · @{user.username}" if user.username else f" · ID {user.telegram_id}"
        rows.append([ui_button(
            text=f"👤 {title}{suffix}"[:60],
            callback_data=f"create:seller:{user.telegram_id}",
        )])
    rows.append([ui_button(text="🔎 Найти по ID или @username", callback_data="create:search")])
    rows.append([ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deal_amount_keyboard() -> InlineKeyboardMarkup:
    amounts = ("10", "25", "50", "100", "250", "500")
    rows = [
        [ui_button(text=f"💵 {amount} USDT", callback_data=f"create:amount:{amount}", style="success")]
        for amount in amounts
    ]
    rows.append([ui_button(text="⌨️ Другая сумма", callback_data="create:amount:custom")])
    rows.append([ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_seller_picker(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DealCreation.seller_id)
    users = await recent_users(message.from_user.id)
    await send_hero(
        message,
        "<b>🤝 Новая OTC-сделка</b>\n\n"
        "Выбери продавца из последних зарегистрированных пользователей "
        "или найди его по Telegram ID / @username.\n\n"
        "🔐 Продавец должен хотя бы один раз открыть этого бота.",
        reply_markup=seller_picker_keyboard(users),
    )


async def show_amount_step(message: Message) -> None:
    await message.answer(
        "💰 <b>Шаг 2 из 3 — сумма сделки</b>\n\n"
        "Выбери готовую сумму или нажми «Другая сумма».",
        parse_mode="HTML",
        reply_markup=deal_amount_keyboard(),
    )


async def show_admin_users_page(message: Message, page: int = 0) -> None:
    page = max(0, page)
    page_size = 8
    async with SessionFactory() as session:
        total = await session.scalar(select(func.count(User.id))) or 0
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages - 1)
        users = list((await session.scalars(
            select(User).order_by(User.created_at.desc())
            .offset(page * page_size).limit(page_size)
        )).all())

    rows = []
    for user in users:
        title = (user.full_name or "Без имени").replace("\\n", " ").strip()[:25]
        handle = f"@{user.username}" if user.username else f"ID {user.telegram_id}"
        rows.append([ui_button(
            text=f"👤 {title} · {handle}"[:60],
            callback_data=f"adminusers:select:{user.telegram_id}",
        )])
    rows.append([ui_button(text="🔎 Поиск по ID / @username", callback_data="adminusers:search")])
    nav = []
    if page > 0:
        nav.append(ui_button(text="⬅️ Назад", callback_data=f"adminusers:page:{page - 1}"))
    if page < pages - 1:
        nav.append(ui_button(text="Вперёд ➡️", callback_data=f"adminusers:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ui_button(text="🏠 Админ-панель", callback_data="adminpanel:home")])
    await send_hero(
        message,
        f"<b>👥 Пользователи платформы</b>\\n\\n"
        f"Всего зарегистрировано: <b>{total}</b>\\n"
        f"Страница: <b>{page + 1}/{pages}</b>\\n\\n"
        "Выбери пользователя или воспользуйся поиском.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


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

    await send_hero(
        message,
        f'<b>{news_emoji("lock")} OTC ESCROW</b>\n\n'
        "Площадка для координации OTC-сделок: создание предложений, резервирование "
        "внутреннего баланса и подтверждение выполнения.\n\n"
        f'{news_emoji("money")} Баланс в USDT — внутренняя единица учёта, не криптокошелёк '
        "и не on-chain escrow.\n\n"
        "<b>Выбери действие:</b>",
        reply_markup=main_menu(),
    )


@dp.message(Command("menu"))
async def show_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_hero(
        message,
        f'<b>{news_emoji("lock")} OTC ESCROW</b>\n\nВыбери действие:',
        reply_markup=main_menu(),
    )


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
                [ui_button(text="➕ Пополнить баланс", callback_data="menu:topup")]
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
    await show_seller_picker(message, state)


@dp.callback_query(F.data == "menu:create")
async def menu_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_seller_picker(callback.message, state)


@dp.callback_query(F.data == "create:search")
async def create_search_seller(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DealCreation.seller_id)
    await callback.answer()
    await callback.message.answer(
        "🔎 Отправь Telegram ID или @username продавца.\n"
        "Например: <code>123456789</code> или <code>@username</code>.\n\n"
        "Найти можно только пользователя, который уже запускал бота.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")]
        ]),
    )


@dp.callback_query(F.data.startswith("create:seller:"))
async def create_select_seller(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        telegram_id = int(callback.data.rsplit(":", 1)[1])
        if telegram_id == callback.from_user.id:
            raise ValueError("Нельзя создать сделку с самим собой.")
        async with SessionFactory() as session:
            seller = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if seller is None or seller.is_blocked:
                raise ValueError("Этот пользователь недоступен для сделки.")
            label = user_label(seller)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(seller_id=telegram_id, seller_label=label)
    await state.set_state(DealCreation.amount)
    await callback.answer("Продавец выбран.")
    await show_amount_step(callback.message)


@dp.callback_query(F.data.startswith("create:amount:"))
async def create_choose_amount(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.rsplit(":", 1)[1]
    if raw == "custom":
        await state.set_state(DealCreation.amount)
        await callback.answer()
        await callback.message.answer(
            "⌨️ Введи сумму в USDT, например <code>75.50</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")]
            ]),
        )
        return
    await state.update_data(amount=raw)
    await state.set_state(DealCreation.description)
    await callback.answer(f"Сумма {raw} USDT выбрана.")
    await callback.message.answer(
        "📝 <b>Шаг 3 из 3 — описание</b>\n\n"
        "Кратко опиши товар или услугу (до 1000 символов).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")]
        ]),
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
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu:topup")
async def menu_topup(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not await has_crypto_pay_token():
        await callback.message.answer(
            "Пополнение пока не подключено. Администратор ещё не настроил Crypto Pay."
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
        [ui_button(text="💳 Оплатить через Crypto Bot", url=invoice_url)],
        [ui_button(text="💰 Проверить баланс", callback_data="menu:balance")],
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
    async with SessionFactory() as session:
        if raw.isdigit() and int(raw) > 0:
            seller = await session.scalar(select(User).where(User.telegram_id == int(raw)))
        else:
            username = raw.removeprefix("@").strip().lower()
            seller = await session.scalar(
                select(User).where(func.lower(User.username) == username)
            ) if username else None
        if seller is None:
            await message.answer(
                "❌ Пользователь не найден. Проверь Telegram ID или @username. "
                "Он должен предварительно запустить бота."
            )
            return
        if seller.telegram_id == message.from_user.id:
            await message.answer("Нельзя создать сделку с самим собой. Выбери другого продавца.")
            return
        if seller.is_blocked:
            await message.answer("Этот пользователь не может участвовать в сделках.")
            return
        seller_id = seller.telegram_id
        label = user_label(seller)
    await state.update_data(seller_id=seller_id, seller_label=label)
    await state.set_state(DealCreation.amount)
    await show_amount_step(message)


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
    await message.answer(
        "📝 <b>Шаг 3 из 3 — описание</b>\\n\\n"
        "Кратко опиши товар или услугу (до 1000 символов).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")]
        ]),
    )


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
        "🧾 <b>Проверь условия сделки</b>\\n\\n"
        f"👤 Продавец: {html.escape(data.get('seller_label', str(data['seller_id'])))}\\n"
        f"💰 Сумма: <b>{Decimal(data['amount']):.2f} USDT</b>\\n"
        f"📦 Условия: {html.escape(description)}\\n\\n"
        "Всё верно? После подтверждения продавцу придёт уведомление.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="✅ Создать сделку", callback_data="create:confirm", style="success")],
            [ui_button(text="💰 Изменить сумму", callback_data="create:edit_amount")],
            [ui_button(text="👤 Изменить продавца", callback_data="create:edit_seller")],
            [ui_button(text="📝 Изменить описание", callback_data="create:edit")],
            [ui_button(text="✖️ Отмена", callback_data="create:cancel", style="danger")],
        ]),
    )


@dp.callback_query(F.data == "create:edit_amount", DealCreation.confirm)
async def edit_deal_amount(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(DealCreation.amount)
    await show_amount_step(callback.message)


@dp.callback_query(F.data == "create:edit_seller", DealCreation.confirm)
async def edit_deal_seller(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_seller_picker(callback.message, state)


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
            buyer = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
            seller = await session.scalar(select(User).where(User.telegram_id == int(data["seller_id"])))
            buyer_name = user_label(buyer) if buyer else str(callback.from_user.id)
            seller_name = user_label(seller) if seller else str(data["seller_id"])
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
        f"✅ <b>Сделка #{deal_id} создана</b>\\n\\n"
        f"👤 Покупатель: {html.escape(buyer_name)}\\n"
        f"👤 Продавец: {html.escape(seller_name)}\\n"
        f"💰 Сумма: <b>{amount:.2f} USDT</b>\\n"
        f"📦 Условия: {html.escape(description)}\\n\\n"
        "⏳ Ожидаем ответа продавца.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
    try:
        await callback.bot.send_message(
            int(data["seller_id"]),
            f"🤝 <b>Вам предложена сделка #{deal_id}</b>\\n\\n"
            f"👤 Покупатель: {html.escape(buyer_name)}\\n"
            f"👤 Вы — продавец: {html.escape(seller_name)}\\n"
            f"💰 Сумма: <b>{amount:.2f} USDT</b>\\n"
            f"📦 Условия: {html.escape(description)}\\n\\n"
            "Выберите действие:",
            parse_mode="HTML",
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
            buyer_name, seller_name = await participant_labels(session, deal)
            deal_amount, deal_description = deal.amount, deal.description

        await callback.answer("Готово.")
        if action == "accept":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                buyer_id,
                f"🤝 <b>Продавец принял сделку #{deal_id}</b>\\n\\n"
                f"👤 Покупатель: {html.escape(buyer_name)}\\n"
                f"👤 Продавец: {html.escape(seller_name)}\\n"
                f"💰 Сумма: {deal_amount:.2f} USDT\\n"
                f"📦 Условия: {html.escape(deal_description)}\\n\\n"
                "Теперь зарезервируйте сумму.",
                parse_mode="HTML",
                reply_markup=deal_keyboard(deal_id, "fund", "cancel"),
            )
        elif action == "fund":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                seller_id,
                f"🔒 <b>Средства по сделке #{deal_id} зарезервированы</b>\\n\\n"
                f"👤 Покупатель: {html.escape(buyer_name)}\\n"
                f"👤 Продавец: {html.escape(seller_name)}\\n"
                f"💰 Сумма: {deal_amount:.2f} USDT\\n"
                f"📦 Условия: {html.escape(deal_description)}\\n\\n"
                "Выполните обязательства и отметьте это кнопкой ниже.",
                parse_mode="HTML",
                reply_markup=deal_keyboard(deal_id, "delivered", "dispute"),
            )
            await callback.bot.send_message(
                buyer_id,
                f"🔒 Сумма по сделке #{deal_id} находится в резерве.\\n"
                f"👤 Продавец: {html.escape(seller_name)}\\n"
                f"💰 {deal_amount:.2f} USDT"
            )
        elif action == "delivered":
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.bot.send_message(
                buyer_id,
                f"📦 <b>Продавец отметил выполнение сделки #{deal_id}</b>\\n\\n"
                f"👤 Продавец: {html.escape(seller_name)}\\n"
                f"💰 Сумма: {deal_amount:.2f} USDT\\n"
                f"📦 Условия: {html.escape(deal_description)}\\n\\n"
                "Проверь результат и подтверди получение.",
                parse_mode="HTML",
                reply_markup=deal_keyboard(deal_id, "confirm", "dispute"),
            )
        elif action == "confirm":
            await callback.message.edit_reply_markup(reply_markup=None)
            for telegram_id in {buyer_id, seller_id}:
                await callback.bot.send_message(
                    telegram_id,
                    f"✅ <b>Сделка #{deal_id} завершена</b>\\n\\n"
                    f"👤 Покупатель: {html.escape(buyer_name)}\\n"
                    f"👤 Продавец: {html.escape(seller_name)}\\n"
                    f"💰 Сумма: {deal_amount:.2f} USDT\\n"
                    "Средства зачислены продавцу.",
                    parse_mode="HTML",
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


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="👥 Пользователи", callback_data="adminpanel:find")],
        [ui_button(text="📊 Общая статистика", callback_data="adminpanel:stats")],
        [ui_button(text="⚙️ Crypto Pay", callback_data="adminpanel:cryptopay")],
    ])


def admin_user_keyboard(user: User) -> InlineKeyboardMarkup:
    status_action = "unblock" if user.is_blocked else "block"
    status_label = "🔓 Разблокировать" if user.is_blocked else "🚫 Заблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="✏️ Изменить показатели профиля", callback_data=f"adminuser:edit:{user.telegram_id}")],
        [ui_button(text="➕ Начислить баланс", callback_data=f"adminuser:credit:{user.telegram_id}"),
         ui_button(text="➖ Списать баланс", callback_data=f"adminuser:debit:{user.telegram_id}")],
        [ui_button(text=status_label, callback_data=f"adminuser:{status_action}:{user.telegram_id}")],
        [ui_button(text="📋 Последние сделки", callback_data=f"adminuser:deals:{user.telegram_id}")],
        [ui_button(text="🧾 История изменений", callback_data=f"adminuser:audit:{user.telegram_id}")],
        [ui_button(text="⬅️ К списку пользователей", callback_data="adminpanel:find")],
        [ui_button(text="🏠 Админ-панель", callback_data="adminpanel:home")],
    ])


def _date_label(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


async def _admin_user_card(message: Message, telegram_id: int) -> None:
    async with SessionFactory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await message.answer("Пользователь не найден.")
            return
        account = await session.scalar(select(Account).where(Account.user_id == user.id))
        available = account.available if account else Decimal("0.00")
        frozen = account.frozen if account else Decimal("0.00")
        currency = account.currency if account else "USDT"
        total = await session.scalar(
            select(func.count(Deal.id)).where(
                or_(Deal.buyer_id == user.id, Deal.seller_id == user.id)
            )
        ) or 0
        successful = await session.scalar(
            select(func.count(Deal.id)).where(
                or_(Deal.buyer_id == user.id, Deal.seller_id == user.id),
                Deal.status == "completed",
            )
        ) or 0
        completed_volume = await session.scalar(
            select(func.coalesce(func.sum(Deal.amount), 0)).where(
                or_(Deal.buyer_id == user.id, Deal.seller_id == user.id),
                Deal.status == "completed",
            )
        )
        display_total = user.profile_deals_override if user.profile_deals_override is not None else total
        display_success = user.profile_successful_override if user.profile_successful_override is not None else successful
        display_since = user.profile_since_override or user.created_at
        username = f"@{user.username}" if user.username else "не указан"
        status = "🚫 Заблокирован" if user.is_blocked else "✅ Активен"
        text = (
            f"👤 Профиль пользователя\n\n"
            f"Telegram ID: <code>{user.telegram_id}</code>\n"
            f"Имя: {html.escape(user.full_name or 'не указано')}\n"
            f"Username: {html.escape(username)}\n"
            f"Статус: {status}\n\n"
            f"💰 Баланс\n"
            f"Доступно: {available:.2f} {currency}\n"
            f"В резерве: {frozen:.2f} {currency}\n\n"
            f"📈 Фактическая статистика\n"
            f"Сделок всего: {total}\n"
            f"Успешных: {successful}\n"
            f"Объём завершённых: {Decimal(str(completed_volume or 0)):.2f} USDT\n"
            f"Регистрация: {_date_label(user.created_at)}\n\n"
            f"🪪 Показатели профиля\n"
            f"Сделок: {display_total}{' ✏️' if user.profile_deals_override is not None else ''}\n"
            f"Успешных: {display_success}{' ✏️' if user.profile_successful_override is not None else ''}\n"
            f'На платформе с: {_date_label(display_since)}'
            f"{' ✏️ (ручная дата)' if user.profile_since_override else ''}\n\n"
            f"✏️ = значение вручную скорректировано администратором."
        )
        await message.answer(text, reply_markup=admin_user_keyboard(user), parse_mode="HTML")


@dp.callback_query(F.data == "adminpanel:home")
async def admin_panel_home(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await send_hero(
        callback.message,
        f'<b>{news_emoji("stats")} ADMIN CONTROL</b>\n\nВыбери раздел управления:',
        reply_markup=admin_main_keyboard(),
    )


@dp.callback_query(F.data == "adminpanel:cryptopay")
async def admin_crypto_pay_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    configured = await has_crypto_pay_token()
    status = "🟢 Подключён" if configured else "🔴 Не настроен"
    keyboard = [
        [ui_button(
            text="🔑 Добавить / заменить токен",
            callback_data="admincryptopay:set",
        )],
    ]
    if configured:
        keyboard.append([ui_button(
            text="🗑 Отключить Crypto Pay",
            callback_data="admincryptopay:remove",
        )])
    keyboard.append([ui_button(
        text="⬅️ Админ-панель", callback_data="adminpanel:home"
    )])
    await callback.answer()
    await callback.message.answer(
        f"⚙️ Настройки Crypto Pay\n\nСтатус: {status}\n"
        "Токен хранится в базе данных и не показывается в сообщениях или панели.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@dp.callback_query(F.data == "admincryptopay:set")
async def admin_crypto_pay_set_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(CryptoPayAdmin.token)
    await callback.answer()
    await callback.message.answer(
        "Отправь API Token от Crypto Pay одним сообщением.\n"
        "Бот проверит токен и сохранит его в базе. Сообщение с токеном будет удалено.\n\n"
        "Получить токен можно в @CryptoBot → Crypto Pay → My Apps.\n"
        "Для отмены отправь /cancel."
    )


@dp.callback_query(F.data == "admincryptopay:remove")
async def admin_crypto_pay_remove(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await delete_crypto_pay_token()
    await callback.answer("Crypto Pay отключён.")
    await callback.message.answer(
        "✅ Токен удалён из базы. Новые пополнения отключены.",
        reply_markup=admin_main_keyboard(),
    )


@dp.message(CryptoPayAdmin.token)
async def admin_crypto_pay_receive_token(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id or message.chat.type != "private":
        await state.clear()
        return

    token = (message.text or "").strip()
    message_deleted = True
    try:
        await message.delete()
    except TelegramAPIError:
        message_deleted = False
        logging.getLogger(__name__).warning(
            "Could not delete the administrator's Crypto Pay token message"
        )

    if not token or len(token) > 512 or any(char.isspace() for char in token):
        warning = "" if message_deleted else "\n⚠️ Не удалось удалить сообщение с токеном — удали его вручную."
        await message.answer(
            "Токен выглядит некорректно. Отправь его одним сообщением ещё раз." + warning
        )
        return

    try:
        await validate_crypto_pay_token(token)
        await save_crypto_pay_token(token)
    except (aiohttp.ClientError, RuntimeError, TypeError, SQLAlchemyError):
        await message.answer(
            "❌ Не удалось проверить токен. Проверь его в Crypto Pay и попробуй ещё раз. "
            "Старый токен, если он был, не изменён."
        )
        return

    await state.clear()
    warning = "" if message_deleted else (
        "\n⚠️ Telegram не позволил удалить исходное сообщение с токеном. "
        "Удали его вручную из этого чата."
    )
    await message.answer(
        "✅ Crypto Pay успешно подключён. Токен проверен и сохранён. "
        "Пополнения снова доступны." + warning,
        reply_markup=admin_main_keyboard(),
    )


@dp.callback_query(F.data == "adminpanel:find")
async def admin_panel_find(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await show_admin_users_page(callback.message, 0)


@dp.callback_query(F.data.startswith("adminusers:page:"))
async def admin_users_page(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    await callback.answer()
    await show_admin_users_page(callback.message, page)


@dp.callback_query(F.data.startswith("adminusers:select:"))
async def admin_users_select(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        telegram_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    await callback.answer()
    await _admin_user_card(callback.message, telegram_id)


@dp.callback_query(F.data == "adminusers:search")
async def admin_users_search(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminUserStates.search)
    await callback.answer()
    await callback.message.answer(
        "🔎 Отправь Telegram ID или @username. Например: <code>123456789</code> или <code>@username</code>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="👥 К списку пользователей", callback_data="adminpanel:find")]
        ]),
    )


@dp.message(AdminUserStates.search)
async def admin_find_user(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    query = (message.text or "").strip()
    async with SessionFactory() as session:
        if query.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(query)))
        else:
            username = query.removeprefix("@").lower()
            user = await session.scalar(
                select(User).where(func.lower(User.username) == username)
            )
        user_id = user.telegram_id if user else None
    await state.clear()
    if user_id is None:
        await message.answer("Не нашёл пользователя. Проверь ID или username.", reply_markup=admin_main_keyboard())
        return
    await _admin_user_card(message, user_id)


@dp.callback_query(F.data == "adminpanel:stats")
async def admin_panel_stats(callback: CallbackQuery) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    async with SessionFactory() as session:
        users = await session.scalar(select(func.count(User.id))) or 0
        deals = await session.scalar(select(func.count(Deal.id))) or 0
        completed = await session.scalar(select(func.count(Deal.id)).where(Deal.status == "completed")) or 0
        disputed = await session.scalar(select(func.count(Deal.id)).where(Deal.status == "disputed")) or 0
        volume = await session.scalar(select(func.coalesce(func.sum(Deal.amount), 0)).where(Deal.status == "completed"))
    await callback.message.answer(
        f"📊 Статистика биржи\n\nПользователей: {users}\n"
        f"Сделок всего: {deals}\nУспешных: {completed}\nВ спорах: {disputed}\n"
        f"Объём завершённых сделок: {Decimal(str(volume or 0)):.2f} USDT",
        reply_markup=admin_main_keyboard(),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("adminuser:"))
async def admin_user_action(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != settings.superadmin_id:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        telegram_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    if action in ("block", "unblock"):
        async with SessionFactory() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            if user is None:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            old = str(user.is_blocked)
            user.is_blocked = action == "block"
            session.add(UserAdminAudit(
                user_id=user.id, actor_telegram_id=callback.from_user.id,
                field="is_blocked", old_value=old, new_value=str(user.is_blocked),
                reason="Изменение статуса через панель администратора",
            ))
        await callback.answer("Статус обновлён.")
        await _admin_user_card(callback.message, telegram_id)
        return

    if action == "audit":
        async with SessionFactory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            rows = list((await session.scalars(
                select(UserAdminAudit).where(UserAdminAudit.user_id == user.id)
                .order_by(UserAdminAudit.created_at.desc()).limit(20)
            )).all())
        await callback.answer()
        if not rows:
            await callback.message.answer("История административных изменений пока пуста.")
        else:
            lines = ["🧾 Последние изменения (до 20):"]
            for row in rows:
                lines.append(
                    f"{row.created_at:%d.%m.%Y %H:%M} · {row.field}\n"
                    f"{row.old_value or '—'} → {row.new_value or '—'}\n"
                    f"Причина: {row.reason}"
                )
            await callback.message.answer("\n\n".join(lines))
        return

    if action == "deals":
        async with SessionFactory() as session:
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                await callback.answer("Пользователь не найден.", show_alert=True)
                return
            deals = list((await session.scalars(
                select(Deal).where(or_(Deal.buyer_id == user.id, Deal.seller_id == user.id))
                .order_by(Deal.created_at.desc()).limit(15)
            )).all())
            participants = {
                participant.id: participant
                for deal in deals
                for participant in (
                    await session.get(User, deal.buyer_id),
                    await session.get(User, deal.seller_id),
                )
                if participant is not None
            }
        await callback.answer()
        if not deals:
            await callback.message.answer("У пользователя пока нет сделок.")
        else:
            lines = ["📋 Последние сделки пользователя:"]
            for deal in deals:
                buyer = participants.get(deal.buyer_id)
                seller = participants.get(deal.seller_id)
                lines.append(
                    f"🤝 #{deal.id} · {deal.amount:.2f} USDT · {deal.status}\\n"
                    f"Покупатель: {user_label(buyer) if buyer else deal.buyer_id}\\n"
                    f"Продавец: {user_label(seller) if seller else deal.seller_id}\\n"
                    f"Условия: {deal.description[:100]}"
                )
            await callback.message.answer("\n\n".join(lines))
        return

    if action in ("credit", "debit"):
        await state.clear()
        await state.update_data(admin_target_id=telegram_id, admin_balance_action=action)
        await state.set_state(AdminUserStates.balance_amount)
        await callback.answer()
        await callback.message.answer(
            f"Введи сумму для {'начисления' if action == 'credit' else 'списания'} "
            f"пользователю {telegram_id}."
        )
        return

    if action == "edit":
        await state.clear()
        await state.update_data(admin_target_id=telegram_id)
        await state.set_state(AdminUserStates.total_deals)
        await callback.answer()
        await callback.message.answer(
            "✏️ Корректировка показателей профиля\n\n"
            "Введи отображаемое количество сделок. Число или '-' чтобы использовать фактическое значение."
        )
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@dp.message(AdminUserStates.balance_amount)
async def admin_balance_amount(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    try:
        amount = Decimal((message.text or "").strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError
    except (InvalidOperation, ValueError):
        await message.answer("Введи положительную сумму, максимум 2 знака после запятой.")
        return
    await state.update_data(admin_balance_amount=str(amount))
    await state.set_state(AdminUserStates.balance_reason)
    await message.answer("Укажи причину операции. Она попадёт в журнал баланса.")


@dp.message(AdminUserStates.balance_reason)
async def admin_balance_reason(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    reason = (message.text or "").strip()
    if not reason or len(reason) > 500:
        await message.answer("Причина должна содержать от 1 до 500 символов.")
        return
    data = await state.get_data()
    credit_action = data["admin_balance_action"] == "credit"
    try:
        async with SessionFactory() as session, session.begin():
            await admin_adjust(
                session, target_telegram_id=int(data["admin_target_id"]),
                actor_telegram_id=message.from_user.id,
                amount=Decimal(data["admin_balance_amount"]),
                credit=credit_action, reason=reason,
            )
    except (ValueError, SQLAlchemyError) as exc:
        await message.answer(f"Не удалось изменить баланс: {exc}")
        await state.clear()
        return
    telegram_id = int(data["admin_target_id"])
    await state.clear()
    await message.answer("✅ Баланс изменён и записан в журнал.")
    await _admin_user_card(message, telegram_id)


@dp.message(AdminUserStates.total_deals)
async def admin_profile_total(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw != "-" and (not raw.isdigit() or int(raw) > 1_000_000_000):
        await message.answer("Введи целое число от 0 или '-' для фактического значения.")
        return
    await state.update_data(profile_total=None if raw == "-" else int(raw))
    await state.set_state(AdminUserStates.successful_deals)
    await message.answer("Введи количество успешных сделок или '-' для фактического значения.")


@dp.message(AdminUserStates.successful_deals)
async def admin_profile_successful(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw != "-" and (not raw.isdigit() or int(raw) > 1_000_000_000):
        await message.answer("Введи целое число от 0 или '-' для фактического значения.")
        return
    await state.update_data(profile_successful=None if raw == "-" else int(raw))
    await state.set_state(AdminUserStates.since_date)
    await message.answer("Введи дату «на платформе с» в формате ГГГГ-ММ-ДД или '-' для реальной даты регистрации.")


@dp.message(AdminUserStates.since_date)
async def admin_profile_since(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    raw = (message.text or "").strip()
    try:
        since = None if raw == "-" else datetime.combine(
            date.fromisoformat(raw), time.min, tzinfo=UTC
        )
        if since and since.date() > datetime.now(UTC).date():
            raise ValueError
    except ValueError:
        await message.answer("Введи корректную дату не из будущего: ГГГГ-ММ-ДД, либо '-'.")
        return
    await state.update_data(profile_since=since.isoformat() if since else None)
    await state.set_state(AdminUserStates.stats_reason)
    await message.answer("Укажи причину корректировки. Она сохранится в журнале администратора.")


@dp.message(AdminUserStates.stats_reason)
async def admin_profile_reason(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await state.clear()
        return
    reason = (message.text or "").strip()
    if not reason or len(reason) > 500:
        await message.answer("Причина должна содержать от 1 до 500 символов.")
        return
    data = await state.get_data()
    telegram_id = int(data["admin_target_id"])
    try:
        async with SessionFactory() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            if user is None:
                raise ValueError("Пользователь не найден.")
            total = await session.scalar(select(func.count(Deal.id)).where(
                or_(Deal.buyer_id == user.id, Deal.seller_id == user.id)
            )) or 0
            successful = await session.scalar(select(func.count(Deal.id)).where(
                or_(Deal.buyer_id == user.id, Deal.seller_id == user.id),
                Deal.status == "completed",
            )) or 0
            total_value = data["profile_total"] if data["profile_total"] is not None else total
            successful_value = data["profile_successful"] if data["profile_successful"] is not None else successful
            if successful_value > total_value:
                raise ValueError("Успешных сделок не может быть больше общего количества.")
            updates = {
                "profile_deals_override": data["profile_total"],
                "profile_successful_override": data["profile_successful"],
                "profile_since_override": datetime.fromisoformat(data["profile_since"]) if data["profile_since"] else None,
            }
            for field, new_value in updates.items():
                old_value = getattr(user, field)
                old_text = old_value.isoformat() if old_value else None
                new_text = new_value.isoformat() if new_value else None
                if old_text != new_text:
                    session.add(UserAdminAudit(
                        user_id=user.id, actor_telegram_id=message.from_user.id,
                        field=field, old_value=old_text, new_value=new_text, reason=reason,
                    ))
                setattr(user, field, new_value)
    except (ValueError, SQLAlchemyError) as exc:
        await message.answer(f"Не удалось сохранить показатели: {exc}")
        await state.clear()
        return
    await state.clear()
    await message.answer("✅ Показатели профиля сохранены. Исторические сделки не изменялись.")
    await _admin_user_card(message, telegram_id)


@dp.message(Command("admin"))
async def admin_help(message: Message, state: FSMContext) -> None:
    if message.from_user.id != settings.superadmin_id:
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await send_hero(
        message,
        f'<b>{news_emoji("stats")} ADMIN CONTROL</b>\n\n'
        "Управление пользователями, балансами, статистикой и настройками платежей.\n\n"
        f'{news_emoji("check")} Все ручные корректировки фиксируются в журнале администратора.',
        reply_markup=admin_main_keyboard(),
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
