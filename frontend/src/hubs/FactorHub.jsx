import Hub from "../components/Hub.jsx";
import Factors from "../features/research/Factors.jsx";
import Research from "../features/research/Research.jsx";
import FactorRegistry from "../components/FactorRegistry.jsx";

// 因子研究：指标 / IC 与归因（研究深度）
export default function FactorHub({ params }) {
  return (
    <Hub
      hubKey="factor_hub"
      initial={params?.tab}
      tabs={[
        { key: "factors", label: "因子/指标", comp: Factors },
        { key: "research", label: "研究深度", comp: Research },
        { key: "registry", label: "因子注册表", comp: FactorRegistry },
      ]}
    />
  );
}