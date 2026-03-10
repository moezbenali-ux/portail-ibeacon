#!/usr/bin/env python3
import paho.mqtt.client as mqtt
import json
import sqlite3
import os
import threading
import time
import logging
from logging.handlers import RotatingFileHandler

# ============================================================
# CONFIGURATION
# ============================================================

MQTT_HOST = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_DETECT = "parking/beacon/detected"
MQTT_TOPIC_OPEN = "parking/relay/command"

DB_PATH = os.path.expanduser("~/portail/data/users.db")
DISPLAY_FILE = "/tmp/current_display.json"
LOG_FILE = "/home/administrateur/portail/logs/portail.log"

DISPLAY_DURATION = 60

ABSENT_THRESHOLD = -80
VERY_WEAK_THRESHOLD = -95
ABSENT_REQUIRED = 10

WORKER_OPEN_COOLDOWN = 10
GLOBAL_OPEN_COOLDOWN = 2

HYSTERESIS_MARGIN = 5

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# ============================================================
# LOGGING
# ============================================================

def setup_logging():
    logger = logging.getLogger("portail")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


log = setup_logging()

# ============================================================
# ÉTAT GLOBAL
# ============================================================

current_timer = None
timer_lock = threading.Lock()

absent_count = {}
present_beacons = set()

last_open_times = {}
last_global_open_time = 0

# ============================================================
# OUTILS
# ============================================================

def make_key(uuid, major, minor):
    return f"{uuid}|{major}|{minor}"

# ============================================================
# PORTAIL
# ============================================================

def open_gate(beacon_key, name):
    global last_global_open_time

    now = time.time()

    if now - last_global_open_time < GLOBAL_OPEN_COOLDOWN:
        remaining = GLOBAL_OPEN_COOLDOWN - (now - last_global_open_time)
        log.info(f"⏱️ Anti-boucle GLOBAL : ouverture refusée ({remaining:.1f}s)")
        return False

    last_time = last_open_times.get(beacon_key, 0)
    if now - last_time < WORKER_OPEN_COOLDOWN:
        return False

    log.info(f"🔊 Tentative d'ouverture pour {name}")

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect(MQTT_HOST, MQTT_PORT, 5)
        client.loop_start()

        result = client.publish(MQTT_TOPIC_OPEN, "OPEN")
        result.wait_for_publish()

        client.loop_stop()
        client.disconnect()

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            last_open_times[beacon_key] = now
            last_global_open_time = now
            log.info(f"🔓 OUVERTURE PORTAIL pour {name}")
            return True

        return False

    except Exception as e:
        log.error(f"Erreur ouverture portail: {e}")
        return False

# ============================================================
# AFFICHAGE
# ============================================================

def cancel_current_timer():
    global current_timer
    with timer_lock:
        if current_timer and current_timer.is_alive():
            current_timer.cancel()
            current_timer = None

def clear_display():
    global current_timer
    try:
        if os.path.exists(DISPLAY_FILE):
            os.remove(DISPLAY_FILE)
    finally:
        current_timer = None

def schedule_clear():
    global current_timer
    with timer_lock:
        current_timer = threading.Timer(DISPLAY_DURATION, clear_display)
        current_timer.start()

def update_display(name):
    try:
        data = {"name": name, "timestamp": time.time()}
        with open(DISPLAY_FILE, "w") as f:
            json.dump(data, f)
        log.info(f"📺 Affichage mis à jour: {name}")
    except Exception as e:
        log.error(e)

# ============================================================
# DB
# ============================================================

def get_user_from_beacon(uuid, major, minor):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        c.execute("""
        SELECT name,email,rssi_threshold,active
        FROM users
        WHERE uuid=? AND major=? AND minor=?
        """,(uuid,major,minor))

        result=c.fetchone()
        conn.close()
        return result

    except Exception as e:
        log.error(e)
        return None

# ============================================================
# PRÉSENCE AVEC HYSTÉRÉSIS
# ============================================================

def handle_presence_state(beacon_key,name,rssi,threshold):

    previously_present = beacon_key in present_beacons
    exit_threshold = threshold - HYSTERESIS_MARGIN

    if rssi < VERY_WEAK_THRESHOLD:
        present_beacons.discard(beacon_key)
        return False,False

    if rssi < ABSENT_THRESHOLD:
        absent_count[beacon_key]=absent_count.get(beacon_key,0)+1
        if absent_count[beacon_key]>=ABSENT_REQUIRED:
            present_beacons.discard(beacon_key)
        return False,False

    absent_count[beacon_key]=0

    if previously_present:

        if rssi <= exit_threshold:
            present_beacons.discard(beacon_key)
            log.info(f"🚪 {name} retiré état actif (RSSI {rssi})")
            return False,False

        return True,False

    else:

        if rssi <= threshold:
            return False,False

        present_beacons.add(beacon_key)
        log.info(f"🔄 {name} est de retour / nouvellement présent")
        return True,True

# ============================================================
# MQTT
# ============================================================

def on_connect(client,userdata,flags,rc,properties=None):

    if rc==0:
        log.info("✅ Connecté MQTT")
        client.subscribe(MQTT_TOPIC_DETECT)

def on_message(client,userdata,msg):

    try:

        payload=json.loads(msg.payload.decode())

        uuid=payload.get("uuid")
        major=payload.get("major")
        minor=payload.get("minor")
        rssi=payload.get("rssi")

        beacon_key=make_key(uuid,major,minor)

        user=get_user_from_beacon(uuid,major,minor)

        if not user:
            return

        name,email,threshold,active=user

        if not active:
            return

        log.info(f"📩 {name} RSSI {rssi}")

        cancel_current_timer()
        update_display(name)
        schedule_clear()

        eligible,returned=handle_presence_state(beacon_key,name,rssi,threshold)

        if not eligible:
            return

        if returned:
            log.info(f"👤 {name} revient - OUVERTURE")
            open_gate(beacon_key,name)

    except Exception as e:
        log.error(e)

# ============================================================
# MAIN
# ============================================================

if __name__=="__main__":

    log.info("🚀 PORTAIL WORKER SANS EXCEPTION")

    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    client.on_connect=on_connect
    client.on_message=on_message

    client.connect(MQTT_HOST,MQTT_PORT,60)

    client.loop_forever()
