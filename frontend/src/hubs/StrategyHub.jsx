import Hub from "../components/Hub.jsx";
import Strategies from "../components/Strategies.jsx";
import StrategyMarket from "../components/StrategyMarket.jsx";

// 策略工场：模板生成与运行 / 市场与导入导出
export default function StrategyHub({ params }) {
  return (
    <Hub
      hubKey="strategy_hub"
      initial={params?.tab}
      tabs={[
        { key: "strategies", label: "生成与运行", comp: Strategies },
        { key: "strmarket", label: "市场与导入导出", comp: StrategyMarket },
      ]}
    />
  );
}