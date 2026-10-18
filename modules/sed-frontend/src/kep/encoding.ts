/** Перетворення байтів: base64, hex, ArrayBuffer. */

export function toBase64(bytes: ArrayBuffer | Uint8Array): string {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000));
  return btoa(s);
}

export function fromBase64(b64: string): Uint8Array {
  const s = atob(b64.replace(/\s+/g, ""));
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

export function fromHex(hex: string): Uint8Array {
  const h = hex.trim().toLowerCase();
  if (!/^([0-9a-f]{2})+$/.test(h)) throw new Error("Некоректний шістнадцятковий рядок");
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function toHex(bytes: ArrayBuffer | Uint8Array): string {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function toArrayBuffer(u: Uint8Array): ArrayBuffer {
  return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
}

export async function sha256Hex(data: ArrayBuffer | Uint8Array): Promise<string> {
  const buf = data instanceof Uint8Array ? toArrayBuffer(data) : data;
  return toHex(await crypto.subtle.digest("SHA-256", buf));
}

/** Файл підпису може бути DER (.p7s) або base64/PEM-текстом — повертаємо base64 DER. */
export async function signatureFileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const head = new TextDecoder("ascii", { fatal: false }).decode(bytes.subarray(0, 64)).trim();
  if (head.startsWith("-----BEGIN")) {
    const text = new TextDecoder().decode(bytes);
    const body = text.replace(/-----BEGIN [^-]+-----|-----END [^-]+-----/g, "").replace(/\s+/g, "");
    return toBase64(fromBase64(body));
  }
  if (bytes[0] !== 0x30 && /^[A-Za-z0-9+/=\s]+$/.test(head)) return toBase64(fromBase64(new TextDecoder().decode(bytes)));
  return toBase64(bytes);
}
