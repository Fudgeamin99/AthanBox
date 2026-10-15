package com.azansip.tv;

import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.text.SpannableString;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.VideoView;

import org.json.JSONObject;

/** Makkah live with Android's own video player ("the app's own player"). Some TVs play the stream
 *  inside the page with a black picture; Android's player draws straight to the screen instead.
 *  The page still decides when it plays, from which link and whether it's quiet (assets/live.js),
 *  and says where its player is; the picture is shown there, over the page, with the page's
 *  "LIVE · Makkah" badge and Sound button copied on top. */
final class LivePlayer {
    /** What the page hears back. */
    interface Events {
        void playing();
        /** @param started whether this link had shown a picture; @param why for the page's message */
        void failed(boolean started, String why);
    }

    private static final long NO_START_MS = 45000;   // as the page's own player
    private static final long FROZEN_MS = 30000;
    private static final long CHECK_MS = 5000;
    private static final int DOT = 2;

    private final Events events;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final FrameLayout box;
    private final VideoView video;
    private final TextView badge;
    private final TextView chip;

    private MediaPlayer mp;
    private String url;
    private boolean started;
    private boolean muted = true;
    private long since;
    private long movedAt;
    private int lastPos = -1;
    private boolean away;

    private final Runnable check = new Runnable() {
        @Override public void run() { check(); }
    };

    LivePlayer(Context c, Events events, View.OnClickListener onSound) {
        this.events = events;
        box = new FrameLayout(c);
        box.setBackgroundColor(Color.BLACK);
        video = new VideoView(c);
        // the remote stays with the page (VideoView takes the focus by itself)
        video.setFocusable(false);
        video.setFocusableInTouchMode(false);
        if (Build.VERSION.SDK_INT >= 26) video.setAudioFocusRequest(AudioManager.AUDIOFOCUS_NONE);   // the azan plays alongside
        video.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
            @Override public void onPrepared(MediaPlayer p) {
                if (url == null) {   // (Android reopens the last link when the picture comes back)
                    handler.post(new Runnable() {
                        @Override public void run() { if (url == null) video.stopPlayback(); }
                    });
                    return;
                }
                mp = p;
                volume();
                video.start();   // also after the picture was away (the app in the background)
            }
        });
        video.setOnInfoListener(new MediaPlayer.OnInfoListener() {
            @Override public boolean onInfo(MediaPlayer p, int what, int extra) {
                if (what == MediaPlayer.MEDIA_INFO_VIDEO_RENDERING_START) started();
                return false;
            }
        });
        video.setOnErrorListener(new MediaPlayer.OnErrorListener() {
            @Override public boolean onError(MediaPlayer p, int what, int extra) {
                fail(started ? "The connection to the stream dropped." : "It didn't start.");
                return true;   // (no "Can't play this video" box)
            }
        });
        video.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
            @Override public void onCompletion(MediaPlayer p) { fail("The stream ended."); }
        });
        box.addView(video, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        badge = new TextView(c);
        badge.setTextColor(Color.WHITE);
        badge.setTypeface(Typeface.DEFAULT_BOLD);
        badge.setSingleLine(true);
        box.addView(badge, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP | Gravity.START));
        chip = new TextView(c);
        chip.setTextColor(Color.WHITE);
        chip.setTypeface(Typeface.DEFAULT_BOLD);
        chip.setSingleLine(true);
        chip.setOnClickListener(onSound);   // (a touch screen: the page's Sound button is under the picture)
        chip.setVisibility(View.GONE);
        box.addView(chip, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM | Gravity.END));
        hide();
    }

    /** The view that goes over the page. */
    View view() {
        return box;
    }

    void play(String link) {
        stop();
        url = link;
        started = false;
        since = movedAt = System.currentTimeMillis();
        lastPos = -1;
        video.setVideoURI(Uri.parse(link));
        if (!away) handler.postDelayed(check, CHECK_MS);
    }

    /** The app is out of sight (the TV asleep, another app in front): Android drops the picture, so the
     *  checks wait; when it's back, Android's player opens the link again and the checks start afresh. */
    void away(boolean gone) {
        if (gone == away) return;
        away = gone;
        handler.removeCallbacks(check);
        if (!gone && url != null) {
            since = movedAt = System.currentTimeMillis();
            lastPos = -1;
            handler.postDelayed(check, CHECK_MS);
        }
    }

    void stop() {
        handler.removeCallbacks(check);
        if (url != null) video.stopPlayback();
        url = null;
        mp = null;
        started = false;
        hide();
    }

    /** Where the page's player is, in the page's pixels (scaled to this screen's with its width). */
    void layout(String json, int left, int top, int webWidth, int webHeight) {
        JSONObject j;
        try {
            j = new JSONObject(json);
        } catch (Exception e) {
            return;
        }
        boolean quiet = j.optBoolean("muted", true);
        if (quiet != muted) {
            muted = quiet;
            volume();
        }
        double vw = j.optDouble("vw", 0);
        if (!j.optBoolean("show") || url == null || !started || vw <= 0 || webWidth <= 0) {
            hide();
            return;
        }
        double s = webWidth / vw;
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) box.getLayoutParams();
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.leftMargin = left + (int) Math.round(j.optDouble("x") * s);
        lp.topMargin = top + (int) Math.round(j.optDouble("y") * s);
        lp.width = Math.max(1, (int) Math.round(j.optDouble("w") * s));
        lp.height = Math.max(1, (int) Math.round(j.optDouble("h") * s));
        box.setLayoutParams(lp);
        copy(badge, j.optJSONObject("badge"), s, true);
        copy(chip, j.optJSONObject("sound"), s, false);
    }

    void release() {
        stop();
        handler.removeCallbacksAndMessages(null);
    }

    // ---------------------------------------------------------------- inside

    private void started() {
        if (url == null || started) return;
        started = true;
        movedAt = System.currentTimeMillis();
        events.playing();
    }

    private void fail(String why) {
        if (url == null) return;
        boolean was = started;
        stop();
        events.failed(was, why);
    }

    /** Every few seconds: started yet, and still moving? (a TV runs for days) */
    private void check() {
        if (url == null || away) return;
        long now = System.currentTimeMillis();
        int pos = -1;
        boolean on = false;
        try {
            pos = video.getCurrentPosition();
            on = video.isPlaying();
        } catch (Exception ignored) {
            // not ready yet
        }
        if (pos != lastPos) {
            if (lastPos >= 0 && pos > lastPos) movedAt = now;
            lastPos = pos;
        } else if (pos <= 0 && on) {
            movedAt = now;   // (some TVs always say 0 for a live stream: playing will do)
        }
        if (!started && on && pos > 1000) started();   // (some TVs never say when the picture starts)
        if (!started && now - since > NO_START_MS) {
            fail("It didn't start.");
            return;
        }
        if (started && now - movedAt > FROZEN_MS) {
            fail("It froze.");
            return;
        }
        handler.postDelayed(check, CHECK_MS);
    }

    private void volume() {
        if (mp == null) return;
        try {
            float v = muted ? 0f : 1f;
            mp.setVolume(v, v);
        } catch (Exception ignored) {
            // released meanwhile
        }
    }

    /** Out of sight: a dot in the corner, so Android's player keeps its picture surface and goes on playing. */
    private void hide() {
        badge.setVisibility(View.GONE);
        chip.setVisibility(View.GONE);
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) box.getLayoutParams();
        if (lp == null) {
            box.setLayoutParams(new FrameLayout.LayoutParams(DOT, DOT, Gravity.TOP | Gravity.START));
        } else if (lp.width != DOT || lp.height != DOT || lp.leftMargin != 0 || lp.topMargin != 0) {
            lp.width = lp.height = DOT;
            lp.leftMargin = lp.topMargin = 0;
            box.setLayoutParams(lp);
        }
    }

    /** The page's badge or Sound button, drawn the same over the picture: {text, x, y, size} with
     *  x and y from the player's top left (the badge) or bottom right (the button), in the page's pixels. */
    private void copy(TextView t, JSONObject o, double s, boolean dot) {
        if (o == null || o.optString("text").isEmpty()) {
            t.setVisibility(View.GONE);
            return;
        }
        float px = (float) (o.optDouble("size", 12) * s);
        String text = o.optString("text");
        CharSequence shown = text;
        if (dot) {
            SpannableString sp = new SpannableString("●  " + text);
            sp.setSpan(new ForegroundColorSpan(0xFFE5484D), 0, 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            shown = sp;
        }
        if (!shown.toString().equals(t.getText().toString())) t.setText(shown);
        t.setTextSize(TypedValue.COMPLEX_UNIT_PX, px);
        int padV = Math.round(px * (dot ? 0.42f : 0.63f));
        int padH = Math.round(px * (dot ? 0.84f : 0.95f));
        t.setPadding(padH, padV, padH, padV);
        GradientDrawable g = new GradientDrawable();
        g.setColor(0x8C000000);
        g.setCornerRadius(dot ? px * 3 : px * 0.63f);
        if (!dot) g.setStroke(Math.max(1, Math.round((float) s)), 0x40FFFFFF);
        t.setBackground(g);
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) t.getLayoutParams();
        int x = (int) Math.round(o.optDouble("x") * s), y = (int) Math.round(o.optDouble("y") * s);
        if (dot) {
            lp.leftMargin = x;
            lp.topMargin = y;
        } else {
            lp.rightMargin = x;
            lp.bottomMargin = y;
        }
        t.setLayoutParams(lp);
        t.setVisibility(View.VISIBLE);
    }
}
