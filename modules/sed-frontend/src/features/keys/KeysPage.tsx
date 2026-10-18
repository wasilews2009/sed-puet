/**
 * Ключі підпису:
 *  - мої ключі (КЕП Дії, ПриватБанку, інших КНЕДП і внутрішні ключі АІ-СЕД ПУЕТ), відкріплення;
 *  - закріплення КЕП: одноразовий виклик, підписаний на комп'ютері користувача (файл-ключ на сервер не передається);
 *  - внутрішній ключ: файл-ключ PKCS#12 (видається один раз) або сертифікат за CSR, ланцюжок ЦСК і CRL;
 *  - перевірка сертифіката і підпису.
 */
import { useNavigate, useParams } from "react-router-dom";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { TabPanel, Tabs } from "../../components/tabs";
import { Card, PageHeader } from "../../components/ui";
import { BindKey } from "./BindKey";
import { InspectCertificate, VerifySignature } from "./Checks";
import { InternalKey } from "./InternalKey";
import { MyKeys } from "./MyKeys";

const SECTIONS = [
  { key: "mine", label: "Мої ключі" },
  { key: "bind", label: "Закріпити КЕП" },
  { key: "internal", label: "Внутрішній ключ" },
  { key: "certificate", label: "Перевірити сертифікат" },
  { key: "signature", label: "Перевірити підпис" },
];

export function KeysPage() {
  useTitle("Ключі підпису");
  const { section = "mine" } = useParams();
  const navigate = useNavigate();
  const { dict } = useApp();
  const current = SECTIONS.some((s) => s.key === section) ? section : "mine";
  return (
    <>
      <PageHeader title="Ключі підпису"
        lead="Кваліфікований електронний підпис (КЕП) надає документу юридичної сили. Особистий ключ і пароль завжди залишаються на вашому комп'ютері: сервер отримує лише підпис і сертифікат." />
      <Card>
        <Tabs idBase="keys" tabs={SECTIONS} current={current} onChange={(k) => navigate(`/keys/${k}`, { replace: true })} />
        <TabPanel idBase="keys" current={current}>
          {current === "mine" && <MyKeys onBind={() => navigate("/keys/bind")} />}
          {current === "bind" && <BindKey onDone={() => navigate("/keys/mine")} mock={dict.modules.kep_mock} />}
          {current === "internal" && <InternalKey />}
          {current === "certificate" && <InspectCertificate />}
          {current === "signature" && <VerifySignature />}
        </TabPanel>
      </Card>
    </>
  );
}
