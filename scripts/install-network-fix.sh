#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
daemon_config="${project_root}/config/docker-daemon.json"

if (( EUID != 0 )); then
    printf 'install-network-fix: rerun with sudo: sudo %q' "${BASH_SOURCE[0]}" >&2
    printf '\n' >&2
    exit 1
fi

[[ -f "${daemon_config}" && ! -L "${daemon_config}" ]] || {
    printf 'install-network-fix: invalid daemon config: %s\n' "${daemon_config}" >&2
    exit 1
}

if [[ -e /etc/docker/daemon.json ]]; then
    if /usr/bin/jq -e '."ip-forward-no-drop" == true' \
        /etc/docker/daemon.json >/dev/null 2>&1; then
        printf 'Docker already has ip-forward-no-drop enabled.\n'
    else
        printf '%s\n' \
            'install-network-fix: /etc/docker/daemon.json already exists without the required setting; refusing to overwrite it' >&2
        exit 1
    fi
else
    /usr/bin/dockerd --validate --config-file "${daemon_config}"
    /usr/bin/install -d -o root -g root -m 0755 /etc/docker
    /usr/bin/install -o root -g root -m 0644 -- "${daemon_config}" \
        /etc/docker/daemon.json
fi

# Apply the documented Waydroid setting immediately.  Docker's daemon option
# prevents future daemon starts from changing it back to DROP.
/usr/bin/iptables -w 5 -P FORWARD ACCEPT

# Remove only the exact temporary rules used by the first live repair.  They
# are redundant once the standard forwarding policy is configured.
while /usr/bin/iptables -w 5 -C DOCKER-USER -i waydroid0 -j ACCEPT \
    >/dev/null 2>&1; do
    /usr/bin/iptables -w 5 -D DOCKER-USER -i waydroid0 -j ACCEPT
done
while /usr/bin/iptables -w 5 -C DOCKER-USER -o waydroid0 \
    -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT >/dev/null 2>&1; do
    /usr/bin/iptables -w 5 -D DOCKER-USER -o waydroid0 \
        -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
done

/usr/bin/dockerd --validate --config-file /etc/docker/daemon.json
/usr/bin/jq -e '."ip-forward-no-drop" == true' \
    /etc/docker/daemon.json >/dev/null
[[ "$(/usr/bin/iptables -w 5 -S FORWARD | /usr/bin/head -n 1)" == '-P FORWARD ACCEPT' ]]

printf 'Docker ip-forward-no-drop is persisted and FORWARD is ACCEPT now.\n'
printf 'Docker was not restarted; running containers were left untouched.\n'
