# Домен + HTTPS — пошагово

Сейчас: `http://IP:8080`  
После настройки: `https://ваш-домен.ru` (без `:8080`)

---

## Что понадобится

- Домен (купленный у Reg.ru, Timeweb, Cloudflare и т.д.)
- Доступ к **DNS** домена (личный кабинет регистратора)
- IP вашего VPS (из DockerHosting)
- PuTTY (SSH на сервер)

---

## ЧАСТЬ 1. DNS — привязать домен к серверу

1. Зайдите в панель, где купили домен → **DNS / Управление зоной**
2. Создайте **A-запись**:

| Тип | Имя (Host) | Значение | TTL |
|-----|------------|----------|-----|
| A | `@` | IP вашего VPS | 300–3600 |
| A | `www` | IP вашего VPS | 300–3600 |

Пример: домен `analytics.mysite.ru` → A-запись `analytics` → `185.xxx.xxx.xxx`

3. Подождите **5–30 минут** (иногда до 2 часов)
4. Проверка с компьютера (PowerShell):

```powershell
nslookup analytics.mysite.ru
```

Должен показать **IP вашего VPS**.

---

## ЧАСТЬ 2. Порты на VPS и в панели хостинга

В панели **DockerHosting** откройте:

| Порт | Зачем |
|------|--------|
| **80** | HTTP (нужен для получения сертификата) |
| **443** | HTTPS |

Порт **8080** после настройки можно **закрыть** снаружи (сайт будет через 443).

На сервере (PuTTY), если включён firewall:

```bash
ufw allow 80
ufw allow 443
ufw status
```

---

## ЧАСТЬ 3. Установка nginx и certbot

Подключитесь к серверу (**PuTTY**) и выполните по очереди:

```bash
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx
systemctl enable nginx
systemctl start nginx
```

---

## ЧАСТЬ 4. Конфиг nginx

**Замените `analytics.mysite.ru`** на ваш реальный домен.

```bash
nano /etc/nginx/sites-available/direct-analytics
```

Вставьте (ПКМ):

```nginx
server {
    listen 80;
    server_name analytics.mysite.ru;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }
}
```

Сохранить: `Ctrl+O` → Enter → `Ctrl+X`.

Включить сайт:

```bash
ln -sf /etc/nginx/sites-available/direct-analytics /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

Проверка в браузере: `http://analytics.mysite.ru` — должен открыться ваш сервис (пока без HTTPS).

> Если не открывается — проверьте DNS и что Docker работает:  
> `cd /opt/direct-analytics-bot && docker compose ps`

---

## ЧАСТЬ 5. Сертификат HTTPS (Let's Encrypt, бесплатно)

```bash
certbot --nginx -d analytics.mysite.ru
```

Certbot спросит:

1. **Email** — для напоминаний о продлении (введите свой)
2. **Terms** — согласиться (`Y`)
3. **Redirect HTTP to HTTPS** — выберите **2** (рекомендуется)

Проверка: `https://analytics.mysite.ru` — замок в браузере.

Автопродление:

```bash
certbot renew --dry-run
```

---

## ЧАСТЬ 6. Закрыть прямой доступ по :8080 (рекомендуется)

Чтобы сайт открывался **только** через HTTPS-домен, а не по `http://IP:8080`.

```bash
cd /opt/direct-analytics-bot
nano docker-compose.yml
```

Измените блок `ports`:

```yaml
    ports:
      - "127.0.0.1:8080:8080"
```

Сохранить и перезапустить:

```bash
docker compose up -d
```

В панели DockerHosting можно **закрыть порт 8080** снаружи (80 и 443 оставить открытыми).

---

## ЧАСТЬ 7. Обновление через Git (без изменений)

Как и раньше — на **Windows** в PowerShell:

```powershell
git push
```

Nginx и HTTPS **не затрагиваются** при деплое.

---

## Частые проблемы

| Проблема | Решение |
|----------|---------|
| `nslookup` не показывает IP VPS | Подождите DNS, проверьте A-запись |
| Certbot: «Connection refused» | Открыты порты 80/443, nginx запущен |
| Certbot: «Domain not found» | DNS ещё не обновился |
| 502 Bad Gateway | `docker compose ps` — контейнер должен быть Up |
| Сайт только по IP:8080 | Nginx не настроен или DNS не на домен |

---

## Чеклист

- [ ] A-запись домена → IP VPS  
- [ ] Порты 80 и 443 открыты  
- [ ] nginx + certbot установлены  
- [ ] `http://домен` открывается  
- [ ] `certbot --nginx -d домен` прошёл  
- [ ] `https://домен` с замком  
- [ ] docker-compose: `127.0.0.1:8080:8080`  
- [ ] Порт 8080 закрыт снаружи (опционально)

---

Готовый шаблон конфига в репозитории: `deploy/nginx/direct-analytics.conf.example`
