/**
 * Точка розширення для засобів підпису КЕП. Сервер приймає лише відокремлений CAdES над SHA-256
 * версії документа (або виклику при закріпленні ключа), тож будь-який засіб зводиться до:
 * «дай SHA-256 → отримай base64 контейнера .p7s».
 *
 * Вбудовані засоби:
 *  - `file`      — файл підпису, створений «ІІТ Користувач ЦСК», Дія.Підпис, ПриватБанком (будь-який КНЕДП);
 *  - `pkcs12`    — файл-ключ PKCS#12 (.p12/.pfx) з ECDSA/RSA, підпис у браузері (WebCrypto): ключі
 *                  внутрішнього ЦСК АІ-СЕД ПУЕТ і КЕП міжнародних алгоритмів; рівень визначає сервер за сертифікатом;
 *  - `mock`      — імітаційний КНЕДП стенду (лише якщо бекенд увімкнув SED_ROUTING_KEP_ALLOW_MOCK).
 * Сюди ж підключається адаптер бібліотеки ІІТ (eusign.js) для ключів ДСТУ 4145 у браузері:
 * зареєструйте `KepSigner` з `id: "iit"` через `registerSigner` (див. README).
 */
import type { SignatureProvider } from "../api/types";

export interface SigningTarget {
  /** SHA-256 (hex), який має стояти в атрибуті message-digest. */
  contentSha256: string;
  /** Що підписується — для підказки користувачу. */
  description: string;
  /** Рівні ключів, які сервер прийме: qualified | internal | mock (з signing-request). */
  acceptedLevels: string[];
  /** Крок маршруту — лише для імітаційного підпису стенду. */
  stepId?: string;
}

export interface SignResult {
  signatureBase64: string;
  provider: SignatureProvider;
}

export interface KepSigner {
  id: string;
  label: string;
  /** Рівень ключа, який дає засіб. */
  level: "qualified" | "internal" | "mock" | "any";
  hint: string;
  /**
   * Підпис для зовнішніх засобів (адаптер ІІТ, хмарний КЕП): отримує SHA-256 і повертає контейнер.
   * Вбудовані засоби (file, pkcs12, mock) мають власні поля введення в SignaturePicker.
   */
  sign?: (target: SigningTarget) => Promise<SignResult>;
}

const external: KepSigner[] = [];

/** Реєстрація додаткового засобу підпису (напр., адаптера ІІТ eusign.js). */
export function registerSigner(s: KepSigner): void {
  if (!external.some((x) => x.id === s.id)) external.push(s);
}

export function builtInSigners(opts: { mock: boolean }): KepSigner[] {
  const list: KepSigner[] = [
    {
      id: "file",
      label: "Файл підпису (.p7s)",
      level: "qualified",
      hint: "Підпишіть геш у застосунку «ІІТ Користувач ЦСК», Дія.Підпис або сервісі вашого КНЕДП (відокремлений CAdES) і завантажте файл.",
    },
    {
      id: "pkcs12",
      label: "Файл-ключ PKCS#12 (.p12, .pfx)",
      level: "any",
      hint: "Ключ відкривається паролем у браузері й не передається на сервер. Внутрішній ключ АІ-СЕД ПУЕТ не є КЕП: годиться лише для внутрішніх віз. Ключі ДСТУ 4145 (Key-6.dat, .jks) — через файл підпису.",
    },
  ];
  if (opts.mock) {
    list.push({ id: "mock", label: "Тестовий ключ стенду", level: "mock", hint: "Імітаційний КНЕДП: підпис без юридичної сили, лише для перевірки маршруту." });
  }
  return [...list, ...external];
}

export function availableSigners(target: SigningTarget, opts: { mock: boolean }): KepSigner[] {
  return builtInSigners(opts).filter((s) => {
    // signing-request не включає "mock" в accepted_levels, хоча стенд з SED_ROUTING_KEP_ALLOW_MOCK його приймає
    if (s.id === "mock") return !!target.stepId;
    // сервер не повідомив рівні — показуємо все, перевірку все одно робить бекенд
    return s.level === "any" || target.acceptedLevels.length === 0 || target.acceptedLevels.includes(s.level);
  });
}
