package local.navbutton;

import android.accessibilityservice.AccessibilityService;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.LayerDrawable;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;

/**
 * A floating round button, like iPhone's AssistiveTouch: tap goes back, a long
 * press goes home, dragging moves it. The phone has no on-screen navigation
 * bar and no hardware back key. An accessibility service is the only
 * unprivileged way to press Back and Home, and its overlay window type needs
 * no draw-over-apps permission.
 */
public class NavService extends AccessibilityService {
    private static final long LONG_PRESS_MS = 450;
    private static final int SIZE_DP = 56;

    private WindowManager wm;
    private View button;
    private WindowManager.LayoutParams lp;
    private final Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onServiceConnected() {
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        DisplayMetrics dm = getResources().getDisplayMetrics();
        int size = Math.round(SIZE_DP * dm.density);

        GradientDrawable outer = new GradientDrawable();
        outer.setShape(GradientDrawable.OVAL);
        outer.setColor(Color.argb(150, 20, 20, 20));
        GradientDrawable ring = new GradientDrawable();
        ring.setShape(GradientDrawable.OVAL);
        ring.setColor(Color.argb(60, 255, 255, 255));
        ring.setStroke(Math.round(3 * dm.density), Color.argb(200, 255, 255, 255));
        LayerDrawable face = new LayerDrawable(new GradientDrawable[] {outer, ring});
        int inset = Math.round(12 * dm.density);
        face.setLayerInset(1, inset, inset, inset, inset);

        button = new View(this);
        button.setBackground(face);
        button.setContentDescription("Back. Long press for Home.");

        SharedPreferences prefs = getSharedPreferences("pos", MODE_PRIVATE);
        lp = new WindowManager.LayoutParams(
                size, size,
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.x = prefs.getInt("x", dm.widthPixels - size - Math.round(8 * dm.density));
        lp.y = prefs.getInt("y", dm.heightPixels * 2 / 3);
        button.setOnTouchListener(new Drag(ViewConfiguration.get(this).getScaledTouchSlop()));
        wm.addView(button, lp);
    }

    private class Drag implements View.OnTouchListener {
        private final int slop;
        private float downX, downY;
        private int startX, startY;
        private boolean moved, longFired;
        private final Runnable longPress = new Runnable() {
            @Override
            public void run() {
                if (!moved) {
                    longFired = true;
                    button.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS);
                    performGlobalAction(GLOBAL_ACTION_HOME);
                }
            }
        };

        Drag(int slop) {
            this.slop = slop;
        }

        @Override
        public boolean onTouch(View v, MotionEvent e) {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    downX = e.getRawX();
                    downY = e.getRawY();
                    startX = lp.x;
                    startY = lp.y;
                    moved = false;
                    longFired = false;
                    v.setAlpha(0.6f);
                    handler.postDelayed(longPress, LONG_PRESS_MS);
                    return true;
                case MotionEvent.ACTION_MOVE:
                    float dx = e.getRawX() - downX;
                    float dy = e.getRawY() - downY;
                    if (!moved && Math.hypot(dx, dy) > slop) {
                        moved = true;
                        handler.removeCallbacks(longPress);
                    }
                    if (moved) {
                        lp.x = startX + Math.round(dx);
                        lp.y = startY + Math.round(dy);
                        wm.updateViewLayout(button, lp);
                    }
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    handler.removeCallbacks(longPress);
                    v.setAlpha(1f);
                    if (moved) {
                        getSharedPreferences("pos", MODE_PRIVATE).edit()
                                .putInt("x", lp.x).putInt("y", lp.y).apply();
                    } else if (!longFired && e.getActionMasked() == MotionEvent.ACTION_UP) {
                        performGlobalAction(GLOBAL_ACTION_BACK);
                    }
                    return true;
                default:
                    return false;
            }
        }
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // The button only acts on touches; no events are read.
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onDestroy() {
        if (button != null && wm != null) {
            wm.removeView(button);
        }
        super.onDestroy();
    }
}
