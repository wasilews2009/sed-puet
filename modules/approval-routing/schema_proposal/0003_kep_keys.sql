-- Запит на зміну схеми для треду «Схема БД для СЕД» (власник ../db-schema).
-- Ключі КЕП користувачів і внутрішній ЦСК «АІ-СЕД ПУЕТ». Тести approval-routing накочують цей файл
-- поверх міграцій 0001–0002, тож DDL перевірено на PostgreSQL 16.

CREATE TYPE kep_key_level AS ENUM ('qualified', 'internal');

-- Який сертифікат закріплено за користувачем (після доказу володіння ключем або видачі
-- внутрішнім ЦСК). РНОКПП — лише HMAC.
CREATE TABLE user_kep_identities (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    level                   kep_key_level NOT NULL,
    cert_fingerprint_sha256 char(64) NOT NULL CHECK (cert_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
    cert_serial             varchar(128) NOT NULL,
    cert_issuer             varchar(512) NOT NULL,
    cert_subject_cn         varchar(512) NOT NULL,
    cert_valid_from         timestamptz NOT NULL,
    cert_valid_to           timestamptz NOT NULL,
    tax_id_hmac             varchar(64),
    key_format              varchar(32),
    bound_via               varchar(32) NOT NULL CHECK (bound_via IN ('proof_of_possession', 'internal_issuance')),
    bound_at                timestamptz NOT NULL DEFAULT now(),
    revoked_at              timestamptz,
    revoke_reason           varchar(256),
    CONSTRAINT ck_user_kep_identities_qualified_tax CHECK (level <> 'qualified' OR tax_id_hmac IS NOT NULL),
    CONSTRAINT ck_user_kep_identities_period CHECK (cert_valid_to > cert_valid_from),
    CONSTRAINT ck_user_kep_identities_revoke_reason CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE UNIQUE INDEX uq_user_kep_identities_active_cert
    ON user_kep_identities (cert_fingerprint_sha256) WHERE revoked_at IS NULL;
CREATE INDEX ix_user_kep_identities_user ON user_kep_identities (user_id) WHERE revoked_at IS NULL;

-- Реєстр сертифікатів, виданих внутрішнім ЦСК (джерело для CRL і статусу при перевірці підпису).
-- Особистих ключів тут немає й ніколи не буде.
CREATE TABLE internal_certificates (
    serial_hex              varchar(64) PRIMARY KEY,
    user_id                 uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    cert_fingerprint_sha256 char(64) NOT NULL UNIQUE,
    cert_der                bytea NOT NULL,
    not_before              timestamptz NOT NULL,
    not_after               timestamptz NOT NULL,
    delivery                varchar(16) NOT NULL CHECK (delivery IN ('csr', 'server_p12')),
    issued_at               timestamptz NOT NULL DEFAULT now(),
    revoked_at              timestamptz,
    revocation_reason       varchar(32) CHECK (revocation_reason IN
                              ('unspecified', 'key_compromise', 'affiliation_changed', 'superseded',
                               'cessation_of_operation', 'certificate_hold')),
    revoked_by_id           uuid REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT ck_internal_certificates_period CHECK (not_after > not_before),
    CONSTRAINT ck_internal_certificates_revocation CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);
-- Один чинний внутрішній ключ на користувача (новий випуск відкликає попередній як superseded).
CREATE UNIQUE INDEX uq_internal_certificates_one_active
    ON internal_certificates (user_id) WHERE revoked_at IS NULL;

-- Відкликання незворотне; інші поля незмінні (тригер з ../db-schema/sed_db/ddl.py).
CREATE TRIGGER trg_internal_certificates_append_only
    BEFORE UPDATE OR DELETE ON internal_certificates
    FOR EACH ROW EXECUTE FUNCTION sed_immutable_except('revoked_at', 'revocation_reason', 'revoked_by_id');
CREATE TRIGGER trg_user_kep_identities_append_only
    BEFORE UPDATE OR DELETE ON user_kep_identities
    FOR EACH ROW EXECUTE FUNCTION sed_immutable_except('revoked_at', 'revoke_reason');

-- Постачальник «внутрішній ключ СЕД» у журналі підписів.
ALTER TYPE signature_provider ADD VALUE IF NOT EXISTS 'sed_internal_key';
