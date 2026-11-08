# mailru-mcp

MCP-сервер для почты Mail.ru: чтение, поиск, отправка, ответы, пересылка, черновики.

## 1. Подготовка ящика Mail.ru

1. **Настройки → Все настройки → Почтовые программы**: включи доступ к ящику
   через почтовые клиенты (IMAP).
2. **Настройки → Безопасность → Пароли для внешних приложений** → создай пароль
   с полным доступом (IMAP + SMTP). Обычный пароль от ящика не подойдёт.

Серверы (уже прописаны по умолчанию): `imap.mail.ru:993` (SSL), `smtp.mail.ru:465` (SSL).

## 2. Установка (pixi)

```powershell
cd C:\utm\ai\mcp
pixi init
pixi add python=3.12
pixi add --pypi "mcp[cli]" imap-tools html2text python-dotenv
```

## 3. Файл .env

Скопируй шаблон и впиши свои данные:

```powershell
copy .env.example .env
```

```env
EMAIL_USER=ты@mail.ru
EMAIL_PASSWORD=пароль_приложения
EMAIL_TIMEZONE=Europe/Chisinau
```

Сервер ищет `.env` **рядом с `server.py`**, так что неважно, из какой папки его
запускают. `.env` уже в `.gitignore` — не коммить его.

Если переменная задана и в окружении (например, в блоке `env` конфига Claude),
и в `.env`, побеждает окружение.

## 4. Проверка

```powershell
pixi run mcp dev server.py
```

Откроется веб-инспектор. Сначала вызови `list_folders`, потом `list_emails`,
потом отправь письмо самому себе через `send_email`.

## 5. Подключение

### Claude Desktop

Файл `%APPDATA%\Claude\claude_desktop_config.json`. Блок `env` не нужен —
пароль берётся из `.env`:

```json
{
  "mcpServers": {
    "mailru": {
      "command": "C:\\utm\\ai\\mcp\\.pixi\\envs\\default\\python.exe",
      "args": ["C:\\utm\\ai\\mcp\\server.py"]
    }
  }
}
```

Перезапусти Claude Desktop.

### Claude Code

```powershell
claude mcp add mailru -- C:\utm\ai\mcp\.pixi\envs\default\python.exe C:\utm\ai\mcp\server.py
```

## Тулы

| Тул | Для чего |
|---|---|
| `list_emails` | последние N писем, за сегодня / вчера / неделю / дату, только непрочитанные, от кого-то |
| `search_emails` | поиск по тексту, теме, отправителю, получателю, датам |
| `read_email` | полный текст письма и список вложений |
| `list_folders` | папки с количеством писем и непрочитанных |
| `get_attachment` | сохранить вложение на диск |
| `find_contact` | найти адрес по имени в истории переписки |
| `send_email` | отправить письмо (cc, bcc, HTML, вложения) |
| `reply_email` | ответить в той же цепочке (можно всем) |
| `forward_email` | переслать с вложениями |
| `create_draft` | сохранить в черновики |
| `mark_emails` | прочитано / непрочитано / флажок |
| `move_emails` | переместить в папку |
| `delete_emails` | в корзину (из корзины — навсегда) |

В параметре `folder` можно писать короткие имена: `inbox`, `sent`, `drafts`,
`trash`, `spam`, `archive`.

## Все переменные

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `EMAIL_USER` | — (обязательно) | адрес ящика |
| `EMAIL_PASSWORD` | — (обязательно) | пароль для внешних приложений |
| `EMAIL_TIMEZONE` | системный | от этого пояса считается «сегодня» |
| `EMAIL_DOWNLOAD_DIR` | `~/Downloads/mcp-email` | куда сохраняются вложения |
| `EMAIL_SAVE_SENT` | `false` | `true`, если отправленные через сервер письма не появляются в «Отправленных» |
| `IMAP_HOST`, `SMTP_HOST`, ... | mail.ru | для другого провайдера |

## Примеры запросов к Claude

- «Покажи письма за сегодня»
- «Последние 10 непрочитанных»
- «Найди письма от деканата за ноябрь»
- «Ответь Ивану на последнее письмо, что я согласен, и приложи C:\utm\report.pdf»
