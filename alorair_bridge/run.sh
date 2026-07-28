#!/usr/bin/env sh
set -e
CONF=/data/options.json
get() { python3 -c "import json;print(json.load(open('$CONF')).get('$1',''))"; }
export MQTT_HOST="$(get mqtt_host)"
export MQTT_PORT="$(get mqtt_port)"
export MQTT_USER="$(get mqtt_user)"
export MQTT_PASS="$(get mqtt_pass)"
export ALLOWED_SOURCE_IP="$(get allowed_source_ip)"
export MQTT_PREFIX="alorair"
export DISCOVERY_PREFIX="homeassistant"
MODE="$(get mode)"
[ -z "$MODE" ] && MODE=local
echo "starting AlorAir bridge mode=${MODE} -> MQTT ${MQTT_HOST}:${MQTT_PORT}"
exec python3 /app/alorair_cloud.py --mode "$MODE" --logfile /data/local.log
