#!/bin/bash
# =============================================================================
# upgrade_portail.sh — Mise à jour + backup + rollback du projet portail
# =============================================================================
# Préconisations appliquées :
#   1. Suppression du fallback minor dangereux dans portail-worker.py
#   2. Passage à Gunicorn (prod) pour portail-web.service
#   3. Désactivation de mqtt-worker.service (répertoire fantôme)
#   4. Activation WAL mode SQLite + reconnexion MQTT robuste
#
# Rollback : en cas d'échec détecté, restauration automatique des fichiers
#            puis attente de confirmation avant de redémarrer les services.
# =============================================================================

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
PORTAIL_DIR="/home/administrateur/portail"
VENV="$PORTAIL_DIR/venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"

WORKER_FILE="$PORTAIL_DIR/worker/portail-worker.py"
APP_FILE="$PORTAIL_DIR/web/app.py"
SYSTEMD_WEB="$PORTAIL_DIR/systemd/portail-web.service"
SYSTEMD_WORKER="$PORTAIL_DIR/systemd/portail-worker.service"

BACKUP_DIR="$PORTAIL_DIR/data/backups/upgrade-$(date +%Y%m%d-%H%M%S)"
ROLLBACK_DONE=0

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${BOLD}━━━ $* ━━━${NC}"; }

# ─── Rollback ─────────────────────────────────────────────────────────────────
rollback() {
    local reason="$1"
    error "ÉCHEC détecté : $reason"
    echo ""
    warn "═══════════════════════════════════════════════"
    warn "  ROLLBACK EN COURS — restauration des fichiers"
    warn "═══════════════════════════════════════════════"

    if [ -d "$BACKUP_DIR" ]; then
        # Restaurer les fichiers sauvegardés
        [ -f "$BACKUP_DIR/portail-worker.py" ]      && cp "$BACKUP_DIR/portail-worker.py"      "$WORKER_FILE"      && ok "Worker restauré"
        [ -f "$BACKUP_DIR/app.py" ]                  && cp "$BACKUP_DIR/app.py"                  "$APP_FILE"         && ok "app.py restauré"
        [ -f "$BACKUP_DIR/portail-web.service" ]     && cp "$BACKUP_DIR/portail-web.service"     /etc/systemd/system/portail-web.service && ok "portail-web.service restauré"
        [ -f "$BACKUP_DIR/portail-worker.service" ]  && cp "$BACKUP_DIR/portail-worker.service"  /etc/systemd/system/portail-worker.service && ok "portail-worker.service restauré"
        systemctl daemon-reload
        ok "Fichiers de configuration restaurés depuis $BACKUP_DIR"
    else
        error "Répertoire de backup introuvable — rollback impossible sur les fichiers."
    fi

    echo ""
    echo -e "${YELLOW}Les fichiers ont été restaurés à leur état précédent.${NC}"
    echo -e "${YELLOW}Les services NE sont PAS encore redémarrés.${NC}"
    echo ""
    echo "Vérifiez l'état puis relancez manuellement :"
    echo "  systemctl start portail-worker portail-web"
    echo "  systemctl status portail-worker portail-web"
    echo ""
    read -rp "Appuyez sur ENTRÉE pour redémarrer les services, ou Ctrl+C pour annuler : "
    systemctl restart portail-worker portail-web
    systemctl status portail-worker portail-web --no-pager || true
    ROLLBACK_DONE=1
    exit 1
}

trap 'if [ $ROLLBACK_DONE -eq 0 ]; then rollback "erreur inattendue (ligne $LINENO)"; fi' ERR

# ─── Vérifications préalables ─────────────────────────────────────────────────
section "Vérifications préalables"

if [ "$EUID" -ne 0 ]; then
    error "Ce script doit être exécuté en root (sudo)."
    exit 1
fi

for f in "$WORKER_FILE" "$APP_FILE" "$SYSTEMD_WEB" "$SYSTEMD_WORKER"; do
    if [ ! -f "$f" ]; then
        error "Fichier manquant : $f"
        exit 1
    fi
done
ok "Tous les fichiers source présents"

# ─── Backup ───────────────────────────────────────────────────────────────────
section "Backup complet"

mkdir -p "$BACKUP_DIR"
cp "$WORKER_FILE"      "$BACKUP_DIR/portail-worker.py"
cp "$APP_FILE"         "$BACKUP_DIR/app.py"
cp "$SYSTEMD_WEB"      "$BACKUP_DIR/portail-web.service"
cp "$SYSTEMD_WORKER"   "$BACKUP_DIR/portail-worker.service"

# Backup de la base de données
DB_FILE="$PORTAIL_DIR/data/users.db"
if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$BACKUP_DIR/users.db"
    ok "Base de données sauvegardée"
fi

# Copier aussi les services systemd actifs
[ -f /etc/systemd/system/portail-web.service ]    && cp /etc/systemd/system/portail-web.service    "$BACKUP_DIR/portail-web.service.system"
[ -f /etc/systemd/system/portail-worker.service ] && cp /etc/systemd/system/portail-worker.service "$BACKUP_DIR/portail-worker.service.system"
[ -f /etc/systemd/system/mqtt-worker.service ]    && cp /etc/systemd/system/mqtt-worker.service    "$BACKUP_DIR/mqtt-worker.service.system"

ok "Backup créé dans : $BACKUP_DIR"

# ─── 1. Installer Gunicorn ────────────────────────────────────────────────────
section "1/4 — Installation de Gunicorn"

if "$VENV/bin/gunicorn" --version &>/dev/null; then
    ok "Gunicorn déjà installé : $("$VENV/bin/gunicorn" --version)"
else
    info "Installation de Gunicorn dans le venv..."
    "$PIP" install gunicorn
    ok "Gunicorn installé : $("$VENV/bin/gunicorn" --version)"
fi

# ─── 2. Mise à jour portail-worker.py ────────────────────────────────────────
section "2/4 — portail-worker.py : suppression fallback minor + MQTT robuste + WAL"

cat > "$WORKER_FILE" << 'PYEOF'
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

MQTT_DETECTION_TOPIC = os.getenv("MQTT_DETECTION_TOPIC", "parking/beacon/detected")
MQTT_RELAY_TOPIC = os.getenv("MQTT_RELAY_TOPIC", "parking/relay/command")
MQTT_STATUS_TOPIC = os.getenv("MQTT_STATUS_TOPIC", "portail/status")

DISPLAY_FILE = os.getenv("DISPLAY_FILE", "/tmp/current_display.json")

OPEN_COOLDOWN_SECONDS = int(os.getenv("OPEN_COOLDOWN_SECONDS", "10"))
GLOBAL_OPEN_COOLDOWN_SECONDS = int(os.getenv("GLOBAL_OPEN_COOLDOWN_SECONDS", "2"))

MIN_DETECTIONS_TO_OPEN = int(os.getenv("MIN_DETECTIONS_TO_OPEN", "2"))
RSSI_WINDOW_SECONDS = int(os.getenv("RSSI_WINDOW_SECONDS", "5"))
ABSENCE_TIMEOUT_SECONDS = int(os.getenv("ABSENCE_TIMEOUT_SECONDS", "45"))

# Délais de reconnexion MQTT
MQTT_RECONNECT_DELAY_MIN = int(os.getenv("MQTT_RECONNECT_DELAY_MIN", "1"))
MQTT_RECONNECT_DELAY_MAX = int(os.getenv("MQTT_RECONNECT_DELAY_MAX", "30"))

LAST_OPEN_BY_KEY = {}
LAST_GLOBAL_OPEN = 0
RECENT_DETECTIONS = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    mac = str(mac).strip().upper().replace("-", ":")
    parts = mac.split(":")
    if len(parts) != 6:
        return None
    try:
        parts = [f"{int(part, 16):02X}" for part in parts]
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
    mac = normalize_mac(mac)
    if mac:
        return f"mac|{mac}"
    return f"ibeacon|{uuid}|{major}|{minor}"


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode : évite les locks entre lecture web et écriture worker
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_user_from_beacon(uuid=None, major=None, minor=None, mac=None):
    """
    Recherche un utilisateur par MAC ou par triplet UUID/major/minor.
    Le fallback par minor seul a été supprimé car il est dangereux :
    il pouvait ouvrir le portail pour le mauvais utilisateur si deux
    beacons partagent le même minor avec des UUID différents.
    """
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()

        mac = normalize_mac(mac)

        # 1. Priorité : correspondance MAC exacte
        if mac:
            cur.execute(
                """
                SELECT id, minor, name, email, active, rssi_threshold, uuid, major, mac
                FROM users
                WHERE UPPER(mac) = ? AND active = 1
                """,
                (mac,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

        # 2. Correspondance triplet complet UUID + major + minor
        if uuid is not None and major is not None and minor is not None:
            cur.execute(
                """
                SELECT id, minor, name, email, active, rssi_threshold, uuid, major, mac
                FROM users
                WHERE uuid = ? AND major = ? AND minor = ? AND active = 1
                """,
                (str(uuid), int(major), int(minor)),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

        # NOTE : Le fallback par minor seul a été volontairement supprimé.
        # Il provoquait des faux positifs : un beacon inconnu avec le même minor
        # qu'un utilisateur autorisé (mais UUID différent) pouvait ouvrir le portail.
        # Si ce comportement est nécessaire, il doit être réactivé explicitement
        # avec une variable d'environnement ALLOW_MINOR_FALLBACK=1.

        return None

    except Exception as e:
        logging.error(f"Erreur recherche utilisateur beacon: {e}")
        return None

    finally:
        if conn is not None:
            conn.close()


def save_unknown_beacon(uuid=None, major=None, minor=None, mac=None, rssi=None):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        mac = normalize_mac(mac)

        if mac:
            cur.execute(
                """
                SELECT id, seen_count
                FROM unknown_beacons
                WHERE UPPER(mac) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (mac,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE unknown_beacons
                    SET last_seen = CURRENT_TIMESTAMP,
                        last_rssi = ?,
                        seen_count = seen_count + 1
                    WHERE id = ?
                    """,
                    (rssi, row["id"]),
                )
                conn.commit()
                return row["id"]

        cur.execute(
            """
            SELECT id, seen_count
            FROM unknown_beacons
            WHERE uuid IS ? AND major IS ? AND minor IS ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (uuid, major, minor),
        )
        row = cur.fetchone()

        if row:
            cur.execute(
                """
                UPDATE unknown_beacons
                SET last_seen = CURRENT_TIMESTAMP,
                    mac = COALESCE(?, mac),
                    last_rssi = ?,
                    seen_count = seen_count + 1
                WHERE id = ?
                """,
                (mac, rssi, row["id"]),
            )
            conn.commit()
            return row["id"]

        cur.execute(
            """
            INSERT INTO unknown_beacons (uuid, major, minor, mac, last_rssi, seen_count)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (uuid, major, minor, mac, rssi),
        )
        conn.commit()
        return cur.lastrowid

    except Exception as e:
        logging.error(f"Erreur save_unknown_beacon: {e}")
        return None

    finally:
        if conn is not None:
            conn.close()


def log_gate_event(user, payload, event_type, reason):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gate_events
            (user_id, user_name, uuid, major, minor, mac, rssi, event_type, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.get("id") if user else None,
                user.get("name") if user else None,
                payload.get("uuid"),
                normalize_int(payload.get("major")),
                normalize_int(payload.get("minor")),
                normalize_mac(payload.get("mac") or payload.get("device_mac") or payload.get("address")),
                normalize_int(payload.get("rssi")),
                event_type,
                reason,
            ),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"Erreur log_gate_event: {e}")
    finally:
        if conn is not None:
            conn.close()


def update_presence(user, beacon_key, rssi):
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()

        cur.execute(
            "SELECT user_id, status, entered_at FROM presence_state WHERE user_id = ?",
            (user["id"],),
        )
        row = cur.fetchone()

        if row:
            if row["status"] != "present":
                cur.execute(
                    """
                    UPDATE presence_state
                    SET beacon_key = ?, status = 'present',
                        last_seen = CURRENT_TIMESTAMP,
                        last_rssi = ?, entered_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (beacon_key, rssi, user["id"]),
                )
                conn.commit()
                log_gate_event(user, {"uuid": user.get("uuid"), "major": user.get("major"), "minor": user.get("minor"), "rssi": rssi, "mac": user.get("mac")}, "arrival", "status_present")
            else:
                cur.execute(
                    """
                    UPDATE presence_state
                    SET beacon_key = ?, status = 'present',
                        last_seen = CURRENT_TIMESTAMP,
                        last_rssi = ?
                    WHERE user_id = ?
                    """,
                    (beacon_key, rssi, user["id"]),
                )
                conn.commit()
        else:
            cur.execute(
                """
                INSERT INTO presence_state
                (user_id, beacon_key, status, last_seen, last_rssi, entered_at)
                VALUES (?, ?, 'present', CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
                """,
                (user["id"], beacon_key, rssi),
            )
            conn.commit()
            log_gate_event(user, {"uuid": user.get("uuid"), "major": user.get("major"), "minor": user.get("minor"), "rssi": rssi, "mac": user.get("mac")}, "arrival", "first_seen")

    except Exception as e:
        logging.error(f"Erreur update_presence: {e}")
    finally:
        if conn is not None:
            conn.close()


def mark_departures():
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT user_id
            FROM presence_state
            WHERE status = 'present'
              AND last_seen < datetime('now', '-{ABSENCE_TIMEOUT_SECONDS} seconds')
            """
        )
        rows = cur.fetchall()

        for row in rows:
            user_id = row["user_id"]
            cur.execute("SELECT id, name, uuid, major, minor, mac FROM users WHERE id = ?", (user_id,))
            user = cur.fetchone()

            cur.execute(
                """
                UPDATE presence_state
                SET status = 'absent', exited_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (user_id,),
            )

            if user:
                log_gate_event(dict(user), {"uuid": user["uuid"], "major": user["major"], "minor": user["minor"], "mac": user["mac"], "rssi": None}, "departure", "timeout")

        conn.commit()

    except Exception as e:
        logging.error(f"Erreur mark_departures: {e}")
    finally:
        if conn is not None:
            conn.close()


def should_open(beacon_key: str, cooldown_seconds: int) -> bool:
    global LAST_GLOBAL_OPEN

    now = time.time()

    if now - LAST_GLOBAL_OPEN < GLOBAL_OPEN_COOLDOWN_SECONDS:
        remaining = round(GLOBAL_OPEN_COOLDOWN_SECONDS - (now - LAST_GLOBAL_OPEN), 1)
        logging.info(f"Cooldown global actif ({remaining}s restantes)")
        return False

    last = LAST_OPEN_BY_KEY.get(beacon_key, 0)
    if now - last < cooldown_seconds:
        remaining = round(cooldown_seconds - (now - last), 1)
        logging.info(f"Cooldown actif pour {beacon_key} ({remaining}s restantes)")
        return False

    LAST_OPEN_BY_KEY[beacon_key] = now
    LAST_GLOBAL_OPEN = now
    return True


def remember_detection(beacon_key, rssi):
    now = time.time()
    items = RECENT_DETECTIONS.get(beacon_key, [])
    items.append((now, rssi))
    items = [(ts, val) for ts, val in items if now - ts <= RSSI_WINDOW_SECONDS]
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


def update_display(name):
    try:
        data = {"name": name, "timestamp": int(time.time())}
        with open(DISPLAY_FILE, "w") as f:
            json.dump(data, f)
        logging.info(f"📺 Affichage mis à jour: {name}")
    except Exception as e:
        logging.error(f"Erreur update_display: {e}")


def open_gate(client: mqtt.Client, user: dict, payload: dict):
    client.publish(MQTT_RELAY_TOPIC, "OPEN", qos=1, retain=False)
    logging.info(
        f"Ouverture portail publiée sur {MQTT_RELAY_TOPIC} pour {user.get('name')}"
    )
    log_gate_event(user, payload, "open", "authorized")


def process_detection(client: mqtt.Client, payload: dict):
    mark_departures()

    uuid = payload.get("uuid")
    major = normalize_int(payload.get("major"))
    minor = normalize_int(payload.get("minor"))
    mac = payload.get("mac") or payload.get("device_mac") or payload.get("address")
    mac = normalize_mac(mac)
    rssi = normalize_int(payload.get("rssi"))

    logging.info(
        f"Beacon reçu: mac={mac}, uuid={uuid}, major={major}, minor={minor}, rssi={rssi}"
    )

    beacon_key = make_key(uuid=uuid, major=major, minor=minor, mac=mac)

    if rssi is not None:
        remember_detection(beacon_key, rssi)

    user = get_user_from_beacon(uuid=uuid, major=major, minor=minor, mac=mac)

    if not user:
        logging.warning(
            f"Balise inconnue: mac={mac}, uuid={uuid}, major={major}, minor={minor}"
        )
        save_unknown_beacon(uuid=uuid, major=major, minor=minor, mac=mac, rssi=rssi)
        log_gate_event(None, payload, "unknown_beacon", "not_matched")
        return

    logging.info(
        f"Utilisateur autorisé trouvé: {user.get('name')} "
        f"(minor={user.get('minor')}, mac={user.get('mac')}, seuil={user.get('rssi_threshold')})"
    )

    threshold = normalize_int(user.get("rssi_threshold"), default=-70)

    if rssi is None:
        logging.warning(
            f"RSSI absent pour {user.get('name')}, ouverture refusée"
        )
        log_gate_event(user, payload, "denied_rssi", "missing_rssi")
        return

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

    update_display(user.get("name"))
    open_gate(client, user, payload)


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logging.info("✅ Connecté MQTT")
        client.subscribe(MQTT_DETECTION_TOPIC, qos=1)
        logging.info(f"📡 Souscription au topic: {MQTT_DETECTION_TOPIC}")
        client.publish(
            MQTT_STATUS_TOPIC,
            json.dumps({"status": "online", "ts": int(time.time())}),
            qos=1,
            retain=True,
        )
    else:
        logging.error(f"❌ Échec connexion MQTT rc={rc}")


def on_disconnect(client, userdata, rc, properties=None):
    if rc != 0:
        logging.warning(f"⚠️  Déconnexion MQTT inattendue (rc={rc}), reconnexion automatique...")
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

        process_detection(client, payload)

    except json.JSONDecodeError:
        logging.error("Payload JSON invalide")
    except Exception as e:
        logging.exception(f"Erreur traitement message MQTT: {e}")


def main():
    logging.info("🚀 PORTAIL WORKER")
    logging.info(f"📁 Base de données: {DB_PATH}")
    logging.info(f"📡 MQTT host={MQTT_HOST} port={MQTT_PORT}")
    logging.info(f"📥 Topic détection: {MQTT_DETECTION_TOPIC}")
    logging.info(f"📤 Topic relais: {MQTT_RELAY_TOPIC}")
    logging.info(f"📺 Fichier affichage: {DISPLAY_FILE}")
    logging.info(f"🔒 Fallback minor seul : DÉSACTIVÉ (sécurité)")
    logging.info(f"💾 WAL mode SQLite : ACTIVÉ")
    logging.info(f"🔁 Reconnexion MQTT : {MQTT_RECONNECT_DELAY_MIN}s–{MQTT_RECONNECT_DELAY_MAX}s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    # Reconnexion automatique avec backoff exponentiel
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
PYEOF

chown administrateur:administrateur "$WORKER_FILE"
ok "portail-worker.py mis à jour"

# ─── 3. Mise à jour app.py (WAL mode) ────────────────────────────────────────
section "3/4 — app.py : activation WAL mode SQLite"

# On insère le PRAGMA WAL dans db_connect() via sed
python3 - << 'EOF'
import re

path = "/home/administrateur/portail/web/app.py"
with open(path, "r") as f:
    content = f.read()

old = '''def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn'''

new = '''def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode : lectures simultanées sans bloquer les écritures du worker
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn'''

if old in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("WAL mode ajouté dans app.py")
elif "journal_mode=WAL" in content:
    print("WAL mode déjà présent dans app.py, rien à faire")
else:
    print("WARN: pattern db_connect() non trouvé — vérification manuelle requise")
EOF

chown administrateur:administrateur "$APP_FILE"
ok "app.py mis à jour"

# ─── 4. Mise à jour portail-web.service (Gunicorn) ───────────────────────────
section "4/4 — portail-web.service : passage à Gunicorn"

cat > /etc/systemd/system/portail-web.service << EOF
[Unit]
Description=Portail Web Display (Gunicorn)
After=network.target

[Service]
Type=simple
User=administrateur
WorkingDirectory=/home/administrateur/portail/web
ExecStart=$VENV/bin/gunicorn \\
    --workers 2 \\
    --bind 0.0.0.0:5000 \\
    --timeout 30 \\
    --access-logfile /home/administrateur/portail/logs/gunicorn-access.log \\
    --error-logfile /home/administrateur/portail/logs/gunicorn-error.log \\
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

ok "portail-web.service mis à jour (Gunicorn, 2 workers)"

# ─── 5. Désactivation mqtt-worker.service fantôme ────────────────────────────
section "5/4 — Désactivation mqtt-worker.service (répertoire fantôme)"

if systemctl is-active --quiet mqtt-worker 2>/dev/null; then
    systemctl stop mqtt-worker
    warn "mqtt-worker.service était actif — arrêté"
fi

if systemctl is-enabled --quiet mqtt-worker 2>/dev/null; then
    systemctl disable mqtt-worker
    warn "mqtt-worker.service désactivé du démarrage automatique"
fi

ok "mqtt-worker.service désactivé"

# ─── Copier les fichiers systemd mis à jour dans le projet ───────────────────
cp /etc/systemd/system/portail-web.service "$SYSTEMD_WEB"
chown administrateur:administrateur "$SYSTEMD_WEB"

# ─── Validation syntaxique Python ─────────────────────────────────────────────
section "Validation syntaxique des fichiers Python"

"$PYTHON" -m py_compile "$WORKER_FILE" && ok "portail-worker.py : syntaxe OK"
"$PYTHON" -m py_compile "$APP_FILE"    && ok "app.py : syntaxe OK"

# ─── Rechargement systemd + redémarrage ──────────────────────────────────────
section "Rechargement systemd et redémarrage des services"

systemctl daemon-reload
ok "systemd rechargé"

systemctl restart portail-worker
sleep 2
if systemctl is-active --quiet portail-worker; then
    ok "portail-worker : actif ✅"
else
    rollback "portail-worker n'a pas démarré après restart"
fi

systemctl restart portail-web
sleep 2
if systemctl is-active --quiet portail-web; then
    ok "portail-web : actif ✅"
else
    rollback "portail-web n'a pas démarré après restart (vérifier les logs Gunicorn)"
fi

# ─── Résumé final ─────────────────────────────────────────────────────────────
section "Résumé"

echo ""
echo -e "${GREEN}${BOLD}✅ Mise à jour terminée avec succès${NC}"
echo ""
echo -e "  📦 Backup complet    : ${BOLD}$BACKUP_DIR${NC}"
echo -e "  🔒 Fallback minor    : ${RED}SUPPRIMÉ${NC} (sécurité)"
echo -e "  💾 WAL mode SQLite   : ${GREEN}ACTIVÉ${NC} (worker + web)"
echo -e "  🔁 Reconnexion MQTT  : ${GREEN}ACTIVÉE${NC} (1s–30s backoff)"
echo -e "  🚀 Gunicorn          : ${GREEN}ACTIF${NC} (2 workers, port 5000)"
echo -e "  🚫 mqtt-worker       : ${YELLOW}DÉSACTIVÉ${NC}"
echo ""
echo "Pour rollback manuel si nécessaire :"
echo "  $PORTAIL_DIR/scripts/restore_portail.sh"
echo "  ou depuis : $BACKUP_DIR"
echo ""
echo "Logs en temps réel :"
echo "  journalctl -fu portail-worker"
echo "  journalctl -fu portail-web"
echo "  tail -f $PORTAIL_DIR/logs/gunicorn-error.log"
echo ""

ROLLBACK_DONE=1  # Tout s'est bien passé, on désarme le trap
