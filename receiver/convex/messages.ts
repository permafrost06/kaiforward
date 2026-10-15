import { ConvexError, v } from "convex/values";
import { internalAction, internalMutation, internalQuery, type MutationCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { paginationOptsValidator } from "convex/server";
import { message } from "./schema";
import type { Id } from "./_generated/dataModel";

const incoming = v.object(message);
type Incoming = typeof incoming.type;

/** Matches Android conversationKey: preserve country prefixes and sender IDs. */
export function conversationKey(number: string) {
  const trimmed = number.trim();
  if (/\p{L}/u.test(trimmed)) return trimmed.toLowerCase();
  return trimmed.replace(/[^\p{Nd}+]/gu, "") || trimmed;
}
const conversationFor = (item: Incoming) => conversationKey(item.direction === "sent" ? item.recipient : item.sender);

/** Summaries are maintained in the message transaction, including during backfill. */
async function assignConversation(ctx: MutationCtx, id: Id<"messages">, item: Incoming) {
  const key = conversationFor(item);
  const prior = await ctx.db.query("conversations").withIndex("by_key", q => q.eq("key", key)).unique();
  const { key: messageKey, id: smsId, timestamp, sender, recipient, direction, body, simSlot } = item;
  const summary = {
    key, number: direction === "sent" ? recipient : sender,
    lastMessageId: id, lastTimestamp: timestamp, lastKey: messageKey,
    // Keep inbox pages small; the complete body remains in messages.
    lastMessage: { key: messageKey, id: smsId, timestamp, sender, recipient, direction,
      body: [...body].slice(0, 256).join(""), ...(simSlot === undefined ? {} : { simSlot }) },
  };
  let conversationId = prior?._id;
  if (prior === null) conversationId = await ctx.db.insert("conversations", summary);
  else if (timestamp > prior.lastTimestamp || (timestamp === prior.lastTimestamp && messageKey > prior.lastKey))
    await ctx.db.patch(prior._id, summary);
  await ctx.db.patch(id, { conversation: key, conversationId: conversationId! });
}

export const conversations = internalQuery({
  args: { paginationOpts: paginationOptsValidator },
  handler: async (ctx, { paginationOpts }) => {
    const page = await ctx.db.query("conversations").withIndex("by_latest").order("desc").paginate(paginationOpts);
    return { conversations: page.page.map(({ key, number, lastMessage }) => ({ key, number, lastMessage })),
      cursor: page.isDone ? null : page.continueCursor };
  },
});

export const backfillConversations = internalMutation({
  args: { cursor: v.union(v.null(), v.string()) },
  handler: async (ctx, { cursor }) => {
    const page = await ctx.db.query("messages").paginate({ cursor, numItems: 100, maximumBytesRead: 1_000_000 });
    let assigned = 0;
    for (const item of page.page) {
      if (item.conversationId === undefined || item.conversation !== conversationFor(item)) {
        await assignConversation(ctx, item._id, item);
        assigned++;
      }
    }
    return { assigned, cursor: page.isDone ? null : page.continueCursor };
  },
});

export const rebuildConversations = internalAction({
  args: { cursor: v.union(v.null(), v.string()) },
  handler: async (ctx, args): Promise<{ assigned: number; cursor: string | null }> => {
    let cursor = args.cursor;
    let assigned = 0;
    for (let batch = 0; batch < 100; batch++) {
      const page: { assigned: number; cursor: string | null } = await ctx.runMutation(internal.messages.backfillConversations, { cursor });
      assigned += page.assigned;
      cursor = page.cursor;
      if (cursor === null) break;
    }
    return { assigned, cursor };
  },
});

export const thread = internalQuery({
  args: { conversation: v.string(), paginationOpts: paginationOptsValidator },
  handler: async (ctx, { conversation, paginationOpts }) => {
    const result = await ctx.db.query("messages")
      .withIndex("by_conversation_timestamp", (q) => q.eq("conversation", conversation))
      .order("desc").paginate(paginationOpts);
    return {
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, conversationId, ...message }) => message),
      cursor: result.isDone ? null : result.continueCursor,
    };
  },
});

function searchText(item: Incoming) {
  const digits = (number: string) => /[a-z]/i.test(number) ? "" : number.replace(/\D/g, "");
  return [item.body, item.sender, item.recipient, digits(item.sender), digits(item.recipient)].join("\n");
}

/** Search a single combined index so pagination never repeats across fields. */
export const search = internalQuery({
  args: { query: v.string(), paginationOpts: paginationOptsValidator },
  handler: async (ctx, { query, paginationOpts }) => {
    const result = await ctx.db.query("messages")
      .withSearchIndex("search_messages", (q) => q.search("searchText", query))
      .paginate(paginationOpts);
    return {
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, conversationId, ...message }) => message),
      cursor: result.isDone ? null : result.continueCursor,
    };
  },
});

/** Bounded migration: add derived search and conversation fields without changing SMS data. */
export const backfillSearch = internalMutation({
  args: { cursor: v.union(v.null(), v.string()) },
  handler: async (ctx, { cursor }) => {
    const result = await ctx.db.query("messages").paginate({ cursor, numItems: 100, maximumBytesRead: 1_000_000 });
    let patched = 0;
    for (const item of result.page) {
      if (item.searchText === undefined || item.conversation === undefined) {
        await ctx.db.patch(item._id, { searchText: searchText(item), conversation: conversationFor(item) });
        patched++;
      }
    }
    return { patched, cursor: result.isDone ? null : result.continueCursor };
  },
});

export const rebuildSearch = internalAction({
  args: { cursor: v.union(v.null(), v.string()) },
  handler: async (ctx, args): Promise<{ patched: number; cursor: string | null }> => {
    let cursor = args.cursor;
    let patched = 0;
    for (let batch = 0; batch < 100; batch++) {
      const result: { patched: number; cursor: string | null } = await ctx.runMutation(internal.messages.backfillSearch, { cursor });
      patched += result.patched;
      cursor = result.cursor;
      if (cursor === null) break;
    }
    return { patched, cursor };
  },
});

/** Private: only the authenticated read HTTP action exposes message history. */
export const list = internalQuery({
  args: { paginationOpts: paginationOptsValidator },
  handler: async (ctx, { paginationOpts }) => {
    const result = await ctx.db.query("messages").withIndex("by_timestamp").order("desc").paginate(paginationOpts);
    return {
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, conversationId, ...message }) => message),
      cursor: result.isDone ? null : result.continueCursor,
    };
  },
});

async function store(ctx: MutationCtx, item: Incoming, receivedAt: number) {
  const prior = await ctx.db
    .query("messages")
    .withIndex("by_key", (q) => q.eq("key", item.key))
    .unique();
  if (!prior) {
    const id = await ctx.db.insert("messages", { ...item, receivedAt, searchText: searchText(item) });
    await assignConversation(ctx, id, item);
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

