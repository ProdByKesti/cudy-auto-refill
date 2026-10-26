package de.prodbykesti.cudyapp;

import android.text.TextUtils;
import android.text.Html;
import android.os.Build;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.CookieManager;
import java.net.CookiePolicy;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Minimal HTTP client for the Cudy LuCI flows verified by the desktop app. */
final class CudyRouterClient {
    private static final int CONNECT_TIMEOUT_MS = 12_000;
    private static final int READ_TIMEOUT_MS = 18_000;
    private static final long SESSION_REUSE_WINDOW_MS = 5 * 60_000L;
    private static final String SMS_PAGE = "/cgi-bin/luci/admin/network/gcom/sms?iface=4g";
    private static final String SMS_LIST = "/cgi-bin/luci/admin/network/gcom/sms/smslist";
    private static final String SMS_NEW = "/cgi-bin/luci/admin/network/gcom/sms/smsnew?nomodal=&iface=4g";
    private static final String BAND_PAGE = "/cgi-bin/luci/admin/network/gcom/config/apn?embedded=&iface=4g";
    private static final String PASSWORD_PAGE = "/cgi-bin/luci/admin/panel";
    private static final String PASSWORD_POST = "/cgi-bin/luci/admin/system/password";

    interface EventLogger {
        void log(String message);
    }

    private final String baseUrl;
    private final String username;
    private final String password;
    private final EventLogger eventLogger;
    private final CookieManager cookies = new CookieManager(null, CookiePolicy.ACCEPT_ALL);
    private long sessionVerifiedAtMs;

    static final class BandConfig {
        final boolean automatic;
        final List<String> available;
        final List<String> selected;

        BandConfig(boolean automatic, List<String> available, List<String> selected) {
            this.automatic = automatic;
            this.available = available;
            this.selected = selected;
        }
    }

    static final class SmsMessage {
        final String id;
        final String sender;
        final String content;
        final String timestamp;
        final String cfg;

        SmsMessage(String id, String sender, String content, String timestamp, String cfg) {
            this.id = id;
            this.sender = sender;
            this.content = content;
            this.timestamp = timestamp;
            this.cfg = cfg;
        }
    }

    static final class VerifiedTrigger {
        private final HttpResult details;
        private final HtmlForms.Form form;
        private final String sender;

        VerifiedTrigger(HttpResult details, HtmlForms.Form form, String sender) {
            this.details = details;
            this.form = form;
            this.sender = sender;
        }
    }

    private static final class HttpResult {
        final int status;
        final String body;
        final String url;

        HttpResult(int status, String body, String url) {
            this.status = status;
            this.body = body;
            this.url = url;
        }
    }

    CudyRouterClient(String routerUrl, String username, String password) throws IOException {
        this(routerUrl, username, password, null);
    }

    CudyRouterClient(String routerUrl, String username, String password, EventLogger eventLogger) throws IOException {
        URL parsed = new URL(routerUrl.trim());
        if (!("http".equalsIgnoreCase(parsed.getProtocol()) || "https".equalsIgnoreCase(parsed.getProtocol()))
                || parsed.getHost().isEmpty() || parsed.getUserInfo() != null
                || (parsed.getPath() != null && !parsed.getPath().isEmpty() && !"/".equals(parsed.getPath()))
                || parsed.getQuery() != null || parsed.getRef() != null) {
            throw new IOException("Enter a valid router URL such as http://192.168.10.1.");
        }
        String normalized = routerUrl.trim();
        while (normalized.endsWith("/")) normalized = normalized.substring(0, normalized.length() - 1);
        this.baseUrl = normalized;
        this.username = username == null || username.trim().isEmpty() ? "admin" : username.trim();
        this.password = password == null ? "" : password;
        this.eventLogger = eventLogger;
    }

    boolean matchesCredentials(String routerUrl, String routerUsername, String routerPassword) {
        if (routerUrl == null) return false;
        String normalizedUrl = routerUrl.trim();
        while (normalizedUrl.endsWith("/")) {
            normalizedUrl = normalizedUrl.substring(0, normalizedUrl.length() - 1);
        }
        String normalizedUsername = routerUsername == null || routerUsername.trim().isEmpty()
                ? "admin" : routerUsername.trim();
        return baseUrl.equals(normalizedUrl)
                && username.equals(normalizedUsername)
                && password.equals(routerPassword == null ? "" : routerPassword);
    }

    void loginAndVerify() throws IOException {
        long now = System.currentTimeMillis();
        if (sessionVerifiedAtMs > 0 && now - sessionVerifiedAtMs < SESSION_REUSE_WINDOW_MS) {
            HttpResult cachedSession = request("GET", resolve(SMS_PAGE), null, null, false);
            if (cachedSession.status == 200 && !hasLoginField(cachedSession.body)) {
                sessionVerifiedAtMs = System.currentTimeMillis();
                log("LOGIN | existing router session verified and reused.");
                return;
            }
            sessionVerifiedAtMs = 0;
            log("LOGIN | saved router session is no longer valid; signing in again.");
        }

        HttpResult loginPage = request("GET", resolve("/cgi-bin/luci/"), null, null, false);
        String csrf = HtmlForms.inputValue(loginPage.body, "_csrf");
        String salt = HtmlForms.inputValue(loginPage.body, "salt");
        String formUsername = HtmlForms.inputValue(loginPage.body, "luci_username");
        boolean hasPasswordField = hasLoginField(loginPage.body);
        log("LOGIN | initial response fields: HTTP=" + loginPage.status
                + ", csrf=" + present(csrf) + ", salt=" + present(salt)
                + ", username=" + present(formUsername)
                + ", passwordField=" + hasPasswordField);
        if (loginPage.status != 200) {
            // The desktop client parses the initial response without requiring HTTP 200.
            // Some Cudy firmware returns 403 together with its usable login form.
            if (loginPage.status == 403 && !TextUtils.isEmpty(salt) && hasPasswordField) {
                log("LOGIN | continuing: HTTP 403 response contains the expected login form.");
            } else {
                throw new IOException("The router login page returned HTTP " + loginPage.status
                        + " and did not provide a usable login form.");
            }
        }
        if (TextUtils.isEmpty(formUsername)) formUsername = username;
        if (TextUtils.isEmpty(salt) || !hasPasswordField) {
            throw new IOException("The router login page did not provide the expected security fields.");
        }

        HttpResult tokenResult = request("POST", resolve("/cgi-bin/luci/admin/get_token"),
                new ArrayList<>(), null, false);
        requireStatus(tokenResult, 200, "The router did not issue a login token.");
        String token = tokenResult.body.trim();
        if (token.isEmpty()) throw new IOException("The router returned an empty login token.");

        String passwordHash = sha256Hex(sha256Hex(password + salt) + token);
        List<HtmlForms.Field> loginFields = new ArrayList<>();
        loginFields.add(new HtmlForms.Field("_csrf", csrf));
        loginFields.add(new HtmlForms.Field("token", token));
        loginFields.add(new HtmlForms.Field("salt", salt));
        loginFields.add(new HtmlForms.Field("zonename", "Europe/Berlin"));
        loginFields.add(new HtmlForms.Field("timeclock", String.valueOf(System.currentTimeMillis() / 1000L)));
        loginFields.add(new HtmlForms.Field("luci_language", "auto"));
        loginFields.add(new HtmlForms.Field("luci_username", formUsername));
        loginFields.add(new HtmlForms.Field("luci_password", passwordHash));
        HttpResult submit = request("POST", resolve("/cgi-bin/luci/"), loginFields, null, false);
        if (submit.status != 200 && submit.status != 302 && submit.status != 303) {
            throw new IOException("The router rejected the login (HTTP " + submit.status + ").");
        }

        HttpResult protectedPage = request("GET", resolve(SMS_PAGE), null, null, false);
        if (protectedPage.status != 200 || hasLoginField(protectedPage.body)) {
            throw new IOException("The router login could not be confirmed. Check the username and password.");
        }
        sessionVerifiedAtMs = System.currentTimeMillis();
        log("LOGIN | protected router page accepted.");
    }

    void sendManualRefill() throws IOException {
        HttpResult page = request("GET", resolve(SMS_NEW), null, null, false);
        if (page.status != 200 || hasLoginField(page.body)) {
            throw new IOException("The router session expired before the SMS form opened.");
        }
        HtmlForms.Form form = HtmlForms.find(page.body,
                "cbid.smsnew.1.phone", "cbid.smsnew.1.content");
        if (form == null || !"post".equalsIgnoreCase(form.method)) {
            throw new IOException("The router's new-message form did not match the expected layout.");
        }
        form.set("cbid.smsnew.1.phone", "80808");
        form.set("cbid.smsnew.1.content", "Refill");
        if (form.hasField("cbi.submit")) form.set("cbi.submit", "1");
        if (form.hasField("timeclock")) {
            form.set("timeclock", String.valueOf(System.currentTimeMillis() / 1000L));
        }
        boolean hasSendButton = false;
        for (HtmlForms.Control control : form.controls) {
            if ("cbid.smsnew.1.send".equals(control.name)) {
                form.set(control.name, TextUtils.isEmpty(control.value) ? "Send" : control.value);
                hasSendButton = true;
                break;
            }
        }
        if (!hasSendButton) {
            throw new IOException("The router's SMS form has no recognized send button.");
        }
        HttpResult result = request("POST", sameOriginAction(page.url, form.action), form.fields,
                page.url, false);
        if (result.status == 403 || hasLoginField(result.body)) {
            throw new IOException("The router session expired while sending the SMS.");
        }
        if (result.status != 200 && result.status != 302 && result.status != 303) {
            throw new IOException("The router did not accept the SMS request (HTTP " + result.status + ").");
        }
    }

    /** Reads only the newest Inbox page so frequent monitoring does not churn router pages. */
    List<SmsMessage> readInboxFirstPage() throws IOException {
        return readSmsFirstPage("rec");
    }

    /** Reads only the newest Outbox page to verify a newly submitted reply. */
    List<SmsMessage> readOutboxFirstPage() throws IOException {
        return readSmsFirstPage("sto");
    }

    VerifiedTrigger prepareTriggerReply(String cfg) throws IOException {
        if (TextUtils.isEmpty(cfg) || !cfg.matches("[A-Za-z0-9._-]{1,160}")) {
            throw new IOException("The trigger SMS did not contain a safe router message reference.");
        }
        String detailsAddress = resolve(SMS_LIST + "/readsms?iface=4g&cfg=" + cfg + "&smsbox=rec");
        HttpResult details = request("GET", detailsAddress, null, resolve(SMS_LIST + "?smsbox=rec&iface=4g"), false);
        if (details.status != 200 || hasLoginField(details.body)) {
            throw new IOException("The router Inbox detail page could not be opened for this trigger SMS.");
        }
        HtmlForms.Form form = HtmlForms.find(details.body,
                "cbid.smsread.1.phone", "cbid.smsread.1.content");
        if (form == null || !"post".equalsIgnoreCase(form.method)) {
            throw new IOException("The router trigger-SMS reply form was not found.");
        }
        String sender = formValue(form, "cbid.smsread.1.phone").trim();
        String content = formValue(form, "cbid.smsread.1.content");
        if (!isLidlSender(sender)) {
            throw new IOException("Automatic refill stopped: the full SMS sender was not confirmed as Lidl Connect 80808.");
        }
        if (!isRefillTrigger(content)) {
            throw new IOException("The full SMS text did not match a configured refill trigger; no reply was sent.");
        }
        return new VerifiedTrigger(details, form, sender);
    }

    void sendTriggerReply(VerifiedTrigger trigger) throws IOException {
        trigger.form.set("cbid.smsread.1.phone", trigger.sender);
        trigger.form.set("cbid.smsread.1.content", "Refill");
        trigger.form.set("cbid.smsread.1.reply", "Reply");
        trigger.form.set("cbi.submit", "1");
        trigger.form.set("timeclock", String.valueOf(System.currentTimeMillis() / 1000L));
        HttpResult submitted = request("POST", sameOriginAction(trigger.details.url, trigger.form.action),
                trigger.form.fields, trigger.details.url, false);
        if (submitted.status == 403 || hasLoginField(submitted.body)) {
            throw new IOException("The router session expired while sending the trigger reply.");
        }
        if (submitted.status != 200 && submitted.status != 302 && submitted.status != 303) {
            throw new IOException("The router did not accept the trigger reply (HTTP " + submitted.status + ").");
        }
        log("REFILL | trigger reply accepted by the router; waiting for a new Outbox entry.");
    }

    static boolean isRefillTrigger(String content) {
        String normalized = normalizeSmsText(content);
        String[] triggers = {
                "fast verbraucht",
                "antworte einfach auf diese sms mit refill",
                "option verbraucht",
                "option ist jetzt verbraucht",
                "option ist verbraucht",
                "option wurde verbraucht",
                "option aufgebraucht"
        };
        for (String trigger : triggers) {
            if (normalized.contains(trigger)) return true;
        }
        return normalized.contains("unlimited") && normalized.contains("option")
                && Pattern.compile("\\bverbrauch\\p{L}*\\b").matcher(normalized).find();
    }

    static boolean isLidlSender(String sender) {
        String normalized = normalizeSmsText(sender);
        if (normalized.isEmpty()) return false;
        String digits = normalized.replaceAll("\\D", "");
        return "80808".equals(digits) || "lidl connect".equals(normalized)
                || Pattern.compile("(?<!\\d)80808(?!\\d)").matcher(normalized).find();
    }

    static boolean isRefillOutboxText(String content) {
        String normalized = normalizeSmsText(content);
        String[] tokens = normalized.split("[^\\p{L}\\p{N}]+");
        if (tokens.length == 0 || tokens[0].isEmpty()) return false;
        for (String token : tokens) if (!"refill".equals(token)) return false;
        return true;
    }

    private List<SmsMessage> readSmsFirstPage(String box) throws IOException {
        String pageAddress = resolve(SMS_LIST + "?smsbox=" + box + "&iface=4g&pagestart=1&pagecurrent=1");
        HttpResult page = request("GET", pageAddress, null,
                resolve(SMS_LIST + "?smsbox=" + box + "&iface=4g"), false);
        if (page.status != 200 || hasLoginField(page.body)) {
            throw new IOException("The router " + ("rec".equals(box) ? "Inbox" : "Outbox")
                    + " could not be read (HTTP " + page.status + ").");
        }
        return parseSmsRows(page.body);
    }

    private static List<SmsMessage> parseSmsRows(String html) throws IOException {
        List<SmsMessage> messages = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        Matcher rows = Pattern.compile("(?is)<tr\\b([^>]*)>(.*?)</tr\\s*>").matcher(html == null ? "" : html);
        int rowNumber = 0;
        while (rows.find()) {
            rowNumber++;
            String rowAttrs = rows.group(1);
            String row = rows.group(2);
            String content = findSmsField(row, "content", "message", "text");
            if (content.isEmpty()) continue;
            String sender = findSmsField(row, "phone", "sender", "number");
            String timestamp = findSmsField(row, "timestamp", "time", "date");
            String cfg = extractSmsCfg(row);
            if (timestamp.isEmpty()) {
                timestamp = firstNonEmpty(attributeValue(rowAttrs, "data-timestamp"),
                        attributeValue(rowAttrs, "data-id"), attributeValue(rowAttrs, "id"), cfg,
                        "row-" + rowNumber + "-" + content.hashCode());
            }
            String id = sha256Hex(sender + "|" + content + "|" + timestamp);
            if (seen.add(id)) messages.add(new SmsMessage(id, sender, content, timestamp, cfg));
        }
        return messages;
    }

    private static String findSmsField(String row, String... suffixes) {
        Matcher tags = Pattern.compile("(?is)<([a-z][a-z0-9:-]*)\\b([^>]*)>").matcher(row);
        String best = "";
        while (tags.find()) {
            String tag = tags.group(1);
            String attrs = tags.group(2);
            String id = attributeValue(attrs, "id");
            if (id.isEmpty() || !hasSuffix(id, suffixes)) continue;
            String candidate;
            if ("input".equalsIgnoreCase(tag)) {
                candidate = decodeHtml(attributeValue(attrs, "value"));
            } else {
                Matcher close = Pattern.compile("(?is)</" + Pattern.quote(tag) + "\\s*>").matcher(row);
                close.region(tags.end(), row.length());
                String inner = close.find() ? row.substring(tags.end(), close.start()) : "";
                candidate = bestElementText(inner);
                candidate = longer(candidate, attributeValue(attrs, "title"));
                candidate = longer(candidate, attributeValue(attrs, "aria-label"));
            }
            if (candidate.length() > best.length()) best = candidate;
        }
        return collapseRepeatedText(best).trim();
    }

    private static String bestElementText(String html) {
        String best = decodeHtml(html.replaceAll("(?is)<[^>]+>", " "));
        Matcher tags = Pattern.compile("(?is)<[a-z][a-z0-9:-]*\\b([^>]*)>").matcher(html);
        while (tags.find()) {
            String attrs = tags.group(1);
            best = longer(best, attributeValue(attrs, "title"));
            best = longer(best, attributeValue(attrs, "aria-label"));
            best = longer(best, attributeValue(attrs, "data-content"));
            best = longer(best, attributeValue(attrs, "data-original-title"));
            best = longer(best, attributeValue(attrs, "data-fulltext"));
            best = longer(best, attributeValue(attrs, "data-full-text"));
        }
        return best;
    }

    private static String extractSmsCfg(String row) {
        Matcher tags = Pattern.compile("(?is)<button\\b([^>]*)>").matcher(row);
        while (tags.find()) {
            String attrs = tags.group(1);
            String name = attributeValue(attrs, "name");
            if (!name.toLowerCase(Locale.ROOT).endsWith(".readsms")) continue;
            String cfg = cfgFromOnclick(attributeValue(attrs, "onclick"));
            if (!cfg.isEmpty()) return cfg;
        }
        return cfgFromOnclick(row);
    }

    private static String cfgFromOnclick(String text) {
        Matcher cfg = Pattern.compile("(?i)(?:[?&]|\\b)cfg=([^&'\"\\s)]+)").matcher(text == null ? "" : text);
        return cfg.find() ? decodeHtml(cfg.group(1)).trim() : "";
    }

    private static boolean hasSuffix(String id, String... suffixes) {
        String lower = id.toLowerCase(Locale.ROOT);
        for (String suffix : suffixes) {
            if (Pattern.compile("(?:^|[._-])" + Pattern.quote(suffix) + "$").matcher(lower).find()) return true;
        }
        return false;
    }

    private static String attributeValue(String attrs, String name) {
        Matcher value = Pattern.compile("(?is)(?:^|\\s)" + Pattern.quote(name)
                + "\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s>]+))").matcher(attrs == null ? "" : attrs);
        if (!value.find()) return "";
        return firstNonEmpty(value.group(1), value.group(2), value.group(3));
    }

    private static String formValue(HtmlForms.Form form, String name) {
        for (HtmlForms.Control control : form.controls) {
            if (name.equals(control.name)) return control.value;
        }
        for (HtmlForms.Field field : form.fields) {
            if (name.equals(field.name)) return field.value;
        }
        return "";
    }

    private static String normalizeSmsText(String text) {
        String normalized = Normalizer.normalize(text == null ? "" : text, Normalizer.Form.NFKC)
                .replaceAll("[\\u00ad\\u200b-\\u200f\\u202a-\\u202e\\u2060\\ufeff]", "")
                .replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
        return normalized;
    }

    private static String collapseRepeatedText(String text) {
        List<String> parts = new ArrayList<>();
        for (String part : text.split("\\s+")) if (!part.isEmpty()) parts.add(part);
        while (parts.size() >= 2 && parts.size() % 2 == 0) {
            int half = parts.size() / 2;
            if (!parts.subList(0, half).equals(parts.subList(half, parts.size()))) break;
            parts = new ArrayList<>(parts.subList(0, half));
        }
        return TextUtils.join(" ", parts);
    }

    private static String decodeHtml(String text) {
        String value = text == null ? "" : text;
        if (Build.VERSION.SDK_INT >= 24) return Html.fromHtml(value, Html.FROM_HTML_MODE_LEGACY).toString();
        //noinspection deprecation
        return Html.fromHtml(value).toString();
    }

    private static String longer(String current, String candidate) {
        String normalized = decodeHtml(candidate).trim();
        return normalized.length() > (current == null ? 0 : current.length()) ? normalized : current;
    }

    private static String firstNonEmpty(String... values) {
        for (String value : values) if (value != null && !value.isEmpty()) return value;
        return "";
    }

    BandConfig readBandConfig() throws IOException {
        HttpResult page = request("GET", resolve(BAND_PAGE), null, null, false);
        if (page.status != 200 || hasLoginField(page.body)) {
            throw new IOException("The router band-selection page could not be opened.");
        }
        HtmlForms.Form form = HtmlForms.find(page.body,
                "cbid.network.4g.selband", "cbid.network.4g.ltebands");
        if (form == null) throw new IOException("The router band-selection form was not found.");
        LinkedHashSet<String> available = new LinkedHashSet<>();
        LinkedHashSet<String> selected = new LinkedHashSet<>();
        for (HtmlForms.Control control : form.controls) {
            if ("cbid.network.4g.ltebands".equals(control.name) && !control.value.isEmpty()) {
                available.add(control.value);
                if (control.checked) selected.add(control.value);
            }
        }
        if (available.isEmpty()) throw new IOException("The router did not report any LTE band choices.");
        String selectedMode = "0";
        for (HtmlForms.Control control : form.controls) {
            if ("cbid.network.4g.selband".equals(control.name) && control.checked) {
                selectedMode = control.value;
                break;
            }
        }
        if ("0".equals(selectedMode)) {
            for (HtmlForms.Field field : form.fields) {
                if ("cbid.network.4g.selband".equals(field.name) && "1".equals(field.value)) {
                    selectedMode = "1";
                    break;
                }
            }
        }
        boolean automatic = !"1".equals(selectedMode);
        return new BandConfig(automatic, new ArrayList<>(available),
                automatic ? new ArrayList<>() : new ArrayList<>(selected));
    }

    void applyBandConfig(boolean automatic, List<String> selectedBands) throws IOException {
        BandConfig before = readBandConfig();
        List<String> desired = automatic
                ? new ArrayList<>()
                : new ArrayList<>(new LinkedHashSet<>(selectedBands));
        if (!automatic && desired.isEmpty()) throw new IOException("Choose at least one LTE band.");
        if (!before.available.containsAll(desired)) throw new IOException("The selection includes a band not offered by this router.");

        try {
            submitBandConfig(automatic, desired);
            verifyBandConfig(automatic, desired, 30_000L);
            if (!waitForInternet(45_000L)) {
                throw new IOException("Internet did not return with the new LTE band choice.");
            }
        } catch (IOException failure) {
            try {
                submitBandConfig(before.automatic, before.selected);
                verifyBandConfig(before.automatic, before.selected, 30_000L);
                throw new IOException(failure.getMessage() + " The previous band choice was restored.", failure);
            } catch (IOException rollbackFailure) {
                if (rollbackFailure.getMessage() != null
                        && rollbackFailure.getMessage().contains("previous band choice was restored")) {
                    throw rollbackFailure;
                }
                throw new IOException(failure.getMessage()
                        + " Automatic rollback could not be confirmed; check the router's band page.", failure);
            }
        }
    }

    void changePasswordAndVerify(String newPassword) throws IOException {
        HttpResult page = request("GET", resolve(PASSWORD_PAGE), null, null, false);
        if (page.status != 200 || hasLoginField(page.body)) {
            throw new IOException("The router password settings could not be opened.");
        }
        String settingsPageUrl = page.url;
        String expectedPost = resolve(PASSWORD_POST);
        HtmlForms.Form form = findPasswordSaveForm(page, expectedPost);
        if (form == null) {
            // On this Cudy firmware /admin/panel is a JavaScript modal shell. Its rendered
            // Admin Account form is fetched separately from the form's own GET route.
            log("PASSWORD | panel response has no password form; requesting the form route directly.");
            page = request("GET", expectedPost, null, settingsPageUrl, false);
            if (page.status != 200 || hasLoginField(page.body)) {
                throw new IOException("The router password form page could not be opened (HTTP "
                        + page.status + ").");
            }
            form = findPasswordSaveForm(page, expectedPost);
        }
        if (form == null) {
            throw new IOException("The router returned its password page, but the password form was not present.");
        }
        String actionUrl = sameOriginAction(page.url, form.action);
        if (!sameUrl(actionUrl, expectedPost)) {
            throw new IOException("The router password form points to an unexpected save address.");
        }
        String token = inputValue(form, "token");
        String oldSalt = inputValue(form, "cbid.luci._pass.pw1.salt");
        String newSalt = inputValue(form, "cbid.luci._pass.pw2.salt");
        String confirmSalt = inputValue(form, "cbid.luci._pass.pw3.salt");
        if (token.isEmpty() || oldSalt.isEmpty() || newSalt.isEmpty() || confirmSalt.isEmpty()) {
            throw new IOException("The router password form did not contain all required security fields.");
        }
        form.set("cbid.luci._pass.pw1", password);
        form.set("cbid.luci._pass.pw1.sha", sha256Hex(password + oldSalt));
        form.set("cbid.luci._pass.pw2", newPassword);
        form.set("cbid.luci._pass.pw2.sha", sha256Hex(newPassword + newSalt));
        form.set("cbid.luci._pass.pw3", newPassword);
        form.set("cbid.luci._pass.pw3.sha", sha256Hex(newPassword + confirmSalt));
        form.set("timeclock", String.valueOf(System.currentTimeMillis() / 1000L));
        form.set("cbi.submit", "1");
        form.set("cbi.apply", "");
        HttpResult submitted = request("POST", actionUrl, form.fields, settingsPageUrl, true);
        if (submitted.status != 200 && submitted.status != 302 && submitted.status != 303) {
            throw new IOException("The router did not accept the password update (HTTP " + submitted.status + ").");
        }
        CudyRouterClient verifier = new CudyRouterClient(baseUrl, username, newPassword, eventLogger);
        verifier.loginAndVerify();
    }

    private HtmlForms.Form findPasswordSaveForm(HttpResult page, String expectedPost) {
        List<HtmlForms.Form> parsedForms = HtmlForms.parse(page.body);
        String lowerPage = page.body.toLowerCase(Locale.ROOT);
        log("PASSWORD | page=" + safePath(page.url) + ", formTag=" + lowerPage.contains("<form")
                + ", passwordSavePath=" + lowerPage.contains(PASSWORD_POST)
                + ", requiredSalts=" + lowerPage.contains("cbid.luci._pass.pw1.salt")
                + "/" + lowerPage.contains("cbid.luci._pass.pw2.salt")
                + "/" + lowerPage.contains("cbid.luci._pass.pw3.salt")
                + ", parsedForms=" + parsedForms.size());
        for (HtmlForms.Form candidate : parsedForms) {
            String candidatePath = "<missing-action>";
            boolean expectedAction = false;
            try {
                String candidateUrl = sameOriginAction(page.url, candidate.action);
                candidatePath = safePath(candidateUrl);
                expectedAction = sameUrl(candidateUrl, expectedPost);
            } catch (IOException ignored) {
                // Report only the safe path marker; the original action may contain query data.
            }
            log("PASSWORD | form candidate: name=" + present(candidate.attributes.get("name"))
                    + ", method=" + candidate.method + ", action=" + candidatePath
                    + ", expectedAction=" + expectedAction);
            if ("post".equalsIgnoreCase(candidate.method) && expectedAction) return candidate;
        }
        return null;
    }

    private void submitBandConfig(boolean automatic, List<String> bands) throws IOException {
        HttpResult page = request("GET", resolve(BAND_PAGE), null, null, false);
        if (page.status != 200 || hasLoginField(page.body)) throw new IOException("Router band form session expired.");
        HtmlForms.Form form = HtmlForms.find(page.body,
                "cbid.network.4g.selband", "cbid.network.4g.ltebands");
        if (form == null || !"post".equalsIgnoreCase(form.method)) {
            throw new IOException("The router band form is not available for saving.");
        }
        form.remove("cbid.network.4g.selband");
        form.remove("cbid.network.4g.ltebands");
        form.remove("cbi.apply");
        form.add("cbid.network.4g.selband", automatic ? "0" : "1");
        for (String band : bands) form.add("cbid.network.4g.ltebands", band);
        form.add("cbi.apply", "");
        if (!form.hasField("cbi.submit")) form.add("cbi.submit", "1");
        else form.set("cbi.submit", "1");
        if (form.hasField("timeclock")) form.set("timeclock", String.valueOf(System.currentTimeMillis() / 1000L));
        HttpResult submitted = request("POST", sameOriginAction(page.url, form.action), form.fields, page.url, false);
        if (submitted.status == 403 || hasLoginField(submitted.body)) {
            throw new IOException("The router session expired while saving the band selection.");
        }
        if (submitted.status != 200 && submitted.status != 302 && submitted.status != 303) {
            throw new IOException("The router rejected the band selection (HTTP " + submitted.status + ").");
        }
    }

    private void verifyBandConfig(boolean automatic, List<String> bands, long timeoutMs) throws IOException {
        long deadline = android.os.SystemClock.elapsedRealtime() + timeoutMs;
        IOException lastError = null;
        do {
            try {
                BandConfig current = readBandConfig();
                if (current.automatic == automatic && (automatic
                        || new LinkedHashSet<>(current.selected).equals(new LinkedHashSet<>(bands)))) return;
            } catch (IOException error) {
                lastError = error;
            }
            try {
                Thread.sleep(1_500L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new IOException("Band verification was interrupted.", interrupted);
            }
        } while (android.os.SystemClock.elapsedRealtime() < deadline);
        throw new IOException(lastError == null
                ? "The router did not confirm the saved band selection."
                : "The router band selection could not be read back: " + lastError.getMessage(), lastError);
    }

    private boolean waitForInternet(long timeoutMs) {
        long deadline = android.os.SystemClock.elapsedRealtime() + timeoutMs;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            HttpURLConnection connection = null;
            try {
                String checkUrl = "https://speed.cloudflare.com/__down?bytes=0&check=" + System.nanoTime();
                connection = (HttpURLConnection) new URL(checkUrl).openConnection();
                connection.setConnectTimeout(5_000);
                connection.setReadTimeout(5_000);
                connection.setRequestMethod("GET");
                int status = connection.getResponseCode();
                if (status >= 200 && status < 400) return true;
            } catch (IOException ignored) {
                // Retry while the router renegotiates LTE.
            } finally {
                if (connection != null) connection.disconnect();
            }
            try {
                Thread.sleep(2_000L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        return false;
    }

    private HttpResult request(String method, String address, List<HtmlForms.Field> fields,
                               String referer, boolean multipart) throws IOException {
        String currentAddress = address;
        String currentReferer = referer;
        for (int redirectCount = 0; ; redirectCount++) {
            HttpURLConnection connection = null;
            String requestPath = safePath(currentAddress);
            log("HTTP | request " + method + " " + requestPath);
            try {
                URL url = new URL(currentAddress);
                connection = (HttpURLConnection) url.openConnection();
                connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
                connection.setReadTimeout(READ_TIMEOUT_MS);
                connection.setInstanceFollowRedirects(false);
                connection.setRequestMethod(method);
                // Match the generic browser-style agent used by the desktop client;
                // some router firmware rejects app-specific User-Agent strings.
                connection.setRequestProperty("User-Agent", "Mozilla/5.0");
                connection.setRequestProperty("Accept", "*/*");
                if (currentReferer != null) connection.setRequestProperty("Referer", currentReferer);
                List<String> cookieHeaders = cookies.get(URI.create(url.toString()), java.util.Collections.emptyMap())
                        .get("Cookie");
                String cookie = cookieHeaders == null || cookieHeaders.isEmpty()
                        ? "" : TextUtils.join("; ", cookieHeaders);
                if (!cookie.isEmpty()) connection.setRequestProperty("Cookie", cookie);

                if ("POST".equals(method)) {
                    connection.setDoOutput(true);
                    byte[] body;
                    if (multipart) {
                        String boundary = "----CudyAndroid" + UUID.randomUUID().toString().replace("-", "");
                        connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
                        body = encodeMultipart(fields, boundary);
                    } else {
                        connection.setRequestProperty("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
                        body = encodeUrlEncoded(fields).getBytes(StandardCharsets.UTF_8);
                    }
                    connection.setFixedLengthStreamingMode(body.length);
                    try (OutputStream output = connection.getOutputStream()) {
                        output.write(body);
                    }
                    java.util.Arrays.fill(body, (byte) 0);
                }

                int status = connection.getResponseCode();
                log("HTTP | response " + method + " " + safePath(url.toString()) + " -> " + status);
                try {
                    cookies.put(url.toURI(), connection.getHeaderFields());
                } catch (Exception ignored) {
                    // Cookie parsing errors are surfaced as login failure on the protected-page check.
                }

                String location = connection.getHeaderField("Location");
                if ("GET".equals(method) && isRedirect(status) && !TextUtils.isEmpty(location)) {
                    if (redirectCount >= 5) {
                        throw new IOException("The router returned too many redirects while opening its login page.");
                    }
                    URL target = new URL(url, location);
                    if (!sameOrigin(url, target)) {
                        throw new IOException("The router redirected to a different address; the app stopped the request for security.");
                    }
                    currentAddress = target.toString();
                    currentReferer = url.toString();
                    continue;
                }

                InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
                String responseBody = stream == null ? "" : readUtf8(stream);
                if (status == 401 || status == 403 || hasLoginField(responseBody)) {
                    sessionVerifiedAtMs = 0;
                } else if (sessionVerifiedAtMs > 0 && status < 400) {
                    sessionVerifiedAtMs = System.currentTimeMillis();
                }
                return new HttpResult(status, responseBody, url.toString());
            } catch (IOException error) {
                log("HTTP | failure " + method + " " + requestPath + " -> "
                        + error.getClass().getSimpleName());
                throw error;
            } finally {
                if (connection != null) connection.disconnect();
            }
        }
    }

    private void log(String message) {
        if (eventLogger != null) eventLogger.log(message);
    }

    private static String present(String value) {
        return TextUtils.isEmpty(value) ? "missing" : "present";
    }

    private static String safePath(String address) {
        try {
            String path = new URL(address).getPath();
            return TextUtils.isEmpty(path) ? "/" : path;
        } catch (Exception ignored) {
            return "<invalid-url>";
        }
    }

    private static boolean isRedirect(int status) {
        return status == 301 || status == 302 || status == 303 || status == 307 || status == 308;
    }

    private static boolean sameOrigin(URL left, URL right) {
        int leftPort = left.getPort() == -1 ? left.getDefaultPort() : left.getPort();
        int rightPort = right.getPort() == -1 ? right.getDefaultPort() : right.getPort();
        return left.getProtocol().equalsIgnoreCase(right.getProtocol())
                && left.getHost().equalsIgnoreCase(right.getHost())
                && leftPort == rightPort;
    }

    private String resolve(String path) throws IOException {
        return new URL(new URL(baseUrl + "/"), path).toString();
    }

    private String sameOriginAction(String pageUrl, String action) throws IOException {
        if (action == null || action.trim().isEmpty()) throw new IOException("Router form is missing its save address.");
        URL page = new URL(pageUrl);
        URL target = new URL(page, action);
        int pagePort = page.getPort() == -1 ? page.getDefaultPort() : page.getPort();
        int targetPort = target.getPort() == -1 ? target.getDefaultPort() : target.getPort();
        if (!page.getProtocol().equalsIgnoreCase(target.getProtocol())
                || !page.getHost().equalsIgnoreCase(target.getHost()) || pagePort != targetPort) {
            throw new IOException("Router form points outside the configured router address.");
        }
        return target.toString();
    }

    private static boolean sameUrl(String left, String right) throws IOException {
        URL a = new URL(left);
        URL b = new URL(right);
        return a.getProtocol().equalsIgnoreCase(b.getProtocol())
                && a.getHost().equalsIgnoreCase(b.getHost())
                && a.getPort() == b.getPort()
                && a.getPath().equals(b.getPath());
    }

    private static void requireStatus(HttpResult result, int expected, String message) throws IOException {
        if (result.status != expected) throw new IOException(message + " (HTTP " + result.status + ").");
    }

    private static boolean hasLoginField(String html) {
        return html != null && PatternHolder.LOGIN_PASSWORD.matcher(html).find();
    }

    private static String inputValue(HtmlForms.Form form, String name) {
        for (HtmlForms.Control control : form.controls) {
            if (name.equals(control.name)) return control.value;
        }
        for (HtmlForms.Field field : form.fields) {
            if (name.equals(field.name)) return field.value;
        }
        return "";
    }

    private static String sha256Hex(String value) throws IOException {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte item : digest) hex.append(String.format(Locale.ROOT, "%02x", item & 0xff));
            java.util.Arrays.fill(digest, (byte) 0);
            return hex.toString();
        } catch (Exception error) {
            throw new IOException("The required password hash could not be calculated.", error);
        }
    }

    private static String encodeUrlEncoded(List<HtmlForms.Field> fields) throws IOException {
        StringBuilder result = new StringBuilder();
        for (HtmlForms.Field field : fields) {
            if (result.length() > 0) result.append('&');
            result.append(URLEncoder.encode(field.name, "UTF-8"));
            result.append('=');
            result.append(URLEncoder.encode(field.value, "UTF-8"));
        }
        return result.toString();
    }

    private static byte[] encodeMultipart(List<HtmlForms.Field> fields, String boundary) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        for (HtmlForms.Field field : fields) {
            String safeName = field.name.replace("\"", "");
            output.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\""
                    + safeName + "\"\r\n\r\n" + field.value + "\r\n").getBytes(StandardCharsets.UTF_8));
        }
        output.write(("--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
        return output.toByteArray();
    }

    private static String readUtf8(InputStream input) throws IOException {
        try (InputStream stream = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = stream.read(buffer)) != -1) output.write(buffer, 0, count);
            return output.toString("UTF-8");
        }
    }

    private static final class PatternHolder {
        static final java.util.regex.Pattern LOGIN_PASSWORD = java.util.regex.Pattern.compile(
                "(?is)<input\\b(?=[^>]*\\bname\\s*=\\s*['\"]?luci_password(?:['\"\\s>]))[^>]*>");
    }
}
