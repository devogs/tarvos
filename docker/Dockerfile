# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: libyang, libnetconf2, sysrepo, netopeer2 and the Python bindings.
# Debian trixie ships none of them at the required versions, so they are built from pinned
# releases. FRR itself comes from the official FRR repository (runtime stage).
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS builder

ARG LIBYANG_VERSION=v5.8.6
ARG LIBNETCONF2_VERSION=v4.4.10
ARG SYSREPO_VERSION=v5.1.0
ARG NETOPEER2_VERSION=v2.8.7
ARG SYSREPO_PY_VERSION=2.0.0
ARG LIBYANG_PY_VERSION=5.0.0
ARG PYROUTE2_VERSION=0.7.12
ARG PAM_TACPLUS_VERSION=v1.7.0
ARG LIBNSS_ATO_VERSION=v0.2.0
# Must match FRR_VERSION in the runtime stage: the FRR YANG modules loaded into Sysrepo are
# taken from the same release tag, so NETCONF serves exactly the schema mgmtd implements.
ARG FRR_TAG=frr-10.7.1

ENV DEBIAN_FRONTEND=noninteractive \
    SYSREPO_REPOSITORY_PATH=/var/lib/sysrepo \
    SYSREPO_SHM_PREFIX=sr_build

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates pkg-config \
        libpcre2-dev libssl-dev libssh-dev libpam0g-dev libcurl4-openssl-dev \
        python3 python3-dev python3-venv libffi-dev \
        autoconf automake libtool gnulib curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deb.frrouting.org/frr/keys.gpg -o /usr/share/keyrings/frrouting.gpg

WORKDIR /src

RUN git clone --depth 1 --branch "${LIBYANG_VERSION}" https://github.com/CESNET/libyang.git \
    && cmake -S libyang -B libyang/build -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTS=OFF \
    && cmake --build libyang/build -j"$(nproc)" && cmake --install libyang/build && ldconfig

RUN git clone --depth 1 --branch "${SYSREPO_VERSION}" https://github.com/sysrepo/sysrepo.git \
    && cmake -S sysrepo -B sysrepo/build -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTS=OFF \
        -DREPO_PATH=/var/lib/sysrepo \
    && cmake --build sysrepo/build -j"$(nproc)" && cmake --install sysrepo/build && ldconfig

# Patch: in system-users mode, upstream checks the SSH "password" method against /etc/shadow
# only (PAM is used for keyboard-interactive alone). Clients such as ncclient/paramiko use
# "password", so TACACS+ users could not log in. The patch routes it through the same PAM
# service (netopeer2.conf).
COPY docker/patches/libnetconf2-password-pam.patch /src/patches/
RUN git clone --depth 1 --branch "${LIBNETCONF2_VERSION}" https://github.com/CESNET/libnetconf2.git \
    && git -C libnetconf2 apply /src/patches/libnetconf2-password-pam.patch \
    && cmake -S libnetconf2 -B libnetconf2/build -DCMAKE_BUILD_TYPE=Release -DENABLE_TESTS=OFF \
    && cmake --build libnetconf2/build -j"$(nproc)" && cmake --install libnetconf2/build && ldconfig

# Host key generation is disabled here: the key is created per container at first boot.
RUN git clone --depth 1 --branch "${NETOPEER2_VERSION}" https://github.com/CESNET/netopeer2.git \
    && cmake -S netopeer2 -B netopeer2/build -DCMAKE_BUILD_TYPE=Release \
        -DGENERATE_HOSTKEY=OFF -DMERGE_LISTEN_CONFIG=ON \
    && cmake --build netopeer2/build -j"$(nproc)" && cmake --install netopeer2/build

# NETCONF "system users" mode: with the local-users-supported feature of ietf-ssh-server off,
# libnetconf2 accepts any SSH username and authenticates it through PAM (so through TACACS+),
# instead of requiring every user to be listed in the Sysrepo config. The NETCONF session then
# carries the real username (for NACM and logs). Must be done here: it cannot be changed while
# Netopeer2 runs, and the install above leaves a user list that has to be emptied first.
# hadolint ignore=DL4006
RUN printf '%s\n' '<netconf-server xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-server"/>' \
        | sysrepocfg --import -d startup -f xml -m ietf-netconf-server \
    && sysrepoctl -c ietf-ssh-server \
        -d local-user-auth-publickey -d local-user-auth-password -d local-user-auth-none \
        -d local-users-supported

# FRR's native YANG modules, from the FRR release tag (the FRR packages embed them in the
# binaries and do not install the .yang files). Data modules:
# * mgmtd backends (pushed to mgmtd by frr_sync): frr-interface, frr-vrf, frr-routing,
#   frr-affinity-map, frr-zebra, frr-staticd, frr-ripd, frr-ripngd;
# * rendered through vtysh by frr_sync (their daemons are not mgmtd backends):
#   frr-filter, frr-route-map, frr-bgp-route-map, frr-bfdd.
# Imports (frr-nexthop, ietf-routing-types, ...) are pulled in by sysrepoctl automatically.
ENV FRR_YANG_DIR=/usr/local/share/yang/frr
RUN git clone --depth 1 --branch "${FRR_TAG}" --filter=blob:none --sparse \
        https://github.com/FRRouting/frr.git frr \
    && git -C frr sparse-checkout set yang \
    && mkdir -p "${FRR_YANG_DIR}" \
    && cp frr/yang/*.yang frr/yang/ietf/*.yang "${FRR_YANG_DIR}/" \
    && set -- \
    && for m in frr-interface frr-vrf frr-routing frr-affinity-map frr-zebra frr-staticd \
                frr-ripd frr-ripngd frr-filter frr-route-map frr-bgp-route-map frr-bfdd; do \
           set -- "$@" -i "${FRR_YANG_DIR}/${m}.yang"; \
       done \
    && sysrepoctl -s "${FRR_YANG_DIR}" "$@" \
    && sysrepoctl -l

# Neither pam_tacplus nor libnss-ato is packaged in Debian trixie.
# The PAM module lands in /usr/local/lib/security/pam_tacplus.so.
# gl_PREREQ_EXPLICIT_BZERO no longer exists in gnulib; it only matters for libcs
# without explicit_bzero, and glibc has it.
# hadolint ignore=DL3003
RUN git clone --depth 1 --branch "${PAM_TACPLUS_VERSION}" https://github.com/kravietz/pam_tacplus.git \
    && cd pam_tacplus \
    && gnulib-tool --makefile-name=Makefile.gnulib --libtool --import \
        fcntl crypto/md5 array-list list xlist getrandom realloc-posix explicit_bzero xalloc getopt-gnu \
    && sed -i '/gl_PREREQ_EXPLICIT_BZERO/d' configure.ac \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local --disable-doc --disable-werror \
    && make -j"$(nproc)" && make install && ldconfig

RUN git clone --depth 1 --branch "${LIBNSS_ATO_VERSION}" https://github.com/donapieppo/libnss-ato.git \
    && make -C libnss-ato libnss_ato \
    && install -D libnss-ato/libnss_ato.so.2 /usr/local/lib/libnss_ato.so.2 && ldconfig

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
        "libyang==${LIBYANG_PY_VERSION}" "sysrepo==${SYSREPO_PY_VERSION}" \
        "pyroute2==${PYROUTE2_VERSION}" \
    && /opt/venv/bin/pip uninstall -y pip setuptools

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM debian:trixie-slim AS runtime

# Official FRR packages (deb.frrouting.org), pinned to one release.
ARG FRR_VERSION=10.7.1-0~deb13u1

ENV DEBIAN_FRONTEND=noninteractive \
    SYSREPO_REPOSITORY_PATH=/var/lib/sysrepo \
    PATH=/opt/venv/bin:$PATH \
    YANG_DIR=/opt/tarvos/yangs

COPY --from=builder /usr/share/keyrings/frrouting.gpg /usr/share/keyrings/frrouting.gpg

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr trixie frr-10.7" \
        >/etc/apt/sources.list.d/frr.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "frr=${FRR_VERSION}" "frr-pythontools=${FRR_VERSION}" \
        tini openssh-server libpam-modules rsyslog lldpd \
        nftables iproute2 python3 \
        libpcre2-8-0 libssl3t64 libssh-4 libpam0g libcurl4t64 \
        procps ca-certificates \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /var/lib/sysrepo /var/lib/sysrepo
COPY --from=builder /opt/venv /opt/venv

# Local fallback account (password set at boot from ADMIN_PASSWORD, default "admin")
# and the shared account that TACACS+-authenticated users are mapped to.
RUN ldconfig \
    && groupadd -g 1001 tacacs \
    && useradd -m -s /bin/bash -G frrvty,frr admin \
    && useradd -m -u 1001 -g tacacs -G frrvty,frr -s /bin/bash tacacs \
    && mkdir -p /opt/tarvos/yangs /opt/tarvos/yangs-builtin /opt/tarvos/daemons /opt/tarvos/scripts /run/sshd /var/lib/tarvos

# Shipped modules live outside the YANG_DIR volume: GNS3 mounts an empty directory over it.
COPY yang/ /opt/tarvos/yangs-builtin/
COPY scripts/ /opt/tarvos/scripts/
COPY daemons/ /opt/tarvos/daemons/
RUN chmod 0755 /opt/tarvos/scripts/*.sh /opt/tarvos/scripts/*.py /opt/tarvos/daemons/*.py

EXPOSE 22 830
VOLUME ["/opt/tarvos/yangs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["sh", "-c", "pgrep -x sshd && pgrep -x netopeer2-serve && pgrep -x watchfrr && pgrep -f frr_sync.py"]

# The supervisor must run as root: sshd, PAM and netopeer2 need it. FRR daemons
# drop to the unprivileged "frr" user themselves.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/tarvos/scripts/entrypoint.sh"]
