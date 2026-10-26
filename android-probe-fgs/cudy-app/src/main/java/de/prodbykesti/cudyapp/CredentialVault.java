package de.prodbykesti.cudyapp;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Encrypts the router password with an Android Keystore AES-GCM key. */
final class CredentialVault {
    private static final String KEY_ALIAS = "cudy-router-password-v1";
    private static final String PREFS = "cudy_secure_credentials";
    private static final String IV_KEY = "password_iv";
    private static final String DATA_KEY = "password_ciphertext";
    private static final String PENDING_IV_KEY = "pending_password_iv";
    private static final String PENDING_DATA_KEY = "pending_password_ciphertext";
    private final SharedPreferences preferences;

    CredentialVault(Context context) {
        preferences = context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    synchronized boolean hasPassword() {
        return preferences.contains(IV_KEY) && preferences.contains(DATA_KEY);
    }

    synchronized void savePassword(String password) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        byte[] iv = cipher.getIV();
        byte[] ciphertext = cipher.doFinal(password.getBytes(StandardCharsets.UTF_8));
        boolean saved = preferences.edit()
                .putString(IV_KEY, Base64.encodeToString(iv, Base64.NO_WRAP))
                .putString(DATA_KEY, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .commit();
        Arrays.fill(iv, (byte) 0);
        Arrays.fill(ciphertext, (byte) 0);
        if (!saved) throw new java.io.IOException("Android could not save the encrypted router password.");
    }

    synchronized String loadPassword() throws Exception {
        return decrypt(IV_KEY, DATA_KEY);
    }

    /** Temporarily protects a first-connection password while Android shows its auth prompt. */
    synchronized void savePendingPassword(String password) throws Exception {
        encrypt(PENDING_IV_KEY, PENDING_DATA_KEY, password);
    }

    synchronized String loadPendingPassword() throws Exception {
        return decrypt(PENDING_IV_KEY, PENDING_DATA_KEY);
    }

    synchronized void clearPendingPassword() throws java.io.IOException {
        boolean cleared = preferences.edit()
                .remove(PENDING_IV_KEY)
                .remove(PENDING_DATA_KEY)
                .commit();
        if (!cleared) throw new java.io.IOException("Android could not clear the temporary router password.");
    }

    private void encrypt(String ivKey, String dataKey, String password) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        byte[] iv = cipher.getIV();
        byte[] ciphertext = cipher.doFinal(password.getBytes(StandardCharsets.UTF_8));
        boolean saved = preferences.edit()
                .putString(ivKey, Base64.encodeToString(iv, Base64.NO_WRAP))
                .putString(dataKey, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .commit();
        Arrays.fill(iv, (byte) 0);
        Arrays.fill(ciphertext, (byte) 0);
        if (!saved) throw new java.io.IOException("Android could not save the encrypted router password.");
    }

    private String decrypt(String ivKey, String dataKey) throws Exception {
        String encodedIv = preferences.getString(ivKey, null);
        String encodedData = preferences.getString(dataKey, null);
        if (encodedIv == null || encodedData == null) return null;
        byte[] iv = Base64.decode(encodedIv, Base64.NO_WRAP);
        byte[] ciphertext = Base64.decode(encodedData, Base64.NO_WRAP);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), new GCMParameterSpec(128, iv));
        byte[] plaintext = cipher.doFinal(ciphertext);
        try {
            return new String(plaintext, StandardCharsets.UTF_8);
        } finally {
            Arrays.fill(plaintext, (byte) 0);
            Arrays.fill(ciphertext, (byte) 0);
            Arrays.fill(iv, (byte) 0);
        }
    }

    private SecretKey getOrCreateKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        keyStore.load(null);
        java.security.Key existing = keyStore.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) return (SecretKey) existing;

        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        KeyGenParameterSpec spec = new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setUserAuthenticationRequired(false)
                .build();
        generator.init(spec);
        return generator.generateKey();
    }
}
