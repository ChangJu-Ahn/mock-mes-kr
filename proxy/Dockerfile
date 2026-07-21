# Tiny Caddy proxy image with a baked-in Caddyfile.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 8080
# Base image entrypoint runs: caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
