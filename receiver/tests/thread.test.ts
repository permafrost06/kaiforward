import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "../convex/schema";
import { internal } from "../convex/_generated/api";
const modules = import.meta.glob("../convex/**/*.ts");
const item = (id: number, sender = "+880 123-456") => ({
  key: `${id}:${id * 1000}`, id, timestamp: id * 1000, sender, recipient: "", direction: "received" as const, body: "Thread message",
});
const request = (conversation: unknown, cursor: unknown = null, token = "reader") => ({
  method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify({ conversation, cursor }),
});
beforeEach(() => { vi.stubEnv("KAIFORWARD_READ_TOKEN", "reader"); vi.stubEnv("KAIFORWARD_TOKEN", "writer"); });
afterEach(() => vi.unstubAllEnvs());

it("paginates only the selected conversation, including both directions, in timestamp order", async () => {
  const t = convexTest(schema, modules);
  const selected = Array.from({ length: 31 }, (_, i) => i % 2 ?
    { ...item(i + 1), sender: "my SIM", recipient: "+880123456", direction: "sent" as const } : item(i + 1));
  const unrelated = Array.from({ length: 80 }, (_, i) => item(i + 100, "Other"));
  await t.mutation(internal.messages.ingest, { messages: [...selected, ...unrelated] });
  const firstResponse = await t.fetch("/v1/messages/thread", request("+880123456"));
  expect(firstResponse.status).toBe(200);
  expect(firstResponse.headers.get("Cache-Control")).toBe("no-store");
  const first = await firstResponse.json();
  expect(first.messages.map((m: {id: number}) => m.id)).toEqual(Array.from({ length: 25 }, (_, i) => 31 - i));
  expect(first.messages[0]).not.toHaveProperty("conversation");
  expect(first.messages[0]).not.toHaveProperty("searchText");
  const second = await (await t.fetch("/v1/messages/thread", request("+880 123-456", first.cursor))).json();
  expect(second.messages.map((m: {id: number}) => m.id)).toEqual([6, 5, 4, 3, 2, 1]);
  expect(second.cursor).toBeNull();
  expect((await t.fetch("/v1/messages/thread", request("Other", first.cursor))).status).toBe(400);
});

it("requires read auth and rejects malformed requests and cursors", async () => {
  const t = convexTest(schema, modules);
  expect((await t.fetch("/v1/messages/thread", request("Other", null, "writer"))).status).toBe(401);
  for (const value of [null, 3, "x".repeat(129), "bad\nname"])
    expect((await t.fetch("/v1/messages/thread", request(value))).status).toBe(400);
  for (const cursor of ["", "invalid", JSON.stringify({ conversation: "other", cursor: "invalid" })])
    expect((await t.fetch("/v1/messages/thread", request("Other", cursor))).status).toBe(400);
  expect(await (await t.fetch("/v1/messages/thread", request("missing"))).json()).toEqual({ messages: [], cursor: null });
});

it("backfills existing messages and preserves sender IDs, country prefixes, and upload replays", async () => {
  const t = convexTest(schema, modules);
  const old = [item(1, " AcmeBANK "), item(2, "880123456"), item(3, "+880123456"), item(4, "")];
  await t.run(async ctx => { for (const m of old) await ctx.db.insert("messages", { ...m, receivedAt: 42, searchText: m.body }); });
  expect((await t.mutation(internal.messages.backfillSearch, { cursor: null })).patched).toBe(4);
  expect((await t.mutation(internal.messages.backfillSearch, { cursor: null })).patched).toBe(0);
  await t.mutation(internal.messages.ingest, { messages: old });
  for (const [key, id] of [["acmebank", 1], ["880123456", 2], ["+880123456", 3], ["", 4]] as const) {
    const result = await (await t.fetch("/v1/messages/thread", request(key))).json();
    expect(result.messages.map((m: {id: number}) => m.id)).toEqual([id]);
    expect(result.messages[0].receivedAt).toBe(42);
  }
});
