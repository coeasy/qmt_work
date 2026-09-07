// F10 财务抽屉面板（自 MarketData.jsx 原样拆出，行为零变更）。
import { fmtAmount } from "../../lib/format.js";

export default function F10Panel({ financial, stockInfo }) {
  return (
    <div className="card sa-f10 mp-drawer-f10">
      {(financial || stockInfo?.main_business) ? (
        <>
          {stockInfo?.main_business && (
            <div className="sa-f10-row"><span>主营</span><b>{stockInfo.main_business}</b></div>
          )}
          {financial?.pe != null && (
            <div className="sa-f10-row"><span>PE(TTM)</span><b>{Number(financial.pe).toFixed(2)}</b></div>
          )}
          {financial?.pb != null && (
            <div className="sa-f10-row"><span>PB</span><b>{Number(financial.pb).toFixed(2)}</b></div>
          )}
          {financial?.roe != null && (
            <div className="sa-f10-row"><span>ROE</span><b>{(Number(financial.roe) * 100).toFixed(2) + "%"}</b></div>
          )}
          {financial?.revenue != null && (
            <div className="sa-f10-row"><span>营收</span><b>{fmtAmount(financial.revenue)}</b></div>
          )}
          {financial?.net_profit != null && (
            <div className="sa-f10-row"><span>净利</span><b>{fmtAmount(financial.net_profit)}</b></div>
          )}
          {financial?.market_cap != null && (
            <div className="sa-f10-row"><span>总市值</span><b>{fmtAmount(financial.market_cap)}</b></div>
          )}
        </>
      ) : (
        <div className="muted">暂无财务数据（连接券商或等待快照后重试）</div>
      )}
    </div>
  );
}
