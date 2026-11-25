#!/bin/sh
# Creates .env for production with random passwords: make env-prod domain=mm.example.com email=ops@example.com
# Never overwrites an existing .env. The S3 settings of backups are filled in by hand afterwards.
set -eu

domain=${1:-}
email=${2:-}
if [ -z "$domain" ] || [ -z "$email" ]; then
    echo "usage: make env-prod domain=mm.example.com email=ops@example.com" >&2
    echo "  domain - the DNS name of this server (an A record to its IP); email - for Let's Encrypt notices" >&2
    exit 1
fi
if [ -e .env ]; then
    echo ".env already exists and is not overwritten. Move it away to create a new one." >&2
    exit 1
fi

random() {  # letters and digits only: the values also go into URLs and the broker's password file
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$1"
}
admin_password=$(random 20)

sed \
    -e "s|^MM_ENV=.*|MM_ENV=prod|" \
    -e "s|^MM_DOMAIN=.*|MM_DOMAIN=$domain|" \
    -e "s|^MM_ACME_EMAIL=.*|MM_ACME_EMAIL=$email|" \
    -e "s|^MM_DB_PASSWORD=.*|MM_DB_PASSWORD=$(random 32)|" \
    -e "s|^MM_MQTT_BACKEND_PASSWORD=.*|MM_MQTT_BACKEND_PASSWORD=$(random 32)|" \
    -e "s|^MM_MQTT_SIM_A_PASSWORD=.*|MM_MQTT_SIM_A_PASSWORD=$(random 32)|" \
    -e "s|^MM_MQTT_SIM_B_PASSWORD=.*|MM_MQTT_SIM_B_PASSWORD=$(random 32)|" \
    -e "s|^MM_SECRET_KEY=.*|MM_SECRET_KEY=$(random 48)|" \
    -e "s|^MM_ADMIN_PASSWORD=.*|MM_ADMIN_PASSWORD=$admin_password|" \
    -e "s|^MM_DEMO_DATA=.*|MM_DEMO_DATA=false|" \
    -e "s|^MM_DEMO_PASSWORD=.*|MM_DEMO_PASSWORD=$(random 20)|" \
    .env.example > .env
chmod 600 .env

echo ".env created for https://$domain with random passwords (mode 600)."
echo "Superadmin: $(sed -n 's/^MM_ADMIN_USERNAME=//p' .env) / $admin_password  - store it in a password manager now."
echo "Next: fill in MM_BACKUP_S3_* in .env, keep a copy of .env in a safe place, then run: make prod-up"
