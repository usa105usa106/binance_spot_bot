# Binance Spot Telegram Trading Bot — Railway single-file version

Версия собрана в один файл `bot.py`, чтобы Railway больше не искал локальный Python-пакет `bot/` и не выдавал ошибку `ModuleNotFoundError: No module named 'bot'`.

## Что делает бот

- Анализирует Binance Spot по названию монеты: `btc`, `eth`, `sol` и т.д.
- Берет актуальную цену, стакан ордеров Spot, свечи OHLCV по выбранному таймфрейму.
- Строит крупный читаемый PNG-график 1920×1080.
- На графике отображает:
  - свечи;
  - объемы;
  - уровни Fibonacci по выбранному таймфрейму;
  - поддержку и сопротивление по стакану;
  - наклонную линию по повышающимся минимумам или понижающимся максимумам;
  - LONG/SHORT проценты;
  - прогноз направления;
  - цели движения;
  - уровень отмены сценария.
- Мониторит стакан каждые 30 минут, если включен режим стакана.
- Хранит настройки и список монет в SQLite.

## Переменные Railway

Обязательно:

```env
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН_ОТ_BOTFATHER
```

Опционально:

```env
DEFAULT_QUOTE=USDT
ORDERBOOK_LIMIT=1000
MONITOR_INTERVAL_MINUTES=30
STRONG_CHANGE_THRESHOLD=35
BOT_VERSION=00002
BINANCE_BASE_URL=https://api.binance.com
BINANCE_BASE_URLS=https://api1.binance.com,https://api2.binance.com,https://api3.binance.com,https://api4.binance.com,https://data-api.binance.vision
```

## Запуск на Railway

В корне проекта должны быть файлы:

```text
bot.py
requirements.txt
Procfile
railway.json
Dockerfile
.env.example
README.md
```

`Procfile`:

```text
worker: python bot.py
```

`Dockerfile` запускает:

```text
python bot.py
```

## Команды

- `/start` — старт и главное меню.
- `/help` — список команд.
- `btc` — анализ BTCUSDT.
- `eth` — анализ ETHUSDT.
- `new btc` — добавить BTCUSDT в мониторинг стакана.
- `del btc` — удалить BTCUSDT из мониторинга.
- `list` — список монет в памяти.
- `stakan on` — включить мониторинг стакана.
- `stakan off` — выключить мониторинг стакана.

## Кнопки в боте

Главное меню:

- `📚 Стакан ордеров`
- `⚙️ Настройки`
- `🖼 Визуализация`
- `🏓 Пинг`

Настройки таймфреймов:

- `15m`
- `1h`
- `4h`
- `1d`
- `1w`
- `⬅️ Назад`

Визуализация:

- `Одно сообщение: картинка + анализ`
- `Два сообщения: график сверху + текст`
- `⬅️ Назад`

## Важно

Бот использует публичные Binance Spot endpoints. Binance API-ключи не нужны.

Аналитика не является финансовой рекомендацией.


## v5 Railway fallback fix

- Добавлен `main.py` как fallback-entrypoint, если Railway продолжает запускать старую команду `python main.py`.
- Основной запуск остается через `bot.py`.
- В `bot.py` добавлен `import logging`.
- В `railway.json` добавлен `startCommand`: `python bot.py`.

Если кнопки не реагируют, сначала проверь Deploy Logs: контейнер должен быть запущен без ошибки `can't open file /app/main.py`.


### Binance 451/403
Бот автоматически перебирает резервные endpoints Binance (`api1`-`api4` и `data-api.binance.vision`) и больше не отправляет пользователю сырые ошибки с URL/JSON-превью. При необходимости укажите свои endpoints в `BINANCE_BASE_URLS` через запятую.
