#!/bin/sh
set -eu

EXPECTED_HOST="moon-poro-web"
SITE_SOURCE="${1:-}"
CADDY_SOURCE="${2:-}"

if [ "$(hostname)" != "$EXPECTED_HOST" ]; then
	echo "Refusing to run on host $(hostname); expected $EXPECTED_HOST." >&2
	exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
	echo "Run this script with sudo." >&2
	exit 1
fi

if [ ! -d "$SITE_SOURCE" ] || [ ! -f "$CADDY_SOURCE" ]; then
	echo "Usage: sudo bootstrap-web.sh SITE_DIST_DIR CADDYFILE" >&2
	exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates caddy nftables unattended-upgrades

# A small swap file prevents a transient memory spike from killing PostgreSQL
# or the Discord bot after they are migrated onto this 1 GB VM.
if ! /sbin/swapon --show=NAME --noheadings | grep -qx /swapfile; then
	if [ ! -f /swapfile ]; then
		dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
		chmod 600 /swapfile
		/sbin/mkswap /swapfile >/dev/null
	fi
	/sbin/swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab

cat > /etc/sysctl.d/99-moon-poro.conf <<'SYSCTL'
vm.swappiness=10
vm.vfs_cache_pressure=50
SYSCTL
/sbin/sysctl --system >/dev/null

install -d -o root -g root -m 0755 /opt/moon-poro/site/dist
find /opt/moon-poro/site/dist -mindepth 1 -delete
cp -a "$SITE_SOURCE"/. /opt/moon-poro/site/dist/
find /opt/moon-poro/site/dist -type d -exec chmod 0755 {} +
find /opt/moon-poro/site/dist -type f -exec chmod 0644 {} +

install -o root -g root -m 0644 "$CADDY_SOURCE" /etc/caddy/Caddyfile
/usr/bin/caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null
systemctl restart caddy

# SSH remains reachable only through the Google IAP firewall rule. Passwords,
# root login and forwarding are disabled at the daemon as a second layer.
install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/60-moon-poro.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
MaxAuthTries 3
SSHD
/usr/sbin/sshd -t
systemctl reload ssh

# Host firewall mirrors the GCP policy: public web traffic and SSH only from
# Google's documented IAP TCP-forwarding range.
cat > /etc/nftables.conf <<'NFT'
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
	chain input {
		type filter hook input priority 0; policy drop;
		ct state invalid drop
		ct state established,related accept
		iifname "lo" accept
		ip protocol icmp accept
		ip6 nexthdr ipv6-icmp accept
		ip saddr 35.235.240.0/20 tcp dport 22 accept
		tcp dport { 80, 443 } accept
	}

	chain forward {
		type filter hook forward priority 0; policy drop;
	}

	chain output {
		type filter hook output priority 0; policy accept;
	}
}
NFT
/usr/sbin/nft -c -f /etc/nftables.conf
systemctl enable nftables >/dev/null
systemctl restart nftables

systemctl enable unattended-upgrades >/dev/null
systemctl start unattended-upgrades

echo "Moon Poro web bootstrap completed."
