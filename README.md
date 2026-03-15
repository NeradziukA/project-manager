# Telegram → Claude Code → VDS (pm2)

## Как работает

```mermaid
sequenceDiagram
    participant U as Ты (Telegram)
    participant B as Bot (FastAPI)
    participant R as Redis
    participant W as Worker
    participant C as Claude Code
    participant G as GitHub (origin)
    participant V as VDS (pm2)

    U->>B: пишешь задачу
    B->>R: claude:pending:{N} + номер задачи
    B->>U: Задача #N принята. /ok_N для подтверждения

    U->>B: /ok_N
    B->>R: rpush claude:tasks:{queue}

    W->>R: blpop claude:tasks (ждёт задачу)
    W->>R: set claude:in_progress (отмечает текущую задачу)
    W->>U: ⚙️ Задача #N принята в работу
    W->>W: git pull
    W->>C: claude --print --dangerously-skip-permissions
    alt Claude задаёт вопрос
        C-->>W: QUESTION: ...
        W->>R: set claude:waiting:{N}
        W->>U: ❓ Задача #N — вопрос: ... /answer_N
        U->>B: /answer_N ответ
        B->>R: rpush claude:tasks (с ответом в промпте)
    else Задача выполнена
        C-->>W: результат
        W->>G: git push origin main
        W->>V: npm run build (client + server)
        W->>V: npx pm2 restart hives
        W->>R: del claude:in_progress
        W->>U: ✅ Задача выполнена
    else Claude завершился с ошибкой
        C-->>W: код выхода != 0 / таймаут
        W->>U: ❌ Причина ошибки + вернул в очередь
        W->>R: rpush claude:tasks (retry+1)
    end

    note over W: При рестарте: stale in_progress → requeue + ⚠️ уведомление в Telegram
```

---

## Быстрая установка

```bash
git clone <этот репо> && cd tg-claude-heroku
bash install.sh
```

Скрипт установит всё автоматически и подскажет что делать дальше.

---

## Ручная установка

### 1. Зависимости

```bash
sudo apt update && sudo apt install -y git curl nginx redis-server python3 python3-venv

# Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Claude Code CLI
sudo npm install -g @anthropic-ai/claude-code
claude auth   # ← авторизация (нужен Anthropic API key)

# Heroku CLI
curl https://cli-assets.heroku.com/install.sh | sh
```

### 2. Клонировать проект и настроить репо

```bash
# Клонировать твой проект
git clone YOUR_REPO_URL /opt/myproject
cd /opt/myproject

# Добавить heroku remote если ещё нет
heroku login
git remote add heroku https://git.heroku.com/YOUR_APP_NAME.git
```

### 3. Настроить бота

```bash
sudo mkdir -p /opt/tg-claude-heroku
cd /opt/tg-claude-heroku
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # ← заполнить все переменные
```

### 4. SSL (нужен домен)

```bash
sudo apt install certbot python3-certbot-nginx
# Сначала поставить домен в nginx.conf, потом:
sudo certbot --nginx -d YOUR_DOMAIN
```

### 5. Запуск

```bash
sudo systemctl start tg-bot tg-worker

# Зарегистрировать webhook (один раз)
python setup_webhook.py
```

---

## Переменные окружения (.env)

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather |
| `ALLOWED_USER_IDS` | Твой Telegram ID (узнать у @userinfobot) |
| `WEBHOOK_URL` | https://твой-домен/webhook |
| `REDIS_URL` | redis://localhost:6379 |
| `REPO_DIR` | Путь к папке с проектом, напр. /opt/myproject |
| `PM2_APP_NAME` | Имя процесса pm2 (по умолч. `hives`) |
| `CLAUDE_TIMEOUT` | Таймаут для Claude Code (сек, по умолч. 300) |

---

## Команды бота

- Любой текст — создаёт задачу в ожидании подтверждения (#N)
- `/ok_N` — подтвердить задачу #N (отправить в работу)
- `/cancel_N` — отменить задачу #N
- `/answer_N текст` — ответить на вопрос Claude по задаче #N
- `/queue` — очередь: ожидающие, задача в работе, статистика
- `/status` — результат последней задачи
- `/restart_hives` — перезапустить сервер hives через pm2 (без сборки)

---

## Что приходит в ответ

**Успех:**
```
✅ Задача выполнена #5 (⏱ 47.2с)

🚀 Деплой: ✅ Деплой успешен

📂 Изменения:
 src/models/user.py  | 12 +++---

📋 Вывод Claude Code:
Я добавил валидацию email...
```

**Ошибка Claude (с автоматическим повтором):**
```
❌ Задача не выполнена — вернул в очередь #5 (⏱ 12.3с • попытка #2)

⚠️ Причина: Код выхода: 1

stderr:
API error: ...

🚀 Деплой: ⏭ Пропущен (Claude Code завершился с ошибкой)
```

**Воркер был перезапущен:**
```
⚠️ Задача #5 — воркер был перезапущен

Задача была прервана и поставлена в очередь повторно (попытка #2).
```

**Необработанная ошибка (задача в FAILED_QUEUE):**
```
💥 Задача #5 — необработанная ошибка

TimeoutError: blpop timeout

Задача перемещена в очередь ошибок.
```

**Превышен лимит попыток (задача в FAILED_QUEUE):**
```
🚫 Задача #5 — превышен лимит попыток

Задача выполнялась 3 раза и каждый раз завершалась ошибкой.
Перемещена в очередь ошибок.
```

---

## Логи

```bash
sudo journalctl -u tg-bot -f      # логи бота
sudo journalctl -u tg-worker -f   # логи воркера
redis-cli llen claude:tasks        # задач в очереди
redis-cli llen claude:failed       # упавших задач
```
