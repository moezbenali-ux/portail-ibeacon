#!/bin/bash
# =============================================================================
# fix_portail.sh — Corrections post-audit
# =============================================================================
# 1. Persistance du cooldown en base SQLite (survie aux redémarrages)
# 2. Désactivation des comptes de test (Test10129 id=99, test id=96)
# 3. Correction PASCALE ROSSO (minor/major vides)
# 4. Détection et rapport des conflits de minor
# =============================================================================

set -euo pipefail

PORTAIL_DIR="/home/administrateur/portail"
VENV="$PORTAIL_DIR/venv"
PYTHON="$VENV/bin/python"
DB="$PORTAIL_DIR/data/users.db"
WORKER_FILE="$PORTAIL_DIR/worker/portail-worker.py"

BACKUP_DIR="$PORTAIL_DIR/data/backups/fix-$(date +%Y%m%d-%H%M%S)"
ROLLBACK_DONE=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${BOLD}━━━ $* ━━━${NC}"; }

rollback() {
    local reason="$1"
    error "ÉCHEC : $reason"
    warn "═══════════════════════════════════"
    warn "  ROLLBACK — restauration en cours"
    warn "═══════════════════════════════════"

    [ -f "$BACKUP_DIR/portail-worker.py" ] && \
        cp "$BACKUP_DIR/portail-worker.py" "$WORKER_FILE" && ok "Worker restauré"
    [ -f "$BACKUP_DIR/users.db" ] && \
        cp "$BACKUP_DIR/users.db" "$DB" && ok "Base de données restaurée"

    systemctl daemon-reload
    echo ""
    warn "Fichiers restaurés. Services NON redémarrés."
    read -rp "Appuyez sur ENTRÉE pour redémarrer les services : "
    systemctl restart portail-worker
    ROLLBACK_DONE=1
    exit 1
}

trap 'if [ $ROLLBACK_DONE -eq 0 ]; then rollback "erreur inattendue (ligne $LINENO)"; fi' ERR

if [ "$EUID" -ne 0 ]; then
    error "Ce script doit être exécuté en root (sudo)."
    exit 1
fi

# ─── Backup ───────────────────────────────────────────────────────────────────
section "Backup"
mkdir -p "$BACKUP_DIR"
cp "$WORKER_FILE" "$BACKUP_DIR/portail-worker.py"
cp "$DB"          "$BACKUP_DIR/users.db"
ok "Backup dans : $BACKUP_DIR"

# ─── 1. Créer la table cooldown en base ───────────────────────────────────────
section "1/4 — Création table cooldown persistant"

sqlite3 "$DB" << 'SQL'
CREATE TABLE IF NOT EXISTS cooldown_state (
    beacon_key   TEXT PRIMARY KEY,
    last_open_ts REAL NOT NULL,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Cooldown global (clé réservée)
INSERT OR IGNORE INTO cooldown_state (beacon_key, last_open_ts)
VALUES ('__global__', 0);
SQL

ok "Table cooldown_state créée (ou déjà existante)"

# ─── 2. Réécriture portail-worker.py avec cooldown persistant ─────────────────
section "2/4 — Mise à jour portail-worker.py (cooldown persistant)"

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
MQTT_RELAY_TOPIC     = os.getenv("MQTT_RELAY_TOPIC",     "parking/relay/command")
MQTT_STATUS_TOPIC    = os.getenv("MQTT_STATUS_TOPIC",    "portail/status")

DISPLAY_FILE = os.getenv("DISPLAY_FILE", "/tmp/current_display.json")

OPEN_COOLDOWN_SECONDS        = int(os.getenv("OPEN_COOLDOWN_SECONDS",        "10"))
GLOBAL_OPEN_COOLDOWN_SECONDS = int(os.getenv("GLOBAL_OPEN_COOLDOWN_SECONDS", "2"))

MIN_DETECTIONS_TO_OPEN = int(os.getenv("MIN_DETECTIONS_TO_OPEN", "2"))
RSSI_WINDOW_SECONDS    = int(os.getenv("RSSI_WINDOW_SECONDS",    "5"))
ABSENCE_TIMEOUT_SECONDS = int(os.getenv("ABSENCE_TIMEOUT_SECONDS", "45"))

MQTT_RECONNECT_DELAY_MIN = int(os.getenv("MQTT_RECONNECT_DELAY_MIN", "1"))
MQTT_RECONNECT_DELAY_MAX = int(os.getenv("MQTT_RECONNECT_DELAY_MAX", "30"))

# Cache mémoire des détections RSSI (pas besoin de persister)
RECENT_DETECTIONS: dict = {}

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
    """Retourne le timestamp de la dernière ouverture pour cette clé (0 si jamais ouverte)."""
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
    """Enregistre le timestamp d'ouverture pour cette clé."""
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


def should_open(beacon_key: str, cooldown_seconds: int) -> bool:
    now = time.time()

    # Cooldown global (anti-rebond multi-beacon)
    last_global = cooldown_get("__global__")
    if now - last_global < GLOBAL_OPEN_COOLDOWN_SECONDS:
        remaining = round(GLOBAL_OPEN_COOLDOWN_SECONDS - (now - last_global), 1)
        logging.info(f"Cooldown global actif ({remaining}s restantes)")
        return False

    # Cooldown par beacon
    last = cooldown_get(beacon_key)
    if now - last < cooldown_seconds:
        remaining = round(cooldown_seconds - (now - last), 1)
        logging.info(f"Cooldown actif pour {beacon_key} ({remaining}s restantes)")
        return False

    # Enregistrement en base — survie aux redémarrages
    cooldown_set(beacon_key, now)
    cooldown_set("__global__", now)
    return True


# ─── Recherche utilisateur ────────────────────────────────────────────────────

def get_user_from_beacon(uuid=None, major=None, minor=None, mac=None):
    """
    Recherche par MAC (priorité) puis triplet UUID/major/minor.
    Le fallback par minor seul est désactivé (risque de faux positifs).
    """
    conn = None
    try:
        conn = db_connect()
        cur = conn.cursor()
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

        if uuid is not None and major is not None and minor is not None:
            cur.execute(
                """SELECT id, minor, name, email, active, rssi_threshold, uuid, major, mac
                   FROM users WHERE uuid = ? AND major = ? AND minor = ? AND active = 1""",
                (str(uuid), int(major), int(minor)),
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


# ─── Affichage & ouverture ────────────────────────────────────────────────────

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
    logging.info(f"🔓 Ouverture portail publiée sur {MQTT_RELAY_TOPIC} pour {user.get('name')}")
    log_gate_event(user, payload, "open", "authorized")


# ─── Traitement détection ─────────────────────────────────────────────────────

def process_detection(client: mqtt.Client, payload: dict):
    mark_departures()

    uuid  = payload.get("uuid")
    major = normalize_int(payload.get("major"))
    minor = normalize_int(payload.get("minor"))
    mac   = normalize_mac(
        payload.get("mac") or payload.get("device_mac") or payload.get("address")
    )
    rssi  = normalize_int(payload.get("rssi"))

    logging.info(f"Beacon reçu: mac={mac}, uuid={uuid}, major={major}, minor={minor}, rssi={rssi}")

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


# ─── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logging.info("✅ Connecté MQTT")
        client.subscribe(MQTT_DETECTION_TOPIC, qos=1)
        logging.info(f"📡 Souscription: {MQTT_DETECTION_TOPIC}")
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
        process_detection(client, payload)
    except json.JSONDecodeError:
        logging.error("Payload JSON invalide")
    except Exception as e:
        logging.exception(f"Erreur traitement message MQTT: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    logging.info("🚀 PORTAIL WORKER")
    logging.info(f"📁 Base de données : {DB_PATH}")
    logging.info(f"📡 MQTT {MQTT_HOST}:{MQTT_PORT}")
    logging.info(f"📥 Topic détection : {MQTT_DETECTION_TOPIC}")
    logging.info(f"📤 Topic relais    : {MQTT_RELAY_TOPIC}")
    logging.info(f"🔒 Cooldown        : persistant en base (survie redémarrages)")
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
PYEOF

chown administrateur:administrateur "$WORKER_FILE"
ok "portail-worker.py mis à jour (cooldown persistant)"

# ─── 3. Désactivation comptes de test ─────────────────────────────────────────
section "3/4 — Désactivation des comptes de test (id 96 et 99)"

sqlite3 "$DB" << 'SQL'
UPDATE users SET active = 0 WHERE id IN (96, 99);
SQL

# Vérification
RESULT=$(sqlite3 "$DB" "SELECT id, name, active FROM users WHERE id IN (96, 99);")
echo "$RESULT"
ok "Comptes Test10129 (99) et test (96) désactivés"

# ─── 4. Rapport conflits de minor ─────────────────────────────────────────────
section "4/4 — Rapport des conflits de minor"

echo ""
echo -e "${BOLD}Utilisateurs partageant le même minor (actifs uniquement) :${NC}"
echo ""

CONFLICTS=$(sqlite3 "$DB" << 'SQL'
SELECT
    minor,
    GROUP_CONCAT(id || ' - ' || name, ' | ') AS utilisateurs,
    COUNT(*) AS nb
FROM users
WHERE active = 1
  AND minor IS NOT NULL
GROUP BY minor
HAVING COUNT(*) > 1
ORDER BY minor;
SQL
)

if [ -z "$CONFLICTS" ]; then
    ok "Aucun conflit de minor détecté parmi les utilisateurs actifs."
else
    echo -e "${YELLOW}⚠️  Conflits détectés :${NC}"
    echo ""
    printf "%-10s %-5s %s\n" "MINOR" "NB" "UTILISATEURS"
    printf "%-10s %-5s %s\n" "─────" "──" "────────────"
    while IFS='|' read -r minor users nb; do
        printf "${RED}%-10s${NC} %-5s %s\n" "$minor" "$nb" "$users"
    done <<< "$CONFLICTS"
    echo ""
    warn "Ces utilisateurs partagent le même minor avec des UUID/MAC différents."
    warn "Sans UUID ni MAC enregistrés, le système ne peut pas les distinguer."
    warn "→ Vérifiez et corrigez via l'interface admin ou manuellement."
fi

# ─── 5. Rapport PASCALE ROSSO ────────────────────────────────────────────────
section "5/4 — Vérification PASCALE ROSSO (id=17)"

echo ""
ROSSO=$(sqlite3 "$DB" "SELECT id, name, mac, uuid, major, minor, active FROM users WHERE id=17;")
echo "État actuel : $ROSSO"
echo ""
MAC_ROSSO=$(sqlite3 "$DB" "SELECT mac FROM users WHERE id=17;")
echo -e "${YELLOW}PASCALE ROSSO a une MAC ($MAC_ROSSO) mais minor/major vides.${NC}"
echo -e "${YELLOW}Elle sera reconnue UNIQUEMENT par sa MAC — c'est suffisant.${NC}"
echo ""
warn "Aucune correction automatique appliquée : la MAC seule est valide."
warn "Si tu veux ajouter son minor/major, utilise l'interface admin."

# ─── Validation syntaxique ────────────────────────────────────────────────────
section "Validation syntaxique Python"

"$PYTHON" -m py_compile "$WORKER_FILE" && ok "portail-worker.py : syntaxe OK"

# ─── Redémarrage ─────────────────────────────────────────────────────────────
section "Redémarrage du worker"

systemctl restart portail-worker
sleep 2

if systemctl is-active --quiet portail-worker; then
    ok "portail-worker : actif ✅"
else
    rollback "portail-worker n'a pas démarré"
fi

# ─── Résumé ───────────────────────────────────────────────────────────────────
section "Résumé"

echo ""
echo -e "${GREEN}${BOLD}✅ Corrections appliquées avec succès${NC}"
echo ""
echo -e "  📦 Backup           : ${BOLD}$BACKUP_DIR${NC}"
echo -e "  🔒 Cooldown         : ${GREEN}PERSISTANT en base${NC} (survie aux redémarrages)"
echo -e "  🚫 Test10129 (99)   : ${RED}DÉSACTIVÉ${NC}"
echo -e "  🚫 test (96)        : ${RED}DÉSACTIVÉ${NC}"
echo -e "  👤 PASCALE ROSSO    : ${YELLOW}MAC seule — suffisant${NC}"
echo ""
echo "Vérifier les logs du worker :"
echo "  journalctl -fu portail-worker"
echo ""

ROLLBACK_DONE=1
