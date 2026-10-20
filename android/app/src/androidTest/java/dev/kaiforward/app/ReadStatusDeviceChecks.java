package dev.kaiforward.app;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/** Opt-in local fixtures using platform APIs, independent of release class optimization. */
public final class ReadStatusDeviceChecks {
    public static void run(Instrumentation runner, Bundle options) {
        Bundle result = new Bundle();
        try {
            String message = options.containsKey("seed_unread_top")
                ? seed(runner.getTargetContext(), Integer.parseInt(options.getString("seed_unread_top")))
                : verify(runner);
            result.putString("stream", message + "\n");
            runner.finish(Activity.RESULT_OK, result);
        } catch (Exception error) {
            result.putString("stream", "Read status device check failed: " + error.getClass().getSimpleName() + ".\n");
            runner.finish(Activity.RESULT_CANCELED, result);
        }
    }

    private static String seed(Context context, int count) throws Exception {
        if (count < 1 || count > 10) throw new IllegalArgumentException();
        List<String> keys = new ArrayList<>();
        List<Long> arrivals = new ArrayList<>();
        String cursor = null;
        do {
            String suffix = cursor == null ? "" : "?cursor=" + android.net.Uri.encode(cursor);
            HttpURLConnection request = (HttpURLConnection) new URL(BuildConfig.RECEIVER_URL + "/v1/conversations" + suffix).openConnection();
            JSONObject page;
            try {
                request.setConnectTimeout(15000);
                request.setReadTimeout(20000);
                request.setRequestProperty("Authorization", "Bearer " + BuildConfig.APP_TOKEN);
                if (request.getResponseCode() != 200) throw new IllegalStateException();
                StringBuilder text = new StringBuilder();
                try (BufferedReader reader = new BufferedReader(new InputStreamReader(request.getInputStream(), java.nio.charset.StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = reader.readLine()) != null) text.append(line);
                }
                page = new JSONObject(text.toString());
            } finally { request.disconnect(); }
            JSONArray rows = page.getJSONArray("conversations");
            for (int index = 0; index < rows.length(); index++) {
                JSONObject row = rows.getJSONObject(index);
                keys.add(row.getString("key"));
                arrivals.add(row.isNull("lastIncomingAt") ? null : row.getLong("lastIncomingAt"));
            }
            cursor = page.isNull("cursor") ? null : page.getString("cursor");
        } while (cursor != null);
        SharedPreferences prefs = context.getSharedPreferences("message-read-status", Context.MODE_PRIVATE);
        if (!prefs.contains("firstSyncAt")) throw new IllegalStateException("Open app online first");
        long original = prefs.getLong("firstSyncAt", 0);
        long baseline = original;
        JSONObject cutoffs = new JSONObject(prefs.getString("openedThrough", "{}"));
        int selected = 0;
        for (int index = 0; index < keys.size(); index++) {
            String key = keys.get(index);
            Long arrival = arrivals.get(index);
            if (arrival != null && selected < count) {
                cutoffs.put(key, arrival - 1);
                baseline = Math.min(baseline, arrival - 1);
                selected++;
            } else cutoffs.put(key, Math.max(original, cutoffs.optLong(key, 0)));
        }
        if (selected == 0) throw new IllegalStateException();
        if (!prefs.edit().putLong("firstSyncAt", baseline).putString("openedThrough", cutoffs.toString()).commit())
            throw new IllegalStateException();
        return "Seeded " + selected + " newest incoming conversations as unread on this device only.";
    }

    private static String verify(Instrumentation runner) throws Exception {
        if (android.os.Build.VERSION.SDK_INT < 30) throw new IllegalStateException();
        runner.startActivitySync(new Intent().setClassName(runner.getTargetContext(), "dev.kaiforward.app.MainActivity")
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
        List<AccessibilityNodeInfo> unread = new ArrayList<>();
        for (int attempt = 0; attempt < 40 && unread.isEmpty(); attempt++) {
            SystemClock.sleep(250);
            unread = unreadNodes(runner.getUiAutomation().getRootInActiveWindow());
        }
        int before = unread.size();
        if (before < 3) throw new IllegalStateException("Expected seeded unread rows");
        AccessibilityNodeInfo row = unread.get(0);
        while (!row.isClickable()) {
            row = row.getParent();
            if (row == null) throw new IllegalStateException();
        }
        if (!row.performAction(AccessibilityNodeInfo.ACTION_CLICK)) throw new IllegalStateException();
        boolean loaded = false;
        for (int attempt = 0; attempt < 60 && !loaded; attempt++) {
            SystemClock.sleep(250);
            loaded = loadedMessages(runner.getUiAutomation().getRootInActiveWindow());
        }
        if (!loaded) throw new IllegalStateException("Conversation did not load");
        runner.sendKeyDownUpSync(KeyEvent.KEYCODE_BACK);
        runner.waitForIdleSync();
        SystemClock.sleep(500);
        int after = unreadNodes(runner.getUiAutomation().getRootInActiveWindow()).size();
        if (after != before - 1) throw new IllegalStateException("Unread status did not clear");
        SharedPreferences prefs = runner.getTargetContext().getSharedPreferences("message-read-status", Context.MODE_PRIVATE);
        if (new JSONObject(prefs.getString("openedThrough", "{}")).length() == 0) throw new IllegalStateException();
        return "Read status OK: " + before + " visible unread rows; opening one leaves " + after + " and saves its read cutoff locally.";
    }

    private static List<AccessibilityNodeInfo> unreadNodes(AccessibilityNodeInfo node) {
        List<AccessibilityNodeInfo> result = new ArrayList<>();
        if (node == null) return result;
        if (android.os.Build.VERSION.SDK_INT >= 30 && "Unread".contentEquals(node.getStateDescription() == null ? "" : node.getStateDescription())) result.add(node);
        for (int i = 0; i < node.getChildCount(); i++) result.addAll(unreadNodes(node.getChild(i)));
        return result;
    }

    private static boolean loadedMessages(AccessibilityNodeInfo node) {
        if (node == null) return false;
        if (node.getText() != null && node.getText().toString().matches("[1-9][0-9]* loaded messages")) return true;
        for (int i = 0; i < node.getChildCount(); i++) if (loadedMessages(node.getChild(i))) return true;
        return false;
    }
}
