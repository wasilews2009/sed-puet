# СЕД ПУЕТ 0.1.0 — пакет для розгортання

Електронний документообіг ПУЕТ з ШІ-асистентом: реєстрація документів з OCR і реквізитами від локальної LLM,
ручна перевірка результатів ШІ (поріг впевненості 0.85), нормативний аудит з цитатами пунктів актів,
маршрути погодження з візами і КЕП. ШІ лише пропонує: юридичної сили документ набуває після КЕП посадової особи.

Усе працює всередині периметра університету: LLM, ембединги, OCR, сховище файлів, SSO.

[![Відкрити в GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/wasilews2009/sed-puet?quickstart=1)

Демо одним кліком: кнопка вище запускає весь стек у GitHub Codespaces (див. [«Демо в GitHub Codespaces»](#демо-в-github-codespaces)).

## Що всередині

| Шлях | Що це |
|---|---|
| `docker-compose.yml` | увесь стек: веб-інтерфейс (nginx), API, міграції, PostgreSQL 16 + pgvector, MinIO, Qdrant, Keycloak, сервер ембедингів, LLM (vLLM або Ollama) |
| `.env.example`, `scripts/init-env.sh` | налаштування; скрипт створює `.env` з випадковими секретами |
| `docker/app.Dockerfile` | образ API (Python 3.13, Tesseract ukr+eng); той самий образ виконує міграції |
| `docker/web.Dockerfile` | збірка інтерфейсу (React) з `modules/sed-frontend` + nginx; Node.js на сервері не потрібен |
| `deploy/keycloak/` | realm `puet`: `realm-puet.json` (без користувачів, для production) і `realm-puet.demo.json` (10 демо-користувачів) |
| `deploy/nginx/` | конфіг nginx: CSP, заголовки безпеки, проксі `/api` на застосунок |
| `constraints.txt` | точні версії Python-залежностей, з якими реліз перевірено |
| `modules/` | вихідний код модулів: `db-schema`, `ingest-pipeline`, `rag-compliance`, `approval-routing`, `sed-app`, `sed-frontend` (з їхніми README і OpenAPI) |
| `docs/smoke-test/` | знімки екрана з перевірки цього релізу |
| `.devcontainer/` | конфігурація GitHub Codespaces: демо-стенд стартує сам після створення codespace |
| `.github/workflows/ci.yml` | CI: тести всіх модулів на PostgreSQL 16 + pgvector, тести і збірка інтерфейсу, збірка Docker-образів |

```
 Браузер ──► web (nginx :8080) ──/api──► app (FastAPI) ──► db (PostgreSQL+pgvector)
    │            статичний інтерфейс        │  ├─► minio (оригінали, підписи; SSE)
    │                                       │  ├─► qdrant + embeddings (нормативний корпус)
    └──► keycloak (:8180) ◄── JWKS ─────────┘  └─► llm (vLLM / Ollama, OpenAI-сумісний API)
              OIDC + PKCE
 migrate (одноразово перед app): міграції Alembic, DDL ключів КЕП, демо-дані
```

## Вимоги до сервера

* Linux x86-64, Docker Engine 24+ з Compose v2 (`docker compose version`).
* Без LLM: 4 ядра, 8 ГБ RAM, 30 ГБ диска. Сервер ембедингів bge-m3 на CPU — ще ≈ 3 ГБ RAM.
* LLM Mistral Small 3.1 24B: на vLLM — NVIDIA GPU з ≈ 48 ГБ VRAM (або 2×24 ГБ) і NVIDIA Container Toolkit;
  на Ollama (CPU) — ≥ 32 ГБ RAM, відповіді повільні (хвилини), лише для ознайомлення.
* Для першого запуску потрібен доступ до Docker Hub, quay.io, ghcr.io, PyPI, репозиторіїв Debian і Hugging Face
  (ваги моделей). Після завантаження можна працювати без інтернету (`HF_HUB_OFFLINE=1`).

## Встановлення (демо-стенд)

```bash
git clone https://github.com/wasilews2009/sed-puet.git && cd sed-puet
scripts/init-env.sh                          # створює .env з випадковими паролями і ключами
docker compose --profile gpu up -d --build   # з GPU (vLLM)
# або без GPU:
#   у .env закоментуйте рядки vLLM і розкоментуйте рядки Ollama (розділ «Локальна LLM»), потім
#   docker compose --profile cpu up -d --build
#   docker compose exec llm-ollama ollama pull mistral-small3.1:24b
docker compose ps                            # app і web мають бути healthy
```

Перший старт довгий: збираються образи, vLLM і сервер ембедингів завантажують ваги (десятки ГБ).

Проіндексуйте нормативний корпус (Статут і положення ПУЕТ, розпізнані з PDF; повторний запуск оновлює лише змінені акти):

```bash
docker compose exec app python -m sed_rag.ingestion.cli ingest-manifest /opt/sed/rag-compliance/corpus/manifest.toml
```

Відкрийте http://localhost:8080 → «Увійти через SSO ПУЕТ». Демо-користувачі (пароль `demo`):

| Логін | Хто | Роль |
|---|---|---|
| `rector` | Олександр Демченко | ректор |
| `vicerector` | Ірина Савчук | проректор |
| `director` | Віктор Гнатюк | директор інституту |
| `head.kknit` | Наталія Кравець | завідувачка кафедри, викладачка |
| `lecturer` | Андрій Мельник | викладач |
| `student` | Марія Бондар | студентка |
| `studgov` | Денис Ткачук | голова студентського самоврядування |
| `buh` | Людмила Романенко | керівниця підрозділу (бухгалтерія) |
| `pfv` | Тетяна Литвин | керівниця підрозділу (планово-фінансовий відділ) |
| `clerk` | Оксана Павленко | співробітниця |

Адмінка Keycloak: http://localhost:8180, логін `admin`, пароль — `KEYCLOAK_ADMIN_PASSWORD` з `.env`.
Консоль MinIO: http://localhost:9001 (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`).

**Основний сценарій для перевірки** (так перевірено цей реліз, знімки в `docs/smoke-test/`): `lecturer` реєструє
службову записку (PDF або скан) → на картці перевіряє реквізити від ШІ, підтверджує або виправляє ті, що нижче 0.85,
переносить упевнені в картку → «Нормативний аудит» → «Погодження»: сформувати і запустити маршрут → по черзі
`head.kknit`, `vicerector`, `buh`, `rector`, `pfv` у розділі «На погодження» накладають візу з КЕП (на стенді —
«Тестовий ключ стенду») → статус «Погоджено», 5 чинних підписів. `student` цього документа не бачить.

## Демо в GitHub Codespaces

Для показу системи без власного сервера натисніть кнопку **Open in GitHub Codespaces** вгорі
(або на сторінці репозиторію Code → Codespaces → Create codespace). Далі все відбувається само:

1. Codespaces створює віртуальну машину з 4 ядрами і 16 ГБ пам'яті (менша не підійде).
2. `.devcontainer/setup.sh` генерує `.env` з випадковими секретами під адресу саме цього codespace
   і збирає образи (перше створення займає 10–20 хвилин).
3. `.devcontainer/start.sh` піднімає стек, завантажує модель і у фоні індексує нормативний корпус.
   Після цього у браузері відкривається вкладка з інтерфейсом (порт 8080 у вкладці **Ports**).

Вхід: «Увійти через SSO ПУЕТ», демо-користувачі з таблиці вище, пароль `demo`.

Чим демо в Codespaces відрізняється від сервера:

* Порт 8080 спершу приватний. Якщо браузер показує **HTTP ERROR 401** (це автентифікація самого Codespaces),
  зробіть порт публічним на час показу: вкладка **Ports** → правою кнопкою на 8080 → Port Visibility → Public.
  Тоді сторінку входу бачить кожен, хто знає адресу codespace, а демо-пароль у всіх `demo`, тож не публікуйте
  посилання, а після показу поверніть Private або зупиніть codespace.
* Keycloak працює на тому ж адресі під шляхом `/auth` (адмінка: `…/auth/admin`, логін `admin`,
  пароль `KEYCLOAK_ADMIN_PASSWORD` з `.env`), тож окремий порт 8180 відкривати не треба.
* LLM працює на процесорі через Ollama з компактною моделлю `gemma3:4b`, бо Mistral Small 24B потребує
  ≥ 32 ГБ RAM. Розпізнавання реквізитів і аудит працюють, але повільніше і менш точно, ніж на сервері з GPU.
  Іншу модель можна задати секретом Codespaces `SED_CODESPACES_LLM_MODEL` (Settings → Secrets → Codespaces) до створення codespace.
* Безкоштовна квота GitHub на Codespaces обмежена (для машини з 4 ядрами це близько 30 годин на місяць,
  плюс ліміт на диск). Зупиняйте codespace, коли ним не користуєтеся, і видаляйте після показу.
* Демо-дані й ключі КЕП імітаційні (`SED_ROUTING_KEP_ALLOW_MOCK=true`): підписи не мають юридичної сили.

Перезапустити стек у codespace: `bash .devcontainer/start.sh`; журнал індексації корпусу —
`.devcontainer/corpus-index.log`.

## Доступ з інших комп'ютерів і TLS

Порти за замовчуванням слухають лише `127.0.0.1`. Для роботи в мережі університету:

1. Поставте перед стеком реверс-проксі з TLS (nginx, Traefik, HAProxy) з двома іменами, наприклад
   `https://sed.puet.edu.ua` → `127.0.0.1:8080` і `https://sso.puet.edu.ua` → `127.0.0.1:8180`.
2. У `.env`: `SED_PUBLIC_URL=https://sed.puet.edu.ua`, `KEYCLOAK_PUBLIC_URL=https://sso.puet.edu.ua`.
3. `docker compose up -d`. Адреси підставляються в realm Keycloak (дозволені redirect URI клієнта `sed-web`),
   у видавця токенів застосунку і в CSP інтерфейсу.

Realm імпортується лише при першому старті Keycloak (том `keycloak`). Якщо адресу змінено пізніше, виправте
redirect URI і Web origins клієнта `sed-web` в адмінці Keycloak.

Без реверс-проксі (лише для внутрішнього тесту): `SED_BIND_ADDR=0.0.0.0` і адреси `http://<ip-сервера>:8080` /
`http://<ip-сервера>:8180`. Keycloak вимагає HTTPS для зовнішніх адрес (`sslRequired=external`), тож без TLS вхід
працює лише з localhost і приватних мереж.

## Перехід на production

| Що | Як |
|---|---|
| Режим | `SED_APP_ENVIRONMENT=production`, `SED_ROUTING_ENVIRONMENT=production` |
| Демо-дані | `SED_MIGRATE_FLAGS=` (порожньо), `KEYCLOAK_REALM_FILE=realm-puet.json` — до першого старту |
| КЕП | `SED_ROUTING_KEP_ALLOW_MOCK=false` (у production імітаційний КНЕДП заборонено); налаштування довірених ЦСК — `modules/approval-routing/README.md` |
| Keycloak | бажано університетський Keycloak з власною БД: імпортуйте `deploy/keycloak/realm-puet.json`, приберіть сервіс `keycloak` з compose, задайте `KEYCLOAK_PUBLIC_URL` і `SED_APP_OIDC_JWKS_URL` |
| Користувачі | створюються в Keycloak з realm-ролями; при першому вході застосунок заводить картку без ролей, ролі з прив'язкою до підрозділу призначає адміністратор (`user_roles`). Діє перетин ролей Keycloak і БД |
| MinIO | `MINIO_KMS_SECRET_KEY` — лише для стенду; у production — KES/Vault |
| Резервні копії | томи `db`, `minio`, `qdrant`, `keycloak`; БД: `docker compose exec db pg_dump -U sed -Fc sed > sed.dump`. Qdrant відновлюється повторною індексацією корпусу |

## Оновлення

```bash
docker compose down            # томи з даними зберігаються
# розпакуйте новий реліз поруч, перенесіть .env
docker compose --profile gpu up -d --build    # migrate накотить нові міграції перед стартом app
```

## Діагностика

* `docker compose logs -f app` — помилки API; `docker compose logs migrate` — міграції.
* `curl -s localhost:8080/healthz` — живість API через nginx.
* Реєстрація «висить» або реквізити не з'являються → перевірте LLM: `docker compose logs llm-vllm` (або `llm-ollama`).
  Якщо модель недоступна, документ реєструється, а реквізити вводяться вручну на картці.
* Нормативний аудит падає → `docker compose logs embeddings qdrant` і чи проіндексовано корпус.
* Реєстр недоступний або quay.io/ghcr.io закриті → у `.env` задайте `MINIO_IMAGE`, `KEYCLOAK_IMAGE`,
  `EMBEDDINGS_IMAGE` на дзеркала (наприклад, Keycloak є також на Docker Hub: `keycloak/keycloak:26.0.7`).

## Як цей реліз перевірено

У середовищі збирання (2026-10-18) стек піднято через `docker compose` з цими файлами і пройдено основний сценарій
у Chromium через справжній вхід Keycloak, двічі (вдруге — після `docker compose down` / `up`: міграції і демо-дані
ідемпотентні, дані збереглися). OCR Tesseract ukr+eng перевірено на зображенні з українським текстом,
production-режим застосунку стартує. Тести інтерфейсу: 14/14.

Відмінності середовища перевірки від цільового сервера (там були закриті Hugging Face, quay.io, ghcr.io і
репозиторії Debian):

* LLM і ембединги підмінено детермінованою заглушкою з OpenAI-сумісним API; справжні vLLM/Ollama і bge-m3
  тут не запускались, якість реквізитів і аудиту на реальній моделі цим тестом не перевірено.
* Образ API зібрано з тим самим Python 3.13 і кодом, але Tesseract узято з Ubuntu 24.04, а не з Debian;
  `docker/app.Dockerfile` у пакеті (Debian) тут не збирався через закриті репозиторії.
* MinIO запускався з образу `pgsty/minio`, Keycloak — з `keycloak/keycloak:26.0.7` (Docker Hub).

## Відомі обмеження

* Демо-користувачі в realm мають тимчасовий пароль, але Keycloak 26 при імпорті не вимагає його змінити — демо-realm
  не використовуйте поза стендом.
* Інжест і нормативний аудит виконуються синхронно в запиті (nginx чекає до 600 с); на CPU-моделі довгі документи
  можуть не вкластися.
* Підпис КЕП ДСТУ 4145 у браузері потребує бібліотеки ІІТ (`modules/sed-frontend/README.md`, `registerSigner`);
  без неї — через файл підпису .p7s.
* Перелік змін API, яких чекає інтерфейс, — `modules/sed-frontend/README.md`, розділ «Потрібні зміни API».
