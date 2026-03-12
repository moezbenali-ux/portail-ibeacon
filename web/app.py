#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import sqlite3
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.expanduser("~/portail/data/users.db")
DISPLAY_FILE = "/tmp/current_display.json"

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode : lectures simultanées sans bloquer les écritures du worker
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@app.route("/")
def index():
    if os.path.exists(os.path.join(BASE_DIR, "index.html")):
        return send_from_directory(BASE_DIR, "index.html")
    return "index.html introuvable", 404


@app.route("/admin")
def admin_page():
    if os.path.exists(os.path.join(BASE_DIR, "admin.html")):
        return send_from_directory(BASE_DIR, "admin.html")
    return "admin.html introuvable", 404


@app.route("/radar.html")
def radar():
    if os.path.exists(os.path.join(BASE_DIR, "radar.html")):
        return send_from_directory(BASE_DIR, "radar.html")
    return "radar.html introuvable", 404


@app.route("/scan_beacons.html")
def scan_beacons():
    if os.path.exists(os.path.join(BASE_DIR, "scan_beacons.html")):
        return send_from_directory(BASE_DIR, "scan_beacons.html")
    return "scan_beacons.html introuvable", 404


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/display")
def api_display():
    try:
        if not os.path.exists(DISPLAY_FILE):
            return jsonify({"name": "En attente", "timestamp": None})

        with open(DISPLAY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return jsonify({
            "name": data.get("name", "En attente"),
            "timestamp": data.get("timestamp")
        })
    except Exception as e:
        return jsonify({
            "error": str(e),
            "name": "En attente",
            "timestamp": None
        }), 500


@app.route("/current")
def current():
    return api_display()


@app.route("/api/users")
def api_users():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            name,
            email,
            active,
            rssi_threshold,
            uuid,
            major,
            minor,
            mac
        FROM users
        ORDER BY name COLLATE NOCASE ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/users", methods=["POST"])
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

    if major == "":
        major = None
    if minor == "":
        minor = None

    if not name:
        return jsonify({"error": "name obligatoire"}), 400

    conn = db_connect()
    cur = conn.cursor()

    if mac:
        cur.execute("""
            SELECT id, name
            FROM users
            WHERE UPPER(mac) = UPPER(?)
        """, (mac,))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"MAC déjà utilisée par {row['name']} (id={row['id']})"}), 409

    if uuid and major is not None and minor is not None:
        cur.execute("""
            SELECT id, name
            FROM users
            WHERE uuid = ? AND major = ? AND minor = ?
        """, (uuid, major, minor))
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
def api_user(user_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            name,
            email,
            active,
            rssi_threshold,
            uuid,
            major,
            minor,
            mac
        FROM users
        WHERE id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "utilisateur introuvable"}), 404

    return jsonify(dict(row))


@app.route("/api/users/<int:user_id>", methods=["POST", "PUT"])
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

    if major == "":
        major = None
    if minor == "":
        minor = None

    if not name:
        return jsonify({"error": "name obligatoire"}), 400

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "utilisateur introuvable"}), 404

    if mac:
        cur.execute("""
            SELECT id, name
            FROM users
            WHERE UPPER(mac) = UPPER(?)
              AND id <> ?
        """, (mac, user_id))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"MAC déjà utilisée par {row['name']} (id={row['id']})"}), 409

    if uuid and major is not None and minor is not None:
        cur.execute("""
            SELECT id, name
            FROM users
            WHERE uuid = ? AND major = ? AND minor = ?
              AND id <> ?
        """, (uuid, major, minor, user_id))
        row = cur.fetchone()
        if row:
            conn.close()
            return jsonify({"error": f"Triplet beacon déjà utilisé par {row['name']} (id={row['id']})"}), 409

    cur.execute("""
        UPDATE users
        SET name = ?,
            email = ?,
            active = ?,
            rssi_threshold = ?,
            uuid = ?,
            major = ?,
            minor = ?,
            mac = ?
        WHERE id = ?
    """, (name, email, active, rssi_threshold, uuid, major, minor, mac, user_id))

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "id": user_id})


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
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

    return jsonify({
        "ok": True,
        "deleted_id": user_id,
        "deleted_name": row["name"]
    })


@app.route("/api/users/check-duplicates", methods=["POST"])
def api_check_duplicates():
    data = request.get_json(silent=True) or {}

    user_id = data.get("id")
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    mac = (data.get("mac") or "").strip()
    uuid = (data.get("uuid") or "").strip()
    major = data.get("major")
    minor = data.get("minor")

    conn = db_connect()
    cur = conn.cursor()

    duplicates = {
        "name": [],
        "email": [],
        "mac": [],
        "triplet": []
    }

    if name:
        if user_id:
            cur.execute("""
                SELECT id, name, email
                FROM users
                WHERE UPPER(name) = UPPER(?)
                  AND id <> ?
            """, (name, user_id))
        else:
            cur.execute("""
                SELECT id, name, email
                FROM users
                WHERE UPPER(name) = UPPER(?)
            """, (name,))
        duplicates["name"] = [dict(r) for r in cur.fetchall()]

    if email:
        if user_id:
            cur.execute("""
                SELECT id, name, email
                FROM users
                WHERE UPPER(email) = UPPER(?)
                  AND id <> ?
            """, (email, user_id))
        else:
            cur.execute("""
                SELECT id, name, email
                FROM users
                WHERE UPPER(email) = UPPER(?)
            """, (email,))
        duplicates["email"] = [dict(r) for r in cur.fetchall()]

    if mac:
        if user_id:
            cur.execute("""
                SELECT id, name, mac
                FROM users
                WHERE UPPER(mac) = UPPER(?)
                  AND id <> ?
            """, (mac, user_id))
        else:
            cur.execute("""
                SELECT id, name, mac
                FROM users
                WHERE UPPER(mac) = UPPER(?)
            """, (mac,))
        duplicates["mac"] = [dict(r) for r in cur.fetchall()]

    if uuid and major is not None and minor is not None:
        if user_id:
            cur.execute("""
                SELECT id, name, uuid, major, minor
                FROM users
                WHERE uuid = ?
                  AND major = ?
                  AND minor = ?
                  AND id <> ?
            """, (uuid, major, minor, user_id))
        else:
            cur.execute("""
                SELECT id, name, uuid, major, minor
                FROM users
                WHERE uuid = ?
                  AND major = ?
                  AND minor = ?
            """, (uuid, major, minor))
        duplicates["triplet"] = [dict(r) for r in cur.fetchall()]

    conn.close()

    blocking = bool(duplicates["mac"] or duplicates["triplet"])

    return jsonify({
        "duplicates": duplicates,
        "blocking": blocking
    })


@app.route("/api/unknown-beacons")
def api_unknown_beacons():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            datetime(first_seen) AS first_seen,
            datetime(last_seen) AS last_seen,
            uuid,
            major,
            minor,
            mac,
            last_rssi,
            seen_count,
            assigned_user_id,
            notes
        FROM unknown_beacons
        WHERE assigned_user_id IS NULL
        ORDER BY last_seen DESC
        LIMIT 100
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/unknown-beacons/<int:beacon_id>")
def api_unknown_beacon(beacon_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            datetime(first_seen) AS first_seen,
            datetime(last_seen) AS last_seen,
            uuid,
            major,
            minor,
            mac,
            last_rssi,
            seen_count,
            assigned_user_id,
            notes
        FROM unknown_beacons
        WHERE id = ?
    """, (beacon_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "balise introuvable"}), 404

    return jsonify(dict(row))


@app.route("/api/unknown-beacons/<int:beacon_id>/assign", methods=["POST"])
def api_assign_unknown_beacon(beacon_id):
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "user_id manquant"}), 400

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, uuid, major, minor, mac
        FROM unknown_beacons
        WHERE id = ?
    """, (beacon_id,))
    beacon = cur.fetchone()

    if not beacon:
        conn.close()
        return jsonify({"error": "balise inconnue introuvable"}), 404

    cur.execute("""
        SELECT id, name
        FROM users
        WHERE id = ?
    """, (user_id,))
    user = cur.fetchone()

    if not user:
        conn.close()
        return jsonify({"error": "utilisateur introuvable"}), 404

    uuid = beacon["uuid"]
    major = beacon["major"]
    minor = beacon["minor"]
    mac = beacon["mac"]

    if uuid is not None and major is not None and minor is not None:
        cur.execute("""
            UPDATE users
            SET uuid = NULL, major = NULL, minor = NULL
            WHERE id <> ?
              AND uuid = ?
              AND major = ?
              AND minor = ?
        """, (user_id, uuid, major, minor))

    if mac:
        cur.execute("""
            UPDATE users
            SET mac = NULL
            WHERE id <> ?
              AND UPPER(mac) = UPPER(?)
        """, (user_id, mac))

    cur.execute("""
        UPDATE users
        SET uuid = ?, major = ?, minor = ?, mac = COALESCE(?, mac)
        WHERE id = ?
    """, (uuid, major, minor, mac, user_id))

    cur.execute("""
        UPDATE unknown_beacons
        SET assigned_user_id = ?
        WHERE id = ?
    """, (user_id, beacon_id))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "beacon_id": beacon_id,
        "user_id": user_id
    })


@app.route("/api/beacons/recent")
def api_beacons_recent():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            ub.id,
            datetime(ub.last_seen) AS last_seen,
            ub.uuid,
            ub.major,
            ub.minor,
            ub.mac,
            ub.last_rssi,
            ub.seen_count,
            u.id AS user_id,
            u.name AS user_name,
            u.active AS user_active
        FROM unknown_beacons ub
        LEFT JOIN users u
          ON (
                (ub.uuid IS NOT NULL AND ub.uuid = u.uuid AND ub.major = u.major AND ub.minor = u.minor)
             OR (ub.mac IS NOT NULL AND UPPER(ub.mac) = UPPER(u.mac))
             OR (ub.minor IS NOT NULL AND ub.minor = u.minor)
          )
        ORDER BY ub.last_seen DESC
        LIMIT 100
    """)

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/stats")
def api_stats():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM users WHERE active = 1")
    active_users = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) AS c
        FROM unknown_beacons
        WHERE assigned_user_id IS NULL
    """)
    unknown_beacons = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) AS c
        FROM gate_events
        WHERE date(ts) = date('now')
    """)
    events_today = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) AS c
        FROM gate_events
        WHERE event_type = 'open'
          AND date(ts) = date('now')
    """)
    opens_today = cur.fetchone()["c"]

    conn.close()

    return jsonify({
        "active_users": active_users,
        "unknown_beacons": unknown_beacons,
        "events_today": events_today,
        "opens_today": opens_today
    })


@app.route("/api/gate-events")
def api_gate_events():
    limit = request.args.get("limit", default=50, type=int)
    limit = max(1, min(limit, 500))

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            datetime(ts) AS ts,
            user_id,
            user_name,
            uuid,
            major,
            minor,
            mac,
            rssi,
            event_type,
            reason
        FROM gate_events
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return jsonify(rows)


@app.route("/api/presence")
def api_presence():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            p.user_id,
            u.name,
            p.beacon_key,
            p.status,
            p.last_seen,
            p.last_rssi,
            p.entered_at,
            p.exited_at
        FROM presence_state p
        LEFT JOIN users u ON u.id = p.user_id
        ORDER BY u.name COLLATE NOCASE ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
