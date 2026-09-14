from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, PreCheckoutQuery, ChatJoinRequest
from app.bot.handlers import shop, admin, admin_wizard, wallet
from app.bot.keyboards import access_button, keyboard, HOME_CALLBACK
from app.bot.callbacks import answer_callback
from app.core.security import Denied


def build_dispatcher(runtime):
    dp = Dispatcher()
    r = runtime

    @dp.pre_checkout_query()
    async def precheckout(query: PreCheckoutQuery):
        try:
            await r.payments.precheckout(
                query.from_user.id, query.invoice_payload, query.currency, query.total_amount
            )
        except Denied as exc:
            await query.answer(ok=False, error_message=str(exc))
        else:
            await query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def paid(message: Message):
        ent = await r.payments.successful(message.from_user.id, message.successful_payment)
        if isinstance(ent, int):
            await message.answer(
                f"✅ Пополнение обработано. Баланс магазина: {ent} ⭐.\nТеперь выберите доступ в каталоге.",
                reply_markup=keyboard([[("Каталог", "catalog:0")], [("Баланс", "w:balance")]]),
            )
            return
        if ent is None:
            await message.answer(
                "Этот платёж возвращён или поставлен на возврат. Проверить статус: /paysupport",
                reply_markup=keyboard([]),
            )
            return
        await message.answer(
            "✅ Оплата получена. Доступ: " + (ent.expires_at.isoformat() if ent.expires_at else "навсегда"),
            reply_markup=access_button(ent.product_id),
        )

    @dp.message(F.refunded_payment)
    async def refunded(message: Message):
        await r.refunds.external(message.from_user.id, message.refunded_payment)

    @dp.chat_join_request()
    async def join(request: ChatJoinRequest):
        await r.access.join(request)

    @dp.message(Command("start"), F.chat.type == "private")
    async def start(message: Message):
        await shop.start(message, r)

    @dp.message(Command("support", "paysupport"), F.chat.type == "private")
    async def support(message: Message):
        await shop.support(message, r)

    @dp.message(Command("terms"), F.chat.type == "private")
    async def terms(message: Message):
        await shop.terms(message, r)

    @dp.message(Command("balance"), F.chat.type == "private")
    async def wallet_balance(message: Message):
        await wallet.balance(message, r, message.from_user.id)

    async def waiting_for_amount(message: Message):
        return (
            message.chat.type == "private"
            and message.text
            and not message.text.startswith("/")
            and bool(await r.redis.get(f"wallet-amount:{message.from_user.id}"))
        )

    @dp.message(waiting_for_amount)
    async def wallet_amount(message: Message):
        await wallet.amount_message(message, r)

    @dp.message(
        Command("admin", "adminhelp", "save", "grant", "refund", "list", "stats"), F.chat.type == "private"
    )
    async def administration(message: Message):
        await admin.command(message, r)

    @dp.message(F.chat.type == "private", F.from_user.id.in_(r.settings.admin_telegram_ids))
    async def admin_input(message: Message):
        await admin_wizard.message(message, r)

    @dp.callback_query()
    async def callback(query: CallbackQuery):
        if (
            not query.message
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            raise Denied("Используйте личный чат с ботом.")
        if query.data == HOME_CALLBACK:
            await answer_callback(query)
            await shop.start(query.message, r, query.from_user)
        elif (query.data or "").startswith(("w:", "spend:")):
            await wallet.callback(query, r)
        elif (query.data or "").startswith(
            ("anew:", "aopen:", "aedit:", "aaccess:", "auser:", "arefund:", "aprefund:", "aw:")
        ):
            await admin_wizard.callback(query, r)
        elif (query.data or "").startswith(("admin", "refundconfirm:")):
            await admin.callback(query, r)
        else:
            await shop.callback(query, r)

    return dp
