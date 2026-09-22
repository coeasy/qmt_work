/**
 * 数据新鲜度：界面上区分「实时行情价」与「快照价」的唯一入口。
 *
 * ## 为什么要有这一层
 *
 * 项目里反复强调「非空 ≠ 够新」，但界面上最容易犯的错恰好是把**快照价**渲染成
 * 「最新价 / 现价」：
 * - 券商持仓接口只在**查询那一刻**给一次价，之后不会自己更新；
 * - 后端的跨账户持仓快照同理，还带一个 `generated_at`；
 * - 拿不到实时行情时两者都会回退到快照，而回退后的数字和实时价**长得一模一样**。
 *
 * 后果是用户拿上一交易日的收盘价当现价去做加减仓、甚至填下单价格。
 * 金额类误判代价高，所以「这个数是不是实时的」必须由界面明说，而不是让用户猜。
 *
 * ## 判据为什么是 `> 0` 而不是 `!== undefined`
 *
 * 停牌 / 未订阅到时行情会推 `price: 0`，而「0」比「没有」更危险（会被读成
 * 「这只股真的跌到 0」）。因此 0 一律视为**缺失**，与 `undefined` 同等待遇。
 * 这条规则此前在 AssetSummary / Positions / Trade 三处各写了一遍，改一处漏两处
 * 就会出现「同一张表有的行标了有的没标」，故收敛到这里。
 */

/**
 * 该价格是不是**可用的实时行情价**（0 与缺失都算不可用）。
 *
 * 签名收 `number | null | undefined`：后端持仓快照的 `price` 字段是**可空**的
 * （`AccountGridPosition.price: number | null`），写成 `number | undefined` 会在
 * 调用处炸 TS2345 —— 可空字段是这里的常态而不是异常。
 *
 * 同时它是**类型谓词**（`price is number`），因此调用后 TS 会把值窄化成 `number`，
 * 调用方不必再写 `&& price !== undefined` 这种冗余保护。
 */
export function isLivePrice(price: number | null | undefined): price is number {
  return typeof price === "number" && price > 0;
}

/**
 * 「N / M 条未取到实时行情」的统一说法。
 *
 * 各页受影响的下游列不同（持仓页是市值/盈亏，交易页是要拿去下单的参考价），
 * 所以后半句由调用方给 `tail`，前半句在这里保持一致 —— 否则同一个事实会在
 * 三张页面上出现三种说法，用户很难建立「虚线下划线 = 非实时」这个共识。
 *
 * @param stale 未取到实时行情的条数
 * @param total 总条数
 * @param tail  受影响的下游列说明（中文，含句号）
 * @param asOf  后端快照生成时间（有就带上，让用户知道截止到什么时候）
 */
export function staleQuoteNote(
  stale: number,
  total: number,
  tail: string,
  asOf?: string,
): string {
  return (
    `有 ${stale} / ${total} 条未取到实时行情，显示的是券商查询快照价` +
    `${asOf ? `（生成于 ${asOf}）` : ""}；${tail}`
  );
}
