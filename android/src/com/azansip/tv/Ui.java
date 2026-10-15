package com.azansip.tv;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.util.TypedValue;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

/** Colours and controls in the style of the Azan SIP display (Masjid green). They show clearly
 *  which one is selected, for a TV remote. */
final class Ui {
    static final int BG = 0xFF0E4A40;
    static final int CARD = 0xFF135748;
    static final int INK = Color.WHITE;
    static final int MUTED = 0xFFCFE3DC;
    static final int ACCENT = 0xFFF4E3B5;
    static final int ON_ACCENT = 0xFF10332C;
    static final int BAD = 0xFFFFB4AB;

    private Ui() {}

    static int dp(Context c, float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, c.getResources().getDisplayMetrics()));
    }

    static GradientDrawable box(Context c, int fill, int stroke, float strokeDp, float radiusDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(c, radiusDp));
        if (strokeDp > 0) g.setStroke(dp(c, strokeDp), stroke);
        return g;
    }

    static Drawable states(Drawable normal, Drawable focused, Drawable pressed) {
        StateListDrawable s = new StateListDrawable();
        s.addState(new int[]{android.R.attr.state_pressed}, pressed);
        s.addState(new int[]{android.R.attr.state_focused}, focused);
        s.addState(new int[]{}, normal);
        return s;
    }

    static TextView text(Context c, String s, float sp, int color, boolean bold) {
        TextView t = new TextView(c);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setLineSpacing(0, 1.15f);
        return t;
    }

    static Button button(Context c, String label, boolean primary) {
        Button b = new Button(c);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(dp(c, 52));
        b.setPadding(dp(c, 22), dp(c, 10), dp(c, 22), dp(c, 10));
        b.setStateListAnimator(null);
        b.setFocusable(true);
        if (primary) {
            b.setTextColor(ON_ACCENT);
            b.setBackground(states(box(c, ACCENT, ACCENT, 0, 12), box(c, 0xFFFFF4D6, INK, 3, 12), box(c, 0xFFE6D29E, ACCENT, 0, 12)));
        } else {
            b.setTextColor(INK);
            b.setBackground(states(box(c, 0x1FFFFFFF, 0x4DFFFFFF, 1, 12), box(c, 0x40FFFFFF, INK, 3, 12), box(c, 0x33FFFFFF, INK, 1, 12)));
        }
        return b;
    }

    static EditText field(Context c, String hint) {
        EditText e = new EditText(c);
        e.setHint(hint);
        e.setSingleLine(true);
        e.setTextSize(TypedValue.COMPLEX_UNIT_SP, 19);
        e.setTextColor(INK);
        e.setHintTextColor(0x99FFFFFF);
        e.setPadding(dp(c, 16), dp(c, 12), dp(c, 16), dp(c, 12));
        e.setBackground(states(box(c, 0x1AFFFFFF, 0x59FFFFFF, 1, 10), box(c, 0x26FFFFFF, ACCENT, 3, 10), box(c, 0x26FFFFFF, ACCENT, 3, 10)));
        e.setBackgroundTintList((ColorStateList) null);
        return e;
    }
}
