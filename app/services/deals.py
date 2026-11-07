from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Deal, DealStatus, LedgerEntry, User
from app.services.ledger import money


async def _locked_deal(session: AsyncSession, deal_id: int) -> Deal:
    deal = await session.scalar(select(Deal).where(Deal.id == deal_id).with_for_update())
    if deal is None:
        raise ValueError("Сделка не найдена.")
    return deal


async def _locked_account(session: AsyncSession, user_id: int) -> Account:
    account = await session.scalar(
        select(Account).where(Account.user_id == user_id).with_for_update()
    )
    if account is None:
        raise ValueError("У участника не создан счёт. Попросите его отправить /start.")
    return account


async def create_deal(
    session: AsyncSession,
    *,
    buyer_telegram_id: int,
    seller_telegram_id: int,
    amount: Decimal,
    description: str,
) -> Deal:
    amount = money(amount)
    description = description.strip()
    if buyer_telegram_id == seller_telegram_id:
        raise ValueError("Нельзя создать сделку с самим собой.")
    if not description or len(description) > 1000:
        raise ValueError("Описание должно содержать от 1 до 1000 символов.")

    buyer = await session.scalar(select(User).where(User.telegram_id == buyer_telegram_id))
    seller = await session.scalar(select(User).where(User.telegram_id == seller_telegram_id))
    if buyer is None:
        raise ValueError("Сначала отправьте /start.")
    if seller is None:
        raise ValueError("Продавец ещё не зарегистрирован. Попросите его отправить /start.")
    if buyer.is_blocked or seller.is_blocked:
        raise ValueError("Один из участников ограничен в использовании бота.")

    deal = Deal(
        buyer_id=buyer.id,
        seller_id=seller.id,
        amount=amount,
        description=description,
        status=DealStatus.AWAITING_SELLER,
    )
    session.add(deal)
    await session.flush()
    return deal


async def accept_deal(session: AsyncSession, *, deal_id: int, actor_telegram_id: int) -> Deal:
    deal = await _locked_deal(session, deal_id)
    seller = await session.get(User, deal.seller_id)
    if seller is None or seller.telegram_id != actor_telegram_id:
        raise ValueError("Принять эту сделку может только указанный продавец.")
    if seller.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status != DealStatus.AWAITING_SELLER:
        raise ValueError("Сделка уже обработана или больше не ожидает продавца.")
    deal.status = DealStatus.AWAITING_FUNDING
    return deal


async def fund_deal(session: AsyncSession, *, deal_id: int, actor_telegram_id: int) -> Deal:
    deal = await _locked_deal(session, deal_id)
    buyer = await session.get(User, deal.buyer_id)
    if buyer is None or buyer.telegram_id != actor_telegram_id:
        raise ValueError("Пополнить гарантию может только покупатель.")
    if buyer.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status != DealStatus.AWAITING_FUNDING:
        raise ValueError("Сделка пока не ожидает резервирования средств.")

    account = await _locked_account(session, deal.buyer_id)
    if account.available < deal.amount:
        raise ValueError(
            f"Недостаточно средств. Доступно: {account.available:.2f} {account.currency}."
        )

    account.available -= deal.amount
    account.frozen += deal.amount
    deal.status = DealStatus.FUNDED
    session.add(
        LedgerEntry(
            account_id=account.id,
            kind="deal_freeze",
            amount=deal.amount,
            available_delta=-deal.amount,
            frozen_delta=deal.amount,
            actor_telegram_id=actor_telegram_id,
            reason=f"Резервирование средств по сделке #{deal.id}",
            deal_id=deal.id,
        )
    )
    return deal


async def mark_delivered(
    session: AsyncSession, *, deal_id: int, actor_telegram_id: int
) -> Deal:
    deal = await _locked_deal(session, deal_id)
    seller = await session.get(User, deal.seller_id)
    if seller is None or seller.telegram_id != actor_telegram_id:
        raise ValueError("Подтвердить отправку может только продавец.")
    if seller.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status != DealStatus.FUNDED:
        raise ValueError("Сделка не находится на этапе исполнения.")
    deal.status = DealStatus.DELIVERED
    return deal


async def confirm_deal(
    session: AsyncSession, *, deal_id: int, actor_telegram_id: int
) -> Deal:
    deal = await _locked_deal(session, deal_id)
    buyer = await session.get(User, deal.buyer_id)
    if buyer is None or buyer.telegram_id != actor_telegram_id:
        raise ValueError("Завершить сделку может только покупатель.")
    if buyer.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status != DealStatus.DELIVERED:
        raise ValueError("Продавец ещё не отметил выполнение сделки.")

    locked_accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.user_id.in_([deal.buyer_id, deal.seller_id]))
                .order_by(Account.user_id)
                .with_for_update()
            )
        ).all()
    )
    accounts_by_user = {account.user_id: account for account in locked_accounts}
    buyer_account = accounts_by_user.get(deal.buyer_id)
    seller_account = accounts_by_user.get(deal.seller_id)
    if buyer_account is None or seller_account is None:
        raise ValueError("У одного из участников не создан счёт.")
    if buyer_account.frozen < deal.amount:
        raise ValueError("Ошибка учёта: у покупателя недостаточно замороженных средств.")

    buyer_account.frozen -= deal.amount
    seller_account.available += deal.amount
    deal.status = DealStatus.COMPLETED
    session.add_all(
        [
            LedgerEntry(
                account_id=buyer_account.id,
                kind="deal_release",
                amount=deal.amount,
                available_delta=Decimal("0.00"),
                frozen_delta=-deal.amount,
                actor_telegram_id=actor_telegram_id,
                reason=f"Списание резерва по сделке #{deal.id}",
                deal_id=deal.id,
            ),
            LedgerEntry(
                account_id=seller_account.id,
                kind="deal_release",
                amount=deal.amount,
                available_delta=deal.amount,
                frozen_delta=Decimal("0.00"),
                actor_telegram_id=actor_telegram_id,
                reason=f"Выплата продавцу по сделке #{deal.id}",
                deal_id=deal.id,
            ),
        ]
    )
    return deal


async def open_dispute(
    session: AsyncSession, *, deal_id: int, actor_telegram_id: int
) -> Deal:
    deal = await _locked_deal(session, deal_id)
    actor = await session.scalar(select(User).where(User.telegram_id == actor_telegram_id))
    if actor is None or actor.id not in (deal.buyer_id, deal.seller_id):
        raise ValueError("Открыть спор могут только участники сделки.")
    if actor.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status not in (DealStatus.FUNDED, DealStatus.DELIVERED):
        raise ValueError("Спор можно открыть после резервирования средств.")
    deal.status = DealStatus.DISPUTED
    return deal


async def cancel_deal(
    session: AsyncSession, *, deal_id: int, actor_telegram_id: int
) -> Deal:
    deal = await _locked_deal(session, deal_id)
    actor = await session.scalar(select(User).where(User.telegram_id == actor_telegram_id))
    if actor is None or actor.id not in (deal.buyer_id, deal.seller_id):
        raise ValueError("Отменить сделку могут только её участники.")
    if actor.is_blocked:
        raise ValueError("Ваш аккаунт заблокирован. Действие недоступно.")
    if deal.status not in (DealStatus.AWAITING_SELLER, DealStatus.AWAITING_FUNDING):
        raise ValueError("После резервирования средств отмена доступна только через спор.")
    deal.status = DealStatus.CANCELLED
    return deal


async def admin_resolve(
    session: AsyncSession,
    *,
    deal_id: int,
    actor_telegram_id: int,
    release_to_seller: bool,
) -> Deal:
    deal = await _locked_deal(session, deal_id)
    if deal.status not in (DealStatus.DISPUTED, DealStatus.DELIVERED, DealStatus.FUNDED):
        raise ValueError("Администратор может разрешить только активную или спорную сделку.")

    locked_accounts = list(
        (
            await session.scalars(
                select(Account)
                .where(Account.user_id.in_([deal.buyer_id, deal.seller_id]))
                .order_by(Account.user_id)
                .with_for_update()
            )
        ).all()
    )
    accounts_by_user = {account.user_id: account for account in locked_accounts}
    buyer_account = accounts_by_user.get(deal.buyer_id)
    seller_account = accounts_by_user.get(deal.seller_id)
    if buyer_account is None or seller_account is None:
        raise ValueError("У одного из участников не создан счёт.")
    if buyer_account.frozen < deal.amount:
        raise ValueError("Ошибка учёта: у покупателя недостаточно замороженных средств.")

    buyer_account.frozen -= deal.amount
    if release_to_seller:
        seller_account.available += deal.amount
        deal.status = DealStatus.COMPLETED
        kind = "deal_release"
        available_delta = deal.amount
        frozen_delta = Decimal("0.00")
        target_account = seller_account
        reason = f"Решение администратора: выплата по сделке #{deal.id}"
    else:
        buyer_account.available += deal.amount
        deal.status = DealStatus.REFUNDED
        kind = "deal_refund"
        available_delta = deal.amount
        frozen_delta = -deal.amount
        target_account = buyer_account
        reason = f"Решение администратора: возврат по сделке #{deal.id}"

    session.add(
        LedgerEntry(
            account_id=target_account.id,
            kind=kind,
            amount=deal.amount,
            available_delta=available_delta,
            frozen_delta=frozen_delta,
            actor_telegram_id=actor_telegram_id,
            reason=reason,
            deal_id=deal.id,
        )
    )
    if release_to_seller:
        session.add(
            LedgerEntry(
                account_id=buyer_account.id,
                kind="deal_release",
                amount=deal.amount,
                available_delta=Decimal("0.00"),
                frozen_delta=-deal.amount,
                actor_telegram_id=actor_telegram_id,
                reason=f"Списание резерва по решению администратора, сделка #{deal.id}",
                deal_id=deal.id,
            )
        )
    return deal
