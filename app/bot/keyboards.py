from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


HOME_CALLBACK = "home"


def keyboard(rows, *, include_home=True):
    rows = [list(row) for row in rows]
    if include_home and not any(value == HOME_CALLBACK for row in rows for _, value in row):
        rows.append([("🏠 Главное меню", HOME_CALLBACK)])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    **({"url": value} if value.startswith("https://") else {"callback_data": value}),
                )
                for text, value in row
            ]
            for row in rows
        ]
    )


def menu(admin=False):
    rows = [
        [("🛍 Каталог", "catalog:0")],
        [("💰 Баланс · Пополнить", "w:balance")],
        [("👤 Мои покупки", "purchases:0")],
        [("❓ Поддержка", "support"), ("📄 Условия", "terms")],
    ]
    if admin:
        rows.append([("⚙️ Админ-панель", "admin")])
    return keyboard(rows, include_home=False)


def access_button(product_id):
    return keyboard([[("🔐 Войти в канал", f"access:{product_id}")], [("👤 Мои покупки", "purchases:0")]])


def invoice_keyboard(amount_stars: int):
    # Telegram requires the Pay button to be first in the invoice keyboard.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {amount_stars} ⭐", pay=True)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data=HOME_CALLBACK)],
        ]
    )
