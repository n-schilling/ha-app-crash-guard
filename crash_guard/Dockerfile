# Pinned by version and digest: this image runs with full control over the
# host, so a moved tag must not change it unnoticed; Dependabot proposes updates
FROM ghcr.io/home-assistant/base-debian:trixie-2026.11.0@sha256:d996c8f8ef0096a1ac3756766149b31b8171afc663068cc192f2bcab20f1b8c9

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
# The store icon, also at the left of the page's header (served as icon.png)
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
