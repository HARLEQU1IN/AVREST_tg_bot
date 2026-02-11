# Telegram Bot для расписания УрГЭУ

Telegram-бот для получения ссылок на онлайн-пары в Толке по расписанию УрГЭУ.

## Возможности

- 📅 **Получение ссылок на пары** — автоматически определяет преподавателя по расписанию для конкретного дня и пары
- 🔔 **Напоминания за час до пары** — автоматические уведомления всем подписчикам
- 👑 **Админ-панель** — управление подписчиками и отправка напоминаний вручную
- 📊 **Выбор конкретной пары** — можно получить ссылку на любую пару заранее

## Установка

1. Клонируй репозиторий:
```bash
git clone <url>
cd tgbot
```

2. Создай виртуальное окружение и установи зависимости:
```bash
python3 -m venv venv
source venv/bin/activate  # на Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. Создай файл `.env` с токеном бота:
```
TELEGRAM_BOT_TOKEN=твой_токен_здесь
TELEGRAM_ADMIN_ID=твой_chat_id_здесь
```

Токен можно получить у [@BotFather](https://t.me/BotFather) в Telegram.  
Chat ID можно узнать у [@userinfobot](https://t.me/userinfobot).

## Настройка

В файле `bot.py` можно изменить:
- `DEFAULT_GROUP` — название твоей группы
- `DEFAULT_PREPOD_ID` и `DEFAULT_GROUP_ID` — используются как fallback, если API недоступно

## Запуск

### Локально
```bash
python bot.py
```

### На сервере через systemd

1. Создай файл `/etc/systemd/system/telegram-bot.service`:
```ini
[Unit]
Description=Telegram Bot
After=network.target

[Service]
ExecStart=/root/tgbot/venv/bin/python /root/tgbot/bot.py
WorkingDirectory=/root/tgbot
User=root
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

2. Запусти сервис:
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot.service
sudo systemctl start telegram-bot.service
```

3. Проверь статус:
```bash
sudo systemctl status telegram-bot.service
```

## Использование

- `/start` — показать меню с кнопками
- `/subscribe` — подписаться на напоминания
- `/unsubscribe` — отписаться от напоминаний
- `/send_reminder [номер_пары]` — отправить напоминание вручную (только для админов)
- `/addadmin [chat_id]` — добавить администратора (только для админов)

## Как это работает

Бот парсит расписание с сайта УрГЭУ через API (`https://www.usue.ru/schedule/`), определяет преподавателя для конкретной даты и номера пары, и формирует ссылку на онлайн-пару в Толке через сервис портфолио.

## Лицензия

MIT
