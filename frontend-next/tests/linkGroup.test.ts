import { describe, expect, it } from "vitest";
import { broadcastLink, joinLinkGroup, linkGroupSize } from "@/charts/linkGroup";

/**
 * 跨窗联动组测试（对标通达信的多图联动）。
 */
describe("图表联动组", () => {
  it("加入后组内计数增加，退出后清理", () => {
    const leave = joinLinkGroup("t1", () => undefined);
    expect(linkGroupSize("t1")).toBe(1);
    leave();
    expect(linkGroupSize("t1")).toBe(0);
  });

  it("广播会通知组内其他成员，但排除来源自身（防回环）", () => {
    const seenA: number[] = [];
    const seenB: number[] = [];
    const a = (p: { dataIndex: number }) => seenA.push(p.dataIndex);
    const b = (p: { dataIndex: number }) => seenB.push(p.dataIndex);

    const leaveA = joinLinkGroup("t2", a);
    const leaveB = joinLinkGroup("t2", b);

    broadcastLink("t2", { dataIndex: 42 }, a);

    expect(seenA).toEqual([]); // 来源被排除
    expect(seenB).toEqual([42]);

    leaveA();
    leaveB();
    expect(linkGroupSize("t2")).toBe(0);
  });

  it("不同组之间互不干扰", () => {
    const g1: number[] = [];
    const g2: number[] = [];
    const leave1 = joinLinkGroup("g1", (p) => g1.push(p.dataIndex));
    const leave2 = joinLinkGroup("g2", (p) => g2.push(p.dataIndex));

    broadcastLink("g1", { dataIndex: 7 });
    expect(g1).toEqual([7]);
    expect(g2).toEqual([]);

    leave1();
    leave2();
  });

  it("向不存在的组广播是安全的空操作", () => {
    expect(() => broadcastLink("nope", { dataIndex: 1 })).not.toThrow();
  });

  it("同一回调重复加入按集合去重", () => {
    const fn = () => undefined;
    const l1 = joinLinkGroup("t3", fn);
    const l2 = joinLinkGroup("t3", fn);
    expect(linkGroupSize("t3")).toBe(1);
    l1();
    l2();
    expect(linkGroupSize("t3")).toBe(0);
  });
});
