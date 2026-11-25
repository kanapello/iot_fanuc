#!/bin/sh
# Generates the password and ACL files from environment variables, then starts the broker.
#
#   MM_MQTT_BACKEND_USER / MM_MQTT_BACKEND_PASSWORD  - ingest, reads everything
#   MM_MQTT_COLLECTORS - one account per collector, comma separated:
#       <collector_id>:<org_code>:<site_code>:<password>[,...]
#     A collector may publish only telemetry/events of its own site and only its own heartbeat.
#     Passwords must not contain ',' or ':'.
#   MM_MQTT_TLS_DOMAIN - production (ADR 0010): MQTT over TLS on 8883 with the certificate Caddy
#     keeps for this domain in its data volume (mounted at /caddy-data). The broker waits for the
#     certificate, and a renewed one is picked up within MM_MQTT_CERT_CHECK_S by SIGHUP.
#   MM_ENV=prod - refuse to start with the passwords of .env.example.
set -eu

: "${MM_MQTT_BACKEND_USER:?}" "${MM_MQTT_BACKEND_PASSWORD:?}" "${MM_MQTT_COLLECTORS:=}"
: "${MM_ENV:=dev}" "${MM_MQTT_TLS_DOMAIN:=}" "${MM_MQTT_CERT_CHECK_S:=3600}"

placeholder() {  # every password of .env.example starts with change-me
    case "$1" in change-me*) return 0 ;; *) return 1 ;; esac
}

if [ "$MM_ENV" = prod ]; then
    problems=""
    if placeholder "$MM_MQTT_BACKEND_PASSWORD"; then
        problems="$problems
  - MM_MQTT_BACKEND_PASSWORD: a placeholder from .env.example (change-me...)"
    fi
    for entry in $(echo "$MM_MQTT_COLLECTORS" | tr ',' ' '); do
        if placeholder "${entry##*:}"; then
            problems="$problems
  - MM_MQTT_COLLECTORS: the password of collector ${entry%%:*} is a placeholder (change-me...)"
        fi
    done
    if [ -n "$problems" ]; then
        echo "Refusing to start in production (MM_ENV=prod): these settings are unsafe:$problems" >&2
        echo "Create .env with random passwords: make env-prod (see docs/runbooks/cloud-install.md)." >&2
        exit 2
    fi
fi

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

CONFIG=/mosquitto/config/mosquitto.conf
if [ -n "$MM_MQTT_TLS_DOMAIN" ]; then
    CERT_DIR=/mosquitto/certs
    mkdir -p "$CERT_DIR" /mosquitto/run

    # Caddy stores certificates as certificates/<issuer>/<domain>/<domain>.crt|.key; the newest wins
    source_cert() {
        # shellcheck disable=SC2012
        ls -t /caddy-data/caddy/certificates/*/"$MM_MQTT_TLS_DOMAIN"/"$MM_MQTT_TLS_DOMAIN".crt 2>/dev/null | head -n 1
    }
    install_cert() {  # copy for the mosquitto user: Caddy's files are readable by root only
        cp "$1" "$CERT_DIR/fullchain.pem"
        cp "${1%.crt}.key" "$CERT_DIR/privkey.pem"
        chown mosquitto:mosquitto "$CERT_DIR/fullchain.pem" "$CERT_DIR/privkey.pem"
        chmod 0644 "$CERT_DIR/fullchain.pem"
        chmod 0600 "$CERT_DIR/privkey.pem"
    }

    waited=0
    until cert=$(source_cert) && [ -n "$cert" ]; do
        if [ $((waited % 30)) -eq 0 ]; then
            echo "waiting for the certificate of $MM_MQTT_TLS_DOMAIN from caddy (DNS and ports 80/443 must reach this server)"
        fi
        sleep 5
        waited=$((waited + 5))
    done
    install_cert "$cert"
    echo "MQTT over TLS on 8883 with the certificate of $MM_MQTT_TLS_DOMAIN ($cert)"

    {
        cat /mosquitto/config/mosquitto.conf
        printf '\n# MQTT over TLS for collectors (ADR 0010); 1883 stays inside the compose network\n'
        printf 'listener 8883\ncertfile %s/fullchain.pem\nkeyfile %s/privkey.pem\ntls_version tlsv1.2\n' "$CERT_DIR" "$CERT_DIR"
    } > /mosquitto/run/mosquitto.conf
    CONFIG=/mosquitto/run/mosquitto.conf

    # A renewed certificate (Caddy renews about 30 days before expiry) is copied and loaded by SIGHUP:
    # mosquitto 2 reloads the TLS certificates of its listeners on SIGHUP, connections stay open.
    (
        set +e  # a failed copy must not stop the watcher: it retries at the next check
        while sleep "$MM_MQTT_CERT_CHECK_S"; do
            cert=$(source_cert) || continue
            if [ -n "$cert" ] && ! cmp -s "$cert" "$CERT_DIR/fullchain.pem"; then
                install_cert "$cert"
                echo "renewed certificate of $MM_MQTT_TLS_DOMAIN installed, reloading mosquitto"
                kill -HUP 1
            fi
        done
    ) &
fi

chown -R mosquitto:mosquitto "$AUTH_DIR" /mosquitto/data
exec /usr/sbin/mosquitto -c "$CONFIG"
