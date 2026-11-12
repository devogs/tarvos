#!/usr/bin/env bash
# Configure the Netopeer2 SSH listener, allowed NETCONF users and NACM in the Sysrepo
# startup and running datastores. Must run BEFORE netopeer2-server starts. Idempotent (full replace).
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

# Access control: Netopeer2's default NACM denies writes, so nothing could be configured.
# Group "admin" (user admin) gets full access, except that the TACACS+ shared secret can
# be written but never read back. That rule needs the tarvos-aaa module, which is only
# present when the YANG directory is mounted, so it is added conditionally (otherwise the
# whole NACM import fails on an unknown path).
secret_rule=""
if sysrepoctl -l 2>/dev/null | grep -qE '^tarvos-aaa[[:space:]]'; then
    secret_rule='<rule>
      <name>hide-tacacs-secret</name>
      <module-name>tarvos-aaa</module-name>
      <path xmlns:taaa="urn:tarvos:params:xml:ns:yang:tarvos-aaa">/taaa:aaa/taaa:tacacs/taaa:server/taaa:secret</path>
      <access-operations>read</access-operations>
      <action>deny</action>
    </rule>'
else
    log "tarvos-aaa not installed: skipping the TACACS+ secret read-protection rule"
fi
nacm="<nacm xmlns=\"urn:ietf:params:xml:ns:yang:ietf-netconf-acm\">
  <enable-nacm>true</enable-nacm>
  <read-default>deny</read-default>
  <write-default>deny</write-default>
  <exec-default>deny</exec-default>
  <groups>
    <group>
      <name>admin</name>
      <user-name>admin</user-name>
    </group>
  </groups>
  <rule-list>
    <name>admin-full-access</name>
    <group>admin</group>
    ${secret_rule}
    <rule>
      <name>permit-all</name>
      <module-name>*</module-name>
      <access-operations>*</access-operations>
      <action>permit</action>
    </rule>
  </rule-list>
</nacm>"
for datastore in startup running; do
    if ! echo "${nacm}" | sysrepocfg --import -d "${datastore}" -f xml -m ietf-netconf-acm >&2; then
        log "ERROR: could not write NACM configuration to ${datastore}"
        exit 1
    fi
done

log "SSH listener on port ${NETCONF_PORT}, user admin enabled with full NACM access"
