# Pinned by version and digest: this image runs with full control over the
# host, so a moved tag must not change it unnoticed; Dependabot proposes updates
FROM ghcr.io/home-assistant/base-debian:trixie-2026.11.1@sha256:a7e1db1748f4b3c2d5c01d680f5f85ab32bfd3a0d320d1a9647e6a74e049ed9e

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# systemd only for journalctl (reads the host journal under /var/log/journal),
# device-tree-compiler for the RAM lock overlay, mosquitto-clients for the
# entities
RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        device-tree-compiler \
        jq \
        mosquitto-clients \
        python3 \
        systemd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin cgweb

COPY rootfs /
# The store icon, served as icon.png (the header of a single view shows it)
COPY icon.png /usr/local/share/ha-app/icon.png

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION
# s6-overlay: warnings and errors only, no per-service start/stop info lines.
# The version from config.yaml (the Supervisor passes it as BUILD_VERSION) for
# the bottom of the web page
ENV S6_VERBOSITY=1 \
    APP_VERSION=${BUILD_VERSION}
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION}
