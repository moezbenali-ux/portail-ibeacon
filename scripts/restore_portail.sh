#!/bin/sh
set -eu

if [ $# -ne 1 ]; then
  echo "Usage: $0 /home/administrateur/portail-YYYYMMDD-HHMMSS.tar.gz"
  exit 1
fi

ARCHIVE="$1"
RESTORE_ROOT="/home/administrateur"
PROJECT_DIR="/home/administrateur/portail"

if [ ! -f "${ARCHIVE}" ]; then
  echo "Archive introuvable: ${ARCHIVE}"
  exit 1
fi

TMP_DIR="${RESTORE_ROOT}/restore-portail-$$"
mkdir -p "${TMP_DIR}"

echo "[1/7] Extraction"
tar -xzf "${ARCHIVE}" -C "${TMP_DIR}"

EXTRACTED_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"

echo "[2/7] Creation des dossiers"
mkdir -p "${PROJECT_DIR}"
mkdir -p "${PROJECT_DIR}/data"
mkdir -p "${PROJECT_DIR}/logs"
mkdir -p "${PROJECT_DIR}/scripts"
mkdir -p "${PROJECT_DIR}/systemd"

echo "[3/7] Copie du projet"
cp -a "${EXTRACTED_DIR}/project/." "${PROJECT_DIR}/"

echo "[4/7] Copie des services systemd"
if [ -d "${EXTRACTED_DIR}/systemd" ]; then
  cp -a "${EXTRACTED_DIR}/systemd/." "${PROJECT_DIR}/systemd/" || true
fi

echo "[5/7] Installation des dependances Python"
cd "${PROJECT_DIR}"
python3 -m venv venv
. "${PROJECT_DIR}/venv/bin/activate"
pip install --upgrade pip
pip install flask paho-mqtt
deactivate

echo "[6/7] Installation des services"
for svc in portail-worker.service portail-web.service mqtt-worker.service; do
  if [ -f "${PROJECT_DIR}/systemd/${svc}" ]; then
    sudo cp "${PROJECT_DIR}/systemd/${svc}" "/etc/systemd/system/${svc}"
  fi
done

sudo systemctl daemon-reload

echo "[7/7] Activation des services disponibles"
for svc in portail-worker.service portail-web.service mqtt-worker.service; do
  if [ -f "/etc/systemd/system/${svc}" ]; then
    sudo systemctl enable "${svc}" || true
    sudo systemctl restart "${svc}" || true
  fi
done

echo "Restauration terminee"
