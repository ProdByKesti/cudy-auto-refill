package de.prodbykesti.cudyrouterprobe;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

public class MainActivity extends Activity {
    private static final int NOTIFICATION_PERMISSION_REQUEST = 4101;
    private TextView statusView;
    private TextView resultsView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int padding = dp(18);
        page.setPadding(padding, padding, padding, padding);

        TextView heading = new TextView(this);
        heading.setText("Cudy background connection probe");
        heading.setTextSize(22);
        heading.setTypeface(null, Typeface.BOLD);
        page.addView(heading, matchWrap());

        TextView explanation = new TextView(this);
        explanation.setText("The test only sends read-only HTTP requests to the router login page. It does not log in, change settings, or send SMS. The background test runs for about one minute and shows an ongoing notification.");
        explanation.setTextSize(15);
        explanation.setPadding(0, dp(10), 0, dp(14));
        page.addView(explanation, matchWrap());

        statusView = new TextView(this);
        statusView.setTextSize(15);
        statusView.setPadding(0, 0, 0, dp(12));
        page.addView(statusView, matchWrap());

        Button oneShotButton = new Button(this);
        oneShotButton.setText("Check router once");
        oneShotButton.setOnClickListener(view -> checkRouterOnce());
        page.addView(oneShotButton, matchWrap());

        Button startButton = new Button(this);
        startButton.setText("Start 12 background checks");
        startButton.setOnClickListener(view -> requestNotificationPermissionThenStart());
        page.addView(startButton, matchWrap());

        Button stopButton = new Button(this);
        stopButton.setText("Stop background checks");
        stopButton.setOnClickListener(view -> sendServiceAction(RouterProbeService.ACTION_STOP));
        page.addView(stopButton, matchWrap());

        TextView resultsHeading = new TextView(this);
        resultsHeading.setText("Results");
        resultsHeading.setTextSize(18);
        resultsHeading.setTypeface(null, Typeface.BOLD);
        resultsHeading.setPadding(0, dp(14), 0, dp(6));
        page.addView(resultsHeading, matchWrap());

        resultsView = new TextView(this);
        resultsView.setTextSize(14);
        resultsView.setTextIsSelectable(true);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(resultsView, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        page.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        setContentView(page);
        refreshResults();
        statusView.setText("Ready. Connect the emulator to the same Wi-Fi network as the Cudy router.");
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (resultsView != null) {
            refreshResults();
        }
    }

    private void requestNotificationPermissionThenStart() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQUEST);
            return;
        }
        startBackgroundChecks();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTIFICATION_PERMISSION_REQUEST) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startBackgroundChecks();
            } else {
                statusView.setText("Notification permission is needed so the foreground test is visible while you switch apps.");
            }
        }
    }

    private void startBackgroundChecks() {
        statusView.setText("Starting the visible background test…");
        sendServiceAction(RouterProbeService.ACTION_START);
    }

    private void sendServiceAction(String action) {
        Intent intent = new Intent(this, RouterProbeService.class);
        intent.setAction(action);
        if (Build.VERSION.SDK_INT >= 26 && RouterProbeService.ACTION_START.equals(action)) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    private void checkRouterOnce() {
        statusView.setText("Checking http://192.168.10.1/ …");
        Thread thread = new Thread(() -> {
            String result = RouterProbeService.makeRouterRequest();
            runOnUiThread(() -> statusView.setText("One-shot result: " + result));
        }, "router-one-shot-check");
        thread.start();
    }

    private void refreshResults() {
        String results = getSharedPreferences(RouterProbeService.PREFS_NAME, MODE_PRIVATE)
                .getString(RouterProbeService.PREF_RESULTS, "No background test results yet.");
        resultsView.setText(results);
        String status = getSharedPreferences(RouterProbeService.PREFS_NAME, MODE_PRIVATE)
                .getString(RouterProbeService.PREF_STATUS, "Ready.");
        statusView.setText(status);
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
