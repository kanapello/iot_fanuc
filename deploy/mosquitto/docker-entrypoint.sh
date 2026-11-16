#!/bin/sh
# Generates the password and ACL files from environment variables, then starts the broker.
set -eu

: "${MM_MQTT_BACKEND_USER:?}" "${MM_MQTT_BACKEND_PASSWORD:?}" "${MM_MQTT_EDGE_USER:?}" "${MM_MQTT_EDGE_PASSWORD:?}"

AUTH_DIR=/mosquitto/auth
mkdir -p "$AUTH_DIR"
rm -f "$AUTH_DIR/passwd"
touch "$AUTH_DIR/passwd"
chmod 0700 "$AUTH_DIR/passwd"
mosquitto_passwd -b "$AUTH_DIR/passwd" "$MM_MQTT_BACKEND_USER" "$MM_MQTT_BACKEND_PASSWORD"
mosquitto_passwd -b "$AUTH_DIR/passwd" "$MM_MQTT_EDGE_USER" "$MM_MQTT_EDGE_PASSWORD"

# Collectors only write, the backend only reads. Per-collector accounts come with stage 6.
cat > "$AUTH_DIR/acl" <<ACL
user $MM_MQTT_EDGE_USER
topic write mm/v1/#

user $MM_MQTT_BACKEND_USER
topic read mm/v1/#
ACL
chmod 0700 "$AUTH_DIR/acl"
chown -R mosquitto:mosquitto "$AUTH_DIR" /mosquitto/data

exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
