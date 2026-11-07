# Модель данных и инварианты

Ниже обязательный логический контракт; Claude уточняет физические колонки и индексы,
сохраняя эти различия. UUID/text IDs; UTC timestamps; IANA timezone; деньги integer
minor units + currency. Не суммировать разные валюты. SQLAlchemy JSON только для
ограниченных versioned payload/audit, не вместо запросных колонок.

| Сущность | Основное содержание |
|---|---|
| settings | timezone, notification defaults, quiet hours, digest time, tone |
| task | title, note, optional safe URL, next_action, due_date/due_at, status, version |
| obligation | title, aliases, kind calendar/coverage/balance, currency, active, version |
| recurrence | owner type/id, frequency, interval, weekday/monthday, anchor date; timezone берётся из settings |
| occurrence | owner type/id, logical period key, nominal due, effective due, status, version |
| payment | obligation_id, paid_at, amount optional, occurrence_id or coverage_until |
| balance_event | obligation_id, time, kind topup/correction/rate_change, amount/rate |
| command_receipt | unique command_id, source key, status/result, applied_at |
| audit_event | command_id, entity, before/after, reason, created_at |
| pending_intent | proposed actions, candidates, expected versions, expires_at, status |
| telegram_inbox | unique update_id, payload, received_at, processing status/result |
| notification_job | subject, occurrence, kind, scheduled_at, revision, status, dedup key |
| delivery | job_id, attempt, pending/sending/sent/failed/unknown, provider_message_id |
| auth_code / web_session | hashed credentials, expiry, failures/revocation |

Повторяющееся дело — task с recurrence; его occurrence имеет собственный статус.
Для разового дела статус живёт в task. Calendar payment использует obligation +
recurrence + occurrence; coverage использует последовательность оплаченных интервалов;
balance использует события и derived forecast. Не прятать три типа в одинаковое «paid».

## Инварианты

1. UNIQUE(owner_type, owner_id, logical_period_key) для occurrence.
2. Номинальный период сохраняет идентичность при переносе effective_due. Изменение
   постоянного правила касается будущих незавершённых событий, не оплаченной истории.
3. command_receipt.command_id unique. Источник Telegram: update_id; callback:
   semantic action token с entity/version, не новый id каждый click. Web: idempotency key.
   Повтор выдаёт исходный результат, не повторяет mutation.
4. Проверка version и запись результата в одной transaction. Конфликт -> понятное
   предложение обновить данные; pending confirmation не применяет устаревший preview.
5. Bulk operations атомарны: либо все валидные действия набора, либо никакие. Уточнения
   собираются до commit. Список IDs показывается при массовой оплате/отмене.
6. Пополнение balance — не payment календарного периода. «Октябрь оплачен» не удаляет
   ноябрь. Неоплаченный прошлый occurrence остаётся открытым.
7. «Пропустить период» создаёт skipped, «отменить сервис» выключает future planning;
   исторические платежи сохраняются. Для outstanding показать явный выбор закрытия.
8. Undo — compensating command, не удаление истории. Доступен если нет зависимых
   поздних изменений; иначе предложить явную корректировку. Отправленное сообщение
   невозможно отменить, но связанные будущие jobs можно отменить.
9. Состояние overdue выводится из времени и незавершённости. Snooze не меняет due.
10. Balance forecast строится из событий; не хранить ежедневно мутируемый «истинный» остаток.
11. Job dedup включает subject/period/kind/schedule_revision. Изменение сроков/rate/topup
    отменяет старые pending jobs и создаёт новые; sender сверяет revision перед отправкой.
12. Telegram updates persist before offset acknowledgement. Recover processing после
    рестарта; application receipt защищает от mutation после crash между commit/ack.

Индексы: due/status, scheduled_at/status, obligation/time для ledger, pending expiry.
Delete в UI — archival с сохранением истории; необратимая очистка не входит в MVP.
