# Приёмка: обязательные проверяемые сценарии

Тесты используют frozen/injectable clock и временную SQLite БД на диске; проверка только
in-memory не доказывает persistence. Telegram/LLM по умолчанию fake; реальные вызовы
отдельно. У каждого сценария проверить сохранённый результат и уведомления, а не только текст.

| ID | Сценарий | Ожидаемое |
|---|---|---|
| A01 | 8 октября: «ответить Юле в течение недели» | Срок 15 октября local, одно дело |
| A02 | «Ответ готов, вечером отправлю» | next_action=отправить; не completed |
| A03 | «Отправил ответ» | completed, pending jobs cancelled |
| A04 | Завтра без времени | Явная дата + видимый default 10:00 |
| A05 | Snooze до вечера | due неизменен; один новый reminder 19:00 |
| A06 | Каждый месяц 15-го; отметить октябрь | Октябрь paid; ноябрь 15-го; неоплаченный сентябрь сохранён |
| A07 | 31 января -> февраль -> март | Последний день февраля, затем 31 марта |
| A08 | Трёхмесячное продление | Calendar months; exclusive coverage date ясно в UI |
| A09 | «До января» | Уточнение границы, без mutation |
| A10 | 1200 RUB / 40 per day от нуля | 30 days; threshold 5 через 25 суток |
| A11 | Пополнение/смена rate/backdated correction | Replay правильно меняет forecast и jobs |
| A12 | Forecast <0 | «Расчётный запас исчерпан», не «сервер отключён» |
| A13 | Пополнение после прогнозного нуля | Явная коррекция/допущение, не выдуманный долг |
| A14 | «Все VPS оплатил» | Preview объектов/периодов; atomic apply после подтверждения |
| A15 | Два похожих VPS; «пополнил его» | Уточнение, без случайного выбора |
| A16 | Дубли update/callback/web idempotency key | Одна mutation, один ledger event |
| A17 | Crash after commit before ack | Recover inbox; receipt возвращает результат |
| A18 | Рестарт процесса и чтение БД | Задачи/периоды/jobs/history сохранены |
| A19 | LLM down/refusal/incomplete | Нет ложного успеха; формы/кнопки/reminders работают |
| A20 | Quiet hours, restart Tuesday | Перенос delivery; одна catch-up, без лавины |
| A21 | Monday digest retry/restart | Одна подтверждённая сводка недели; sent не повторяется |
| A22 | Сводка покрыла предмет | Обычный дубль подавлен; точный user reminder сохранён |
| A23 | Telegram timeout после возможной отправки | unknown, bounded delayed retry; no exactly-once claim |
| A24 | Тариф 0 / negative | 0=no depletion, negative rejected |
| A25 | Undo payment/topup и конфликт | Compensation сохранена; при зависимых правках понятный отказ |
| A26 | Чужой Telegram user / forged callback | Read/write/login denied |
| A27 | Session expiry, CSRF, brute force code | Denied; код одноразовый, logs без секретов |
| A28 | Notes HTML/script и prompt injection | Escaped; не исполняются команды из заметок |
| A29 | Миграция и backup/restore в новый путь | Читаемые данные, jobs/receipts/history сохранены |
| A30 | Запуск второго экземпляра | Отказ; не второй poller/scheduler |
| A31 | DST boundary и смена timezone | Один reminder, ожидаемые local dates, история не меняется |
| A32 | Сводка «напоминал»/«оплатили неделю назад» | Только из delivery/payment history |
| A33 | Balance: запас 4 дня при пороге 5; затем пополнение до финального предупреждения | Предупреждение сразу в ответе, без немедленного дубля; одна будущая запись за сутки до расчётного нуля (с учётом тихих часов); пополнение пересчитывает план и отменяет её; при ≤ 1 суток — только явная фраза в ответе, без прошедшего напоминания; везде «оценка» |

## Проверки по этапам

Этап 1: domain/application/storage tests A01–A15, A24–A25, A29 migrations, A31;
A16–A17 на уровне application idempotency/receipts; A18 (сохранение после рестарта) —
полноценно. Telegram inbox/ack и восстановление между commit и транспортным ack
окончательно проверяются на этапе 2.
Естественные фразы пока через fixture proposals, не pretending LLM eval.
Этап 2: scheduler/Telegram integration A16–A23, A26, A30, A32 с fake transport.
Этап 3: OpenAI adapter request/response contract tests + saved intent eval fixtures.
Этап 4: web auth/CSRF, формы и desktop/mobile QA; browser screenshots на 390px и 1440px,
проверка overflow, длинных названий, empty/error states. Playwright ставить только для
этого этапа. Скриншоты с вымышленными данными.
Этап 5: build, health, process restart, backup/restore, затем разрешённый live smoke.

## Готовность к пилоту

- uv sync --frozen --group dev; pytest; Ruff lint/format; reproducible container build.
- README содержит проверенные команды; конфигурация без ключей работает в test/demo.
- Без production secrets production startup явно отказывается/объясняет недостающие поля.
- Локальная дата, суммы, прогнозы и сохранённые изменения обозримы.
- Реальные Telegram delivery на телефон и часы проверены пользователем; настройки
  OS/Telegram не считаются доказанными cloud-тестом.
- С разрешения подключены OpenAI key/model, ограничены вызовы; небольшой live eval
  отличён от fixture тестов. Account/model/network access проверяются отдельно.
- Backup восстановлен в новую БД; выбран внешний health monitoring и копия вне VPS.

Пилот полезности: несколько недель с настоящими обязательствами, включая месячный
платёж. Не выдавать этот будущий пользовательский опыт за уже пройденную приёмку.
