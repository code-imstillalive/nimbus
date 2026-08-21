# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# Real, compiled dependency this whole app exists for -- highspy has
# published wheels for amd64/aarch64 (this base image's musllinux/Alpine
# target, confirmed 2026-08-21 against the real PyPI file listing) but
# NOT for armv7/i386, matching config.yaml's own arch: list above. If
# this RUN step ever starts failing on a "no matching wheel" error for
# an architecture NOT already excluded from arch:, that's real, new
# information -- don't just add --no-binary and let it silently fall
# back to a from-source build without deliberately deciding to support
# that.
RUN apk add --no-cache python3 py3-pip py3-numpy \
    && pip3 install --break-system-packages --no-cache-dir highspy

# The pure-Python solver/ package (network.py/elements.py/lp.py) this
# whole app runs, plus the real writer script it wraps -- both copied
# straight from this same repo's own tracked source, never duplicated/
# forked. NIMBUS_SOLVER_PATH (set in the run script) points the writer
# script's own sys.path.insert() at wherever this lands.
COPY custom_components/nimbus_load/solver /opt/nimbus_solver/solver
COPY docs/real-world-integration/files/nimbus_solver_forecast_writer.py \
     /opt/nimbus_solver/nimbus_solver_forecast_writer.py

# Copy root filesystem (the s6-overlay service definition -- see
# rootfs/etc/services.d/nimbus_solver/)
COPY rootfs /

LABEL \
    org.opencontainers.image.title="Nimbus Solver" \
    org.opencontainers.image.description="Runs the Nimbus LP dispatch Solver against live HA sensors and publishes a battery/grid plan forecast." \
    org.opencontainers.image.source="https://github.com/code-imstillalive/nimbus" \
    org.opencontainers.image.licenses="MIT"
