import { ConvexError, v } from "convex/values";
import { internalAction, internalMutation, internalQuery, type MutationCtx } from "./_generated/server";
import { internal } from "./_generated/api";
import { paginationOptsValidator } from "convex/server";
import { message } from "./schema";

const incoming = v.object(message);
type Incoming = typeof incoming.type;

/** Matches Android conversationKey: preserve country prefixes and sender IDs. */
export function conversationKey(number: string) {
  const trimmed = number.trim();
  if (/\p{L}/u.test(trimmed)) return trimmed.toLowerCase();
  return trimmed.replace(/[^\p{Nd}+]/gu, "") || trimmed;
}
const conversationFor = (item: Incoming) => conversationKey(item.direction === "sent" ? item.recipient : item.sender);

export const thread = internalQuery({
  args: { conversation: v.string(), paginationOpts: paginationOptsValidator },
  handler: async (ctx, { conversation, paginationOpts }) => {
    const result = await ctx.db.query("messages")
      .withIndex("by_conversation_timestamp", (q) => q.eq("conversation", conversation))
      .order("desc").paginate(paginationOpts);
    return {
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, ...message }) => message),
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
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, ...message }) => message),
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
      messages: result.page.map(({ _id, _creationTime, searchText, conversation, ...message }) => message),
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
    await ctx.db.insert("messages", { ...item, receivedAt, searchText: searchText(item), conversation: conversationFor(item) });
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

