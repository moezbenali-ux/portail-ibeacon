#!/usr/bin/env python3
import csv, sqlite3, sys, re
DB = '/home/administrateur/portail/data/users.db'
CSV = sys.argv[1] if len(sys.argv)>1 else 'users.csv'

def norm_mac(mac):
    if not mac: return None
    mac = mac.strip().upper().replace('-', ':')
    parts = mac.split(':')
    if len(parts) != 6:
        return None
    try:
        parts = [f"{int(p,16):02X}" for p in parts]
    except:
        return None
    return ':'.join(parts)

conn = sqlite3.connect(DB)
cur = conn.cursor()

with open(CSV, newline='') as f:
    reader = csv.DictReader(f)
    rows = []
    for r in reader:
        minor = r.get('minor') or None
        minor = int(minor) if minor not in (None, '', 'NULL') else None
        name = r.get('name') or ''
        email = r.get('email') or None
        active = 1 if r.get('active','1') in ('1','true','True') else 0
        rssi = int(r.get('rssi_threshold') or -70)
        uuid = r.get('uuid') or None
        major = r.get('major') or None
        major = int(major) if major not in (None, '', 'NULL') else None
        mac = norm_mac(r.get('mac') or '')
        if not mac:
            print(f"SKIP: ligne sans MAC valide: {r}")
            continue
        rows.append((minor, name, email, active, rssi, uuid, major, mac))
cur.executemany('''
    INSERT OR REPLACE INTO users (minor, name, email, active, rssi_threshold, uuid, major, mac)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
''', rows)
conn.commit()
conn.close()
print(f"Import OK - {len(rows)} lignes insérées")
