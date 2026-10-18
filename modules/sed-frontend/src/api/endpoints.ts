/**
 * Виклики API sed-app, згруповані за модулями. Типи запитів і відповідей — з OpenAPI,
 * тому зміна контракту на бекенді ламає збірку тут, а не користувача в браузері.
 */
import { fetchBlob, http, unwrap } from "./client";
import type {
  ActIn,
  AuthConfig,
  DocumentSort,
  DocumentStatus,
  FieldDecision,
  KeyFormat,
  MockSignature,
  SignatureProvider,
} from "./types";

export const authApi = {
  async config(): Promise<AuthConfig> {
    const r = await fetch("/api/v1/auth/config");
    if (!r.ok) throw new Error("Сервер СЕД недоступний");
    return r.json();
  },
  devUsers: () => unwrap(http.GET("/api/v1/auth/dev-users")),
  devLogin: (user_id: string) => unwrap(http.POST("/api/v1/auth/dev-login", { body: { user_id } })),
  me: () => unwrap(http.GET("/api/v1/me")),
  dictionaries: () => unwrap(http.GET("/api/v1/dictionaries")),
};

export interface DocumentFilter {
  q?: string;
  status?: DocumentStatus | "";
  type_code?: string;
  org_unit_id?: string;
  needs_review?: boolean;
  mine?: boolean;
  sort?: DocumentSort;
  limit?: number;
  offset?: number;
}

const clean = <T extends object>(o: T): T =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v !== undefined && v !== null && v !== false)) as T;

export const documentsApi = {
  list: (f: DocumentFilter) =>
    unwrap(http.GET("/api/v1/documents", { params: { query: clean(f) as never } })),
  card: (id: string) => unwrap(http.GET("/api/v1/documents/{document_id}", { params: { path: { document_id: id } } })),
  file: (docId: string, versionId: string) => fetchBlob(`/api/v1/documents/${docId}/versions/${versionId}/file`),
  ingest: (form: FormData) =>
    unwrap(
      http.POST("/api/v1/documents/ingest", {
        body: form as never,
        bodySerializer: (b) => b as unknown as FormData,
      }),
    ),
  reviewField: (docId: string, fieldId: string, body: { decision: FieldDecision; value?: unknown; comment?: string | null; row_version?: number }) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/fields/{field_id}/review", {
        params: { path: { document_id: docId, field_id: fieldId } },
        body,
      }),
    ),
  acceptConfident: (docId: string, row_version: number) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/requisites/accept-confident", {
        params: { path: { document_id: docId } },
        body: { row_version },
      }),
    ),
  saveRequisites: (docId: string, values: Record<string, unknown>, row_version: number) =>
    unwrap(
      http.PUT("/api/v1/documents/{document_id}/requisites", {
        params: { path: { document_id: docId } },
        body: { values, row_version },
      }),
    ),
  resolveFinding: (docId: string, findingId: string, status: "accepted" | "dismissed" | "fixed", comment: string | null) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/findings/{finding_id}/resolution", {
        params: { path: { document_id: docId, finding_id: findingId } },
        body: { status, comment },
      }),
    ),
  reviewRun: (docId: string, runId: string, decision: FieldDecision, comment: string | null) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/ai-runs/{run_id}/review", {
        params: { path: { document_id: docId, run_id: runId } },
        body: { decision, comment },
      }),
    ),
  runCompliance: (docId: string, as_of?: string | null) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/compliance-checks", {
        params: { path: { document_id: docId } },
        body: { as_of: as_of || null },
      }),
    ),
  reviewQueue: () => unwrap(http.GET("/api/v1/review-queue")),
  inbox: () => unwrap(http.GET("/api/v1/approvals/inbox")),
};

export const routingApi = {
  preview: (docId: string) =>
    unwrap(http.POST("/api/v1/documents/{document_id}/route-preview", { params: { path: { document_id: docId } }, body: {} })),
  create: (docId: string, acknowledge_findings: boolean) =>
    unwrap(
      http.POST("/api/v1/documents/{document_id}/routes", {
        params: { path: { document_id: docId } },
        body: { start: true, acknowledge_findings },
      }),
    ),
  documentRoute: (docId: string) =>
    unwrap(http.GET("/api/v1/documents/{document_id}/route", { params: { path: { document_id: docId } } })),
  cancel: (routeId: string, reason: string) =>
    unwrap(http.POST("/api/v1/routes/{route_id}/cancel", { params: { path: { route_id: routeId } }, body: { reason } })),
  signingRequest: (stepId: string) =>
    unwrap(http.GET("/api/v1/steps/{step_id}/signing-request", { params: { path: { step_id: stepId } } })),
  sign: (
    stepId: string,
    body: { signature_base64: string; provider: SignatureProvider; decision: "approved" | "approved_with_remarks"; comment: string | null },
  ) => unwrap(http.POST("/api/v1/steps/{step_id}/sign", { params: { path: { step_id: stepId } }, body })),
  decide: (stepId: string, decision: "approved" | "approved_with_remarks" | "rejected", comment: string | null) =>
    unwrap(http.POST("/api/v1/steps/{step_id}/decision", { params: { path: { step_id: stepId } }, body: { decision, comment } })),
  /** Лише стенд розробника (SED_ROUTING_KEP_ALLOW_MOCK): імітаційна КЕП без юридичної сили. */
  async mockSignature(stepId: string): Promise<MockSignature> {
    const r = await (http as unknown as { POST: (p: string, o: object) => Promise<{ data?: MockSignature }> }).POST(
      `/api/v1/dev/steps/${stepId}/mock-signature`,
      { body: {} },
    );
    return r.data as MockSignature;
  },
};

export const regulationsApi = {
  search: (q: string, as_of?: string, k = 10) =>
    unwrap(http.GET("/api/v1/regulations/search", { params: { query: { q, k, ...(as_of ? { as_of } : {}) } } })),
  checkText: (text: string, as_of?: string | null) =>
    unwrap(http.POST("/api/v1/compliance/check", { body: { text, as_of: as_of || null } })),
  ingestAct: (act: ActIn, text: string, text_quality = 1) =>
    unwrap(http.POST("/api/v1/regulations", { body: { act, text, text_quality } })),
  updateStatus: (sourceId: string, act: ActIn) =>
    unwrap(http.PUT("/api/v1/regulations/{source_id}/status", { params: { path: { source_id: sourceId } }, body: act })),
};

export const kepApi = {
  challenge: () => unwrap(http.POST("/api/v1/kep/challenges")),
  bind: (challenge_token: string, signature_base64: string, key_format: KeyFormat | null) =>
    unwrap(http.POST("/api/v1/kep/keys", { body: { challenge_token, signature_base64, key_format } })),
  myKeys: (include_revoked: boolean) =>
    unwrap(http.GET("/api/v1/kep/keys", { params: { query: { include_revoked } } })),
  revoke: (bindingId: string, reason: string) =>
    unwrap(http.POST("/api/v1/kep/keys/{binding_id}/revoke", { params: { path: { binding_id: bindingId } }, body: { reason } })),
  inspectCertificate: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return unwrap(
      http.POST("/api/v1/kep/certificates/inspect", { body: fd as never, bodySerializer: (b) => b as unknown as FormData }),
    );
  },
  verifySignature: (signature_base64: string, content_sha256: string) =>
    unwrap(http.POST("/api/v1/kep/signatures/verify", { body: { signature_base64, content_sha256 } })),
  caChain: () => fetchBlob("/api/v1/kep/internal/ca.pem"),
  crl: () => fetchBlob("/api/v1/kep/internal/crl"),
  enrollCsr: (csr_pem: string) => unwrap(http.POST("/api/v1/kep/internal/certificates/csr", { body: { csr_pem } })),
  keyFile: (password: string) =>
    fetchBlob("/api/v1/kep/internal/key-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    }),
  revokeInternal: (serial: string, reason: string) =>
    unwrap(
      http.POST("/api/v1/kep/internal/certificates/{serial}/revoke", { params: { path: { serial } }, body: { reason } }),
    ),
};
