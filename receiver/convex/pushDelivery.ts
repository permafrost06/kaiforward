"use node";

import { createHash, createSign } from "node:crypto";
import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { internal } from "./_generated/api";

// Service credentials stay in Convex; message previews travel through FCM.
let cached: { credential: string; token: string; expires: number } | undefined;
async function accessToken(raw: string) {
  if (cached?.credential === raw && cached.expires > Date.now()) return cached.token;
  const account = JSON.parse(raw);
  const now = Math.floor(Date.now() / 1000);
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({
    iss: account.client_email, scope: "https://www.googleapis.com/auth/firebase.messaging",
    aud: "https://oauth2.googleapis.com/token", iat: now, exp: now + 3600,
  })}`;
  const signature = createSign("RSA-SHA256").update(unsigned).sign(account.private_key, "base64url");
  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST", signal: AbortSignal.timeout(15_000),
    body: new URLSearchParams({ grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer", assertion: `${unsigned}.${signature}` }),
  });
  if (!response.ok) throw new Error(`Push OAuth failed (${response.status})`);
  const result = await response.json();
  cached = { credential: raw, token: result.access_token, expires: Date.now() + (result.expires_in - 60) * 1000 };
  return cached.token;
}

export const send = internalAction({
  args: { deviceId: v.id("pushDevices"), messageId: v.id("messages"), attempt: v.number() },
  handler: async (ctx, args) => {
    const raw = process.env.FIREBASE_SERVICE_ACCOUNT_JSON;
    const readToken = process.env.KAIFORWARD_READ_TOKEN;
    if (!raw || !readToken) return; // Push is optional; uploads never depend on Firebase availability.
    const device = await ctx.runQuery(internal.push.device, { id: args.deviceId });
    if (!device || device.credential !== createHash("sha256").update(readToken).digest("hex")) return;
    const message = await ctx.runQuery(internal.push.message, { id: args.messageId });
    if (!message || message.direction !== "received") return;
    const characters = [...message.body];
    // Keep the entire data payload comfortably below FCM's 4096-byte limit.
    let limit = Math.min(700, characters.length);
    const data = { sender: message.sender, body: "", conversation: message.conversation ?? message.sender, messageKey: message.key };
    do {
      data.body = characters.slice(0, limit).join("") + (characters.length > limit ? "…" : "");
      if (Buffer.byteLength(JSON.stringify(data), "utf8") <= 3500) break;
      limit--;
    } while (limit >= 0);
    try {
      const token = await accessToken(raw);
      const project = JSON.parse(raw).project_id;
      const response = await fetch(`https://fcm.googleapis.com/v1/projects/${encodeURIComponent(project)}/messages:send`, {
        method: "POST", signal: AbortSignal.timeout(15_000),
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ message: {
          token: device.token,
          data,
          android: { priority: "high", ttl: "86400s" },
        } }),
      });
      if (response.ok) return;
      const result = await response.json().catch(() => ({}));
      if (result.error?.details?.some((detail: { errorCode?: string }) => detail.errorCode === "UNREGISTERED")) {
        await ctx.runMutation(internal.push.remove, { id: device._id, token: device.token });
        return;
      }
      if (response.status === 401) cached = undefined;
      if (response.status === 429 || response.status >= 500 || response.status === 401) throw new Error(`Push send failed (${response.status})`);
      // Never log Firebase responses: they can contain registration tokens.
      console.error(`Push rejected (${response.status}); check Firebase project and service account`);
    } catch {
      if (args.attempt < 4) await ctx.scheduler.runAfter(60_000 * 2 ** args.attempt, internal.pushDelivery.send, {
        ...args, attempt: args.attempt + 1,
      });
      else console.error("Push delivery failed after retries; check Firebase configuration");
    }
  },
});
