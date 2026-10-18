import { describe, expect, it } from "vitest";
import type { Dictionaries, Me } from "../api/types";
import { dispositionName, errorMessage } from "../api/client";
import { navigation } from "./access";
import { fmtDate, fmtPercent, toScore } from "./format";
import { makeLabeler, toneOf } from "./labels";
import { passwordProblems } from "./password";
import { parsePeople, peopleText, requisiteText } from "./requisites";

const dict = {
  org_units: [],
  document_types: [],
  labels: { DocumentStatus: { approved: "Погоджено" } },
  field_labels: {},
  confidence_threshold: "0.85",
  modules: { ingest: true, compliance: true, routing: true, kep_mock: false },
} as unknown as Dictionaries;

const me = (roles: string[], upload = true): Me =>
  ({
    id: "u",
    display_name: "Андрій МЕЛЬНИК",
    email: "a@puet.edu.ua",
    primary_org_unit_id: null,
    roles: roles.map((role) => ({ role, role_label: role, org_unit_id: null, org_unit_name: null, scope_note: null, is_acting: false })),
    permissions: { upload_all_units: false, upload_unit_ids: upload ? ["x"] : [], corpus_admin: false, view_all_documents: false },
  }) as unknown as Me;

describe("навігація за ролями", () => {
  it("студент не бачить ключів підпису, викладач бачить", () => {
    expect(navigation(me(["student"]), dict).map((n) => n.key)).not.toContain("keys");
    expect(navigation(me(["lecturer"]), dict).map((n) => n.key)).toContain("keys");
  });
  it("без права реєстрації немає «Зареєструвати» і перевірки проєкту", () => {
    const keys = navigation(me(["staff"], false), dict).map((n) => n.key);
    expect(keys).not.toContain("upload");
    expect(keys).not.toContain("draft-check");
  });
  it("без ролей немає черги перевірки ШІ", () => {
    expect(navigation(me([]), dict).map((n) => n.key)).not.toContain("review");
  });
});

describe("реквізити", () => {
  it("дата ISO → дд.мм.рррр, особа без ПІБ", () => {
    expect(requisiteText("2026-10-17")).toBe("17.10.2026");
    expect(requisiteText({ full_name: null, position: "Завідувач кафедри", org_unit: null })).toBe("ПІБ не розпізнано, Завідувач кафедри");
  });
  it("зацікавлені особи: рядки ↔ список", () => {
    const p = parsePeople("Іван ПЕТРЕНКО, доцент\n\nОльга КОВАЛЬ");
    expect(p).toEqual([{ full_name: "Іван ПЕТРЕНКО", position: "доцент" }, { full_name: "Ольга КОВАЛЬ", position: null }]);
    expect(peopleText(p)).toBe("Іван ПЕТРЕНКО, доцент\nОльга КОВАЛЬ");
  });
});

describe("форматування і підписи", () => {
  it("впевненість з рядка-десяткового", () => {
    expect(toScore("0.912")).toBeCloseTo(0.912);
    expect(toScore(null)).toBeNull();
    expect(fmtPercent(0.849)).toBe("85%");
  });
  it("дата без часу не з'їжджає на добу", () => {
    expect(fmtDate("2026-01-01")).toBe("01.01.2026");
  });
  it("підписи з довідника, тон за значенням", () => {
    const L = makeLabeler(dict);
    expect(L("DocumentStatus", "approved")).toBe("Погоджено");
    expect(L("RegulatoryLevel", "law")).toBe("Закон");
    expect(toneOf("SignatureValidation", "invalid")).toBe("danger");
  });
});

describe("пароль до файлу-ключа (та сама політика, що на сервері)", () => {
  it("відхиляє короткий, одноманітний і з ім'ям/поштою", () => {
    expect(passwordProblems("Short1!", "Андрій КОВАЛЬ", "a.koval@puet.edu.ua")).toContain("щонайменше 12 символів");
    expect(passwordProblems("alllowercase-long", "Андрій КОВАЛЬ", "a@x.ua").length).toBeGreaterThan(0);
    expect(passwordProblems("koval-Kafedra-2026", "Андрій КОВАЛЬ", "a.koval@puet.edu.ua")).toContain("без імені, прізвища чи пошти");
    expect(passwordProblems("Kafedra-Visa-2026!", "Наталія КРАВЕЦЬ", "head.kknit@demo.puet.local")).toEqual([]);
  });
});

describe("помилки API", () => {
  it("detail-рядок і список помилок Pydantic", () => {
    expect(errorMessage({ detail: "Немає доступу" }, 403)).toBe("Немає доступу");
    expect(errorMessage({ detail: [{ msg: "Value error, Поганий формат" }, { msg: "Інше" }] }, 422)).toBe("Поганий формат; Інше");
    expect(errorMessage(null, 503)).toMatch(/недоступний/);
  });
  it("ім'я файлу з Content-Disposition (UTF-8)", () => {
    expect(dispositionName(`attachment; filename="x.p12"; filename*=UTF-8''%D0%9A%D0%BE%D0%B2%D0%B0%D0%BB%D1%8C.p12`)).toBe("Коваль.p12");
  });
});
