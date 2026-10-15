import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "../convex/schema";
import { internal } from "../convex/_generated/api";

const modules = import.meta.glob("../convex/**/*.ts");
const message = (id: number) => ({
  key: `${id}:${id * 1000}`, id, timestamp: id * 1000, sender: "+8801712345678",
  recipient: "", direction: "received" as const, body: `Message ${id}`, simSlot: 1 as const,
});
const readHeaders = { Authorization: "Bearer reader-secret" };

beforeEach(() => {
  vi.stubEnv("KAIFORWARD_READ_TOKEN", "reader-secret");
  vi.stubEnv("KAIFORWARD_TOKEN", "writer-secret");
});
afterEach(() => vi.unstubAllEnvs());

describe("authenticated message viewing", () => {
  it("rejects absent and upload credentials before exposing history", async () => {
    const t = convexTest(schema, modules);
    for (const headers of [{}, { Authorization: "Bearer writer-secret" }, { Authorization: "Bearer wrong" }]) {
      const response = await t.fetch("/v1/messages", { headers });
      expect(response.status).toBe(401);
      expect(await response.json()).toEqual({ error: "Unauthorized" });
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    }
  });

  it("disables reads when the read token is unconfigured", async () => {
    vi.stubEnv("KAIFORWARD_READ_TOKEN", "");
    const t = convexTest(schema, modules);
    expect((await t.fetch("/v1/messages", { headers: readHeaders })).status).toBe(503);
  });

  it("paginates newest first without exposing database metadata", async () => {
    const t = convexTest(schema, modules);
    await t.mutation(internal.messages.ingest, { messages: Array.from({ length: 31 }, (_, i) => message(i + 1)) });
    const first = await (await t.fetch("/v1/messages", { headers: readHeaders })).json();
    expect(first.messages).toHaveLength(25);
    expect(first.messages[0]).toMatchObject(message(31));
    expect(first.messages[0]).not.toHaveProperty("_id");
    expect(first.messages[0]).not.toHaveProperty("_creationTime");
    expect(typeof first.cursor).toBe("string");
    const second = await (await t.fetch(`/v1/messages?cursor=${encodeURIComponent(first.cursor)}`, { headers: readHeaders })).json();
    expect(second.messages).toHaveLength(6);
    expect(second.cursor).toBeNull();
    expect([...first.messages, ...second.messages].map((m: { id: number }) => m.id))
      .toEqual(Array.from({ length: 31 }, (_, i) => 31 - i));
  });

  it("returns an empty final page and rejects empty or excessive cursors", async () => {
    const t = convexTest(schema, modules);
    expect(await (await t.fetch("/v1/messages", { headers: readHeaders })).json()).toEqual({ messages: [], cursor: null });
    for (const cursor of ["", "x".repeat(4097), "invalid-cursor"]) {
      expect((await t.fetch(`/v1/messages?cursor=${cursor}`, { headers: readHeaders })).status).toBe(400);
    }
  });

  it("keeps upload authorization and replay behavior intact", async () => {
    const t = convexTest(schema, modules);
    const item = message(1);
    const body = JSON.stringify({ messages: [{ ...item, sim_slot: item.simSlot }] });
    const upload = (token: string) => t.fetch("/v1/messages", {
      method: "POST", body, headers: { Authorization: `Bearer ${token}`, "Content-Length": String(new TextEncoder().encode(body).length) },
    });
    expect((await upload("reader-secret")).status).toBe(401);
    for (let i = 0; i < 2; i++) expect(await (await upload("writer-secret")).json()).toEqual({ accepted: [item.key] });
    const result = await (await t.fetch("/v1/messages", { headers: readHeaders })).json();
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0]).toMatchObject(item);
  });
});
