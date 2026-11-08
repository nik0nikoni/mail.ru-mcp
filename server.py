"""
MCP-сервер для почты Mail.ru (IMAP для чтения, SMTP для отправки).

Переменные окружения (можно положить в файл .env рядом с server.py):
    EMAIL_USER          — адрес, например ivan@mail.ru (обязательно)
    EMAIL_PASSWORD      — пароль для внешних приложений (обязательно)
    EMAIL_TIMEZONE      — часовой пояс для "today"/"yesterday", например Europe/Chisinau
                          (по умолчанию — системный)
    EMAIL_DOWNLOAD_DIR  — куда сохранять вложения (по умолчанию ~/Downloads/mcp-email)
    EMAIL_SAVE_SENT     — "true", если отправленные письма не появляются в папке
                          "Отправленные" сами (по умолчанию false)
    IMAP_HOST / IMAP_PORT / SMTP_HOST / SMTP_PORT — если нужен другой провайдер
"""

import functools
import logging
import mimetypes
import os
import re
import smtplib
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

import html2text
from dotenv import load_dotenv
from imap_tools import AND, MailBox, MailMessage, MailMessageFlags
from imap_tools.errors import MailboxLoginError
from mcp.types import ToolAnnotations

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

# Для stdio-транспорта stdout занят протоколом MCP, поэтому логи пишем только в stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("mailru-mcp")

# ---------------------------------------------------------------- настройки

# .env ищем рядом с server.py, а не в текущей папке: Claude Desktop запускает
# сервер из своей рабочей директории. Переменные, уже заданные в окружении
# (например, в блоке "env" конфига), имеют приоритет над .env.
load_dotenv(Path(__file__).resolve().parent / ".env")

IMAP_HOST = os.getenv("IMAP_HOST", "imap.mail.ru")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
TZ = ZoneInfo(os.environ["EMAIL_TIMEZONE"]) if os.getenv("EMAIL_TIMEZONE") else datetime.now().astimezone().tzinfo
DOWNLOAD_DIR = Path(os.getenv("EMAIL_DOWNLOAD_DIR", Path.home() / "Downloads" / "mcp-email"))
SAVE_SENT_COPY = os.getenv("EMAIL_SAVE_SENT", "false").lower() in ("1", "true", "yes")

MAX_LIMIT = 50  # больше писем за раз модели не нужно — только забьёт контекст

# Короткие имена папок -> (IMAP special-use флаг, запасные названия)
FOLDER_ALIASES = {
    "inbox": (None, ["INBOX"]),
    "sent": ("\\Sent", ["Отправленные", "Sent"]),
    "drafts": ("\\Drafts", ["Черновики", "Drafts"]),
    "trash": ("\\Trash", ["Корзина", "Trash"]),
    "spam": ("\\Junk", ["Спам", "Spam", "Junk"]),
    "archive": ("\\Archive", ["Архив", "Archive"]),
    "self": (None, ["INBOX/ToMyself", "Письма себе", "ToMyself"]),
}

mcp = MCPServer(
    "mailru-email",
    instructions=(
        "Работа с почтой Mail.ru. id письма действителен только внутри своей папки, "
        "поэтому вместе с id передавай тот же folder, из которого письмо получено. "
        "Перед отправкой письма покажи пользователю адресата, тему и текст."
    ),
)

def tool(annotations: ToolAnnotations):
    """@mcp.tool, который отдаёт модели понятный текст ошибки, а не "Error executing tool"."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except (OSError, smtplib.SMTPException) as e:
                log.exception("Сетевая ошибка в %s", fn.__name__)
                raise ToolError(f"Ошибка соединения с почтовым сервером: {e}") from e
            except Exception as e:
                log.exception("Ошибка в %s", fn.__name__)
                raise ToolError(str(e)) from e
        return mcp.tool(annotations=annotations)(wrapper)
    return decorator


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


# ------------------------------------------------------------- помощники IMAP

def _credentials() -> tuple[str, str]:
    user, password = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASSWORD")
    if not user or not password:
        raise RuntimeError("Не заданы переменные окружения EMAIL_USER и EMAIL_PASSWORD.")
    return user, password


def _resolve_folder(mb: MailBox, folder: str) -> str:
    """Превращает то, что передала модель, в настоящее имя папки на сервере:
    'sent' -> 'Отправленные', 'ToMyself' -> 'INBOX/ToMyself', 'inbox' -> 'INBOX'."""
    key = folder.strip().lower()
    folders = mb.folder.list()
    lowered = {f.name.lower(): f.name for f in folders}

    if key in FOLDER_ALIASES:
        flag, names = FOLDER_ALIASES[key]
        if flag:
            for f in folders:
                if flag in f.flags:
                    return f.name
        candidates = names
    else:
        candidates = [folder.strip()]

    for name in candidates:
        if name.lower() in lowered:  # точное совпадение
            return lowered[name.lower()]
    for name in candidates:  # по последней части пути: ToMyself -> INBOX/ToMyself
        matches = [f.name for f in folders
                   if re.split(r"[/.]", f.name)[-1].lower() == name.lower()]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(
        f"Папка '{folder}' не найдена. Есть: {', '.join(f.name for f in folders)}"
    )


@contextmanager
def _open(folder: str = "inbox"):
    """Открывает IMAP-соединение и выбирает папку. Возвращает (mailbox, настоящее_имя_папки)."""
    user, password = _credentials()
    try:
        mb = MailBox(IMAP_HOST, IMAP_PORT).login(user, password, initial_folder=None)
    except MailboxLoginError as e:
        raise RuntimeError(
            "Mail.ru не принял логин. Нужен пароль для внешних приложений "
            "(Настройки → Безопасность), а не обычный пароль от ящика."
        ) from e
    with mb:
        name = _resolve_folder(mb, folder)
        mb.folder.set(name)
        yield mb, name


def _charset(*values) -> str:
    """Для поиска по кириллице серверу нужно явно сказать CHARSET UTF-8."""
    return "UTF-8" if any(v and not str(v).isascii() for v in values) else "US-ASCII"


def _criteria(**kw):
    kw = {k: v for k, v in kw.items() if v is not None}
    return AND(**kw) if kw else "ALL"


def _fetch_latest(mb: MailBox, criteria, limit: int, charset="US-ASCII",
                  start: datetime | None = None, end: datetime | None = None) -> list[MailMessage]:
    """Самые новые письма по критерию. start/end — точная фильтрация по времени
    (IMAP умеет фильтровать только по датам, без времени и часового пояса)."""
    uids = sorted(mb.uids(criteria, charset), key=int, reverse=True)
    result: list[MailMessage] = []
    for i in range(0, len(uids), 25):  # качаем пачками, пока не наберём limit
        chunk = uids[i:i + 25]
        msgs = mb.fetch(AND(uid=chunk), mark_seen=False, bulk=True)
        for msg in sorted(msgs, key=lambda m: int(m.uid), reverse=True):
            dt = _local(msg.date)
            if start and (dt is None or dt < start):
                continue
            if end and (dt is None or dt >= end):
                continue
            result.append(msg)
            if len(result) >= limit:
                return result
    return result


def _get_one(mb: MailBox, email_id: str, mark_seen=False) -> MailMessage:
    for msg in mb.fetch(AND(uid=str(email_id)), mark_seen=mark_seen):
        return msg
    raise ValueError(f"Письмо с id={email_id} не найдено в этой папке.")


# ------------------------------------------------------ помощники форматирования

def _local(dt: datetime | None) -> datetime | None:
    if dt is None or dt.year < 1980:  # imap-tools отдаёт 1900-01-01, если дата не распозналась
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def _fmt_date(dt: datetime | None) -> str:
    local = _local(dt)
    return local.strftime("%Y-%m-%d %H:%M") if local else "?"


def _html_to_text(html: str) -> str:
    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0
    return h.handle(html)


def _body(msg: MailMessage) -> str:
    text = msg.text if msg.text and msg.text.strip() else (_html_to_text(msg.html) if msg.html else "")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _addr(values) -> list[str]:
    return [a.full for a in values] if values else []


def _summary(msg: MailMessage, folder: str) -> dict:
    preview = re.sub(r"\s+", " ", _body(msg))[:200]
    return {
        "id": msg.uid,
        "folder": folder,
        "date": _fmt_date(msg.date),
        "from": msg.from_values.full if msg.from_values else msg.from_,
        "to": _addr(msg.to_values),
        "subject": msg.subject or "(без темы)",
        "unread": MailMessageFlags.SEEN not in msg.flags,
        "has_attachments": bool(msg.attachments),
        "preview": preview,
    }


def _period_bounds(period: str | None) -> tuple[datetime | None, datetime | None]:
    if not period:
        return None, None
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    p = period.strip().lower()
    if p == "today":
        return today, None
    if p == "yesterday":
        return today - timedelta(days=1), today
    if p == "week":
        return now - timedelta(days=7), None
    if p == "month":
        return now - timedelta(days=30), None
    try:  # конкретный день: 2026-11-08
        day = datetime.combine(date.fromisoformat(p), datetime.min.time(), TZ)
        return day, day + timedelta(days=1)
    except ValueError:
        raise ValueError("period: today, yesterday, week, month или дата YYYY-MM-DD") from None


def _imap_dates(start, end) -> dict:
    """Границы для IMAP-поиска с запасом в день — точнее отфильтруем уже в Python."""
    return {
        "date_gte": (start - timedelta(days=1)).date() if start else None,
        "date_lt": (end + timedelta(days=1)).date() if end else None,
    }


# ------------------------------------------------------------- помощники SMTP

def _build_message(to, subject, body, cc=None, html=False, attachments=None) -> EmailMessage:
    user, _ = _credentials()
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    if html:
        msg.set_content(_html_to_text(body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for path in attachments or []:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Файл для вложения не найден: {p}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def _send(msg: EmailMessage, bcc=None) -> None:
    user, password = _credentials()
    recipients = [a for h in ("To", "Cc") for a in (msg.get_all(h) or [])]
    recipients = [a.strip() for r in recipients for a in r.split(",") if a.strip()] + list(bcc or [])
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg, from_addr=user, to_addrs=recipients)
    if SAVE_SENT_COPY:
        with _open("sent") as (mb, name):
            mb.append(msg.as_bytes(), name, flag_set=[MailMessageFlags.SEEN])


def _quote(msg: MailMessage, limit=3000) -> str:
    quoted = "\n".join("> " + line for line in _body(msg)[:limit].splitlines())
    who = msg.from_values.full if msg.from_values else msg.from_
    return f"\n\n{_fmt_date(msg.date)}, {who} написал(а):\n{quoted}"


# ====================================================================== ТУЛЫ
# --------------------------------------------------------------- чтение

@tool(READ_ONLY)
def list_emails(limit: int = 10, period: str | None = None, unread_only: bool = False,
                from_addr: str | None = None, folder: str = "inbox") -> list[dict]:
    """Список последних писем (от новых к старым) в кратком виде: id, дата, от кого, тема, начало текста.

    limit: сколько писем вернуть (1-50).
    period: today | yesterday | week | month | YYYY-MM-DD. Пусто — без ограничения по дате.
    unread_only: только непрочитанные.
    from_addr: фильтр по отправителю (адрес или часть имени).
    folder: inbox | sent | drafts | trash | spam | archive | self (письма себе)
            или имя папки из list_folders (можно без префикса INBOX/).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    start, end = _period_bounds(period)
    crit = _criteria(**_imap_dates(start, end), seen=False if unread_only else None, from_=from_addr)
    with _open(folder) as (mb, name):
        msgs = _fetch_latest(mb, crit, limit, _charset(from_addr), start, end)
        return [_summary(m, name) for m in msgs]


@tool(READ_ONLY)
def search_emails(query: str | None = None, from_addr: str | None = None, to_addr: str | None = None,
                  subject: str | None = None, since: str | None = None, before: str | None = None,
                  folder: str = "inbox", limit: int = 10) -> list[dict]:
    """Поиск писем. Все условия объединяются через И.

    query: слово или фраза в теме или тексте письма.
    since / before: даты YYYY-MM-DD (since включительно, before не включительно).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    crit = _criteria(
        text=query, from_=from_addr, to=to_addr, subject=subject,
        date_gte=date.fromisoformat(since) if since else None,
        date_lt=date.fromisoformat(before) if before else None,
    )
    with _open(folder) as (mb, name):
        msgs = _fetch_latest(mb, crit, limit, _charset(query, from_addr, to_addr, subject))
        return [_summary(m, name) for m in msgs]


@tool(READ_ONLY)
def read_email(email_id: str, folder: str = "inbox", max_chars: int = 5000,
               mark_as_read: bool = False) -> dict:
    """Полное содержимое письма: отправитель, получатели, текст, список вложений."""
    with _open(folder) as (mb, name):
        msg = _get_one(mb, email_id, mark_seen=mark_as_read)
        body = _body(msg)
        truncated = len(body) > max_chars
        return {
            **_summary(msg, name),
            "cc": _addr(msg.cc_values),
            "reply_to": _addr(msg.reply_to_values),
            "body": body[:max_chars] + ("\n\n[...текст обрезан...]" if truncated else ""),
            "body_truncated": truncated,
            "attachments": [
                {"filename": a.filename, "content_type": a.content_type, "size_kb": round(a.size / 1024, 1)}
                for a in msg.attachments
            ],
        }


@tool(READ_ONLY)
def list_folders() -> list[dict]:
    """Все папки ящика. Короткие имена inbox/sent/drafts/trash/spam/archive тоже работают в параметре folder."""
    user, password = _credentials()
    with MailBox(IMAP_HOST, IMAP_PORT).login(user, password, initial_folder=None) as mb:
        result = []
        for f in mb.folder.list():
            status = mb.folder.status(f.name, ["MESSAGES", "UNSEEN"]) if "\\Noselect" not in f.flags else {}
            result.append({"name": f.name, "total": status.get("MESSAGES"), "unread": status.get("UNSEEN")})
        return result


@tool(WRITES)
def get_attachment(email_id: str, filename: str, folder: str = "inbox") -> dict:
    """Сохраняет вложение из письма на диск и возвращает путь к файлу."""
    with _open(folder) as (mb, _):
        msg = _get_one(mb, email_id)
        for att in msg.attachments:
            if att.filename == filename:
                DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
                path = DOWNLOAD_DIR / Path(filename).name  # защита от путей вида ../../
                path.write_bytes(att.payload)
                return {"saved_to": str(path), "size_kb": round(att.size / 1024, 1)}
        names = [a.filename for a in msg.attachments]
        raise ValueError(f"Вложение '{filename}' не найдено. Есть: {names}")


@tool(READ_ONLY)
def find_contact(name: str, scan_last: int = 300) -> list[dict]:
    """Ищет адреса по имени или части адреса среди последних писем во «Входящих» и «Отправленных».
    Нужен, чтобы отправить письмо «Ивану», не спрашивая адрес."""
    needle = name.strip().lower()
    counts: Counter = Counter()
    names: dict[str, str] = {}
    for folder in ("inbox", "sent"):
        try:
            with _open(folder) as (mb, _):
                for msg in mb.fetch(limit=min(scan_last, 1000), reverse=True,
                                    headers_only=True, mark_seen=False, bulk=True):
                    for a in (*([msg.from_values] if msg.from_values else []), *msg.to_values, *msg.cc_values):
                        if needle in a.email.lower() or needle in (a.name or "").lower():
                            counts[a.email.lower()] += 1
                            if a.name:
                                names[a.email.lower()] = a.name
        except ValueError:
            continue  # папки нет — пропускаем
    return [{"email": e, "name": names.get(e, ""), "messages": n} for e, n in counts.most_common(10)]


# --------------------------------------------------------------- отправка

@tool(WRITES)
def send_email(to: list[str], subject: str, body: str, cc: list[str] | None = None,
               bcc: list[str] | None = None, html: bool = False,
               attachments: list[str] | None = None) -> str:
    """Отправляет новое письмо.

    to / cc / bcc: списки адресов.
    html: true, если body — HTML.
    attachments: пути к файлам на диске.
    """
    msg = _build_message(to, subject, body, cc, html, attachments)
    _send(msg, bcc)
    return f"Письмо «{subject}» отправлено: {', '.join(to + (cc or []) + (bcc or []))}"


@tool(WRITES)
def reply_email(email_id: str, body: str, reply_all: bool = False, folder: str = "inbox") -> str:
    """Отвечает на письмо в той же цепочке (Re:, цитата исходного письма).
    reply_all: ответить также всем получателям исходного письма."""
    user, _ = _credentials()
    with _open(folder) as (mb, _):
        orig = _get_one(mb, email_id)
    to = [a.email for a in orig.reply_to_values] or ([orig.from_values.email] if orig.from_values else [])
    cc = []
    if reply_all:
        others = [a.email for a in (*orig.to_values, *orig.cc_values)]
        cc = [e for e in dict.fromkeys(others) if e.lower() != user.lower() and e not in to]
    subject = orig.subject or ""
    if not re.match(r"^\s*re:", subject, re.I):
        subject = "Re: " + subject
    msg = _build_message(to, subject, body + _quote(orig), cc)
    orig_id = (orig.headers.get("message-id") or ("",))[0].strip()
    if orig_id:
        refs = (orig.headers.get("references") or ("",))[0].strip()
        msg["In-Reply-To"] = orig_id
        msg["References"] = f"{refs} {orig_id}".strip()
    _send(msg)
    with _open(folder) as (mb, _):
        mb.flag(str(email_id), MailMessageFlags.ANSWERED, True)
    return f"Ответ отправлен: {', '.join(to + cc)}"


@tool(WRITES)
def forward_email(email_id: str, to: list[str], comment: str = "", folder: str = "inbox") -> str:
    """Пересылает письмо вместе с вложениями. comment — текст над пересылаемым письмом."""
    with _open(folder) as (mb, _):
        orig = _get_one(mb, email_id)
    header = (
        "\n\n---------- Пересланное сообщение ----------\n"
        f"От: {orig.from_values.full if orig.from_values else orig.from_}\n"
        f"Дата: {_fmt_date(orig.date)}\n"
        f"Тема: {orig.subject}\n"
        f"Кому: {', '.join(_addr(orig.to_values))}\n\n"
    )
    msg = _build_message(to, "Fwd: " + (orig.subject or ""), comment + header + _body(orig))
    for att in orig.attachments:
        maintype, _, subtype = (att.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(att.payload, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=att.filename)
    _send(msg)
    return f"Письмо «{orig.subject}» переслано: {', '.join(to)}"


@tool(WRITES)
def create_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None,
                 html: bool = False, attachments: list[str] | None = None) -> str:
    """Сохраняет письмо в «Черновики», не отправляя его."""
    msg = _build_message(to, subject, body, cc, html, attachments)
    with _open("drafts") as (mb, name):
        mb.append(msg.as_bytes(), name, flag_set=[MailMessageFlags.DRAFT, MailMessageFlags.SEEN])
    return f"Черновик «{subject}» сохранён в папку «{name}»"


# -------------------------------------------------------------- управление

@tool(WRITES)
def mark_emails(email_ids: list[str], action: str, folder: str = "inbox") -> str:
    """Меняет отметки у писем. action: read | unread | flag | unflag."""
    actions = {
        "read": (MailMessageFlags.SEEN, True), "unread": (MailMessageFlags.SEEN, False),
        "flag": (MailMessageFlags.FLAGGED, True), "unflag": (MailMessageFlags.FLAGGED, False),
    }
    if action not in actions:
        raise ValueError(f"action должен быть одним из: {', '.join(actions)}")
    flag, value = actions[action]
    with _open(folder) as (mb, _):
        mb.flag([str(i) for i in email_ids], flag, value)
    return f"Готово: {action} для {len(email_ids)} писем"


@tool(WRITES)
def move_emails(email_ids: list[str], to_folder: str, folder: str = "inbox") -> str:
    """Перемещает письма в другую папку (например archive или точное имя папки)."""
    with _open(folder) as (mb, _):
        dest = _resolve_folder(mb, to_folder)
        mb.move([str(i) for i in email_ids], dest)
    return f"Перемещено {len(email_ids)} писем в «{dest}»"


@tool(DESTRUCTIVE)
def delete_emails(email_ids: list[str], folder: str = "inbox") -> str:
    """Перемещает письма в «Корзину». Если письма уже в корзине — удаляет их навсегда."""
    ids = [str(i) for i in email_ids]
    with _open(folder) as (mb, name):
        trash = _resolve_folder(mb, "trash")
        if name == trash:
            mb.delete(ids)
            return f"Удалено навсегда: {len(ids)} писем"
        mb.move(ids, trash)
    return f"Перемещено в корзину: {len(ids)} писем"


if __name__ == "__main__":
    mcp.run()  # stdio