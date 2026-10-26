package de.prodbykesti.cudyapp;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** User-started, notification-visible router Inbox monitor and trigger-based refill worker. */
public final class AutomaticRefillService extends Service {
    static final String ACTION_START = "de.prodbykesti.cudyapp.action.START_MONITORING";
    static final String ACTION_STOP = "de.prodbykesti.cudyapp.action.STOP_MONITORING";
    static final String ACTION_LOG_UPDATED = "de.prodbykesti.cudyapp.action.LOG_UPDATED";
    static final String ACTION_STATE_CHANGED = "de.prodbykesti.cudyapp.action.MONITOR_STATE_CHANGED";
    static final String EXTRA_MONITOR_STATE = "monitor_state";
    static final String STATE_PREFS = "cudy_refill_monitor_state";
    static final String KEY_ENABLED = "enabled";

    private static final String CHANNEL_ID = "cudy_refill_monitor_status";
    private static final int NOTIFICATION_ID = 8016;
    private static final long POLL_INTERVAL_MS = 10_000L;
    private static final long SESSION_REFRESH_MS = 4 * 60_000L;
    private static final String KEY_READY = "ready";
    private static final long PENDING_WARNING_MS = 5 * 60_000L;
    private static final int MAX_KNOWN_IDS = 500;
    private static final String KEY_SEEDED = "inbox_baseline_seeded";
    private static final String KEY_KNOWN_IDS = "known_inbox_ids";
    private static final String KEY_QUEUE = "trigger_queue";
    private static final String KEY_PENDING_ID = "pending_sms_id";
    private static final String KEY_PENDING_BASELINE = "pending_outbox_baseline";
    private static final String KEY_PENDING_SINCE = "pending_since";
    private static final String KEY_PENDING_WARNING_LOGGED = "pending_warning_logged";
    private static volatile boolean running;

    private SharedPreferences state;
    private ScheduledExecutorService worker;
    private PowerManager.WakeLock wakeLock;
    private CudyRouterClient client;
    private long lastLoginAt;
    private String lastTransientError = "";
    private volatile boolean stopping;

    static boolean isEnabled(Context context) {
        return running && context.getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
                .getBoolean(KEY_ENABLED, false);
    }

    static boolean isReady(Context context) {
        return isEnabled(context) && context.getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
                .getBoolean(KEY_READY, false);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        state = getSharedPreferences(STATE_PREFS, MODE_PRIVATE);
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopMonitoring("Background refill monitoring stopped by the user.");
            return START_NOT_STICKY;
        }
        if (ACTION_START.equals(action)) {
            state.edit().putBoolean(KEY_ENABLED, true).putBoolean(KEY_READY, false).apply();
        } else if (!state.getBoolean(KEY_ENABLED, false)) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }

        try {
            startForegroundWithNotification("Starting router monitoring…");
            running = true;
            broadcastMonitorState("starting");
            acquireWakeLock();
            if (worker == null || worker.isShutdown()) {
                stopping = false;
                worker = Executors.newSingleThreadScheduledExecutor(runnable -> {
                    Thread thread = new Thread(runnable, "cudy-refill-monitor");
                    thread.setDaemon(true);
                    return thread;
                });
                worker.scheduleWithFixedDelay(this::pollSafely, 0, POLL_INTERVAL_MS, TimeUnit.MILLISECONDS);
                logEvent("MONITOR | started by the user; Inbox page 1 is checked every 10 seconds.");
            }
            return START_STICKY;
        } catch (RuntimeException error) {
            running = false;
            state.edit().putBoolean(KEY_ENABLED, false).putBoolean(KEY_READY, false).apply();
            logEvent("MONITOR | could not start: " + error.getClass().getSimpleName() + ".");
            broadcastMonitorState("error");
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf(startId);
            return START_NOT_STICKY;
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        stopping = true;
        running = false;
        if (state != null) state.edit().putBoolean(KEY_READY, false).apply();
        if (worker != null) worker.shutdownNow();
        releaseWakeLock();
        super.onDestroy();
    }

    private void pollSafely() {
        if (stopping || !state.getBoolean(KEY_ENABLED, false)) return;
        try {
            ensureRouterSession();
            List<CudyRouterClient.SmsMessage> inbox = readInboxWithSessionRecovery();
            if (!state.getBoolean(KEY_SEEDED, false)) {
                Set<String> baseline = readKnownIds();
                for (CudyRouterClient.SmsMessage message : inbox) baseline.add(message.id);
                saveKnownIds(baseline);
                state.edit().putBoolean(KEY_SEEDED, true).apply();
                logEvent("INBOX | initial baseline saved; existing messages will not trigger a refill.");
            } else {
                enqueueNewTriggers(inbox);
            }

            if (!state.getBoolean(KEY_READY, false)) {
                state.edit().putBoolean(KEY_READY, true).apply();
                logEvent("MONITOR | ready; the first Inbox check completed successfully.");
                broadcastMonitorState("running");
            }

            boolean pending = checkPendingOutbox();
            if (!pending) processNextTrigger();
            lastTransientError = "";
            String status = pending
                    ? (state.getBoolean(KEY_PENDING_WARNING_LOGGED, false)
                    ? "No Outbox confirmation; automatic sending is paused."
                    : "Refill sent; checking for router confirmation…")
                    : "Inbox check successful";
            updateNotification(status + " · Last check " + currentTime());
        } catch (Exception error) {
            String message = safeError(error);
            if (!message.equals(lastTransientError)) {
                logEvent("MONITOR | router check failed: " + message);
                lastTransientError = message;
            }
            updateNotification("Router check failed · Last attempt " + currentTime()
                    + " · retry in 10 seconds");
        }
    }

    private void ensureRouterSession() throws Exception {
        long now = android.os.SystemClock.elapsedRealtime();
        if (client != null && now - lastLoginAt < SESSION_REFRESH_MS) return;
        String password = new CredentialVault(this).loadPassword();
        if (password == null || password.isEmpty()) {
            throw new IOException("No encrypted router password is available. Set up the router again.");
        }
        SharedPreferences appPrefs = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
        String url = appPrefs.getString(MainActivity.ROUTER_URL, "http://192.168.10.1");
        String username = appPrefs.getString(MainActivity.ROUTER_USERNAME, "admin");
        client = new CudyRouterClient(url, username, password, this::logEvent);
        client.loginAndVerify();
        lastLoginAt = android.os.SystemClock.elapsedRealtime();
        logEvent("MONITOR | router login verified.");
    }

    private List<CudyRouterClient.SmsMessage> readInboxWithSessionRecovery() throws IOException {
        try {
            return client.readInboxFirstPage();
        } catch (IOException error) {
            if (!looksLikeExpiredSession(error)) throw error;
            client.loginAndVerify();
            lastLoginAt = android.os.SystemClock.elapsedRealtime();
            logEvent("MONITOR | router session renewed after an Inbox request failed.");
            return client.readInboxFirstPage();
        }
    }

    private List<CudyRouterClient.SmsMessage> readOutboxWithSessionRecovery() throws IOException {
        try {
            return client.readOutboxFirstPage();
        } catch (IOException error) {
            if (!looksLikeExpiredSession(error)) throw error;
            client.loginAndVerify();
            lastLoginAt = android.os.SystemClock.elapsedRealtime();
            logEvent("MONITOR | router session renewed after an Outbox request failed.");
            return client.readOutboxFirstPage();
        }
    }

    private static boolean looksLikeExpiredSession(IOException error) {
        String message = error.getMessage() == null ? "" : error.getMessage().toLowerCase(java.util.Locale.ROOT);
        return message.contains("http 403") || message.contains("session expired")
                || message.contains("login form");
    }

    private void enqueueNewTriggers(List<CudyRouterClient.SmsMessage> inbox) throws JSONException {
        Set<String> known = readKnownIds();
        JSONArray queue = readQueue();
        Set<String> queued = new HashSet<>();
        for (int index = 0; index < queue.length(); index++) {
            JSONObject item = queue.optJSONObject(index);
            if (item != null) queued.add(item.optString("id", ""));
        }
        for (CudyRouterClient.SmsMessage message : inbox) {
            if (!known.add(message.id)) continue;
            if (CudyRouterClient.isRefillTrigger(message.content)) {
                if (message.cfg.isEmpty()) {
                    logEvent("INBOX | refill-like SMS found, but the router detail reference was missing; no reply was sent.");
                } else if (queued.add(message.id)) {
                    JSONObject item = new JSONObject();
                    item.put("id", message.id);
                    item.put("cfg", message.cfg);
                    item.put("checks", 0);
                    queue.put(item);
                    logEvent("INBOX | a new refill trigger was detected; confirming the full sender and text.");
                }
            } else {
                logEvent("INBOX | a new SMS was seen; it did not match a refill trigger.");
            }
        }
        saveKnownIds(known);
        state.edit().putString(KEY_QUEUE, queue.toString()).apply();
    }

    private void processNextTrigger() throws Exception {
        JSONArray queue = readQueue();
        if (queue.length() == 0) return;
        JSONObject candidate = queue.optJSONObject(0);
        JSONArray remaining = new JSONArray();
        for (int index = 1; index < queue.length(); index++) {
            JSONObject item = queue.optJSONObject(index);
            if (item != null) remaining.put(item);
        }
        state.edit().putString(KEY_QUEUE, remaining.toString()).apply();
        if (candidate == null) return;

        String smsId = candidate.optString("id", "");
        String cfg = candidate.optString("cfg", "");
        int checks = candidate.optInt("checks", 0);
        CudyRouterClient.VerifiedTrigger verified;
        try {
            verified = client.prepareTriggerReply(cfg);
        } catch (IOException error) {
            if (isPermanentTriggerRejection(error)) {
                logEvent("REFILL | sender or full message check failed; no SMS was sent.");
                return;
            }
            requeueOnce(candidate, checks, error);
            return;
        }

        Set<String> outboxBaseline = new HashSet<>();
        try {
            for (CudyRouterClient.SmsMessage message : readOutboxWithSessionRecovery()) {
                if (CudyRouterClient.isLidlSender(message.sender)
                        && CudyRouterClient.isRefillOutboxText(message.content)) {
                    outboxBaseline.add(message.id);
                }
            }
        } catch (IOException error) {
            requeueOnce(candidate, checks, error);
            return;
        }

        state.edit()
                .putString(KEY_PENDING_ID, smsId)
                .putString(KEY_PENDING_BASELINE, toJsonArray(outboxBaseline))
                .putLong(KEY_PENDING_SINCE, System.currentTimeMillis())
                .putBoolean(KEY_PENDING_WARNING_LOGGED, false)
                .apply();
        logEvent("REFILL | trigger and sender verified; submitting one Refill reply and waiting for Outbox confirmation.");
        try {
            client.sendTriggerReply(verified);
            updateNotification("Refill sent; checking for router confirmation…");
        } catch (IOException error) {
            logEvent("REFILL | send result is uncertain; automatic sending is paused until Outbox confirmation.");
            updateNotification("Refill result uncertain; automatic sending is paused.");
        }
    }

    private void requeueOnce(JSONObject candidate, int checks, IOException error) throws JSONException {
        if (checks >= 1) {
            logEvent("REFILL | trigger could not be verified after two checks; no reply was sent. " + safeError(error));
            return;
        }
        candidate.put("checks", checks + 1);
        JSONArray queue = readQueue();
        JSONArray updated = new JSONArray();
        updated.put(candidate);
        for (int index = 0; index < queue.length(); index++) {
            JSONObject item = queue.optJSONObject(index);
            if (item != null) updated.put(item);
        }
        state.edit().putString(KEY_QUEUE, updated.toString()).apply();
        logEvent("REFILL | trigger verification was delayed by a router read error; one retry is queued.");
    }

    private static boolean isPermanentTriggerRejection(IOException error) {
        String message = error.getMessage() == null ? "" : error.getMessage().toLowerCase(java.util.Locale.ROOT);
        return message.contains("sender was not confirmed") || message.contains("did not match a configured refill trigger");
    }

    private boolean checkPendingOutbox() throws IOException {
        String pendingId = state.getString(KEY_PENDING_ID, "");
        if (pendingId.isEmpty()) return false;
        Set<String> baseline = fromJsonArray(state.getString(KEY_PENDING_BASELINE, "[]"));
        for (CudyRouterClient.SmsMessage message : readOutboxWithSessionRecovery()) {
            if (CudyRouterClient.isLidlSender(message.sender)
                    && CudyRouterClient.isRefillOutboxText(message.content)
                    && !baseline.contains(message.id)) {
                clearPendingConfirmation();
                logEvent("REFILL | a new matching Outbox entry confirmed the router accepted the reply.");
                updateNotification("Monitoring the router Inbox every 10 seconds.");
                return false;
            }
        }
        long sentAt = state.getLong(KEY_PENDING_SINCE, System.currentTimeMillis());
        if (System.currentTimeMillis() - sentAt >= PENDING_WARNING_MS
                && !state.getBoolean(KEY_PENDING_WARNING_LOGGED, false)) {
            state.edit().putBoolean(KEY_PENDING_WARNING_LOGGED, true).apply();
            logEvent("REFILL | no new Outbox entry appeared within five minutes; automatic sends remain paused for safety.");
            updateNotification("No Outbox confirmation; automatic sending is paused.");
        }
        return true;
    }

    private void clearPendingConfirmation() {
        state.edit().remove(KEY_PENDING_ID).remove(KEY_PENDING_BASELINE)
                .remove(KEY_PENDING_SINCE).remove(KEY_PENDING_WARNING_LOGGED).apply();
    }

    private Set<String> readKnownIds() {
        return fromJsonArray(state.getString(KEY_KNOWN_IDS, "[]"));
    }

    private void saveKnownIds(Set<String> ids) {
        List<String> bounded = new ArrayList<>(ids);
        if (bounded.size() > MAX_KNOWN_IDS) {
            bounded = bounded.subList(bounded.size() - MAX_KNOWN_IDS, bounded.size());
        }
        state.edit().putString(KEY_KNOWN_IDS, toJsonArray(bounded)).apply();
    }

    private JSONArray readQueue() {
        try {
            return new JSONArray(state.getString(KEY_QUEUE, "[]"));
        } catch (JSONException ignored) {
            return new JSONArray();
        }
    }

    private static String toJsonArray(Iterable<String> values) {
        JSONArray result = new JSONArray();
        for (String value : values) result.put(value);
        return result.toString();
    }

    private static Set<String> fromJsonArray(String json) {
        Set<String> result = new HashSet<>();
        try {
            JSONArray values = new JSONArray(json);
            for (int index = 0; index < values.length(); index++) {
                String value = values.optString(index, "");
                if (!value.isEmpty()) result.add(value);
            }
        } catch (JSONException ignored) {
            // A malformed old state is treated as empty; the first Inbox read reseeds it.
        }
        return result;
    }

    private static String safeError(Exception error) {
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) return error.getClass().getSimpleName();
        return message.replaceAll("[\\r\\n]+", " ");
    }

    private void stopMonitoring(String message) {
        stopping = true;
        running = false;
        state.edit().putBoolean(KEY_ENABLED, false).putBoolean(KEY_READY, false).apply();
        broadcastMonitorState("stopped");
        logEvent("MONITOR | " + message);
        if (worker != null) worker.shutdownNow();
        releaseWakeLock();
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) return;
        PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
        if (powerManager == null) return;
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                getPackageName() + ":refill-monitor");
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire();
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        wakeLock = null;
    }

    private void logEvent(String message) {
        MainActivity.appendPersistentDiagnosticLog(this, message);
        Intent update = new Intent(ACTION_LOG_UPDATED).setPackage(getPackageName());
        sendBroadcast(update);
    }

    private void broadcastMonitorState(String monitorState) {
        Intent update = new Intent(ACTION_STATE_CHANGED)
                .setPackage(getPackageName())
                .putExtra(EXTRA_MONITOR_STATE, monitorState);
        sendBroadcast(update);
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "Cudy monitoring status", NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Quiet ongoing status with the time of the latest router Inbox check.");
        channel.setSound(null, null);
        channel.enableVibration(false);
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        manager.createNotificationChannel(channel);
    }

    private void startForegroundWithNotification(String status) {
        Notification notification = buildNotification(status);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void updateNotification(String status) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.notify(NOTIFICATION_ID, buildNotification(status));
    }

    private Notification buildNotification(String status) {
        Intent openApp = new Intent(this, MainActivity.class);
        PendingIntent openPending = PendingIntent.getActivity(this, 0, openApp,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent stop = new Intent(this, AutomaticRefillService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(this, 1, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID) : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("Cudy Auto Refill")
                .setContentText(status)
                .setStyle(new Notification.BigTextStyle().bigText(status))
                .setWhen(System.currentTimeMillis())
                .setShowWhen(true)
                .setOnlyAlertOnce(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setContentIntent(openPending)
                .setOngoing(true)
                .setCategory(Notification.CATEGORY_SERVICE)
                .addAction(new Notification.Action.Builder(0, "Stop", stopPending).build());
        if (Build.VERSION.SDK_INT >= 31) {
            builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return builder.build();
    }

    private static String currentTime() {
        return new SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(new Date());
    }
}
