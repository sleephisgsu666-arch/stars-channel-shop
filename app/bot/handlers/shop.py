from app.bot.callbacks import answer_callback
from sqlalchemy import select
from app.bot.keyboards import keyboard, menu, access_button
from app.core.security import Denied, uuid_value
from app.core.logging import event
from app.db.models import Entitlement, Product, Order
from app.services.catalog import get_user, get_plan


async def start(message, r, telegram_user=None):
    from app.services.catalog import ensure_user
    from app.services.admin_forms import AdminForms

    telegram_user = telegram_user or message.from_user
    user = await ensure_user(r.db, telegram_user)
    if user.is_blocked:
        raise Denied("Пользователь заблокирован.")
    await r.redis.delete(f"wallet-amount:{telegram_user.id}")
    is_admin = telegram_user.id in r.settings.admin_telegram_ids
    if is_admin:
        async with r.db.lock(f"admin-form:{telegram_user.id}"):
            await AdminForms(r).clear(telegram_user.id)
    event("user_started", telegram_user_id=telegram_user.id)
    await message.answer("Выберите раздел:", reply_markup=menu(is_admin))


async def support(message, r):
    await r.limiter.check(message.from_user.id, "support")
    await message.answer(
        f"Поддержка по покупкам и оплате: {r.settings.support_contact}", reply_markup=keyboard([])
    )


async def terms(message, r):
    await message.answer(
        r.settings.terms_text, reply_markup=keyboard([[("Полные условия", r.settings.terms_url)]])
    )


async def catalog(query, r, page):
    products = await r.catalog.page(page)
    rows = [[(p.title, f"product:{p.id}")] for p in products[:8]]
    navigation = []
    if page:
        navigation.append(("←", f"catalog:{page - 1}"))
    if len(products) > 8:
        navigation.append(("→", f"catalog:{page + 1}"))
    if navigation:
        rows.append(navigation)
    await query.message.answer("Каталог" if products else "Каталог пока пуст.", reply_markup=keyboard(rows))


async def product(query, r, product_id, page=0):
    item, plans = await r.catalog.product(product_id, page)
    event("product_viewed", product_id=item.id)
    rows = [
        [(f"{p.name} — {p.price_stars} ⭐" if p.price_stars else f"🎁 {p.name}", f"plan:{p.id}")]
        for p in plans[:8]
    ]
    if page:
        rows.append([("← Тарифы", f"product:{item.id}:{page - 1}")])
    if len(plans) > 8:
        rows.append([("Тарифы →", f"product:{item.id}:{page + 1}")])
    rows.append([("Каталог", "catalog:0")])
    if item.cover_file_id:
        await query.message.answer_photo(item.cover_file_id, reply_markup=keyboard([]))
    await query.message.answer(
        f"{item.title}\n\n{item.description}\n\nВыберите вариант доступа:", reply_markup=keyboard(rows)
    )


async def plan(query, r, plan_id):
    async with r.db.sessions() as s:
        tariff, item = await get_plan(s, plan_id)
    if tariff.billing_type not in ("free", "fixed", "lifetime"):
        raise Denied("Автопродление отключено. Выберите другой тариф.")
    duration = "Навсегда" if tariff.duration_days is None else f"{tariff.duration_days} дней"
    rows = []
    price = tariff.price_stars
    value = await r.wallet.balance(query.from_user.id)
    if tariff.billing_type == "free":
        rows.append([("🎁 Получить бесплатно", f"free:{tariff.id}")])
    else:
        order = await r.wallet.quote(query.from_user.id, tariff.id, f"quote:{query.id}")
        price = order.amount_stars
        duration = "Навсегда" if order.duration_days is None else f"{order.duration_days} дней"
        rows.append([(f"Купить с баланса · {order.amount_stars} ⭐", f"spend:{order.id}")])
    rows += [
        [("💰 Пополнить баланс", "w:balance")],
        [("Условия", r.settings.terms_url)],
        [("Назад", f"product:{item.id}")],
    ]
    await query.message.answer(
        f"{item.title}\n{tariff.name}: {duration}\n"
        f"Стоимость: {price} ⭐\nВаш баланс: {value} ⭐\nБез автопродления.",
        reply_markup=keyboard(rows),
    )


async def purchase(query, r, plan_id, free=False):
    uid = query.from_user.id
    await r.limiter.check(uid, "free" if free else "invoice")
    if free:
        ent = await r.orders.free(uid, plan_id)
        await query.message.answer(
            "Доступ активирован." if ent.status == "active" else "Бесплатный доступ уже использован.",
            reply_markup=access_button(ent.product_id),
        )
        return
    await plan(query, r, plan_id)


async def purchases(query, r, page):
    if not 0 <= page <= 100000:
        raise Denied("Некорректная страница.")
    async with r.db.sessions() as s:
        user = await get_user(s, query.from_user.id)
        rows = (
            await s.execute(
                select(Entitlement, Product)
                .join(Product)
                .where(Entitlement.user_id == user.id)
                .order_by(Entitlement.created_at.desc(), Entitlement.id)
                .offset(page * 5)
                .limit(6)
            )
        ).all()
        orders = list(
            await s.scalars(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.billing_type == "recurring_30d",
                    Order.subscription_charge_id.is_not(None),
                    Order.product_id.in_([e.product_id for e, _ in rows[:5]]),
                )
                .order_by(Order.created_at.desc())
                .limit(20)
            )
        )
    if not rows:
        await query.message.answer("Покупок пока нет.", reply_markup=menu())
    for ent, item in rows[:5]:
        buttons = [[("🔐 Получить доступ", f"access:{item.id}")], [("Продлить", f"product:{item.id}")]]
        for order in orders:
            if order.product_id == item.id:
                buttons.append([("Отменить продление", f"cancel:{order.id}")])
        await query.message.answer(
            f"{item.title}\nСтатус: {ent.status}\n"
            f"До: {ent.expires_at.isoformat() if ent.expires_at else 'навсегда'}",
            reply_markup=keyboard(buttons),
        )
    navigation = []
    if page:
        navigation.append(("←", f"purchases:{page - 1}"))
    if len(rows) > 5:
        navigation.append(("→", f"purchases:{page + 1}"))
    if navigation:
        await query.message.answer("Страницы:", reply_markup=keyboard([navigation]))


async def callback(query, r):
    data = query.data or ""
    if len(data.encode()) > 64:
        raise Denied("Некорректная кнопка.")
    await r.limiter.check(query.from_user.id, "callback")
    await answer_callback(query)
    if data == "support":
        await r.limiter.check(query.from_user.id, "support")
        await query.message.answer(f"Поддержка: {r.settings.support_contact}", reply_markup=keyboard([]))
        return
    if data == "terms":
        await terms(query.message, r)
        return
    parts = data.split(":")
    operation = parts[0]
    if operation in ("catalog", "purchases") and len(parts) == 2:
        try:
            page = int(parts[1])
        except ValueError:
            raise Denied("Некорректная страница.") from None
        await (catalog if operation == "catalog" else purchases)(query, r, page)
        return
    if len(parts) not in (2, 3):
        raise Denied("Неизвестная кнопка.")
    if len(parts) == 3 and operation != "product":
        raise Denied("Некорректная кнопка.")
    entity_id = uuid_value(parts[1])
    if operation == "product":
        try:
            page = int(parts[2]) if len(parts) == 3 else 0
        except ValueError:
            raise Denied("Некорректная страница.") from None
        await product(query, r, entity_id, page)
    elif operation == "plan":
        await plan(query, r, entity_id)
    elif operation in ("buy", "free"):
        await purchase(query, r, entity_id, operation == "free")
    elif operation == "access":
        await r.limiter.check(query.from_user.id, "invite")
        url = await r.access.invite(query.from_user.id, entity_id)
        await query.message.answer(
            "Персональная ссылка действует не более 10 минут.",
            reply_markup=keyboard([[("Подать заявку", url)]]),
        )
    elif operation == "cancel":
        await r.subscriptions.change(query.from_user.id, entity_id, True)
        await query.message.answer(
            "Автопродление отменено. Доступ сохранён до конца оплаченного периода.", reply_markup=keyboard([])
        )
    else:
        raise Denied("Неизвестная кнопка.")
