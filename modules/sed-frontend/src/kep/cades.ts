/**
 * Відокремлений підпис CAdES-BES (CMS SignedData, RFC 5652 + ETSI EN 319 122-1) у браузері.
 * Підписується не файл, а його SHA-256 (message-digest), який повідомив сервер: той самий контракт,
 * що й у `sed_routing.kep.cades.build_detached_cades_bes` і утиліті `keytool sign`.
 *
 * Підписані атрибути: content-type, signing-time, message-digest, signing-certificate-v2.
 * Криптографія — WebCrypto (ECDSA P-256/P-384 або RSA PKCS#1 v1.5, SHA-256); ключ неекспортований.
 */
import * as asn1js from "asn1js";
import { fromHex, toArrayBuffer } from "./encoding";
import type { LoadedKey } from "./pkcs12";

const OID = {
  data: "1.2.840.113549.1.7.1",
  signedData: "1.2.840.113549.1.7.2",
  contentType: "1.2.840.113549.1.9.3",
  messageDigest: "1.2.840.113549.1.9.4",
  signingTime: "1.2.840.113549.1.9.5",
  signingCertificateV2: "1.2.840.113549.1.9.16.2.47",
  sha256: "2.16.840.1.101.3.4.2.1",
  ecdsaSha256: "1.2.840.10045.4.3.2",
  rsaSha256: "1.2.840.113549.1.1.11",
};

const oid = (v: string) => new asn1js.ObjectIdentifier({ value: v });
const algId = (v: string, nullParams = false) =>
  new asn1js.Sequence({ value: nullParams ? [oid(v), new asn1js.Null()] : [oid(v)] });
const reparse = (b: ArrayBuffer) => asn1js.fromBER(b).result;

function attribute(type: string, value: asn1js.AsnType): asn1js.Sequence {
  return new asn1js.Sequence({ value: [oid(type), new asn1js.Set({ value: [value] })] });
}

function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return a[i]! - b[i]!;
  return a.length - b.length;
}

/** DER: елементи SET OF упорядковуються за їхнім кодуванням (X.690 §11.6). */
function derSetOf(items: asn1js.AsnType[]): ArrayBuffer[] {
  return items.map((x) => x.toBER(false)).sort((a, b) => compareBytes(new Uint8Array(a), new Uint8Array(b)));
}

function concat(parts: ArrayBuffer[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.byteLength, 0));
  let o = 0;
  for (const p of parts) {
    out.set(new Uint8Array(p), o);
    o += p.byteLength;
  }
  return out;
}

/** Довжина DER (короткий/довгий формат). */
function derLength(n: number): Uint8Array {
  if (n < 0x80) return Uint8Array.of(n);
  const bytes: number[] = [];
  while (n > 0) {
    bytes.unshift(n & 0xff);
    n >>= 8;
  }
  return Uint8Array.of(0x80 | bytes.length, ...bytes);
}

function tlv(tag: number, content: Uint8Array): Uint8Array {
  return concat([toArrayBuffer(Uint8Array.of(tag)), toArrayBuffer(derLength(content.length)), toArrayBuffer(content)]);
}

/** WebCrypto повертає ECDSA-підпис як r‖s (IEEE P1363); CMS вимагає DER SEQUENCE { r INTEGER, s INTEGER }. */
export function p1363ToDer(sig: Uint8Array): Uint8Array {
  const half = sig.length / 2;
  const int = (x: Uint8Array) => {
    let i = 0;
    while (i < x.length - 1 && x[i] === 0) i++;
    let v = x.subarray(i);
    if (v[0]! & 0x80) v = Uint8Array.of(0, ...v);
    return tlv(0x02, v);
  };
  return tlv(0x30, concat([toArrayBuffer(int(sig.subarray(0, half))), toArrayBuffer(int(sig.subarray(half)))]));
}

function utcTime(d: Date): asn1js.UTCTime {
  const t = new Date(Math.floor(d.getTime() / 1000) * 1000);
  return new asn1js.UTCTime({ valueDate: t });
}

export interface CadesOptions {
  /** SHA-256 вмісту (hex), який сервер очікує в message-digest. */
  contentSha256: string;
  signingTime?: Date;
}

/** Повертає DER контейнера .p7s. */
export async function signDetachedCadesBes(key: LoadedKey, opts: CadesOptions): Promise<Uint8Array> {
  const digest = fromHex(opts.contentSha256);
  if (digest.length !== 32) throw new Error("Очікується SHA-256 (64 шістнадцяткові символи)");
  const certDer = key.certificateDer;
  const certHash = await crypto.subtle.digest("SHA-256", certDer);
  const issuerDer = key.certificate.issuer.toSchema().toBER(false);
  const serialDer = key.certificate.serialNumber.toBER(false);

  const essCertIdV2 = new asn1js.Sequence({
    value: [
      algId(OID.sha256),
      new asn1js.OctetString({ valueHex: certHash }),
      new asn1js.Sequence({
        value: [
          new asn1js.Sequence({ value: [new asn1js.Constructed({ idBlock: { tagClass: 3, tagNumber: 4 }, value: [reparse(issuerDer)] })] }),
          reparse(serialDer),
        ],
      }),
    ],
  });
  const attrs = derSetOf([
    attribute(OID.contentType, oid(OID.data)),
    attribute(OID.signingTime, utcTime(opts.signingTime ?? new Date())),
    attribute(OID.messageDigest, new asn1js.OctetString({ valueHex: toArrayBuffer(digest) })),
    attribute(OID.signingCertificateV2, new asn1js.Sequence({ value: [new asn1js.Sequence({ value: [essCertIdV2] })] })),
  ]);
  const attrsContent = concat(attrs);
  const toBeSigned = tlv(0x31, attrsContent); // SET OF — саме ці байти підписуються
  const signedAttrsImplicit = tlv(0xa0, attrsContent); // [0] IMPLICIT у SignerInfo

  let signature: Uint8Array;
  let sigAlg: asn1js.Sequence;
  if (key.algorithm === "ECDSA") {
    const raw = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key.privateKey, toArrayBuffer(toBeSigned)));
    signature = p1363ToDer(raw);
    sigAlg = algId(OID.ecdsaSha256);
  } else {
    signature = new Uint8Array(await crypto.subtle.sign({ name: "RSASSA-PKCS1-v1_5" }, key.privateKey, toArrayBuffer(toBeSigned)));
    sigAlg = algId(OID.rsaSha256, true);
  }

  const enc = (x: asn1js.AsnType) => x.toBER(false);
  const signerInfo = tlv(
    0x30,
    concat([
      enc(new asn1js.Integer({ value: 1 })),
      enc(new asn1js.Sequence({ value: [reparse(issuerDer), reparse(serialDer)] })),
      enc(algId(OID.sha256)),
      toArrayBuffer(signedAttrsImplicit),
      enc(sigAlg),
      enc(new asn1js.OctetString({ valueHex: toArrayBuffer(signature) })),
    ]),
  );
  const signedData = tlv(
    0x30,
    concat([
      enc(new asn1js.Integer({ value: 1 })),
      toArrayBuffer(tlv(0x31, new Uint8Array(enc(algId(OID.sha256))))),
      enc(new asn1js.Sequence({ value: [oid(OID.data)] })), // encapContentInfo без eContent — відокремлений підпис
      toArrayBuffer(tlv(0xa0, new Uint8Array(certDer))), // certificates [0] IMPLICIT SET OF
      toArrayBuffer(tlv(0x31, signerInfo)),
    ]),
  );
  return tlv(0x30, concat([enc(oid(OID.signedData)), toArrayBuffer(tlv(0xa0, signedData))]));
}
