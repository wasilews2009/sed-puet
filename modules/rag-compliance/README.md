# Нормативний аудит проєктів наказів (модуль RAG-контур / Compliance Checker)

Сервіс бере проєкт наказу чи розпорядження, шукає у векторній базі **Qdrant** релевантні пункти
**Статуту ПУЕТ**, положень університету, законів України та актів **МОН**, проганяє кожен пункт
проєкту через граф перевірки **LangGraph** на локальній LLM і повертає **звіт про комплаєнс**:
вердикт, зауваження з дослівними цитатами-першоджерелами та **перелік конкретних пунктів актів,
які можуть бути порушені** («Статут ПУЕТ, п. 13.2», «Статут ПУЕТ, п. 7.2.13»).

Звіт ШІ не має юридичної сили. Будь-який висновок із впевненістю < 0.85 позначено для ручної
валідації, а збережена перевірка потрапляє в чергу оператора (`ai_runs.human_review_status = pending`).

Модуль спирається на шар даних `../db-schema` (таблиці `regulatory_*`, `compliance_*`, `ai_runs`,
тригери HITL і `audit_logs`) і перевикористовує з `../ingest-pipeline` клієнт локальної LLM,
псевдонімізацію ПД та перевірку JWT Keycloak. Файли цих модулів не змінено; що від них
потрібно — у розділі [«Що потрібно від інших модулів»](#що-потрібно-від-інших-модулів).

**Перевірено: 62 тести зелені**, зокрема 5 інтеграційних на живому PostgreSQL 16 + pgvector
(міграції `0001`–`0002` з `../db-schema`, тригери HITL, `audit_logs` і його хеш-ланцюг) та тест на
**справжньому Статуті й положеннях ПУЕТ**, розпізнаних зі сканів із папки проєкту. Qdrant у тестах
справжній (`AsyncQdrantClient(":memory:")`, той самий Query API), сервер ембедингів і LLM підмінено
детермінованими фейками.

## Архітектура

```mermaid
flowchart LR
    U[Автор / погоджувач] -- "Bearer JWT" --> API
    KC[Keycloak] -. JWKS .-> API
    subgraph API["FastAPI · sed_rag.api"]
        R1[POST /compliance/check]
        R2[POST /documents/versions/:id/compliance-checks]
        R3[GET /regulations/search]
        R4[POST /regulations · admin]
    end
    R1 & R2 --> G[ComplianceAuditor<br/>граф LangGraph]
    G --> PII[RegexPseudonymizer<br/>ПІБ → ОСОБА_n]
    G --> HR[HybridRetriever]
    HR -->|dense| EMB[[bge-m3 · /v1/embeddings<br/>vLLM / Ollama / TEI]]
    HR -->|dense + BM25,<br/>фільтр чинності| Q[(Qdrant<br/>sed_regulations)]
    HR -.->|необов'язково| RR[[bge-reranker-v2-m3<br/>TEI /rerank]]
    G -->|JSON Schema strict| LLM[[vLLM / Ollama<br/>локально]]
    R2 --> DB[(PostgreSQL<br/>ai_runs · compliance_*)]
    DB -. тригер на COMMIT .-> AU[(audit_logs<br/>verdict · violations)]
    R4 --> ING[CorpusIngestor] --> Q
    ING --> DBR[(regulatory_sources<br/>regulatory_chunks)]
    CLI[CLI: OCR сканів, manifest] --> ING
```

Clean Architecture, як в `ingest-pipeline`: `domain/` (акти, пункти, звіт), `corpus/` (очищення,
сегментація, BM25), `retrieval/`, `audit/` (граф, промпти, звірка цитат, скоринг) залежать лише від
портів `ports.py`; `adapters/` реалізують їх (Qdrant, ембединги, reranker, SQLAlchemy, пам'ять);
`api/` — HTTP і composition root. Той самий `ComplianceService` викликається з Arq/Celery-воркера.

### Граф перевірки

```mermaid
flowchart TD
    S([START]) --> P[prepare<br/>маскування ПД, розбір на пункти,<br/>посада підписанта]
    P --> REF[check_references<br/>посилання на скасовані / ще не чинні акти]
    REF --> RET[retrieve<br/>dense + BM25 → RRF → reranker<br/>для кожного пункту]
    RET -->|слабкий збіг,<br/>лише 1 раз| RW[rewrite_queries<br/>CRAG: запит мовою актів] --> RET
    RET --> AS[assess<br/>LLM: пункт проти норм N1…N6]
    AS -->|є зауваження| V[verify_findings<br/>незалежна повторна перевірка<br/>кожного зауваження]
    AS -->|немає| F
    V --> F[finalize<br/>звірка цитат · confidence · вердикт · звіт]
    F --> E([END])
```

1. **prepare.** ПД у тексті замінюються мітками `[ОСОБА_n]`, `[РНОКПП_n]` (той самий
   `RegexPseudonymizer`, що в інжесті). Наказ розбирається за ДСТУ 4163:2020: заголовок «Про …»,
   констатувальна частина, «НАКАЗУЮ:», пункти розпорядчої частини (підпункти 1.1, 1.2 — разом із
   пунктом; задовгий пункт ділиться), «Підстава:», підпис. Посада підписанта («Перший проректор»)
   іде в кожен промпт: від неї залежить перевищення повноважень. Документ без розпорядчої
   частини (службова записка, проєкт положення) перевіряється за абзацами.
2. **check_references** (без LLM). Посилання «Положення про …», «Закону України «…»», «наказу МОН
   від … № …» звіряються з каталогом корпусу (номер акта, назва). Акт скасований або ще не чинний
   на дату перевірки → зауваження `outdated_reference`. Акта немає в корпусі → попередження.
3. **retrieve.** Для кожного пункту окремо: dense-пошук і BM25 у Qdrant, **обидва з фільтром
   чинності на дату перевірки** (скасовані, ще не чинні акти й проєкти актів не потрапляють у
   кандидати), Reciprocal Rank Fusion, необов'язковий cross-encoder reranker, не більше 4
   фрагментів одного акта, 6 норм на пункт.
4. **rewrite_queries** (Corrective RAG). Якщо збіг слабкий, LLM переформульовує пункт мовою
   нормативних актів («порядок обрання завідувача кафедри, строк контракту»), пошук повторюється.
   Залишається кращий з двох результатів; цикл не повторюється.
5. **assess.** LLM отримує пункт, контекст (вид, заголовок, підписант, преамбула) і норми з
   мітками N1…N6 (назва акта, рівень, статус, строк дії, пункт). Вона повертає за JSON-схемою
   зауваження з категорією, серйозністю, дослівними цитатами з пункту й норми і самооцінкою, а
   також `no_issue_confidence`. Промпт задає ієрархію актів (закон > КМУ > МОН > Статут >
   положення) і вимагає використовувати лише надані норми.
6. **verify_findings.** Кожне зауваження перевіряє окремий виклик-«рецензент», якому дано лише
   пункт, одну норму і твердження: `confirmed`, `refuted` або `uncertain`.
7. **finalize.** Детермінований код звіряє цитати, рахує confidence, вирішує вердикт і пише
   резюме (без LLM, щоб резюме не вигадувало).

## Структура

| Шлях | Що це |
|---|---|
| `sed_rag/domain/models.py` | Акт (`ActMeta`, чинність на дату), пункт, фрагмент індексу, кандидат-норма, помилки домену |
| `sed_rag/domain/report.py` | `ComplianceReport` — відповідь API (зауваження, цитати, `violated_clauses`, покриття пунктів) |
| `sed_rag/corpus/segmentation.py` | Очищення OCR-тексту, поділ акта на пункти (розділи, статті/частини, 7.2.13, підзаголовки), відновлення номерів після OCR |
| `sed_rag/corpus/sparse.py` | BM25 зі стемінгом української (IDF рахує Qdrant) |
| `sed_rag/retrieval/hybrid.py` | Гібридний пошук: dense + BM25 → RRF → reranker, різноманіття джерел, сила збігу |
| `sed_rag/audit/draft.py` | Розбір проєкту наказу на пункти, посада підписанта |
| `sed_rag/audit/references.py` | Посилання проєкту на акти і їх чинність |
| `sed_rag/audit/graph.py` | Граф LangGraph і складання звіту |
| `sed_rag/audit/prompts.py`, `llm_schema.py` | Версіоновані промпти, схеми structured output |
| `sed_rag/audit/verification.py`, `scoring.py` | Звірка цитат, confidence |
| `sed_rag/adapters/*` | Qdrant, ембединги/reranker (httpx), SQLAlchemy (корпус і перевірки), реалізації в пам'яті |
| `sed_rag/ingestion/*` | Індексація акта, читання PDF/сканів (Tesseract), CLI |
| `sed_rag/api/*`, `service.py` | FastAPI, прикладний сервіс |
| `corpus/manifest.toml` | Що індексувати: 6 актів ПУЕТ (OCR) і 2 демонстраційні фрагменти (закон, МОН) |
| `corpus/ocr/*.ocr.txt` | OCR-текст сканів Статуту й положень із папки проєкту (не вичитаний) |
| `openapi.yaml` | Згенерована OpenAPI 3.1 специфікація |

## Контракт

```bash
# Перевірка тексту (чернетка в редакторі, згенерований проєкт). У БД нічого не пише.
curl -X POST https://sed.puet.local/api/v1/compliance/check -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"text": "…НАКАЗУЮ:\n1. …", "as_of": "2026-10-17"}'

# Перевірка збереженої версії документа: текст з document_versions.extracted_text,
# результат пишеться в ai_runs + compliance_* (+ audit_logs тригером), відповідь 201.
curl -X POST https://sed.puet.local/api/v1/documents/versions/$VERSION_ID/compliance-checks \
  -H "Authorization: Bearer $TOKEN" -H "X-Request-ID: 7f3c…"

# Пошук пунктів актів (для UI і налагодження)
curl "https://sed.puet.local/api/v1/regulations/search?q=строк+контракту+завідувача+кафедри&k=5" -H "Authorization: Bearer $TOKEN"
```

Відповідь (скорочено; корпус — справжній Статут ПУЕТ після OCR, проєкт наказу з тестів, висновки
LLM у цьому прикладі дала тестова заглушка):

```json
{
  "verdict": "non_compliant",
  "verdict_label": "Виявлено суперечності чинним нормам",
  "summary": "Перевірено 4 пункт(ів) проєкту за 24 фрагментами 7 акт(ів), чинних на 17.10.2026. Зауважень: 2 (критичне — 1, суттєве — 1). Можуть бути порушені: Статут ПУЕТ, п. 13.2; Статут ПУЕТ, п. 7.2.13.",
  "confidence": "0.8550", "confidence_threshold": "0.85", "requires_human_review": false,
  "violated_clauses": [
    {"citation_label": "Статут ПУЕТ, п. 13.2", "max_severity": "critical", "finding_ordinals": [1],
     "quoted_text": "Зміни до Статуту, запропоновані Вченою радою Університету, погоджуються вищим органом громадського самоврядування Університету Ta затверджуються Засновником Університету"},
    {"citation_label": "Статут ПУЕТ, п. 7.2.13", "max_severity": "major", "finding_ordinals": [2], "quoted_text": "…строком на п'ять років…"}
  ],
  "findings": [{
    "ordinal": 2, "severity": "major", "category": "contradiction",
    "title": "Строк і порядок обрання завідувача кафедри суперечать Статуту",
    "fragment": {"provision_label": "п. 1 розпорядчої частини", "text": "строком на 7 років без проведення конкурсу",
                 "char_start": 328, "char_end": 370},
    "confidence": "0.9300", "requires_human_review": false, "issues": [],
    "verifier": {"verdict": "confirmed", "confidence": 0.9, "reason": "…"},
    "citations": [{"rank": 1, "chunk_id": "…", "citation_label": "Статут ПУЕТ, п. 7.2.13", "source_status": "in_force",
                   "quoted_text": "…", "quote_verified": true,
                   "dense_score": 0.41, "sparse_score": 12.9, "fused_score": 1.0, "rerank_score": null}]
  }],
  "provisions": [{"label": "п. 1 розпорядчої частини", "coverage": "strong",
                  "norms_considered": ["Статут ПУЕТ, п. 7.2.13", "Статут ПУЕТ, п. 7.2.12", "…"], "assessed": true}],
  "disclaimer": "Звіт сформовано ШІ-асистентом і він не має юридичної сили. …",
  "ai_run_id": "…", "check_id": "…"
}
```

Вердикти: `compliant` (зауважень немає і всі пункти оцінено), `issues_found` (є зауваження, але
жодне суттєве чи критичне не досягає 0.85), `non_compliant` (є суттєве/критичне з впевненістю
≥ 0.85), `inconclusive` (LLM недоступна, корпус порожній, пункти не оцінено). Помилки:
`401`, `403`, `404`, `422`, `503`, тіло `{"detail": "…українською…", "code": "…"}`.

## Як рахується confidence і Human-in-the-loop

Самооцінка моделі — лише верхня межа (≤ 0.95). Для кожного зауваження:

```
conf = min(llm, 0.95) × цитата_норми × цитата_проєкту × верифікатор × позиція_в_пошуку × джерело
```

| Сигнал | Як перевіряється | Вплив |
|---|---|---|
| цитата норми | є дослівно у фрагменті (з точністю до регістру, лапок, тире, пробілів, «…») / ≥ 80 % слів в одному реченні / 40–80 % / менше | 1.0 / 0.9 / 0.7 / 0.4 («можлива галюцинація») |
| цитата проєкту | знайдено в тексті проєкту (після відновлення ПД) | 1.0 / 0.75 |
| верифікатор | confirmed / uncertain / refuted / не відповів | 1.0 (0.9 при його conf < 0.8) / 0.75 / 0.45 / 0.9 |
| позиція норми | у топ-3 пошуку для пункту | 1.0 / 0.95 |
| джерело | акт діє зі змінами; OCR-текст із якістю q < 0.9; демонстраційний текст | ×0.95; ×(0.7 + 0.3q); ×0.9 |

* `quote_verified = true` означає одне: `quoted_text` буквально міститься у фрагменті корпусу.
  У БД і звіт завжди йде текст самої норми (дослівний збіг або найближче речення пункту), а
  розбіжність із цитатою моделі знижує confidence і пояснюється в `issues`.
* Посилання на неіснуючу норму (`N9` при шести нормах) відкидаються з попередженням.
* «Суперечностей немає» по пункту: `min(no_issue, 0.95) × покриття` (сильне 1.0, слабке 0.8,
  немає 0.5). Тож висновок «усе гаразд» на слабкому пошуку завжди йде на ручну перевірку.
* Впевненість перевірки — **мінімум** по зауваженнях і пунктах; `requires_human_review`, якщо
  вона < 0.85 або є будь-яка причина в `review_reasons`.
* Поріг береться з `sed_db.base.CONFIDENCE_THRESHOLD`. У БД `compliance_findings.requires_human_review`
  — згенерований стовпець, а guard-тригер не дає ролі ШІ закрити зауваження чи підтвердити
  власну перевірку (перевірено тестом).

## Що пишеться в БД (перевірка версії, одна транзакція)

| Таблиця | Запис |
|---|---|
| `ai_runs` | `task_type = compliance_check`, `pii_masked = true`, `input_sha256` усіх промптів (сирий вхід не зберігається), `output` = відповіді моделі **з мітками ПД**, токени, затримка, `prompt_template_id` = `compliance.assess_provision` v1, у `parameters` — версії всіх трьох промптів і конфіг пошуку; `pending`, якщо потрібна ручна валідація |
| `compliance_checks` | вердикт, резюме, `regulations_as_of` (дата документа або сьогодні), `retrieval_config`, `sources_considered` |
| `compliance_findings` | серйозність, категорія, заголовок (з мітками ПД: він копіюється в `audit_logs`), пояснення, рекомендація, фрагмент проєкту і `char_start`/`char_end`, confidence |
| `compliance_citations` | `chunk_id` (= point id у Qdrant), дослівна цитата, `quote_verified`, статус акта на момент перевірки, `dense/sparse/fused/rerank_score` |
| `prompt_templates`, `ai_models` | реєструються при першому використанні; змінений текст промпту без нової версії → помилка |
| `audit_logs` | пише тригер на COMMIT: вердикт, confidence, `violations` з цитатами (акт, пункт, цитата) |

Дані проходять DTO `sed_db.schemas` (`AiRunCreate`, `AiRunResult`, `ComplianceCheckIn`), тож
правила «хоча б одна перевірена цитата на зауваження» і «вердикт ↔ серйозність» перевіряються до
БД. Актор транзакції — `ai_service`.

## Нормативний корпус

**Що проіндексовано зараз** (`corpus/manifest.toml`):

| Акт | Звідки | Фрагментів |
|---|---|---|
| Статут ПУЕТ (нова редакція, рішення засновника від 05.08.2025) | `statut_puet (2).pdf`, OCR, якість 0.92 | 462 |
| Положення про кафедру (2025) | `polozh-kaf.pdf`, OCR 3 стор. + текстовий шар | 51 |
| Положення про ректорат (2023) | `polozhennya-pro-rektorat.pdf`, OCR | 64 |
| Положення про Вчену раду (2023) | `polozhennya_pro_vchenu_radu.pdf`, OCR | 52 |
| Положення про науково-методичну раду (2025) | `polozh_pro_nm_radu.pdf` | 29 |
| Положення про відділ кадрів (2024) | `polozhennya-pro-viddil-kadriv.pdf`, OCR (текстовий шар битий) | 22 |
| ЗУ «Про вищу освіту», МОН — **демонстраційні фрагменти** | `corpus/samples/*.SAMPLE.txt`, `is_sample = true` | 5 + 3 |

Скани ПУЕТ мають «фальшивий» текстовий шар (кирилиця латинськими двійниками: «3MiCT» замість
«ЗМІСТ»), тому індексатор перевіряє частку кирилиці і розпізнає такі сторінки Tesseract `ukr+eng`.
Офіційних текстів законів і наказів МОН у проєкті немає: два файли в `corpus/samples/` —
навчальні фрагменти, їх цитати у звіті позначаються `is_sample` і мають нижчий confidence.

**Додати чи оновити акт:**

```bash
export PYTHONPATH=.:../db-schema:../ingest-pipeline
python -m sed_rag.ingestion.cli extract-text "../polozh-orgu-osv-proc.pdf" -o corpus/ocr/polozh-orgu-osv-proc.ocr.txt
# вичитати текст → додати [[act]] у manifest.toml (file, title, level, issuer, дати, text_quality)
python -m sed_rag.ingestion.cli ingest-manifest corpus/manifest.toml
python -m sed_rag.ingestion.cli search "хто затверджує зміни до Статуту"
```

Або через API: `POST /api/v1/regulations` (realm-роль `sed-regulations-admin`) і
`PUT /api/v1/regulations/{source_id}/status`, коли акт скасовано.

Гарантії індексації: SHA-256 нормалізованого тексту → повторна індексація того самого тексту
нічого не робить; змінений текст → нова `index_revision`, старі фрагменти `is_current = false`
(не видаляються: на них посилаються цитати історичних перевірок). Ембединги рахуються до
транзакції; точки в Qdrant пишуться всередині транзакції БД, тож збій Qdrant відкочує БД, а
збій COMMIT видаляє щойно додані точки. `cli reconcile` звіряє Qdrant з `regulatory_chunks`.

## Запуск

```bash
pip install -r requirements.txt            # + apt install tesseract-ocr tesseract-ocr-ukr для сканів
export PYTHONPATH=.:../db-schema:../ingest-pipeline
export SED_RAG_DATABASE_URL=postgresql+psycopg://sed_ai_worker:***@db/sed
export SED_RAG_QDRANT_URL=http://qdrant:6333
export SED_RAG_EMBEDDING_BASE_URL=http://embeddings:8000/v1 SED_RAG_EMBEDDING_MODEL=BAAI/bge-m3
export SED_RAG_LLM_BASE_URL=http://vllm:8000/v1 SED_RAG_LLM_MODEL=mistral-small-3.1-24b-instruct-uk
export SED_RAG_RERANKER_URL=http://reranker:8080          # необов'язково
uvicorn sed_rag.api.app:create_app --factory --host 0.0.0.0 --port 8081

# Ембединги:  vllm serve BAAI/bge-m3 --task embed   |  Ollama: SED_RAG_EMBEDDING_MODEL=bge-m3
# Reranker:   text-embeddings-inference --model-id BAAI/bge-reranker-v2-m3
# Qdrant:     docker run -p 6333:6333 -v qdrant:/qdrant/storage qdrant/qdrant:v1.12.4

# тести (інтеграційні з БД — лише якщо задано URL; БД перестворюється)
export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:***@localhost/sed_rag_test
PYTHONDONTWRITEBYTECODE=1 pytest -q
```

Усі налаштування — у `sed_rag/config.py` (префікс `SED_RAG_`, параметри пошуку — `SED_RAG_RETRIEVAL_*`):
k для dense/BM25, RRF, кількість норм на пункт, пороги сили збігу, паралельність викликів LLM,
ліміти довжини проєкту і пунктів.

## Граничні випадки і ризики

| Ситуація | Як закрито |
|---|---|
| ПД у LLM | Псевдонімізація до виклику, `pii_masked = true`; у `ai_runs.output` і `audit_logs` лише мітки; заголовки зауважень зберігаються маскованими; сирий вхід — тільки SHA-256 |
| Пропущені розпізнавачем ПД | Залишковий ризик, як в інжесті; LLM локальна. Заміна `PiiMasker` на Presidio + spaCy не змінює граф |
| Prompt injection у проєкті чи акті | Тексти лише між маркерами, маркери в даних екрануються (перевірено тестом), промпт називає їх даними, вихід обмежено JSON-схемою, моделі не дано інструментів, ШІ не змінює статус документа |
| Галюцинована норма або цитата | Мітка поза списком — зауваження відкидається; цитата звіряється з фрагментом, у звіт іде лише реальний текст норми; confidence падає |
| Хибне спрацювання | Незалежний верифікатор; `refuted` → ×0.45 і ручна валідація; непідтверджене зауваження не робить вердикт `non_compliant` |
| «Усе гаразд», бо нічого не знайдено | Слабкий/порожній пошук знижує впевненість «немає суперечностей»; порожній корпус → `inconclusive` |
| Скасований або ще не чинний акт | Фільтр чинності в Qdrant на дату перевірки (за замовчуванням — дата реєстрації документа); посилання проєкту на такий акт → `outdated_reference` |
| Перевірка «заднім числом» | `as_of` у запиті: акти оцінюються чинними на ту дату |
| Зміна тексту акта | Нова ревізія; цитати старих перевірок і далі вказують на свій фрагмент (`source_status_snapshot`) |
| Помилки OCR в акті | Перевірка частки кирилиці, відновлення номерів «72.14» → «7.2.14», колонтитули й номери сторінок прибираються; `text_quality` знижує confidence і дає примітку «звірте з оригіналом». Рекомендовано вичитати Статут |
| LLM недоступна / невалідний JSON | Повтори транспорту (адаптер інжесту), один «ремонт» за помилками валідації; пункт без оцінки → `inconclusive`, детерміновані перевірки посилань усе одно в звіті |
| Qdrant або сервер ембедингів недоступні | `503` з поясненням; індексація не лишає напівзаписаної ревізії |
| Розійшлися Qdrant і БД | Перед записом перевіряється, що всі `chunk_id` є в `regulatory_chunks`, інакше помилка з порадою `cli reconcile` |
| Довгий проєкт | Ліміт 60 000 символів і 40 пунктів; задовгий пункт ділиться на частини |
| Чужий документ | `403`: автор, працівник підрозділу, роль у підрозділі або ректор/проректор |
| Зміна корпусу | Лише realm-роль `sed-regulations-admin`; дія пишеться в `audit_logs` тригером з актором |

## Що потрібно від інших модулів

**Схема БД** (тред «Схема БД для СЕД», файли не змінено):

1. `regulatory_sources`: стовпці `short_title` (як підписувати цитату: «Статут ПУЕТ») і `is_sample`
   (або `metadata jsonb`), та унікальний ключ `(level, issuer, title, doc_number)` проти дублів
   при паралельній індексації. Зараз ці поля живуть лише в payload Qdrant.
2. `regulatory_chunks`: `heading_path text[]` і `text_quality numeric(5,4)` (зараз лише в Qdrant).
   Стовпець `embedding` при Qdrant лишається NULL; його можна прибрати (рішення 8 у README схеми)
   або залишити як запасний pgvector-шлях.
3. Seed-міграція `0003` для промптів `compliance.assess_provision`, `compliance.verify_finding`,
   `compliance.rewrite_query` v1 (зараз реєструються при першому використанні з перевіркою SHA-256).
4. `ai_models` унікальний за `(name, version)`, а `task_type` — один: та сама LLM для NER і
   комплаєнсу отримує `task_type` першої задачі. Варто прибрати `task_type` з `ai_models` або
   включити його в ключ.
5. `roles.sql`, роль `sed_ai_worker`: потрібні `SELECT` на `users`; `INSERT` на `ai_models` і
   `prompt_templates` (або seed, п. 3). Для індексації корпусу — окрема роль (напр.
   `sed_corpus_admin`) з `INSERT, UPDATE` на `regulatory_sources` і `UPDATE (is_current)` на
   `regulatory_chunks`.

**Інжест** (тред «Пайплайн обробки вхідних файлів»): обов'язкового нічого. Модуль імпортує
`OpenAICompatibleLlm`, `RegexPseudonymizer`, `KeycloakJwtVerifier` і `LlmCompletion`/`LlmError`;
зміна їх сигнатур зламає цей модуль, тож їх варто винести в спільний пакет. Опційно: після
інжесту проєкту наказу ставити в чергу `ComplianceService.check_version`.
