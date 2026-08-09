# tg_brief — план разработки MVP

## 0. Цель MVP

Сделать Telegram-бота-интерфейс, который работает вместе с Telegram user-client.

Пользовательский сценарий:

1. Открыть бота.
2. Получить список доступных Telegram-чатов / групп / каналов из личного аккаунта.
3. Выбрать нужный чат.
4. Выбрать режим:
   - суммировать все непрочитанные;
   - последние 100 сообщений;
   - последние 500 сообщений;
   - последние 1000 сообщений.
5. Получить AI-summary.
6. В summary видеть основные темы и ссылки на важные оригинальные сообщения.

---

## Смена подхода (2026-08-09)

Изначальный план (Node.js + TypeScript + TDLib с нуля) заменён на **форк готового проекта**
[Telebrief](https://github.com/belaytzev/Telebrief) вместо билда с нуля — он уже закрывает
большую часть инфраструктуры.

Причины:
- TDLib на Windows требует нативной сборки (`tdjson.dll`) — болезненно в установке.
- Библиотека `teleproto` (альтернатива GramJS) при аудите оказалась соло-мейнтейнер форком без
  прозрачной истории — решили не рисковать сессией личного аккаунта.
- Telebrief уже реализует user-client (Telethon) + Telegram bot (python-telegram-bot) + AI-саммари
  с темами и ссылками на оригинальные сообщения — то есть почти весь стек из разделов 1–9 старого
  плана, только на Python вместо Node/TS.

Код Telebrief **вендорен локально** в этот репозиторий (не через `git fork` на GitHub — здесь нет
настроенного `gh`/токена). Если понадобится push в собственный GitHub-репозиторий или синхронизация
с апстримом, форкнуть вручную через https://github.com/belaytzev/Telebrief/fork и добавить как
`upstream` remote.

Лицензия: MIT (сохранена, файл `LICENSE` в репозитории).

---

## 1. Что уже есть в Telebrief (переиспользуем как есть)

| Компонент | Файл | Роль |
|---|---|---|
| User-client (Telethon) | `src/collector.py` | подключение к личному аккаунту, чтение сообщений по `entity` + `limit` |
| Bot commands | `src/bot_commands.py` | polling, обработка команд, rate-limit, авторизация по `target_user_id` |
| AI-саммари | `src/summarizer.py`, `src/ai_providers.py` | chunking + вызов OpenAI/Anthropic/Ollama |
| Группировка по темам | `src/grouper.py` | AI-detected topics (аналог `topics[]` из раздела 7 старого плана) |
| Форматирование вывода | `src/formatter.py` | Markdown, эмодзи, ссылки на сообщения |
| Конфиг | `src/config_loader.py`, `config.yaml.example` | настройки, привязанные к `.env` |
| Хранилище (опционально) | `src/storage.py` | SQLite/Postgres — соответствует `storage/` из раздела 4 |
| Одноразовая авторизация | `create_session.py` / `create_session.sh` | интерактивный логин личного аккаунта → `sessions/user.session` |

`.env` полностью совместим с уже созданными переменными:

```env
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=
OPENAI_API_KEY=
LOG_LEVEL=INFO
```

---

## 2. Чего в Telebrief нет и что нужно доделать (адаптация под наш сценарий)

Telebrief рассчитан на **статический список каналов** в `config.yaml` и дайджест по времени
(`lookback_hours`), с командами `/digest`, `/status`, `/cleanup`. Нашему сценарию (раздел 0) нужно
другое:

1. **Динамический список чатов вместо статичного config.yaml**
   Добавить метод в `MessageCollector` (или новый модуль), который вызывает
   `client.get_dialogs()` и возвращает список чатов личного аккаунта с `id`, `title`, `type`,
   `unread_count` — это раздел 5 старого плана (шаги 3–4).

2. **Inline-кнопки выбора чата и режима**
   Новый flow в `bot_commands.py` (или отдельный `src/chat_picker.py`):
   `/start` → кнопки со списком чатов (title + unread) → выбор чата → кнопки режима
   (`Все непрочитанные` / `Последние 100` / `Последние 500` / `Последние 1000`) → генерация.
   Соответствует разделу 8 старого плана. Технически — `CallbackQueryHandler` из
   `python-telegram-bot` с `callback_data` вида `chat:<id>` / `mode:<n>`.

3. **Fetch по количеству и по непрочитанным, а не по времени**
   `fetch_channel_messages` в `collector.py` сейчас работает через `lookback_hours`. Нужно добавить
   режимы:
   - `last N` — `client.iter_messages(entity, limit=N)`;
   - `unread` — прочитать `dialog.unread_count` из `get_dialogs()` и взять именно столько последних
     сообщений (без пометки "прочитано" — как и требовал раздел 6 старого плана).

4. **Саммари по одному выбранному чату "на лету"**
   Сейчас `summarizer.py`/`grouper.py` работают над структурой "канал → сообщения" из конфига.
   Нужно прогнать через них результат fetch для одного произвольного чата, выбранного в рантайме
   (а не из `config.yaml`).

Форматирование вывода (топики + ссылки на сообщения, раздел 9 старого плана) уже покрыто
`formatter.py` — адаптировать по мелочи под "один чат" вместо "несколько каналов".

Scheduled-дайджест по `config.yaml` (родная фича Telebrief) можно оставить как есть — она не мешает
и может пригодиться отдельно.

---

## 3. Порядок работ (обновлённый)

```text
✅ Получить TELEGRAM_API_ID / TELEGRAM_API_HASH (my.telegram.org)
✅ Получить TELEGRAM_BOT_TOKEN (@BotFather)
✅ Форкнуть/вендорить Telebrief, разобрать структуру
✅ Привести .env к формату Telebrief
        ↓
Получить свой Telegram user_id (@userinfobot) → target_user_id в config.yaml
        ↓
Создать config.yaml из config.yaml.example
        ↓
Установить зависимости (uv sync или pip install -r requirements.txt)
        ↓
Авторизовать личный аккаунт: python create_session.py → sessions/user.session
        ↓
Получить список диалогов (get_dialogs) — первая проверка user-client
        ↓
Реализовать динамический список чатов + inline-кнопки (п. 2.1, 2.2)
        ↓
Реализовать fetch по unread / last N (п. 2.3)
        ↓
Прогнать через summarizer/grouper/formatter для одного чата (п. 2.4)
        ↓
End-to-end тест: /start → выбор чата → выбор режима → summary с темами и ссылками
```

---

## Текущий статус

Сделано:

- [x] Название проекта: `tg_brief`
- [x] Создание Telegram application (my.telegram.org)
- [x] `TELEGRAM_API_ID` получен
- [x] `TELEGRAM_API_HASH` получен
- [x] Telegram Bot создан через @BotFather, `TELEGRAM_BOT_TOKEN` получен
- [x] Решение по стеку: форк Telebrief (Python/Telethon/python-telegram-bot) вместо Node/TS/TDLib
- [x] Код Telebrief вендорен в репозиторий, лишнее (маркетинговый `website/`) удалено
- [x] `.env` приведён к формату Telebrief, лишние MTProto-ключи убраны
- [x] Локальный git-репозиторий инициализирован, baseline закоммичен

Дальше:

- [ ] Получить свой Telegram `user_id` через @userinfobot
- [ ] Создать `config.yaml` из `config.yaml.example`, указать `target_user_id`
- [ ] Установить зависимости проекта
- [ ] Авторизовать личный Telegram-аккаунт (`create_session.py`) → `sessions/user.session`
- [ ] Получить и вывести список диалогов (chat_id, title, type, unread_count)
- [ ] Добавить inline-кнопки выбора чата и режима в `bot_commands.py`
- [ ] Добавить fetch по `unread` / `last 100/500/1000` в `collector.py`
- [ ] Связать fetch с `summarizer.py`/`grouper.py` для одного произвольного чата
- [ ] End-to-end проверка сценария из раздела 0
