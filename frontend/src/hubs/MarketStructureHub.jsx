import Hub from "../components/Hub.jsx";
import Boards from "../components/Boards.jsx";
import Etfs from "../components/Etfs.jsx";
import IndexOverview from "../components/IndexOverview.jsx";
import Rotation from "../components/Rotation.jsx";

// 市场结构中心：板块行情 / ETF 基金 / 指数分析 / 板块轮动
export default function MarketStructureHub({ params }) {
  return (
    <Hub
      hubKey="mktstructure"
      initial={params?.tab}
      tabs={[
        { key: "boards", label: "板块行情", comp: Boards },
        { key: "etfs", label: "ETF 基金", comp: Etfs },
        { key: "index_overview", label: "指数分析", comp: IndexOverview },
        { key: "rotation", label: "板块轮动", comp: Rotation },
      ]}
    />
  );
}