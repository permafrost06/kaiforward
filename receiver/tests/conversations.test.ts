import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "../convex/schema";
import { internal } from "../convex/_generated/api";
const modules = import.meta.glob("../convex/**/*.ts");
const sms = (id: number, sender = "Bank", timestamp = id * 1000) => ({
  key: `${id}:${timestamp}`, id, timestamp, sender, recipient: "", direction: "received" as const, body: `SMS ${id}`,
});
const request = (cursor?: string | null, token = "reader") => ({ headers: { Authorization: `Bearer ${token}` } });
const url = (cursor?: string | null) => "/v1/conversations" + (cursor == null ? "" : "?cursor=" + encodeURIComponent(cursor));
beforeEach(() => { vi.stubEnv("KAIFORWARD_READ_TOKEN", "reader"); vi.stubEnv("KAIFORWARD_TOKEN", "writer"); });
afterEach(() => vi.unstubAllEnvs());

it("paginates distinct conversations even when hundreds of recent messages share a sender", async () => {
  const t = convexTest(schema, modules);
  await t.mutation(internal.messages.ingest, { messages: [
    ...Array.from({ length: 31 }, (_, i) => sms(i + 1, `Sender ${i}`)),
    ...Array.from({ length: 100 }, (_, i) => sms(i + 100, "Sender 0")),
  ] });
  const response = await t.fetch(url(), request());
  expect(response.status).toBe(200);
  expect(response.headers.get("Cache-Control")).toBe("no-store");
  const first = await response.json();
  expect(first.conversations).toHaveLength(25);
  expect(first.conversations[0].key).toBe("sender 0");
  expect(first.conversations[0].lastMessage.id).toBe(199);
  const second = await (await t.fetch(url(first.cursor), request())).json();
  expect(second.conversations).toHaveLength(6);
  expect(second.cursor).toBeNull();
  expect(new Set([...first.conversations, ...second.conversations].map(c => c.key)).size).toBe(31);
});

it("assigns both directions to the normalized peer while preserving sender IDs and country prefixes", async () => {
  const t = convexTest(schema, modules);
  const messages = [sms(1, "+880 123-456"), { ...sms(2, "My SIM"), recipient: "+880123456", direction: "sent" as const },
    sms(3, "880123456"), sms(4, " AcmeBANK "), sms(5, "acmebank"), sms(6, "")];
  await t.mutation(internal.messages.ingest, { messages });
  await t.run(async ctx => {
    const rows = await ctx.db.query("conversations").collect();
    expect(rows).toHaveLength(4);
    for (const item of await ctx.db.query("messages").collect()) {
      expect(item.conversationId).toBeDefined();
      const conversation = await ctx.db.get(item.conversationId!);
      expect(conversation?.key).toBe(item.conversation);
    }
    const phone = rows.find(r => r.key === "+880123456")!;
    expect(phone.lastMessage.direction).toBe("sent");
    expect((await ctx.db.get(phone.lastMessageId))?.key).toBe(messages[1].key);
  });
});

it("keeps latest previews stable across out-of-order uploads, timestamp ties, and retries", async () => {
  const t = convexTest(schema, modules);
  const newer = sms(20, "Bank", 5000);
  const tied = sms(30, "Bank", 5000);
  await t.mutation(internal.messages.ingest, { messages: [newer, sms(10, "Bank", 1000), tied] });
  const before = await t.run(async ctx => await ctx.db.query("conversations").unique());
  await t.mutation(internal.messages.ingest, { messages: [newer, tied, sms(10, "Bank", 1000)] });
  const after = await t.run(async ctx => await ctx.db.query("conversations").unique());
  expect(after).toEqual(before);
  expect(after?.lastKey).toBe(tied.key);
});

it("rolls back conversation updates along with a conflicting upload batch", async () => {
  const t = convexTest(schema, modules);
  const original = sms(1);
  await t.mutation(internal.messages.ingest, { messages: [original] });
  await expect(t.mutation(internal.messages.ingest, { messages: [sms(2, "New sender"), { ...original, body: "Conflict" }] })).rejects.toThrow();
  expect(await t.run(async ctx => (await ctx.db.query("conversations").collect()).map(c => c.key))).toEqual(["bank"]);
  expect(await t.run(async ctx => (await ctx.db.query("messages").collect()).length)).toBe(1);
});

it("backfills legacy messages idempotently and coexists with new uploads between batches", async () => {
  const t = convexTest(schema, modules);
  await t.run(async ctx => {
    for (let i = 1; i <= 120; i++) await ctx.db.insert("messages", { ...sms(i, i % 2 ? "Bank" : "Other"), receivedAt: 42 });
  });
  const first = await t.mutation(internal.messages.backfillConversations, { cursor: null });
  expect(first.assigned).toBe(100);
  expect(first.cursor).not.toBeNull();
  await t.mutation(internal.messages.ingest, { messages: [sms(200, "Bank")] });
  const second = await t.mutation(internal.messages.backfillConversations, { cursor: first.cursor });
  expect(second.assigned).toBe(20);
  expect(second.cursor).toBeNull();
  const again = await t.mutation(internal.messages.backfillConversations, { cursor: null });
  expect(again.assigned).toBe(0);
  await t.run(async ctx => {
    const rows = await ctx.db.query("conversations").collect();
    expect(rows).toHaveLength(2);
    expect(rows.find(c => c.key === "bank")?.lastMessage.id).toBe(200);
    const all = await ctx.db.query("messages").collect();
    expect(all.every(m => m.conversationId !== undefined && m.conversation !== undefined)).toBe(true);
    expect(all.filter(m => m.id <= 120).every(m => m.receivedAt === 42)).toBe(true);
  });
});

it("limits preview size while retaining complete thread bodies and requires read auth", async () => {
  const t = convexTest(schema, modules);
  const message = { ...sms(1), body: "🙂".repeat(1000) };
  await t.mutation(internal.messages.ingest, { messages: [message] });
  expect((await t.fetch(url(), request(null, "writer"))).status).toBe(401);
  expect((await t.fetch(url("invalid"), request())).status).toBe(400);
  const inbox = await (await t.fetch(url(), request())).json();
  expect([...inbox.conversations[0].lastMessage.body]).toHaveLength(256);
  expect(inbox.conversations[0]).not.toHaveProperty("_id");
  const full = await t.query(internal.messages.thread, { conversation: "bank", paginationOpts: { cursor: null, numItems: 25 } });
  expect(full.messages[0].body).toBe(message.body);
  expect(full.messages[0]).not.toHaveProperty("conversationId");
});
