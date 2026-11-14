FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm
# The image's Yarn APT source fails signature checks (NO_PUBKEY), which stops the
# sshd feature's apt-get update before post-create runs. The parser does not use Yarn.
# Remove only that source; Debian signature checks stay on and no key is imported.
RUN set -eu; \
    yarn_sources="$(grep -l 'dl\.yarnpkg\.com' /etc/apt/sources.list.d/* 2>/dev/null || true)"; \
    test -n "$yarn_sources" || { echo 'No Yarn APT source found; inspect the image.' >&2; exit 1; }; \
    rm -f $yarn_sources
