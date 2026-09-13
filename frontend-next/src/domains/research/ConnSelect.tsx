import { useEffect } from "react";
import { Select } from "@/design/primitives";
import { useBrokerStore } from "@/stores/broker";

/**
 * 活跃连接下拉（因子/研究页面共用）。
 * 空数据时触发一次 load，避免先于「连接管理」打开时下拉为空。
 */
export function ConnSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const connections = useBrokerStore((st) => st.connections);
  const load = useBrokerStore((st) => st.load);
  const loaded = useBrokerStore((st) => st.connections.length > 0);

  useEffect(() => {
    if (!loaded) void load();
  }, [loaded, load]);

  return (
    <Select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      options={[
        { value: "", label: "默认" },
        ...connections.map((c) => ({
          value: c.conn_id,
          label: c.broker_name || c.conn_id,
          disabled: !c.connected,
        })),
      ]}
    />
  );
}
