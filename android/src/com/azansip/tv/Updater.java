package com.azansip.tv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.util.Arrays;

/** "Update the app" in the menu: fetches the newest Azan SIP TV from the server it shows (/apk) and
 *  from AthanBox's releases on GitHub, and hands a newer one to Android's installer, which asks
 *  before installing. Android installs it over this app only if it was signed with the same key. */
final class Updater {
    static final String TYPE = "application/vnd.android.package-archive";
    static final int ASK_INSTALLS = 41;
    /** AthanBox's releases (public) carry the app too. */
    private static final String RELEASES = "https://api.github.com/repos/Fudgeamin99/azan-sip-box/releases?per_page=30";
    private static final long MAX = 40L * 1024 * 1024;

    private final Activity a;
    private final String base;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private AlertDialog dialog;
    private int run;
    private boolean busy;

    Updater(Activity a, String base) {
        this.a = a;
        this.base = base;
    }

    /** Where the update waits for the installer (ApkProvider hands it over). */
    static File file(Context c) {
        return new File(new File(c.getCacheDir(), "update"), ApkProvider.NAME);
    }

    /** This app's version, as people see it ("1.1.0"). */
    static String version(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "";
        }
    }

    private static final class Apk {
        File file;
        long code;
        String name;
        Signature[] signatures;
        String from;
    }

    // ---------------------------------------------------------------- looking for one

    void start() {
        if (busy) return;
        busy = true;
        final int mine = ++run;
        dialog = new AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Update the app")
                .setMessage("Looking for a newer Azan SIP TV…")
                .setNegativeButton("Cancel", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) { cancel(); }
                })
                .setOnCancelListener(new DialogInterface.OnCancelListener() {
                    @Override public void onCancel(DialogInterface d) { cancel(); }
                })
                .show();
        new Thread(new Runnable() {
            @Override public void run() {
                final String[] text = new String[1];
                final boolean ready = look(text);
                handler.post(new Runnable() {
                    @Override public void run() {
                        if (mine != run || a.isFinishing()) return;
                        busy = false;
                        close();
                        if (ready) offer(text[0]);
                        else say("Update the app", text[0]);
                    }
                });
            }
        }, "update").start();
    }

    private void cancel() {
        run++;
        busy = false;
    }

    /** Fetches both and keeps the newest that can go over this app. True if there is one (text says
     *  what it is), otherwise text says why not. */
    @SuppressWarnings("deprecation")
    private boolean look(String[] text) {
        PackageManager pm = a.getPackageManager();
        long have = 0;
        String haveName = "";
        Signature[] haveSig = null;
        try {
            PackageInfo pi = pm.getPackageInfo(a.getPackageName(), PackageManager.GET_SIGNATURES);
            have = code(pi);
            haveName = pi.versionName;
            haveSig = pi.signatures;
        } catch (Exception ignored) {
            // (always there)
        }
        File dir = file(a).getParentFile();
        dir.mkdirs();
        for (File old : dir.listFiles() == null ? new File[0] : dir.listFiles()) old.delete();

        String[] why = new String[2];
        Apk fromServer = fetch(base + "/apk", new File(dir, "server.apk"), Net.display(base), why, 0);
        String link = null;
        try {
            link = githubLink();
            if (link == null) why[1] = "no release has the app";
        } catch (Exception e) {
            why[1] = reason(e);
        }
        Apk fromGithub = link == null ? null : fetch(link, new File(dir, "github.apk"), "GitHub", why, 1);

        Apk best = null;
        Apk other = null;   // newer, but signed with another key
        for (Apk x : new Apk[]{fromServer, fromGithub}) {
            if (x == null || x.code <= have) continue;
            boolean same = haveSig == null || x.signatures == null || x.signatures.length == 0
                    || Arrays.equals(haveSig, x.signatures);
            if (same) {
                if (best == null || x.code > best.code) best = x;
            } else if (other == null || x.code > other.code) {
                other = x;
            }
        }
        if (best != null) {
            File to = file(a);
            if (!best.file.renameTo(to)) {
                text[0] = "Couldn't keep the download on this screen. Is its storage full?";
                return false;
            }
            text[0] = "Version " + best.name + " is ready (this screen has " + haveName + "). It came from " + best.from
                    + ". Android will ask before it installs it, and the display opens again after.";
            return true;
        }
        if (other != null) {
            text[0] = "Version " + other.name + " is out, but it's signed differently from the app on this screen, "
                    + "so Android won't install it over it. Remove Azan SIP TV from this screen once (Settings → Apps), "
                    + "then install it again from " + Net.display(base) + "/apk, as the first time. After that, "
                    + "updates install from here.";
            return false;
        }
        if (fromServer == null && fromGithub == null) {
            text[0] = "Couldn't look for an update. From " + Net.display(base) + ": " + why[0] + ". From GitHub: " + why[1] + ".";
            return false;
        }
        text[0] = "Azan SIP TV is up to date (version " + haveName + ")."
                + (fromServer == null ? " Couldn't check " + Net.display(base) + ": " + why[0] + "." : "")
                + (fromGithub == null ? " Couldn't check GitHub: " + why[1] + "." : "");
        return false;
    }

    @SuppressWarnings("deprecation")
    private static long code(PackageInfo pi) {
        return Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode;
    }

    /** Downloads an APK and reads it; null if it isn't this app (why[i] says why). */
    @SuppressWarnings("deprecation")
    private Apk fetch(String link, File to, String from, String[] why, int i) {
        try {
            download(link, to);
        } catch (Exception e) {
            why[i] = reason(e);
            to.delete();
            return null;
        }
        PackageInfo pi = a.getPackageManager().getPackageArchiveInfo(to.getPath(), PackageManager.GET_SIGNATURES);
        if (pi == null || !a.getPackageName().equals(pi.packageName)) {
            why[i] = "what it sent isn't the app";
            to.delete();
            return null;
        }
        Apk x = new Apk();
        x.file = to;
        x.code = code(pi);
        x.name = pi.versionName;
        x.signatures = pi.signatures;
        x.from = from;
        return x;
    }

    /** The newest AthanBox release with the app attached. */
    private static String githubLink() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        get(RELEASES, out, 4L * 1024 * 1024, "application/vnd.github+json");
        JSONArray all = new JSONArray(out.toString("UTF-8"));
        for (int i = 0; i < all.length(); i++) {
            JSONObject r = all.getJSONObject(i);
            if (r.optBoolean("draft") || r.optBoolean("prerelease")) continue;
            JSONArray assets = r.optJSONArray("assets");
            for (int k = 0; assets != null && k < assets.length(); k++) {
                JSONObject f = assets.getJSONObject(k);
                if (ApkProvider.NAME.equals(f.optString("name"))) return f.optString("browser_download_url", null);
            }
        }
        return null;
    }

    private static void download(String link, File to) throws Exception {
        OutputStream out = new FileOutputStream(to);
        try {
            get(link, out, MAX, null);
        } finally {
            out.close();
        }
    }

    /** GET, following redirects (also from http to https, which Android doesn't by itself). */
    private static void get(String link, OutputStream out, long max, String accept) throws Exception {
        URL u = new URL(link);
        for (int hop = 0; hop < 6; hop++) {
            HttpURLConnection con = (HttpURLConnection) u.openConnection();
            try {
                con.setInstanceFollowRedirects(false);
                con.setConnectTimeout(8000);
                con.setReadTimeout(30000);
                con.setUseCaches(false);
                con.setRequestProperty("User-Agent", "AzanSIPTV");
                if (accept != null) con.setRequestProperty("Accept", accept);
                int status = con.getResponseCode();
                if (status >= 300 && status < 400 && con.getHeaderField("Location") != null) {
                    u = new URL(u, con.getHeaderField("Location"));
                    continue;
                }
                if (status != 200) throw new Failed(status == 404 ? "it isn't there" : "error " + status);
                InputStream in = con.getInputStream();
                byte[] buf = new byte[16384];
                long total = 0;
                int n;
                while ((n = in.read(buf)) > 0) {
                    total += n;
                    if (total > max) throw new Failed("it's too big");
                    out.write(buf, 0, n);
                }
                in.close();
                return;
            } finally {
                con.disconnect();
            }
        }
        throw new Failed("too many redirects");
    }

    private static final class Failed extends Exception {
        Failed(String why) { super(why); }
    }

    private static String reason(Exception e) {
        if (e instanceof Failed) return e.getMessage();
        if (e instanceof UnknownHostException) return "no internet";
        if (e instanceof SocketTimeoutException) return "it didn't answer";
        return "couldn't reach it";
    }

    // ---------------------------------------------------------------- installing it

    private void offer(String text) {
        dialog = new AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Update the app")
                .setMessage(text)
                .setPositiveButton("Install", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int w) { install(); }
                })
                .setNegativeButton("Not now", null)
                .show();
    }

    private void install() {
        if (!file(a).isFile()) return;
        if (Build.VERSION.SDK_INT >= 26 && !a.getPackageManager().canRequestPackageInstalls()) {
            dialog = new AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
                    .setTitle("Allow updates")
                    .setMessage("Android asks once whether Azan SIP TV may install its updates. On the next screen, switch on "
                            + "“Allow from this source”, then press Back.")
                    .setPositiveButton("Open the setting", new DialogInterface.OnClickListener() {
                        @Override public void onClick(DialogInterface d, int w) { allow(); }
                    })
                    .setNegativeButton("Not now", null)
                    .show();
            return;
        }
        Intent i = new Intent(Intent.ACTION_VIEW);
        if (Build.VERSION.SDK_INT >= 24) {
            i.setDataAndType(ApkProvider.uri(), TYPE);
            i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } else {   // (Android 5 and 6 install from a file the installer can read)
            File f = file(a);
            a.getCacheDir().setExecutable(true, false);
            f.getParentFile().setExecutable(true, false);
            f.setReadable(true, false);
            i.setDataAndType(Uri.fromFile(f), TYPE);
        }
        try {
            a.startActivity(i);
        } catch (ActivityNotFoundException e) {
            say("Update the app", "This screen has no app installer. Install the update from " + Net.display(base) + "/apk instead.");
        }
    }

    private void allow() {
        try {
            a.startActivityForResult(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + a.getPackageName())), ASK_INSTALLS);
        } catch (ActivityNotFoundException e) {
            say("Allow updates", "Open Settings → Apps → Special app access → Install unknown apps, choose Azan SIP TV "
                    + "and allow it. Then choose “Update the app” again.");
        }
    }

    /** Back from the setting: install if it's allowed now. */
    void allowed() {
        if (Build.VERSION.SDK_INT >= 26 && a.getPackageManager().canRequestPackageInstalls()) install();
        else if (file(a).isFile()) say("Allow updates", "Installing updates isn't allowed yet, so the update wasn't installed.");
    }

    private void say(String title, String text) {
        dialog = new AlertDialog.Builder(a, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle(title)
                .setMessage(text)
                .setPositiveButton("OK", null)
                .show();
    }

    private void close() {
        if (dialog != null && dialog.isShowing()) dialog.dismiss();
        dialog = null;
    }

    void release() {
        run++;
        close();
    }
}
