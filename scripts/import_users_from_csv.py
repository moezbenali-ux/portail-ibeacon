#!/usr/bin/env python3
import csv, sqlite3, re, sys
DB = '/home/administrateur/portail/data/users.db'
CSV = sys.argv[1] if len(sys.argv)>1 else '/home/administrateur/portail/users.csv'
MAC_RE = re.compile(r'^[0-9A-F]{2}(:[0-9A-F]{2}){5}$')

def norm_mac(mac):
    if mac is None: return None
    s = mac.strip().upper().replace('-', ':')
    parts = s.split(':')
    if len(parts) != 6:
        return None
    try:
        parts = [f"{int(p,16):02X}" for p in parts]
    except:
        return None
    return ':'.join(parts)

rows = []
seen_macs = {}
seen_minors = {}
skipped = 0
with open(CSV, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for i, r in enumerate(reader, start=1):
        # Adapt to the exact header the user gave:
        minor = r.get('minor') or None
        minor = int(minor) if minor not in (None,'','NULL') else None
        name = (r.get('name') or '').strip()
        email = (r.get('email') or '').strip() or None
        active = 1 if (r.get('active') or '').strip() in ('1','true','True','') else 0
        rssi = r.get('rssi_threshold') or r.get('rssi') or None
        rssi = int(rssi) if rssi not in (None,'','NULL') else -70
        # In your CSV the MAC comes before major/uuid
        mac = norm_mac(r.get('mac') or '')
        major = r.get('major') or None
        major = int(major) if major not in (None,'','') else None
        uuid = (r.get('uuid') or '').strip() or None

        if not mac:
            print(f"SKIP line {i}: MAC invalide ou manquant -> {r}")
            skipped += 1
            continue

        if mac in seen_macs:
            print(f"NOTE line {i}: MAC DUPLIQUÉ {mac} (déjà ligne {seen_macs[mac]}) - écrasera précédant via INSERT OR REPLACE")
        seen_macs[mac] = i

        if minor is not None:
            seen_minors.setdefault(minor, 0)
            seen_minors[minor] += 1

        rows.append((minor, name, email, active, rssi, uuid, major, mac))

# rapport
print(f"Prêt à insérer {len(rows)} lignes (skipped={skipped}). Minors duplicates: {[m for m,c in seen_minors.items() if c>1][:20]}")

# insertion
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.executemany('''
    INSERT OR REPLACE INTO users (minor, name, email, active, rssi_threshold, uuid, major, mac)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
''', rows)
conn.commit()
print("Import terminé :", len(rows), "lignes insérées (ou remplacées).")
conn.close()
