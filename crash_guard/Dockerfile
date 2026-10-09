ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:trixie
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

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
    && rm -rf /var/lib/apt/lists/*

COPY rootfs /

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION
# s6-overlay: warnings and errors only, no per-service start/stop info lines
ENV S6_VERBOSITY=1
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION}
