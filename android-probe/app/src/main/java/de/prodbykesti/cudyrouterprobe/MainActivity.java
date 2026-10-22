package de.prodbykesti.cudyrouterprobe;

import android.app.Activity;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Read-only connectivity probe. It never logs in or submits router settings. */
public final class MainActivity extends Activity {
    private static final String ROUTER_URL = "http://192.168.10.1/";
    private static final int MONITOR_REQUESTS = 12;
    private static final long MONITOR_INTERVAL_MS = 5_000L;

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private volatile boolean cancelMonitor;

    private TextView resultView;
    private Button monitorButton;
    private boolean monitorRunning;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        ScrollView scrollView = new ScrollView(this);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(24), dp(28), dp(24), dp(24));
        scrollView.addView(content);

        TextView title = new TextView(this);
        title.setText("Cudy Router Probe");
        title.setTextSize(24);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        content.addView(title, fullWidth());

        TextView explanation = new TextView(this);
        explanation.setText("Read-only connection check for the router on your local Wi-Fi. "
                + "This probe does not log in, read SMS, or change router settings.");
        explanation.setTextSize(16);
        addSpaced(content, explanation, 12);

        TextView target = new TextView(this);
        target.setText("Router address\n" + ROUTER_URL);
        target.setTextSize(16);
        addSpaced(content, target, 20);

        Button singleCheckButton = new Button(this);
        singleCheckButton.setText("Check router connection");
        addSpaced(content, singleCheckButton, 8);

        monitorButton = new Button(this);
        monitorButton.setText("Start 60-second background check");
        addSpaced(content, monitorButton, 8);

        resultView = new TextView(this);
        resultView.setText("No check has run yet.");
        resultView.setTextSize(16);
        addSpaced(content, resultView, 20);

        singleCheckButton.setOnClickListener(view -> runSingleCheck(singleCheckButton));
        monitorButton.setOnClickListener(view -> {
            if (monitorRunning) {
                cancelMonitor = true;
                resultView.setText("Stopping the background check…");
            } else {
                startMonitor();
            }
        });

        setContentView(scrollView);
    }

    private void runSingleCheck(Button button) {
        button.setEnabled(false);
        resultView.setText("Checking the router…");
        worker.execute(() -> {
            String result = requestRouter();
            mainHandler.post(() -> {
                resultView.setText("Single check\n" + result);
                button.setEnabled(true);
            });
        });
    }

    private void startMonitor() {
        monitorRunning = true;
        cancelMonitor = false;
        monitorButton.setText("Stop background check");
        resultView.setText("Background check running. Switch apps or lock the screen, then return here.");

        final ArrayList<String> history = new ArrayList<>();
        worker.execute(() -> {
            int completed = 0;
            int connectionErrors = 0;
            String lastResult = "No request completed yet.";

            for (int attempt = 1; attempt <= MONITOR_REQUESTS && !cancelMonitor; attempt++) {
                lastResult = requestRouter();
                completed = attempt;
                if (!lastResult.startsWith("HTTP ")) {
                    connectionErrors++;
                }
                history.add(String.format(Locale.ROOT, "%02d: %s", attempt, lastResult));
                final int shownCompleted = completed;
                final int shownConnectionErrors = connectionErrors;
                final String shownHistory = String.join("\n", history);
                mainHandler.post(() -> resultView.setText(String.format(
                        Locale.ROOT,
                        "Background check: %d/%d requests\nConnection errors: %d\n%s\nYou can switch apps or lock the screen now.",
                        shownCompleted,
                        MONITOR_REQUESTS,
                        shownConnectionErrors,
                        shownHistory)));

                if (attempt < MONITOR_REQUESTS && !cancelMonitor) {
                    try {
                        Thread.sleep(MONITOR_INTERVAL_MS);
                    } catch (InterruptedException interrupted) {
                        Thread.currentThread().interrupt();
                        cancelMonitor = true;
                    }
                }
            }

            final int finalCompleted = completed;
            final int finalConnectionErrors = connectionErrors;
            final String finalHistory = String.join("\n", history);
            mainHandler.post(() -> {
                monitorRunning = false;
                monitorButton.setText("Start 60-second background check");
                String state = cancelMonitor ? "stopped" : "finished";
                resultView.setText(String.format(
                        Locale.ROOT,
                        "Background check %s: %d/%d requests\nConnection errors: %d\n%s",
                        state,
                        finalCompleted,
                        MONITOR_REQUESTS,
                        finalConnectionErrors,
                        finalHistory));
            });
        });
    }

    private String requestRouter() {
        HttpURLConnection connection = null;
        long startedAt = System.nanoTime();
        try {
            connection = (HttpURLConnection) new URL(ROUTER_URL).openConnection();
            connection.setRequestMethod("GET");
            connection.setConnectTimeout(5_000);
            connection.setReadTimeout(5_000);
            connection.setInstanceFollowRedirects(false);

            int responseCode = connection.getResponseCode();
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000L;
            return String.format(Locale.ROOT, "HTTP %d in %d ms", responseCode, elapsedMs);
        } catch (IOException error) {
            long elapsedMs = (System.nanoTime() - startedAt) / 1_000_000L;
            return String.format(Locale.ROOT, "Connection failed (%s) after %d ms",
                    error.getClass().getSimpleName(), elapsedMs);
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void addSpaced(LinearLayout parent, TextView view, int topMarginDp) {
        LinearLayout.LayoutParams params = fullWidth();
        params.topMargin = dp(topMarginDp);
        parent.addView(view, params);
    }

    private LinearLayout.LayoutParams fullWidth() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onDestroy() {
        cancelMonitor = true;
        worker.shutdownNow();
        super.onDestroy();
    }
}
