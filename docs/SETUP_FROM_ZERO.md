# Полная настройка с нуля: Git + VPS + автодеплой

Инструкция для тех, кто никогда не деплоил. Делайте **строго по порядку**.

---

## Что получится в итоге

1. Код лежит на **GitHub** (облачное хранилище проекта).
2. Сервис работает на **VPS** (DockerHosting) круглосуточно.
3. Вы меняете код на ПК → `git push` → сервер **сам обновляется** за 1–2 минуты.

---

# БЛОК A. Подготовка на компьютере (Windows)

## A1. Установите Git

1. Откройте https://git-scm.com/download/win  
2. Скачайте и установите (везде **Next**, ничего не меняйте).  
3. Проверка — откройте **PowerShell** (`Win + X` → Windows PowerShell):

```powershell
git --version
```

Должно показать версию, например `git version 2.x`.

---

## A2. Зарегистрируйтесь на GitHub

1. https://github.com/signup  
2. Подтвердите email.  
3. Запомните **логин** — дальше он будет вместо `ВАШ_ЛОГИН`.

---

## A3. Создайте репозиторий на GitHub

1. https://github.com/new  
2. **Repository name:** `direct-analytics-bot`  
3. **Private** — включите (рекомендуется).  
4. **НЕ** ставьте галочки «Add README», «Add .gitignore», «Choose a license».  
5. **Create repository**.  
6. Страница покажет команды — **пока не закрывайте**, пригодится позже.

---

## A4. Залейте проект на GitHub (первый раз)

Откройте PowerShell:

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/ВАШ_ЛОГИН/direct-analytics-bot.git
git push -u origin main
```

**Замените `ВАШ_ЛОГИН`** на свой логин GitHub.

### Если просит логин и пароль

GitHub **не принимает обычный пароль**. Нужен **Personal Access Token**:

1. GitHub → аватар → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**  
2. **Generate new token (classic)**  
3. Note: `direct-analytics-bot`  
4. Срок: 90 days или No expiration  
5. Галочка **repo**  
6. **Generate token**  
7. **Скопируйте токен** (больше не покажут!)  
8. При `git push`:
   - Username: ваш логин GitHub  
   - Password: **вставьте токен** (не пароль от сайта)

После успешного push на GitHub в репозитории появятся файлы проекта.

> **Важно:** файл `.env` с секретами в Git **не попадает**. Это правильно.

---

# БЛОК B. Сервер VPS (DockerHosting)

## B1. Данные от хостинга

В личном кабинете DockerHosting найдите и запишите:

- **IP-адрес** (например `185.123.45.67`)
- **Логин** (обычно `root`)
- **Пароль**

---

## B2. Подключитесь к серверу

### Установите PuTTY

https://www.putty.org/ → скачать → установить.

### Подключение

1. **Host Name:** IP сервера  
2. **Port:** 22  
3. **Open**  
4. Login: `root`  
5. Password: пароль от VPS (при вводе символы не видны — это нормально)

Успех: строка вида `root@...:~#`

---

## B3. Проверьте Docker

В окне PuTTY по очереди:

```bash
docker --version
docker compose version
```

Если обе команды показывают версии — Docker уже есть (вы брали VPS + Docker).

Если «command not found»:

```bash
curl -sS https://get.docker.com/ | sh
systemctl enable docker
```

---

# БЛОК C. Сервер ↔ GitHub (Deploy Key)

Чтобы сервер мог **скачивать код** из вашего приватного репозитория.

## C1. Создайте ключ на сервере

В PuTTY:

```bash
ssh-keygen -t ed25519 -C "vps-deploy" -f ~/.ssh/github_deploy -N ""
```

(Просто Enter, если спросит что-то ещё.)

## C2. Покажите публичный ключ

```bash
cat ~/.ssh/github_deploy.pub
```

Скопируйте **всю строку** (`ssh-ed25519 AAAA... vps-deploy`).

## C3. Добавьте ключ в GitHub

1. Откройте репозиторий на GitHub  
2. **Settings** → слева **Deploy keys** → **Add deploy key**  
3. **Title:** `vps-dockerhosting`  
4. **Key:** вставьте скопированную строку  
5. **Allow write access** — **НЕ включайте**  
6. **Add key**

## C4. Настройте GitHub на сервере

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
```

## C5. Проверка

```bash
ssh -T git@github.com
```

Ожидаемый ответ (с вашим логином):

```
Hi ВАШ_ЛОГИН! You've successfully authenticated...
```

---

# БЛОК D. Первый запуск на сервере

## D1. Склонируйте репозиторий

```bash
cd /opt
git clone git@github.com:ВАШ_ЛОГИН/direct-analytics-bot.git
cd direct-analytics-bot
chmod +x scripts/deploy.sh
```

## D2. Создайте файл настроек `.env`

**Способ 1 — скопировать с компьютера (если уже работало локально):**

- Установите **WinSCP** (https://winscp.net/)  
- Подключитесь: SFTP, IP, root, пароль  
- Слева: `C:\Users\Frostbitten\direct-analytics-bot\.env`  
- Справа: `/opt/direct-analytics-bot/`  
- Перетащите файл `.env`  
- Если есть клиенты локально — также перетащите папку `data`

**Способ 2 — создать на сервере:**

```bash
cp .env.example .env
nano .env
```

Заполните (значения возьмите из рабочего `.env` на ПК):

```
YANDEX_DIRECT_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SECRET_KEY=любая-длинная-случайная-строка
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ваш-надёжный-пароль
REPORT_TIME=09:00
TIMEZONE=Europe/Moscow
VAT_RATE=0.22
```

Сохранить в nano: `Ctrl+O` → Enter → `Ctrl+X`.

## D3. Запустите Docker

```bash
docker compose up -d --build
```

Первый раз — 5–10 минут. Проверка:

```bash
docker compose ps
```

Должно быть **running**.

## D4. Откройте порт 8080

В панели DockerHosting → Firewall / Сеть → разрешите **TCP 8080**.

На сервере (если включён firewall):

```bash
ufw allow 8080
```

## D5. Откройте сайт

В браузере:

```
http://IP_ВАШЕГО_СЕРВЕРА:8080
```

Логин: `admin`  
Пароль: из `ADMIN_PASSWORD` в `.env`

---

# БЛОК E. Автодеплой (CI/CD)

При каждом `git push` GitHub сам зайдёт на сервер и обновит проект.

Нужен **второй** SSH-ключ — для входа GitHub Actions **на ваш VPS**.

## E1. Создайте ключ на Windows

PowerShell:

```powershell
ssh-keygen -t ed25519 -C "github-actions" -f $env:USERPROFILE\.ssh\github_actions_deploy -N '""'
```

## E2. Публичный ключ → на сервер

Показать ключ:

```powershell
type $env:USERPROFILE\.ssh\github_actions_deploy.pub
```

Скопируйте строку. В **PuTTY** на сервере:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "ВСТАВЬТЕ_СЮДА_ПУБЛИЧНЫЙ_КЛЮЧ" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

## E3. Приватный ключ → секреты GitHub

1. Репозиторий → **Settings** → **Secrets and variables** → **Actions**  
2. **New repository secret** — добавьте по одному:

| Name | Что вставить |
|------|----------------|
| `SSH_PRIVATE_KEY` | Откройте `C:\Users\Frostbitten\.ssh\github_actions_deploy` в Блокноте — **весь файл** от `-----BEGIN` до `-----END` |
| `SERVER_HOST` | IP сервера, например `185.123.45.67` |
| `SERVER_USER` | `root` |
| `SERVER_PORT` | `22` |

---

# БЛОК F. Проверка автодеплоя

## F1. Тестовый push

PowerShell на ПК:

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot
git add .
git commit -m "test: проверка автодеплоя"
git push
```

## F2. Смотрите GitHub Actions

1. Репозиторий на GitHub → вкладка **Actions**  
2. Workflow **Deploy to VPS**  
3. Ждите 1–3 минуты  
4. **Зелёная галочка** = деплой успешен  

## F3. Ручной деплой (кнопка)

**Actions** → **Deploy to VPS** → **Run workflow** → **Run workflow**

---

# БЛОК G. Как работать каждый день

После любых изменений в коде:

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot
git add .
git commit -m "кратко: что изменили"
git push
```

GitHub Actions обновит сервер автоматически.

**Не коммитьте `.env`** — секреты только на сервере и локально у вас.

**Клиенты и база** — в папке `data/` на сервере; при деплое не затираются.

---

# БЛОК H. Если что-то пошло не так

## `git push` не работает

- Проверьте логин в URL репозитория  
- Используйте Personal Access Token вместо пароля  

## `ssh -T git@github.com` на сервере — отказ

- Deploy key добавлен в GitHub?  
- Скопировали **публичный** ключ (`*.pub`), не приватный?  

## GitHub Actions — красный крест

Откройте job → шаг **Deploy over SSH**:

| Текст ошибки | Решение |
|--------------|---------|
| `Permission denied` | Проверьте `SSH_PRIVATE_KEY` и `authorized_keys` на сервере |
| `.env not found` | Создайте `.env` в `/opt/direct-analytics-bot` |
| `git fetch` failed | Deploy key и `~/.ssh/config` на сервере |

## Проверка деплоя вручную на сервере

```bash
cd /opt/direct-analytics-bot
bash scripts/deploy.sh
```

## Логи приложения

```bash
cd /opt/direct-analytics-bot
docker compose logs -f
```

---

# Чеклист «всё готово»

- [ ] GitHub: репозиторий создан, код залит (`git push`)  
- [ ] VPS: PuTTY подключается  
- [ ] Docker работает (`docker --version`)  
- [ ] Deploy key на GitHub, `ssh -T git@github.com` OK  
- [ ] Репозиторий склонирован в `/opt/direct-analytics-bot`  
- [ ] `.env` на сервере заполнен  
- [ ] `docker compose up -d --build` — сайт открывается  
- [ ] Порт 8080 открыт  
- [ ] Secrets в GitHub: `SSH_PRIVATE_KEY`, `SERVER_HOST`, `SERVER_USER`, `SERVER_PORT`  
- [ ] Push → Actions → зелёная галочка  

---

# Схема

```
┌─────────────┐    git push     ┌─────────────┐
│  Ваш ПК     │ ──────────────► │   GitHub    │
│  Windows    │                 │  (код)      │
└─────────────┘                 └──────┬──────┘
                                       │
                              GitHub Actions
                              (SSH на VPS)
                                       │
                                       ▼
                              ┌─────────────┐
                              │  VPS        │
                              │  git pull   │
                              │  docker up  │
                              └─────────────┘
```

Два SSH-ключа:

- **На сервере** → GitHub (Deploy key) — сервер качает код  
- **GitHub Actions** → сервер (CI key) — GitHub запускает деплой  
