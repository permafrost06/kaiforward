import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const message = {
  key: v.string(),
  id: v.number(),
  timestamp: v.number(),
  sender: v.string(),
  recipient: v.string(),
  direction: v.union(v.literal("received"), v.literal("sent")),
  body: v.string(),
  simSlot: v.optional(v.union(v.literal(1), v.literal(2))),
};

export default defineSchema({
  conversations: defineTable({
    key: v.string(),
    number: v.string(),
    lastMessageId: v.id("messages"),
    lastTimestamp: v.number(),
    lastKey: v.string(),
    lastMessage: v.object({ ...message }),
  })
    .index("by_key", ["key"])
    .index("by_latest", ["lastTimestamp", "lastKey"]),
  messages: defineTable({
    ...message,
    receivedAt: v.number(),
    searchText: v.optional(v.string()),
    conversation: v.optional(v.string()),
    conversationId: v.optional(v.id("conversations")),
  })
    .index("by_key", ["key"])
    .index("by_timestamp", ["timestamp"])
    .index("by_conversation_timestamp", ["conversation", "timestamp"])
    .searchIndex("search_messages", { searchField: "searchText" }),
});
