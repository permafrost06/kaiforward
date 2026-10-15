import { httpRouter } from "convex/server";
import { ConvexError } from "convex/values";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { conversationKey } from "./messages";

const MAX_BODY = 1024 * 1024;

class Invalid extends Error {}

function respond(status: number, payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function sameSecret(actual: string, expected: string) {
  const a = new TextEncoder().encode(actual);
  const b = new TextEncoder().encode(expected);
  let difference = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) difference |= (a[i] ?? 0) ^ b[i];
  return difference === 0;
}

const isInt = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value);

/** Mirrors the PoC receiver's /v1/messages validation. */
function validate(payload: unknown) {
  // The token identifies the phone; the payload's device_id is ignored.
  if (typeof payload !== "object" || payload === null) throw new Invalid("Invalid request");
  const messages = (payload as any).messages;
  if (!Array.isArray(messages) || messages.length < 1 || messages.length > 50)
    throw new Invalid("Expected 1 to 50 messages");
  const keys = new Set<string>();
  return messages.map((message) => {
    if (typeof message !== "object" || message === null || Array.isArray(message))
      throw new Invalid("Invalid message");
    const { id, timestamp, sender, body } = message;
    if (!isInt(id) || id < 0 || !isInt(timestamp) || timestamp < 0 || timestamp >= 1e14)
      throw new Invalid("Invalid id or timestamp");
    const key = `${id}:${timestamp}`;
    if (message.key !== key || keys.has(key)) throw new Invalid("Invalid or repeated message key");
    if (typeof sender !== "string" || sender.length > 128 || typeof body !== "string" || body.length > 65536)
      throw new Invalid("Invalid sender or body");
    const direction = message.direction ?? "received";
    const recipient = message.recipient ?? "";
    if ((direction !== "received" && direction !== "sent") || typeof recipient !== "string" || recipient.length > 128)
      throw new Invalid("Invalid direction or recipient");
    if (direction === "sent" && !recipient) throw new Invalid("Sent SMS requires a recipient");
    const simSlot = message.sim_slot;
    if (simSlot !== undefined && simSlot !== 1 && simSlot !== 2) throw new Invalid("Invalid SIM slot");
    keys.add(key);
    return { key, id, timestamp, sender, recipient, direction, body, simSlot } as const;
  });
}

const http = httpRouter();

http.route({
  path: "/health",
  method: "GET",
  handler: httpAction(async () => respond(200, { ok: true })),
});

http.route({
  path: "/v1/messages",
  method: "GET",
  handler: httpAction(async (ctx, request) => {
    // Read access is separate from the firmware's write credential.
    const token = process.env.KAIFORWARD_READ_TOKEN;
    if (!token) return respond(503, { error: "Message viewing not configured" });
    if (!sameSecret(request.headers.get("Authorization") ?? "", `Bearer ${token}`))
      return respond(401, { error: "Unauthorized" });
    const cursor = new URL(request.url).searchParams.get("cursor");
    if (cursor !== null && (cursor.length === 0 || cursor.length > 4096))
      return respond(400, { error: "Invalid cursor" });
    try {
      return respond(200, await ctx.runQuery(internal.messages.list, {
        paginationOpts: { cursor, numItems: 25 },
      }));
    } catch (error) {
      // Convex validates opaque pagination cursors inside the query.
      if (error instanceof Error && /cursor/i.test(error.message))
        return respond(400, { error: "Invalid cursor" });
      throw error;
    }
  }),
});

http.route({
  path: "/v1/messages/thread",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    const token = process.env.KAIFORWARD_READ_TOKEN;
    if (!token) return respond(503, { error: "Message viewing not configured" });
    if (!sameSecret(request.headers.get("Authorization") ?? "", `Bearer ${token}`))
      return respond(401, { error: "Unauthorized" });
    let conversation: string;
    let cursor: string | null = null;
    try {
      const raw = await request.arrayBuffer();
      if (raw.byteLength > 16_384) return respond(413, { error: "Thread request too large" });
      const payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
      if (typeof payload?.conversation !== "string" || payload.conversation.length > 128 ||
          /[\u0000-\u001f]/u.test(payload.conversation)) throw new Invalid("Invalid conversation");
      // Empty sender IDs are valid in uploaded SMS data.
      conversation = conversationKey(payload.conversation);
      if (payload.cursor !== undefined && payload.cursor !== null) {
        if (typeof payload.cursor !== "string" || payload.cursor.length > 8192) throw new Invalid("Invalid cursor");
        const saved = JSON.parse(payload.cursor);
        if (saved.conversation !== conversation || typeof saved.cursor !== "string" || !saved.cursor || saved.cursor.length > 4096)
          throw new Invalid("Invalid cursor");
        cursor = saved.cursor;
      }
    } catch {
      return respond(400, { error: "Invalid conversation or cursor" });
    }
    try {
      const result = await ctx.runQuery(internal.messages.thread, { conversation, paginationOpts: { cursor, numItems: 25 } });
      return respond(200, { messages: result.messages,
        cursor: result.cursor === null ? null : JSON.stringify({ conversation, cursor: result.cursor }) });
    } catch (error) {
      if (error instanceof Error && /cursor|not valid JSON|Unexpected token/i.test(error.message))
        return respond(400, { error: "Invalid thread cursor" });
      throw error;
    }
  }),
});

http.route({
  path: "/v1/messages/search",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    const token = process.env.KAIFORWARD_READ_TOKEN;
    if (!token) return respond(503, { error: "Message viewing not configured" });
    if (!sameSecret(request.headers.get("Authorization") ?? "", `Bearer ${token}`))
      return respond(401, { error: "Unauthorized" });
    let query: string;
    let cursor: string | null;
    try {
      const raw = await request.arrayBuffer();
      if (raw.byteLength > 16_384) return respond(413, { error: "Search request too large" });
      const payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
      if (typeof payload?.query !== "string") throw new Invalid("Invalid query");
      query = payload.query.trim();
      if (!query || query.length > 256 || /[\u0000-\u001f]/u.test(query)) throw new Invalid("Invalid query");
      const terms = query.split(/[\s\p{P}\p{S}]+/u).filter(Boolean);
      if (terms.length < 1 || terms.length > 16 || terms.some((term) => [...term].length > 32))
        throw new Invalid("Invalid query");
      // Treat a formatted telephone number as one searchable digit token.
      if (/^[+\d\s().-]+$/.test(query)) query = query.replace(/\D/g, "");
      cursor = null;
      if (payload.cursor !== undefined && payload.cursor !== null) {
        if (typeof payload.cursor !== "string" || payload.cursor.length > 8192) throw new Invalid("Invalid cursor");
        const saved = JSON.parse(payload.cursor);
        if (saved.query !== query || typeof saved.cursor !== "string" || !saved.cursor || saved.cursor.length > 4096)
          throw new Invalid("Invalid cursor");
        cursor = saved.cursor;
      }
    } catch {
      return respond(400, { error: "Invalid search query or cursor" });
    }
    try {
      const result = await ctx.runQuery(internal.messages.search, { query, paginationOpts: { cursor, numItems: 25 } });
      return respond(200, {
        messages: result.messages,
        cursor: result.cursor === null ? null : JSON.stringify({ query, cursor: result.cursor }),
      });
    } catch (error) {
      if (error instanceof Error && /cursor|not valid JSON|Unexpected token/i.test(error.message))
        return respond(400, { error: "Invalid search cursor" });
      throw error;
    }
  }),
});

http.route({
  path: "/v1/messages",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    const token = process.env.KAIFORWARD_TOKEN;
    if (!token) return respond(503, { error: "Receiver not configured" });
    if (!sameSecret(request.headers.get("Authorization") ?? "", `Bearer ${token}`))
      return respond(401, { error: "Unauthorized" });
    const length = Number(request.headers.get("Content-Length") ?? "0");
    if (!(length > 0 && length <= MAX_BODY)) return respond(413, { error: "Body size outside limits" });
    let messages;
    try {
      const raw = await request.arrayBuffer();
      if (raw.byteLength > MAX_BODY) return respond(413, { error: "Body size outside limits" });
      messages = validate(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)));
    } catch (error) {
      if (error instanceof Invalid || error instanceof SyntaxError || error instanceof TypeError)
        return respond(400, { error: "Invalid request" });
      throw error;
    }
    let accepted;
    try {
      accepted = await ctx.runMutation(internal.messages.ingest, { messages });
    } catch (error) {
      if (error instanceof ConvexError) return respond(409, { error: "Message key conflict" });
      throw error;
    }
    // The phone marks a message uploaded only on exactly {"accepted":[key]}.
    return respond(200, { accepted });
  }),
});

export default http;
