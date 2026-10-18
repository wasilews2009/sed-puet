/** Короткі імена типів, згенерованих з OpenAPI sed-app (`npm run gen:api`). */
import type { components } from "./schema";

type S = components["schemas"];

export type AuthConfig = S["AuthConfigOut"];
export type DevUser = S["DevUserOut"];
export type Me = S["MeOut"];
export type Role = S["RoleOut"];
export type UserRole = S["UserRole"];
export type Dictionaries = S["DictionariesOut"];
export type OrgUnit = S["OrgUnitOut"];
export type DocumentType = S["DocumentTypeOut"];
export type DocumentPage = S["DocumentPage"];
export type DocumentListItem = S["DocumentListItem"];
export type DocumentStatus = S["DocumentStatus"];
export type DocumentSort = S["DocumentSort"];
export type DocumentCard = S["DocumentCard"];
export type ExtractedField = S["ExtractedFieldOut"];
export type AiRun = S["AiRunOut"];
export type ComplianceOut = S["ComplianceOut"];
export type FindingOut = S["FindingOut"];
export type RouteOut = S["RouteOut"];
export type StepOut = S["StepOut"];
export type VersionOut = S["VersionOut"];
export type SignatureOut = S["SignatureOut"];
export type StatusChange = S["StatusChangeOut"];
export type IngestResponse = S["IngestResponse"];
export type ReviewQueueItem = S["ReviewQueueItem"];
export type InboxItem = S["sed_app__api__schemas__InboxItem"];
export type RoutePlan = S["RoutePlanView"];
export type RouteView = S["RouteView"];
export type SigningRequest = S["SigningRequestView"];
export type SignatureProvider = S["SignatureProvider"];
export type FieldDecision = S["FieldDecision"];
export type ComplianceReport = S["ComplianceReport"];
export type DraftFinding = S["Finding"];
export type SearchResponse = S["SearchResponse"];
export type SearchHit = S["SearchHit"];
export type ActIn = S["ActIn"];
export type RegulatoryLevel = S["RegulatoryLevel"];
export type RegulatoryStatus = S["RegulatoryStatus"];
export type IngestActResponse = S["IngestActResponse"];
export type Challenge = S["ChallengeView"];
export type KeyBinding = S["KeyBindingView"];
export type KeyFormat = NonNullable<S["KeyRegisterRequest"]["key_format"]>;
export type CertificateCheck = S["CertificateCheckView"];
export type SignatureVerify = S["SignatureVerifyView"];
export type InternalCert = S["InternalCertView"];

/** Відповідь dev-ендпоінта імітаційної КЕП (не входить у production-специфікацію). */
export interface MockSignature {
  signature_base64: string;
  certificate_subject: string;
}
