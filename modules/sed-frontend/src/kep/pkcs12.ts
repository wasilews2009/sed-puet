/**
 * Розбір файлу-ключа PKCS#12 (.p12/.pfx) у браузері: особистий ключ не залишає комп'ютер користувача.
 * Підтримка: PBES2 (PBKDF2-HMAC-SHA256 + AES-CBC) — так видає ключі внутрішній ЦСК АІ-СЕД ПУЕТ;
 * ключі ECDSA P-256/P-384 і RSA. Ключі ДСТУ 4145 (Key-6.dat, JKS ПриватБанку) тут не розбираються:
 * для них потрібна бібліотека ІІТ «Користувач ЦСК» (див. README, адаптер `KepSigner`).
 */
import * as asn1js from "asn1js";
import * as pkijs from "pkijs";
import { toArrayBuffer } from "./encoding";

export interface LoadedKey {
  privateKey: CryptoKey;
  certificate: pkijs.Certificate;
  certificateDer: ArrayBuffer;
  algorithm: "ECDSA" | "RSA";
  subjectCn: string;
  notAfter: Date;
}

export class KeyFileError extends Error {}

const OID = {
  pkcs8ShroudedKeyBag: "1.2.840.113549.1.12.10.1.2",
  keyBag: "1.2.840.113549.1.12.10.1.1",
  certBag: "1.2.840.113549.1.12.10.1.3",
  x509Certificate: "1.2.840.113549.1.9.22.1",
  ecPublicKey: "1.2.840.10045.2.1",
  rsaEncryption: "1.2.840.113549.1.1.1",
  p256: "1.2.840.10045.3.1.7",
  p384: "1.3.132.0.34",
  cn: "2.5.4.3",
};

let engine: pkijs.ICryptoEngine | null = null;
/** Явний рушій WebCrypto (у браузері й у Node 20+ однаковий `globalThis.crypto`). */
function cryptoEngine(): pkijs.ICryptoEngine {
  engine ??= new pkijs.CryptoEngine({ name: "webcrypto", crypto: globalThis.crypto });
  return engine;
}

function parseDer(buf: ArrayBuffer, what: string) {
  const asn = asn1js.fromBER(buf);
  if (asn.offset === -1) throw new KeyFileError(`Файл не є ${what}`);
  return asn.result;
}

export function commonName(name: pkijs.RelativeDistinguishedNames): string {
  const tv = name.typesAndValues.find((x) => x.type === OID.cn);
  return tv ? String((tv.value as asn1js.BaseStringBlock).valueBlock.value) : "";
}

async function importPrivateKey(pki: pkijs.PrivateKeyInfo): Promise<{ key: CryptoKey; algorithm: "ECDSA" | "RSA" }> {
  const der = pki.toSchema().toBER(false);
  const alg = pki.privateKeyAlgorithm.algorithmId;
  if (alg === OID.ecPublicKey) {
    const params = pki.privateKeyAlgorithm.algorithmParams as asn1js.ObjectIdentifier | undefined;
    const curveOid = params?.valueBlock?.toString();
    const namedCurve = curveOid === OID.p384 ? "P-384" : curveOid === OID.p256 ? "P-256" : null;
    if (!namedCurve) throw new KeyFileError("Крива ключа не підтримується браузером (потрібна P-256 або P-384)");
    const key = await crypto.subtle.importKey("pkcs8", der, { name: "ECDSA", namedCurve }, false, ["sign"]);
    return { key, algorithm: "ECDSA" };
  }
  if (alg === OID.rsaEncryption) {
    const key = await crypto.subtle.importKey("pkcs8", der, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
    return { key, algorithm: "RSA" };
  }
  throw new KeyFileError(
    "Алгоритм ключа не підтримується в браузері. Ключі ДСТУ 4145 (Дія, ПриватБанк, Key-6.dat) підписуйте застосунком «ІІТ Користувач ЦСК» або Дія.Підпис і завантажте файл підпису .p7s",
  );
}

/** Відкриває PKCS#12 паролем і повертає ключ підпису (неекспортований CryptoKey) і сертифікат. */
export async function loadPkcs12(file: ArrayBuffer, password: string): Promise<LoadedKey> {
  const pwd = toArrayBuffer(new TextEncoder().encode(password));
  const pfx = new pkijs.PFX({ schema: parseDer(file, "файлом PKCS#12") });
  try {
    await pfx.parseInternalValues({ password: pwd, checkIntegrity: true }, cryptoEngine());
  } catch {
    throw new KeyFileError("Неправильний пароль або пошкоджений файл ключа");
  }
  const safe = pfx.parsedValue?.authenticatedSafe;
  if (!safe) throw new KeyFileError("Файл ключа не містить даних");
  try {
    await safe.parseInternalValues({ safeContents: safe.safeContents.map(() => ({ password: pwd })) }, cryptoEngine());
  } catch {
    throw new KeyFileError("Неправильний пароль або непідтримуване шифрування файлу ключа");
  }

  let pki: pkijs.PrivateKeyInfo | null = null;
  const certs: pkijs.Certificate[] = [];
  for (const sc of (safe.parsedValue?.safeContents ?? []) as { value: pkijs.SafeContents }[]) {
    for (const bag of sc.value.safeBags) {
      if (bag.bagId === OID.pkcs8ShroudedKeyBag) {
        const shrouded = bag.bagValue as pkijs.PKCS8ShroudedKeyBag;
        // метод у типах pkijs позначено protected, але саме він розшифровує EncryptedPrivateKeyInfo
        await (shrouded as unknown as { parseInternalValues(p: { password: ArrayBuffer }, c: pkijs.ICryptoEngine): Promise<void> })
          .parseInternalValues({ password: pwd }, cryptoEngine());
        pki = shrouded.parsedValue ?? null;
      } else if (bag.bagId === OID.keyBag) {
        pki = bag.bagValue as unknown as pkijs.PrivateKeyInfo;
      } else if (bag.bagId === OID.certBag) {
        const cb = bag.bagValue as pkijs.CertBag;
        if (cb.certId === OID.x509Certificate && cb.parsedValue instanceof pkijs.Certificate) certs.push(cb.parsedValue);
      }
    }
  }
  if (!pki) throw new KeyFileError("У файлі немає особистого ключа");
  if (!certs.length) throw new KeyFileError("У файлі немає сертифіката");
  const { key, algorithm } = await importPrivateKey(pki);

  // сертифікат підписувача — той, чий відкритий ключ відповідає особистому; решта — ланцюжок ЦСК
  const pub = await publicKeyFingerprint(pki);
  let signer: pkijs.Certificate | undefined;
  for (const c of certs) {
    const spki = c.subjectPublicKeyInfo.subjectPublicKey.valueBlock.valueHexView;
    if (pub && equalBytes(spki, pub)) signer = c;
  }
  signer ??= certs.find((c) => !isCa(c)) ?? certs[0]!;
  return {
    privateKey: key,
    certificate: signer,
    certificateDer: signer.toSchema().toBER(false),
    algorithm,
    subjectCn: commonName(signer.subject),
    notAfter: signer.notAfter.value,
  };
}

function isCa(c: pkijs.Certificate): boolean {
  const bc = c.extensions?.find((e) => e.extnID === "2.5.29.19")?.parsedValue as pkijs.BasicConstraints | undefined;
  return !!bc?.cA;
}

/** Відкритий ключ EC з PKCS#8 (поле publicKey в ECPrivateKey), щоб знайти сертифікат підписувача. */
async function publicKeyFingerprint(pki: pkijs.PrivateKeyInfo): Promise<Uint8Array | null> {
  if (pki.privateKeyAlgorithm.algorithmId !== OID.ecPublicKey) return null;
  try {
    const inner = asn1js.fromBER(pki.privateKey.valueBlock.valueHexView.slice().buffer);
    const seq = inner.result as asn1js.Sequence;
    for (const el of seq.valueBlock.value) {
      if (el.idBlock.tagClass === 3 && el.idBlock.tagNumber === 1) {
        const bits = (el as asn1js.Constructed).valueBlock.value[0] as asn1js.BitString;
        return new Uint8Array(bits.valueBlock.valueHexView);
      }
    }
  } catch {
    /* ключ без відкритої частини — шукатимемо інакше */
  }
  return null;
}

function equalBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
