-- Ролі БД СЕД ПУЕТ (виконує DBA один раз на кластер, поза Alembic).
-- Принцип найменших привілеїв: AI-воркер не може змінювати юридично значущі дані
-- навіть у разі компрометації сервісу або підробки змінної sed.actor_type.

CREATE ROLE sed_owner NOLOGIN;          -- власник схеми, від нього йдуть міграції
CREATE ROLE sed_app LOGIN;              -- core API (FastAPI)
CREATE ROLE sed_ai_worker LOGIN;        -- Celery/Arq воркери ШІ-пайплайнів
CREATE ROLE sed_auditor LOGIN;          -- внутрішній аудит: лише читання

GRANT USAGE ON SCHEMA public TO sed_app, sed_ai_worker, sed_auditor;

-- Core API: CRUD без прямого доступу на запис у журнал аудиту.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO sed_app;
GRANT DELETE ON document_links, route_template_steps, version_attachments TO sed_app;
-- Журнал і історія статусів пишуться лише тригерами (SECURITY DEFINER).
REVOKE INSERT, UPDATE ON audit_logs, document_status_history, approval_step_history FROM sed_app;
GRANT EXECUTE ON FUNCTION sed_register_document(uuid, uuid, date) TO sed_app;

-- AI-воркер: читає документи й корпус, пише лише у свої таблиці.
GRANT SELECT ON documents, document_versions, document_types, version_attachments,
               regulatory_sources, regulatory_chunks, ai_models, prompt_templates,
               route_templates, route_template_steps, org_units, staff_assignments, user_roles,
               approval_routes, approval_steps
      TO sed_ai_worker;
GRANT SELECT, INSERT ON ai_runs, ai_extracted_fields, compliance_checks,
                        compliance_findings, compliance_citations, regulatory_chunks
      TO sed_ai_worker;
GRANT UPDATE (status, output, confidence, tokens_in, tokens_out, latency_ms, error_message,
              started_at, finished_at, pii_masked, pii_entities_masked, human_review_status,
              input_sha256)
      ON ai_runs TO sed_ai_worker;     -- human_review_status лише 'pending' (guard-тригер)
GRANT UPDATE (extracted_text) ON document_versions TO sed_ai_worker;  -- текстовий шар OCR
GRANT UPDATE (status) ON documents TO sed_ai_worker;  -- лише draft <-> ai_review (guard-тригер)
-- Журнал: рядкові зміни пишуть тригери (SECURITY DEFINER), бізнес-події — ця функція.
GRANT EXECUTE ON FUNCTION sed_audit_event(text, text, uuid, uuid, jsonb) TO sed_app, sed_ai_worker;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO sed_auditor;
GRANT EXECUTE ON FUNCTION sed_audit_verify(bigint) TO sed_auditor;

-- Персональні дані: аудитору не потрібен текст документів.
REVOKE SELECT ON document_versions FROM sed_auditor;
GRANT SELECT (id, document_id, version_number, content_sha256, created_by_id, created_at, is_locked, locked_at)
      ON document_versions TO sed_auditor;
