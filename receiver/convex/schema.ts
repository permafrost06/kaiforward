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
  messages: defineTable({
    ...message,
    receivedAt: v.number(),
  })
    .index("by_key", ["key"])
    .index("by_timestamp", ["timestamp"]),
});
