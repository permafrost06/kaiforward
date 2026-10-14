import { httpRouter } from "convex/server";
import { ConvexError } from "convex/values";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";

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
