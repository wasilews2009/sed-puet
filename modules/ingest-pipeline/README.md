# Інжест вхідних документів СЕД ПУЕТ (модуль OCR Pipeline / IDP)

`POST /api/v1/documents/ingest` приймає PDF або зображення і:

1. зберігає оригінал у **MinIO** (versioning, SSE, SHA-256 у метаданих);
2. витягує сирий текст: **текстовий шар PDF першим**, **Tesseract `ukr+eng`** для сканів і зіпсованих шарів (посторінково);
3. **псевдонімізує ПД** і передає текст у **локальну LLM** (vLLM / Ollama, OpenAI-сумісний API) зі **structured output** за Pydantic-схемою;
4. повертає **валідовані реквізити за ДСТУ 4163:2020** (вид, дата, реєстраційний індекс, автор, заголовок, короткий зміст, зацікавлені особи), кожен зі своїм **confidence score**, і створює картку документа в статусі «чернетка».

Будується на шарі даних `../db-schema` (моделі `sed_db`, DTO `AiRunCreate`/`AiRunResult`, тригери HITL та `audit_logs`); ці файли модуль не змінює.

Перевірено: **81 тест зелений**, з них 6 інтеграційних на живому PostgreSQL 16 + pgvector (міграція `0001` з `../db-schema`, тригери, хеш-ланцюг аудиту); OCR перевіряється на справжньому Tesseract 5.3.4 на згенерованих сканах. MinIO і LLM у тестах підмінені (фейковий клієнт `minio`, `httpx.MockTransport`).

## Архітектура

```mermaid
flowchart LR
    U[Діловод / працівник] -- "multipart + Bearer JWT" --> API
    KC[Keycloak] -. JWKS .-> API
    subgraph API["FastAPI · sed_ingest.api"]
        R[routes.ingest_document] --> V[file_validation<br/>magic bytes, розмір]
    end
    V --> O[IngestPipeline<br/>оркестратор]
    O -->|1. put| S3[(MinIO<br/>sed-documents)]
    O -->|2. to_thread| OCR[TesseractOcrEngine<br/>pypdfium2 + Tesseract]
    O -->|3.| X[RequisitesExtractor]
    X --> PII[RegexPseudonymizer<br/>ПІБ → ОСОБА_n]
    X -->|JSON Schema strict| LLM[[vLLM / Ollama<br/>локально]]
    X --> SC[scoring<br/>ґрунтування цитат,<br/>формат, якість OCR]
    O -->|4. одна транзакція| DB[(PostgreSQL<br/>documents, versions,<br/>ai_runs, ai_extracted_fields)]
    DB -. тригер на COMMIT .-> AU[(audit_logs<br/>ai_analysis)]
    O -. помилка після put .-> S3
```

Clean Architecture: `domain/` (реквізити, схема LLM, результат) і `pipeline/` (оркестратор, скоринг, PII, промпт) залежать лише від портів `ports.py`. `adapters/` реалізують порти (MinIO, Tesseract, OpenAI-сумісна LLM, SQLAlchemy). `api/` — HTTP, автентифікація і composition root. Той самий `IngestPipeline.run()` можна викликати з Arq/Celery-воркера.

### Потік одного запиту

```mermaid
sequenceDiagram
    participant C as Клієнт
    participant A as API
    participant P as IngestPipeline
    participant M as MinIO
    participant T as OCR
    participant L as LLM
    participant D as PostgreSQL
    C->>A: POST /ingest (file, org_unit_id)
    A->>A: JWT (підпис, iss, aud, exp) · тип за вмістом · ліміт розміру
    A->>P: IngestCommand
    P->>D: користувач активний і має право на підрозділ? дублікат SHA-256?
    P->>M: put documents/{doc}/v1/{ver}.pdf
    P->>T: текстовий шар / Tesseract (пул потоків, ліміт паралельності)
    P->>P: маскування ПД → промпт (дані в маркерах, початок+кінець довгого тексту)
    P->>L: chat/completions, response_format=json_schema strict, temperature 0
    L-->>P: JSON з мітками [ОСОБА_n]
    P->>P: Pydantic-валідація (1 повтор з помилками) → скоринг → відновлення ПД
    P->>D: Document(draft) + Version + AiRun(ocr) + AiRun(ner) + AiExtractedField × 7
    D-->>D: COMMIT → audit_logs (confidence, метадані; ПД замасковано)
    P-->>C: 201 IngestResponse
    Note over P,M: будь-яка помилка після put → видалення об'єкта (компенсація)
```

## Структура

| Шлях | Що це |
|---|---|
| `sed_ingest/domain/requisites.py` | Види документів, ролі зацікавлених осіб, обов'язкові реквізити за видом, `FieldResult[T]`, `DocumentRequisites` |
| `sed_ingest/domain/llm_schema.py` | Pydantic-схема structured output (`extra="forbid"`, усі поля обов'язкові, nullable) |
| `sed_ingest/domain/result.py` | `IngestResponse`: що повертає API |
| `sed_ingest/domain/models.py` | Внутрішні об'єкти й помилки домену (з HTTP-кодами та повідомленнями українською) |
| `sed_ingest/ports.py` | Протоколи `ObjectStorage`, `OcrEngine`, `PiiMasker`, `StructuredLlm`, `IngestRepository` |
| `sed_ingest/pipeline/orchestrator.py` | Послідовність кроків, компенсація MinIO, посилення грифу доступу |
| `sed_ingest/pipeline/extraction.py` | Маскування → LLM → валідація з одним «ремонтом» → скоринг |
| `sed_ingest/pipeline/scoring.py` | Підсумковий confidence кожного реквізиту |
| `sed_ingest/pipeline/pii.py` | Оборотна псевдонімізація ПД (РНОКПП, IBAN, телефон, e-mail, паспорт/ID, ПІБ) |
| `sed_ingest/pipeline/prompts.py` | Версіонований системний промпт, захист маркерів даних, бюджет контексту |
| `sed_ingest/pipeline/normalize.py` | Дати «17.10.2026» / «“17” жовтня 2026 р.», індекси «№ 245-од» |
| `sed_ingest/adapters/*` | MinIO, Tesseract + pypdfium2, OpenAI-сумісна LLM (httpx), SQLAlchemy async |
| `sed_ingest/api/*` | FastAPI-застосунок, ендпоінт, Keycloak JWT |
| `openapi.yaml` | Згенерована OpenAPI 3.1 специфікація ендпоінта |

## Контракт

```bash
curl -X POST https://sed.puet.local/api/v1/documents/ingest \
  -H "Authorization: Bearer $TOKEN" -H "X-Request-ID: 7f3c…" \
  -F file=@nakaz_245.pdf -F org_unit_id=1b0e…   # необов'язково: direction, access_level, correspondent_name
```

Відповідь `201` (скорочено, справжній вивід тесту на скані):

```json
{
  "status": "needs_review",
  "document_type_code": "ORDER_MAIN",
  "confidence": "0.7904", "confidence_threshold": "0.85", "requires_human_review": true,
  "review_reasons": ["реєстраційний індекс: впевненість 0.7904 < 0.85 (Низька якість розпізнавання (58%))"],
  "requisites": {
    "document_type": {"value": "order_main", "confidence": "0.9297", "requires_human_review": false, "...": "..."},
    "document_date": {"value": "2026-10-17", "confidence": "0.9348", "evidence": "17.10.2026 м. Полтава № 245-00", "page_no": 1},
    "registration_number": {"value": "245-00", "confidence": "0.7904", "requires_human_review": true,
                            "issues": ["Низька якість розпізнавання (58%)"]},
    "author": {"value": {"full_name": "Іван РЕКТОРЕНКО", "position": "Ректор"}, "confidence": "0.9297"},
    "title": {"value": "Про призначення відповідальних за організацію практики", "confidence": "0.9297"},
    "summary": {"value": "Призначено відповідальних за організацію практики…", "confidence": "0.8808"},
    "interested_parties": {"value": [{"full_name": "Петренко Олену Іванівну", "role": "executor", "...": "..."},
                                     {"full_name": "Коваленка Андрія Петровича", "role": "controller"}],
                           "confidence": "0.9297"},
    "document_type_label": "Наказ з основної діяльності"
  },
  "ocr": {"used_ocr": true, "quality": "0.9466", "pages": [{"page_no": 1, "source": "ocr", "...": "..."}]},
  "file": {"storage_key": "documents/{document_id}/v1/{version_id}.png", "sha256": "…"},
  "ai_runs": {"ocr": "…", "extraction": "…"}
}
```

Тут Tesseract прочитав «№ 245-од» як «245-00»; LLM чесно повторила прочитане, але впевненість саме цього слова в OCR була 0.58, тож номер, і лише він, пішов на ручну валідацію.

Статуси: `processed` (усе ≥ 0.85), `needs_review` (є реквізити < 0.85 → черга оператора), `manual_entry_required` (текст не розпізнано або LLM недоступна: картка й файл збережені, запуск NER `failed` у черзі оператора). Помилки: `401`, `403`, `415`, `422`, `503`, тіло `{"detail": "…українською…", "code": "…"}`.

## Як рахується confidence

Самооцінка моделі — лише верхня межа (≤ 0.97). Підсумок кожного реквізиту:

```
conf = min(llm, 0.97) × ґрунтування × узгодженість × формат × (0.6 + 0.4 × якість_OCR)
```

| Сигнал | Як перевіряється | Вплив |
|---|---|---|
| ґрунтування | цитату-підставу знайдено в тексті, який бачила модель (дослівно або за словесними триграмами) | < 0.6 → ×(g·0.5), «можлива галюцинація» |
| узгодженість | значення міститься в цитаті; ПІБ є в тексті (модель бачить лише мітки, тож вигадане ім'я одразу видно) | ×0.8 |
| формат | дата розпізнається і в межах 24.08.1991…сьогодні; індекс схожий на реєстраційний | ×0.6, значення відкидається |
| якість OCR | 1.0 для текстового шару; для скану — **мінімальна впевненість Tesseract серед слів самого значення** | 0.6…1.0 |
| вид документа | назва виду є в шапці (ціле слово) / вказано інший вид | ×1.0 / ×0.6 / ×0.85 |
| короткий зміст | частка змістовних слів, що спираються на текст | ×(0.7 + 0.3·опора) |

Відсутній **обов'язковий** реквізит (набір залежить від виду: у наказу є номер, у заяви номера до реєстрації немає) дає 0. Впевненість документа — **мінімум** по реквізитах. Поріг 0.85 береться з `sed_db.base.CONFIDENCE_THRESHOLD`.

## Що пишеться в БД (одна транзакція)

| Таблиця | Запис |
|---|---|
| `documents` | `status = draft`, вид за класифікацією (або `UNSORTED`, якщо класифікація < 0.85 чи LLM недоступна), `author_id` — хто завантажив; `requisites` лишається `{}`: туди реквізити переносить людина після валідації |
| `document_versions` | версія 1, `source = ocr_scan / upload`, ключ і `version_id` MinIO, SHA-256, `extracted_text` |
| `ai_runs` (ocr) | `tesseract` + версія, якість по сторінках у `output`, `pending`, якщо якість < 0.85 |
| `ai_runs` (ner_extraction) | `pii_masked = true`, `input_sha256` промпту (без сирого тексту), `output` з **мітками** ПД, токени, затримка, `prompt_template_id` |
| `ai_extracted_fields` | 7 реквізитів зі значеннями, confidence, сторінкою; `pending` для < 0.85 |
| `audit_logs` | пише тригер на COMMIT: confidence, клас, метадані (поля з `"x-pii": true` — замасковано) |

Перед записом дані проходять DTO `AiRunCreate`/`AiRunResult` зі схеми, тож ті самі бізнес-правила перевіряються ще до БД.

## Запуск

```bash
apt install tesseract-ocr tesseract-ocr-ukr          # + osd для автоповороту (є в пакеті tesseract-ocr)
pip install -r requirements.txt
export PYTHONPATH=.:../db-schema
export SED_INGEST_DATABASE_URL=postgresql+psycopg://sed_app:***@db/sed
export SED_INGEST_MINIO_ENDPOINT=minio:9000 SED_INGEST_MINIO_ACCESS_KEY=… SED_INGEST_MINIO_SECRET_KEY=…
export SED_INGEST_LLM_BASE_URL=http://vllm:8000/v1 SED_INGEST_LLM_MODEL=mistral-small-3.1-24b-instruct-uk
uvicorn sed_ingest.api.app:create_app --factory --host 0.0.0.0 --port 8080

# vLLM:   vllm serve <model> --guided-decoding-backend xgrammar
# Ollama: SED_INGEST_LLM_BASE_URL=http://ollama:11434/v1  SED_INGEST_LLM_PROVIDER=ollama  (Ollama ≥ 0.5)

# тести (інтеграційні з БД — лише якщо задано URL; БД перестворюється)
export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:***@localhost/sed_ingest_test
pytest -q
```

Усі налаштування — у `sed_ingest/config.py` (префікс `SED_INGEST_`): ліміти файлу та сторінок, DPI, мова OCR, бюджет контексту LLM, відповідність видів кодам `document_types`.

## Граничні випадки і ризики

| Ситуація | Як закрито |
|---|---|
| Файл з чужим розширенням / виконуваний файл | Тип визначається за magic bytes; ім'я файлу очищується від шляхів і керівних символів |
| Завеликий файл | Читається не більше ліміту + 1 байт; ліміт також варто задати в reverse proxy |
| Decompression bomb, гігантська сторінка | Ліміт пікселів на екземпляр рушія до декодування растру; PIL-захист як другий рубіж |
| PDF з паролем / пошкоджений | `422` з поясненням; завантажений об'єкт видаляється |
| Текстовий шар «Íàêàç» (cp1251 → latin-1) або шрифти без ToUnicode | Евристика якості шару; така сторінка розпізнається OCR |
| Змішаний PDF (друкований наказ + скан додатка) | Рішення посторінково, у кожної сторінки своя якість |
| Скан боком / догори дриґом | Tesseract OSD, автоповорот |
| «№» розпізнано як «Ne», «од» як «00» | Заміна «Ne/No/N2» перед цифрами на «№»; низька впевненість слова → ручна валідація саме цього реквізиту |
| Документ понад ліміт сторінок | Обробляються перші `max_pages`, попередження в `warnings`, `ocr.truncated` |
| Довгий документ і контекст LLM | Передається 70 % початку і 30 % кінця (шапка і підписи), попередження |
| Prompt injection у тексті документа | Текст лише між маркерами, маркери в тексті екрануються, промпт називає текст даними; вихід обмежено JSON-схемою; модель без інструментів і не впливає на статус документа |
| Галюцинація значення | Цитата-підстава звіряється з текстом; вигадане ПІБ видно, бо модель бачить лише мітки |
| Невалідний JSON / порушення схеми | Guided decoding на сервері; Pydantic-валідація; один повтор із текстом помилок |
| LLM недоступна або тайм-аут | 2 повтори з backoff; далі документ зберігається, NER `failed` у черзі оператора, статус `manual_entry_required` |
| БД недоступна після завантаження файлу | Компенсація: об'єкт видаляється з MinIO (shielded від скасування запиту) |
| Падіння процесу між `put` і COMMIT | Об'єкт без картки. Рекомендовано lifecycle-правило або щоденна звірка `documents/*` з `document_versions` |
| Повторне завантаження того самого файлу | `duplicate_of` і попередження; не блокується (типовий бланк може повторюватися) |
| Чужий підрозділ | `403` до завантаження в MinIO: потрібен основний підрозділ або роль у ньому (проректор/ректор — будь-який) |
| Вхідний документ без кореспондента | `422` одразу (так само вимагає CHECK у БД) |
| РНОКПП / паспорт / IBAN у тексті | Гриф автоматично посилюється до `personal_data` (лише посилення, не послаблення) |
| ПД у LLM | Псевдонімізація до виклику; `pii_masked = true`; у `ai_runs.output` лише мітки; сирий вхід не зберігається, тільки SHA-256 |
| Пропущені розпізнавачем ПД (ім'я без по батькові, ім'я в непрямому відмінку без ініціалів) | Залишковий ризик. Пом'якшено тим, що LLM локальна (on-premise); наступний крок — Presidio + spaCy `uk_core_news_lg` і словник ПІБ працівників із `users` через той самий порт `PiiMasker` |
| Хибне маскування абревіатур («Статут ПУЕТ») | Стоп-список абревіатур для шаблону «Ім'я ПРІЗВИЩЕ» |
| Зміна тексту промпту без нової версії | Звірка `body_sha256` з `prompt_templates` → помилка конфігурації |
| OCR «з'їдає» CPU API-вузла | Пул потоків з обмеженням паралельності (`ocr_concurrency`) |
| Довгі синхронні запити | Для пакетів сканів — фонова обробка тим самим `IngestPipeline` в Arq (див. нижче) |

## Що потрібно від схеми БД (тред «Схема БД для СЕД»)

1. **Seed `document_types`** з кодами з `config.document_type_codes` (`ORDER_MAIN`, `ORDER_PERSONNEL`, `ORDER_STUDENTS`, `DIRECTIVE`, `APPLICATION`, `MEMO`, `REPORT_MEMO`, `EXPLANATORY_NOTE`, `LETTER`, `PROTOCOL`, `REGULATION`) і обов'язково **`UNSORTED`** (вид за замовчуванням). Без `UNSORTED` інжест завершиться помилкою конфігурації.
2. У `requisites_schema` цих видів позначити `"author": {"x-pii": true}` і `"interested_parties": {"x-pii": true}`, інакше ПІБ потраплять у `audit_logs.extracted_metadata` відкритими (константа `PII_FIELD_KEYS`).
3. **Seed `prompt_templates`** для `ingest.requisites_dstu4163` v1 від службового користувача. Зараз запис створюється при першому інжесті з `created_by_id` = той, хто завантажив.
4. Ролі «діловод» і «оператор валідації ШІ» (уже в «Що далі» схеми): тоді право реєструвати документи підрозділу варто прив'язати до ролі діловода, а не до основного підрозділу.

## Що далі

- Фоновий режим: `202 Accepted` + задача Arq з тим самим оркестратором для пакетів і багатосторінкових сканів, статус через `ai_runs`.
- Presidio + spaCy `uk_core_news_lg` і словник ПІБ з `users` замість правил у `PiiMasker`.
- Детекція таблиць і рукописного тексту (DocTR / PaddleOCR) як другий `OcrEngine` для фото з телефона.
- Logprobs від vLLM як ще один сигнал впевненості; калібрування коефіцієнтів на розмічених документах ПУЕТ.
- Ендпоінт оператора: підтвердження/виправлення реквізитів (`HumanReviewDecision`) з перенесенням у `documents.requisites`.
