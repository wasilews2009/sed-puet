"""Серверні інваріанти PostgreSQL, які не виражаються декларативно в SQLAlchemy:
розширення, тригери незмінності, guard «ШІ не приймає юридичних рішень»,
хеш-ланцюжок журналу аудиту та атомарна реєстрація документа.

Використовується міграцією Alembic (`op.execute`) і тестами.

Контекст виконавця передається застосунком на початку кожної транзакції:

    SET LOCAL sed.actor_user_id = '<uuid>';
    SET LOCAL sed.actor_type    = 'user' | 'system' | 'ai_service';
    SET LOCAL sed.request_id    = '<X-Request-ID>';

Для AI-воркерів рекомендовано окрему роль БД `sed_ai_worker` (див. roles.sql):
guard перевіряє і роль, і `sed.actor_type`, тож підробка змінної не допоможе.
"""

from __future__ import annotations

EXTENSIONS = ("pgcrypto", "pg_trgm", "btree_gist", "vector")

# Таблиці, зміни яких автоматично потрапляють у audit_logs.
AUDITED_TABLES = (
    "documents",
    "document_versions",
    "document_links",
    "approval_routes",
    "approval_steps",
    "signatures",
    "delegations",
    "route_templates",
    "route_template_steps",
    "staff_assignments",
    "user_roles",
    "ai_runs",
    "ai_extracted_fields",
    "compliance_checks",
    "compliance_findings",
)

# Таблиці «тільки додавання»: таблиця → стовпці, які ще можна змінювати.
APPEND_ONLY = {
    "signatures": ("validation_status", "validation_report", "validated_at"),
    "prompt_templates": (),
    "regulatory_chunks": ("is_current", "content_tsv"),
    "compliance_citations": (),
    "compliance_checks": (),
    "document_status_history": (),
    "approval_step_history": (),
    "audit_logs": (),
}

FUNCTIONS_SQL: list[str] = [
    # ------------------------------------------------------------------ helpers
    r"""
CREATE OR REPLACE FUNCTION sed_is_ai_actor() RETURNS boolean
LANGUAGE sql STABLE AS $$
    -- Пряме членство замість pg_has_role(): суперкористувач «член» будь-якої ролі.
    SELECT coalesce(current_setting('sed.actor_type', true), '') = 'ai_service'
        OR current_user = 'sed_ai_worker'
        OR EXISTS (SELECT 1
                     FROM pg_auth_members m
                     JOIN pg_roles r ON r.oid = m.roleid
                     JOIN pg_roles u ON u.oid = m.member
                    WHERE r.rolname = 'sed_ai_worker' AND u.rolname = current_user)
$$;
""",
    # ------------------------------------------------------ append-only tables
    r"""
CREATE OR REPLACE FUNCTION sed_immutable_except() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    mutable text[] := coalesce(TG_ARGV, '{}'::text[]);
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'table % is append-only: TRUNCATE is forbidden', TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'table % is append-only: DELETE is forbidden', TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - mutable) IS DISTINCT FROM (to_jsonb(OLD) - mutable) THEN
        RAISE EXCEPTION 'table % is append-only: only % may change', TG_TABLE_NAME, mutable
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
""",
    # ---------------------------------------------- document version immutability
    r"""
CREATE OR REPLACE FUNCTION sed_protect_document_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.is_locked THEN
            RAISE EXCEPTION 'document version % is locked and cannot be deleted', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF (NEW.document_id, NEW.version_number, NEW.parent_version_id, NEW.source,
        NEW.storage_bucket, NEW.storage_key, NEW.storage_version_id, NEW.file_name,
        NEW.mime_type, NEW.size_bytes, NEW.content_sha256, NEW.created_by_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.document_id, OLD.version_number, OLD.parent_version_id, OLD.source,
        OLD.storage_bucket, OLD.storage_key, OLD.storage_version_id, OLD.file_name,
        OLD.mime_type, OLD.size_bytes, OLD.content_sha256, OLD.created_by_id, OLD.created_at)
    THEN
        RAISE EXCEPTION 'content of document version % is immutable; create a new version', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.is_locked AND NOT NEW.is_locked THEN
        RAISE EXCEPTION 'document version % cannot be unlocked', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
""",
    # ---------------------------------------------------- KEP signature checks
    r"""
CREATE OR REPLACE FUNCTION sed_signature_before_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_hash  text;
    v_step  record;
BEGIN
    IF sed_is_ai_actor() THEN
        RAISE EXCEPTION 'AI service cannot create signatures' USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT content_sha256 INTO v_hash
      FROM document_versions WHERE id = NEW.document_version_id FOR UPDATE;
    IF v_hash IS DISTINCT FROM NEW.signed_content_sha256 THEN
        RAISE EXCEPTION 'signed hash % does not match document version hash %',
            NEW.signed_content_sha256, v_hash USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.approval_step_id IS NOT NULL THEN
        SELECT s.assignee_id, s.status, s.requires_kep, r.document_version_id
          INTO v_step
          FROM approval_steps s JOIN approval_routes r ON r.id = s.route_id
         WHERE s.id = NEW.approval_step_id;
        IF v_step.assignee_id IS DISTINCT FROM NEW.signer_id THEN
            RAISE EXCEPTION 'signer is not the assignee of approval step %', NEW.approval_step_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_step.document_version_id IS DISTINCT FROM NEW.document_version_id THEN
            RAISE EXCEPTION 'approval step % belongs to a different document version', NEW.approval_step_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF v_step.status <> 'active' THEN
            RAISE EXCEPTION 'approval step % is not active (status %)', NEW.approval_step_id, v_step.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
""",
    r"""
CREATE OR REPLACE FUNCTION sed_signature_after_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE document_versions
       SET is_locked = true, locked_at = coalesce(locked_at, now())
     WHERE id = NEW.document_version_id AND NOT is_locked;
    RETURN NULL;
END $$;
""",
    # ---------------------------------------- route activation locks the version
    r"""
CREATE OR REPLACE FUNCTION sed_route_lock_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'active' THEN
        UPDATE document_versions
           SET is_locked = true, locked_at = coalesce(locked_at, now())
         WHERE id = NEW.document_version_id AND NOT is_locked;
    END IF;
    RETURN NULL;
END $$;
""",
    # ---------------------- Human-in-the-loop guard: AI never makes legal decisions
    r"""
CREATE OR REPLACE FUNCTION sed_guard_legal_decisions() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_requires_kep boolean;
BEGIN
    IF TG_TABLE_NAME = 'documents' THEN
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            -- ШІ може лише відправити проєкт на автоматичну перевірку і повернути з неї.
            IF sed_is_ai_actor() AND NOT (NEW.status IN ('draft','ai_review')
                                          AND OLD.status IN ('draft','ai_review')) THEN
                RAISE EXCEPTION 'AI service cannot change document status % -> %', OLD.status, NEW.status
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            -- Юридична сила: «підписано» лише за наявності валідного КЕП на поточній версії.
            IF NEW.status = 'signed' THEN
                SELECT requires_kep INTO v_requires_kep
                  FROM document_types WHERE id = NEW.document_type_id;
                IF v_requires_kep AND NOT EXISTS (
                    SELECT 1 FROM signatures s
                     WHERE s.document_version_id = NEW.current_version_id
                       AND s.validation_status = 'valid') THEN
                    RAISE EXCEPTION 'document % cannot become signed without a valid KEP on its current version', NEW.id
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
        END IF;
        -- Після підписання не можна підмінити поточну версію.
        IF NEW.current_version_id IS DISTINCT FROM OLD.current_version_id
           AND OLD.status IN ('signed','registered','in_execution','executed','archived') THEN
            RAISE EXCEPTION 'current version of signed document % is frozen', NEW.id
                USING ERRCODE = 'restrict_violation';
        END IF;

    ELSIF TG_TABLE_NAME = 'approval_steps' THEN
        IF NEW.status IS DISTINCT FROM OLD.status AND sed_is_ai_actor()
           AND NEW.status IN ('approved','approved_with_remarks','rejected','skipped','delegated') THEN
            RAISE EXCEPTION 'AI service cannot decide approval steps' USING ERRCODE = 'insufficient_privilege';
        END IF;

    ELSIF TG_TABLE_NAME IN ('ai_runs', 'ai_extracted_fields') THEN
        IF NEW.human_review_status IS DISTINCT FROM OLD.human_review_status AND sed_is_ai_actor()
           AND NEW.human_review_status IN ('confirmed','corrected','rejected') THEN
            RAISE EXCEPTION 'AI service cannot perform human review' USING ERRCODE = 'insufficient_privilege';
        END IF;

    ELSIF TG_TABLE_NAME = 'compliance_findings' THEN
        IF NEW.status IS DISTINCT FROM OLD.status AND sed_is_ai_actor() THEN
            RAISE EXCEPTION 'AI service cannot resolve compliance findings' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN NEW;
END $$;
""",
    # ------------------------------------------------------- audit hash chain
    r"""
CREATE OR REPLACE FUNCTION sed_audit_hash(r audit_logs, p_prev bytea) RETURNS bytea
LANGUAGE sql IMMUTABLE AS $$
    SELECT digest(
        coalesce(p_prev, '\x'::bytea) || convert_to(concat_ws('|',
            r.chain_seq,
            (extract(epoch FROM r.occurred_at) * 1000000)::bigint,
            r.category::text,
            r.actor_type::text,
            coalesce(r.actor_user_id::text, ''),
            r.action,
            r.entity_table,
            coalesce(r.entity_id::text, ''),
            coalesce(r.document_id::text, ''),
            coalesce(r.document_version_id::text, ''),
            r.changes::text,
            -- результати ШІ теж під хешем: підробити confidence чи порушення непомітно не вийде
            CASE WHEN r.ai_run_id IS NULL THEN '' ELSE jsonb_build_object(
                'run', r.ai_run_id, 'task', r.ai_task_type, 'model', r.ai_model,
                'confidence', r.confidence_score, 'review', r.requires_human_review,
                'verdict', r.ai_verdict, 'violations', r.violations,
                'metadata', r.extracted_metadata)::text END), 'UTF8'),
        'sha256')
$$;
""",
    r"""
CREATE OR REPLACE FUNCTION sed_audit_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_seq  bigint;
    v_hash bytea;
BEGIN
    -- Серіалізуємо записи ланцюга; lock знімається на COMMIT/ROLLBACK.
    PERFORM pg_advisory_xact_lock(hashtext('sed.audit_logs.chain'));
    SELECT chain_seq, record_hash INTO v_seq, v_hash
      FROM audit_logs ORDER BY chain_seq DESC LIMIT 1;

    NEW.occurred_at := clock_timestamp();   -- не довіряємо часу від клієнта
    NEW.chain_seq   := coalesce(v_seq, 0) + 1;
    NEW.prev_hash   := v_hash;
    NEW.record_hash := sed_audit_hash(NEW, v_hash);
    RETURN NEW;
END $$;
""",
    r"""
CREATE OR REPLACE FUNCTION sed_audit_verify(p_from_seq bigint DEFAULT 1)
RETURNS TABLE (broken_seq bigint, reason text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r        audit_logs%ROWTYPE;
    v_prev   bytea;
    v_expect bigint := p_from_seq;
BEGIN
    SELECT record_hash INTO v_prev FROM audit_logs WHERE chain_seq = p_from_seq - 1;
    FOR r IN SELECT * FROM audit_logs WHERE chain_seq >= p_from_seq ORDER BY chain_seq LOOP
        IF r.chain_seq <> v_expect THEN
            broken_seq := v_expect; reason := 'missing record'; RETURN NEXT; RETURN;
        END IF;
        IF r.prev_hash IS DISTINCT FROM v_prev THEN
            broken_seq := r.chain_seq; reason := 'prev_hash mismatch'; RETURN NEXT; RETURN;
        END IF;
        IF r.record_hash <> sed_audit_hash(r, r.prev_hash) THEN
            broken_seq := r.chain_seq; reason := 'record_hash mismatch'; RETURN NEXT; RETURN;
        END IF;
        v_prev := r.record_hash;
        v_expect := v_expect + 1;
    END LOOP;
END $$;
""",
    # ------------------------------------------------------ generic row audit
    r"""
CREATE OR REPLACE FUNCTION sed_audit_row_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    -- Великі/похідні поля не дублюємо в журнал.
    v_skip   text[] := ARRAY['extracted_text','text_tsv','search_tsv','content_tsv','embedding',
                             'output','validation_report','updated_at','row_version'];
    v_old    jsonb;
    v_new    jsonb;
    v_diff   jsonb;
    v_row    jsonb;
    v_actor  uuid  := nullif(current_setting('sed.actor_user_id', true), '')::uuid;
    v_kind   text  := nullif(current_setting('sed.actor_type', true), '');
    v_doc    uuid;
    v_ver    uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD) - v_skip; END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW) - v_skip; END IF;

    IF TG_OP = 'UPDATE' THEN
        SELECT coalesce(jsonb_object_agg(n.key, jsonb_build_object('old', v_old -> n.key, 'new', n.value)), '{}')
          INTO v_diff
          FROM jsonb_each(v_new) AS n
         WHERE n.value IS DISTINCT FROM v_old -> n.key;
        IF v_diff = '{}'::jsonb THEN RETURN NULL; END IF;
    ELSIF TG_OP = 'INSERT' THEN
        v_diff := jsonb_build_object('new', v_new);
    ELSE
        v_diff := jsonb_build_object('old', v_old);
    END IF;

    v_row := coalesce(v_new, v_old);
    v_ver := CASE TG_TABLE_NAME
        WHEN 'document_versions' THEN (v_row ->> 'id')::uuid
        WHEN 'compliance_findings' THEN
            (SELECT document_version_id FROM compliance_checks WHERE id = (v_row ->> 'check_id')::uuid)
        ELSE (v_row ->> 'document_version_id')::uuid
    END;
    v_doc := CASE TG_TABLE_NAME
        WHEN 'documents' THEN (v_row ->> 'id')::uuid
        WHEN 'document_links' THEN (v_row ->> 'source_document_id')::uuid
        WHEN 'approval_steps' THEN
            (SELECT document_id FROM approval_routes WHERE id = (v_row ->> 'route_id')::uuid)
        ELSE coalesce((v_row ->> 'document_id')::uuid,
            (SELECT document_id FROM document_versions WHERE id = v_ver))
    END;

    INSERT INTO audit_logs (category, actor_type, actor_user_id, action, entity_table, entity_id,
                            document_id, document_version_id, changes, request_id)
    VALUES ('data_change',
            coalesce(v_kind, CASE WHEN v_actor IS NULL THEN 'system' ELSE 'user' END)::audit_actor_type,
            v_actor, lower(TG_OP), TG_TABLE_NAME, (v_row ->> 'id')::uuid, v_doc, v_ver, v_diff,
            nullif(current_setting('sed.request_id', true), ''));
    RETURN NULL;
END $$;
""",
    # ------------------------------------- AI analysis results -> audit_logs
    r"""
CREATE OR REPLACE FUNCTION sed_log_ai_analysis() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    r            ai_runs%ROWTYPE;
    v_model      text;
    v_check_id   uuid;
    v_verdict    text;
    v_violations jsonb := '[]'::jsonb;
    v_metadata   jsonb := '{}'::jsonb;
    v_schema     jsonb;
    v_doc        uuid;
BEGIN
    -- Відкладений тригер: читаємо актуальний стан запуску на момент COMMIT.
    SELECT * INTO r FROM ai_runs WHERE id = NEW.id;
    IF NOT FOUND OR r.status <> 'succeeded'
       OR EXISTS (SELECT 1 FROM audit_logs WHERE ai_run_id = r.id) THEN
        RETURN NULL;
    END IF;

    SELECT name || ':' || version INTO v_model FROM ai_models WHERE id = r.model_id;

    SELECT c.id, c.verdict::text INTO v_check_id, v_verdict
      FROM compliance_checks c WHERE c.ai_run_id = r.id;
    IF v_check_id IS NOT NULL THEN
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'finding_id', f.id,
                   'severity', f.severity,
                   'category', f.category,
                   'title', f.title,
                   'confidence', f.confidence,
                   'requires_human_review', f.requires_human_review,
                   'char_start', f.char_start,
                   'char_end', f.char_end,
                   'citations', coalesce((
                       SELECT jsonb_agg(jsonb_build_object(
                                  'source', s.title, 'doc_number', s.doc_number,
                                  'section', ch.section_ref, 'quote', c.quoted_text,
                                  'quote_verified', c.quote_verified) ORDER BY c.rank)
                         FROM compliance_citations c
                         JOIN regulatory_chunks ch ON ch.id = c.chunk_id
                         JOIN regulatory_sources s ON s.id = ch.source_id
                        WHERE c.finding_id = f.id), '[]'::jsonb)
               ) ORDER BY f.ordinal), '[]'::jsonb)
          INTO v_violations
          FROM compliance_findings f WHERE f.check_id = v_check_id;
    END IF;

    SELECT d.id, t.requisites_schema INTO v_doc, v_schema
      FROM document_versions v
      JOIN documents d ON d.id = v.document_id
      JOIN document_types t ON t.id = d.document_type_id
     WHERE v.id = r.document_version_id;

    -- Реквізити з "x-pii": true у схемі виду документа — лише замасковано.
    SELECT coalesce(jsonb_object_agg(e.field_key,
               CASE WHEN coalesce(v_schema #>> ARRAY['properties', e.field_key, 'x-pii'], 'false') = 'true'
                    THEN jsonb_build_object('masked', true, 'field_id', e.id,
                                            'confidence', e.confidence,
                                            'requires_human_review', e.requires_human_review)
                    ELSE jsonb_build_object('value', e.extracted_value,
                                            'confidence', e.confidence,
                                            'requires_human_review', e.requires_human_review)
               END), '{}'::jsonb)
      INTO v_metadata
      FROM ai_extracted_fields e WHERE e.ai_run_id = r.id;

    INSERT INTO audit_logs (category, actor_type, actor_user_id, action, entity_table, entity_id,
                            document_id, document_version_id, changes,
                            ai_run_id, ai_task_type, ai_model, confidence_score,
                            requires_human_review, ai_verdict, violations, extracted_metadata,
                            request_id)
    VALUES ('ai_analysis', 'ai_service', NULL, 'ai.' || r.task_type::text, 'ai_runs', r.id,
            coalesce(r.document_id, v_doc), r.document_version_id,
            jsonb_build_object('initiated_by', r.initiated_by_id,
                               'human_review_status', r.human_review_status,
                               'pii_masked', r.pii_masked,
                               'prompt_template_id', r.prompt_template_id),
            r.id, r.task_type, v_model, r.confidence, r.requires_human_review,
            coalesce(v_verdict, r.output ->> 'label'), v_violations, v_metadata,
            nullif(current_setting('sed.request_id', true), ''));
    RETURN NULL;
END $$;
""",
    # ------------------------------------------- approval status history
    r"""
CREATE OR REPLACE FUNCTION sed_document_status_history() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_actor uuid := nullif(current_setting('sed.actor_user_id', true), '')::uuid;
    v_kind  text := nullif(current_setting('sed.actor_type', true), '');
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    INSERT INTO document_status_history (document_id, from_status, to_status, document_version_id,
                                         route_id, actor_type, changed_by_id, reason)
    VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status, NEW.current_version_id,
            (SELECT id FROM approval_routes WHERE document_id = NEW.id ORDER BY created_at DESC LIMIT 1),
            coalesce(v_kind, CASE WHEN v_actor IS NULL THEN 'system' ELSE 'user' END)::audit_actor_type,
            v_actor, nullif(current_setting('sed.change_reason', true), ''));
    RETURN NULL;
END $$;
""",
    r"""
CREATE OR REPLACE FUNCTION sed_approval_step_history() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_actor uuid := nullif(current_setting('sed.actor_user_id', true), '')::uuid;
    v_kind  text := nullif(current_setting('sed.actor_type', true), '');
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NULL;
    END IF;
    INSERT INTO approval_step_history (step_id, route_id, document_id, from_status, to_status,
                                       assignee_id, acting_role, actor_type, changed_by_id, comment)
    SELECT NEW.id, NEW.route_id, r.document_id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END,
           NEW.status, NEW.assignee_id, NEW.acting_role,
           coalesce(v_kind, CASE WHEN v_actor IS NULL THEN 'system' ELSE 'user' END)::audit_actor_type,
           v_actor, NEW.decision_comment
      FROM approval_routes r WHERE r.id = NEW.route_id;
    RETURN NULL;
END $$;
""",
    # ---------------------------------------------------- role scope check
    r"""
CREATE OR REPLACE FUNCTION sed_check_role_scope() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_kind     text;
    v_expected text := CASE NEW.role
        WHEN 'department_head' THEN 'department'
        WHEN 'institute_director' THEN 'institute'
    END;
BEGIN
    IF v_expected IS NOT NULL THEN
        SELECT kind::text INTO v_kind FROM org_units WHERE id = NEW.org_unit_id;
        IF v_kind IS DISTINCT FROM v_expected THEN
            RAISE EXCEPTION 'role % requires org unit of kind %, got %', NEW.role, v_expected, v_kind
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
""",
    # ----------------- business events (перегляд, експорт, завантаження файлу)
    r"""
CREATE OR REPLACE FUNCTION sed_audit_event(
    p_action text, p_entity_table text, p_entity_id uuid,
    p_document_id uuid DEFAULT NULL, p_details jsonb DEFAULT '{}'::jsonb
) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    v_actor uuid := nullif(current_setting('sed.actor_user_id', true), '')::uuid;
    v_kind  text := nullif(current_setting('sed.actor_type', true), '');
    v_id    bigint;
BEGIN
    IF p_action IN ('insert', 'update', 'delete') OR p_action LIKE 'ai.%' THEN
        RAISE EXCEPTION 'action % is reserved for triggers', p_action USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO audit_logs (category, actor_type, actor_user_id, action, entity_table, entity_id,
                            document_id, changes, request_id)
    VALUES ('business_event',
            coalesce(v_kind, CASE WHEN v_actor IS NULL THEN 'system' ELSE 'user' END)::audit_actor_type,
            v_actor, p_action, p_entity_table, p_entity_id, p_document_id, p_details,
            nullif(current_setting('sed.request_id', true), ''))
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;
""",
    # Журнал пишуть лише SECURITY DEFINER-функції; прямий доступ ролям застосунку не потрібен.
    "REVOKE ALL ON FUNCTION sed_audit_event(text, text, uuid, uuid, jsonb) FROM PUBLIC;",
    # ---------------------------------------- atomic registration (no gaps)
    r"""
CREATE OR REPLACE FUNCTION sed_register_document(
    p_document_id uuid, p_journal_id uuid, p_date date DEFAULT current_date
) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_journal registration_journals%ROWTYPE;
    v_number  text;
BEGIN
    -- Рядковий lock лічильника: номери без пропусків (rollback повертає лічильник).
    UPDATE registration_journals SET last_seq = last_seq + 1, updated_at = now()
     WHERE id = p_journal_id
    RETURNING * INTO v_journal;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'registration journal % not found', p_journal_id USING ERRCODE = 'no_data_found';
    END IF;
    IF v_journal.year <> extract(year FROM p_date)::int THEN
        RAISE EXCEPTION 'journal % is for year %, registration date is %', v_journal.code, v_journal.year, p_date
            USING ERRCODE = 'check_violation';
    END IF;

    v_number := replace(replace(v_journal.number_pattern, '{seq}', v_journal.last_seq::text),
                        '{suffix}', coalesce(v_journal.suffix, ''));

    UPDATE documents
       SET journal_id = p_journal_id, registration_seq = v_journal.last_seq,
           registration_number = v_number, registration_date = p_date,
           status = 'registered', row_version = row_version + 1, updated_at = now()
     WHERE id = p_document_id AND status = 'signed' AND registration_number IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'document % is not in status signed or already registered', p_document_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN v_number;
END $$;
""",
]


def _triggers_sql() -> list[str]:
    stmts: list[str] = [
        """CREATE TRIGGER trg_document_versions_protect
           BEFORE UPDATE OR DELETE ON document_versions
           FOR EACH ROW EXECUTE FUNCTION sed_protect_document_version();""",
        """CREATE TRIGGER trg_signatures_before_insert
           BEFORE INSERT ON signatures
           FOR EACH ROW EXECUTE FUNCTION sed_signature_before_insert();""",
        """CREATE TRIGGER trg_signatures_lock_version
           AFTER INSERT ON signatures
           FOR EACH ROW EXECUTE FUNCTION sed_signature_after_insert();""",
        """CREATE TRIGGER trg_approval_routes_lock_version
           AFTER INSERT OR UPDATE OF status ON approval_routes
           FOR EACH ROW EXECUTE FUNCTION sed_route_lock_version();""",
        """CREATE TRIGGER trg_audit_logs_chain
           BEFORE INSERT ON audit_logs
           FOR EACH ROW EXECUTE FUNCTION sed_audit_chain();""",
        # На COMMIT: підсумок аналізу ШІ (confidence, порушення, метадані) -> audit_logs.
        """CREATE CONSTRAINT TRIGGER trg_ai_runs_log_analysis
           AFTER INSERT OR UPDATE OF status ON ai_runs
           DEFERRABLE INITIALLY DEFERRED
           FOR EACH ROW WHEN (NEW.status = 'succeeded')
           EXECUTE FUNCTION sed_log_ai_analysis();""",
        """CREATE TRIGGER trg_documents_status_history
           AFTER INSERT OR UPDATE OF status ON documents
           FOR EACH ROW EXECUTE FUNCTION sed_document_status_history();""",
        """CREATE TRIGGER trg_approval_steps_history
           AFTER INSERT OR UPDATE OF status ON approval_steps
           FOR EACH ROW EXECUTE FUNCTION sed_approval_step_history();""",
        """CREATE TRIGGER trg_user_roles_scope
           BEFORE INSERT OR UPDATE OF role, org_unit_id ON user_roles
           FOR EACH ROW EXECUTE FUNCTION sed_check_role_scope();""",
    ]
    for table in ("documents", "approval_steps", "ai_runs", "ai_extracted_fields", "compliance_findings"):
        stmts.append(
            f"""CREATE TRIGGER trg_{table}_guard_legal
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION sed_guard_legal_decisions();"""
        )
    for table, mutable in APPEND_ONLY.items():
        args = ", ".join(f"'{c}'" for c in mutable)
        stmts.append(
            f"""CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION sed_immutable_except({args});"""
        )
        stmts.append(
            f"""CREATE TRIGGER trg_{table}_no_truncate
                BEFORE TRUNCATE ON {table}
                FOR EACH STATEMENT EXECUTE FUNCTION sed_immutable_except();"""
        )
    for table in AUDITED_TABLES:
        stmts.append(
            f"""CREATE TRIGGER trg_{table}_audit
                AFTER INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION sed_audit_row_change();"""
        )
    return stmts


def upgrade_extensions_sql() -> list[str]:
    return [f'CREATE EXTENSION IF NOT EXISTS "{ext}";' for ext in EXTENSIONS]


def upgrade_server_logic_sql() -> list[str]:
    return [*FUNCTIONS_SQL, *_triggers_sql()]


def downgrade_server_logic_sql() -> list[str]:
    """Виконується ПЕРШИМ у downgrade: CASCADE прибирає тригери, а sed_audit_hash
    залежить від рядкового типу audit_logs і блокував би DROP TABLE."""
    return [
        "DROP FUNCTION IF EXISTS sed_register_document(uuid, uuid, date) CASCADE;",
        "DROP FUNCTION IF EXISTS sed_audit_event(text, text, uuid, uuid, jsonb) CASCADE;",
        "DROP FUNCTION IF EXISTS sed_check_role_scope() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_approval_step_history() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_document_status_history() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_log_ai_analysis() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_audit_row_change() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_audit_verify(bigint) CASCADE;",
        "DROP FUNCTION IF EXISTS sed_audit_chain() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_audit_hash(audit_logs, bytea) CASCADE;",
        "DROP FUNCTION IF EXISTS sed_guard_legal_decisions() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_route_lock_version() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_signature_after_insert() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_signature_before_insert() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_protect_document_version() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_immutable_except() CASCADE;",
        "DROP FUNCTION IF EXISTS sed_is_ai_actor() CASCADE;",
    ]
