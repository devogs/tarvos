#!/usr/bin/env bash
# Configure the Netopeer2 SSH listener and allowed NETCONF users in the Sysrepo startup
# datastore. Must run BEFORE netopeer2-server starts. Idempotent (full replace).
#
# Netopeer2 only lets users listed in ietf-netconf-server log in. "admin" is listed with
# keyboard-interactive/use-system-auth, so authentication goes through /etc/pam.d/netopeer2
# (local password, or TACACS+ when configured).
set -uo pipefail

NETCONF_PORT="${NETCONF_PORT:-830}"
log() { echo "[netconf] $*" >&2; }

if ! [[ "${NETCONF_PORT}" =~ ^[0-9]{1,5}$ ]] || [ "${NETCONF_PORT}" -lt 1 ] || [ "${NETCONF_PORT}" -gt 65535 ]; then
    log "ERROR: invalid NETCONF_PORT '${NETCONF_PORT}'"
    exit 1
fi

config="<netconf-server xmlns=\"urn:ietf:params:xml:ns:yang:ietf-netconf-server\">
  <listen>
    <endpoints>
      <endpoint>
        <name>default-ssh</name>
        <ssh>
          <tcp-server-parameters>
            <local-bind>
              <local-address>0.0.0.0</local-address>
              <local-port>${NETCONF_PORT}</local-port>
            </local-bind>
          </tcp-server-parameters>
          <ssh-server-parameters>
            <server-identity>
              <host-key>
                <name>default-key</name>
                <public-key>
                  <central-keystore-reference>genkey</central-keystore-reference>
                </public-key>
              </host-key>
            </server-identity>
            <client-authentication>
              <users>
                <user>
                  <name>admin</name>
                  <keyboard-interactive xmlns=\"urn:cesnet:libnetconf2-netconf-server\">
                    <use-system-auth/>
                  </keyboard-interactive>
                </user>
              </users>
            </client-authentication>
          </ssh-server-parameters>
        </ssh>
      </endpoint>
    </endpoints>
  </listen>
</netconf-server>"

# The whole ietf-netconf-server config is REPLACED (not merged): the build-time install of
# Netopeer2 leaves a default listener and a user for whoever ran it (root), which must not
# survive. Sysrepo persists "running" separately from "startup", so both are written.
for datastore in startup running; do
    if ! echo "${config}" | sysrepocfg --import -d "${datastore}" -f xml -m ietf-netconf-server >&2; then
        log "ERROR: could not write ietf-netconf-server configuration to ${datastore}"
        exit 1
    fi
done
log "SSH listener on port ${NETCONF_PORT}, user admin enabled"
