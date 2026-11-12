#!/usr/bin/env bash
# Tarvos container entrypoint (runs under tini as PID 1).
# Boot order: AAA -> sshd -> YANG registration -> sysrepo-plugind -> netopeer2
#             -> FRR -> Python backend daemons.
# All configuration comes from the environment (see .env.example).
set -uo pipefail

LOG_LEVEL="${LOG_LEVEL:-info}"
YANG_DIR="${YANG_DIR:-/opt/tarvos/yangs}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/opt/tarvos/scripts}"
DAEMONS_DIR="${DAEMONS_DIR:-/opt/tarvos/daemons}"
export YANG_DIR

log() { echo "[entrypoint] $*" >&2; }

case "${LOG_LEVEL}" in
    debug | info | error) DAEMON_VERBOSITY="${LOG_LEVEL}" ;;
    warn | warning) DAEMON_VERBOSITY="warning" ;;
    *) DAEMON_VERBOSITY="info" ;;
esac

# name:pid of supervised foreground daemons
declare -A PIDS=()
CRITICAL=(sshd sysrepo-plugind netopeer2-server)
shutting_down=0

start() {
    local name="$1"
    shift
    "$@" &
    PIDS["${name}"]=$!
    log "started ${name} (pid ${PIDS[${name}]})"
}

stop_all() {
    shutting_down=1
    log "shutting down"
    /usr/lib/frr/frrinit.sh stop >&2 || true
    local name
    for name in "${!PIDS[@]}"; do
        kill -TERM "${PIDS[${name}]}" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap 'stop_all; exit 0' SIGTERM SIGINT

# --- 0a. rsyslog: /dev/log -> stdout (+ optional remote collectors) ---------------
python3 "${SCRIPTS_DIR}/syslog_configure.py" || log "WARNING: syslog configuration failed"
mkdir -p /var/spool/rsyslog
start rsyslogd rsyslogd -n -f /etc/rsyslog.conf
for _ in $(seq 1 50); do
    [ -S /dev/log ] && break
    sleep 0.1
done

# --- 0b. Management VRF (eth0 -> vrf mgmt); never fatal ----------------------------
"${SCRIPTS_DIR}/mgmt_vrf.sh" || log "WARNING: management VRF setup failed"

# --- 1. AAA: admin account, PAM, NSS, sshd config --------------------------------
python3 "${SCRIPTS_DIR}/aaa_configure.py" || { log "AAA configuration failed"; exit 1; }

# --- 2. sshd (foreground, logs to syslog) ----------------------------------------
mkdir -p /run/sshd
ssh-keygen -A >/dev/null
start sshd /usr/sbin/sshd -D

# --- 3. YANG modules -> Sysrepo (before plugind/netopeer2) ----------------------
"${SCRIPTS_DIR}/register_yangs.sh" || log "WARNING: some YANG modules failed to register"

# --- 4. Sysrepo plugin daemon and Netopeer2 ---------------------------------------
start sysrepo-plugind sysrepo-plugind -d -v "${DAEMON_VERBOSITY}"

# Per-container SSH host key for NETCONF (never baked into the image).
hostkey_marker=/var/lib/tarvos/netopeer2-hostkey.done
if [ ! -f "${hostkey_marker}" ]; then
    mkdir -p /var/lib/tarvos
    if /usr/local/share/netopeer2/scripts/merge_hostkey.sh >&2; then
        touch "${hostkey_marker}"
    else
        log "WARNING: could not generate NETCONF host key"
    fi
fi
"${SCRIPTS_DIR}/netconf_configure.sh" || log "WARNING: NETCONF listener configuration failed"
start netopeer2-server netopeer2-server -d -v "${DAEMON_VERBOSITY}"

# --- 5. FRR -----------------------------------------------------------------------
if [ ! -s /etc/frr/frr.conf ]; then
    printf 'frr defaults traditional\nlog syslog informational\n!\n' >/etc/frr/frr.conf
    chown frr:frr /etc/frr/frr.conf
    chmod 640 /etc/frr/frr.conf
fi
sed -i -E 's/^(bgpd|ospfd|isisd|bfdd)=no/\1=yes/' /etc/frr/daemons
/usr/lib/frr/frrinit.sh start >&2 || log "WARNING: FRR failed to start"

# --- 6. Python backend daemons (optional) ----------------------------------------
for daemon in ${TARVOS_DAEMONS:-}; do
    case "${daemon}" in
        *[!A-Za-z0-9_.-]*) log "skipping invalid daemon name: ${daemon}" ;;
        *)
            if [ -f "${DAEMONS_DIR}/${daemon}" ]; then
                start "${daemon}" python3 "${DAEMONS_DIR}/${daemon}"
            else
                log "daemon not found: ${daemon}"
            fi
            ;;
    esac
done

log "Tarvos is up"

# --- Supervise: exit (so the orchestrator restarts us) if a critical daemon dies --
while true; do
    wait -n || true
    [ "${shutting_down}" -eq 1 ] && exit 0
    for name in "${CRITICAL[@]}"; do
        if ! kill -0 "${PIDS[${name}]}" 2>/dev/null; then
            log "critical daemon ${name} exited"
            stop_all
            exit 1
        fi
    done
done
