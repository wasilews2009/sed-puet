// @vitest-environment node
/**
 * Підпис у браузерному коді (WebCrypto) має проходити перевірку сервера: тест підписує геш ключем
 * dev/fixtures/test.p12 (видано внутрішнім ЦСК) і перевіряє контейнер утилітою `keytool verify`
 * модуля маршрутів, якщо поруч є ../approval-routing (інакше — лише структуру).
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import * as asn1js from "asn1js";
import { describe, expect, it } from "vitest";
import { signDetachedCadesBes, p1363ToDer } from "./cades";
import { loadPkcs12 } from "./pkcs12";

const fixture = resolve(__dirname, "../../dev/fixtures/test.p12");
const routing = process.env.SED_ROUTING_DIR ?? resolve(__dirname, "../../../approval-routing");
const SHA = "a".repeat(64);

describe("CAdES-BES у браузері", () => {
  it("P1363 → DER з додатковим нулем для старшого біта", () => {
    const der = p1363ToDer(Uint8Array.of(0x80, 0x01, 0x00, 0x02));
    expect(Array.from(der)).toEqual([0x30, 0x08, 0x02, 0x03, 0x00, 0x80, 0x01, 0x02, 0x01, 0x02]);
  });

  it("неправильний пароль — зрозуміла помилка", async () => {
    const buf = readFileSync(fixture);
    await expect(loadPkcs12(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), "wrong-password-123")).rejects.toThrow(/пароль/);
  });

  it("підпис внутрішнім ключем проходить перевірку сервера", async () => {
    const buf = readFileSync(fixture);
    const key = await loadPkcs12(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), "Strong-Pass-2026!xyz");
    expect(key.algorithm).toBe("ECDSA");
    expect(key.subjectCn).toContain("КОВАЛЬ");
    const p7s = await signDetachedCadesBes(key, { contentSha256: SHA });
    expect(asn1js.fromBER(p7s.slice().buffer).offset).not.toBe(-1);

    if (!existsSync(join(routing, "sed_routing"))) return;
    const dir = mkdtempSync(join(tmpdir(), "cades-"));
    writeFileSync(join(dir, "s.p7s"), p7s);
    const out = execFileSync(
      "python3",
      ["-m", "sed_routing.keytool", "verify", "--sig", join(dir, "s.p7s"), "--sha256", SHA, "--ca", resolve(__dirname, "../../dev/fixtures/ca.pem")],
      { cwd: routing, env: { ...process.env, PYTHONPATH: `.:../db-schema` }, encoding: "utf8" },
    );
    const report = JSON.parse(out) as { status: string };
    expect(report.status).toBe("valid");
  }, 60_000);
});
