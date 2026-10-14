import { ConvexError, v } from "convex/values";
import { internalMutation, type MutationCtx } from "./_generated/server";
import { message } from "./schema";

const incoming = v.object(message);
type Incoming = typeof incoming.type;

async function store(ctx: MutationCtx, item: Incoming, receivedAt: number) {
  const prior = await ctx.db
    .query("messages")
    .withIndex("by_key", (q) => q.eq("key", item.key))
    .unique();
  if (!prior) {
    await ctx.db.insert("messages", { ...item, receivedAt });
    return;
  }
  // A replayed key is accepted only with identical content, as in the PoC receiver.
  const same = (Object.keys(message) as (keyof Incoming)[]).every((field) => prior[field] === item[field]);
  if (!same) throw new ConvexError({ conflict: item.key });
}

/** Stores one upload batch atomically; replays of stored messages are no-ops and a conflict rolls back the batch. */
export const ingest = internalMutation({
  args: { messages: v.array(incoming) },
  returns: v.array(v.string()),
  handler: async (ctx, { messages }) => {
    const receivedAt = Date.now();
    for (const item of messages) await store(ctx, item, receivedAt);
    return messages.map((item) => item.key);
  },
});

