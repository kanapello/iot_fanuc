#!/bin/sh
# Generates the password and ACL files from environment variables, then starts the broker.
#
#   MM_MQTT_BACKEND_USER / MM_MQTT_BACKEND_PASSWORD  - ingest, reads everything
#   MM_MQTT_COLLECTORS - one account per collector, comma separated:
#       <collector_id>:<org_code>:<site_code>:<password>[,...]
#     A collector may publish only telemetry/events of its own site and only its own heartbeat.
#     Passwords must not contain ',' or ':'.
set -eu

: "${MM_MQTT_BACKEND_USER:?}" "${MM_MQTT_BACKEND_PASSWORD:?}" "${MM_MQTT_COLLECTORS:=}"

AUTH_DIR=/mosquitto/auth
mkdir -p "$AUTH_DIR"
rm -f "$AUTH_DIR/passwd" "$AUTH_DIR/acl"
touch "$AUTH_DIR/passwd" "$AUTH_DIR/acl"
chmod 0700 "$AUTH_DIR/passwd" "$AUTH_DIR/acl"
mosquitto_passwd -b "$AUTH_DIR/passwd" "$MM_MQTT_BACKEND_USER" "$MM_MQTT_BACKEND_PASSWORD"

cat >> "$AUTH_DIR/acl" <<ACL
user $MM_MQTT_BACKEND_USER
topic read mm/v1/#

ACL

OLD_IFS=$IFS
IFS=','
for entry in $MM_MQTT_COLLECTORS; do
    [ -n "$entry" ] || continue
    IFS=':'
    # shellcheck disable=SC2086
    set -- $entry
    IFS=','
    if [ "$#" -ne 4 ]; then
        echo "MM_MQTT_COLLECTORS: expected <collector_id>:<org>:<site>:<password>, got '$entry'" >&2
        exit 1
    fi
    collector=$1 org=$2 site=$3 password=$4
    mosquitto_passwd -b "$AUTH_DIR/passwd" "$collector" "$password"
    cat >> "$AUTH_DIR/acl" <<ACL
user $collector
topic write mm/v1/$org/$site/+/telemetry
topic write mm/v1/$org/$site/+/event
topic write mm/v1/$org/$site/collector/$collector/heartbeat

ACL
done
IFS=$OLD_IFS

chown -R mosquitto:mosquitto "$AUTH_DIR" /mosquitto/data
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
