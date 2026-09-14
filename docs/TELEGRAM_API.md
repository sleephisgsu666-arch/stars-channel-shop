# Проверенный контракт Telegram

Официальная документация проверена 13.09.2026. Источник: [Telegram Bot API](https://core.telegram.org/bots/api).

| Метод/тип | Использование в проекте |
|---|---|
| [sendInvoice](https://core.telegram.org/bots/api#sendinvoice) | Разовая покупка: XTR, integer LabeledPrice, UUID payload, пустой provider_token |
| [createInvoiceLink](https://core.telegram.org/bots/api#createinvoicelink) | Recurring: subscription_period 2592000, максимум 10000 Stars |
| [answerPreCheckoutQuery](https://core.telegram.org/bots/api#answerprecheckoutquery) | Ответ в пределах 10 секунд; приложение ограничивает обработку 8 секундами |
| [SuccessfulPayment](https://core.telegram.org/bots/api#successfulpayment) | Charge IDs, recurring flags, expiration Unix timestamp |
| [BotSubscriptionUpdated](https://core.telegram.org/bots/api#botsubscriptionupdated) | Update.subscription: user, invoice_payload, state; адаптер для aiogram |
| [createChatInviteLink](https://core.telegram.org/bots/api#createchatinvitelink) | creates_join_request=true и expire_date; без member_limit |
| [revokeChatInviteLink](https://core.telegram.org/bots/api#revokechatinvitelink) | Отзыв персонального приглашения |
| [approveChatJoinRequest](https://core.telegram.org/bots/api#approvechatjoinrequest) | Только проверенному владельцу действующего права |
| [declineChatJoinRequest](https://core.telegram.org/bots/api#declinechatjoinrequest) | Чужая, неизвестная, истёкшая ссылка |
| [banChatMember](https://core.telegram.org/bots/api#banchatmember) | Удаление при истечении/отзыве |
| [unbanChatMember](https://core.telegram.org/bots/api#unbanchatmember) | only_if_banned=true после удаления, чтобы разрешить новую покупку |
| [refundStarPayment](https://core.telegram.org/bots/api#refundstarpayment) | Полный возврат по Telegram user ID и charge ID |
| [editUserStarSubscription](https://core.telegram.org/bots/api#edituserstarsubscription) | Отмена автопродления без немедленного отзыва |

SDK зафиксирован в lock-файлах. Неподдерживаемое SDK поле subscription проверяется собственным Pydantic DTO, без выдуманных параметров Telegram-методов. Состояние подписки само по себе никогда не означает оплату.
