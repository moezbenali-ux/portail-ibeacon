#!/usr/bin/env python3
from flask import Flask, render_template_string, jsonify, request, send_from_directory
import json
import os
import time
import sqlite3
import re
import random

app = Flask(__name__)

# Fichiers
DISPLAY_FILE = "/tmp/current_display.json"
DB_PATH = "/home/administrateur/portail/data/users.db"
LOG_FILE = "/home/administrateur/portail/logs/portail.log"

# Page d'affichage public (écran 27")
HTML_PUBLIC = """
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Portail Parking</title>
  <style>
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: #0f172a;
      color: white;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      height: 100vh;
      text-align: center;
    }

    #clock {
      position: absolute;
      top: 30px;
      right: 40px;
      font-size: 32px;
      font-weight: bold;
    }

    #message {
      font-size: 64px;
      font-weight: bold;
      padding: 20px 40px;
    }
  </style>
</head>
<body>
  <div id="clock"></div>
  <div id="message">En attente...</div>

  <script>
    let lastCustomMessageAt = 0;

    function updateClock() {
      const now = new Date();
      const date = now.toLocaleDateString('fr-FR', {
        weekday: 'long',
        day: '2-digit',
        month: '2-digit',
        year: 'numeric'
      });
      const time = now.toLocaleTimeString('fr-FR');
      document.getElementById('clock').textContent = date + ' ' + time;
    }

    async function refreshMessage() {
      try {
        const res = await fetch('/current');
        const data = await res.json();

        const messageEl = document.getElementById('message');
        const nowMs = Date.now();

        if (data.message && data.message !== 'En attente...') {
          lastCustomMessageAt = nowMs;
          messageEl.textContent = data.message;
        } else {
          if (nowMs - lastCustomMessageAt > 30000) {
            messageEl.textContent = 'En attente...';
          }
        }
      } catch (e) {
        console.error('Erreur récupération message:', e);
      }
    }

    updateClock();
    setInterval(updateClock, 1000);

    refreshMessage();
    setInterval(refreshMessage, 2000);

    setInterval(() => {
      const messageEl = document.getElementById('message');
      const nowMs = Date.now();
      if (nowMs - lastCustomMessageAt > 30000) {
        messageEl.textContent = 'En attente...';
      }
    }, 1000);
  </script>
</body>
</html>
"""

# ==================== ROUTES PAGES ====================

@app.route('/')
def index():
    return HTML_PUBLIC

@app.route('/admin.html')
def admin():
    return send_from_directory('.', 'admin.html')

@app.route('/radar.html')
def radar():
    return send_from_directory('.', 'radar.html')

@app.route('/scan_beacons.html')
def scan_beacons():
    return send_from_directory('.', 'scan_beacons.html')

@app.route('/<path:filename>')
def serve_static(filename):
    if '..' in filename or filename.startswith('/'):
        return "Not found", 404
    static_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(static_dir, filename)
    if os.path.isfile(filepath):
        return send_from_directory(static_dir, filename)
    return "Not found", 404

@app.route('/current')
def current():
    try:
        if os.path.exists(DISPLAY_FILE):
            with open(DISPLAY_FILE, 'r') as f:
                data = json.load(f)
            if time.time() - data.get('timestamp', 0) < 60:
                return jsonify({
                    "message": f"Bienvenue {data['name']}",
                    "timestamp": data['timestamp']
                })
    except:
        pass
    return jsonify({"message": "En attente..."})

# ==================== ROUTES API UTILISATEURS ====================

@app.route('/api/users', methods=['GET'])
def api_get_users():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT minor, name, email, rssi_threshold, active, uuid, major FROM users ORDER BY minor")
        users = []
        for row in c.fetchall():
            users.append({
                'minor': row[0],
                'name': row[1],
                'email': row[2],
                'rssi_threshold': row[3],
                'active': bool(row[4]),
                'uuid': row[5],
                'major': row[6]
            })
        conn.close()
        return jsonify(users)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users', methods=['POST'])
def api_add_user():
    try:
        data = request.json
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT minor FROM users WHERE minor = ?", (data['minor'],))
        if c.fetchone():
            return jsonify({'error': 'Ce minor existe déjà'}), 400
        c.execute("""
            INSERT INTO users (minor, name, email, rssi_threshold, active, uuid, major)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data['minor'],
            data['name'],
            data.get('email', ''),
            data.get('rssi_threshold', -70),
            data.get('active', 1),
            data.get('uuid', None),
            data.get('major', None)
        ))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:minor>', methods=['PUT'])
def api_update_user(minor):
    try:
        data = request.json
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        updates = []
        values = []
        if 'name' in data:
            updates.append("name = ?")
            values.append(data['name'])
        if 'email' in data:
            updates.append("email = ?")
            values.append(data['email'])
        if 'rssi_threshold' in data:
            updates.append("rssi_threshold = ?")
            values.append(data['rssi_threshold'])
        if 'active' in data:
            updates.append("active = ?")
            values.append(1 if data['active'] else 0)
        if 'uuid' in data:
            updates.append("uuid = ?")
            values.append(data['uuid'])
        if 'major' in data:
            updates.append("major = ?")
            values.append(data['major'])
        if updates:
            query = f"UPDATE users SET {', '.join(updates)} WHERE minor = ?"
            values.append(minor)
            c.execute(query, values)
            conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/users/<int:minor>', methods=['DELETE'])
def api_delete_user(minor):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE minor = ?", (minor,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== ROUTES API CONFIG ====================

@app.route('/api/config', methods=['GET'])
def api_get_config():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
        defaults = {'global_rssi_threshold': '-70', 'display_duration': '60'}
        for key, val in defaults.items():
            c.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (key, val))
        conn.commit()
        c.execute("SELECT key, value FROM config")
        config = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return jsonify(config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config', methods=['POST'])
def api_save_config():
    try:
        data = request.json
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)')
        for key, value in data.items():
            c.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== ROUTE API POUR LE SCAN ====================

@app.route('/api/beacons/recent')
def api_recent_beacons():
    try:
        # Récupérer les utilisateurs autorisés
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT uuid, major, minor, name FROM users")
        users = {}
        for row in c.fetchall():
            users[row[2]] = {'uuid': row[0], 'major': row[1], 'name': row[3]}
        conn.close()
        
        beacons = []
        seen_minors = set()
        
        # Lire les logs récents
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()[-1000:]  # 1000 dernières lignes
                
                for line in reversed(lines):
                    # Chercher les lignes de détection
                    if "Minor:" in line and "RSSI:" in line:
                        # Extraire UUID si présent
                        uuid_match = re.search(r'UUID: ([0-9a-f-]+)', line)
                        major_match = re.search(r'Major: (\d+)', line)
                        minor_match = re.search(r'Minor: (\d+)', line)
                        rssi_match = re.search(r'RSSI: (-?\d+)', line)
                        
                        if minor_match and rssi_match:
                            minor = int(minor_match.group(1))
                            rssi = int(rssi_match.group(1))
                            
                            # Éviter les doublons (garder le plus récent)
                            if minor not in seen_minors:
                                seen_minors.add(minor)
                                
                                uuid = uuid_match.group(1) if uuid_match else None
                                major = int(major_match.group(1)) if major_match else None
                                
                                if minor in users:
                                    # Balise autorisée
                                    beacons.append({
                                        'minor': minor,
                                        'uuid': users[minor]['uuid'],
                                        'major': users[minor]['major'],
                                        'rssi': rssi,
                                        'authorized': True,
                                        'name': users[minor]['name']
                                    })
                                else:
                                    # Balise inconnue - on garde les valeurs détectées
                                    beacons.append({
                                        'minor': minor,
                                        'uuid': uuid,
                                        'major': major,
                                        'rssi': rssi,
                                        'authorized': False,
                                        'name': f"Inconnu {minor}"
                                    })
        
        # Si pas de logs, données de démonstration avec les utilisateurs connus
        if not beacons:
            for minor, data in users.items():
                beacons.append({
                    'minor': minor,
                    'uuid': data['uuid'],
                    'major': data['major'],
                    'rssi': random.randint(-85, -45),
                    'authorized': True,
                    'name': data['name']
                })
        
        return jsonify(beacons)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== SANTÉ ====================

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'time': time.time(),
        'db': os.path.exists(DB_PATH),
        'log': os.path.exists(LOG_FILE)
    })

if __name__ == '__main__':
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    print(f"🚀 Serveur web démarré sur port 5000")
    print(f"📁 Base de données: {DB_PATH}")
    print(f"📁 Fichier logs: {LOG_FILE}")
    app.run(host='0.0.0.0', port=5000, debug=False)
