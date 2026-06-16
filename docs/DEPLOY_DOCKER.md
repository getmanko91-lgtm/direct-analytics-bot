# Деплой на VPS/VDS через Docker

Инструкция для Linux-сервера (Ubuntu/Debian) с Docker — в том числе [DockerHosting.ru](https://dockerhosting.ru/).

## Требования к серверу

- **KVM** (не OpenVZ 6 — на нём Docker не работает)
- Ubuntu 22.04+ / Debian 11+ или аналог
- 1 GB RAM минимум, 2 GB комфортнее
- Открытый порт **8080** (или 80/443 через nginx)

## 1. Установка Docker на сервере

Подключитесь по SSH (root или sudo):

```bash
apt-get update
curl -sS https://get.docker.com/ | sh
systemctl enable docker
docker run hello-world
```

Опционально — доступ без root:

```bash
usermod -aG docker YOUR_USER
# перелогиньтесь
```

## 2. Загрузка проекта на сервер

**Вариант A — git (рекомендуется)**

```bash
cd /opt
git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ> direct-analytics-bot
cd direct-analytics-bot
```

**Вариант B — архив с локального ПК**

На Windows (PowerShell):

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot
tar -czf direct-analytics-bot.tar.gz --exclude=.venv --exclude=data --exclude=.git *
scp direct-analytics-bot.tar.gz root@ВАШ_IP:/opt/
```

На сервере:

```bash
mkdir -p /opt/direct-analytics-bot && cd /opt/direct-analytics-bot
tar -xzf ../direct-analytics-bot.tar.gz
```

## 3. Настройка `.env`

```bash
cp .env.example .env
nano .env
```

Обязательно заполните и **смените**:

| Переменная | Значение |
|---|---|
| `YANDEX_DIRECT_TOKEN` | OAuth-токен Директа |
| `TELEGRAM_BOT_TOKEN` | Токен бота |
| `TELEGRAM_CHAT_ID` | Реальный chat_id |
| `SECRET_KEY` | Длинная случайная строка |
| `ADMIN_PASSWORD` | Надёжный пароль |

Для Docker `WEB_HOST` и `WEB_PORT` задаются в `docker-compose.yml` (`0.0.0.0:8080`).

## 4. Запуск

```bash
docker compose up -d --build
docker compose logs -f
```

Проверка: `http://ВАШ_IP:8080`

## 5. Полезные команды

```bash
docker compose ps
docker compose restart
docker compose down
docker compose up -d --build   # после обновления кода
```

База SQLite хранится в `./data/app.db` на хосте — при пересборке контейнера данные сохраняются.

## 10. Автодеплой через Git

См. [GIT_CI_CD.md](GIT_CI_CD.md) — настройка GitHub, SSH-ключей и GitHub Actions.

## 6. Автозапуск

В `docker-compose.yml` уже указано `restart: unless-stopped`. После перезагрузки сервера контейнер поднимется сам, если Docker включён:

```bash
systemctl enable docker
```

## 7. Доступ по домену и HTTPS (опционально)

Для production лучше не светить `:8080` наружу, а поставить **nginx** + **Let's Encrypt**:

```bash
apt install nginx certbot python3-certbot-nginx
```

Пример `/etc/nginx/sites-available/direct-analytics`:

```nginx
server {
    listen 80;
    server_name analytics.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/direct-analytics /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d analytics.example.com
```

В `docker-compose.yml` тогда можно оставить порт только на localhost:

```yaml
ports:
  - "127.0.0.1:8080:8080"
```

## 8. Бэкап

Регулярно копируйте каталог `data/`:

```bash
tar -czf backup-$(date +%F).tar.gz data/
```

## 9. Обновление

```bash
cd /opt/direct-analytics-bot
git pull          # или загрузите новый архив
docker compose up -d --build
```
