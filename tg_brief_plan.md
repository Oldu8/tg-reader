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

## 1. Telegram application

Открыть:

`https://my.telegram.org`

Далее:

`API development tools` → `Create new application`

Заполнить:

- App title: `TG Brief`
- Short name: `tgbrief`
- URL: можно оставить пустым
- Platform: `Desktop`
- Description: `Personal Telegram client for summarizing messages and chats.`

После создания приложения на странице конфигурации должны быть параметры:

- `App api_id`
- `App api_hash`

Они нужны для подключения user-client к Telegram API.

Сохранить позже в `.env`:

```env
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
```

Важно: `api_hash` нельзя публиковать и коммитить в Git.

### Что такое "Available MTProto servers"

Блок вида:

- Test configuration
- Production configuration
- Public keys
- MTProto server addresses

— это серверная конфигурация Telegram.

Для обычной разработки через TDLib вручную эти IP, порты и RSA public keys использовать не нужно.

Нужны именно `api_id` и `api_hash`.

Обычно они находятся выше на той же странице в секции **App configuration**.

---

## 2. Telegram Bot

Через `@BotFather` создать обычного Telegram-бота.

Получить:

```env
TELEGRAM_BOT_TOKEN=
```

Этот бот будет только интерфейсом:

- показывать кнопки;
- выбирать чат;
- выбирать количество сообщений;
- присылать summary.

Он сам не сможет читать историю личного Telegram-аккаунта.

---

## 3. Telegram user-client

Для чтения личных чатов использовать официальный Telegram client API.

Предпочтительный вариант:

**TDLib**

User-client будет:

- авторизовываться через личный Telegram-аккаунт;
- получать список чатов;
- получать unread_count;
- читать историю;
- получать message_id;
- работать с группами, супергруппами и каналами.

---

## 4. Начальная структура проекта

```text
tg_brief/
├── src/
│   ├── bot/
│   ├── telegram-client/
│   ├── summarizer/
│   ├── storage/
│   └── index.ts
├── data/
├── .env
├── .env.example
├── .gitignore
├── package.json
└── tsconfig.json
```

Стек для MVP:

- Node.js
- TypeScript
- TDLib
- Telegram Bot API
- OpenAI API
- SQLite

---

## 5. Первый milestone — без AI и без бота

Сначала сделать маленький CLI-прототип.

Он должен уметь:

1. Запустить TDLib.
2. Авторизовать личный Telegram-аккаунт.
3. Получить последние 20–50 чатов.
4. Вывести:
   - chat_id;
   - title;
   - type;
   - unread_count.
5. Выбрать один chat_id.
6. Скачать последние 100 сообщений.
7. Сохранить их в `messages.json`.

Пример структуры сообщения:

```ts
{
  messageId: number;
  chatId: number;
  senderId: number;
  date: number;
  text: string;
  replyToMessageId?: number;
}
```

---

## 6. Получение непрочитанных

В дальнейшем реализовать отдельный режим:

`Summarize unread`

Логика:

1. Получить состояние чата и `last_read_inbox_message_id`.
2. Читать историю назад.
3. Собрать сообщения новее последнего прочитанного.
4. Передать их в summarizer.

Не помечать сообщения прочитанными автоматически на первом этапе.

---

## 7. OpenAI summary

Добавить:

```env
OPENAI_API_KEY=
```

Не отправлять 1000–3000 сообщений одним огромным prompt.

Использовать chunking:

```text
messages
   ↓
chunk 1 → summary
chunk 2 → summary
chunk 3 → summary
   ↓
final summary
```

Желательно просить модель возвращать JSON:

```json
{
  "topics": [
    {
      "title": "Стоматологии",
      "summary": "Обсуждали...",
      "importantMessageIds": [123, 150]
    }
  ]
}
```

Backend затем сам преобразует `messageId` в ссылки на Telegram-сообщения.

---

## 8. Telegram Bot UI

Начальный интерфейс:

```text
Выберите чат:

[Украинцы в Астурии — 3124 unread]
[IT Spain — 486 unread]
[Аренда — 95 unread]
```

После выбора:

```text
Украинцы в Астурии

[Все непрочитанные]
[Последние 100]
[Последние 500]
[Последние 1000]
[Назад]
```

---

## 9. Формат summary

Пример:

```text
Украинцы в Астурии
Проанализировано: 500 сообщений

1. Стоматологии

Обсуждали несколько клиник в Овьедо.
Пользователи сравнивали цены, очереди и качество обслуживания.

Полезные сообщения:
→ начало обсуждения
→ рекомендация клиники

2. Документы

Обсуждали ...
```

---

## 10. Порядок разработки

Рекомендуемый порядок:

```text
Telegram App credentials
        ↓
TDLib authorization
        ↓
List chats
        ↓
Read 100 messages
        ↓
messages.json
        ↓
OpenAI summary
        ↓
Telegram bot
        ↓
Inline buttons
        ↓
Unread mode
        ↓
Links to original messages
```

---

## Текущий статус

Сделано:

- [x] Название проекта: `tg_brief`
- [x] Создание Telegram application начато
- [ ] Найти `api_id`
- [ ] Найти `api_hash`
- [ ] Создать локальный Node.js + TypeScript проект
- [ ] Подключить TDLib
- [ ] Авторизовать Telegram account
- [ ] Получить список чатов
- [ ] Получить последние 100 сообщений
- [ ] Добавить OpenAI summary
- [ ] Создать Telegram Bot UI
