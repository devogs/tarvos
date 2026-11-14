#!/usr/bin/env bash
# Tarvos container entrypoint (runs under tini as PID 1).
# Boot order: AAA -> sshd -> YANG registration -> sysrepo-plugind -> netopeer2
#             -> LLDP -> FRR -> Python backend daemons (netdev, FRR, LLDP, AAA, QoS,
#             nftables) -> eBPF telemetry agent.
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

# name:pid and name:command of supervised foreground daemons
declare -A PIDS=() CMDS=() RESTARTS=() WINDOW=()
# Critical daemons: without them the node cannot be managed, or NETCONF would accept
# configuration nobody applies. They are restarted like the others, but a crash loop
# (CRASH_LOOP_MAX restarts within CRASH_LOOP_WINDOW seconds) stops the container so the
# orchestrator can restart it.
CRITICAL=(sshd sysrepo-plugind netopeer2-server frr_sync netdev_sync kernel_sync)
CRASH_LOOP_MAX="${CRASH_LOOP_MAX:-5}"
CRASH_LOOP_WINDOW="${CRASH_LOOP_WINDOW:-120}"
shutting_down=0

start() {
    local name="$1"
    shift
    CMDS["${name}"]="$(printf '%q ' "$@")"
    "$@" &
    PIDS["${name}"]=$!
    log "started ${name} (pid ${PIDS[${name}]})"
}

is_critical() {
    local c
    for c in "${CRITICAL[@]}"; do [ "$c" = "$1" ] && return 0; done
    return 1
}

# restart <name>: start a daemon again after it exited, with a short backoff (1 s per
# recent restart, at most 10 s). Returns 1 when a critical daemon is crash looping.
restart() {
    local name="$1" now
    now=$(date +%s)
    if [ $((now - ${WINDOW[${name}]:-0})) -gt "${CRASH_LOOP_WINDOW}" ]; then
        WINDOW["${name}"]=${now}
        RESTARTS["${name}"]=0
    fi
    RESTARTS["${name}"]=$((${RESTARTS[${name}]:-0} + 1))
    if is_critical "${name}" && [ "${RESTARTS[${name}]}" -gt "${CRASH_LOOP_MAX}" ]; then
        log "critical daemon ${name} restarted ${CRASH_LOOP_MAX} times in ${CRASH_LOOP_WINDOW}s: giving up"
        return 1
    fi
    local delay=$((RESTARTS[${name}] < 10 ? RESTARTS[${name}] : 10))
    log "${name} exited; restarting in ${delay}s (restart ${RESTARTS[${name}]})"
    sleep "${delay}" &
    wait $! 2>/dev/null || true # interruptible: SIGTERM is handled at once
    [ "${shutting_down}" -eq 1 ] && return 0
    eval "${CMDS[${name}]} &"
    PIDS["${name}"]=$!
    log "restarted ${name} (pid ${PIDS[${name}]})"
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
# -t: timeout of every Sysrepo call made for a NETCONF request, including the backends'
# change callbacks (Sysrepo's default, 5 s, is too short for large edits: 100 VNIs).
NETCONF_TIMEOUT="${NETCONF_TIMEOUT:-120}"
[[ "${NETCONF_TIMEOUT}" =~ ^[0-9]{1,4}$ ]] || NETCONF_TIMEOUT=120
start netopeer2-server netopeer2-server -d -v "${DAEMON_VERBOSITY}" -t "${NETCONF_TIMEOUT}"

lldp_running=0
# --- 4b. LLDP (lldpd); optional and never fatal -----------------------------------
python3 "${SCRIPTS_DIR}/lldp_configure.py"
lldp_rc=$?
if [ "${lldp_rc}" -eq 0 ]; then
    mkdir -p /run/lldpd
    start lldpd /usr/sbin/lldpd -d
    lldp_running=1
elif [ "${lldp_rc}" -ne 10 ]; then
    log "WARNING: LLDP configuration failed"
fi

# --- 5. FRR -----------------------------------------------------------------------
if [ ! -s /etc/frr/frr.conf ]; then
    printf 'frr defaults traditional\nlog syslog informational\n!\n' >/etc/frr/frr.conf
    chown frr:frr /etc/frr/frr.conf
    chmod 640 /etc/frr/frr.conf
fi
"${SCRIPTS_DIR}/frr_configure.sh" || log "WARNING: FRR daemon selection failed"
/usr/lib/frr/frrinit.sh start >&2 || log "WARNING: FRR failed to start"

# --- 6. Python backend daemons ----------------------------------------------------
# Kernel VRF/bridge/VXLAN devices (tarvos-vrf, tarvos-vxlan), then FRR: native FRR YANG
# through mgmtd, the other daemons through vtysh/frr-reload.
# watchfrr supervises the FRR daemons but is itself started by frrinit.sh: a supervised
# guard restarts FRR when watchfrr is gone (configuration comes back from frr.conf and
# frr_sync's periodic re-application).
start frr_guard bash -c 'while sleep 5; do
    pgrep -x watchfrr >/dev/null || { echo "[frr-guard] watchfrr is gone: restarting FRR" >&2; /usr/lib/frr/frrinit.sh restart >&2; }
done'
start netdev_sync python3 "${DAEMONS_DIR}/netdev_sync.py"
start frr_sync python3 "${DAEMONS_DIR}/frr_sync.py"
# LLDP: applies tarvos-lldp NETCONF configuration to lldpd and serves its neighbours.
if [ "${lldp_running}" -eq 1 ]; then
    start lldp_sync python3 "${DAEMONS_DIR}/lldp_sync.py"
fi
# Kernel backends of the remaining Tarvos models, in one process: runtime TACACS+
# (tarvos-aaa), tc QoS (tarvos-qos), nftables CoPP (tarvos-nftables). A backend whose
# module is not installed is skipped.
start kernel_sync python3 "${DAEMONS_DIR}/kernel_sync.py"
# eBPF telemetry agent (counters for the Prometheus and gNMI exporters). Not critical:
# without CAP_BPF/CAP_PERFMON or kernel BTF it logs why and the node keeps working.
case "${TELEMETRY_ENABLED:-yes}" in
    yes | true | 1)
        # YANG list keys and module list for the gNMI server (all modules are registered).
        python3 "${SCRIPTS_DIR}/yang_index.py" /run/tarvos/yang-index.json ||
            log "WARNING: YANG index failed; gNMI serves Sysrepo data as JSON only"
        start telemetry /usr/local/bin/tarvos-telemetry run
        ;;
    *) log "telemetry disabled (TELEMETRY_ENABLED=${TELEMETRY_ENABLED})" ;;
esac
# Optional user-selected daemons
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

# --- Supervise: restart any daemon that exits; stop on a critical crash loop -------
while true; do
    wait -n || true
    [ "${shutting_down}" -eq 1 ] && exit 0
    for name in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[${name}]}" 2>/dev/null; then
            if ! restart "${name}"; then
                stop_all
                exit 1
            fi
        fi
    done
done
