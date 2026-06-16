# Git + автодеплой (CI/CD) — пошагово

После настройки: вы меняете код на компьютере → `git push` → сервер **сам** обновляется и перезапускает Docker.

Нужны **два SSH-ключа** (это нормально, у них разные задачи):

| Ключ | Где создаётся | Куда кладётся | Зачем |
|------|---------------|---------------|-------|
| **Deploy key** | На **сервере** | GitHub → Deploy keys | Сервер делает `git pull` |
| **CI key** | На **вашем ПК** | GitHub Secrets + `authorized_keys` на сервере | GitHub Actions подключается к серверу |

---

## Часть 1. Репозиторий на GitHub

### 1.1. Установите Git на Windows

https://git-scm.com/download/win — всё по умолчанию, Next → Finish.

### 1.2. Создайте репозиторий на GitHub

1. https://github.com/new  
2. Name: `direct-analytics-bot`  
3. **Private** (рекомендуется — там секреты в `.env.example` не должны быть, но проект закрытый)  
4. **Не** ставьте галочки README / .gitignore — они уже есть локально  
5. Create repository

### 1.3. Залейте код с компьютера

PowerShell:

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:ВАШ_ЛОГИН/direct-analytics-bot.git
git push -u origin main
```

> Файлы `.env` и `data/` в git **не попадут** — они в `.gitignore`. На сервере `.env` создаётся один раз вручную.

Если `git push` просит авторизацию — настройте SSH для GitHub на ПК или используйте HTTPS с Personal Access Token.

---

## Часть 2. Deploy key — сервер ↔ GitHub

Чтобы сервер мог **скачивать** код из приватного репозитория.

### 2.1. Подключитесь к VPS (PuTTY)

### 2.2. Создайте ключ на сервере

```bash
ssh-keygen -t ed25519 -C "deploy-direct-analytics" -f ~/.ssh/github_deploy -N ""
```

### 2.3. Скопируйте **публичный** ключ

```bash
cat ~/.ssh/github_deploy.pub
```

Скопируйте всю строку (`ssh-ed25519 AAAA...`).

### 2.4. Добавьте в GitHub

1. Репозиторий → **Settings** → **Deploy keys** → **Add deploy key**  
2. Title: `vps-dockerhosting`  
3. Key: вставьте скопированное  
4. **Allow write access** — **не включайте** (только чтение)  
5. Add key

### 2.5. Настройте SSH на сервере для GitHub

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

### 2.6. Проверка

```bash
ssh -T git@github.com
```

Должно быть: `Hi USERNAME/REPO! You've successfully authenticated...`

---

## Часть 3. Первый клон на сервере

```bash
mkdir -p /opt
cd /opt
git clone git@github.com:ВАШ_ЛОГИН/direct-analytics-bot.git
cd direct-analytics-bot
chmod +x scripts/deploy.sh
```

### 3.1. Файл `.env` (один раз)

```bash
cp .env.example .env
nano .env
```

Заполните токены, пароль, `SECRET_KEY`. Сохраните: `Ctrl+O`, Enter, `Ctrl+X`.

Если уже работало локально — проще скопировать `.env` и папку `data/` через WinSCP.

### 3.2. Первый запуск

```bash
docker compose up -d --build
```

Проверка: `http://IP_СЕРВЕРА:8080`

---

## Часть 4. CI key — GitHub Actions → сервер

Чтобы при `git push` GitHub **подключался к VPS** и запускал деплой.

### 4.1. Создайте ключ на **Windows** (PowerShell)

```powershell
ssh-keygen -t ed25519 -C "github-actions-deploy" -f $env:USERPROFILE\.ssh\github_actions_deploy -N '""'
```

Появятся файлы:
- `github_actions_deploy` — **приватный** (никому не показывать)
- `github_actions_deploy.pub` — **публичный**

### 4.2. Публичный ключ → на сервер

```powershell
type $env:USERPROFILE\.ssh\github_actions_deploy.pub
```

Скопируйте строку. На **сервере**:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "ВСТАВЬТЕ_ПУБЛИЧНЫЙ_КЛЮЧ_СЮДА" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 4.3. Приватный ключ → GitHub Secrets

1. Репозиторий → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Добавьте:

| Name | Value |
|------|--------|
| `SSH_PRIVATE_KEY` | содержимое файла `github_actions_deploy` **целиком** (включая `-----BEGIN...` и `-----END...`) |
| `SERVER_HOST` | IP вашего VPS, например `185.123.45.67` |
| `SERVER_USER` | `root` |
| `SERVER_PORT` | `22` (если другой — укажите свой) |
| `APP_DIR` | `/opt/direct-analytics-bot` (можно не добавлять — подставится по умолчанию) |

---

## Часть 5. Как пользоваться каждый день

На компьютере после любых изменений:

```powershell
cd C:\Users\Frostbitten\direct-analytics-bot
git add .
git commit -m "описание изменения"
git push
```

1. Откройте GitHub → вкладка **Actions**  
2. Должен запуститься workflow **Deploy to VPS**  
3. Зелёная галочка = деплой успешен  

На сервере вручную ничего делать не нужно.

### Ручной деплой (кнопка)

GitHub → **Actions** → **Deploy to VPS** → **Run workflow** → **Run workflow**

---

## Часть 6. Что не попадает в Git

| Файл/папка | Где живёт |
|------------|-----------|
| `.env` | Только на сервере (и локально у вас) |
| `data/app.db` | Только на сервере — клиенты и настройки |

При деплое они **не затираются** — обновляется только код.

---

## Часть 7. Если деплой упал

### GitHub Actions → красный крест → открыть job → шаг Deploy

Частые причины:

| Ошибка | Решение |
|--------|---------|
| `Permission denied (publickey)` | Проверьте `SSH_PRIVATE_KEY` и `authorized_keys` на сервере |
| `git fetch` failed | Deploy key на GitHub / `~/.ssh/config` на сервере |
| `.env not found` | Создайте `.env` на сервере в `/opt/direct-analytics-bot` |
| `docker compose` failed | На сервере: `cd /opt/direct-analytics-bot && docker compose logs` |

### Проверка на сервере вручную

```bash
cd /opt/direct-analytics-bot
bash scripts/deploy.sh
```

---

## Схема

```
Ваш ПК  ──git push──►  GitHub
                          │
                          │ GitHub Actions (SSH)
                          ▼
                       VPS сервер
                       git pull + docker compose up
```
