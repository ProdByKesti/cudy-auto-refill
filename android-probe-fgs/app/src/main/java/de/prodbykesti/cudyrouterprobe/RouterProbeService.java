package de.prodbykesti.cudyrouterprobe;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;

public class RouterProbeService extends Service {
    public static final String ACTION_START = "de.prodbykesti.cudyrouterprobe.START";
    public static final String ACTION_STOP = "de.prodbykesti.cudyrouterprobe.STOP";
    public static final String PREFS_NAME = "router_probe";
    public static final String PREF_RESULTS = "results";
    public static final String PREF_STATUS = "status";

    private static final String CHANNEL_ID = "router_probe_running";
    private static final int NOTIFICATION_ID = 4102;
    private static final int CHECK_COUNT = 12;
    private static final long INTERVAL_MS = 5_000L;
    private volatile boolean running;
    private Thread worker;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopMonitoring();
            return START_NOT_STICKY;
        }
        if (!running) {
            running = true;
            clearOldResults();
            createNotificationChannel();
            startForeground(NOTIFICATION_ID, buildNotification("Starting router checks…"));
            setStatus("Background test running. You can switch to the Phone app now.");
            worker = new Thread(this::runChecks, "router-foreground-probe");
            worker.start();
        }
        return START_NOT_STICKY;
    }

    private void runChecks() {
        int errors = 0;
        for (int attempt = 1; attempt <= CHECK_COUNT && running; attempt++) {
            long startedAt = System.nanoTime();
            String result = makeRouterRequest();
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000L;
            boolean succeeded = result.startsWith("HTTP ");
            if (!succeeded) {
                errors++;
            }
            String row = String.format(Locale.ROOT, "%02d/%02d  %s  (%d ms)",
                    attempt, CHECK_COUNT, result, elapsedMs);
            appendResult(row);
            updateNotification("Check " + attempt + "/" + CHECK_COUNT + " — " + errors + " errors");
            setStatus("Background test running: " + attempt + "/" + CHECK_COUNT
                    + "; connection errors: " + errors + ".");
            if (attempt < CHECK_COUNT && running) {
                try {
                    Thread.sleep(INTERVAL_MS);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }

        if (running) {
            setStatus("Background test complete: " + CHECK_COUNT + " checks; connection errors: " + errors + ".");
            appendResult("Finished — connection errors: " + errors + " of " + CHECK_COUNT + ".");
        } else {
            setStatus("Background test stopped by the user.");
            appendResult("Stopped by user.");
        }
        running = false;
        stopSelf();
    }

    private void stopMonitoring() {
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        setStatus("Background test stopped by the user.");
        stopForegroundCompat();
        stopSelf();
    }

    private void clearOldResults() {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit()
                .putString(PREF_RESULTS, "")
                .putString(PREF_STATUS, "Background test starting.")
                .apply();
    }

    private void appendResult(String row) {
        android.content.SharedPreferences preferences = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String current = preferences.getString(PREF_RESULTS, "");
        String updated = current == null || current.isEmpty() ? row : current + "\n" + row;
        preferences.edit().putString(PREF_RESULTS, updated).apply();
    }

    private void setStatus(String status) {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit().putString(PREF_STATUS, status).apply();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Cudy router connection test", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Shows progress while the user-started router connection test runs.");
            NotificationManager manager = getSystemService(NotificationManager.class);
            manager.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification(String text) {
        Intent stopIntent = new Intent(this, RouterProbeService.class);
        stopIntent.setAction(ACTION_STOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent stopPendingIntent = PendingIntent.getService(this, 0, stopIntent, flags);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
                .setContentTitle("Cudy connection test")
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .addAction(android.R.drawable.ic_media_pause, "Stop", stopPendingIntent);
        return builder.build();
    }

    private void updateNotification(String text) {
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.notify(NOTIFICATION_ID, buildNotification(text));
    }

    private void stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= 24) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            stopForeground(true);
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        stopForegroundCompat();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    static String makeRouterRequest() {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http://192.168.10.1/").openConnection();
            connection.setRequestMethod("GET");
            connection.setConnectTimeout(5_000);
            connection.setReadTimeout(5_000);
            connection.setInstanceFollowRedirects(true);
            int code = connection.getResponseCode();
            return "HTTP " + code;
        } catch (IOException exception) {
            String message = exception.getMessage();
            return "ERROR " + (message == null ? exception.getClass().getSimpleName() : message);
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }
}
