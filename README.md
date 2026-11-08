# Спектр знаний

Учебный бот для MAX и Сферума. Учитель загружает учебники и создаёт квизы и тесты. Ученики регистрируются, отвечают на вопросы и получают монеты за тесты. Монеты можно потратить на сертификаты в магазине.

Бот: https://max.ru/ideya_fiks_140_bot

Демонстрационная админка: https://peremena.elros.info/admin

Логин: `admin`

Пароль: `admin_ideafix_spectr`

## Что нужно для запуска на Linux

- Git.
- Docker и Docker Compose с поддержкой `--wait`.
- Доступ в интернет для загрузки образа, библиотек и работы API.
- Действующий токен своего бота MAX.
- Для генерации вопросов, проверки тем и OCR — ключ OpenAI API и HTTP/HTTPS-прокси.

## Подготовка

Скачай проект:

```bash
git clone https://github.com/Spectr946/bot.git
cd bot
```

Создай `.env` из примера:

```bash
cp .env.example .env
```

Открой `.env` и заполни:

- `MAX_BOT_TOKEN` — токен бота MAX. Он проверяется при запуске.
- `OPENAI_API_KEY` — ключ OpenAI API.
- `OPENAI_PROXY_URL` — адрес прокси в формате `http://LOGIN:PASSWORD@HOST:PORT`.
- `SESSION_SECRET` — длинная случайная строка для сессий.

Для входа в локальную админку поставь в `.env`:

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin_ideafix_spectr
COOKIE_SECURE=false
```

Готовая база уже находится в `data/study.sqlite3`. Оставь папку `data` в корне проекта рядом с `compose.yaml`.

## Сборка и запуск

Из папки проекта выполни:

```bash
docker compose up -d --build --wait
```

Эта команда собирает образ и запускает контейнер в фоне. Сборку и запуск можно выполнить отдельно:

```bash
docker compose build
docker compose up -d --wait
```

Открой http://127.0.0.1:8080/admin. Логин — `admin`, пароль — `admin_ideafix_spectr`.

Найди своего бота в MAX и отправь `/start`. Для регистрации укажи ФИО и класс.

## Остановка

```bash
docker compose down
```

База, загруженные файлы и результаты сохраняются в локальной папке `data`. Для повторного запуска:

```bash
docker compose up -d --wait
```
