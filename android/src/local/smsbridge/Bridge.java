package local.smsbridge;

import android.Manifest;
import android.app.Activity;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Bundle;
import android.telephony.SmsManager;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Read-only view of received SMS for the USB shell.
 * content://smsbridge/sms?after=ID returns one column "j": base64 JSON per message with _id > ID.
 * call(method="send", arg=number, extras b=base64 UTF-8 body) sends an SMS and returns once the
 * radio reports every part sent.
 * Base64 keeps bodies with commas, quotes and newlines intact through the adb shell.
 */
public class Bridge extends ContentProvider {
    private static final String[] FIELDS = {"_id", "address", "date", "body"};
    private static final long SEND_TIMEOUT_SECONDS = 60;

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        String after = uri.getQueryParameter("after");
        long afterId = after == null ? 0 : Long.parseLong(after);
        MatrixCursor out = new MatrixCursor(new String[] {"j"});
        try (Cursor c = getContext().getContentResolver().query(
                Uri.parse("content://sms/inbox"), FIELDS, "_id > ?",
                new String[] {Long.toString(afterId)}, "_id ASC")) {
            if (c == null) throw new IllegalStateException("sms provider returned null cursor");
            while (c.moveToNext()) {
                JSONObject o = new JSONObject();
                o.put("id", c.getLong(0));
                o.put("address", c.getString(1));
                o.put("date", c.getLong(2));
                o.put("body", c.getString(3));
                byte[] bytes = o.toString().getBytes(StandardCharsets.UTF_8);
                out.addRow(new Object[] {Base64.encodeToString(bytes, Base64.NO_WRAP)});
            }
        } catch (JSONException e) {
            throw new IllegalStateException("json encode failed", e);
        }
        return out;
    }

    /** Provider permissions do not cover call(), so the shell-only check is enforced here. */
    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        if (getContext().checkCallingPermission(Manifest.permission.DUMP)
                != PackageManager.PERMISSION_GRANTED) {
            throw new SecurityException("caller lacks DUMP");
        }
        if (!"send".equals(method)) throw new IllegalArgumentException("unknown method " + method);
        if (arg == null || extras == null || extras.getString("b") == null) {
            throw new IllegalArgumentException("send needs arg=number and extra b=base64 body");
        }
        String body = new String(Base64.decode(extras.getString("b"), Base64.DEFAULT), StandardCharsets.UTF_8);
        send(arg, body);
        return new Bundle();
    }

    private void send(String to, String body) {
        Context ctx = getContext();
        SmsManager sms = SmsManager.getDefault();
        ArrayList<String> parts = sms.divideMessage(body);
        String action = "local.smsbridge.SENT." + UUID.randomUUID();
        CountDownLatch latch = new CountDownLatch(parts.size());
        List<Integer> failures = new ArrayList<>();
        BroadcastReceiver receiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent intent) {
                if (getResultCode() != Activity.RESULT_OK) {
                    synchronized (failures) {
                        failures.add(getResultCode());
                    }
                }
                latch.countDown();
            }
        };
        ctx.registerReceiver(receiver, new IntentFilter(action));
        try {
            ArrayList<PendingIntent> sent = new ArrayList<>();
            for (int i = 0; i < parts.size(); i++) {
                Intent intent = new Intent(action).setPackage(ctx.getPackageName());
                sent.add(PendingIntent.getBroadcast(ctx, i, intent, PendingIntent.FLAG_ONE_SHOT));
            }
            sms.sendMultipartTextMessage(to, null, parts, sent, null);
            if (!latch.await(SEND_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
                throw new IllegalStateException("no send result within " + SEND_TIMEOUT_SECONDS + "s");
            }
            if (!failures.isEmpty()) {
                throw new IllegalStateException("send failed, SmsManager result codes " + failures);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted while sending", e);
        } finally {
            ctx.unregisterReceiver(receiver);
        }
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        throw new UnsupportedOperationException("read-only");
    }
}
