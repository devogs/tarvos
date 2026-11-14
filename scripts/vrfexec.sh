#!/usr/bin/env bash
# vrfexec: run a command in a VRF (installed as /usr/local/bin/vrfexec).
#
#   vrfexec                      list the VRFs
#   vrfexec <vrf> <command...>   run <command> with its sockets in <vrf>
#   vrfexec <vrf>                open a shell in <vrf>
#
# <vrf> is "default", the management VRF ("mgmt") or any VRF device (tenant VRFs created
# over NETCONF need no extra setup). Works as root and as unprivileged users (admin,
# TACACS+ users): sockets are bound with SO_BINDTODEVICE by libtarvos-vrf.so instead of
# "ip vrf exec", which needs root and a writable cgroup hierarchy.
#
# Limit: name resolution done inside glibc (getaddrinfo: curl, nc, ping <name>) is not
# bound to the VRF and uses the node resolver (/etc/resolv.conf); dig, nslookup and host
# send their queries from the VRF.
set -euo pipefail

LIB=libtarvos-vrf.so

list_vrfs() {
    echo default
    ip -o link show type vrf 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | sort
}

usage() {
    cat >&2 <<'USAGE'
usage: vrfexec <vrf> [command [args...]]
       vrfexec            (list VRFs)
examples:
  vrfexec RED ping 10.1.100.12
  vrfexec mgmt dig @192.0.2.53 example.com
  vrfexec RED traceroute -n 10.10.2.1
  vrfexec RED            (shell whose commands all run in RED)
USAGE
    exit 2
}

case "${1:-}" in
    "" | -l | --list)
        list_vrfs
        exit 0
        ;;
    -h | --help) usage ;;
esac

vrf="$1"
shift
if [ $# -eq 0 ]; then
    set -- "${SHELL:-/bin/bash}"
fi

# Strip a previous vrfexec from the environment (nested calls, "vrfexec default" from a
# VRF shell).
preload=""
for lib in ${LD_PRELOAD:-}; do
    [ "${lib}" = "${LIB}" ] || preload="${preload:+${preload} }${lib}"
done

if [ "${vrf}" = default ]; then
    unset TARVOS_VRF
    if [ -n "${preload}" ]; then export LD_PRELOAD="${preload}"; else unset LD_PRELOAD; fi
    exec "$@"
fi

if ! [[ "${vrf}" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || ! ip -d -o link show dev "${vrf}" 2>/dev/null | grep -q ' vrf table '; then
    echo "vrfexec: no VRF named '${vrf}' (VRFs: $(list_vrfs | tr '\n' ' '))" >&2
    exit 2
fi

export TARVOS_VRF="${vrf}"
export LD_PRELOAD="${LIB}${preload:+ ${preload}}"
exec "$@"
