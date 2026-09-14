from app.bot.callbacks import answer_callback
from app.bot.keyboards import keyboard
from app.core.security import Denied, uuid_value

HELP = """Откройте /admin и выберите раздел.

➕ Добавить — бот по очереди спросит нужные данные.
Нажмите на существующую запись, чтобы изменить её.
Канал и продукт выбираются кнопками. Обложка загружается фотографией.

В разделе «Доступы» можно выдать, продлить или отозвать доступ.
В разделе «Платежи» выберите оплату для возврата.

Перед сохранением бот покажет итог и попросит подтверждение.
Отмена на любом шаге: кнопка «Отмена» или /cancel.
Черновик сохраняется на 30 минут. JSON и UUID вводить не нужно."""


async def menu(message, r, user_id):
    r.admin.auth(user_id)
    rows = [
        [(label, f"adminlist:{kind}:0")]
        for kind, label in [
            ("product", "📦 Продукты"),
            ("plan", "💰 Тарифы"),
            ("channel", "📢 Каналы"),
            ("user", "👥 Пользователи"),
            ("access", "🎫 Доступы"),
            ("payment", "💳 Пополнения / ↩️ Возвраты"),
            ("purchase", "🛍 Покупки с баланса"),
            ("audit", "📝 Audit Log"),
        ]
    ]
    rows += [[("📊 Статистика", "adminstats")], [("Как изменить данные", "adminhelp")]]
    from app.services.admin_forms import AdminForms

    async with r.db.lock(f"admin-form:{user_id}"):
        await AdminForms(r).clear(user_id)
    await message.answer("Админ-панель", reply_markup=keyboard(rows))


async def listing(message, r, user_id, kind, page):
    from app.bot.handlers.admin_wizard import listing as show_list

    await show_list(message, r, user_id, kind, page)


async def statistics(message, r, uid):
    from app.services.admin_forms import AdminForms
    from app.bot.handlers.admin_wizard import show_text

    data = await r.admin.stats(uid)
    lines = [
        "📊 Статистика",
        f"Пользователей: {data['users']}",
        f"Новых за 24 часа: {data['new_24h']}",
        f"Активных доступов: {data['active_entitlements']}",
        f"Stars-платежей без возврата: {data['paid_payments']}",
        f"Stars за вычетом возвратов: {data['net_stars']}",
        f"Возвратов: {data['refunds']}",
    ]
    lines += [
        f"Покупок с баланса: {data.get('wallet_sales', 0)}",
        f"Баланс пользователей: {data.get('wallet_balances', 0)} ⭐",
    ]
    forms = AdminForms(r)
    for key, kind, title in [("products", "product", "По продуктам"), ("plans", "plan", "По тарифам")]:
        if data[key]:
            lines.append("\n" + title + " (до 20 записей):")
        for row in data[key]:
            _, label, _ = await forms.record(uid, kind, row["id"])
            lines.append(f"{label}: {row['payments']} оплат, {row['stars']} ⭐")
    await show_text(message, "\n".join(lines), [[("← Админ-панель", "admin")]])


async def command(message, r):
    uid = message.from_user.id
    r.admin.auth(uid)
    await r.limiter.check(uid, "admin")
    if message.photo:
        await message.answer(f"cover_file_id: {message.photo[-1].file_id}", reply_markup=keyboard([]))
        return
    text = message.text or ""
    if len(text) > 4096:
        raise Denied("Слишком длинный ввод.")
    cmd, _, tail = text.partition(" ")
    cmd = cmd.split("@")[0]
    key = f"admin:{uid}:{message.message_id}"
    if cmd == "/admin":
        await menu(message, r, uid)
    elif cmd == "/adminhelp":
        await message.answer(HELP, reply_markup=keyboard([]))
    elif cmd == "/save":
        parts = tail.split(" ", 2)
        if len(parts) != 3:
            raise Denied("Формат: /save тип new|UUID JSON")
        kind, target, raw = parts
        result = await r.admin.save(uid, kind, None if target == "new" else uuid_value(target), raw, key)
        await message.answer(f"Сохранено: {result}", reply_markup=keyboard([]))
    elif cmd == "/grant":
        result = await r.admin.manual(uid, tail, key)
        await message.answer(f"Доступ обновлён: {result}", reply_markup=keyboard([]))
    elif cmd == "/refund":
        payment_id = uuid_value(tail.strip())
        await message.answer(
            f"Подтвердите полный возврат платежа {payment_id}. Его вклад в доступ будет отозван.",
            reply_markup=keyboard([[("Подтвердить возврат", f"refundconfirm:{payment_id}")]]),
        )
    elif cmd == "/stats":
        await statistics(message, r, uid)
    elif cmd == "/list":
        parts = tail.split()
        if len(parts) not in (1, 2):
            raise Denied("Формат: /list тип [страница]")
        await listing(message, r, uid, parts[0], int(parts[1]) if len(parts) == 2 else 0)
    else:
        raise Denied("Неизвестная команда. /adminhelp")


async def callback(query, r):
    uid = query.from_user.id
    r.admin.auth(uid)
    await r.limiter.check(uid, "admin")
    await answer_callback(query)
    data = query.data
    if data == "admin":
        await menu(query.message, r, uid)
    elif data == "adminhelp":
        await query.message.answer(HELP, reply_markup=keyboard([]))
    elif data == "adminstats":
        await statistics(query.message, r, uid)
    elif data.startswith("adminlist:"):
        _, kind, page = data.split(":")
        await listing(query.message, r, uid, kind, int(page))
    elif data.startswith("refundconfirm:"):
        await r.refunds.request(uid, uuid_value(data.split(":")[1]), f"admin-refund:{query.id}")
        await query.message.answer(
            "Возврат поставлен в очередь. Результат — в платежах и Audit Log.", reply_markup=keyboard([])
        )
    else:
        raise Denied("Неизвестная кнопка.")
