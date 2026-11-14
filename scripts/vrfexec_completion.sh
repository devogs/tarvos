# shellcheck shell=bash
# Bash completion for vrfexec: VRF names from the kernel, then commands and their arguments.
_vrfexec() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    if [ "${COMP_CWORD}" -eq 1 ]; then
        mapfile -t COMPREPLY < <(compgen -W "$(vrfexec --list 2>/dev/null)" -- "${cur}")
    elif [ "${COMP_CWORD}" -eq 2 ]; then
        mapfile -t COMPREPLY < <(compgen -c -- "${cur}")
    else
        mapfile -t COMPREPLY < <(compgen -f -- "${cur}")
    fi
}
complete -F _vrfexec vrfexec
