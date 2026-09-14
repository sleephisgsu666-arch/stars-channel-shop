from aiogram.types import LabeledPrice
from app.bot.callbacks import answer_callback
from app.bot.keyboards import keyboard, invoice_keyboard, access_button
from app.core.security import Denied, InsufficientBalance, uuid_value


async def balance(message, r, uid):
    value = await r.wallet.balance(uid)
    rows = [
        [(f"{amount} ⭐", f"w:preview:{amount}") for amount in r.wallet.TOPUPS[i : i + 3]]
        for i in range(0, len(r.wallet.TOPUPS), 3)
    ]
    rows += [[("Другая сумма", "w:amount")], [("Каталог", "catalog:0")]]
    await message.answer(
        f"💰 Баланс магазина: {value} ⭐\n\n"
        "Пополнение через Telegram Stars: 1 Star = 1 единица баланса. "
        "После пополнения выберите доступ в каталоге. Автоматических списаний нет.",
        reply_markup=keyboard(rows),
    )


async def preview(message, r, amount):
    if type(amount) is not int or not 1 <= amount <= 100000:
        raise Denied("Введите целое число от 1 до 100000 Stars.")
    await message.answer(
        f"Пополнить баланс на {amount} ⭐?\n"
        "Это внутренний баланс магазина для покупки доступа. Само пополнение не открывает каналы.",
        reply_markup=keyboard(
            [
                [("Условия", r.settings.terms_url)],
                [("Согласен с условиями · Пополнить", f"w:invoice:{amount}")],
                [("Назад к балансу", "w:balance")],
            ]
        ),
    )


async def callback(query, r):
    uid = query.from_user.id
    await r.limiter.check(uid, "callback")
    await answer_callback(query)
    parts = (query.data or "").split(":")
    if parts == ["w", "balance"]:
        await r.redis.delete(f"wallet-amount:{uid}")
        await balance(query.message, r, uid)
    elif parts == ["w", "amount"]:
        await r.redis.set(f"wallet-amount:{uid}", "1", ex=600)
        await query.message.answer(
            "Введите сумму пополнения целым числом от 1 до 100000 Stars:",
            reply_markup=keyboard([[("Назад", "w:balance")]]),
        )
    elif len(parts) == 3 and parts[:2] == ["w", "preview"]:
        await preview(query.message, r, int(parts[2]))
    elif len(parts) == 3 and parts[:2] == ["w", "invoice"]:
        await r.limiter.check(uid, "invoice")
        order = await r.wallet.create_topup(uid, int(parts[2]), f"topup-button:{query.id}")
        await r.bot.send_invoice(
            chat_id=uid,
            title="Пополнение баланса",
            description=f"Внутренний баланс магазина: {order.amount_stars} единиц. Без автопродления.",
            payload=str(order.id),
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Пополнение", amount=order.amount_stars)],
            start_parameter=str(order.id),
            reply_markup=invoice_keyboard(order.amount_stars),
        )
    elif len(parts) == 2 and parts[0] == "spend":
        await r.limiter.check(uid, "invoice")
        try:
            ent = await r.wallet.purchase(uid, uuid_value(parts[1]))
        except InsufficientBalance as exc:
            await query.message.answer(
                str(exc),
                reply_markup=keyboard(
                    [
                        [("💰 Пополнить баланс", "w:balance")],
                        [("Повторить покупку", f"spend:{parts[1]}")],
                    ]
                ),
            )
            return
        await query.message.answer(
            "✅ Покупка оплачена с баланса. Доступ: "
            + (ent.expires_at.isoformat() if ent.expires_at else "навсегда")
            + ". Автопродления нет.",
            reply_markup=access_button(ent.product_id),
        )
    else:
        raise Denied("Неизвестная кнопка.")


async def amount_message(message, r):
    await r.limiter.check(message.from_user.id, "callback")
    text = (message.text or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 6:
        raise Denied("Введите целое число от 1 до 100000.")
    await preview(message, r, int(text))
    await r.redis.delete(f"wallet-amount:{message.from_user.id}")
