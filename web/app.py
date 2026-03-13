#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from functools import wraps

from flask import Flask, jsonify, redirect, request, send_from_directory, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.expanduser("~/portail/data/users.db")
DISPLAY_FILE = "/tmp/current_display.json"

# ── Auth config ────────────────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv(
    "ADMIN_PASSWORD_HASH",
    hashlib.sha256("portail2026!".encode()).hexdigest()
)
SESSION_LIFETIME_HOURS = int(os.getenv("SESSION_LIFETIME_HOURS", "8"))
DISPLAY_TOKEN = os.getenv("DISPLAY_TOKEN", "431189e19d626eb94f9199dd21ba8ad7efc4b63140ce7b445c3358353476b72a")

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "changez-moi-en-production-svp")
app.permanent_session_lifetime = timedelta(hours=SESSION_LIFETIME_HOURS)


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ── Décorateur auth ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Non authentifié", "redirect": "/login"}), 401
            return redirect(f"/login?next={request.path}")
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authenticated"):
        return redirect("/admin")
    return send_from_directory(os.path.join(BASE_DIR, "templates"), "login.html")


@app.route("/login", methods=["POST"])
def login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    if username == ADMIN_USERNAME and password_hash == ADMIN_PASSWORD_HASH:
        session.permanent = True
        session["authenticated"] = True
        session["username"] = username
        next_url = request.args.get("next", "/admin")
        return jsonify({"ok": True, "redirect": next_url})
    else:
        return jsonify({"ok": False, "error": "Identifiants incorrects"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── Pages publiques ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if os.path.exists(os.path.join(BASE_DIR, "index.html")):
        return send_from_directory(BASE_DIR, "index.html")
    return "index.html introuvable", 404


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/display")
def api_display():
    token = request.headers.get("X-Display-Token", "")
    if token != DISPLAY_TOKEN:
        return jsonify({"error": "Non autorisé"}), 401
    try:
        if not os.path.exists(DISPLAY_FILE):
            return jsonify({"name": "En attente", "timestamp": None})
        with open(DISPLAY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({
            "name": data.get("name", "En attente"),
            "side": data.get("side", "unknown"),
            "timestamp": data.get("timestamp")
        })
    except Exception as e:
        return jsonify({"error": str(e), "name": "En attente", "timestamp": None}), 500


@app.route("/current")
def current():
    return api_display()

# ── Pages protégées ────────────────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin_page():
    if os.path.exists(os.path.join(BASE_DIR, "admin.html")):
        return send_from_directory(BASE_DIR, "admin.html")
    return "admin.html introuvable", 404


@app.route("/radar.html")
@login_required
def radar():
    if os.path.exists(os.path.join(BASE_DIR, "radar.html")):
        return send_from_directory(BASE_DIR, "radar.html")
    return "radar.html introuvable", 404


@app.route("/scan_beacons.html")
@login_required
def scan_beacons():
    if os.path.exists(os.path.join(BASE_DIR, "scan_beacons.html")):
        return send_from_directory(BASE_DIR, "scan_beacons.html")
    return "scan_beacons.html introuvable", 404


@app.route("/insights")
@login_required
def insights():
    return send_from_directory(os.path.join(BASE_DIR, "templates"), "insights.html")


# ── API protégées ──────────────────────────────────────────────────────────────
@app.route("/api/users")
@login_required
def api_users():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, active, rssi_threshold, uuid, major, minor, mac
        FROM users
        WHERE active = 1
        ORDER BY name COLLATE NOCASE
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/users", methods=["POST"])
@login_required
def api_create_user():
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip() or None
    active = int(data.get("active", 1))
    rssi_threshold = int(data.get("rssi_threshold", -70))
    uuid = (data.get("uuid") or "").strip() or None
    major = data.get("major")
    minor = data.get("minor")
    mac = (data.get("mac") or "").strip() or None

    if major == "": major = None
    if minor == "": minor = None
    if not name:
        return jsonify({"error": "name obligatoire"}), 400

    conn = db_connect()
    cur = conn.cursor()

    if mac:
        cur.execute("SELECT id, name FROM users WHERE UPPER(mac) = UPPER(?)", (mac,))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"MAC déjà utilisée par {row['name']} (id={row['id']})"}), 409

    if uuid and major is not None and minor is not None:
        cur.execute("SELECT id, name FROM users WHERE uuid = ? AND major = ? AND minor = ?", (uuid, major, minor))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"Triplet beacon déjà utilisé par {row['name']} (id={row['id']})"}), 409

    cur.execute("""
        INSERT INTO users (name, email, active, rssi_threshold, uuid, major, minor, mac)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, email, active, rssi_threshold, uuid, major, minor, mac))

    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": user_id})


@app.route("/api/users/<int:user_id>")
@login_required
def api_user(user_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, active, rssi_threshold, uuid, major, minor, mac
        FROM users WHERE id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "utilisateur introuvable"}), 404
    return jsonify(dict(row))


@app.route("/api/users/<int:user_id>", methods=["POST", "PUT"])
@login_required
def api_update_user(user_id):
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip() or None
    active = int(data.get("active", 1))
    rssi_threshold = int(data.get("rssi_threshold", -70))
    uuid = (data.get("uuid") or "").strip() or None
    major = data.get("major")
    minor = data.get("minor")
    mac = (data.get("mac") or "").strip() or None

    if major == "": major = None
    if minor == "": minor = None
    if not name:
        return jsonify({"error": "name obligatoire"}), 400

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "utilisateur introuvable"}), 404

    if mac:
        cur.execute("SELECT id, name FROM users WHERE UPPER(mac) = UPPER(?) AND id <> ?", (mac, user_id))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"MAC déjà utilisée par {row['name']} (id={row['id']})"}), 409

    if uuid and major is not None and minor is not None:
        cur.execute("SELECT id, name FROM users WHERE uuid = ? AND major = ? AND minor = ? AND id <> ?", (uuid, major, minor, user_id))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"Triplet beacon déjà utilisé par {row['name']} (id={row['id']})"}), 409

    cur.execute("""
        UPDATE users
        SET name=?, email=?, active=?, rssi_threshold=?, uuid=?, major=?, minor=?, mac=?
        WHERE id=?
    """, (name, email, active, rssi_threshold, uuid, major, minor, mac, user_id))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": user_id})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@login_required
def api_delete_user(user_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "utilisateur introuvable"}), 404

    cur.execute("DELETE FROM presence_state WHERE user_id = ?", (user_id,))
    cur.execute("UPDATE unknown_beacons SET assigned_user_id = NULL WHERE assigned_user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted_id": user_id, "deleted_name": row["name"]})


@app.route("/api/users/check-duplicates", methods=["POST"])
@login_required
def api_check_duplicates():
    data = request.get_json(silent=True) or {}
    user_id = data.get("id")
    name    = (data.get("name") or "").strip()
    email   = (data.get("email") or "").strip()
    mac     = (data.get("mac") or "").strip()
    uuid    = (data.get("uuid") or "").strip()
    major   = data.get("major")
    minor   = data.get("minor")

    conn = db_connect()
    cur = conn.cursor()
    duplicates = {"name": [], "email": [], "mac": [], "triplet": []}

    if name:
        q = "SELECT id, name, email FROM users WHERE UPPER(name) = UPPER(?)"
        p = [name]
        if user_id: q += " AND id <> ?"; p.append(user_id)
        cur.execute(q, p); duplicates["name"] = [dict(r) for r in cur.fetchall()]

    if email:
        q = "SELECT id, name, email FROM users WHERE UPPER(email) = UPPER(?)"
        p = [email]
        if user_id: q += " AND id <> ?"; p.append(user_id)
        cur.execute(q, p); duplicates["email"] = [dict(r) for r in cur.fetchall()]

    if mac:
        q = "SELECT id, name, mac FROM users WHERE UPPER(mac) = UPPER(?)"
        p = [mac]
        if user_id: q += " AND id <> ?"; p.append(user_id)
        cur.execute(q, p); duplicates["mac"] = [dict(r) for r in cur.fetchall()]

    if uuid and major is not None and minor is not None:
        q = "SELECT id, name, uuid, major, minor FROM users WHERE uuid=? AND major=? AND minor=?"
        p = [uuid, major, minor]
        if user_id: q += " AND id <> ?"; p.append(user_id)
        cur.execute(q, p); duplicates["triplet"] = [dict(r) for r in cur.fetchall()]

    conn.close()
    return jsonify({"duplicates": duplicates, "blocking": bool(duplicates["mac"] or duplicates["triplet"])})


@app.route("/api/unknown-beacons")
@login_required
def api_unknown_beacons():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, datetime(first_seen) AS first_seen, datetime(last_seen) AS last_seen,
               uuid, major, minor, mac, last_rssi, seen_count, assigned_user_id, notes
        FROM unknown_beacons
        WHERE assigned_user_id IS NULL
        ORDER BY last_seen DESC LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/unknown-beacons/<int:beacon_id>")
@login_required
def api_unknown_beacon(beacon_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, datetime(first_seen) AS first_seen, datetime(last_seen) AS last_seen,
               uuid, major, minor, mac, last_rssi, seen_count, assigned_user_id, notes
        FROM unknown_beacons WHERE id = ?
    """, (beacon_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "balise introuvable"}), 404
    return jsonify(dict(row))


@app.route("/api/unknown-beacons/<int:beacon_id>/assign", methods=["POST"])
@login_required
def api_assign_unknown_beacon(beacon_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id manquant"}), 400

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id, uuid, major, minor, mac FROM unknown_beacons WHERE id = ?", (beacon_id,))
    beacon = cur.fetchone()
    if not beacon:
        conn.close()
        return jsonify({"error": "balise inconnue introuvable"}), 404

    cur.execute("SELECT id, name FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({"error": "utilisateur introuvable"}), 404

    uuid, major, minor, mac = beacon["uuid"], beacon["major"], beacon["minor"], beacon["mac"]

    if uuid is not None and major is not None and minor is not None:
        cur.execute("UPDATE users SET uuid=NULL, major=NULL, minor=NULL WHERE id<>? AND uuid=? AND major=? AND minor=?", (user_id, uuid, major, minor))
    if mac:
        cur.execute("UPDATE users SET mac=NULL WHERE id<>? AND UPPER(mac)=UPPER(?)", (user_id, mac))

    cur.execute("UPDATE users SET uuid=?, major=?, minor=?, mac=COALESCE(?,mac) WHERE id=?", (uuid, major, minor, mac, user_id))
    cur.execute("UPDATE unknown_beacons SET assigned_user_id=? WHERE id=?", (user_id, beacon_id))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "beacon_id": beacon_id, "user_id": user_id})


@app.route("/api/beacons/recent")
@login_required
def api_beacons_recent():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT ub.id, datetime(ub.last_seen) AS last_seen,
               ub.uuid, ub.major, ub.minor, ub.mac, ub.last_rssi, ub.seen_count,
               u.id AS user_id, u.name AS user_name, u.active AS user_active
        FROM unknown_beacons ub
        LEFT JOIN users u ON (
            (ub.uuid IS NOT NULL AND ub.uuid=u.uuid AND ub.major=u.major AND ub.minor=u.minor)
            OR (ub.mac IS NOT NULL AND UPPER(ub.mac)=UPPER(u.mac))
            OR (ub.minor IS NOT NULL AND ub.minor=u.minor)
        )
        ORDER BY ub.last_seen DESC LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/stats")
@login_required
def api_stats():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE active=1")
    active_users = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM unknown_beacons WHERE assigned_user_id IS NULL")
    unknown_beacons = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM gate_events WHERE date(ts)=date('now')")
    events_today = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM gate_events WHERE event_type='open' AND date(ts)=date('now')")
    opens_today = cur.fetchone()["c"]
    conn.close()
    return jsonify({"active_users": active_users, "unknown_beacons": unknown_beacons,
                    "events_today": events_today, "opens_today": opens_today})


@app.route("/api/gate-events")
@login_required
def api_gate_events():
    limit = max(1, min(request.args.get("limit", default=50, type=int), 500))
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, datetime(ts) AS ts, user_id, user_name, uuid, major, minor, mac,
               rssi, event_type, reason
        FROM gate_events ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/presence")
@login_required
def api_presence():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.user_id, u.name, p.beacon_key, p.status, p.last_seen,
               p.last_rssi, p.entered_at, p.exited_at
        FROM presence_state p
        LEFT JOIN users u ON u.id=p.user_id
        ORDER BY u.name COLLATE NOCASE ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


# ── OTA Firmware (public — accès ESP32) ───────────────────────────────────────
OTA_DIR = os.path.join(BASE_DIR, "ota")

@app.route("/ota/<filename>")
def ota_firmware(filename):
    if not filename.endswith(".bin"):
        return jsonify({"error": "Fichier non autorisé"}), 403
    if not os.path.exists(os.path.join(OTA_DIR, filename)):
        return jsonify({"error": "Firmware introuvable"}), 404
    return send_from_directory(OTA_DIR, filename, mimetype="application/octet-stream")


@app.route("/api/ota/status")
@login_required
def api_ota_status():
    if not os.path.exists(OTA_DIR):
        return jsonify([])
    files = []
    for f in os.listdir(OTA_DIR):
        if f.endswith(".bin"):
            path = os.path.join(OTA_DIR, f)
            files.append({"filename": f, "size": os.path.getsize(path),
                          "modified": int(os.path.getmtime(path))})
    return jsonify(files)


# ── Insights ───────────────────────────────────────────────────────────────────
@app.route("/api/insights")
@login_required
def api_insights():
    from_date = request.args.get("from", "")
    to_date   = request.args.get("to", "")
    user      = request.args.get("user", "")
    event     = request.args.get("event", "")

    try:
        conn = db_connect()
        cur  = conn.cursor()

        if event == "sortie":
            simple_query = "SELECT id, name, NULL AS entree_time, rssi, timestamp AS sortie_time, NULL AS duration_min FROM access_log WHERE event='sortie'"
            sp = []
            if from_date: simple_query += " AND timestamp >= ?"; sp.append(from_date)
            if to_date:   simple_query += " AND timestamp <= ?"; sp.append(to_date)
            if user:      simple_query += " AND name = ?";       sp.append(user)
            simple_query += " ORDER BY sortie_time DESC"
        else:
            simple_query = """
                SELECT e.id, e.name, e.timestamp AS entree_time, e.rssi,
                    (SELECT s.timestamp FROM access_log s
                     WHERE s.user_id=e.user_id AND s.event='sortie' AND s.timestamp>e.timestamp
                     ORDER BY s.timestamp ASC LIMIT 1) AS sortie_time,
                    ROUND((julianday(
                        (SELECT s2.timestamp FROM access_log s2
                         WHERE s2.user_id=e.user_id AND s2.event='sortie' AND s2.timestamp>e.timestamp
                         ORDER BY s2.timestamp ASC LIMIT 1)
                    ) - julianday(e.timestamp)) * 1440, 1) AS duration_min
                FROM access_log e
                WHERE e.event='entree'
            """
            sp = []
            if from_date: simple_query += " AND e.timestamp >= ?"; sp.append(from_date)
            if to_date:   simple_query += " AND e.timestamp <= ?"; sp.append(to_date)
            if user:      simple_query += " AND e.name = ?";       sp.append(user)
            simple_query += " ORDER BY entree_time DESC"

        cur.execute(simple_query, sp)
        rows = [dict(r) for r in cur.fetchall()]

        stats_query = """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN event='entree' THEN 1 ELSE 0 END) AS entrees,
                   SUM(CASE WHEN event='sortie' THEN 1 ELSE 0 END) AS sorties,
                   COUNT(DISTINCT user_id) AS users
            FROM access_log WHERE 1=1
        """
        sp2 = []
        if from_date: stats_query += " AND timestamp >= ?"; sp2.append(from_date)
        if to_date:   stats_query += " AND timestamp <= ?"; sp2.append(to_date)
        if user:      stats_query += " AND name = ?";       sp2.append(user)

        cur.execute(stats_query, sp2)
        stats = dict(cur.fetchone())
        conn.close()
        return jsonify({"rows": rows, "stats": stats})

    except Exception as e:
        app.logger.error(f"api_insights error: {e}")
        return jsonify({"error": str(e), "rows": [], "stats": {}}), 500


@app.route("/api/insights/users")
@login_required
def api_insights_users():
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT u.id, u.name
            FROM users u
            INNER JOIN access_log a ON a.user_id=u.id
            WHERE u.active=1
            ORDER BY u.name
        """)
        users = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e), "users": []}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
