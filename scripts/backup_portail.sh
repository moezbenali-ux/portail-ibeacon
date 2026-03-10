#!/bin/sh
set -eu

PROJECT_DIR="/home/administrateur/portail"
BACKUP_ROOT="/home/administrateur/backups"
DATE_TAG="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/portail-${DATE_TAG}"

mkdir -p "${BACKUP_DIR}"

echo "[1/6] Creation du dossier ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}/project"
mkdir -p "${BACKUP_DIR}/systemd"
mkdir -p "${BACKUP_DIR}/meta"

echo "[2/6] Copie du projet"
rsync -a \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'logs/*' \
  "${PROJECT_DIR}/" "${BACKUP_DIR}/project/"

echo "[3/6] Sauvegarde de la base SQLite"
if [ -f "${PROJECT_DIR}/data/users.db" ]; then
  cp "${PROJECT_DIR}/data/users.db" "${BACKUP_DIR}/project/data/users.db"
fi

echo "[4/6] Sauvegarde des services systemd"
for svc in portail-worker.service portail-web.service mqtt-worker.service; do
  if [ -f "/etc/systemd/system/${svc}" ]; then
    cp "/etc/systemd/system/${svc}" "${BACKUP_DIR}/systemd/${svc}"
  fi
done

echo "[5/6] Meta informations"
uname -a > "${BACKUP_DIR}/meta/uname.txt"
python3 --version > "${BACKUP_DIR}/meta/python-version.txt" 2>/dev/null || true
pip3 --version > "${BACKUP_DIR}/meta/pip-version.txt" 2>/dev/null || true
systemctl list-unit-files > "${BACKUP_DIR}/meta/systemd-unit-files.txt" 2>/dev/null || true

echo "[6/6] Archive finale"
cd "${BACKUP_ROOT}"
tar -czf "portail-${DATE_TAG}.tar.gz" "portail-${DATE_TAG}"

echo "Sauvegarde terminee : ${BACKUP_ROOT}/portail-${DATE_TAG}.tar.gz"
