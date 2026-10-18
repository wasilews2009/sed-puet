-- СЕД ПУЕТ: повний DDL (PostgreSQL 16 + pgvector), знятий pg_dump --schema-only після 'alembic upgrade head'.
-- Довідковий файл для рев'ю. Джерело істини: моделі sed_db/ та міграції migrations/.

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

CREATE TYPE public.access_level AS ENUM (
    'public',
    'internal',
    'restricted',
    'personal_data'
);

CREATE TYPE public.ai_run_status AS ENUM (
    'queued',
    'running',
    'succeeded',
    'failed',
    'cancelled'
);

CREATE TYPE public.ai_task_type AS ENUM (
    'ocr',
    'classification',
    'ner_extraction',
    'compliance_check',
    'route_suggestion',
    'anomaly_detection',
    'document_generation',
    'pii_masking'
);

CREATE TYPE public.assignee_rule AS ENUM (
    'specific_user',
    'position_in_unit',
    'unit_head',
    'author_unit_head',
    'role',
    'author_unit_role'
);

CREATE TYPE public.audit_actor_type AS ENUM (
    'user',
    'system',
    'ai_service'
);

CREATE TYPE public.audit_event_category AS ENUM (
    'data_change',
    'ai_analysis',
    'business_event'
);

CREATE TYPE public.compliance_verdict AS ENUM (
    'compliant',
    'issues_found',
    'non_compliant',
    'inconclusive'
);

CREATE TYPE public.document_direction AS ENUM (
    'internal',
    'incoming',
    'outgoing'
);

CREATE TYPE public.document_link_type AS ENUM (
    'executes',
    'amends',
    'revokes',
    'replies_to',
    'supplements',
    'based_on'
);

CREATE TYPE public.document_status AS ENUM (
    'draft',
    'ai_review',
    'on_approval',
    'revision_required',
    'approved',
    'signed',
    'registered',
    'in_execution',
    'executed',
    'archived',
    'cancelled'
);

CREATE TYPE public.finding_category AS ENUM (
    'contradiction',
    'outdated_reference',
    'missing_requisite',
    'dstu_formatting',
    'competence_violation',
    'deadline_violation',
    'personal_data',
    'other'
);

CREATE TYPE public.finding_severity AS ENUM (
    'info',
    'minor',
    'major',
    'critical'
);

CREATE TYPE public.finding_status AS ENUM (
    'open',
    'accepted',
    'dismissed',
    'fixed'
);

CREATE TYPE public.human_review_status AS ENUM (
    'not_required',
    'pending',
    'confirmed',
    'corrected',
    'rejected'
);

CREATE TYPE public.model_provider AS ENUM (
    'vllm',
    'ollama',
    'local_other'
);

CREATE TYPE public.org_unit_kind AS ENUM (
    'university',
    'rectorate',
    'institute',
    'faculty',
    'department',
    'division',
    'center',
    'collegial_body'
);

CREATE TYPE public.regulatory_level AS ENUM (
    'law',
    'cabinet',
    'ministry',
    'standard',
    'university',
    'unit'
);

CREATE TYPE public.regulatory_status AS ENUM (
    'draft',
    'in_force',
    'amended',
    'repealed'
);

CREATE TYPE public.route_origin AS ENUM (
    'manual',
    'template',
    'ai_suggested'
);

CREATE TYPE public.route_status AS ENUM (
    'draft',
    'active',
    'completed',
    'rejected',
    'cancelled'
);

CREATE TYPE public.signature_format AS ENUM (
    'CAdES-BES',
    'CAdES-X-Long',
    'XAdES-B-LT',
    'PAdES-B-LT',
    'ASiC-E'
);

CREATE TYPE public.signature_provider AS ENUM (
    'file_key',
    'hardware_token',
    'cloud_kep',
    'diia_signature'
);

CREATE TYPE public.signature_validation AS ENUM (
    'pending',
    'valid',
    'invalid',
    'cert_revoked',
    'cert_expired',
    'indeterminate'
);

CREATE TYPE public.step_kind AS ENUM (
    'visa',
    'approval',
    'signing',
    'ratification',
    'acknowledgement',
    'registration'
);

CREATE TYPE public.step_status AS ENUM (
    'waiting',
    'active',
    'approved',
    'approved_with_remarks',
    'rejected',
    'skipped',
    'delegated',
    'expired'
);

CREATE TYPE public.user_role AS ENUM (
    'student',
    'lecturer',
    'department_head',
    'institute_director',
    'unit_head',
    'rector',
    'vice_rector',
    'staff',
    'student_government_head'
);

CREATE TYPE public.version_source AS ENUM (
    'upload',
    'editor',
    'template',
    'ai_generated',
    'ocr_scan'
);

CREATE FUNCTION public.sed_approval_step_history() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.sed_audit_chain() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.sed_audit_event(p_action text, p_entity_table text, p_entity_id uuid, p_document_id uuid DEFAULT NULL::uuid, p_details jsonb DEFAULT '{}'::jsonb) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE TABLE public.audit_logs (
    id bigint NOT NULL,
    chain_seq bigint DEFAULT 0 NOT NULL,
    occurred_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    category public.audit_event_category DEFAULT 'data_change'::public.audit_event_category NOT NULL,
    actor_type public.audit_actor_type NOT NULL,
    actor_user_id uuid,
    action character varying(64) NOT NULL,
    entity_table character varying(63) NOT NULL,
    entity_id uuid,
    document_id uuid,
    document_version_id uuid,
    changes jsonb DEFAULT '{}'::jsonb NOT NULL,
    ai_run_id uuid,
    ai_task_type public.ai_task_type,
    ai_model character varying(200),
    confidence_score numeric(5,4),
    requires_human_review boolean,
    ai_verdict character varying(64),
    violations jsonb,
    extracted_metadata jsonb,
    request_id character varying(64),
    client_ip inet,
    user_agent character varying(512),
    prev_hash bytea,
    record_hash bytea DEFAULT '\x'::bytea NOT NULL,
    CONSTRAINT ck_audit_logs_ai_analysis_complete CHECK (((category <> 'ai_analysis'::public.audit_event_category) OR ((ai_task_type IS NOT NULL) AND (confidence_score IS NOT NULL) AND (requires_human_review IS NOT NULL) AND (jsonb_typeof(violations) = 'array'::text) AND (jsonb_typeof(extracted_metadata) = 'object'::text)))),
    CONSTRAINT ck_audit_logs_ai_fields_only_for_ai CHECK (((category = 'ai_analysis'::public.audit_event_category) = (ai_run_id IS NOT NULL))),
    CONSTRAINT ck_audit_logs_confidence_range CHECK (((confidence_score IS NULL) OR ((confidence_score >= (0)::numeric) AND (confidence_score <= (1)::numeric))))
);

CREATE FUNCTION public.sed_audit_hash(r public.audit_logs, p_prev bytea) RETURNS bytea
    LANGUAGE sql IMMUTABLE
    AS $$
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

CREATE FUNCTION public.sed_audit_row_change() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.sed_audit_verify(p_from_seq bigint DEFAULT 1) RETURNS TABLE(broken_seq bigint, reason text)
    LANGUAGE plpgsql STABLE
    AS $$
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

CREATE FUNCTION public.sed_check_role_scope() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION public.sed_document_status_history() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.sed_guard_legal_decisions() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION public.sed_immutable_except() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION public.sed_is_ai_actor() RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
    -- Пряме членство замість pg_has_role(): суперкористувач «член» будь-якої ролі.
    SELECT coalesce(current_setting('sed.actor_type', true), '') = 'ai_service'
        OR current_user = 'sed_ai_worker'
        OR EXISTS (SELECT 1
                     FROM pg_auth_members m
                     JOIN pg_roles r ON r.oid = m.roleid
                     JOIN pg_roles u ON u.oid = m.member
                    WHERE r.rolname = 'sed_ai_worker' AND u.rolname = current_user)
$$;

CREATE FUNCTION public.sed_log_ai_analysis() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
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

CREATE FUNCTION public.sed_protect_document_version() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION public.sed_register_document(p_document_id uuid, p_journal_id uuid, p_date date DEFAULT CURRENT_DATE) RETURNS text
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION public.sed_route_lock_version() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.status = 'active' THEN
        UPDATE document_versions
           SET is_locked = true, locked_at = coalesce(locked_at, now())
         WHERE id = NEW.document_version_id AND NOT is_locked;
    END IF;
    RETURN NULL;
END $$;

CREATE FUNCTION public.sed_signature_after_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE document_versions
       SET is_locked = true, locked_at = coalesce(locked_at, now())
     WHERE id = NEW.document_version_id AND NOT is_locked;
    RETURN NULL;
END $$;

CREATE FUNCTION public.sed_signature_before_insert() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE TABLE public.ai_extracted_fields (
    ai_run_id uuid NOT NULL,
    document_version_id uuid NOT NULL,
    field_key character varying(64) NOT NULL,
    extracted_value jsonb,
    validated_value jsonb,
    confidence numeric(5,4) NOT NULL,
    requires_human_review boolean GENERATED ALWAYS AS ((confidence < confidence_threshold)) STORED NOT NULL,
    page_no smallint,
    bbox jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    confidence_threshold numeric(5,4) DEFAULT 0.85 NOT NULL,
    human_review_status public.human_review_status DEFAULT 'not_required'::public.human_review_status NOT NULL,
    reviewed_by_id uuid,
    reviewed_at timestamp with time zone,
    review_comment text,
    CONSTRAINT ck_ai_extracted_fields_confidence_range CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))),
    CONSTRAINT ck_ai_extracted_fields_corrected_has_value CHECK (((human_review_status <> 'corrected'::public.human_review_status) OR (validated_value IS NOT NULL))),
    CONSTRAINT ck_ai_extracted_fields_low_confidence_needs_review CHECK (((NOT COALESCE((confidence < confidence_threshold), false)) OR (human_review_status <> 'not_required'::public.human_review_status))),
    CONSTRAINT ck_ai_extracted_fields_review_has_reviewer CHECK (((human_review_status = ANY (ARRAY['confirmed'::public.human_review_status, 'corrected'::public.human_review_status, 'rejected'::public.human_review_status])) = ((reviewed_by_id IS NOT NULL) AND (reviewed_at IS NOT NULL)))),
    CONSTRAINT ck_ai_extracted_fields_threshold_not_lowered CHECK (((confidence_threshold >= 0.85) AND (confidence_threshold <= (1)::numeric)))
);

CREATE TABLE public.ai_models (
    name character varying(128) NOT NULL,
    version character varying(64) NOT NULL,
    provider public.model_provider NOT NULL,
    task_type public.ai_task_type NOT NULL,
    weights_digest character varying(128),
    quantization character varying(32),
    context_window integer,
    embedding_dim smallint,
    is_active boolean DEFAULT true NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.ai_runs (
    task_type public.ai_task_type NOT NULL,
    status public.ai_run_status DEFAULT 'queued'::public.ai_run_status NOT NULL,
    document_id uuid,
    document_version_id uuid,
    model_id uuid NOT NULL,
    prompt_template_id uuid,
    initiated_by_id uuid,
    worker_task_id character varying(128),
    input_sha256 character varying(64),
    pii_masked boolean DEFAULT false NOT NULL,
    pii_entities_masked integer,
    parameters jsonb DEFAULT '{}'::jsonb NOT NULL,
    output jsonb,
    confidence numeric(5,4),
    requires_human_review boolean GENERATED ALWAYS AS (COALESCE((confidence < confidence_threshold), false)) STORED NOT NULL,
    tokens_in integer,
    tokens_out integer,
    latency_ms integer,
    error_message text,
    queued_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    confidence_threshold numeric(5,4) DEFAULT 0.85 NOT NULL,
    human_review_status public.human_review_status DEFAULT 'not_required'::public.human_review_status NOT NULL,
    reviewed_by_id uuid,
    reviewed_at timestamp with time zone,
    review_comment text,
    CONSTRAINT ck_ai_runs_confidence_range CHECK (((confidence IS NULL) OR ((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))),
    CONSTRAINT ck_ai_runs_failed_has_error CHECK (((status <> 'failed'::public.ai_run_status) OR (error_message IS NOT NULL))),
    CONSTRAINT ck_ai_runs_llm_input_pii_masked CHECK (((task_type <> ALL (ARRAY['classification'::public.ai_task_type, 'compliance_check'::public.ai_task_type, 'document_generation'::public.ai_task_type, 'ner_extraction'::public.ai_task_type, 'route_suggestion'::public.ai_task_type])) OR pii_masked)),
    CONSTRAINT ck_ai_runs_low_confidence_needs_review CHECK (((NOT COALESCE((confidence < confidence_threshold), false)) OR (human_review_status <> 'not_required'::public.human_review_status))),
    CONSTRAINT ck_ai_runs_review_has_reviewer CHECK (((human_review_status = ANY (ARRAY['confirmed'::public.human_review_status, 'corrected'::public.human_review_status, 'rejected'::public.human_review_status])) = ((reviewed_by_id IS NOT NULL) AND (reviewed_at IS NOT NULL)))),
    CONSTRAINT ck_ai_runs_sha256_format CHECK (((input_sha256 IS NULL) OR ((input_sha256)::text ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT ck_ai_runs_succeeded_has_confidence CHECK (((status <> 'succeeded'::public.ai_run_status) OR ((confidence IS NOT NULL) AND (finished_at IS NOT NULL)))),
    CONSTRAINT ck_ai_runs_threshold_not_lowered CHECK (((confidence_threshold >= 0.85) AND (confidence_threshold <= (1)::numeric))),
    CONSTRAINT ck_ai_runs_time_order CHECK (((finished_at IS NULL) OR (started_at IS NULL) OR (finished_at >= started_at)))
);

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

CREATE TABLE public.approval_routes (
    document_id uuid NOT NULL,
    document_version_id uuid NOT NULL,
    template_id uuid,
    origin public.route_origin NOT NULL,
    suggested_by_ai_run_id uuid,
    status public.route_status DEFAULT 'draft'::public.route_status NOT NULL,
    current_stage smallint,
    initiated_by_id uuid NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    cancel_reason character varying(1024),
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_approval_routes_active_started CHECK (((status <> 'active'::public.route_status) OR (started_at IS NOT NULL))),
    CONSTRAINT ck_approval_routes_ai_origin_has_run CHECK (((origin <> 'ai_suggested'::public.route_origin) OR (suggested_by_ai_run_id IS NOT NULL))),
    CONSTRAINT ck_approval_routes_cancel_reason CHECK (((status <> 'cancelled'::public.route_status) OR (cancel_reason IS NOT NULL))),
    CONSTRAINT ck_approval_routes_finished_has_time CHECK (((status <> ALL (ARRAY['completed'::public.route_status, 'rejected'::public.route_status, 'cancelled'::public.route_status])) OR (completed_at IS NOT NULL))),
    CONSTRAINT ck_approval_routes_template_origin CHECK (((origin <> 'template'::public.route_origin) OR (template_id IS NOT NULL)))
);

CREATE TABLE public.approval_step_history (
    id bigint NOT NULL,
    step_id uuid NOT NULL,
    route_id uuid NOT NULL,
    document_id uuid NOT NULL,
    from_status public.step_status,
    to_status public.step_status NOT NULL,
    assignee_id uuid NOT NULL,
    acting_role public.user_role,
    actor_type public.audit_actor_type NOT NULL,
    changed_by_id uuid,
    comment text,
    changed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_approval_step_history_real_transition CHECK ((from_status IS DISTINCT FROM to_status))
);

ALTER TABLE public.approval_step_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.approval_step_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.approval_steps (
    route_id uuid NOT NULL,
    template_step_id uuid,
    stage_no smallint NOT NULL,
    ordinal smallint NOT NULL,
    step_kind public.step_kind NOT NULL,
    assignee_id uuid NOT NULL,
    original_assignee_id uuid,
    delegation_id uuid,
    acting_role public.user_role,
    status public.step_status DEFAULT 'waiting'::public.step_status NOT NULL,
    is_mandatory boolean DEFAULT true NOT NULL,
    requires_kep boolean DEFAULT false NOT NULL,
    activated_at timestamp with time zone,
    due_at timestamp with time zone,
    decided_at timestamp with time zone,
    decision_comment text,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_approval_steps_activated_has_time CHECK (((status = ANY (ARRAY['waiting'::public.step_status, 'skipped'::public.step_status])) OR (activated_at IS NOT NULL))),
    CONSTRAINT ck_approval_steps_decided_has_time CHECK (((status = ANY (ARRAY['approved'::public.step_status, 'approved_with_remarks'::public.step_status, 'rejected'::public.step_status, 'skipped'::public.step_status, 'delegated'::public.step_status, 'expired'::public.step_status])) = (decided_at IS NOT NULL))),
    CONSTRAINT ck_approval_steps_delegation_consistent CHECK (((delegation_id IS NULL) = (original_assignee_id IS NULL))),
    CONSTRAINT ck_approval_steps_positive_order CHECK (((stage_no > 0) AND (ordinal > 0))),
    CONSTRAINT ck_approval_steps_rejection_has_comment CHECK (((status <> ALL (ARRAY['rejected'::public.step_status, 'approved_with_remarks'::public.step_status])) OR (length(btrim(COALESCE(decision_comment, ''::text))) > 0))),
    CONSTRAINT ck_approval_steps_signing_requires_kep CHECK (((step_kind <> 'signing'::public.step_kind) OR requires_kep))
);

ALTER TABLE public.audit_logs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.audit_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.compliance_checks (
    ai_run_id uuid NOT NULL,
    document_version_id uuid NOT NULL,
    verdict public.compliance_verdict NOT NULL,
    summary text,
    regulations_as_of date NOT NULL,
    retrieval_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    sources_considered integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL
);

CREATE TABLE public.compliance_citations (
    finding_id uuid NOT NULL,
    chunk_id uuid NOT NULL,
    rank smallint NOT NULL,
    quoted_text text NOT NULL,
    quote_verified boolean NOT NULL,
    source_status_snapshot public.regulatory_status NOT NULL,
    dense_score double precision,
    sparse_score double precision,
    fused_score double precision,
    rerank_score double precision,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_compliance_citations_quote_not_empty CHECK ((length(quoted_text) > 0)),
    CONSTRAINT ck_compliance_citations_rank_positive CHECK ((rank > 0))
);

CREATE TABLE public.compliance_findings (
    check_id uuid NOT NULL,
    ordinal smallint NOT NULL,
    severity public.finding_severity NOT NULL,
    category public.finding_category NOT NULL,
    title character varying(512) NOT NULL,
    explanation text NOT NULL,
    recommendation text,
    fragment_text text,
    page_no smallint,
    char_start integer,
    char_end integer,
    confidence numeric(5,4) NOT NULL,
    requires_human_review boolean GENERATED ALWAYS AS ((confidence < 0.85)) STORED NOT NULL,
    status public.finding_status DEFAULT 'open'::public.finding_status NOT NULL,
    resolved_by_id uuid,
    resolved_at timestamp with time zone,
    resolution_comment text,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_compliance_findings_confidence_range CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric))),
    CONSTRAINT ck_compliance_findings_dismissal_has_reason CHECK (((status <> 'dismissed'::public.finding_status) OR (length(btrim(COALESCE(resolution_comment, ''::text))) > 0))),
    CONSTRAINT ck_compliance_findings_fragment_span CHECK ((((char_start IS NULL) AND (char_end IS NULL)) OR ((char_start >= 0) AND (char_end > char_start)))),
    CONSTRAINT ck_compliance_findings_resolution_has_actor CHECK (((status = 'open'::public.finding_status) = ((resolved_by_id IS NULL) AND (resolved_at IS NULL))))
);

CREATE TABLE public.delegations (
    from_user_id uuid NOT NULL,
    to_user_id uuid NOT NULL,
    document_type_id uuid,
    valid_from timestamp with time zone NOT NULL,
    valid_to timestamp with time zone NOT NULL,
    basis character varying(512),
    created_by_id uuid NOT NULL,
    revoked_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_delegations_not_self CHECK ((from_user_id <> to_user_id)),
    CONSTRAINT ck_delegations_valid_period CHECK ((valid_to > valid_from))
);

CREATE TABLE public.document_links (
    source_document_id uuid NOT NULL,
    target_document_id uuid NOT NULL,
    link_type public.document_link_type NOT NULL,
    note character varying(512),
    created_by_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_document_links_not_self CHECK ((source_document_id <> target_document_id))
);

CREATE TABLE public.document_status_history (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    from_status public.document_status,
    to_status public.document_status NOT NULL,
    document_version_id uuid,
    route_id uuid,
    actor_type public.audit_actor_type NOT NULL,
    changed_by_id uuid,
    reason text,
    changed_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ck_document_status_history_real_transition CHECK ((from_status IS DISTINCT FROM to_status))
);

ALTER TABLE public.document_status_history ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.document_status_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.document_types (
    code character varying(64) NOT NULL,
    name_uk character varying(256) NOT NULL,
    default_direction public.document_direction DEFAULT 'internal'::public.document_direction NOT NULL,
    default_access_level public.access_level DEFAULT 'internal'::public.access_level NOT NULL,
    requires_kep boolean DEFAULT true NOT NULL,
    dstu_template_key character varying(128),
    requisites_schema jsonb,
    is_active boolean DEFAULT true NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.document_versions (
    document_id uuid NOT NULL,
    version_number integer NOT NULL,
    parent_version_id uuid,
    source public.version_source NOT NULL,
    ai_run_id uuid,
    storage_bucket character varying(63) NOT NULL,
    storage_key character varying(1024) NOT NULL,
    storage_version_id character varying(128),
    file_name character varying(512) NOT NULL,
    mime_type character varying(128) NOT NULL,
    size_bytes bigint NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    page_count integer,
    extracted_text text,
    text_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(extracted_text, ''::text))) STORED,
    change_note character varying(1024),
    created_by_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_locked boolean DEFAULT false NOT NULL,
    locked_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_document_versions_lock_timestamp CHECK ((is_locked = (locked_at IS NOT NULL))),
    CONSTRAINT ck_document_versions_not_self_parent CHECK (((parent_version_id IS NULL) OR (parent_version_id <> id))),
    CONSTRAINT ck_document_versions_sha256_format CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_document_versions_size_positive CHECK ((size_bytes > 0)),
    CONSTRAINT ck_document_versions_version_number_positive CHECK ((version_number > 0))
);

CREATE TABLE public.documents (
    document_type_id uuid NOT NULL,
    direction public.document_direction NOT NULL,
    status public.document_status DEFAULT 'draft'::public.document_status NOT NULL,
    access_level public.access_level NOT NULL,
    title character varying(1024) NOT NULL,
    summary text,
    author_id uuid NOT NULL,
    org_unit_id uuid NOT NULL,
    nomenclature_case_id uuid,
    journal_id uuid,
    registration_seq integer,
    registration_number character varying(64),
    registration_date date,
    correspondent_name character varying(512),
    correspondent_number character varying(64),
    correspondent_date date,
    due_date date,
    effective_from date,
    executed_at timestamp with time zone,
    requisites jsonb DEFAULT '{}'::jsonb NOT NULL,
    current_version_id uuid,
    row_version integer DEFAULT 1 NOT NULL,
    deleted_at timestamp with time zone,
    search_tsv tsvector GENERATED ALWAYS AS (((setweight(to_tsvector('simple'::regconfig, (COALESCE(title, ''::character varying))::text), 'A'::"char") || setweight(to_tsvector('simple'::regconfig, (COALESCE(registration_number, ''::character varying))::text), 'A'::"char")) || setweight(to_tsvector('simple'::regconfig, COALESCE(summary, ''::text)), 'B'::"char"))) STORED NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_documents_incoming_has_correspondent CHECK (((direction <> 'incoming'::public.document_direction) OR (correspondent_name IS NOT NULL))),
    CONSTRAINT ck_documents_journal_seq_together CHECK (((journal_id IS NULL) = (registration_seq IS NULL))),
    CONSTRAINT ck_documents_registered_has_number CHECK (((status <> ALL (ARRAY['registered'::public.document_status, 'in_execution'::public.document_status, 'executed'::public.document_status, 'archived'::public.document_status])) OR ((registration_number IS NOT NULL) AND (registration_date IS NOT NULL)))),
    CONSTRAINT ck_documents_row_version_positive CHECK ((row_version > 0))
);

CREATE TABLE public.nomenclature_cases (
    year smallint NOT NULL,
    case_index character varying(32) NOT NULL,
    title_uk character varying(1024) NOT NULL,
    org_unit_id uuid NOT NULL,
    retention_term character varying(64) NOT NULL,
    retention_article character varying(64),
    closed_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_nomenclature_cases_year_range CHECK (((year >= 2000) AND (year <= 2100)))
);

CREATE TABLE public.org_units (
    parent_id uuid,
    kind public.org_unit_kind NOT NULL,
    code character varying(32) NOT NULL,
    name_uk character varying(512) NOT NULL,
    short_name_uk character varying(128),
    is_active boolean DEFAULT true NOT NULL,
    valid_from date DEFAULT CURRENT_DATE NOT NULL,
    valid_to date,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_org_units_not_self_parent CHECK (((parent_id IS NULL) OR (parent_id <> id))),
    CONSTRAINT ck_org_units_valid_period CHECK (((valid_to IS NULL) OR (valid_to > valid_from)))
);

CREATE TABLE public.prompt_templates (
    code character varying(64) NOT NULL,
    version integer NOT NULL,
    task_type public.ai_task_type NOT NULL,
    body text NOT NULL,
    body_sha256 character varying(64) NOT NULL,
    created_by_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_prompt_templates_sha256_format CHECK (((body_sha256)::text ~ '^[0-9a-f]{64}$'::text))
);

CREATE TABLE public.registration_journals (
    code character varying(64) NOT NULL,
    name_uk character varying(256) NOT NULL,
    year smallint NOT NULL,
    document_type_id uuid,
    number_pattern character varying(64) DEFAULT '{seq}'::character varying NOT NULL,
    suffix character varying(16),
    last_seq integer DEFAULT 0 NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_registration_journals_last_seq_non_negative CHECK ((last_seq >= 0))
);

CREATE TABLE public.regulatory_chunks (
    source_id uuid NOT NULL,
    index_revision integer NOT NULL,
    chunk_no integer NOT NULL,
    section_ref character varying(256),
    content text NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, content)) STORED NOT NULL,
    embedding public.vector(1024),
    embedding_model_id uuid,
    token_count integer,
    is_current boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_regulatory_chunks_embedding_has_model CHECK (((embedding IS NULL) = (embedding_model_id IS NULL)))
);

CREATE TABLE public.regulatory_sources (
    level public.regulatory_level NOT NULL,
    issuer character varying(256) NOT NULL,
    title character varying(1024) NOT NULL,
    doc_number character varying(64),
    adopted_on date,
    effective_from date,
    effective_to date,
    status public.regulatory_status NOT NULL,
    supersedes_id uuid,
    document_id uuid,
    org_unit_id uuid,
    source_url character varying(2048),
    storage_bucket character varying(63),
    storage_key character varying(1024),
    content_sha256 character varying(64),
    current_index_revision integer DEFAULT 0 NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_regulatory_sources_effective_period CHECK (((effective_to IS NULL) OR (effective_from IS NULL) OR (effective_to >= effective_from))),
    CONSTRAINT ck_regulatory_sources_not_self_supersede CHECK (((supersedes_id IS NULL) OR (supersedes_id <> id))),
    CONSTRAINT ck_regulatory_sources_repealed_has_end CHECK (((status <> 'repealed'::public.regulatory_status) OR (effective_to IS NOT NULL))),
    CONSTRAINT ck_regulatory_sources_sha256_format CHECK (((content_sha256 IS NULL) OR ((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)))
);

CREATE TABLE public.route_template_steps (
    template_id uuid NOT NULL,
    stage_no smallint NOT NULL,
    ordinal smallint NOT NULL,
    step_kind public.step_kind NOT NULL,
    assignee_rule public.assignee_rule NOT NULL,
    assignee_user_id uuid,
    assignee_org_unit_id uuid,
    position_code character varying(64),
    role public.user_role,
    deadline_hours integer,
    is_mandatory boolean DEFAULT true NOT NULL,
    requires_kep boolean DEFAULT false NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_route_template_steps_assignee_rule_params CHECK ((((assignee_rule <> 'specific_user'::public.assignee_rule) OR (assignee_user_id IS NOT NULL)) AND ((assignee_rule <> 'position_in_unit'::public.assignee_rule) OR ((assignee_org_unit_id IS NOT NULL) AND (position_code IS NOT NULL))) AND ((assignee_rule <> 'unit_head'::public.assignee_rule) OR (assignee_org_unit_id IS NOT NULL)) AND ((assignee_rule <> ALL (ARRAY['role'::public.assignee_rule, 'author_unit_role'::public.assignee_rule])) OR (role IS NOT NULL)))),
    CONSTRAINT ck_route_template_steps_deadline_positive CHECK (((deadline_hours IS NULL) OR (deadline_hours > 0))),
    CONSTRAINT ck_route_template_steps_positive_order CHECK (((stage_no > 0) AND (ordinal > 0))),
    CONSTRAINT ck_route_template_steps_signing_requires_kep CHECK (((step_kind <> 'signing'::public.step_kind) OR requires_kep))
);

CREATE TABLE public.route_templates (
    code character varying(64) NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    name_uk character varying(256) NOT NULL,
    description text,
    document_type_id uuid NOT NULL,
    org_unit_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_by_id uuid NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_route_templates_version_positive CHECK ((version > 0))
);

CREATE TABLE public.signatures (
    document_version_id uuid NOT NULL,
    signer_id uuid NOT NULL,
    approval_step_id uuid,
    signature_format public.signature_format NOT NULL,
    provider public.signature_provider NOT NULL,
    storage_bucket character varying(63) NOT NULL,
    storage_key character varying(1024) NOT NULL,
    signed_content_sha256 character varying(64) NOT NULL,
    cert_serial character varying(128) NOT NULL,
    cert_issuer character varying(512) NOT NULL,
    cert_subject_cn character varying(512) NOT NULL,
    signer_tax_id_hmac character varying(64),
    cert_valid_from timestamp with time zone NOT NULL,
    cert_valid_to timestamp with time zone NOT NULL,
    signed_at timestamp with time zone NOT NULL,
    tsp_time timestamp with time zone,
    validation_status public.signature_validation DEFAULT 'pending'::public.signature_validation NOT NULL,
    validation_report jsonb,
    validated_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_signatures_cert_period CHECK ((cert_valid_to > cert_valid_from)),
    CONSTRAINT ck_signatures_sha256_format CHECK (((signed_content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_signatures_signed_within_cert_period CHECK (((signed_at >= cert_valid_from) AND (signed_at <= cert_valid_to))),
    CONSTRAINT ck_signatures_validated_has_time CHECK (((validation_status = 'pending'::public.signature_validation) OR (validated_at IS NOT NULL)))
);

CREATE TABLE public.staff_assignments (
    user_id uuid NOT NULL,
    org_unit_id uuid NOT NULL,
    position_code character varying(64) NOT NULL,
    position_title_uk character varying(256) NOT NULL,
    is_head boolean DEFAULT false NOT NULL,
    is_acting boolean DEFAULT false NOT NULL,
    valid_from date NOT NULL,
    valid_to date,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_staff_assignments_valid_period CHECK (((valid_to IS NULL) OR (valid_to > valid_from)))
);

CREATE TABLE public.user_roles (
    user_id uuid NOT NULL,
    role public.user_role NOT NULL,
    org_unit_id uuid,
    scope_note character varying(256),
    is_acting boolean DEFAULT false NOT NULL,
    valid_from date DEFAULT CURRENT_DATE NOT NULL,
    valid_to date,
    basis character varying(512),
    granted_by_id uuid,
    keycloak_synced_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_user_roles_rector_university_level CHECK (((role <> 'rector'::public.user_role) OR (org_unit_id IS NULL))),
    CONSTRAINT ck_user_roles_unit_scope_required CHECK (((role <> ALL (ARRAY['department_head'::public.user_role, 'institute_director'::public.user_role, 'lecturer'::public.user_role, 'staff'::public.user_role, 'student'::public.user_role, 'unit_head'::public.user_role])) OR (org_unit_id IS NOT NULL))),
    CONSTRAINT ck_user_roles_valid_period CHECK (((valid_to IS NULL) OR (valid_to > valid_from)))
);

CREATE TABLE public.users (
    keycloak_sub uuid NOT NULL,
    email character varying(320) NOT NULL,
    last_name character varying(128) NOT NULL,
    first_name character varying(128) NOT NULL,
    patronymic character varying(128),
    primary_org_unit_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    last_synced_at timestamp with time zone,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.version_attachments (
    document_version_id uuid NOT NULL,
    ordinal smallint NOT NULL,
    title character varying(512) NOT NULL,
    storage_bucket character varying(63) NOT NULL,
    storage_key character varying(1024) NOT NULL,
    mime_type character varying(128) NOT NULL,
    size_bytes bigint NOT NULL,
    content_sha256 character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT ck_version_attachments_ordinal_positive CHECK ((ordinal > 0)),
    CONSTRAINT ck_version_attachments_sha256_format CHECK (((content_sha256)::text ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT ck_version_attachments_size_positive CHECK ((size_bytes > 0))
);

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY public.staff_assignments
    ADD CONSTRAINT ex_staff_assignments_one_head EXCLUDE USING gist (org_unit_id WITH =, daterange(valid_from, valid_to, '[)'::text) WITH &&) WHERE (is_head);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT ex_user_roles_no_duplicates EXCLUDE USING gist (user_id WITH =, role WITH =, COALESCE(org_unit_id, '00000000-0000-0000-0000-000000000000'::uuid) WITH =, daterange(valid_from, valid_to, '[)'::text) WITH &&);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT ex_user_roles_single_head EXCLUDE USING gist (role WITH =, COALESCE(org_unit_id, '00000000-0000-0000-0000-000000000000'::uuid) WITH =, daterange(valid_from, valid_to, '[)'::text) WITH &&) WHERE ((role = ANY (ARRAY['department_head'::public.user_role, 'institute_director'::public.user_role, 'rector'::public.user_role, 'student_government_head'::public.user_role, 'unit_head'::public.user_role])));

ALTER TABLE ONLY public.ai_extracted_fields
    ADD CONSTRAINT pk_ai_extracted_fields PRIMARY KEY (id);

ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT pk_ai_models PRIMARY KEY (id);

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT pk_ai_runs PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT pk_approval_routes PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT pk_approval_step_history PRIMARY KEY (id);

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT pk_approval_steps PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT pk_audit_logs PRIMARY KEY (id);

ALTER TABLE ONLY public.compliance_checks
    ADD CONSTRAINT pk_compliance_checks PRIMARY KEY (id);

ALTER TABLE ONLY public.compliance_citations
    ADD CONSTRAINT pk_compliance_citations PRIMARY KEY (id);

ALTER TABLE ONLY public.compliance_findings
    ADD CONSTRAINT pk_compliance_findings PRIMARY KEY (id);

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT pk_delegations PRIMARY KEY (id);

ALTER TABLE ONLY public.document_links
    ADD CONSTRAINT pk_document_links PRIMARY KEY (id);

ALTER TABLE ONLY public.document_status_history
    ADD CONSTRAINT pk_document_status_history PRIMARY KEY (id);

ALTER TABLE ONLY public.document_types
    ADD CONSTRAINT pk_document_types PRIMARY KEY (id);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT pk_document_versions PRIMARY KEY (id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT pk_documents PRIMARY KEY (id);

ALTER TABLE ONLY public.nomenclature_cases
    ADD CONSTRAINT pk_nomenclature_cases PRIMARY KEY (id);

ALTER TABLE ONLY public.org_units
    ADD CONSTRAINT pk_org_units PRIMARY KEY (id);

ALTER TABLE ONLY public.prompt_templates
    ADD CONSTRAINT pk_prompt_templates PRIMARY KEY (id);

ALTER TABLE ONLY public.registration_journals
    ADD CONSTRAINT pk_registration_journals PRIMARY KEY (id);

ALTER TABLE ONLY public.regulatory_chunks
    ADD CONSTRAINT pk_regulatory_chunks PRIMARY KEY (id);

ALTER TABLE ONLY public.regulatory_sources
    ADD CONSTRAINT pk_regulatory_sources PRIMARY KEY (id);

ALTER TABLE ONLY public.route_template_steps
    ADD CONSTRAINT pk_route_template_steps PRIMARY KEY (id);

ALTER TABLE ONLY public.route_templates
    ADD CONSTRAINT pk_route_templates PRIMARY KEY (id);

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT pk_signatures PRIMARY KEY (id);

ALTER TABLE ONLY public.staff_assignments
    ADD CONSTRAINT pk_staff_assignments PRIMARY KEY (id);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT pk_user_roles PRIMARY KEY (id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);

ALTER TABLE ONLY public.version_attachments
    ADD CONSTRAINT pk_version_attachments PRIMARY KEY (id);

ALTER TABLE ONLY public.ai_extracted_fields
    ADD CONSTRAINT uq_ai_extracted_fields_ai_run_id_field_key UNIQUE (ai_run_id, field_key);

ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT uq_ai_models_name_version UNIQUE (name, version);

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT uq_ai_runs_id_version UNIQUE (id, document_version_id);

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT uq_approval_steps_route_id_stage_no_ordinal UNIQUE (route_id, stage_no, ordinal);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT uq_audit_logs_chain_seq UNIQUE (chain_seq);

ALTER TABLE ONLY public.compliance_checks
    ADD CONSTRAINT uq_compliance_checks_ai_run_id UNIQUE (ai_run_id);

ALTER TABLE ONLY public.compliance_citations
    ADD CONSTRAINT uq_compliance_citations_finding_id_chunk_id UNIQUE (finding_id, chunk_id);

ALTER TABLE ONLY public.compliance_citations
    ADD CONSTRAINT uq_compliance_citations_finding_id_rank UNIQUE (finding_id, rank);

ALTER TABLE ONLY public.compliance_findings
    ADD CONSTRAINT uq_compliance_findings_check_id_ordinal UNIQUE (check_id, ordinal);

ALTER TABLE ONLY public.document_links
    ADD CONSTRAINT uq_document_links_source_document_id_target_document_id_1307 UNIQUE (source_document_id, target_document_id, link_type);

ALTER TABLE ONLY public.document_types
    ADD CONSTRAINT uq_document_types_code UNIQUE (code);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT uq_document_versions_document_id_version_number UNIQUE (document_id, version_number);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT uq_document_versions_id_document UNIQUE (id, document_id);

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT uq_document_versions_storage_bucket_storage_key_storage_db7a UNIQUE NULLS NOT DISTINCT (storage_bucket, storage_key, storage_version_id);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_journal_id_registration_seq UNIQUE (journal_id, registration_seq);

ALTER TABLE ONLY public.nomenclature_cases
    ADD CONSTRAINT uq_nomenclature_cases_year_case_index UNIQUE (year, case_index);

ALTER TABLE ONLY public.org_units
    ADD CONSTRAINT uq_org_units_code UNIQUE (code);

ALTER TABLE ONLY public.prompt_templates
    ADD CONSTRAINT uq_prompt_templates_code_version UNIQUE (code, version);

ALTER TABLE ONLY public.registration_journals
    ADD CONSTRAINT uq_registration_journals_code_year UNIQUE (code, year);

ALTER TABLE ONLY public.regulatory_chunks
    ADD CONSTRAINT uq_regulatory_chunks_source_id_index_revision_chunk_no UNIQUE (source_id, index_revision, chunk_no);

ALTER TABLE ONLY public.route_template_steps
    ADD CONSTRAINT uq_route_template_steps_template_id_stage_no_ordinal UNIQUE (template_id, stage_no, ordinal);

ALTER TABLE ONLY public.route_templates
    ADD CONSTRAINT uq_route_templates_code_version UNIQUE (code, version);

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT uq_signatures_document_version_id_signer_id_approval_step_id UNIQUE NULLS NOT DISTINCT (document_version_id, signer_id, approval_step_id);

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT uq_signatures_storage_bucket_storage_key UNIQUE (storage_bucket, storage_key);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_keycloak_sub UNIQUE (keycloak_sub);

ALTER TABLE ONLY public.version_attachments
    ADD CONSTRAINT uq_version_attachments_document_version_id_ordinal UNIQUE (document_version_id, ordinal);

CREATE INDEX ix_ai_extracted_fields_ai_run_id ON public.ai_extracted_fields USING btree (ai_run_id);

CREATE INDEX ix_ai_extracted_fields_document_version_id ON public.ai_extracted_fields USING btree (document_version_id);

CREATE INDEX ix_ai_runs_document_id ON public.ai_runs USING btree (document_id);

CREATE INDEX ix_ai_runs_document_version_id ON public.ai_runs USING btree (document_version_id);

CREATE INDEX ix_ai_runs_review_queue ON public.ai_runs USING btree (queued_at) WHERE (human_review_status = 'pending'::public.human_review_status);

CREATE INDEX ix_ai_runs_task_status ON public.ai_runs USING btree (task_type, status);

CREATE INDEX ix_approval_routes_document_id ON public.approval_routes USING btree (document_id);

CREATE INDEX ix_approval_routes_document_version_id ON public.approval_routes USING btree (document_version_id);

CREATE INDEX ix_approval_step_history_document ON public.approval_step_history USING btree (document_id, changed_at);

CREATE INDEX ix_approval_step_history_route_id ON public.approval_step_history USING btree (route_id);

CREATE INDEX ix_approval_step_history_step_id ON public.approval_step_history USING btree (step_id);

CREATE INDEX ix_approval_steps_assignee_id ON public.approval_steps USING btree (assignee_id);

CREATE INDEX ix_approval_steps_inbox ON public.approval_steps USING btree (assignee_id, status) WHERE (status = 'active'::public.step_status);

CREATE INDEX ix_approval_steps_overdue ON public.approval_steps USING btree (due_at) WHERE ((status = 'active'::public.step_status) AND (due_at IS NOT NULL));

CREATE INDEX ix_approval_steps_route_id ON public.approval_steps USING btree (route_id);

CREATE INDEX ix_audit_logs_actor_user_id ON public.audit_logs USING btree (actor_user_id);

CREATE INDEX ix_audit_logs_ai_review ON public.audit_logs USING btree (occurred_at) WHERE ((category = 'ai_analysis'::public.audit_event_category) AND requires_human_review);

CREATE INDEX ix_audit_logs_document ON public.audit_logs USING btree (document_id, occurred_at);

CREATE INDEX ix_audit_logs_entity ON public.audit_logs USING btree (entity_table, entity_id);

CREATE INDEX ix_audit_logs_occurred_brin ON public.audit_logs USING brin (occurred_at);

CREATE INDEX ix_audit_logs_violations ON public.audit_logs USING gin (violations jsonb_path_ops);

CREATE INDEX ix_compliance_checks_document_version_id ON public.compliance_checks USING btree (document_version_id);

CREATE INDEX ix_compliance_citations_chunk_id ON public.compliance_citations USING btree (chunk_id);

CREATE INDEX ix_compliance_citations_finding_id ON public.compliance_citations USING btree (finding_id);

CREATE INDEX ix_compliance_findings_check_id ON public.compliance_findings USING btree (check_id);

CREATE INDEX ix_compliance_findings_open ON public.compliance_findings USING btree (severity) WHERE (status = 'open'::public.finding_status);

CREATE INDEX ix_delegations_from_user_id ON public.delegations USING btree (from_user_id);

CREATE INDEX ix_delegations_to_user_id ON public.delegations USING btree (to_user_id);

CREATE INDEX ix_document_links_source_document_id ON public.document_links USING btree (source_document_id);

CREATE INDEX ix_document_links_target_document_id ON public.document_links USING btree (target_document_id);

CREATE INDEX ix_document_status_history_document_id ON public.document_status_history USING btree (document_id);

CREATE INDEX ix_document_status_history_timeline ON public.document_status_history USING btree (document_id, changed_at);

CREATE INDEX ix_document_versions_document_id ON public.document_versions USING btree (document_id);

CREATE INDEX ix_document_versions_sha256 ON public.document_versions USING btree (content_sha256);

CREATE INDEX ix_document_versions_text_tsv ON public.document_versions USING gin (text_tsv);

CREATE INDEX ix_documents_author_id ON public.documents USING btree (author_id);

CREATE INDEX ix_documents_document_type_id ON public.documents USING btree (document_type_id);

CREATE INDEX ix_documents_org_unit_id ON public.documents USING btree (org_unit_id);

CREATE INDEX ix_documents_registration_number ON public.documents USING btree (registration_number);

CREATE INDEX ix_documents_search_tsv ON public.documents USING gin (search_tsv);

CREATE INDEX ix_documents_status_due ON public.documents USING btree (status, due_date) WHERE (deleted_at IS NULL);

CREATE INDEX ix_documents_title_trgm ON public.documents USING gin (title public.gin_trgm_ops);

CREATE INDEX ix_nomenclature_cases_org_unit_id ON public.nomenclature_cases USING btree (org_unit_id);

CREATE INDEX ix_org_units_parent_id ON public.org_units USING btree (parent_id);

CREATE INDEX ix_regulatory_chunks_content_tsv ON public.regulatory_chunks USING gin (content_tsv);

CREATE INDEX ix_regulatory_chunks_embedding_hnsw ON public.regulatory_chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='128') WHERE is_current;

CREATE INDEX ix_regulatory_chunks_source_id ON public.regulatory_chunks USING btree (source_id);

CREATE INDEX ix_regulatory_sources_in_force ON public.regulatory_sources USING btree (level, effective_from, effective_to);

CREATE INDEX ix_route_template_steps_template_id ON public.route_template_steps USING btree (template_id);

CREATE INDEX ix_route_templates_document_type_id ON public.route_templates USING btree (document_type_id);

CREATE INDEX ix_signatures_approval_step_id ON public.signatures USING btree (approval_step_id);

CREATE INDEX ix_signatures_cert_serial ON public.signatures USING btree (cert_issuer, cert_serial);

CREATE INDEX ix_signatures_document_version_id ON public.signatures USING btree (document_version_id);

CREATE INDEX ix_signatures_signer_id ON public.signatures USING btree (signer_id);

CREATE INDEX ix_staff_assignments_org_unit_id ON public.staff_assignments USING btree (org_unit_id);

CREATE INDEX ix_staff_assignments_user_id ON public.staff_assignments USING btree (user_id);

CREATE INDEX ix_user_roles_org_unit_id ON public.user_roles USING btree (org_unit_id);

CREATE INDEX ix_user_roles_role_unit ON public.user_roles USING btree (role, org_unit_id);

CREATE INDEX ix_user_roles_user_id ON public.user_roles USING btree (user_id);

CREATE INDEX ix_users_primary_org_unit_id ON public.users USING btree (primary_org_unit_id);

CREATE INDEX ix_version_attachments_document_version_id ON public.version_attachments USING btree (document_version_id);

CREATE UNIQUE INDEX uq_approval_routes_one_open_per_document ON public.approval_routes USING btree (document_id) WHERE (status = ANY (ARRAY['draft'::public.route_status, 'active'::public.route_status]));

CREATE UNIQUE INDEX uq_audit_logs_ai_run ON public.audit_logs USING btree (ai_run_id) WHERE (ai_run_id IS NOT NULL);

CREATE UNIQUE INDEX uq_users_email_lower ON public.users USING btree (lower((email)::text));

CREATE TRIGGER trg_ai_extracted_fields_audit AFTER INSERT OR DELETE OR UPDATE ON public.ai_extracted_fields FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_ai_extracted_fields_guard_legal BEFORE UPDATE ON public.ai_extracted_fields FOR EACH ROW EXECUTE FUNCTION public.sed_guard_legal_decisions();

CREATE TRIGGER trg_ai_runs_audit AFTER INSERT OR DELETE OR UPDATE ON public.ai_runs FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_ai_runs_guard_legal BEFORE UPDATE ON public.ai_runs FOR EACH ROW EXECUTE FUNCTION public.sed_guard_legal_decisions();

CREATE CONSTRAINT TRIGGER trg_ai_runs_log_analysis AFTER INSERT OR UPDATE OF status ON public.ai_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ((new.status = 'succeeded'::public.ai_run_status)) EXECUTE FUNCTION public.sed_log_ai_analysis();

CREATE TRIGGER trg_approval_routes_audit AFTER INSERT OR DELETE OR UPDATE ON public.approval_routes FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_approval_routes_lock_version AFTER INSERT OR UPDATE OF status ON public.approval_routes FOR EACH ROW EXECUTE FUNCTION public.sed_route_lock_version();

CREATE TRIGGER trg_approval_step_history_append_only BEFORE DELETE OR UPDATE ON public.approval_step_history FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_approval_step_history_no_truncate BEFORE TRUNCATE ON public.approval_step_history FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_approval_steps_audit AFTER INSERT OR DELETE OR UPDATE ON public.approval_steps FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_approval_steps_guard_legal BEFORE UPDATE ON public.approval_steps FOR EACH ROW EXECUTE FUNCTION public.sed_guard_legal_decisions();

CREATE TRIGGER trg_approval_steps_history AFTER INSERT OR UPDATE OF status ON public.approval_steps FOR EACH ROW EXECUTE FUNCTION public.sed_approval_step_history();

CREATE TRIGGER trg_audit_logs_append_only BEFORE DELETE OR UPDATE ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_audit_logs_chain BEFORE INSERT ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.sed_audit_chain();

CREATE TRIGGER trg_audit_logs_no_truncate BEFORE TRUNCATE ON public.audit_logs FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_compliance_checks_append_only BEFORE DELETE OR UPDATE ON public.compliance_checks FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_compliance_checks_audit AFTER INSERT OR DELETE OR UPDATE ON public.compliance_checks FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_compliance_checks_no_truncate BEFORE TRUNCATE ON public.compliance_checks FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_compliance_citations_append_only BEFORE DELETE OR UPDATE ON public.compliance_citations FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_compliance_citations_no_truncate BEFORE TRUNCATE ON public.compliance_citations FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_compliance_findings_audit AFTER INSERT OR DELETE OR UPDATE ON public.compliance_findings FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_compliance_findings_guard_legal BEFORE UPDATE ON public.compliance_findings FOR EACH ROW EXECUTE FUNCTION public.sed_guard_legal_decisions();

CREATE TRIGGER trg_delegations_audit AFTER INSERT OR DELETE OR UPDATE ON public.delegations FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_document_links_audit AFTER INSERT OR DELETE OR UPDATE ON public.document_links FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_document_status_history_append_only BEFORE DELETE OR UPDATE ON public.document_status_history FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_document_status_history_no_truncate BEFORE TRUNCATE ON public.document_status_history FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_document_versions_audit AFTER INSERT OR DELETE OR UPDATE ON public.document_versions FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_document_versions_protect BEFORE DELETE OR UPDATE ON public.document_versions FOR EACH ROW EXECUTE FUNCTION public.sed_protect_document_version();

CREATE TRIGGER trg_documents_audit AFTER INSERT OR DELETE OR UPDATE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_documents_guard_legal BEFORE UPDATE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.sed_guard_legal_decisions();

CREATE TRIGGER trg_documents_status_history AFTER INSERT OR UPDATE OF status ON public.documents FOR EACH ROW EXECUTE FUNCTION public.sed_document_status_history();

CREATE TRIGGER trg_prompt_templates_append_only BEFORE DELETE OR UPDATE ON public.prompt_templates FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_prompt_templates_no_truncate BEFORE TRUNCATE ON public.prompt_templates FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_regulatory_chunks_append_only BEFORE DELETE OR UPDATE ON public.regulatory_chunks FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except('is_current', 'content_tsv');

CREATE TRIGGER trg_regulatory_chunks_no_truncate BEFORE TRUNCATE ON public.regulatory_chunks FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_route_template_steps_audit AFTER INSERT OR DELETE OR UPDATE ON public.route_template_steps FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_route_templates_audit AFTER INSERT OR DELETE OR UPDATE ON public.route_templates FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_signatures_append_only BEFORE DELETE OR UPDATE ON public.signatures FOR EACH ROW EXECUTE FUNCTION public.sed_immutable_except('validation_status', 'validation_report', 'validated_at');

CREATE TRIGGER trg_signatures_audit AFTER INSERT OR DELETE OR UPDATE ON public.signatures FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_signatures_before_insert BEFORE INSERT ON public.signatures FOR EACH ROW EXECUTE FUNCTION public.sed_signature_before_insert();

CREATE TRIGGER trg_signatures_lock_version AFTER INSERT ON public.signatures FOR EACH ROW EXECUTE FUNCTION public.sed_signature_after_insert();

CREATE TRIGGER trg_signatures_no_truncate BEFORE TRUNCATE ON public.signatures FOR EACH STATEMENT EXECUTE FUNCTION public.sed_immutable_except();

CREATE TRIGGER trg_staff_assignments_audit AFTER INSERT OR DELETE OR UPDATE ON public.staff_assignments FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_user_roles_audit AFTER INSERT OR DELETE OR UPDATE ON public.user_roles FOR EACH ROW EXECUTE FUNCTION public.sed_audit_row_change();

CREATE TRIGGER trg_user_roles_scope BEFORE INSERT OR UPDATE OF role, org_unit_id ON public.user_roles FOR EACH ROW EXECUTE FUNCTION public.sed_check_role_scope();

ALTER TABLE ONLY public.ai_extracted_fields
    ADD CONSTRAINT fk_ai_extracted_fields_ai_run_id_ai_runs FOREIGN KEY (ai_run_id) REFERENCES public.ai_runs(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_extracted_fields
    ADD CONSTRAINT fk_ai_extracted_fields_document_version_id_document_versions FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_extracted_fields
    ADD CONSTRAINT fk_ai_extracted_fields_reviewed_by_id_users FOREIGN KEY (reviewed_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_document_version_id_document_versions FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_initiated_by_id_users FOREIGN KEY (initiated_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_model_id_ai_models FOREIGN KEY (model_id) REFERENCES public.ai_models(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_prompt_template_id_prompt_templates FOREIGN KEY (prompt_template_id) REFERENCES public.prompt_templates(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.ai_runs
    ADD CONSTRAINT fk_ai_runs_reviewed_by_id_users FOREIGN KEY (reviewed_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT fk_approval_routes_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT fk_approval_routes_initiated_by_id_users FOREIGN KEY (initiated_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT fk_approval_routes_suggested_by_ai_run_id_ai_runs FOREIGN KEY (suggested_by_ai_run_id) REFERENCES public.ai_runs(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT fk_approval_routes_template_id_route_templates FOREIGN KEY (template_id) REFERENCES public.route_templates(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_routes
    ADD CONSTRAINT fk_approval_routes_version_same_document FOREIGN KEY (document_version_id, document_id) REFERENCES public.document_versions(id, document_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT fk_approval_step_history_assignee_id_users FOREIGN KEY (assignee_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT fk_approval_step_history_changed_by_id_users FOREIGN KEY (changed_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT fk_approval_step_history_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT fk_approval_step_history_route_id_approval_routes FOREIGN KEY (route_id) REFERENCES public.approval_routes(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_step_history
    ADD CONSTRAINT fk_approval_step_history_step_id_approval_steps FOREIGN KEY (step_id) REFERENCES public.approval_steps(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_assignee_id_users FOREIGN KEY (assignee_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_delegation_id_delegations FOREIGN KEY (delegation_id) REFERENCES public.delegations(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_original_assignee_id_users FOREIGN KEY (original_assignee_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_route_id_approval_routes FOREIGN KEY (route_id) REFERENCES public.approval_routes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.approval_steps
    ADD CONSTRAINT fk_approval_steps_template_step_id_route_template_steps FOREIGN KEY (template_step_id) REFERENCES public.route_template_steps(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT fk_audit_logs_actor_user_id_users FOREIGN KEY (actor_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT fk_audit_logs_ai_run_id_ai_runs FOREIGN KEY (ai_run_id) REFERENCES public.ai_runs(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_checks
    ADD CONSTRAINT fk_compliance_checks_document_version FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_checks
    ADD CONSTRAINT fk_compliance_checks_run_same_version FOREIGN KEY (ai_run_id, document_version_id) REFERENCES public.ai_runs(id, document_version_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_citations
    ADD CONSTRAINT fk_compliance_citations_chunk_id_regulatory_chunks FOREIGN KEY (chunk_id) REFERENCES public.regulatory_chunks(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_citations
    ADD CONSTRAINT fk_compliance_citations_finding_id_compliance_findings FOREIGN KEY (finding_id) REFERENCES public.compliance_findings(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_findings
    ADD CONSTRAINT fk_compliance_findings_check_id_compliance_checks FOREIGN KEY (check_id) REFERENCES public.compliance_checks(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compliance_findings
    ADD CONSTRAINT fk_compliance_findings_resolved_by_id_users FOREIGN KEY (resolved_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT fk_delegations_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT fk_delegations_document_type_id_document_types FOREIGN KEY (document_type_id) REFERENCES public.document_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT fk_delegations_from_user_id_users FOREIGN KEY (from_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.delegations
    ADD CONSTRAINT fk_delegations_to_user_id_users FOREIGN KEY (to_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_links
    ADD CONSTRAINT fk_document_links_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_links
    ADD CONSTRAINT fk_document_links_source_document_id_documents FOREIGN KEY (source_document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_links
    ADD CONSTRAINT fk_document_links_target_document_id_documents FOREIGN KEY (target_document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_status_history
    ADD CONSTRAINT fk_document_status_history_changed_by_id_users FOREIGN KEY (changed_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_status_history
    ADD CONSTRAINT fk_document_status_history_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_status_history
    ADD CONSTRAINT fk_document_status_history_document_version_id_document_3031 FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_status_history
    ADD CONSTRAINT fk_document_status_history_route_id_approval_routes FOREIGN KEY (route_id) REFERENCES public.approval_routes(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT fk_document_versions_ai_run_id_ai_runs FOREIGN KEY (ai_run_id) REFERENCES public.ai_runs(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT fk_document_versions_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT fk_document_versions_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.document_versions
    ADD CONSTRAINT fk_document_versions_parent_version_id_document_versions FOREIGN KEY (parent_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_author_id_users FOREIGN KEY (author_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_current_version_same_document FOREIGN KEY (current_version_id, id) REFERENCES public.document_versions(id, document_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_document_type_id_document_types FOREIGN KEY (document_type_id) REFERENCES public.document_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_journal_id_registration_journals FOREIGN KEY (journal_id) REFERENCES public.registration_journals(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_nomenclature_case_id_nomenclature_cases FOREIGN KEY (nomenclature_case_id) REFERENCES public.nomenclature_cases(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.nomenclature_cases
    ADD CONSTRAINT fk_nomenclature_cases_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.org_units
    ADD CONSTRAINT fk_org_units_parent_id_org_units FOREIGN KEY (parent_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.prompt_templates
    ADD CONSTRAINT fk_prompt_templates_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.registration_journals
    ADD CONSTRAINT fk_registration_journals_document_type_id_document_types FOREIGN KEY (document_type_id) REFERENCES public.document_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.regulatory_chunks
    ADD CONSTRAINT fk_regulatory_chunks_embedding_model_id_ai_models FOREIGN KEY (embedding_model_id) REFERENCES public.ai_models(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.regulatory_chunks
    ADD CONSTRAINT fk_regulatory_chunks_source_id_regulatory_sources FOREIGN KEY (source_id) REFERENCES public.regulatory_sources(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.regulatory_sources
    ADD CONSTRAINT fk_regulatory_sources_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.regulatory_sources
    ADD CONSTRAINT fk_regulatory_sources_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.regulatory_sources
    ADD CONSTRAINT fk_regulatory_sources_supersedes_id_regulatory_sources FOREIGN KEY (supersedes_id) REFERENCES public.regulatory_sources(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.route_template_steps
    ADD CONSTRAINT fk_route_template_steps_assignee_org_unit_id_org_units FOREIGN KEY (assignee_org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.route_template_steps
    ADD CONSTRAINT fk_route_template_steps_assignee_user_id_users FOREIGN KEY (assignee_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.route_template_steps
    ADD CONSTRAINT fk_route_template_steps_template_id_route_templates FOREIGN KEY (template_id) REFERENCES public.route_templates(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.route_templates
    ADD CONSTRAINT fk_route_templates_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.route_templates
    ADD CONSTRAINT fk_route_templates_document_type_id_document_types FOREIGN KEY (document_type_id) REFERENCES public.document_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.route_templates
    ADD CONSTRAINT fk_route_templates_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT fk_signatures_approval_step_id_approval_steps FOREIGN KEY (approval_step_id) REFERENCES public.approval_steps(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT fk_signatures_document_version_id_document_versions FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.signatures
    ADD CONSTRAINT fk_signatures_signer_id_users FOREIGN KEY (signer_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.staff_assignments
    ADD CONSTRAINT fk_staff_assignments_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.staff_assignments
    ADD CONSTRAINT fk_staff_assignments_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_user_roles_granted_by_id_users FOREIGN KEY (granted_by_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_user_roles_org_unit_id_org_units FOREIGN KEY (org_unit_id) REFERENCES public.org_units(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT fk_user_roles_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.users
    ADD CONSTRAINT fk_users_primary_org_unit_id_org_units FOREIGN KEY (primary_org_unit_id) REFERENCES public.org_units(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.version_attachments
    ADD CONSTRAINT fk_version_attachments_document_version_id_document_versions FOREIGN KEY (document_version_id) REFERENCES public.document_versions(id) ON DELETE RESTRICT;

