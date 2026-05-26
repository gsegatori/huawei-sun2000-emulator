#!/bin/bash
#
# Deploy / aggiornamento dell'emulatore Huawei SUN2000 su Linux Docker.
#
# Prima esecuzione (es. sul mini PC Ubuntu):
#   git clone git@github.com:gsegatori/huawei-sun2000-emulator.git ~/huawei-sun2000-emulator
#   cd ~/huawei-sun2000-emulator && ./update.sh
#
# Esecuzioni successive (auto-update + restart):
#   cd ~/huawei-sun2000-emulator && ./update.sh
#
set -euo pipefail

echo "== Aggiornamento Huawei SUN2000 emulator =="

# --- 1) sync codice (auto-stash di eventuali mod locali, es. chmod +x manuale) ---
if [ -d .git ]; then
  echo "[1/5] git pull..."
  if ! git diff --quiet HEAD 2>/dev/null; then
    STASH_MSG="auto-stash-update.sh-$(date +%Y%m%d-%H%M%S)"
    echo "       mod locali presenti, stash come '$STASH_MSG' (recuperabile con git stash list)"
    git stash push -q -m "$STASH_MSG"
  fi
  git pull --ff-only
else
  echo "[1/5] non sono dentro un repo git, salto pull (eseguito post-clone manuale)"
fi

# --- 2) .env ---
if [ ! -f .env ]; then
  echo "[2/5] creo .env da .env.example..."
  cp .env.example .env
  LAN_IP=$(ip -4 route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
  if [ -n "${LAN_IP:-}" ]; then
    echo "       host LAN IP rilevato: ${LAN_IP}"
  fi
  echo "       ATTENZIONE: verifica OPENHAB_BASE_URL e HUAWEI_SN in .env"
else
  echo "[2/5] .env esiste, lasciato com'e'"
fi

# --- 3) stop precedente container (se presente) ---
echo "[3/5] stop container precedente..."
docker compose down 2>/dev/null || true

# --- 4) build + up + cleanup immagini orfane ---
echo "[4/5] build + up (host network, porta 502 + 5050)..."
docker compose up -d --build
echo "       cleanup immagini dangling..."
docker image prune -f >/dev/null 2>&1 || true

# --- 5) attendi healthy ---
echo "[5/5] attendo healthy..."
for i in $(seq 1 20); do
  status=$(docker compose ps --format '{{.Status}}' 2>/dev/null | head -1 || true)
  case "$status" in
    *"(healthy)"*) echo "       healthy ✓"; break ;;
    *"unhealthy"*) echo "       UNHEALTHY, vedi logs"; docker compose logs --tail 30; exit 1 ;;
  esac
  sleep 2
done

echo
echo "=== status ==="
docker compose ps
echo
LAN_IP=$(ip -4 route get 8.8.8.8 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
if [ -n "${LAN_IP:-}" ]; then
  echo "Endpoints live:"
  echo "   Modbus TCP:   ${LAN_IP}:502  (unit_id=1)"
  echo "   Admin UI:     http://${LAN_IP}:5050/"
  echo "   Healthcheck:  http://${LAN_IP}:5050/healthz"
  echo "   Registri:     http://${LAN_IP}:5050/admin/registers"
fi

echo
echo "== Emulator aggiornato! ✅ =="
