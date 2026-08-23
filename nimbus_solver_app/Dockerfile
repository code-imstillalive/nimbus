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
# whole app runs, plus the real writer script it wraps. NIMBUS_SOLVER_
# PATH (set in the run script) points the writer script's own sys.path.
# insert() at wherever this lands.
#
# 2026-08-23 (issue #48): these used to COPY straight from
# custom_components/nimbus_load/solver/ and docs/real-world-integration/
# files/ -- both OUTSIDE this add-on's own directory. That works in CI
# only because PR #47's workflow widens the build context to the repo
# root; a real HA Supervisor build always roots the context at the
# add-on's own folder (nimbus_solver_app/), so those two COPYs fail
# identically on any real install (Docker can't COPY a path outside its
# build context, full stop -- confirmed by the CI failure Mark filed
# this issue against). Fixed by keeping genuine, git-tracked COPIES of
# both trees INSIDE this add-on directory (solver/,
# nimbus_solver_forecast_writer.py) -- the same synced-copy convention
# already used for the writer script across 3 other locations in this
# repo (nimbus's own solver_writer.py, the portable docs/ copy, and
# 116KAT-HA-AI's own scripts/ copy). Keep these in sync manually after
# any change to the real source (custom_components/nimbus_load/solver/,
# docs/real-world-integration/files/nimbus_solver_forecast_writer.py) --
# `diff -rq` both before committing, same discipline as the other 3
# copies.
COPY solver /opt/nimbus_solver/solver
COPY nimbus_solver_forecast_writer.py \
     /opt/nimbus_solver/nimbus_solver_forecast_writer.py

# Copy root filesystem (the s6-overlay service definition -- see
# rootfs/etc/services.d/nimbus_solver/)
COPY rootfs /

LABEL \
    org.opencontainers.image.title="Nimbus Solver" \
    org.opencontainers.image.description="Runs the Nimbus LP dispatch Solver against live HA sensors and publishes a battery/grid plan forecast." \
    org.opencontainers.image.source="https://github.com/code-imstillalive/nimbus" \
    org.opencontainers.image.licenses="MIT"
