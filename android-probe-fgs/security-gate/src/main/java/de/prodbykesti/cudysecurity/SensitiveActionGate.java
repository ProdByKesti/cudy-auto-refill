package de.prodbykesti.cudysecurity;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;

import androidx.biometric.BiometricManager;
import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.FragmentActivity;

/**
 * Requests a fresh Android system-authentication grant for one named action.
 * It never keeps a general unlocked flag; success is delivered only to the
 * callback for the action that opened the prompt.
 */
public final class SensitiveActionGate {
    private static final int DEVICE_CREDENTIAL_REQUEST = 4102;
    private static final String STATE_PENDING_ACTION = "sensitive_action.pending";
    private static final int DEVICE_AUTHENTICATORS = BiometricManager.Authenticators.BIOMETRIC_STRONG
            | BiometricManager.Authenticators.DEVICE_CREDENTIAL;

    public enum Action {
        MANUAL_REFILL("Manual refill"),
        CHANGE_ROUTER_PASSWORD("Change router password"),
        CHANGE_ROUTER_SETTINGS("Change router settings"),
        ENABLE_AUTOMATIC_REFILL("Enable automatic refill monitoring");

        private final String label;

        Action(String label) {
            this.label = label;
        }

        public String getLabel() {
            return label;
        }
    }

    public interface Listener {
        void onStatus(String message);

        void onAuthorized(Action action);
    }

    private final FragmentActivity activity;
    private final Listener listener;
    private final BiometricPrompt biometricPrompt;
    private Action pendingAction;

    public SensitiveActionGate(FragmentActivity activity, Bundle savedInstanceState, Listener listener) {
        this.activity = activity;
        this.listener = listener;
        if (savedInstanceState != null) {
            String savedAction = savedInstanceState.getString(STATE_PENDING_ACTION);
            if (savedAction != null) {
                try {
                    pendingAction = Action.valueOf(savedAction);
                } catch (IllegalArgumentException ignored) {
                    pendingAction = null;
                }
            }
        }
        biometricPrompt = new BiometricPrompt(
                activity,
                ContextCompat.getMainExecutor(activity),
                new BiometricPrompt.AuthenticationCallback() {
                    @Override
                    public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                        super.onAuthenticationSucceeded(result);
                        Action action = pendingAction;
                        pendingAction = null;
                        if (action != null) {
                            listener.onAuthorized(action);
                        }
                    }

                    @Override
                    public void onAuthenticationError(int errorCode, CharSequence errorString) {
                        super.onAuthenticationError(errorCode, errorString);
                        Action action = pendingAction;
                        if (errorCode == BiometricPrompt.ERROR_NEGATIVE_BUTTON && action != null) {
                            KeyguardManager keyguard = activity.getSystemService(KeyguardManager.class);
                            if (keyguard != null && keyguard.isDeviceSecure()) {
                                launchDeviceCredential(action, keyguard);
                                return;
                            }
                        }
                        pendingAction = null;
                        listener.onStatus("Authentication canceled or unavailable. No action was authorized.");
                    }
                });
    }

    public void request(Action action) {
        pendingAction = action;
        KeyguardManager keyguard = activity.getSystemService(KeyguardManager.class);
        boolean hasDeviceCredential = keyguard != null && keyguard.isDeviceSecure();
        BiometricManager biometricManager = BiometricManager.from(activity);
        int availability;

        if (Build.VERSION.SDK_INT >= 30) {
            availability = biometricManager.canAuthenticate(DEVICE_AUTHENTICATORS);
            if (availability == BiometricManager.BIOMETRIC_SUCCESS) {
                showPrompt(action, hasDeviceCredential, true);
                return;
            }
        } else {
            availability = biometricManager.canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG);
            if (availability == BiometricManager.BIOMETRIC_SUCCESS) {
                showPrompt(action, hasDeviceCredential, false);
                return;
            }
        }

        if (hasDeviceCredential) {
            launchDeviceCredential(action, keyguard);
        } else {
            pendingAction = null;
            listener.onStatus("Protected action unavailable. Set a screen lock or enroll a strong biometric first.");
        }
    }

    public void saveState(Bundle outState) {
        outState.putString(STATE_PENDING_ACTION, pendingAction == null ? null : pendingAction.name());
    }

    public boolean onActivityResult(int requestCode, int resultCode) {
        if (requestCode != DEVICE_CREDENTIAL_REQUEST) {
            return false;
        }
        Action action = pendingAction;
        pendingAction = null;
        if (action == null) {
            return true;
        }
        if (resultCode == Activity.RESULT_OK) {
            listener.onAuthorized(action);
        } else {
            listener.onStatus("Authentication canceled. No action was authorized.");
        }
        return true;
    }

    private void showPrompt(Action action, boolean hasDeviceCredential,
                            boolean allowDeviceCredentialInPrompt) {
        BiometricPrompt.PromptInfo.Builder promptBuilder = new BiometricPrompt.PromptInfo.Builder()
                .setTitle("Confirm protected action")
                .setSubtitle("Authenticate to continue: " + action.getLabel());
        if (allowDeviceCredentialInPrompt) {
            promptBuilder.setAllowedAuthenticators(DEVICE_AUTHENTICATORS);
        } else {
            promptBuilder.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                    .setNegativeButtonText(hasDeviceCredential ? "Use screen lock" : "Cancel");
        }
        listener.onStatus("Waiting for Android system authentication…");
        biometricPrompt.authenticate(promptBuilder.build());
    }

    @SuppressWarnings("deprecation")
    private void launchDeviceCredential(Action action, KeyguardManager keyguard) {
        if (!keyguard.isDeviceSecure()) {
            pendingAction = null;
            listener.onStatus("Device screen lock is not configured. No action was authorized.");
            return;
        }
        Intent confirmation = keyguard.createConfirmDeviceCredentialIntent(
                "Confirm protected action", "Authenticate to continue: " + action.getLabel());
        if (confirmation == null) {
            pendingAction = null;
            listener.onStatus("Android could not open device authentication. No action was authorized.");
            return;
        }
        pendingAction = action;
        listener.onStatus("Waiting for Android device credential…");
        activity.startActivityForResult(confirmation, DEVICE_CREDENTIAL_REQUEST);
    }
}
