// Typed application event vocabulary shared by WS adapters and UI contexts.
export const EVENT_TYPES = Object.freeze({
  QUOTE: "quote",
  ORDER: "order",
  TRADE: "trade",
  ACCOUNT: "account",
  CONNECTION: "connection",
  JOB: "job",
  DATASET: "dataset",
});

export function normalizePlatformEvent(event) {
  if (!event || typeof event !== "object") return null;
  const type = String(event.type || "");
  if (!Object.values(EVENT_TYPES).includes(type)) return null;
  return {
    type,
    ts: event.ts || event.timestamp || new Date().toISOString(),
    connectionId: event.connection_id || event.conn_id || "",
    data: event.data ?? {},
  };
}

