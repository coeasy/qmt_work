import { describe, expect, it } from "vitest";
import { EVENT_TYPES, normalizePlatformEvent } from "../events.js";

describe("platform event contract", () => {
  it("normalizes canonical connection identity", () => {
    expect(normalizePlatformEvent({
      type: EVENT_TYPES.QUOTE, conn_id: "c1", data: { last: 10 },
    })).toMatchObject({ type: "quote", connectionId: "c1", data: { last: 10 } });
  });

  it("rejects unknown event types", () => {
    expect(normalizePlatformEvent({ type: "raw-db" })).toBeNull();
  });
});
