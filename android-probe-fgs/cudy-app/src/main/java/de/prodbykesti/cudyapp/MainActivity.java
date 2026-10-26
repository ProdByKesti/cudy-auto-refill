package de.prodbykesti.cudyapp;

import android.app.AlertDialog;
import android.Manifest;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Bundle;
import android.os.Build;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import androidx.fragment.app.FragmentActivity;

import java.util.ArrayList;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import de.prodbykesti.cudysecurity.SensitiveActionGate;

/** Initial Android production UI with action-bound system authentication. */
public class MainActivity extends FragmentActivity {
    static final String PREFS = MainActivity.class.getSimpleName();
    static final String ROUTER_URL = "router_url";
    static final String ROUTER_USERNAME = "router_username";
    private static final String STATE_PENDING_SETTINGS = "cudy.pending_settings";
    private static final String STATE_PENDING_URL = "cudy.pending_url";
    private static final String STATE_PENDING_USERNAME = "cudy.pending_username";
    static final String PREF_DIAGNOSTIC_LOG = "diagnostic_log";
    private static final int MAX_DIAGNOSTIC_LOG_CHARS = 24000;
    private static final int REQUEST_POST_NOTIFICATIONS = 6201;

    private enum SettingsIntent { NONE, CONNECT_AND_SAVE, APPLY_BANDS }

    private final ExecutorService networkExecutor = Executors.newSingleThreadExecutor();
    private final Map<String, CheckBox> bandChecks = new LinkedHashMap<>();
    private final Object diagnosticLogLock = new Object();
    private final StringBuilder diagnosticLog = new StringBuilder();
    private CredentialVault vault;
    private CudyRouterClient cachedRouterClient;
    private SensitiveActionGate gate;
    private TextView statusView;
    private TextView logView;
    private ScrollView logScrollView;
    private Button followLogButton;
    private Button monitoringButton;
    private TextView connectionView;
    private TextView monitoringStatusView;
    private TextView bandSummary;
    private LinearLayout bandChoices;
    private CheckBox automaticBands;
    private EditText routerUrlInput;
    private EditText usernameInput;
    private EditText setupPasswordInput;
    private EditText newPasswordInput;
    private EditText confirmPasswordInput;
    private String pendingSetupPassword;
    private String pendingNewPassword;
    private String pendingRouterUrl;
    private String pendingUsername;
    private SettingsIntent pendingSettingsIntent = SettingsIntent.NONE;
    private CudyRouterClient.BandConfig loadedBands;
    private boolean destroyed;
    private boolean followLog = true;
    private boolean startAfterNotificationPermission;
    private boolean automationReceiverRegistered;
    private final BroadcastReceiver automationLogReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            reloadDiagnosticLog();
            refreshMonitoringControls();
            if (AutomaticRefillService.ACTION_STATE_CHANGED.equals(intent.getAction())) {
                String monitorState = intent.getStringExtra(AutomaticRefillService.EXTRA_MONITOR_STATE);
                if ("starting".equals(monitorState)) {
                    showStatus("Background refill monitoring is starting and connecting to the router.");
                } else if ("running".equals(monitorState)) {
                    showStatus("Background refill monitoring is running and checking the Inbox every 10 seconds.");
                } else if ("stopped".equals(monitorState)) {
                    showStatus("Background refill monitoring stopped.");
                } else if ("error".equals(monitorState)) {
                    showStatus("Background refill monitoring could not start. Check the diagnostics.");
                }
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        vault = new CredentialVault(this);
        String savedLog = getPreferences(MODE_PRIVATE).getString(PREF_DIAGNOSTIC_LOG, "");
        if (savedLog != null) diagnosticLog.append(savedLog);
        appendDiagnosticLog("App session started.");
        if (savedInstanceState == null) {
            try {
                vault.clearPendingPassword();
            } catch (Exception ignored) {
                // A stale temporary password is never used on a fresh app launch.
            }
        } else {
            String savedIntent = savedInstanceState.getString(STATE_PENDING_SETTINGS);
            if (savedIntent != null) {
                try {
                    pendingSettingsIntent = SettingsIntent.valueOf(savedIntent);
                } catch (IllegalArgumentException ignored) {
                    pendingSettingsIntent = SettingsIntent.NONE;
                }
            }
            pendingRouterUrl = savedInstanceState.getString(STATE_PENDING_URL);
            pendingUsername = savedInstanceState.getString(STATE_PENDING_USERNAME);
        }
        gate = new SensitiveActionGate(this, savedInstanceState,
                new SensitiveActionGate.Listener() {
                    @Override
                    public void onStatus(String message) {
                        showStatus(message);
                        String lower = message.toLowerCase(java.util.Locale.ROOT);
                        if (lower.contains("canceled") || lower.contains("cancelled")
                                || lower.contains("unavailable")) {
                            clearPendingSecrets();
                            pendingSettingsIntent = SettingsIntent.NONE;
                            discardPendingSetupPassword();
                        }
                    }

                    @Override
                    public void onAuthorized(SensitiveActionGate.Action action) {
                        if (action == SensitiveActionGate.Action.MANUAL_REFILL) {
                            confirmAndSendRefill();
                        } else if (action == SensitiveActionGate.Action.CHANGE_ROUTER_PASSWORD) {
                            changeRouterPassword();
                        } else if (action == SensitiveActionGate.Action.CHANGE_ROUTER_SETTINGS) {
                            handleAuthorizedSettingsAction();
                        } else if (action == SensitiveActionGate.Action.ENABLE_AUTOMATIC_REFILL) {
                            startAutomaticMonitoring();
                        }
                    }
                });
        setContentView(buildScreen());
        String savedUrl = getPreferences(MODE_PRIVATE).getString(ROUTER_URL, "http://192.168.10.1");
        String savedUsername = getPreferences(MODE_PRIVATE).getString(ROUTER_USERNAME, "admin");
        routerUrlInput.setText(savedUrl);
        usernameInput.setText(savedUsername);
        if (pendingRouterUrl != null) routerUrlInput.setText(pendingRouterUrl);
        if (pendingUsername != null) usernameInput.setText(pendingUsername);
        refreshConnectionLabel();
        refreshMonitoringControls();
        if (AutomaticRefillService.isReady(this)) {
            showStatus("Background refill monitoring is running and checking the Inbox every 10 seconds.");
        } else if (AutomaticRefillService.isEnabled(this)) {
            showStatus("Background refill monitoring is starting and connecting to the router.");
        } else {
            showStatus("Ready. Connect the phone or emulator to the Cudy router's Wi-Fi.");
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (automationReceiverRegistered) return;
        IntentFilter filter = new IntentFilter(AutomaticRefillService.ACTION_LOG_UPDATED);
        filter.addAction(AutomaticRefillService.ACTION_STATE_CHANGED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(automationLogReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(automationLogReceiver, filter);
        }
        automationReceiverRegistered = true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshMonitoringControls();
        reloadDiagnosticLog();
    }

    @Override
    protected void onStop() {
        if (automationReceiverRegistered) {
            unregisterReceiver(automationLogReceiver);
            automationReceiverRegistered = false;
        }
        super.onStop();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_POST_NOTIFICATIONS || !startAfterNotificationPermission) return;
        startAfterNotificationPermission = false;
        if (grantResults.length > 0 && grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED) {
            gate.request(SensitiveActionGate.Action.ENABLE_AUTOMATIC_REFILL);
        } else {
            showStatus("Allow Cudy notifications before enabling background monitoring. The service needs a visible Stop control.");
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        if (gate != null) gate.saveState(outState);
        outState.putString(STATE_PENDING_SETTINGS,
                pendingSettingsIntent == SettingsIntent.NONE ? null : pendingSettingsIntent.name());
        outState.putString(STATE_PENDING_URL, pendingRouterUrl);
        outState.putString(STATE_PENDING_USERNAME, pendingUsername);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, android.content.Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (gate != null) gate.onActivityResult(requestCode, resultCode);
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        clearPendingSecrets();
        networkExecutor.shutdownNow();
        super.onDestroy();
    }

    private View buildScreen() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        LinearLayout diagnosticsPanel = new LinearLayout(this);
        diagnosticsPanel.setOrientation(LinearLayout.VERTICAL);
        diagnosticsPanel.setPadding(dp(12), dp(8), dp(12), dp(8));
        diagnosticsPanel.setBackgroundColor(0xFFEAF1F8);
        statusView = new TextView(this);
        statusView.setTextIsSelectable(true);
        statusView.setTextSize(14);
        statusView.setPadding(dp(2), dp(2), dp(2), dp(6));
        diagnosticsPanel.addView(statusView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout logActions = new LinearLayout(this);
        logActions.setOrientation(LinearLayout.HORIZONTAL);
        followLogButton = button("Follow: On");
        followLogButton.setOnClickListener(view -> {
            followLog = !followLog;
            followLogButton.setText(followLog ? "Follow: On" : "Follow: Off");
            if (followLog && logScrollView != null) {
                logScrollView.post(() -> logScrollView.fullScroll(View.FOCUS_DOWN));
            }
        });
        Button copyDiagnosticsButton = button("Copy diagnostics");
        copyDiagnosticsButton.setOnClickListener(view -> copyDiagnostics());
        logActions.addView(followLogButton, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        logActions.addView(copyDiagnosticsButton, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        diagnosticsPanel.addView(logActions, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        logScrollView = new ScrollView(this);
        logView = new TextView(this);
        logView.setTextIsSelectable(true);
        logView.setTextSize(11);
        logView.setTypeface(Typeface.MONOSPACE);
        logView.setPadding(dp(4), dp(3), dp(4), dp(3));
        logScrollView.addView(logView, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        diagnosticsPanel.addView(logScrollView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(112)));
        root.addView(diagnosticsPanel, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        ScrollView scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        int padding = dp(20);
        page.setPadding(padding, dp(12), padding, padding);
        scroll.addView(page, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        synchronized (diagnosticLogLock) {
            logView.setText(diagnosticLog.toString());
        }
        if (followLog) logScrollView.post(() -> logScrollView.fullScroll(View.FOCUS_DOWN));

        TextView title = new TextView(this);
        title.setText("Cudy Auto Refill");
        title.setTextSize(26);
        title.setTypeface(null, Typeface.BOLD);
        page.addView(title, matchWrap());

        TextView intro = new TextView(this);
        intro.setText("Android app for router setup, protected controls and optional background refill monitoring.");
        intro.setTextSize(15);
        intro.setPadding(0, dp(8), 0, dp(14));
        page.addView(intro, matchWrap());

        section(page, "Router connection");
        routerUrlInput = textField("Router address", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        page.addView(routerUrlInput, matchWrap());
        usernameInput = textField("Router username", InputType.TYPE_CLASS_TEXT);
        page.addView(usernameInput, matchWrap());
        setupPasswordInput = textField("Current router password (leave blank if already saved)",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        page.addView(setupPasswordInput, matchWrap());
        Button connectButton = button("Verify connection and save securely");
        connectButton.setOnClickListener(view -> requestConnectAndSave());
        page.addView(connectButton, matchWrap());
        connectionView = new TextView(this);
        connectionView.setTextSize(14);
        connectionView.setPadding(0, dp(6), 0, dp(4));
        page.addView(connectionView, matchWrap());

        section(page, "Manual refill");
        TextView refillNote = new TextView(this);
        refillNote.setText("A fresh Android system authentication and a second send confirmation are required. The outgoing SMS should appear in the router's Outbox, not its Inbox. Delivery is not yet verified by the app.");
        refillNote.setTextSize(14);
        page.addView(refillNote, matchWrap());
        Button refillButton = button("Send emergency refill");
        refillButton.setOnClickListener(view -> gate.request(SensitiveActionGate.Action.MANUAL_REFILL));
        page.addView(refillButton, matchWrap());

        section(page, "Background monitoring and automatic refill");
        TextView monitoringNote = new TextView(this);
        monitoringNote.setText("When enabled, the app checks the newest Inbox page every 10 seconds and replies only to verified Lidl Connect refill triggers. It verifies the full sender and text, then waits for a new matching Outbox entry. Android keeps a permanent notification with a Stop button. Screen-off monitoring keeps the device awake and uses extra battery. After restarting the phone, start monitoring again. Starting requires Android system authentication; stopping never does.");
        monitoringNote.setTextSize(14);
        page.addView(monitoringNote, matchWrap());
        monitoringStatusView = new TextView(this);
        monitoringStatusView.setTextSize(14);
        monitoringStatusView.setPadding(0, dp(6), 0, dp(4));
        page.addView(monitoringStatusView, matchWrap());
        monitoringButton = button("Start background monitoring");
        monitoringButton.setOnClickListener(view -> toggleAutomaticMonitoring());
        page.addView(monitoringButton, matchWrap());

        section(page, "LTE band selection");
        TextView bandNote = new TextView(this);
        bandNote.setText("Loading choices is read-only. Applying a band requires system authentication, router read-back, and an internet check with rollback if the connection does not return.");
        bandNote.setTextSize(14);
        page.addView(bandNote, matchWrap());
        Button loadBandsButton = button("Load router band choices");
        loadBandsButton.setOnClickListener(view -> loadBandChoices());
        page.addView(loadBandsButton, matchWrap());
        bandSummary = new TextView(this);
        bandSummary.setText("Band choices have not been loaded.");
        bandSummary.setTextSize(14);
        bandSummary.setPadding(0, dp(6), 0, dp(4));
        page.addView(bandSummary, matchWrap());
        automaticBands = new CheckBox(this);
        automaticBands.setText("Use automatic band selection");
        automaticBands.setEnabled(false);
        automaticBands.setOnCheckedChangeListener((button, checked) -> updateBandChoiceMode(checked, true));
        page.addView(automaticBands, matchWrap());
        bandChoices = new LinearLayout(this);
        bandChoices.setOrientation(LinearLayout.VERTICAL);
        page.addView(bandChoices, matchWrap());
        Button applyBandsButton = button("Apply band selection");
        applyBandsButton.setOnClickListener(view -> requestApplyBands());
        page.addView(applyBandsButton, matchWrap());

        section(page, "Change router password");
        TextView passwordNote = new TextView(this);
        passwordNote.setText("The router validates the old password. The new password is saved in Android Keystore-backed encrypted storage only after a fresh login with it succeeds.");
        passwordNote.setTextSize(14);
        page.addView(passwordNote, matchWrap());
        newPasswordInput = textField("New router password (8–64 characters)",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        page.addView(newPasswordInput, matchWrap());
        confirmPasswordInput = textField("Confirm new router password",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        page.addView(confirmPasswordInput, matchWrap());
        Button passwordButton = button("Change router password");
        passwordButton.setOnClickListener(view -> requestPasswordChange());
        page.addView(passwordButton, matchWrap());

        return root;
    }

    private void requestConnectAndSave() {
        String enteredPassword = setupPasswordInput.getText().toString();
        pendingSetupPassword = enteredPassword.isEmpty() ? null : enteredPassword;
        pendingRouterUrl = routerUrlInput.getText().toString().trim();
        pendingUsername = usernameInput.getText().toString().trim();
        if (pendingRouterUrl.isEmpty()) {
            showStatus("Enter the router address first.");
            clearPendingSecrets();
            return;
        }
        if (pendingSetupPassword == null && !vault.hasPassword()) {
            showStatus("Enter the current router password to set up this app.");
            return;
        }
        if (pendingSetupPassword != null) {
            try {
                vault.savePendingPassword(pendingSetupPassword);
            } catch (Exception error) {
                clearPendingSecrets();
                showStatus("The router password could not be protected temporarily. Check secure storage and try again.");
                return;
            }
        }
        setupPasswordInput.setText("");
        pendingSettingsIntent = SettingsIntent.CONNECT_AND_SAVE;
        gate.request(SensitiveActionGate.Action.CHANGE_ROUTER_SETTINGS);
    }

    private void requestPasswordChange() {
        String newPassword = newPasswordInput.getText().toString();
        String confirmation = confirmPasswordInput.getText().toString();
        int enteredLength = newPassword.length();
        if (enteredLength < 8 || enteredLength > 64) {
            showStatus("The new router password must contain 8 to 64 characters. "
                    + "The app counted " + enteredLength + ". The fields are still filled in.");
            return;
        }
        if (!newPassword.equals(confirmation)) {
            showStatus("The two new password entries do not match.");
            return;
        }
        if (!vault.hasPassword()) {
            showStatus("Verify and save the current router password before changing it.");
            return;
        }
        newPasswordInput.setText("");
        confirmPasswordInput.setText("");
        pendingNewPassword = newPassword;
        gate.request(SensitiveActionGate.Action.CHANGE_ROUTER_PASSWORD);
    }

    private void requestApplyBands() {
        if (loadedBands == null) {
            showStatus("Load the router's band choices before applying a selection.");
            return;
        }
        if (!automaticBands.isChecked() && selectedBands().isEmpty()) {
            showStatus("Select at least one LTE band, or choose automatic selection.");
            return;
        }
        pendingSettingsIntent = SettingsIntent.APPLY_BANDS;
        gate.request(SensitiveActionGate.Action.CHANGE_ROUTER_SETTINGS);
    }

    private void handleAuthorizedSettingsAction() {
        SettingsIntent intent = pendingSettingsIntent;
        pendingSettingsIntent = SettingsIntent.NONE;
        if (intent == SettingsIntent.CONNECT_AND_SAVE) {
            connectAndSave();
        } else if (intent == SettingsIntent.APPLY_BANDS) {
            applyBands();
        } else {
            showStatus("No pending router settings action was found. Nothing was changed.");
            clearPendingSecrets();
            discardPendingSetupPassword();
        }
    }

    private void toggleAutomaticMonitoring() {
        if (AutomaticRefillService.isEnabled(this)) {
            stopAutomaticMonitoring();
            return;
        }
        if (!vault.hasPassword()) {
            showStatus("Verify and save the router password before enabling automatic refill.");
            return;
        }
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            startAfterNotificationPermission = true;
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_POST_NOTIFICATIONS);
            return;
        }
        gate.request(SensitiveActionGate.Action.ENABLE_AUTOMATIC_REFILL);
    }

    private void startAutomaticMonitoring() {
        if (!vault.hasPassword()) {
            showStatus("Automatic monitoring was not started because no router password is saved.");
            return;
        }
        Intent service = new Intent(this, AutomaticRefillService.class)
                .setAction(AutomaticRefillService.ACTION_START);
        try {
            getSharedPreferences(AutomaticRefillService.STATE_PREFS, MODE_PRIVATE)
                    .edit().putBoolean(AutomaticRefillService.KEY_ENABLED, true).apply();
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(service);
            else startService(service);
            showStatus("Automatic refill monitoring is starting. Stop it from this screen or its notification.");
            refreshMonitoringControls();
        } catch (RuntimeException error) {
            getSharedPreferences(AutomaticRefillService.STATE_PREFS, MODE_PRIVATE)
                    .edit().putBoolean(AutomaticRefillService.KEY_ENABLED, false).apply();
            showStatus("Android could not start background monitoring: " + error.getClass().getSimpleName() + ".");
            refreshMonitoringControls();
        }
    }

    private void stopAutomaticMonitoring() {
        getSharedPreferences(AutomaticRefillService.STATE_PREFS, MODE_PRIVATE)
                .edit().putBoolean(AutomaticRefillService.KEY_ENABLED, false).apply();
        stopService(new Intent(this, AutomaticRefillService.class));
        showStatus("Background refill monitoring stopped. No authentication is needed to stop it.");
        refreshMonitoringControls();
    }

    private void refreshMonitoringControls() {
        if (monitoringButton == null || monitoringStatusView == null) return;
        boolean enabled = AutomaticRefillService.isEnabled(this);
        boolean ready = AutomaticRefillService.isReady(this);
        monitoringButton.setText(enabled ? "Stop background monitoring" : "Start background monitoring");
        monitoringStatusView.setText(enabled
                ? (ready
                ? "Automatic refill monitoring is active and checking the Inbox every 10 seconds."
                : "Automatic refill monitoring is starting and connecting to the router.")
                : "Automatic refill monitoring is off.");
    }

    private void connectAndSave() {
        String candidatePassword = pendingSetupPassword;
        if (candidatePassword == null) {
            try {
                candidatePassword = vault.loadPendingPassword();
            } catch (Exception error) {
                showStatus("The temporarily protected router password could not be recovered. Enter it again.");
                clearPendingSecrets();
                discardPendingSetupPassword();
                return;
            }
        }
        try {
            vault.clearPendingPassword();
        } catch (Exception error) {
            showStatus("The temporary router password could not be cleared securely. Nothing was sent.");
            clearPendingSecrets();
            return;
        }
        String candidateUrl = pendingRouterUrl;
        String candidateUsername = pendingUsername;
        if (candidateUrl == null) candidateUrl = routerUrlInput.getText().toString().trim();
        if (candidateUsername == null) candidateUsername = usernameInput.getText().toString().trim();
        clearPendingSecrets();
        final String routerUrl = candidateUrl;
        final String routerUsername = candidateUsername;
        final String routerPassword = candidatePassword;
        executeNetwork("Checking the router login…", () -> {
            String password = routerPassword;
            if (password == null) password = vault.loadPassword();
            if (password == null || password.isEmpty()) throw new Exception("No router password is available.");
            CudyRouterClient client = getOrCreateRouterClient(
                    routerUrl, routerUsername, password);
            client.loginAndVerify();
            vault.savePassword(password);
            getPreferences(MODE_PRIVATE).edit()
                    .putString(ROUTER_URL, routerUrl)
                    .putString(ROUTER_USERNAME, routerUsername.isEmpty() ? "admin" : routerUsername)
                    .commit();
            runOnUiThread(this::refreshConnectionLabel);
            return "Router login verified. The password is stored encrypted on this device.";
        });
    }

    private void confirmAndSendRefill() {
        new AlertDialog.Builder(this)
                .setTitle("Send manual refill?")
                .setMessage("This sends the SMS “Refill” to 80808 through the Cudy router.")
                .setNegativeButton("Cancel", (dialog, which) -> showStatus("Manual refill canceled. No SMS was sent."))
                .setPositiveButton("Send SMS", (dialog, which) -> sendRefill())
                .show();
    }

    private void sendRefill() {
        executeWithStoredCredentials("Sending the refill request…", client -> {
            client.loginAndVerify();
            client.sendManualRefill();
            return "The router accepted the Refill SMS request. Check SMS → Outbox; outgoing messages do not appear in Inbox. Delivery is not yet verified by the app.";
        });
    }

    private void loadBandChoices() {
        executeWithStoredCredentials("Reading LTE band choices…", client -> {
            client.loginAndVerify();
            CudyRouterClient.BandConfig config = client.readBandConfig();
            runOnUiThread(() -> showBandChoices(config));
            return "Router band choices loaded. No setting was changed.";
        });
    }

    private void showBandChoices(CudyRouterClient.BandConfig config) {
        loadedBands = config;
        bandChecks.clear();
        bandChoices.removeAllViews();
        automaticBands.setEnabled(true);
        automaticBands.setChecked(config.automatic);
        for (String band : config.available) {
            CheckBox choice = new CheckBox(this);
            choice.setText("Band " + band);
            choice.setChecked(!config.automatic && config.selected.contains(band));
            choice.setEnabled(!config.automatic);
            bandChecks.put(band, choice);
            bandChoices.addView(choice, matchWrap());
        }
        bandSummary.setText(config.automatic
                ? "Current router selection: automatic. Manual bands are inactive. Available bands: " + join(config.available)
                : "Current router selection: Band " + join(config.selected)
                + ". Available bands: " + join(config.available));
    }

    private void updateBandChoiceMode(boolean automatic, boolean clearManualBands) {
        for (CheckBox choice : bandChecks.values()) {
            if (automatic && clearManualBands) choice.setChecked(false);
            choice.setEnabled(!automatic);
        }
    }

    private void applyBands() {
        boolean automatic = automaticBands.isChecked();
        List<String> bands = automatic ? new ArrayList<>() : selectedBands();
        executeWithStoredCredentials("Applying the LTE band choice…", client -> {
            client.loginAndVerify();
            client.applyBandConfig(automatic, bands);
            CudyRouterClient.BandConfig config = client.readBandConfig();
            runOnUiThread(() -> showBandChoices(config));
            return "LTE band choice saved, read back from the router, and internet access confirmed.";
        });
    }

    private void changeRouterPassword() {
        String nextPassword = pendingNewPassword;
        pendingNewPassword = null;
        if (nextPassword == null) {
            showStatus("Password change was not run because the new password is no longer available. Enter it again.");
            return;
        }
        executeWithStoredCredentials("Changing and verifying the router password…", client -> {
            client.loginAndVerify();
            client.changePasswordAndVerify(nextPassword);
            cachedRouterClient = null;
            vault.savePassword(nextPassword);
            return "Router password changed and verified. The new password is encrypted on this device.";
        });
    }

    private interface RouterOperation {
        String run(CudyRouterClient client) throws Exception;
    }

    private void executeWithStoredCredentials(String working, RouterOperation operation) {
        executeNetwork(working, () -> {
            String password = vault.loadPassword();
            if (password == null || password.isEmpty()) {
                throw new Exception("Set up and securely save the router password first.");
            }
            String url = getPreferences(MODE_PRIVATE).getString(ROUTER_URL, "http://192.168.10.1");
            String username = getPreferences(MODE_PRIVATE).getString(ROUTER_USERNAME, "admin");
            return operation.run(getOrCreateRouterClient(url, username, password));
        });
    }

    private CudyRouterClient getOrCreateRouterClient(String url, String username, String password)
            throws java.io.IOException {
        if (cachedRouterClient == null
                || !cachedRouterClient.matchesCredentials(url, username, password)) {
            cachedRouterClient = new CudyRouterClient(
                    url, username, password, this::appendDiagnosticLog);
        }
        return cachedRouterClient;
    }

    private interface NetworkOperation {
        String run() throws Exception;
    }

    private void executeNetwork(String working, NetworkOperation operation) {
        appendDiagnosticLog("ACTION | started: " + working);
        showStatus(working);
        networkExecutor.execute(() -> {
            String outcome;
            try {
                outcome = operation.run();
                appendDiagnosticLog("ACTION | completed: " + outcome);
            } catch (Exception error) {
                outcome = error.getMessage() == null ? "The router action failed." : error.getMessage();
                appendDiagnosticLog("ACTION | failed: " + error.getClass().getSimpleName()
                        + " | " + outcome);
            }
            String finalOutcome = outcome;
            if (!destroyed) runOnUiThread(() -> showStatus(finalOutcome));
        });
    }

    private List<String> selectedBands() {
        List<String> selected = new ArrayList<>();
        for (Map.Entry<String, CheckBox> item : bandChecks.entrySet()) {
            if (item.getValue().isChecked()) selected.add(item.getKey());
        }
        return selected;
    }

    private void refreshConnectionLabel() {
        if (connectionView != null) {
            connectionView.setText(vault.hasPassword()
                    ? "A router password is saved in encrypted device storage."
                    : "Router password not set up yet.");
        }
    }

    private void showStatus(String message) {
        if (!destroyed && statusView != null) {
            String previous = statusView.getText().toString();
            statusView.setText(message);
            if (!message.equals(previous)) appendDiagnosticLog("STATUS | " + message);
        }
    }

    private void appendDiagnosticLog(String message) {
        String snapshot = appendPersistentDiagnosticLog(this, message);
        synchronized (diagnosticLogLock) {
            diagnosticLog.setLength(0);
            diagnosticLog.append(snapshot);
        }
        if (!destroyed && logView != null) {
            runOnUiThread(() -> {
                if (logView == null || destroyed) return;
                logView.setText(snapshot);
                if (followLog && logScrollView != null) {
                    logScrollView.post(() -> logScrollView.fullScroll(View.FOCUS_DOWN));
                }
            });
        }
    }

    private void reloadDiagnosticLog() {
        if (destroyed) return;
        String snapshot = getSharedPreferences(PREFS, MODE_PRIVATE).getString(PREF_DIAGNOSTIC_LOG, "");
        synchronized (diagnosticLogLock) {
            diagnosticLog.setLength(0);
            if (snapshot != null) diagnosticLog.append(snapshot);
        }
        if (logView != null) {
            logView.setText(snapshot == null ? "" : snapshot);
            if (followLog && logScrollView != null) {
                logScrollView.post(() -> logScrollView.fullScroll(View.FOCUS_DOWN));
            }
        }
    }

    static String appendPersistentDiagnosticLog(Context context, String message) {
        synchronized (MainActivity.class) {
            SharedPreferences preferences = context.getSharedPreferences(PREFS, MODE_PRIVATE);
            StringBuilder current = new StringBuilder(preferences.getString(PREF_DIAGNOSTIC_LOG, ""));
            String timestamp = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.ROOT).format(new Date());
            current.append(timestamp).append(" | ").append(message).append('\n');
            if (current.length() > MAX_DIAGNOSTIC_LOG_CHARS) {
                int removeUntil = current.length() - MAX_DIAGNOSTIC_LOG_CHARS;
                int nextLine = current.indexOf("\n", removeUntil);
                current.delete(0, nextLine >= 0 ? nextLine + 1 : removeUntil);
            }
            String snapshot = current.toString();
            preferences.edit().putString(PREF_DIAGNOSTIC_LOG, snapshot).apply();
            return snapshot;
        }
    }

    private void copyDiagnostics() {
        String report = buildDiagnosticsReport();
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        if (clipboard == null) {
            showStatus("Could not access the clipboard. Select and copy the visible log instead.");
            return;
        }
        clipboard.setPrimaryClip(ClipData.newPlainText("Cudy Auto Refill diagnostics", report));
        showStatus("Log and diagnostics copied. Passwords, tokens, cookies, and SMS contents are excluded.");
    }

    private String buildDiagnosticsReport() {
        String appVersion = "unknown";
        try {
            appVersion = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception ignored) {
            // Keep diagnostics copy available even when package metadata cannot be read.
        }
        String routerHost = "not set";
        try {
            String address = routerUrlInput == null
                    ? getPreferences(MODE_PRIVATE).getString(ROUTER_URL, "http://192.168.10.1")
                    : routerUrlInput.getText().toString().trim();
            java.net.URL parsed = new java.net.URL(address);
            routerHost = parsed.getHost() + (parsed.getPort() == -1 ? "" : ":" + parsed.getPort());
        } catch (Exception ignored) {
            // Do not include an invalid free-form URL in the copied report.
        }

        StringBuilder report = new StringBuilder();
        report.append("Cudy Auto Refill Android diagnostics\n")
                .append("App version: ").append(appVersion).append('\n')
                .append("Android: ").append(android.os.Build.VERSION.RELEASE)
                .append(" (API ").append(android.os.Build.VERSION.SDK_INT).append(")\n")
                .append("Device: ").append(android.os.Build.MANUFACTURER).append(' ')
                .append(android.os.Build.MODEL).append('\n')
                .append("Active network: ").append(activeNetworkSummary()).append('\n')
                .append("Router host: ").append(routerHost).append('\n')
                .append("Sensitive values are not included: passwords, tokens, cookies, or SMS contents.\n\n")
                .append("Recent app log:\n");
        synchronized (diagnosticLogLock) {
            report.append(diagnosticLog);
        }
        return report.toString();
    }

    private String activeNetworkSummary() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (manager == null) return "connectivity service unavailable";
        Network active = manager.getActiveNetwork();
        NetworkCapabilities capabilities = active == null ? null : manager.getNetworkCapabilities(active);
        if (capabilities == null) return "no active network capabilities";
        List<String> transports = new ArrayList<>();
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) transports.add("Wi-Fi");
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) transports.add("cellular");
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) transports.add("Ethernet");
        if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) transports.add("VPN");
        if (transports.isEmpty()) transports.add("other");
        return android.text.TextUtils.join(", ", transports)
                + "; internet=" + capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                + "; validated=" + capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
    }

    private void clearPendingSecrets() {
        pendingSetupPassword = null;
        pendingNewPassword = null;
        pendingRouterUrl = null;
        pendingUsername = null;
    }

    private void discardPendingSetupPassword() {
        if (vault == null) return;
        try {
            vault.clearPendingPassword();
        } catch (Exception ignored) {
            // The next fresh app launch also clears any leftover temporary value.
        }
    }

    private EditText textField(String hint, int inputType) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setSingleLine(true);
        field.setInputType(inputType);
        return field;
    }

    private Button button(String title) {
        Button button = new Button(this);
        button.setText(title);
        button.setAllCaps(false);
        return button;
    }

    private void section(LinearLayout page, String title) {
        TextView heading = new TextView(this);
        heading.setText(title);
        heading.setTextSize(19);
        heading.setTypeface(null, Typeface.BOLD);
        heading.setPadding(0, dp(18), 0, dp(6));
        page.addView(heading, matchWrap());
    }

    private String join(List<String> values) {
        return values.isEmpty() ? "none" : android.text.TextUtils.join(", ", values);
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
