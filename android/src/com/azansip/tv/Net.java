package com.azansip.tv;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Talking to Azan SIP: the saved server, checking an address, and finding it on the network. */
final class Net {
    static final int PORT = 8765;
    /** The first Azan SIP with the TV app (/tv). */
    static final int[] NEEDS = {1, 10, 0};

    enum Result { OK, NOT_AZAN, NOT_ALLOWED, TOO_OLD, UNREACHABLE }

    static final class Check {
        final Result result;
        final String version;
        Check(Result result, String version) { this.result = result; this.version = version; }
    }

    private Net() {}

    // ---------------------------------------------------------------- saved settings
    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("azan", Context.MODE_PRIVATE);
    }

    static String server(Context c) {
        return prefs(c).getString("server", null);
    }

    static String name(Context c) {
        return prefs(c).getString("name", "");
    }

    static void save(Context c, String server, String name) {
        prefs(c).edit().putString("server", server).putString("name", name == null ? "" : name.trim()).apply();
    }

    // ---------------------------------------------------------------- addresses
    /** "192.168.1.20", "192.168.1.20:8765" or "http://pc-name:8765/tv" -> "http://192.168.1.20:8765" (null if unusable). */
    static String normalize(String text) {
        if (text == null) return null;
        String s = text.trim();
        s = s.replaceFirst("(?i)^https?://", "");
        int slash = s.indexOf('/');
        if (slash >= 0) s = s.substring(0, slash);
        if (s.isEmpty()) return null;
        String host = s;
        int port = PORT;
        int colon = s.lastIndexOf(':');
        if (colon > 0 && s.indexOf(':') == colon) {
            host = s.substring(0, colon);
            try {
                port = Integer.parseInt(s.substring(colon + 1));
            } catch (NumberFormatException e) {
                return null;
            }
            if (port < 1 || port > 65535) return null;
        }
        if (!host.matches("[A-Za-z0-9.\\-]+")) return null;
        return "http://" + host + ":" + port;
    }

    /** "http://192.168.1.20:8765" -> "192.168.1.20" (the port is left out when it's the usual one). */
    static String display(String base) {
        if (base == null) return "";
        String s = base.replaceFirst("^http://", "");
        return s.endsWith(":" + PORT) ? s.substring(0, s.length() - (":" + PORT).length()) : s;
    }

    // ---------------------------------------------------------------- checking
    static Check check(String base) {
        HttpURLConnection con = null;
        try {
            con = (HttpURLConnection) new URL(base + "/api/ping").openConnection();
            con.setConnectTimeout(4000);
            con.setReadTimeout(4000);
            con.setUseCaches(false);
            int code = con.getResponseCode();
            if (code == 403) return new Check(Result.NOT_ALLOWED, null);
            if (code != 200) return new Check(Result.NOT_AZAN, null);
            String body = read(con.getInputStream(), 4096);
            if (!body.contains("\"azan-sip\"")) return new Check(Result.NOT_AZAN, null);
            Matcher m = Pattern.compile("\"version\"\\s*:\\s*\"([0-9.]+)\"").matcher(body);
            String version = m.find() ? m.group(1) : "0";
            return new Check(atLeast(version, NEEDS) ? Result.OK : Result.TOO_OLD, version);
        } catch (Exception e) {
            return new Check(Result.UNREACHABLE, null);
        } finally {
            if (con != null) con.disconnect();
        }
    }

    static boolean atLeast(String version, int[] want) {
        String[] p = version.split("\\.");
        for (int i = 0; i < want.length; i++) {
            int v = 0;
            if (i < p.length) {
                try { v = Integer.parseInt(p[i]); } catch (NumberFormatException e) { v = 0; }
            }
            if (v != want[i]) return v > want[i];
        }
        return true;
    }

    private static String read(InputStream in, int limit) throws java.io.IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int n;
            while ((n = in.read(buf)) > 0 && out.size() < limit) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } finally {
            in.close();
        }
    }

    // ---------------------------------------------------------------- searching the network
    /** This device's own addresses on the local network (Wi-Fi or cable). */
    static List<Inet4Address> localAddresses() {
        List<Inet4Address> out = new ArrayList<>();
        try {
            Enumeration<NetworkInterface> all = NetworkInterface.getNetworkInterfaces();
            if (all == null) return out;
            for (NetworkInterface ni : Collections.list(all)) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) out.add((Inet4Address) a);
                }
            }
        } catch (Exception ignored) {
            // no network
        }
        return out;
    }

    /** Looks for Azan SIP on every address next to this device's (x.y.z.1 to x.y.z.254). */
    static List<String> search() {
        final Set<String> found = Collections.synchronizedSet(new LinkedHashSet<String>());
        ExecutorService pool = Executors.newFixedThreadPool(48);
        Set<String> mine = new LinkedHashSet<>();
        Set<String> prefixes = new LinkedHashSet<>();
        for (Inet4Address a : localAddresses()) {
            String ip = a.getHostAddress();
            mine.add(ip);
            prefixes.add(ip.substring(0, ip.lastIndexOf('.') + 1));
        }
        for (String prefix : prefixes) {
            for (int i = 1; i < 255; i++) {
                final String ip = prefix + i;
                pool.execute(new Runnable() {
                    @Override public void run() {
                        Socket s = new Socket();
                        try {
                            s.connect(new InetSocketAddress(ip, PORT), 500);
                        } catch (Exception e) {
                            return;
                        } finally {
                            try { s.close(); } catch (Exception ignored) { }
                        }
                        Check c = check("http://" + ip + ":" + PORT);
                        if (c.result != Result.NOT_AZAN && c.result != Result.UNREACHABLE) found.add(ip);
                    }
                });
            }
        }
        pool.shutdown();
        try {
            pool.awaitTermination(20, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        pool.shutdownNow();
        List<String> list = new ArrayList<>(found);
        list.removeAll(mine);
        return list;
    }
}
