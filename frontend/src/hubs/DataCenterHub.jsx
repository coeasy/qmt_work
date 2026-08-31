import Hub from "../components/Hub.jsx";
import MarketTools from "../components/MarketTools.jsx";
import Reference from "../components/Reference.jsx";

// 数据中心：行情数据（缓存/抓取/同步）/ 参考数据
export default function DataCenterHub({ params }) {
  return (
    <Hub
      hubKey="datacenter"
      initial={params?.tab}
      tabs={[
        { key: "markettools", label: "行情数据", comp: MarketTools },
        { key: "reference", label: "参考数据", comp: Reference },
      ]}
    />
  );
}