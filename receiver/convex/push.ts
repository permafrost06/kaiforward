import { v } from "convex/values";
import { internalMutation, internalQuery } from "./_generated/server";
import { internal } from "./_generated/api";

export const register = internalMutation({
  args: { installation: v.string(), token: v.string(), credential: v.string() },
  handler: async (ctx, args) => {
    const prior = await ctx.db.query("pushDevices").withIndex("by_installation", q => q.eq("installation", args.installation)).unique();
    const value = { ...args, updatedAt: Date.now() };
    if (prior) await ctx.db.patch(prior._id, value);
    else await ctx.db.insert("pushDevices", value);
  },
});

// Paginate fanout so the number of installations never exceeds a transaction's limits.
export const enqueue = internalMutation({
  args: { messageId: v.id("messages"), receivedAt: v.number(), cursor: v.union(v.string(), v.null()) },
  handler: async (ctx, args) => {
    const page = await ctx.db.query("pushDevices").paginate({ cursor: args.cursor, numItems: 100 });
    for (const device of page.page) {
      if (device.updatedAt < args.receivedAt - 60 * 86400_000) await ctx.db.delete(device._id);
      else if (device._creationTime <= args.receivedAt) await ctx.scheduler.runAfter(0, internal.pushDelivery.send, {
        deviceId: device._id, messageId: args.messageId, attempt: 0,
      });
    }
    if (!page.isDone) await ctx.scheduler.runAfter(0, internal.push.enqueue, { ...args, cursor: page.continueCursor });
  },
});

export const device = internalQuery({
  args: { id: v.id("pushDevices") },
  handler: (ctx, { id }) => ctx.db.get(id),
});

export const remove = internalMutation({
  args: { id: v.id("pushDevices"), token: v.string() },
  handler: async (ctx, { id, token }) => {
    const device = await ctx.db.get(id);
    if (device?.token === token) await ctx.db.delete(id);
  },
});

export const message = internalQuery({
  args: { id: v.id("messages") },
  handler: (ctx, { id }) => ctx.db.get(id),
});
