# Backups of the database to S3-compatible storage (ADR 0010).
# pg_dump / pg_restore of the same PostgreSQL 16 + TimescaleDB as the server; rclone (a static
# binary from its official image) for S3. Build context: repository root.
FROM rclone/rclone:1.76.0 AS rclone

FROM timescale/timescaledb:2.32.0-pg16
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY deploy/backup/mm-backup.sh /usr/local/bin/mm-backup
RUN chmod 0755 /usr/local/bin/mm-backup && mkdir -p /work
WORKDIR /work
ENTRYPOINT ["mm-backup"]
CMD ["schedule"]
