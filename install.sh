#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  install.sh — полная установка системы на чистый Ubuntu VPS
#  Запуск: bash install.sh
# ══════════════════════════════════════════════════════════════════════════════
set -e

GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; NC="\033[0m"
ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${YELLOW}→${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }

PROJECT_DIR="/opt/tg-claude-heroku"
VENV="$PROJECT_DIR/venv"

# ── 1. Системные пакеты ───────────────────────────────────────────────────────
info "Устанавливаю системные пакеты..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl nginx redis-server python3 python3-venv python3-pip
sudo systemctl enable redis-server && sudo systemctl start redis-server
ok "Системные пакеты установлены"

# ── 2. Node.js 20 ─────────────────────────────────────────────────────────────
if ! command -v node &>/dev/null; then
    info "Устанавливаю Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi
ok "Node.js $(node --version)"

# ── 3. Claude Code CLI ────────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    info "Устанавливаю Claude Code CLI..."
    sudo npm install -g @anthropic-ai/claude-code
fi
ok "Claude Code $(claude --version 2>/dev/null || echo 'installed')"

# ── 4. Heroku CLI ─────────────────────────────────────────────────────────────
if ! command -v heroku &>/dev/null; then
    info "Устанавливаю Heroku CLI..."
    curl https://cli-assets.heroku.com/install.sh | sh
fi
ok "Heroku CLI установлен"

# ── 5. Развернуть проект ──────────────────────────────────────────────────────
info "Разворачиваю проект в $PROJECT_DIR..."
sudo mkdir -p "$PROJECT_DIR"
sudo chown "$USER:$USER" "$PROJECT_DIR"
cp -r . "$PROJECT_DIR/"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"
ok "Python окружение готово"

# ── 6. .env ───────────────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  Заполни конфигурацию:  nano $PROJECT_DIR/.env${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
fi

# ── 7. Systemd сервисы ────────────────────────────────────────────────────────
info "Устанавливаю systemd сервисы..."
CURRENT_USER="$USER"

sudo tee /etc/systemd/system/tg-bot.service > /dev/null <<EOF
[Unit]
Description=TG Claude Bot (webhook)
After=network.target redis.service
Requires=redis.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV/bin/uvicorn bot.bot:app --host 127.0.0.1 --port \${PORT:-8000}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/tg-worker.service > /dev/null <<EOF
[Unit]
Description=TG Claude Worker
After=network.target redis.service
Requires=redis.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV/bin/python -m worker.worker
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable tg-bot tg-worker
ok "Systemd сервисы установлены"

# ── 8. Nginx ──────────────────────────────────────────────────────────────────
info "Настраиваю Nginx..."
sudo cp "$PROJECT_DIR/config/nginx.conf" /etc/nginx/sites-available/tg-claude
sudo ln -sf /etc/nginx/sites-available/tg-claude /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
ok "Nginx настроен"

# ── итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Установка завершена!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "Следующие шаги:"
echo ""
echo "  1. Заполни конфиг:"
echo "     nano $PROJECT_DIR/.env"
echo ""
echo "  2. Настрой SSL (нужен домен):"
echo "     sudo apt install certbot python3-certbot-nginx"
echo "     sudo certbot --nginx -d YOUR_DOMAIN"
echo "     # Замени YOUR_DOMAIN в /etc/nginx/sites-available/tg-claude"
echo ""
echo "  3. Склонируй свой проект:"
echo "     git clone YOUR_REPO_URL \$REPO_DIR"
echo "     cd \$REPO_DIR && git remote add heroku https://git.heroku.com/YOUR_APP.git"
echo ""
echo "  4. Авторизуй Claude Code:"
echo "     claude auth"
echo ""
echo "  5. Запусти сервисы:"
echo "     sudo systemctl start tg-bot tg-worker"
echo ""
echo "  6. Зарегистрируй webhook:"
echo "     cd $PROJECT_DIR && source venv/bin/activate"
echo "     python setup_webhook.py"
echo ""
echo "  7. Проверь логи:"
echo "     sudo journalctl -u tg-bot -f"
echo "     sudo journalctl -u tg-worker -f"
