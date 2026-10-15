package com.azansip.tv;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

/** The display, full screen: Azan SIP's TV app (/tv), which shows the prayer times and plays
 *  the azan. Sound is allowed without a tap, the screen stays on, and Back opens a small menu.
 *  Makkah live plays with Android's own player (LivePlayer) unless "the page's player" is chosen. */
public class TvActivity extends Activity {
    private static final long RETRY_MS = 10000;
    /** Makkah live didn't start this many times in a row with the app's own player: the page's instead (once). */
    private static final int GIVE_UP = 3;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private FrameLayout root;
    private WebView web;
    private LinearLayout errorBox;
    private TextView errorTitle;
    private TextView errorText;
    private String base;
    private boolean failed;
    private LivePlayer live;
    private Updater updater;
    private String liveJs;
    private boolean liveRelay;
    private int liveFaults;

    private final Runnable retry = new Runnable() {
        @Override public void run() { load(); }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        base = Net.server(this);
        if (base == null) {
            startActivity(new Intent(this, SetupActivity.class).putExtra("change", true));
            finish();
            return;
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(lp);
        }
        root = new FrameLayout(this);
        root.setBackgroundColor(Ui.BG);
        setContentView(root);
        liveJs = asset("live.js");
        live = new LivePlayer(this, new LiveEvents(), new View.OnClickListener() {
            @Override public void onClick(View v) { soundClick(); }
        });
        updater = new Updater(this, base);
        makeWebView();
        root.addView(live.view());   // over the page, under the error box
        makeErrorBox();
        hideSystemBars();
        load();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void makeWebView() {
        if (web != null) {
            live.stop();
            root.removeView(web);
            web.destroy();
        }
        web = new WebView(this);
        web.setBackgroundColor(Ui.BG);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // the azan plays without a tap
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setUserAgentString(s.getUserAgentString() + " AzanSIPTV");
        web.addJavascriptInterface(new Bridge(), "AzanTVApp");   // for assets/live.js
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient() {
            @Override public Bitmap getDefaultVideoPoster() {   // no grey play button before Makkah live starts
                return Bitmap.createBitmap(1, 1, Bitmap.Config.ARGB_8888);
            }
        });
        root.addView(web, 0, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void makeErrorBox() {
        errorBox = new LinearLayout(this);
        errorBox.setOrientation(LinearLayout.VERTICAL);
        errorBox.setGravity(Gravity.CENTER);
        errorBox.setBackgroundColor(Ui.BG);
        int pad = Ui.dp(this, 32);
        errorBox.setPadding(pad, pad, pad, pad);
        errorTitle = Ui.text(this, "", 30, Ui.INK, true);
        errorTitle.setGravity(Gravity.CENTER);
        errorText = Ui.text(this, "", 18, Ui.MUTED, false);
        errorText.setGravity(Gravity.CENTER);
        errorText.setMaxWidth(Ui.dp(this, 640));
        errorBox.addView(errorTitle);
        View g = new View(this);
        errorBox.addView(g, new LinearLayout.LayoutParams(1, Ui.dp(this, 12)));
        errorBox.addView(errorText);
        View g2 = new View(this);
        errorBox.addView(g2, new LinearLayout.LayoutParams(1, Ui.dp(this, 24)));
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER);
        Button again = Ui.button(this, "Try again", true);
        again.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { load(); }
        });
        Button change = Ui.button(this, "Change server", false);
        change.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { changeServer(); }
        });
        row.addView(again);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.leftMargin = Ui.dp(this, 12);
        row.addView(change, lp);
        errorBox.addView(row);
        errorBox.setVisibility(View.GONE);
        root.addView(errorBox, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private String url() {
        String name = Net.name(this);
        String u = base + "/tv?app=android";
        if (name != null && !name.trim().isEmpty()) u += "&name=" + Uri.encode(name.trim());
        return u;
    }

    private void load() {
        handler.removeCallbacks(retry);
        live.stop();
        failed = false;
        web.loadUrl(url());
    }

    private void showError(String title, String text) {
        failed = true;
        live.stop();
        errorTitle.setText(title);
        errorText.setText(text + " Trying again in 10 seconds.");
        errorBox.setVisibility(View.VISIBLE);
        View first = errorBox.findFocus();
        if (first == null) errorBox.requestFocus();
        handler.removeCallbacks(retry);
        handler.postDelayed(retry, RETRY_MS);
    }

    private class Client extends WebViewClient {
        @Override
        @SuppressWarnings("deprecation")
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return !url.startsWith(base);   // stay on Azan SIP; nothing else opens in here
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            live.stop();   // (the page reloads by itself after an update)
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (url == null || !url.startsWith(base)) return;
            if (!failed) errorBox.setVisibility(View.GONE);
            if (liveJs != null) view.evaluateJavascript(liveJs, null);
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onReceivedError(WebView view, int code, String description, String failingUrl) {
            // the page itself couldn't load (later on, the page shows by itself when Azan SIP isn't running)
            showError("Can't reach Azan SIP", "Looked for it at " + Net.display(base) + ". Check that the computer is on, "
                    + "Azan SIP is running and “Let TV screens connect” is on in its TV app page.");
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (!request.isForMainFrame()) return;
            if (response.getStatusCode() == 403) {
                showError("TV screens aren't allowed yet", "In Azan SIP's control panel, open TV app and switch on "
                        + "“Let TV screens connect”.");
            } else if (response.getStatusCode() == 404) {
                showError("Azan SIP needs an update", "The TV app needs Azan SIP 1.10.0 or newer. Update it from "
                        + "Settings → Updates in its control panel.");
            }
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // the page crashed or was closed to free memory: start it again rather than closing the app
            handler.post(new Runnable() {
                @Override public void run() {
                    makeWebView();
                    load();
                }
            });
            return true;
        }
    }

    // ------------------------------------------------------------------ menu (Back or Menu)
    @Override
    public void onBackPressed() {
        showMenu();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            showMenu();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private void showMenu() {
        if (failed) {
            changeServer();
            return;
        }
        // ask the page what's on, so the menu can say "Show" or "Hide"
        web.evaluateJavascript("(window.azanApp ? azanApp.state() : '') + (typeof LV === 'object' && LV.on"
                + " ? ',lvs:' + (document.getElementById('video').muted ? 0 : 1) : '')", new ValueCallback<String>() {
            @Override public void onReceiveValue(String value) {
                boolean live = value != null && value.contains("live:1");
                boolean sound = value == null || !value.contains("sound:0");
                boolean liveSound = value != null && value.contains("lvs:1");
                boolean ready = value != null && value.contains("live:");
                openMenu(ready, live, sound, liveSound);
            }
        });
    }

    private static final int LIVE = 1, LIVE_SOUND = 2, SOUND = 3, PLAYER = 4, UPDATE = 5, RELOAD = 6, SERVER = 7, CLOSE = 8;

    private void openMenu(boolean ready, boolean live, boolean sound, boolean liveSound) {
        final List<String> items = new ArrayList<>();
        final List<Integer> acts = new ArrayList<>();
        if (ready) {
            items.add(live ? "Hide Makkah live" : "Show Makkah live");
            acts.add(LIVE);
            if (live) {
                items.add(liveSound ? "Turn the Makkah live sound off" : "Turn the Makkah live sound on");
                acts.add(LIVE_SOUND);
            }
            items.add(sound ? "Keep this screen quiet (no azan sound)" : "Play the azan on this screen");
            acts.add(SOUND);
            items.add(appPlayer() ? "Makkah live: use the page's player instead" : "Makkah live picture black? Use the app's own player");
            acts.add(PLAYER);
        }
        items.add("Update the app");
        acts.add(UPDATE);
        items.add("Reload");
        acts.add(RELOAD);
        items.add("Change server (" + Net.display(base) + ")");
        acts.add(SERVER);
        items.add("Close the app");
        acts.add(CLOSE);
        String version = Updater.version(this);
        new AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
                .setTitle("Azan SIP TV" + (version.isEmpty() ? "" : " " + version))
                .setItems(items.toArray(new String[0]), new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int which) {
                        switch (acts.get(which)) {
                            case LIVE: web.evaluateJavascript("azanApp.toggleLive()", null); break;
                            case LIVE_SOUND: soundClick(); break;
                            case SOUND: web.evaluateJavascript("azanApp.toggleSound()", null); break;
                            case PLAYER: setPlayer(!appPlayer(), true); load(); break;
                            case UPDATE: updater.start(); break;
                            case RELOAD: load(); break;
                            case SERVER: changeServer(); break;
                            default: finishAffinity();
                        }
                    }
                })
                .setOnDismissListener(new DialogInterface.OnDismissListener() {
                    @Override public void onDismiss(DialogInterface d) { hideSystemBars(); }
                })
                .show();
    }

    /** The page's Sound button (under the picture while the app's own player shows it). */
    private void soundClick() {
        web.evaluateJavascript("(function(b){ if (b) b.click(); })(document.getElementById('sound'))", null);
    }

    private void changeServer() {
        handler.removeCallbacks(retry);
        startActivity(new Intent(this, SetupActivity.class).putExtra("change", true));
        finish();
    }

    // ------------------------------------------------------------------ Makkah live: the app's own player
    private boolean appPlayer() {
        return !"page".equals(Net.prefs(this).getString("live_player", "app"));
    }

    /** @param chosen chosen in the menu (then it's never changed by itself) */
    private void setPlayer(boolean app, boolean chosen) {
        Net.prefs(this).edit().putString("live_player", app ? "app" : "page")
                .putBoolean("live_player_set", chosen || Net.prefs(this).getBoolean("live_player_set", false)).apply();
        liveFaults = 0;
    }

    /** True if the page shown is Azan SIP's (only it may use the app's player). */
    private boolean ours() {
        String u = web == null ? null : web.getUrl();
        return u != null && u.startsWith(base);
    }

    private void page(String js) {
        if (web != null && ours()) web.evaluateJavascript(js, null);
    }

    /** What assets/live.js calls. (On the page's thread: each is done on the app's.) */
    private final class Bridge {
        @JavascriptInterface public String mode() {
            return appPlayer() ? "app" : "page";
        }

        @JavascriptInterface public void play(final String url, final boolean relay) {
            if (url == null || !url.matches("(?i)https?://.+")) return;
            handler.post(new Runnable() {
                @Override public void run() {
                    if (!ours() || !appPlayer()) return;
                    liveRelay = relay;
                    live.play(url);
                }
            });
        }

        @JavascriptInterface public void stop() {
            handler.post(new Runnable() {
                @Override public void run() { live.stop(); }
            });
        }

        @JavascriptInterface public void layout(final String json) {
            handler.post(new Runnable() {
                @Override public void run() {
                    if (web != null && ours()) live.layout(json, web.getLeft(), web.getTop(), web.getWidth(), web.getHeight());
                }
            });
        }
    }

    private final class LiveEvents implements LivePlayer.Events {
        @Override public void playing() {
            liveFaults = 0;
            page("window.azanTV && azanTV.playing && azanTV.playing()");
        }

        @Override public void failed(boolean started, String why) {
            if (started) {
                liveFaults = 0;
            } else if (liveRelay && ++liveFaults >= GIVE_UP && !Net.prefs(TvActivity.this).getBoolean("live_player_set", false)) {
                // Azan SIP has the stream, but this TV's player never started it: the page's player, from now on
                setPlayer(false, true);
                Toast.makeText(TvActivity.this, "Makkah live didn't start with the app's own player, so the page's player "
                        + "is used now. Back → Makkah live changes it.", Toast.LENGTH_LONG).show();
                load();
                return;
            }
            page("window.azanTV && azanTV.failed && azanTV.failed(" + started + ", " + JSONObject.quote(why) + ")");
        }
    }

    private String asset(String name) {
        try {
            InputStream in = getAssets().open(name);
            try {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                return out.toString("UTF-8");
            } finally {
                in.close();
            }
        } catch (Exception e) {
            return null;
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == Updater.ASK_INSTALLS) updater.allowed();
    }

    // ------------------------------------------------------------------ full screen
    @SuppressWarnings("deprecation")
    private void hideSystemBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (live != null) live.away(false);
    }

    @Override
    protected void onStop() {
        if (live != null) live.away(true);
        super.onStop();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
            web.resumeTimers();
        }
        hideSystemBars();
    }

    // Not paused in onPause: a screen that shows the prayer times should keep playing the azan even
    // when something briefly covers it (a notification, a dialog, a picture-in-picture app).

    @Override
    protected void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (live != null) live.release();
        if (updater != null) updater.release();
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
