import Hub from "../components/Hub.jsx";
import Signal from "../components/Signal.jsx";
import Alerts from "../components/Alerts.jsx";
import Notifications from "../components/Notifications.jsx";
import Webhooks from "../components/Webhooks.jsx";

// 通知与集成子页签：纵向堆叠通知渠道 + 出站 Webhook
function NotifyStack() {
  return (
    <div className="hub-body">
      <Notifications />
      <Webhooks />
    </div>
  );
}

// 信号与自动化中心：信号路由 / 告警规则 / 通知与集成
export default function AutomationHub({ params }) {
  return (
    <Hub
      hubKey="automation"
      initial={params?.tab}
      tabs={[
        { key: "signal", label: "信号路由", comp: Signal },
        { key: "alerts", label: "告警规则", comp: Alerts },
        { key: "notifications", label: "通知与集成", comp: NotifyStack },
      ]}
    />
  );
}