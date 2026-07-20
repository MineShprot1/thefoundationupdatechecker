# Discord Roblox Update Monitor

Бот проверяет Roblox-игру по place id из ссылки Rolimon's:

https://www.rolimons.com/game/6516141723

Он следит за обновлением игры, дочерних places, gamepasses и badges. Канал для уведомлений выбирается командой `!select`, а `!check` показывает последнее время обновления игры и всех places.

## Запуск

1. Установи зависимости:

```powershell
python -m pip install -r requirements.txt
```

2. Создай `.env` рядом с `bot.py` по примеру `.env.example`.

Важно: токен, который был отправлен в чат, лучше перевыпустить в Discord Developer Portal и вставить в `.env` уже новый.

3. Включи у бота `MESSAGE CONTENT INTENT` в Discord Developer Portal.

4. Запусти:

```powershell
python bot.py
```

## Команды

- `!select` - выбрать текущий канал для уведомлений. Доступно только пользователям из `OWNER_USERNAMES` или `OWNER_USER_IDS`.
- `!check` - проверить сейчас и вывести время последнего обновления игры и всех дочерних places. Доступно всем.

## Настройки

- `DISCORD_TOKEN` - токен Discord-бота.
- `ROOT_PLACE_ID` - place id из ссылки Rolimon's. По умолчанию `6516141723`.
- `POLL_SECONDS` - интервал фоновой проверки. Минимум 30 секунд.
- `TIMEZONE` - часовой пояс для вывода времени.
- `OWNER_USERNAMES` - имена, которым разрешен `!select`.
- `OWNER_USER_IDS` - Discord user id владельцев. Надежнее, чем имена.
