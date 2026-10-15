package com.azansip.tv;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.List;

/** The sign-in screen: asks for the address of the computer running Azan SIP, checks it and
 *  remembers it. After that the app opens straight into the display. */
public class SetupActivity extends Activity {
    private EditText address;
    private EditText name;
    private Button connect;
    private Button search;
    private TextView status;
    private boolean busy;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        boolean change = getIntent().getBooleanExtra("change", false);
        if (!change && Net.server(this) != null) {   // already set up: straight to the display
            openDisplay();
            return;
        }
        getWindow().setStatusBarColor(Ui.BG);
        getWindow().setNavigationBarColor(Ui.BG);
        setContentView(build());
        String server = Net.server(this);
        if (server != null) address.setText(Net.display(server));
        name.setText(Net.name(this));
        address.requestFocus();
    }

    private View build() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Ui.BG);

        FrameLayout outer = new FrameLayout(this);
        scroll.addView(outer, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = Ui.dp(this, 28);
        col.setPadding(pad, pad, pad, pad);
        FrameLayout.LayoutParams colLp = new FrameLayout.LayoutParams(
                Math.min(getResources().getDisplayMetrics().widthPixels, Ui.dp(this, 620)),
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        outer.addView(col, colLp);

        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);
        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher);
        head.addView(logo, new LinearLayout.LayoutParams(Ui.dp(this, 64), Ui.dp(this, 64)));
        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.setPadding(Ui.dp(this, 16), 0, 0, 0);
        titles.addView(Ui.text(this, "Azan SIP TV", 28, Ui.INK, true));
        titles.addView(Ui.text(this, "Prayer times and the azan on this screen", 16, Ui.MUTED, false));
        head.addView(titles);
        col.addView(head);

        col.addView(gap(26));
        col.addView(Ui.text(this, "Server address", 16, Ui.ACCENT, true));
        col.addView(gap(8));
        address = Ui.field(this, "For example 192.168.1.20");
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setImeOptions(EditorInfo.IME_ACTION_NEXT);
        col.addView(address, matchWidth());
        col.addView(gap(6));
        col.addView(Ui.text(this, "The computer running Azan SIP. Its address is on the TV app page of the control panel. "
                + "Add :port if you changed it from 8765.", 14, Ui.MUTED, false));

        col.addView(gap(20));
        col.addView(Ui.text(this, "Screen name (optional)", 16, Ui.ACCENT, true));
        col.addView(gap(8));
        name = Ui.field(this, "For example Main hall");
        name.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_WORDS);
        name.setImeOptions(EditorInfo.IME_ACTION_GO);
        name.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
                if (actionId == EditorInfo.IME_ACTION_GO || actionId == EditorInfo.IME_ACTION_DONE) {
                    connect();
                    return true;
                }
                return false;
            }
        });
        col.addView(name, matchWidth());
        col.addView(gap(6));
        col.addView(Ui.text(this, "Shown in the control panel, so you can tell the screens apart.", 14, Ui.MUTED, false));

        col.addView(gap(24));
        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        connect = Ui.button(this, "Connect", true);
        connect.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { connect(); }
        });
        search = Ui.button(this, "Search the network", false);
        search.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { search(); }
        });
        buttons.addView(connect);
        LinearLayout.LayoutParams second = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        second.leftMargin = Ui.dp(this, 12);
        buttons.addView(search, second);
        col.addView(buttons);

        col.addView(gap(18));
        status = Ui.text(this, "", 16, Ui.MUTED, false);
        col.addView(status, matchWidth());

        col.addView(gap(22));
        TextView help = Ui.text(this, "In Azan SIP's control panel, open TV app and switch on “Let TV screens connect”. "
                + "This device must be on the same network as the computer.", 14, Ui.MUTED, false);
        help.setPadding(Ui.dp(this, 14), Ui.dp(this, 12), Ui.dp(this, 14), Ui.dp(this, 12));
        help.setBackground(Ui.box(this, 0x14FFFFFF, 0x26FFFFFF, 1, 12));
        col.addView(help, matchWidth());
        return scroll;
    }

    private View gap(int dp) {
        View v = new View(this);
        v.setLayoutParams(new LinearLayout.LayoutParams(1, Ui.dp(this, dp)));
        return v;
    }

    private LinearLayout.LayoutParams matchWidth() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private void setBusy(boolean b, String message) {
        busy = b;
        connect.setEnabled(!b);
        search.setEnabled(!b);
        status.setTextColor(Ui.MUTED);
        status.setText(message);
    }

    private void fail(String message) {
        setBusy(false, message);
        status.setTextColor(Ui.BAD);
    }

    private void hideKeyboard() {
        InputMethodManager im = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        if (im != null && getCurrentFocus() != null) im.hideSoftInputFromWindow(getCurrentFocus().getWindowToken(), 0);
    }

    private void connect() {
        if (busy) return;
        final String base = Net.normalize(address.getText().toString());
        if (base == null) {
            fail("Type the computer's address, for example 192.168.1.20.");
            address.requestFocus();
            return;
        }
        hideKeyboard();
        final String screen = name.getText().toString();
        setBusy(true, "Connecting to " + Net.display(base) + "…");
        new Thread(new Runnable() {
            @Override public void run() {
                final Net.Check c = Net.check(base);
                runOnUiThread(new Runnable() {
                    @Override public void run() { connected(base, screen, c); }
                });
            }
        }).start();
    }

    private void connected(String base, String screen, Net.Check c) {
        String where = Net.display(base);
        switch (c.result) {
            case OK:
                Net.save(this, base, screen);
                setBusy(false, "Connected");
                openDisplay();
                return;
            case NOT_ALLOWED:
                fail("Azan SIP is at " + where + ", but TV screens aren't allowed yet. In its control panel, open TV app "
                        + "and switch on “Let TV screens connect”, then press Connect again.");
                return;
            case TOO_OLD:
                fail("Azan SIP at " + where + " is version " + c.version + ". Update it to 1.10.0 or newer "
                        + "(Settings → Updates in its control panel), then press Connect again.");
                return;
            case NOT_AZAN:
                fail("Something answered at " + where + ", but it isn't Azan SIP. Check the address and the port.");
                return;
            default:
                fail("Can't reach Azan SIP at " + where + ". Check the address, that Azan SIP is running, that "
                        + "“Let TV screens connect” is on, and that this device is on the same network.");
        }
    }

    private void search() {
        if (busy) return;
        hideKeyboard();
        if (Net.localAddresses().isEmpty()) {
            fail("This device isn't connected to a network. Connect it to the same Wi-Fi or network as the computer.");
            return;
        }
        setBusy(true, "Searching the network for Azan SIP… (up to 20 seconds)");
        new Thread(new Runnable() {
            @Override public void run() {
                final List<String> found = Net.search();
                runOnUiThread(new Runnable() {
                    @Override public void run() { searched(found); }
                });
            }
        }).start();
    }

    private void searched(List<String> found) {
        if (found.isEmpty()) {
            fail("Azan SIP wasn't found. Check that it's running and that “Let TV screens connect” is on, "
                    + "or type its address. (The search only finds it on the usual port, 8765.)");
            return;
        }
        address.setText(found.get(0));
        StringBuilder msg = new StringBuilder("Found Azan SIP at " + found.get(0) + ".");
        if (found.size() > 1) {
            msg.append(" Also at:");
            for (int i = 1; i < found.size(); i++) msg.append(' ').append(found.get(i));
            msg.append('.');
        }
        msg.append(" Press Connect.");
        setBusy(false, msg.toString());
        connect.requestFocus();
    }

    private void openDisplay() {
        Intent i = new Intent(this, TvActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivity(i);
        finish();
    }
}
