package de.prodbykesti.cudyapp;

import android.os.Build;
import android.text.Html;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Small, router-focused HTML form reader. It keeps successful values and all input choices. */
final class HtmlForms {
    private static final Pattern FORM = Pattern.compile("(?is)<form\\b([^>]*)>(.*?)</form\\s*>");
    private static final Pattern CONTROL = Pattern.compile(
            "(?is)<(input|textarea|select|button)\\b([^>]*)>(.*?)</\\s*\\1\\s*>|<input\\b([^>]*)/?>");
    private static final Pattern ATTRIBUTE = Pattern.compile(
            "(?is)([a-z_:][a-z0-9_:.-]*)\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s>]+))");
    private static final Pattern OPTION = Pattern.compile("(?is)<option\\b([^>]*)>(.*?)</option\\s*>");

    static final class Field {
        final String name;
        final String value;

        Field(String name, String value) {
            this.name = name;
            this.value = value == null ? "" : value;
        }
    }

    static final class Control {
        final String name;
        final String value;
        final String type;
        final boolean checked;

        Control(String name, String value, String type, boolean checked) {
            this.name = name;
            this.value = value == null ? "" : value;
            this.type = type == null ? "" : type.toLowerCase(Locale.ROOT);
            this.checked = checked;
        }
    }

    static final class Form {
        final String action;
        final String method;
        final Map<String, String> attributes;
        final List<Field> fields = new ArrayList<>();
        final List<Control> controls = new ArrayList<>();

        Form(Map<String, String> attributes) {
            this.attributes = attributes;
            this.action = valueOr(attributes, "action", "");
            this.method = valueOr(attributes, "method", "get");
        }

        boolean hasField(String name) {
            for (Control control : controls) {
                if (name.equals(control.name)) return true;
            }
            for (Field field : fields) {
                if (name.equals(field.name)) return true;
            }
            return false;
        }

        void set(String name, String value) {
            remove(name);
            fields.add(new Field(name, value));
        }

        void remove(String name) {
            for (int index = fields.size() - 1; index >= 0; index--) {
                if (name.equals(fields.get(index).name)) fields.remove(index);
            }
        }

        void add(String name, String value) {
            fields.add(new Field(name, value));
        }
    }

    private HtmlForms() { }

    static List<Form> parse(String html) {
        List<Form> forms = new ArrayList<>();
        Matcher formMatcher = FORM.matcher(html == null ? "" : html);
        while (formMatcher.find()) {
            Form form = new Form(attributes(formMatcher.group(1)));
            parseControls(form, formMatcher.group(2));
            forms.add(form);
        }
        return forms;
    }

    static Form find(String html, String... requiredNames) {
        for (Form form : parse(html)) {
            boolean matches = true;
            for (String requiredName : requiredNames) {
                if (!form.hasField(requiredName)) {
                    matches = false;
                    break;
                }
            }
            if (matches) return form;
        }
        return null;
    }

    static String inputValue(String html, String name) {
        for (Form form : parse(html)) {
            for (Control control : form.controls) {
                if (name.equals(control.name) && control.checked) return control.value;
            }
        }
        Matcher matcher = CONTROL.matcher(html == null ? "" : html);
        while (matcher.find()) {
            String tag = matcher.group(1);
            String attrs = tag == null ? matcher.group(4) : matcher.group(2);
            if (tag == null || "input".equalsIgnoreCase(tag)) {
                Map<String, String> values = attributes(attrs);
                if (name.equals(values.get("name"))) return valueOr(values, "value", "");
            }
        }
        return "";
    }

    private static void parseControls(Form form, String body) {
        Matcher matcher = CONTROL.matcher(body);
        while (matcher.find()) {
            String tag = matcher.group(1);
            String attrs = tag == null ? matcher.group(4) : matcher.group(2);
            String content = tag == null ? "" : matcher.group(3);
            if (attrs == null) continue;
            Map<String, String> values = attributes(attrs);
            String name = values.get("name");
            if (name == null || name.isEmpty()) continue;

            if (tag == null || "input".equalsIgnoreCase(tag)) {
                String type = valueOr(values, "type", "text").toLowerCase(Locale.ROOT);
                String value = valueOr(values, "value", "");
                boolean checked = hasAttribute(attrs, "checked");
                Control control = new Control(name, value, type, checked);
                form.controls.add(control);
                if (isSuccessfulInput(type, checked)) form.fields.add(new Field(name, value));
            } else if ("textarea".equalsIgnoreCase(tag)) {
                String value = decode(content.replaceAll("(?is)</?[^>]+>", "").trim());
                form.controls.add(new Control(name, value, "textarea", false));
                form.fields.add(new Field(name, value));
            } else if ("select".equalsIgnoreCase(tag)) {
                boolean multiple = hasAttribute(attrs, "multiple");
                List<Field> options = new ArrayList<>();
                Matcher optionMatcher = OPTION.matcher(content);
                boolean selectedFound = false;
                while (optionMatcher.find()) {
                    Map<String, String> optionAttributes = attributes(optionMatcher.group(1));
                    String optionText = decode(optionMatcher.group(2).replaceAll("(?is)</?[^>]+>", "").trim());
                    String value = valueOr(optionAttributes, "value", optionText);
                    boolean selected = hasAttribute(optionMatcher.group(1), "selected");
                    selectedFound |= selected;
                    if (selected) options.add(new Field(name, value));
                    form.controls.add(new Control(name, value, "option", selected));
                }
                if (options.isEmpty() && !selectedFound && !multiple) {
                    for (Control control : form.controls) {
                        if (name.equals(control.name) && "option".equals(control.type)) {
                            options.add(new Field(name, control.value));
                            break;
                        }
                    }
                }
                form.fields.addAll(options);
            } else if ("button".equalsIgnoreCase(tag)) {
                String type = valueOr(values, "type", "submit").toLowerCase(Locale.ROOT);
                String buttonText = decode(content.replaceAll("(?is)</?[^>]+>", "").trim());
                String value = valueOr(values, "value", buttonText);
                form.controls.add(new Control(name, value, type, false));
                if (!"reset".equals(type) && !"button".equals(type)) {
                    form.fields.add(new Field(name, value));
                }
            }
        }
    }

    private static boolean isSuccessfulInput(String type, boolean checked) {
        if ("submit".equals(type) || "button".equals(type) || "reset".equals(type)
                || "image".equals(type) || "file".equals(type)) return false;
        return !("checkbox".equals(type) || "radio".equals(type)) || checked;
    }

    private static Map<String, String> attributes(String source) {
        Map<String, String> result = new LinkedHashMap<>();
        Matcher matcher = ATTRIBUTE.matcher(source == null ? "" : source);
        while (matcher.find()) {
            String value = matcher.group(2) != null ? matcher.group(2)
                    : matcher.group(3) != null ? matcher.group(3) : matcher.group(4);
            result.put(matcher.group(1).toLowerCase(Locale.ROOT), decode(value));
        }
        return result;
    }

    private static boolean hasAttribute(String source, String name) {
        return Pattern.compile("(?i)(?:^|\\s)" + Pattern.quote(name) + "(?:\\s|=|$)")
                .matcher(source == null ? "" : source).find();
    }

    private static String valueOr(Map<String, String> values, String name, String fallback) {
        String value = values.get(name);
        return value == null ? fallback : value;
    }

    private static String decode(String value) {
        String safe = value == null ? "" : value;
        if (Build.VERSION.SDK_INT >= 24) {
            return Html.fromHtml(safe, Html.FROM_HTML_MODE_LEGACY).toString();
        }
        //noinspection deprecation
        return Html.fromHtml(safe).toString();
    }
}
