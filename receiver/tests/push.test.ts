import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHash, generateKeyPairSync } from "node:crypto";
import { convexTest } from "convex-test";
import schema from "../convex/schema";
import { internal } from "../convex/_generated/api";

const modules = import.meta.glob("../convex/**/*.ts");
const installation = "12345678-1234-1234-1234-123456789abc";
const credential = createHash("sha256").update("reader").digest("hex");
const item = { key: "1:1000", id: 1, timestamp: 1000, sender: "+8801234567", recipient: "", direction: "received" as const, body: "Your SMS" };
const account = JSON.stringify({ project_id: "kaiforward", client_email: "push@example.com",
  private_key: generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey.export({ type: "pkcs8", format: "pem" }) });

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubEnv("KAIFORWARD_READ_TOKEN", "reader");
  vi.stubEnv("FIREBASE_SERVICE_ACCOUNT_JSON", account);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("new-message push", () => {
  it("requires viewer authorization, validates registrations and updates rotated tokens", async () => {
    const t = convexTest(schema, modules);
    const register = (token: string, payload: unknown) => t.fetch("/v1/push/devices", {
      method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(payload),
    });
    expect((await register("writer", { installation, token: "fcm-token" })).status).toBe(401);
    expect((await register("reader", { installation: "bad", token: "fcm-token" })).status).toBe(400);
    expect((await register("reader", { installation, token: "fcm-token" })).status).toBe(200);
    expect((await register("reader", { installation, token: "rotated-token" })).status).toBe(200);
    const devices = await t.run(ctx => ctx.db.query("pushDevices").collect());
    expect(devices).toHaveLength(1);
    expect(devices[0]).toMatchObject({ token: "rotated-token", credential });
  });

  it("schedules only newly inserted incoming SMS and rolls back conflicting batches", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.messages.ingest, { messages: [item] });
    await t.mutation(internal.messages.ingest, { messages: [item] });
    await t.mutation(internal.messages.ingest, { messages: [{ ...item, key: "2:2000", id: 2, timestamp: 2000, direction: "sent", recipient: "123" }] });
    expect(await t.run(ctx => ctx.db.system.query("_scheduled_functions").collect())).toHaveLength(1);
    await expect(t.mutation(internal.messages.ingest, { messages: [
      { ...item, key: "3:3000", id: 3, timestamp: 3000 }, { ...item, body: "conflict" },
    ] })).rejects.toThrow();
    expect(await t.run(ctx => ctx.db.system.query("_scheduled_functions").collect())).toHaveLength(1);
  });

  it("fans out only to existing active installations and prunes stale devices", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.push.register, { installation, token: "active", credential });
    await t.mutation(internal.push.register, { installation: "stale", token: "stale", credential });
    const stale = await t.run(ctx => ctx.db.query("pushDevices").withIndex("by_installation", q => q.eq("installation", "stale")).unique());
    await t.run(ctx => ctx.db.patch(stale!._id, { updatedAt: Date.now() - 61 * 86400_000 }));
    vi.advanceTimersByTime(10);
    const receivedAt = Date.now();
    const messageId = await t.run(ctx => ctx.db.insert("messages", { ...item, receivedAt }));
    vi.advanceTimersByTime(10);
    await t.mutation(internal.push.register, { installation: "new", token: "new", credential });
    await t.mutation(internal.push.enqueue, { messageId, receivedAt, cursor: null });
    const jobs = await t.run(ctx => ctx.db.system.query("_scheduled_functions").collect());
    expect(jobs.filter(job => job.name === "pushDelivery:send")).toHaveLength(1);
    const devices = await t.run(ctx => ctx.db.query("pushDevices").collect());
    expect(devices.map(device => device.token).sort()).toEqual(["active", "new"]);
  });

  async function setup() {
    const t = convexTest(schema, modules);
    await t.mutation(internal.push.register, { installation, token: "fcm-token", credential });
    await t.mutation(internal.messages.ingest, { messages: [{ ...item, body: "😀".repeat(2000) }] });
    const device = (await t.run(ctx => ctx.db.query("pushDevices").collect()))[0];
    const message = (await t.run(ctx => ctx.db.query("messages").collect()))[0];
    return { t, args: { deviceId: device._id, messageId: message._id, attempt: 0 } };
  }

  it("sends bounded sender/text data and stops delivery after read-token rotation", async () => {
    const { t, args } = await setup();
    const fetch = vi.fn(async (url: string, _init: RequestInit) =>
      url.includes("oauth2") ? Response.json({ access_token: "oauth", expires_in: 3600 }) : Response.json({ name: "accepted" }));
    vi.stubGlobal("fetch", fetch);
    await t.action(internal.pushDelivery.send, args);
    const call = fetch.mock.calls.find(([url]) => url.includes("fcm.googleapis.com"));
    expect(call).toBeDefined();
    const body = call![1].body as string;
    const payload = JSON.parse(body);
    expect(payload.message.data).toMatchObject({ sender: item.sender, conversation: item.sender, messageKey: item.key });
    expect(payload.message.data.body).toBe("😀".repeat(700) + "…");
    expect(new TextEncoder().encode(body).length).toBeLessThan(4096);
    expect(body).not.toContain("reader");
    expect(body).not.toContain("private_key");
    // JSON escaping can be larger than UTF-8 text (for example, control characters).
    await t.run(ctx => ctx.db.patch(args.messageId, { body: "\u0001".repeat(2000) }));
    fetch.mockClear();
    await t.action(internal.pushDelivery.send, args);
    const escaped = JSON.parse(fetch.mock.calls.find(([url]) => url.includes("fcm.googleapis.com"))![1].body as string);
    expect(new TextEncoder().encode(JSON.stringify(escaped.message.data)).length).toBeLessThanOrEqual(3500);
    expect(escaped.message.data.body.endsWith("…")).toBe(true);
    fetch.mockClear(); vi.stubEnv("KAIFORWARD_READ_TOKEN", "new-reader");
    const rotated = await setup();
    await rotated.t.action(internal.pushDelivery.send, rotated.args);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("removes unregistered tokens and retries temporary failures", async () => {
    const { t, args } = await setup();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("oauth2")
      ? Response.json({ access_token: "oauth", expires_in: 3600 })
      : Response.json({ error: { details: [{ errorCode: "UNREGISTERED" }] } }, { status: 404 })));
    await t.action(internal.pushDelivery.send, args);
    expect(await t.run(ctx => ctx.db.query("pushDevices").collect())).toHaveLength(0);
    await t.mutation(internal.push.register, { installation, token: "replacement", credential });
    const device = (await t.run(ctx => ctx.db.query("pushDevices").collect()))[0];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.includes("oauth2")
      ? Response.json({ access_token: "oauth", expires_in: 3600 }) : new Response(null, { status: 503 })));
    await t.action(internal.pushDelivery.send, { ...args, deviceId: device._id });
    expect(await t.run(ctx => ctx.db.query("pushDevices").collect())).toHaveLength(1);
    const scheduled = await t.run(ctx => ctx.db.system.query("_scheduled_functions").collect());
    expect(scheduled.some(job => job.args[0]?.attempt === 1)).toBe(true);
  });
});
