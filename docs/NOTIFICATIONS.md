# Уведомления, понедельничная сводка и доставка

## Defaults, которые пользователь может изменить

Timezone Asia/Krasnoyarsk; quiet 23:00–09:00; digest Monday 10:00.
У date-only due notice 10:00; advance default 3 days — только для платежей; balance
threshold 5 days (если запас ниже порога уже при пополнении: предупреждение в ответе и
одно финальное за сутки до расчётного нуля, при ≤ 1 суток — только в ответе). Coverage: advance за 3 календарных дня до coverage_until; последнее
своевременное напоминание в coverage_until − 1 день («сегодня последний оплаченный
день»); один follow-up через настроенный интервал от начала coverage_until с учётом
тихих часов.
После пропуска срока: одно отдельное обращение через 24 часа после окончания due date;
далее weekly digest/overdue list. Для важных обязательств отдельный профиль только
после явного выбора пользователя. Daily digest выключен.

Показать настройки в onboarding; численные значения ещё не подтверждены пользователем.
Тихие часы переносят delivery к окончанию quiet, не due. Исключение только явное.
«Вечером» -> 19:00; «позже» без времени -> показать default 2 часа и дать изменить.
Snooze отменяет pending обращение, не закрывает дело и не меняет срок.

## Планирование

DB job на advance/due/overdue/balance-low/digest с deterministic dedup key и revision.
Scheduler tick 30 секунд выбирает due jobs в короткой transaction; sender выполняет
HTTP вне неё и записывает результат. Claim/lease предотвращает одновременно два
обработчика, процесс lock предотвращает второй scheduler/poller на volume.
При startup reclaim истёкших leases и проверка outdated jobs.

Не более одного обычного самостоятельного сообщения по одному предмету за локальный
день, кроме явного snooze/назначенного пользователем точного reminder. Default общий
предел 4 автоматических обычных сообщения/день (digest включён), избыток объединить;
это инженерный default, не пользовательское разрешение спамить. Manual replies не
считаются. Близкие jobs в одном tick можно объединить без сдвига точного user reminder.
Успешная сводка помечает покрытые subjects/stages и подавляет обычные дубли до конца
дня. Не подавлять на основании failed/pending digest и не отменять точный reminder.

## Рестарт / пропущенное

Jobs старше 6 часов default -> одна catch-up сводка актуального, вместо replay всех
старых сообщений. Новые ещё актуальные deadline jobs доставлять; завершённые, archived
и старые revisions отменять. Digest unique(local_week_start, timezone): не две сводки
за неделю. При возвращении во вторник — одна актуальная сводка с пометкой задержки.

## Telegram: границы надёжности

Persist inbox updates before acknowledgement; receipt prevents repeated commands.
Callback отвечает быстро, semantic token owner/entity/version проверяется на сервере.
Transport retries: retry_after, 1m/5m/30m transient backoff; permanent forbidden ->
delivery_failed, web показывает проблему; это не пометка «пользователь видел».

API sendMessage не даёт нашему приложению end-to-end exactly-once. Если Telegram принял
сообщение, но ответ потерялся, результат unknown. Не обещать абсолютное отсутствие
дублей: unknown не resend сразу, отложить retry минимум на 30 минут, ограничить число
попыток двумя, логировать возможный дубль. Следующая сводка включает нерешённое.
Подтверждённая доставка с message_id не пересылается при рестарте. DB dedup защищает
нашу очередь, но не убирает неопределённость внешней доставки.

## Содержание и характер

Short actionable fact + выбор done/paid/snooze/open. Мат допустим по owner setting,
не влияет на cadence и не обязателен в каждом сообщении. «Срок прошёл, отметки нет»;
«по расчёту осталось примерно …»; delivery history только «напоминание отправлялось».
Report: urgent -> due this week -> long pending -> кратко спокойные платежи.
Давность — с saved created_at/last action, не выдуманное время работы над задачей.
