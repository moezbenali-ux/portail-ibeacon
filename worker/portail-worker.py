#!/usr/bin/env python3

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import paho.mqtt.client as mqtt

DB_PATH = os.path.expanduser(os.getenv("DB_PATH", "~/portail/data/users.db"))

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

MQTT_DETECTION_TOPIC  = os.getenv("MQTT_DETECTION_TOPIC",  "parking/beacon/+")
MQTT_TOPIC_ENTREE     = os.getenv("MQTT_TOPIC_ENTREE",     "parking/beacon/entree")
MQTT_TOPIC_SORTIE     = os.getenv("MQTT_TOPIC_SORTIE",     "parking/beacon/sortie")
MQTT_RELAY_TOPIC      = os.getenv("MQTT_RELAY_TOPIC",      "parking/relay/command")
MQTT_STATUS_TOPIC     = os.getenv("MQTT_STATUS_TOPIC",     "portail/status")

DISPLAY_FILE = os.getenv("DISPLAY_FILE", "/tmp/current_display.json")

OPEN_COOLDOWN_SECONDS        = int(os.getenv("OPEN_COOLDOWN_SECONDS",        "10"))
GLOBAL_OPEN_COOLDOWN_SECONDS = int(os.getenv("GLOBAL_OPEN_COOLDOWN_SECONDS", "2"))

MIN_DETECTIONS_TO_OPEN  = int(os.getenv("MIN_DETECTIONS_TO_OPEN",  "2"))
RSSI_WINDOW_SECONDS     = int(os.getenv("RSSI_WINDOW_SECONDS",     "5"))
ABSENCE_TIMEOUT_SECONDS = int(os.getenv("ABSENCE_TIMEOUT_SECONDS", "45"))

MQTT_RECONNECT_DELAY_MIN = int(os.getenv("MQTT_RECONNECT_DELAY_MIN", "1"))
MQTT_RECONNECT_DELAY_MAX = int(os.getenv("MQTT_RECONNECT_DELAY_MAX", "30"))

RSSI_ABSENCE_THRESHOLD = int(os.getenv("RSSI_ABSENCE_THRESHOLD", "-80"))
RSSI_ABSENCE_COUNT     = int(os.getenv("RSSI_ABSENCE_COUNT",     "5"))

CORRELATION_WINDOW_SECONDS = int(os.getenv("CORRELATION_WINDOW_SECONDS", "3"))

RECENT_DETECTIONS: dict = {}
WEAK_DETECTION_COUNT: dict = {}
SIDE_DETECTIONS: dict = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    mac = str(mac).strip().upper().replace("-", ":")
    parts = mac.split(":")
    if len(parts) != 6:
        return None
    try:
        parts = [f"{int(p, 16):02X}" for p in parts]
    except ValueError:
        return None
    return ":".join(parts)


def normalize_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def make_key(uuid=None, major=None, minor=None, mac=None):
    if major is not None and minor is not None:
        return f"beacon|{major}|{minor}"
    mac = normalize_mac(mac)
    if mac:
        return f"mac|{mac}"
    return f"ibeacon|{uuid}|{major}|{minor}"

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ─── Cooldown persistant en base ──────────────────────────────────────────────

def cooldown_get(beacon_key: str) -> float:
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT last_open_ts FROM cooldown_state WHERE beacon_key = ?",
            (beacon_key,)
        )
        row = cur.fetchone()
        conn.close()
        return row["last_open_ts"] if row else 0.0
    except Exception as e:
        logging.error(f"cooldown_get error: {e}")
        return 0.0


def cooldown_set(beacon_key: str, ts: float):
    try:
        conn = db_connect()
        conn.execute(
            """
            INSERT INTO cooldown_state (beacon_key, last_open_ts, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(beacon_key) DO UPDATE
            SET last_open_ts = excluded.last_open_ts,
                updated_at   = CURRENT_TIMESTAMP
            """,
            (beacon_key, ts)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"cooldown_set error: {e}")


def cooldown_reset(beacon_key: str):
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT last_open_ts FROM cooldown_state WHERE beacon_key = ?", (beacon_key,))
        row = cur.fetchone()
        if row:
            conn.execute("DELETE FROM cooldown_state WHERE beacon_key = ?", (beacon_key,))
            conn.commit()
            logging.info(
                f"🔄 Cooldown resetté pour {beacon_key} "
                f"(balise éloignée depuis {RSSI_ABSENCE_COUNT} détections < {RSSI_ABSENCE_THRESHOLD} dBm)"
            )
        conn.close()
    except Exception as e:
        logging.error(f"cooldown_reset error: {e}")


def should_open(beacon_key: str, cooldown_seconds: int) -> bool:
    now = time.time()

    last_global = cooldown_get("__global__")
    if now - last_global < GLOBAL_OPEN_COOLDOWN_SECONDS:
        remaining = round(GLOBAL_OPEN_COOLDOWN_SECONDS - (now - last_global), 1)
        logging.info(f"Cooldown global actif ({remaining}s restantes)")
        return False

    last = cooldown_get(beacon_key)
    if now - last < cooldown_seconds:
        remaining = round(cooldown_seconds - (now - last), 1)
        logging.info(f"Cooldown actif pour {beacon_key} ({remaining}s restantes)")
        return False

    cooldown_set(beacon_key, now)
    cooldown_set("__global__", now)
    return True


# ─── Recherche utilisateur ────────────────────────────────────────────────────

def get_user_from_beacon(uuid=None, major=None, minor=None, mac=None):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()

        if major is not None and minor is not None:
            cur.execute(
                """SELECT id, minor, name, email, active, rssi_threshold, uuid, major, mac
                   FROM users WHERE major = ? AND minor = ? AND active = 1""",
                (int(major), int(minor)),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

        mac = normalize_mac(mac)
        if mac:
            cur.execute(
                """SELECT id, minor, name, email, active, rssi_threshold, uuid, major, mac
                   FROM users WHERE UPPER(mac) = ? AND active = 1""",
                (mac,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

        return None

    except Exception as e:
        logging.error(f"Erreur recherche utilisateur beacon: {e}")
        return None
    finally:
        if conn:
            conn.close()

# ─── Balises inconnues ────────────────────────────────────────────────────────

def save_unknown_beacon(uuid=None, major=None, minor=None, mac=None, rssi=None):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        mac = normalize_mac(mac)

        if mac:
            cur.execute(
                """SELECT id FROM unknown_beacons WHERE UPPER(mac) = ?
                   ORDER BY id DESC LIMIT 1""",
                (mac,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE unknown_beacons
                       SET last_seen = CURRENT_TIMESTAMP, last_rssi = ?,
                           seen_count = seen_count + 1
                       WHERE id = ?""",
                    (rssi, row["id"]),
                )
                conn.commit()
                return row["id"]

        cur.execute(
            """SELECT id FROM unknown_beacons
               WHERE uuid IS ? AND major IS ? AND minor IS ?
               ORDER BY id DESC LIMIT 1""",
            (uuid, major, minor),
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                """UPDATE unknown_beacons
                   SET last_seen = CURRENT_TIMESTAMP, mac = COALESCE(?, mac),
                       last_rssi = ?, seen_count = seen_count + 1
                   WHERE id = ?""",
                (mac, rssi, row["id"]),
            )
            conn.commit()
            return row["id"]

        cur.execute(
            """INSERT INTO unknown_beacons (uuid, major, minor, mac, last_rssi, seen_count)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (uuid, major, minor, mac, rssi),
        )
        conn.commit()
        return cur.lastrowid

    except Exception as e:
        logging.error(f"Erreur save_unknown_beacon: {e}")
        return None
    finally:
        if conn:
            conn.close()


# ─── Événements & présence ────────────────────────────────────────────────────

def log_gate_event(user, payload, event_type, reason):
    conn = None
    try:
        conn = db_connect()
        conn.execute(
            """INSERT INTO gate_events
               (user_id, user_name, uuid, major, minor, mac, rssi, event_type, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user.get("id")   if user else None,
                user.get("name") if user else None,
                payload.get("uuid"),
                normalize_int(payload.get("major")),
                normalize_int(payload.get("minor")),
                normalize_mac(
                    payload.get("mac") or
                    payload.get("device_mac") or
                    payload.get("address")
                ),
                normalize_int(payload.get("rssi")),
                event_type,
                reason,
            ),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"Erreur log_gate_event: {e}")
    finally:
        if conn:
            conn.close()


def update_presence(user, beacon_key, rssi):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, status FROM presence_state WHERE user_id = ?",
            (user["id"],),
        )
        row = cur.fetchone()

        if row:
            if row["status"] != "present":
                conn.execute(
                    """UPDATE presence_state
                       SET beacon_key = ?, status = 'present',
                           last_seen = CURRENT_TIMESTAMP, last_rssi = ?,
                           entered_at = CURRENT_TIMESTAMP
                       WHERE user_id = ?""",
                    (beacon_key, rssi, user["id"]),
                )
                conn.commit()
                log_gate_event(
                    user,
                    {"uuid": user.get("uuid"), "major": user.get("major"),
                     "minor": user.get("minor"), "rssi": rssi, "mac": user.get("mac")},
                    "arrival", "status_present"
                )
            else:
                conn.execute(
                    """UPDATE presence_state
                       SET beacon_key = ?, status = 'present',
                           last_seen = CURRENT_TIMESTAMP, last_rssi = ?
                       WHERE user_id = ?""",
                    (beacon_key, rssi, user["id"]),
                )
                conn.commit()
        else:
            conn.execute(
                """INSERT INTO presence_state
                   (user_id, beacon_key, status, last_seen, last_rssi, entered_at)
                   VALUES (?, ?, 'present', CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)""",
                (user["id"], beacon_key, rssi),
            )
            conn.commit()
            log_gate_event(
                user,
                {"uuid": user.get("uuid"), "major": user.get("major"),
                 "minor": user.get("minor"), "rssi": rssi, "mac": user.get("mac")},
                "arrival", "first_seen"
            )
    except Exception as e:
        logging.error(f"Erreur update_presence: {e}")
    finally:
        if conn:
            conn.close()


def mark_departures():
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            f"""SELECT user_id FROM presence_state
                WHERE status = 'present'
                  AND last_seen < datetime('now', '-{ABSENCE_TIMEOUT_SECONDS} seconds')"""
        )
        rows = cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            cur.execute(
                "SELECT id, name, uuid, major, minor, mac FROM users WHERE id = ?",
                (user_id,)
            )
            user = cur.fetchone()
            conn.execute(
                """UPDATE presence_state
                   SET status = 'absent', exited_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (user_id,),
            )
            if user:
                log_gate_event(
                    dict(user),
                    {"uuid": user["uuid"], "major": user["major"],
                     "minor": user["minor"], "mac": user["mac"], "rssi": None},
                    "departure", "timeout"
                )
        conn.commit()
    except Exception as e:
        logging.error(f"Erreur mark_departures: {e}")
    finally:
        if conn:
            conn.close()


# ─── RSSI ─────────────────────────────────────────────────────────────────────

def remember_detection(beacon_key, rssi):
    now = time.time()
    items = RECENT_DETECTIONS.get(beacon_key, [])
    items.append((now, rssi))
    items = [(ts, v) for ts, v in items if now - ts <= RSSI_WINDOW_SECONDS]
    RECENT_DETECTIONS[beacon_key] = items
    return items


def detection_is_strong_enough(beacon_key, threshold):
    items = RECENT_DETECTIONS.get(beacon_key, [])
    if len(items) < MIN_DETECTIONS_TO_OPEN:
        return False, f"pas assez de détections ({len(items)}/{MIN_DETECTIONS_TO_OPEN})"
    rssis = [r for _, r in items if r is not None]
    if not rssis:
        return False, "aucun RSSI exploitable"
    avg_rssi = sum(rssis) / len(rssis)
    max_rssi = max(rssis)
    if avg_rssi < threshold:
        return False, f"moyenne RSSI insuffisante ({avg_rssi:.1f} < {threshold})"
    if max_rssi < threshold:
        return False, f"pic RSSI insuffisant ({max_rssi} < {threshold})"
    return True, f"ok avg={avg_rssi:.1f} max={max_rssi} n={len(rssis)}"


def check_absence_and_reset_cooldown(beacon_key: str, rssi: int):
    if rssi < RSSI_ABSENCE_THRESHOLD:
        WEAK_DETECTION_COUNT[beacon_key] = WEAK_DETECTION_COUNT.get(beacon_key, 0) + 1
        count = WEAK_DETECTION_COUNT[beacon_key]
        logging.debug(f"Détection faible {beacon_key}: {count}/{RSSI_ABSENCE_COUNT} (RSSI={rssi})")
        if count >= RSSI_ABSENCE_COUNT:
            cooldown_reset(beacon_key)
            WEAK_DETECTION_COUNT[beacon_key] = 0
    else:
        if WEAK_DETECTION_COUNT.get(beacon_key, 0) > 0:
            logging.debug(f"Compteur absence resetté pour {beacon_key} (RSSI={rssi} >= {RSSI_ABSENCE_THRESHOLD})")
        WEAK_DETECTION_COUNT[beacon_key] = 0


def correlate_sides(beacon_key: str, side: str, rssi: int) -> str:
    now = time.time()

    if beacon_key not in SIDE_DETECTIONS:
        SIDE_DETECTIONS[beacon_key] = {}

    SIDE_DETECTIONS[beacon_key][side] = (now, rssi)

    sides = SIDE_DETECTIONS[beacon_key]
    sides = {s: (ts, r) for s, (ts, r) in sides.items()
             if now - ts <= CORRELATION_WINDOW_SECONDS}
    SIDE_DETECTIONS[beacon_key] = sides

    if "entree" in sides and "sortie" in sides:
        rssi_entree = sides["entree"][1]
        rssi_sortie = sides["sortie"][1]
        logging.info(
            f"🔀 Corrélation {beacon_key}: entree={rssi_entree} dBm, sortie={rssi_sortie} dBm"
        )
        return "entree" if rssi_entree >= rssi_sortie else "sortie"

    return "unknown"


# ─── Affichage & ouverture ────────────────────────────────────────────────────

def update_display(name, side="unknown"):  # ✅ AJOUT: paramètre side
    try:
        data = {
            "name": name,
            "side": side,              # ✅ "entree", "sortie", ou "unknown"
            "timestamp": int(time.time())
        }
        with open(DISPLAY_FILE, "w") as f:
            json.dump(data, f)
        logging.info(f"📺 Affichage mis à jour: {name} [{side}]")
    except Exception as e:
        logging.error(f"Erreur update_display: {e}")


def open_gate(client: mqtt.Client, user: dict, payload: dict):
    client.publish(MQTT_RELAY_TOPIC, "OPEN", qos=1, retain=False)
    rssi = payload.get("rssi", "?")
    logging.info(f"🔓 Ouverture portail publiée sur {MQTT_RELAY_TOPIC} pour {user.get('name')} (RSSI: {rssi} dBm)")
    log_gate_event(user, payload, "open", "authorized")

# ─── Traitement détection ─────────────────────────────────────────────────────

def process_detection(client: mqtt.Client, payload: dict, side: str = "unknown"):
    mark_departures()

    uuid  = payload.get("uuid")
    major = normalize_int(payload.get("major"))
    minor = normalize_int(payload.get("minor"))
    mac   = normalize_mac(
        payload.get("mac") or payload.get("device_mac") or payload.get("address")
    )
    rssi  = normalize_int(payload.get("rssi"))

    logging.info(f"Beacon reçu [{side}]: mac={mac}, uuid={uuid}, major={major}, minor={minor}, rssi={rssi}")

    beacon_key = make_key(uuid=uuid, major=major, minor=minor, mac=mac)

    if rssi is not None:
        remember_detection(beacon_key, rssi)

    user = get_user_from_beacon(uuid=uuid, major=major, minor=minor, mac=mac)

    if not user:
        logging.warning(f"Balise inconnue: mac={mac}, uuid={uuid}, major={major}, minor={minor}")
        save_unknown_beacon(uuid=uuid, major=major, minor=minor, mac=mac, rssi=rssi)
        log_gate_event(None, payload, "unknown_beacon", "not_matched")
        return

    logging.info(
        f"Utilisateur autorisé trouvé: {user.get('name')} "
        f"(minor={user.get('minor')}, mac={user.get('mac')}, seuil={user.get('rssi_threshold')})"
    )

    threshold = normalize_int(user.get("rssi_threshold"), default=-70)

    if rssi is None:
        logging.warning(f"RSSI absent pour {user.get('name')}, ouverture refusée")
        log_gate_event(user, payload, "denied_rssi", "missing_rssi")
        return

    # ── Corrélation entrée/sortie ──────────────────────────────────────────────
    dominant_side = correlate_sides(beacon_key, side, rssi)

    if dominant_side == "sortie":
        logging.info(f"🚪 {user.get('name')} sort (RSSI sortie dominant) → reset cooldown")
        cooldown_reset(beacon_key)
        WEAK_DETECTION_COUNT[beacon_key] = 0
        log_gate_event(user, payload, "departure", "sortie_dominant")
        update_presence(user, beacon_key, rssi)
        return

    # ── Logique entrée (ou un seul ESP32 actif) ────────────────────────────────
    check_absence_and_reset_cooldown(beacon_key, rssi)

    is_ok, reason = detection_is_strong_enough(beacon_key, threshold)
    if not is_ok:
        logging.info(f"Filtre RSSI refuse {user.get('name')}: {reason}")
        log_gate_event(user, payload, "denied_rssi", reason)
        update_presence(user, beacon_key, rssi)
        return

    update_presence(user, beacon_key, rssi)

    if not should_open(beacon_key, OPEN_COOLDOWN_SECONDS):
        log_gate_event(user, payload, "cooldown", "cooldown_active")
        return

    # ✅ MODIFIÉ: passer le side dominant à update_display
    display_side = dominant_side if dominant_side != "unknown" else side
    update_display(user.get("name"), side=display_side)
    open_gate(client, user, payload)


# ─── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logging.info("✅ Connecté MQTT")
        client.subscribe(MQTT_TOPIC_ENTREE, qos=1)
        client.subscribe(MQTT_TOPIC_SORTIE, qos=1)
        logging.info(f"📡 Souscriptions: {MQTT_TOPIC_ENTREE}, {MQTT_TOPIC_SORTIE}")
        client.publish(
            MQTT_STATUS_TOPIC,
            json.dumps({"status": "online", "ts": int(time.time())}),
            qos=1, retain=True,
        )
    else:
        logging.error(f"❌ Échec connexion MQTT rc={rc}")


def on_disconnect(client, userdata, rc, properties=None):
    if rc != 0:
        logging.warning(f"⚠️  Déconnexion MQTT inattendue (rc={rc}), reconnexion auto...")
    else:
        logging.info("Déconnexion MQTT propre.")


def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", errors="replace")
        logging.info(f"📥 Message MQTT reçu sur {msg.topic}: {raw}")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            logging.warning("Payload JSON non objet, ignoré")
            return

        if msg.topic == MQTT_TOPIC_ENTREE:
            side = "entree"
        elif msg.topic == MQTT_TOPIC_SORTIE:
            side = "sortie"
        else:
            side = payload.get("side", "unknown")

        process_detection(client, payload, side=side)
    except json.JSONDecodeError:
        logging.error("Payload JSON invalide")
    except Exception as e:
        logging.exception(f"Erreur traitement message MQTT: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    logging.info("🚀 PORTAIL WORKER")
    logging.info(f"📁 Base de données : {DB_PATH}")
    logging.info(f"📡 MQTT {MQTT_HOST}:{MQTT_PORT}")
    logging.info(f"📥 Topics détection : {MQTT_TOPIC_ENTREE}, {MQTT_TOPIC_SORTIE}")
    logging.info(f"🔀 Corrélation      : fenêtre {CORRELATION_WINDOW_SECONDS}s")
    logging.info(f"📤 Topic relais    : {MQTT_RELAY_TOPIC}")
    logging.info(f"🔒 Cooldown        : persistant en base (survie redémarrages)")
    logging.info(f"🔄 Reset cooldown  : {RSSI_ABSENCE_COUNT} détections consécutives < {RSSI_ABSENCE_THRESHOLD} dBm")
    logging.info(f"🔒 Fallback minor  : DÉSACTIVÉ")
    logging.info(f"💾 WAL mode        : ACTIVÉ")
    logging.info(f"🔁 Reconnexion     : {MQTT_RECONNECT_DELAY_MIN}s–{MQTT_RECONNECT_DELAY_MAX}s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.reconnect_delay_set(
        min_delay=MQTT_RECONNECT_DELAY_MIN,
        max_delay=MQTT_RECONNECT_DELAY_MAX,
    )
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
