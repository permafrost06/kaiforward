import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "../convex/schema";
import { internal } from "../convex/_generated/api";

const modules = import.meta.glob("../convex/**/*.ts");
const message = (id: number, body = "Searchable archive message") => ({
  key: `${id}:${id * 1000}`, id, timestamp: id * 1000, sender: "+8801712345678",
  recipient: "", direction: "received" as const, body,
});
const request = (query: unknown, cursor: unknown = null, token = "reader-secret") => ({
  method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify({ query, cursor }),
});

beforeEach(() => {
  vi.stubEnv("KAIFORWARD_READ_TOKEN", "reader-secret");
  vi.stubEnv("KAIFORWARD_TOKEN", "writer-secret");
});
afterEach(() => vi.unstubAllEnvs());

describe("indexed message search", () => {
  it("requires the read credential and never accepts the upload credential", async () => {
    const t = convexTest(schema, modules);
    expect((await t.fetch("/v1/messages/search", request("archive", null, "writer-secret"))).status).toBe(401);
    expect((await t.fetch("/v1/messages/search", request("archive", null, "wrong"))).status).toBe(401);
    vi.stubEnv("KAIFORWARD_READ_TOKEN", "");
    expect((await t.fetch("/v1/messages/search", request("archive"))).status).toBe(503);
  });

  it("finds older matches independently of the newest history page", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.messages.ingest, { messages: [message(1, "Ancient uniquephrase"), ...Array.from({ length: 40 }, (_, i) => message(i + 2, "Recent SMS"))] });
    const response = await t.fetch("/v1/messages/search", request("UNIQUEPHR"));
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const result = await response.json();
    expect(result.messages.map((item: { id: number }) => item.id)).toEqual([1]);
    expect(result.messages[0]).not.toHaveProperty("_id");
    expect(result.messages[0]).not.toHaveProperty("searchText");
    expect(result.cursor).toBeNull();
  });

  it("returns distinct matching pages and a terminal null cursor", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.messages.ingest, { messages: Array.from({ length: 31 }, (_, i) => message(i + 1)) });
    const first = await (await t.fetch("/v1/messages/search", request("archive"))).json();
    expect(first.messages).toHaveLength(25);
    expect(typeof first.cursor).toBe("string");
    const second = await (await t.fetch("/v1/messages/search", request("archive", first.cursor))).json();
    expect(second.messages).toHaveLength(6);
    expect(second.cursor).toBeNull();
    expect(new Set([...first.messages, ...second.messages].map((m: { key: string }) => m.key)).size).toBe(31);
    expect((await t.fetch("/v1/messages/search", request("different", first.cursor))).status).toBe(400);
  });

  it("indexes sender IDs, recipient numbers, and formatted phone numbers", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.messages.ingest, { messages: [
      { ...message(1), sender: "AcmeBank" },
      { ...message(2), sender: "my SIM", recipient: "+44 (7700) 900123", direction: "sent" },
    ] });
    expect((await (await t.fetch("/v1/messages/search", request("AcmeB"))).json()).messages[0].id).toBe(1);
    expect((await (await t.fetch("/v1/messages/search", request("+44 7700-900123"))).json()).messages[0].id).toBe(2);
  });

  it("backfills preexisting messages without changing upload replay semantics", async () => {
    const t = convexTest(schema, modules);
    const item = message(1, "Legacy archive text");
    await t.run(async (ctx) => { await ctx.db.insert("messages", { ...item, receivedAt: 123 }); });
    expect(await t.mutation(internal.messages.backfillSearch, { cursor: null })).toEqual({ patched: 1, cursor: null });
    expect(await t.mutation(internal.messages.backfillSearch, { cursor: null })).toEqual({ patched: 0, cursor: null });
    expect(await t.mutation(internal.messages.ingest, { messages: [item] })).toEqual([item.key]);
    const result = await (await t.fetch("/v1/messages/search", request("Legacy"))).json();
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].receivedAt).toBe(123);
  });

  it("returns no matches and rejects invalid searches and cursors", async () => {
    const t = convexTest(schema, modules);
    expect(await (await t.fetch("/v1/messages/search", request("missing"))).json()).toEqual({ messages: [], cursor: null });
    for (const query of ["", " ", null, 123, "x".repeat(257), "word ".repeat(17), "!!!"]) {
      expect((await t.fetch("/v1/messages/search", request(query))).status).toBe(400);
    }
    for (const cursor of ["", "invalid", JSON.stringify({ query: "missing", cursor: "invalid" }), "x".repeat(8193)]) {
      expect((await t.fetch("/v1/messages/search", request("missing", cursor))).status).toBe(400);
    }
  });
});
