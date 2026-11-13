#!/usr/bin/env bash
# Register/update YANG modules from ${YANG_DIR} into the Sysrepo datastore.
# Built-in modules (baked into the image at ${BUILTIN_YANG_DIR}) are registered first; modules in
# ${YANG_DIR} (a mounted volume) are added and win when they carry the same module name.
# Must run BEFORE sysrepo-plugind / netopeer2-server start so that the NETCONF
# <hello> advertises every module. Idempotent: unchanged modules are skipped.
set -uo pipefail

YANG_DIR="${YANG_DIR:-/opt/tarvos/yangs}"
BUILTIN_YANG_DIR="${BUILTIN_YANG_DIR:-/opt/tarvos/yangs-builtin}"
log() { echo "[yang] $*" >&2; }

failures=0
shopt -s nullglob
# Module name -> file path; the mounted directory is read last so it overrides built-ins.
declare -A by_module=()
for dir in "${BUILTIN_YANG_DIR}" "${YANG_DIR}"; do
    for f in "${dir}"/*.yang; do
        m="$(sed -nE 's/^[[:space:]]*module[[:space:]]+([A-Za-z0-9_.-]+).*/\1/p' "${f}" | head -n1)"
        by_module["${m:-${f}}"]="${f}"
    done
done
files=("${by_module[@]}")
if [ "${#files[@]}" -eq 0 ]; then
    log "no YANG files in ${BUILTIN_YANG_DIR} or ${YANG_DIR}"
    exit 0
fi

installed="$(sysrepoctl -l 2>/dev/null || true)"

# Echo the installed revision of module $1 (empty if not installed).
installed_revision() {
    awk -F'|' -v name="$1" '
        { for (i = 1; i <= NF; i++) gsub(/^[ \t]+|[ \t]+$/, "", $i) }
        $1 == name || $2 == name { print ($1 == name ? $2 : $3); exit }
    ' <<<"${installed}"
}

for file in "${files[@]}"; do
    # Exact module name and latest revision come from the file content, not its name.
    name="$(sed -nE 's/^[[:space:]]*module[[:space:]]+([A-Za-z0-9_.-]+).*/\1/p' "${file}" | head -n1)"
    revision="$(sed -nE 's/^[[:space:]]*revision[[:space:]]+"?([0-9-]{10})"?.*/\1/p' "${file}" | sort | tail -n1)"
    if [ -z "${name}" ]; then
        log "ERROR: ${file} has no module statement"
        failures=$((failures + 1))
        continue
    fi

    current="$(installed_revision "${name}")"
    if [ -z "${current}" ]; then
        log "installing ${name}@${revision}"
        sysrepoctl -s "$(dirname "${file}")" -i "${file}" || failures=$((failures + 1))
    elif [ -n "${revision}" ] && [ "${current}" != "${revision}" ]; then
        log "updating ${name} ${current} -> ${revision}"
        sysrepoctl -s "$(dirname "${file}")" -U "${file}" || failures=$((failures + 1))
    else
        log "${name}@${current} already registered"
    fi
done

[ "${failures}" -eq 0 ] || { log "${failures} module(s) failed to register"; exit 1; }
