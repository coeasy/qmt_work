import Hub from "../components/Hub.jsx";
import Audit from "../components/Audit.jsx";
import Reconcile from "../components/Reconcile.jsx";

// 审计对账：审计日志 / 对账核销
export default function AuditReconHub({ params }) {
  return (
    <Hub
      hubKey="audit_recon"
      initial={params?.tab}
      tabs={[
        { key: "audit", label: "审计日志", comp: Audit },
        { key: "reconcile", label: "对账核销", comp: Reconcile },
      ]}
    />
  );
}