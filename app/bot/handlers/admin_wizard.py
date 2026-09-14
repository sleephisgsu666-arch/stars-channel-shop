"""Button-driven administration; all writes go through authorized audited services."""

from app.bot.callbacks import answer_callback
import json
from uuid import UUID
from pydantic import ValidationError
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.exc import IntegrityError
from app.bot.keyboards import keyboard
from app.core.security import Denied, uuid_value
from app.services.admin_forms import (
    AdminForms,
    FIELDS,
    LABELS,
    TYPES,
    STATUS,
    clean,
    new_draft,
    next_step,
    parse_input,
)

EDIT = {
    "channel": ["title", "is_active"],
    "product": ["title", "description", "short_description", "cover_file_id", "sort_order", "is_active"],
    "plan": ["name", "billing_type", "price_stars", "duration_days", "sort_order", "is_active"],
}


def button(token, operation, value=""):
    return f"aw:{token}:{operation}" + (f":{value}" if value else "")


def summary(data):
    lines = []
    for key, value in data.items():
        if key.startswith("_") or key in ("channel_id", "product_id"):
            continue
        label = FIELDS.get(
            key, {"revoke": "Отозвать доступ", "status": "Статус", "expires_at": "До"}.get(key, key)
        )
        if key == "cover_file_id":
            value = "Прикреплена" if value else "Без обложки"
        elif isinstance(value, bool):
            value = "Да" if value else "Нет"
        elif value is None:
            value = "Навсегда"
        elif key == "billing_type":
            value = TYPES.get(value, value)
        elif key == "status":
            value = STATUS.get(value, value)
        lines.append(f"{label}: {value}")
    for key, label in [("_channel", "Канал"), ("_product", "Продукт"), ("_balance", "Баланс ⭐")]:
        if key in data:
            lines.insert(0, f"{label}: {data[key]}")
    return "\n".join(lines)


async def show_text(message, text, rows):
    # Long descriptions must not exceed Telegram's message limit.
    for start in range(0, max(1, len(text)), 3800):
        last = start + 3800 >= len(text)
        await message.answer(
            text[start : start + 3800] or "Пусто", reply_markup=keyboard(rows) if last else keyboard([])
        )


async def listing(message, r, uid, kind, page=0):
    forms = AdminForms(r)
    records = await forms.page(uid, kind, page)
    rows = []
    if kind in ("channel", "product", "plan"):
        rows.append([("➕ Добавить", f"anew:{kind}")])
    if kind == "access":
        rows.append([("➕ Выдать доступ", "anew:access")])
    for entity_id, label, data in records:
        marker = "✅ " if data.get("is_active") else ""
        rows.append([(marker + label[:55], f"aopen:{kind}:{entity_id}")])
    nav = []
    if page:
        nav.append(("←", f"adminlist:{kind}:{page - 1}"))
    if len(records) == 8:
        nav.append(("→", f"adminlist:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("← Админ-панель", "admin")])
    await message.answer(
        f"{LABELS[kind]} · страница {page + 1}" + ("\nПока ничего нет." if not records else ""),
        reply_markup=keyboard(rows),
    )


async def card(message, r, uid, kind, entity_id):
    _, label, data = await AdminForms(r).record(uid, kind, entity_id)
    rows = []
    if kind in EDIT:
        for field in EDIT[kind]:
            if kind == "plan" and (
                (field == "price_stars" and data["billing_type"] == "free")
                or (field == "duration_days" and data["billing_type"] in ("lifetime", "recurring_30d"))
            ):
                continue
            rows.append([(f"✏️ {FIELDS[field]}", f"aedit:{kind}:{entity_id}:{EDIT[kind].index(field)}")])
    elif kind == "access":
        rows += [
            [("Продлить / выдать навсегда", f"aaccess:extend:{entity_id}")],
            [("Отозвать доступ", f"aaccess:revoke:{entity_id}")],
        ]
    elif kind == "user":
        rows.append([("Выдать доступ", f"auser:{entity_id}")])
    elif kind == "purchase" and data["status"] == "paid":
        rows.append([("Вернуть покупку на баланс", f"aprefund:{entity_id}")])
    elif kind == "payment" and data["status"] == "paid":
        rows.append([("↩️ Вернуть Stars", f"arefund:{entity_id}")])
    rows.append([("← К списку", f"adminlist:{kind}:0")])
    await show_text(message, label + "\n\n" + summary(data), rows)


async def prompt(message, r, uid, draft, page=0):
    token, field = draft["token"], draft["step"]
    rows = []
    text = ""
    if field == "confirm":
        text = "Проверьте перед сохранением:\n\n" + summary(draft["data"])
        if draft["kind"] == "purchase_refund":
            text += "\n\nСтоимость покупки вернётся на баланс магазина. Доступ по этой покупке будет отозван."
        if draft["kind"] == "refund":
            text += "\n\nБудет выполнен полный возврат этой оплаты. Её вклад в доступ будет отозван."
        if draft["data"].get("revoke"):
            text += "\n\nПользователь потеряет доступ. Удаление выполнит worker."
        rows.append([("✅ Подтвердить", button(token, "save"))])
    elif field in ("channel_id", "product_id", "telegram_user_id"):
        kind = {"channel_id": "channel", "product_id": "product", "telegram_user_id": "user"}[field]
        records = await AdminForms(r).page(uid, kind, page)
        text = f"Выберите: {FIELDS[field].lower()}."
        if field == "telegram_user_id":
            text += "\nМожно также отправить числовой Telegram ID пользователя, который уже нажал /start."
        if not records:
            text += "\nСписок пуст. Сначала добавьте запись в соответствующем разделе."
        for entity_id, label, _ in records:
            rows.append([(label[:55], button(token, "pick", entity_id))])
        nav = []
        if page:
            nav.append(("←", button(token, "page", str(page - 1))))
        if len(records) == 8:
            nav.append(("→", button(token, "page", str(page + 1))))
        if nav:
            rows.append(nav)
    elif field == "billing_type":
        text = "Выберите тип тарифа:"
        rows += [[(label, button(token, "value", value))] for value, label in TYPES.items()]
    elif field == "is_active":
        text = "Сделать доступным?"
        if draft["kind"] == "product":
            text += "\nДа — показать в каталоге. Нет — оставить скрытым."
        rows += [[("✅ Да", button(token, "value", "yes")), ("Нет", button(token, "value", "no"))]]
    else:
        text = {
            "telegram_chat_id": "Отправьте числовой ID закрытого канала (начинается с -100).\n"
            "Или перешлите сообщение из канала. Бот уже должен быть его администратором.",
            "title": "Введите название:",
            "name": "Введите название тарифа, например «90 дней»: ",
            "description": "Отправьте описание продукта (до 3000 символов):",
            "short_description": "Отправьте краткое описание (до 500 символов):",
            "cover_file_id": "Отправьте фотографию для обложки:",
            "price_stars": "Введите цену целым числом в Stars:",
            "duration_days": "Введите срок в днях:",
            "days": "На сколько дней выдать или продлить доступ?",
            "sort_order": "Введите порядок в списке: меньшие числа показываются первыми.",
        }[field]
        if field == "price_stars" and draft["data"].get("billing_type") == "recurring_30d":
            text += "\nДля подписки: от 1 до 10000 Stars."
        if field in ("cover_file_id", "description", "short_description"):
            rows.append(
                [("Без обложки" if field == "cover_file_id" else "Оставить пустым", button(token, "empty"))]
            )
        if field == "days" or (field == "duration_days" and draft["data"].get("billing_type") == "free"):
            rows.append([("♾ Навсегда", button(token, "forever"))])
    rows.append([("Отмена", button(token, "cancel"))])
    await show_text(message, text, rows)


async def begin(message, r, uid, draft):
    draft["last_message"] = message.message_id
    await AdminForms(r).put(uid, draft)
    await prompt(message, r, uid, draft)


async def accept(message, r, uid, draft, value):
    field = draft["step"]
    if field == "price_stars" and draft["data"].get("billing_type") == "recurring_30d" and value > 10000:
        raise Denied("Цена подписки не должна превышать 10000 Stars.")
    draft["data"][field] = value
    next_step(draft)
    await AdminForms(r).put(uid, draft)
    await prompt(message, r, uid, draft)


async def submit(message, r, uid, draft):
    forms = AdminForms(r)
    kind = draft["kind"]
    data = clean(draft["data"])
    if kind in ("product", "plan", "channel"):
        try:
            result = await r.admin.save(
                uid,
                kind,
                UUID(draft["entity_id"]) if draft["entity_id"] else None,
                json.dumps(data, ensure_ascii=False),
                draft["request_key"],
                expected_data=draft["baseline"],
            )
        except TelegramAPIError:
            raise Denied(
                "Не удалось проверить канал в Telegram. Убедитесь, что ID верный, канал закрытый, "
                "а бот — администратор с правами приглашать и блокировать участников. "
                "Повторите подтверждение или отмените форму."
            ) from None
        except ValidationError:
            raise Denied("Поля тарифа не согласованы. Отмените форму и выберите тип тарифа заново.") from None
        except IntegrityError:
            raise Denied(
                "Этот канал уже добавлен или уже связан с продуктом. Отмените форму и выберите другую запись."
            ) from None
        await message.answer("✅ Сохранено.", reply_markup=keyboard([]))
        await forms.clear(uid)
        await card(message, r, uid, kind, result)
    elif kind == "access":
        await r.admin.manual(uid, json.dumps(data), draft["request_key"])
        await forms.clear(uid)
        await message.answer(
            "✅ Доступ обновлён.", reply_markup=keyboard([[("К доступам", "adminlist:access:0")]])
        )
    elif kind == "purchase_refund":
        await r.wallet.refund_purchase(uid, UUID(draft["entity_id"]), draft["request_key"])
        await forms.clear(uid)
        await message.answer("Покупка возвращена на баланс.", reply_markup=keyboard([]))
    elif kind == "refund":
        await r.refunds.request(uid, UUID(draft["entity_id"]), draft["request_key"])
        await forms.clear(uid)
        await message.answer(
            "Возврат поставлен в очередь.", reply_markup=keyboard([[("К платежам", "adminlist:payment:0")]])
        )


async def callback(query, r):
    uid = query.from_user.id
    r.admin.auth(uid)
    await r.limiter.check(uid, "admin")
    await answer_callback(query)
    forms = AdminForms(r)
    async with r.db.lock(f"admin-form:{uid}"):
        parts = (query.data or "").split(":")
        if len((query.data or "").encode()) > 64:
            raise Denied("Некорректная кнопка.")
        op = parts[0]
        if op == "anew" and len(parts) == 2:
            await begin(query.message, r, uid, new_draft(parts[1]))
        elif op == "aopen" and len(parts) == 3:
            await card(query.message, r, uid, parts[1], uuid_value(parts[2]))
        elif op == "aedit" and len(parts) == 4:
            kind, entity_id, field_index = parts[1:]
            if not field_index.isdigit() or int(field_index) >= len(EDIT.get(kind, [])):
                raise Denied("Это поле нельзя изменить.")
            field = EDIT[kind][int(field_index)]
            if field not in EDIT.get(kind, []):
                raise Denied("Это поле нельзя изменить.")
            _, _, data = await forms.record(uid, kind, uuid_value(entity_id))
            await begin(query.message, r, uid, new_draft(kind, entity_id, data, field))
        elif op == "aaccess" and len(parts) == 3 and parts[1] in ("extend", "revoke"):
            _, _, data = await forms.record(uid, "access", uuid_value(parts[2]))
            data = {k: v for k, v in data.items() if k in ("telegram_user_id", "product_id", "_product")}
            data.update(days=None, revoke=parts[1] == "revoke")
            draft = new_draft("access", data=data, field="days")
            if data["revoke"]:
                draft["step"] = "confirm"
            await begin(query.message, r, uid, draft)
        elif op == "auser" and len(parts) == 2:
            _, _, data = await forms.record(uid, "user", uuid_value(parts[1]))
            draft = new_draft("access")
            draft["data"].update(data)
            next_step(draft)
            await begin(query.message, r, uid, draft)
        elif op == "aprefund" and len(parts) == 2:
            _, _, data = await forms.record(uid, "purchase", uuid_value(parts[1]))
            await begin(query.message, r, uid, new_draft("purchase_refund", parts[1], data))
        elif op == "arefund" and len(parts) == 2:
            _, _, data = await forms.record(uid, "payment", uuid_value(parts[1]))
            await begin(query.message, r, uid, new_draft("refund", parts[1], data))
        elif op == "aw" and len(parts) in (3, 4):
            draft = await forms.load(uid)
            if not draft or draft["token"] != parts[1]:
                raise Denied("Эта кнопка устарела. Используйте последнее сообщение или откройте /admin.")
            action = parts[2]
            field = draft["step"]
            value = parts[3] if len(parts) == 4 else ""
            if action == "cancel":
                await forms.clear(uid)
                await query.message.answer(
                    "Изменения отменены.", reply_markup=keyboard([[("Админ-панель", "admin")]])
                )
            elif action == "save" and field == "confirm":
                await submit(query.message, r, uid, draft)
            elif action == "page" and field in ("channel_id", "product_id", "telegram_user_id"):
                await prompt(query.message, r, uid, draft, int(value))
            elif action == "pick" and field in ("channel_id", "product_id", "telegram_user_id"):
                kind = {"channel_id": "channel", "product_id": "product", "telegram_user_id": "user"}[field]
                entity_id, label, data = await forms.record(uid, kind, uuid_value(value))
                if kind in ("channel", "product"):
                    draft["data"]["_" + kind] = label
                await accept(
                    query.message, r, uid, draft, data["telegram_user_id"] if kind == "user" else entity_id
                )
            elif action == "value" and field == "billing_type" and value in TYPES:
                await accept(query.message, r, uid, draft, value)
            elif action == "value" and field == "is_active" and value in ("yes", "no"):
                await accept(query.message, r, uid, draft, value == "yes")
            elif action == "empty" and field in ("cover_file_id", "description", "short_description"):
                await accept(query.message, r, uid, draft, None if field == "cover_file_id" else "")
            elif action == "forever" and (
                field == "days" or (field == "duration_days" and draft["data"].get("billing_type") == "free")
            ):
                await accept(query.message, r, uid, draft, None)
            else:
                raise Denied("Кнопка не соответствует текущему шагу.")
        else:
            raise Denied("Неизвестная кнопка.")


async def message(message, r):
    uid = message.from_user.id
    r.admin.auth(uid)
    await r.limiter.check(uid, "admin")
    forms = AdminForms(r)
    async with r.db.lock(f"admin-form:{uid}"):
        draft = await forms.load(uid)
        if (message.text or "").split("@")[0] == "/cancel":
            await forms.clear(uid)
            await message.answer("Изменения отменены.", reply_markup=keyboard([[("Админ-панель", "admin")]]))
            return
        if not draft:
            await message.answer(
                "Откройте /admin и выберите раздел → «Добавить» или запись для редактирования.",
                reply_markup=keyboard([]),
            )
            return
        if message.message_id <= draft.get("last_message", 0):
            return
        field = draft["step"]
        if field in ("confirm", "billing_type", "is_active", "channel_id", "product_id"):
            await prompt(message, r, uid, draft)
            return
        value = None
        if field == "telegram_chat_id":
            origin = message.forward_origin
            if origin and origin.type == "channel":
                value = origin.chat.id
        if value is None:
            value = parse_input(field, message.text, message.photo)
        draft["last_message"] = message.message_id
        await accept(message, r, uid, draft, value)
