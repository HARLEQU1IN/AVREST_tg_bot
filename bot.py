import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse, quote
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, JobQueue

# Часовой пояс Екатеринбурга (где находится УрГЭУ)
EKATERINBURG_TZ = ZoneInfo("Asia/Yekaterinburg")


BASE_URL = "https://www.usue.ru/raspisanie/"
# API расписания возвращает JSON по датам и группе (см. https://www.usue.ru/schedule/)
SCHEDULE_API_URL = "https://www.usue.ru/schedule/"
PORTFOLIO_BASE_URL = "https://portfolio.usue.ru/home/getonlinelink"

# TODO: при необходимости поменяй на свой код группы/препода
DEFAULT_GROUP = "ОЗИВТ(ППК-2)-24-1-у"
# Эти идентификаторы взяты из ссылки на Толк в расписании
DEFAULT_PREPOD_ID = "3186"
DEFAULT_GROUP_ID = "19144"

# Типовая сетка пар УрГЭУ (местное время):
# 1) 08:30–10:00
# 2) 10:10–11:40
# 3) 11:50–13:20
# 4) 13:50–15:20
# 5) 15:30–17:00
# 6) 17:10–18:40
# 7) 18:50–20:20
# 8) 20:30–22:00
PAIR_SCHEDULE = [
    (dt.time(8, 30), dt.time(10, 0)),
    (dt.time(10, 10), dt.time(11, 40)),
    (dt.time(11, 50), dt.time(13, 20)),
    (dt.time(13, 50), dt.time(15, 20)),
    (dt.time(15, 30), dt.time(17, 0)),
    (dt.time(17, 10), dt.time(18, 40)),
    (dt.time(18, 50), dt.time(20, 20)),
    (dt.time(20, 30), dt.time(22, 0)),
]

# Файл для хранения списка участников
SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.json"
# Файл для хранения списка админов
ADMINS_FILE = Path(__file__).parent / "admins.json"
# Множество для отслеживания уже отправленных напоминаний (чтобы не дублировать)
SENT_REMINDERS: Set[str] = set()


def get_local_datetime() -> dt.datetime:
    """Возвращает текущее время в часовом поясе Екатеринбурга."""
    return dt.datetime.now(EKATERINBURG_TZ)


def get_current_par_id(now: Optional[dt.datetime] = None) -> Optional[str]:
    """
    Определяет номер текущей пары по локальному времени Екатеринбурга.

    Возвращает строку "1"..."8" или None, если сейчас не идёт ни одна пара.
    """
    if now is None:
        now = get_local_datetime()
    else:
        # Если передан datetime без timezone, добавляем часовой пояс
        if now.tzinfo is None:
            now = now.replace(tzinfo=EKATERINBURG_TZ)

    current_time = now.time()

    for idx, (start, end) in enumerate(PAIR_SCHEDULE, start=1):
        if start <= current_time <= end:
            return str(idx)

    return None


def get_next_pair_time(now: Optional[dt.datetime] = None) -> Optional[dt.time]:
    """
    Возвращает время начала следующей пары или None, если сегодня больше пар не будет.
    """
    if now is None:
        now = get_local_datetime()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=EKATERINBURG_TZ)
    
    current_time = now.time()
    
    for start, end in PAIR_SCHEDULE:
        if current_time < start:
            return start
    
    return None


def load_subscribers() -> Set[int]:
    """Загружает список chat_id подписчиков из JSON файла."""
    if not SUBSCRIBERS_FILE.exists():
        return set()
    
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("subscribers", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def save_subscribers(subscribers: Set[int]) -> None:
    """Сохраняет список chat_id подписчиков в JSON файл."""
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"subscribers": list(subscribers)}, f, ensure_ascii=False, indent=2)


def add_subscriber(chat_id: int) -> bool:
    """Добавляет chat_id в список подписчиков. Возвращает True, если добавлен новый."""
    subscribers = load_subscribers()
    if chat_id in subscribers:
        return False
    subscribers.add(chat_id)
    save_subscribers(subscribers)
    return True


def remove_subscriber(chat_id: int) -> bool:
    """Удаляет chat_id из списка подписчиков. Возвращает True, если был удалён."""
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        return False
    subscribers.remove(chat_id)
    save_subscribers(subscribers)
    return True


def load_admins() -> Set[int]:
    """Загружает список chat_id админов из JSON файла или переменной окружения."""
    admins = set()
    
    # Сначала проверяем переменную окружения для первого админа
    load_dotenv()
    admin_env = os.getenv("TELEGRAM_ADMIN_ID")
    if admin_env:
        try:
            admins.add(int(admin_env))
        except ValueError:
            pass
    
    # Затем загружаем из файла
    if ADMINS_FILE.exists():
        try:
            with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                admins.update(data.get("admins", []))
        except (json.JSONDecodeError, KeyError):
            pass
    
    return admins


def save_admins(admins: Set[int]) -> None:
    """Сохраняет список chat_id админов в JSON файл."""
    ADMINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump({"admins": list(admins)}, f, ensure_ascii=False, indent=2)


def add_admin(chat_id: int) -> bool:
    """Добавляет chat_id в список админов. Возвращает True, если добавлен новый."""
    admins = load_admins()
    if chat_id in admins:
        return False
    admins.add(chat_id)
    save_admins(admins)
    return True


def is_admin(chat_id: int) -> bool:
    """Проверяет, является ли пользователь админом."""
    return chat_id in load_admins()


def _extract_prepod_group_from_href(href: str) -> Optional[Tuple[str, str]]:
    """Извлекает (prepod_id, group_id) из ссылки на getonlinelink."""
    if not href or "getonlinelink" not in href and "portfolio.usue.ru" not in href:
        return None
    try:
        if href.startswith("/"):
            href = "https://portfolio.usue.ru" + href
        elif not href.startswith("http"):
            return None
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        prepod_id = qs.get("prepod_id", [None])[0]
        group_id = qs.get("group_id", [None])[0]
        if prepod_id and group_id:
            return (str(prepod_id), str(group_id))
    except Exception:
        pass
    return None


# Заголовки для запроса к API расписания (сайт может отдавать JSON только при запросе "как браузер")
SCHEDULE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.usue.ru/raspisanie/",
}


async def fetch_schedule_json(date: dt.date, group: str = DEFAULT_GROUP) -> Optional[list]:
    """
    Загружает расписание из API УрГЭУ (JSON).
    Параметры: action=show, startDate и endDate — понедельник и воскресенье недели, group — URL-encoded.
    """
    weekday = date.weekday()  # 0=Пн, 6=Вс
    start = date - dt.timedelta(days=weekday)
    end = start + dt.timedelta(days=6)
    start_str = start.strftime("%d.%m.%Y")
    end_str = end.strftime("%d.%m.%Y")
    # Параметр group передаём как есть — httpx закодирует; при ручном quote() сервер иногда отдаёт пустые schedulePairs
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        try:
            resp = await client.get(
                SCHEDULE_API_URL,
                params={
                    "action": "show",
                    "startDate": start_str,
                    "endDate": end_str,
                    "group": group,
                },
                headers=SCHEDULE_HEADERS,
            )
            if resp.status_code != 200:
                return None
            # Ответ может быть JSON-массивом напрямую
            raw = resp.text
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            # На случай обёртки вида {"data": [...]}
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                return data["data"]
        except (httpx.RequestError, json.JSONDecodeError, TypeError):
            pass
    return None


async def fetch_schedule_html(date: dt.date, group: str = DEFAULT_GROUP) -> Optional[str]:
    """Загружает HTML страницы расписания (fallback, если API недоступен)."""
    date_str = date.strftime("%d.%m.%Y")
    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        try:
            resp = await client.get(BASE_URL, params={"date": date_str, "group": group})
            if resp.status_code == 200:
                return resp.text
            resp = await client.get(BASE_URL)
            if resp.status_code == 200:
                return resp.text
        except httpx.RequestError:
            pass
    return None


def parse_tolk_from_schedule_json(
    data: list, target_date: dt.date, par_id: int
) -> Optional[Tuple[str, str]]:
    """
    Из JSON расписания возвращает (prepod_id, group_id) для пары в указанную дату.
    Структура: список дней, у каждого date, pairs; у каждой пары N (1–8), schedulePairs с prepod_id, group_id.
    """
    if not data or par_id < 1 or par_id > 8:
        return None
    target_date_str = target_date.strftime("%d.%m.%Y")
    for day in data:
        day_date = day.get("date")
        if day_date != target_date_str:
            continue
        pairs = day.get("pairs") or []
        for p in pairs:
            try:
                n = int(p.get("N") or 0)
            except (TypeError, ValueError):
                continue
            if n != par_id:
                continue
            schedule_pairs = p.get("schedulePairs") or []
            for sp in schedule_pairs:
                pid = sp.get("prepod_id")
                gid = sp.get("group_id")
                if pid is not None and gid is not None:
                    return (str(int(pid)), str(int(gid)))
        return None
    return None


def parse_tolk_link_from_schedule(
    html: str, target_date: dt.date, par_id: int
) -> Optional[Tuple[str, str]]:
    """
    Парсит HTML расписания и возвращает (prepod_id, group_id) для пары в указанную дату.
    Ищет ссылки на portfolio.usue.ru/home/getonlinelink в ячейках таблицы или блоках по дням.
    """
    if not html or par_id < 1 or par_id > 8:
        return None
    soup = BeautifulSoup(html, "html.parser")
    target_weekday = target_date.weekday()  # 0=Пн, 3=Чт
    target_date_str = target_date.strftime("%d.%m.%Y")
    target_date_short = target_date.strftime("%d.%m")  # на случай без года

    # Собираем все ссылки на Толк
    all_links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "getonlinelink" not in href and "portfolio.usue.ru" not in href:
            continue
        ids = _extract_prepod_group_from_href(href)
        if not ids:
            continue
        all_links.append((a, ids, href))

    if not all_links:
        return None

    # Вариант 1: ищем блоки по дате (заголовок или data-date с датой)
    for block in soup.find_all(["div", "section", "tbody", "tr"]):
        block_text = block.get_text() or ""
        if target_date_str not in block_text and target_date_short not in block_text:
            # проверяем data-date и т.п.
            date_attr = (
                block.get("data-date")
                or block.get("data-day")
                or (block.get("data-date-day") and block.get("data-date-month"))
            )
            if not date_attr:
                continue
            if target_date_str not in str(date_attr) and target_date_short not in str(date_attr):
                continue
        links_in_block = []
        for a in block.find_all("a", href=True):
            href = a.get("href", "")
            if "getonlinelink" not in href and "portfolio.usue.ru" not in href:
                continue
            ids = _extract_prepod_group_from_href(href)
            if ids:
                links_in_block.append(ids)
        if len(links_in_block) >= par_id:
            return links_in_block[par_id - 1]

    # Вариант 2: таблица — строки = пары (1–8), столбцы = дни недели
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < par_id:
            continue
        # первая строка может быть заголовком
        row = rows[par_id - 1] if par_id <= len(rows) else rows[par_id]
        cells = row.find_all(["td", "th"])
        if target_weekday < len(cells):
            cell = cells[target_weekday]
            for a in cell.find_all("a", href=True):
                ids = _extract_prepod_group_from_href(a.get("href", ""))
                if ids:
                    return ids
        # если столбцы = пары, а строки = дни
        if target_weekday < len(rows):
            row = rows[target_weekday]
            cells = row.find_all(["td", "th"])
            if par_id <= len(cells):
                cell = cells[par_id - 1]
                for a in cell.find_all("a", href=True):
                    ids = _extract_prepod_group_from_href(a.get("href", ""))
                    if ids:
                        return ids

    # Вариант 3: все ссылки подряд — порядок по дням затем по парам (пн 1–8, вт 1–8, …)
    if len(all_links) >= 8:
        idx = target_weekday * 8 + (par_id - 1)
        if idx < len(all_links):
            return all_links[idx][1]
    # Вариант 4: порядок по парам затем по дням (пара1 пн–вс, пара2 пн–вс, …)
    if len(all_links) >= 8:
        idx = (par_id - 1) * 7 + min(target_weekday, 6)
        if idx < len(all_links):
            return all_links[idx][1]

    return None


async def get_today_tolk_link(
    group: str = DEFAULT_GROUP,
    prepod_id: Optional[str] = None,
    group_id: Optional[str] = None,
    par_id: Optional[str] = None,
) -> Optional[str]:
    """
    Формирует ссылку на онлайн‑пару в Толке по расписанию на указанный день и пару.

    Сначала парсит страницу расписания УрГЭУ для данной даты и группы, находит
    преподавателя и группу для нужной пары и подставляет их в ссылку getonlinelink.
    Если парсинг не удался — используется дефолтный prepod_id/group_id.
    """
    now_local = get_local_datetime()
    today = now_local.date()

    if par_id is None:
        par_id = get_current_par_id(now_local)
        if par_id is None:
            return None

    par_num = int(par_id)
    if par_num < 1 or par_num > 8:
        return None

    # Берём prepod_id и group_id только из расписания — если на эту пару нет занятия, ссылку не даём
    if prepod_id is None or group_id is None:
        schedule_data = await fetch_schedule_json(today, group)
        if schedule_data:
            parsed = parse_tolk_from_schedule_json(schedule_data, today, par_num)
            if parsed:
                prepod_id, group_id = parsed
        if prepod_id is None or group_id is None:
            html = await fetch_schedule_html(today, group)
            if html:
                parsed = parse_tolk_link_from_schedule(html, today, par_num)
                if parsed:
                    prepod_id, group_id = parsed
        # Не используем DEFAULT_PREPOD_ID — ссылку отдаём только когда в расписании есть занятие с Толком
        if prepod_id is None or group_id is None:
            return None

    date_str = today.strftime("%d.%m.%Y")
    url = (
        f"{PORTFOLIO_BASE_URL}"
        f"?prepod_id={prepod_id}&group_id={group_id}"
        f"&par_id={par_id}&date={date_str}"
    )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            resp = await client.get(url, timeout=15.0)
        except httpx.RequestError:
            return None
        if resp.status_code == 200:
            return url
    return None


def get_main_keyboard(is_admin_user: bool = False, show_pair_selection: bool = False) -> ReplyKeyboardMarkup:
    """Создаёт основную клавиатуру с всеми элементами управления."""
    if show_pair_selection:
        # Клавиатура для выбора пары
        keyboard = [
            [KeyboardButton("1-я пара (08:30)"), KeyboardButton("2-я пара (10:10)")],
            [KeyboardButton("3-я пара (11:50)"), KeyboardButton("4-я пара (13:50)")],
            [KeyboardButton("5-я пара (15:30)"), KeyboardButton("6-я пара (17:10)")],
            [KeyboardButton("7-я пара (18:50)"), KeyboardButton("8-я пара (20:30)")],
            [KeyboardButton("◀️ Назад")]
        ]
    else:
        # Основная клавиатура
        keyboard = [
            [KeyboardButton("Получить ссылку"), KeyboardButton("📅 Выбрать пару")],
            [KeyboardButton("🔔 Подписаться на напоминания"), KeyboardButton("❌ Отписаться от напоминаний")],
            [KeyboardButton("📊 Статус подписки")]
        ]
        
        # Добавляем админские кнопки, если пользователь админ
        if is_admin_user:
            keyboard.append([KeyboardButton("👑 Админ-панель")])
            keyboard.append([
                KeyboardButton("📢 Отправить напоминание сейчас"),
                KeyboardButton("👥 Список подписчиков")
            ])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    is_admin_user = is_admin(chat_id)
    reply_markup = get_main_keyboard(is_admin_user)
    
    message = "Привет! Используй кнопки ниже для управления ботом:\n\n"
    message += "• Получить ссылку — получить ссылку на текущую онлайн-пару\n"
    message += "• Выбрать пару — получить ссылку на конкретную пару заранее\n"
    message += "• Подписаться на напоминания — получать уведомления за час до пары\n"
    message += "• Отписаться от напоминаний — отменить подписку\n"
    message += "• Статус подписки — проверить, подписан ли ты"
    
    if is_admin_user:
        message += "\n\n👑 Ты администратор! Доступны дополнительные функции."
    
    await update.message.reply_text(message, reply_markup=reply_markup)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для подписки на напоминания за час до пары."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    is_admin_user = is_admin(chat_id)
    reply_markup = get_main_keyboard(is_admin_user)
    if add_subscriber(chat_id):
        await update.message.reply_text(
            "✅ Ты подписан на напоминания! Я буду пинговать тебя за час до каждой пары.",
            reply_markup=reply_markup,
        )
    else:
        await update.message.reply_text(
            "ℹ️ Ты уже подписан на напоминания.",
            reply_markup=reply_markup,
        )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для отписки от напоминаний."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    is_admin_user = is_admin(chat_id)
    reply_markup = get_main_keyboard(is_admin_user)
    if remove_subscriber(chat_id):
        await update.message.reply_text(
            "❌ Ты отписан от напоминаний.",
            reply_markup=reply_markup,
        )
    else:
        await update.message.reply_text(
            "ℹ️ Ты не был подписан на напоминания.",
            reply_markup=reply_markup,
        )


async def check_subscription_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет и показывает статус подписки пользователя."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    subscribers = load_subscribers()
    is_admin_user = is_admin(chat_id)
    reply_markup = get_main_keyboard(is_admin_user)
    
    if chat_id in subscribers:
        await update.message.reply_text(
            "✅ Ты подписан на напоминания.\n\n"
            "Я буду отправлять тебе уведомления за час до начала каждой пары.",
            reply_markup=reply_markup,
        )
    else:
        await update.message.reply_text(
            "❌ Ты не подписан на напоминания.\n\n"
            "Нажми кнопку 'Подписаться на напоминания', чтобы получать уведомления за час до пары.",
            reply_markup=reply_markup,
        )


async def send_manual_reminder(context: ContextTypes.DEFAULT_TYPE, par_id: Optional[int] = None) -> str:
    """
    Отправляет напоминание всем подписчикам вручную.
    Если par_id не указан, определяет следующую пару автоматически.
    """
    now = get_local_datetime()
    
    # Если номер пары не указан, определяем следующую
    if par_id is None:
        current_time = now.time()
        for idx, (start_time, end_time) in enumerate(PAIR_SCHEDULE, start=1):
            if current_time < start_time:
                par_id = idx
                break
        if par_id is None:
            return "❌ Сегодня больше пар не будет."
    
    if par_id < 1 or par_id > len(PAIR_SCHEDULE):
        return f"❌ Неверный номер пары. Должно быть от 1 до {len(PAIR_SCHEDULE)}."
    
    start_time, _ = PAIR_SCHEDULE[par_id - 1]
    
    # Получаем ссылку на пару
    link = await get_today_tolk_link(par_id=str(par_id))
    
    # Формируем сообщение
    message = f"🔔 Напоминание: через час начинается {par_id}-я пара ({start_time.strftime('%H:%M')})"
    if link:
        message += f"\n\nСсылка на онлайн-пару:\n{link}"
    
    # Отправляем всем подписчикам
    subscribers = load_subscribers()
    sent_count = 0
    failed_count = 0
    
    for chat_id in subscribers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            print(f"Не удалось отправить напоминание пользователю {chat_id}: {e}")
    
    result = f"✅ Напоминание отправлено {par_id}-й пары ({start_time.strftime('%H:%M')})\n"
    result += f"📊 Отправлено: {sent_count} пользователям"
    if failed_count > 0:
        result += f"\n❌ Ошибок: {failed_count}"
    
    return result


async def admin_send_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админская функция для отправки напоминания вручную."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ У тебя нет прав администратора.")
        return
    
    is_admin_user = True
    reply_markup = get_main_keyboard(is_admin_user)
    
    # Пытаемся определить номер пары из аргументов команды
    par_id = None
    if context.args and len(context.args) > 0:
        try:
            par_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Используй: /send_reminder [номер_пары]",
                reply_markup=reply_markup,
            )
            return
    
    result = await send_manual_reminder(context, par_id)
    await update.message.reply_text(result, reply_markup=reply_markup)


async def admin_show_subscribers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список подписчиков админу."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ У тебя нет прав администратора.")
        return
    
    subscribers = load_subscribers()
    is_admin_user = True
    reply_markup = get_main_keyboard(is_admin_user)
    
    if not subscribers:
        await update.message.reply_text(
            "📊 Подписчиков пока нет.",
            reply_markup=reply_markup,
        )
        return
    
    message = f"👥 Список подписчиков ({len(subscribers)}):\n\n"
    for idx, sub_id in enumerate(sorted(subscribers), 1):
        message += f"{idx}. `{sub_id}`\n"
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Добавляет нового админа (только для существующих админов)."""
    if not update.message:
        return
    
    chat_id = update.message.chat_id
    if not is_admin(chat_id):
        await update.message.reply_text("❌ У тебя нет прав администратора.")
        return
    
    is_admin_user = True
    reply_markup = get_main_keyboard(is_admin_user)
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "❌ Укажи chat_id пользователя. Пример: /addadmin 123456789",
            reply_markup=reply_markup,
        )
        return
    
    try:
        new_admin_id = int(context.args[0])
        if add_admin(new_admin_id):
            await update.message.reply_text(
                f"✅ Пользователь {new_admin_id} добавлен как администратор.",
                reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text(
                f"ℹ️ Пользователь {new_admin_id} уже является администратором.",
                reply_markup=reply_markup,
            )
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат chat_id. Должно быть число.",
            reply_markup=reply_markup,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения и нажатия на кнопки."""
    if not update.message or not update.message.text:
        return
    
    text = update.message.text
    chat_id = update.message.chat_id
    is_admin_user = is_admin(chat_id)
    reply_markup = get_main_keyboard(is_admin_user)
    
    if text == "Получить ссылку":
        group = DEFAULT_GROUP
        link = await get_today_tolk_link(group)

        if link:
            await update.message.reply_text(
                f"Ссылка на сегодняшнюю онлайн‑пару в Толке:\n{link}",
                reply_markup=reply_markup,
            )
        else:
            await update.message.reply_text(
                "Сейчас у вашей группы нет пары в расписании или ссылка на Толк не указана.",
                reply_markup=reply_markup,
            )
    
    elif text == "📅 Выбрать пару" or text == "Выбрать пару":
        # Показываем клавиатуру для выбора пары
        pair_keyboard = get_main_keyboard(is_admin_user, show_pair_selection=True)
        await update.message.reply_text(
            "Выбери пару, для которой хочешь получить ссылку:",
            reply_markup=pair_keyboard,
        )
    
    elif text == "◀️ Назад":
        # Возвращаемся к основной клавиатуре
        main_keyboard = get_main_keyboard(is_admin_user)
        await update.message.reply_text(
            "Главное меню",
            reply_markup=main_keyboard,
        )
    
    elif text.startswith("1-я пара") or text.startswith("2-я пара") or text.startswith("3-я пара") or \
         text.startswith("4-я пара") or text.startswith("5-я пара") or text.startswith("6-я пара") or \
         text.startswith("7-я пара") or text.startswith("8-я пара"):
        # Извлекаем номер пары из текста
        par_num = None
        if "1-я" in text:
            par_num = "1"
        elif "2-я" in text:
            par_num = "2"
        elif "3-я" in text:
            par_num = "3"
        elif "4-я" in text:
            par_num = "4"
        elif "5-я" in text:
            par_num = "5"
        elif "6-я" in text:
            par_num = "6"
        elif "7-я" in text:
            par_num = "7"
        elif "8-я" in text:
            par_num = "8"
        
        if par_num:
            link = await get_today_tolk_link(par_id=par_num)
            start_time, _ = PAIR_SCHEDULE[int(par_num) - 1]
            
            # Возвращаемся к основной клавиатуре после выбора пары
            main_keyboard = get_main_keyboard(is_admin_user)
            
            if link:
                await update.message.reply_text(
                    f"✅ Ссылка на {par_num}-ю пару ({start_time.strftime('%H:%M')}):\n\n{link}",
                    reply_markup=main_keyboard,
                )
            else:
                await update.message.reply_text(
                    f"❌ В это время ({start_time.strftime('%H:%M')}) у вашей группы нет пары в расписании — ссылка на Толк не выдаётся.",
                    reply_markup=main_keyboard,
                )
        else:
            await update.message.reply_text(
                "Не удалось определить номер пары. Попробуй ещё раз.",
                reply_markup=reply_markup,
            )
    
    elif text == "🔔 Подписаться на напоминания" or text == "Подписаться на напоминания":
        await subscribe(update, context)
    
    elif text == "❌ Отписаться от напоминаний" or text == "Отписаться от напоминаний":
        await unsubscribe(update, context)
    
    elif text == "📊 Статус подписки" or text == "Статус подписки":
        await check_subscription_status(update, context)
    
    # Админские кнопки
    elif is_admin_user and (text == "📢 Отправить напоминание сейчас" or text == "Отправить напоминание сейчас"):
        result = await send_manual_reminder(context)
        await update.message.reply_text(result, reply_markup=reply_markup)
    
    elif is_admin_user and (text == "👥 Список подписчиков" or text == "Список подписчиков"):
        await admin_show_subscribers(update, context)
    
    elif is_admin_user and text == "👑 Админ-панель":
        message = "👑 Админ-панель\n\n"
        message += "Доступные функции:\n"
        message += "• Отправить напоминание сейчас — отправить напоминание всем подписчикам\n"
        message += "• Список подписчиков — показать всех подписчиков\n\n"
        message += "Команды:\n"
        message += "/send_reminder [номер_пары] — отправить напоминание для конкретной пары\n"
        message += "/addadmin [chat_id] — добавить нового админа"
        await update.message.reply_text(message, reply_markup=reply_markup)
    
    else:
        # Если неизвестная команда, показываем подсказку
        help_text = "Используй кнопки ниже для управления ботом или команды:\n"
        help_text += "/start — показать меню\n"
        help_text += "/subscribe — подписаться на напоминания\n"
        help_text += "/unsubscribe — отписаться от напоминаний"
        if is_admin_user:
            help_text += "\n\n👑 Админские команды:\n"
            help_text += "/send_reminder [номер_пары] — отправить напоминание\n"
            help_text += "/addadmin [chat_id] — добавить админа"
        await update.message.reply_text(help_text, reply_markup=reply_markup)


async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Проверяет, нужно ли отправить напоминания за час до пары.
    Вызывается периодически через JobQueue.
    """
    now = get_local_datetime()
    current_time = now.time()
    
    # Проверяем каждую пару: если до начала остался ровно час (с точностью до минуты)
    for idx, (start_time, end_time) in enumerate(PAIR_SCHEDULE, start=1):
        # Вычисляем время за час до начала пары
        reminder_time = dt.datetime.combine(now.date(), start_time) - dt.timedelta(hours=1)
        reminder_time_only = reminder_time.time()
        
        # Проверяем, что текущее время совпадает с временем напоминания (с точностью до минуты)
        if (reminder_time_only.hour == current_time.hour and 
            reminder_time_only.minute == current_time.minute):
            
            # Создаём уникальный ключ для этого напоминания (дата + номер пары)
            reminder_key = f"{now.date()}_{idx}"
            
            # Если уже отправили это напоминание сегодня, пропускаем
            if reminder_key in SENT_REMINDERS:
                continue
            
            # Получаем ссылку на пару (если есть) - используем номер будущей пары
            link = await get_today_tolk_link(par_id=str(idx))
            
            # Формируем сообщение
            message = f"🔔 Напоминание: через час начинается {idx}-я пара ({start_time.strftime('%H:%M')})"
            if link:
                message += f"\n\nСсылка на онлайн-пару:\n{link}"
            
            # Отправляем напоминание всем подписчикам
            subscribers = load_subscribers()
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    # Если не удалось отправить (пользователь заблокировал бота и т.д.)
                    # Просто пропускаем этого пользователя
                    print(f"Не удалось отправить напоминание пользователю {chat_id}: {e}")
            
            # Помечаем, что напоминание отправлено
            SENT_REMINDERS.add(reminder_key)
    
    # Очищаем старые напоминания (не сегодняшние)
    today = now.date()
    keys_to_remove = [key for key in SENT_REMINDERS if not key.startswith(str(today))]
    for key in keys_to_remove:
        SENT_REMINDERS.discard(key)


def load_token() -> str:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Создай файл .env рядом с bot.py и добавь строку TELEGRAM_BOT_TOKEN=... "
            "или задай переменную окружения."
        )
    return token


def main() -> None:
    token = load_token()
    application = Application.builder().token(token).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    
    # Админские команды
    application.add_handler(CommandHandler("send_reminder", admin_send_reminder))
    application.add_handler(CommandHandler("addadmin", admin_add_admin))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Настраиваем JobQueue для периодической проверки напоминаний
    # Проверяем каждую минуту, нужно ли отправить напоминание
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(
            check_and_send_reminders,
            interval=60,  # Проверяем каждую минуту
            first=10,  # Первая проверка через 10 секунд после запуска
        )

    # Запускаем бота
    # Для Python 3.14+ нужно явно создать event loop
    import sys
    if sys.version_info >= (3, 14):
        import asyncio
        # Создаём новый event loop для Python 3.14+
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            application.run_polling(drop_pending_updates=True)
        finally:
            loop.close()
    else:
        application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

