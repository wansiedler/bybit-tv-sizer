# VPS tunnel: a fixed egress IP for the bot (and a VPN for your devices)

The Bybit API key is locked to one IP. The home line's address is dynamic,
so the bot's HTTPS leaves through a rented VPS with a permanent address —
`HTTPS_PROXY` in `bipboop` points at a proxy that is only reachable inside a
WireGuard tunnel. The same server doubles as a personal VPN for the iPad.

```text
container ──HTTPS_PROXY──▶ 10.77.0.1:8888 (tinyproxy)
        Mac ──wg0 (utun)──▶ VPS <VPS_IP> ──▶ Bybit / Telegram / Sheets
       iPad ──WireGuard────▶ VPS (full tunnel, NAT)
```

## The server (Hetzner CPX12, Ubuntu 24.04, Falkenstein)

```bash
apt-get update && apt-get install -y wireguard tinyproxy ufw
```

### WireGuard — `/etc/wireguard/wg0.conf`

```ini
[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = <wg genkey>
# NAT so full-tunnel peers (the iPad) reach the internet; the bot's proxy
# does not need it, but it does not hurt it either.
PostUp = iptables -t nat -A POSTROUTING -s 10.77.0.0/24 -o eth0 -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s 10.77.0.0/24 -o eth0 -j MASQUERADE

[Peer]
# the mac
PublicKey = <mac public key>
AllowedIPs = 10.77.0.2/32

[Peer]
# the ipad
PublicKey = <ipad public key>
AllowedIPs = 10.77.0.3/32
```

```bash
systemctl enable --now wg-quick@wg0
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-wg-forward.conf
sysctl -w net.ipv4.ip_forward=1
```

### tinyproxy — `/etc/tinyproxy/tinyproxy.conf`

Listens on the tunnel address only; the internet cannot see it.

```ini
User tinyproxy
Group tinyproxy
Port 8888
Listen 10.77.0.1
Timeout 600
MaxClients 50
Allow 10.77.0.0/24
LogLevel Warning
```

tinyproxy must start after wg0 owns 10.77.0.1, or the bind fails on boot:

```bash
mkdir -p /etc/systemd/system/tinyproxy.service.d
printf '[Unit]\nAfter=wg-quick@wg0.service\nRequires=wg-quick@wg0.service\n' \
  > /etc/systemd/system/tinyproxy.service.d/after-wg.conf
systemctl daemon-reload && systemctl enable --now tinyproxy
```

### Firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 51820/udp
ufw allow in on wg0          # the proxy lives behind the tunnel
ufw route allow in on wg0 out on eth0
sed -i 's/^DEFAULT_FORWARD_POLICY.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
ufw --force enable
```

Open to the world: SSH and the WireGuard port. Nothing else.

## The Mac (runs the bot)

`/opt/homebrew/etc/wireguard/wg0.conf` — note the narrow `AllowedIPs`: only
packets to the proxy enter the tunnel, the rest of the Mac's traffic is
untouched.

```ini
[Interface]
PrivateKey = <mac private key>
Address = 10.77.0.2/24

[Peer]
PublicKey = <server public key>
Endpoint = <VPS_IP>:51820
AllowedIPs = 10.77.0.1/32
PersistentKeepalive = 25
```

Up now and on every boot:

```bash
sudo wg-quick up wg0
sudo cp com.lexx.wg0.plist /Library/LaunchDaemons/   # RunAtLoad wg-quick up
sudo launchctl load -w /Library/LaunchDaemons/com.lexx.wg0.plist
```

Then in `bipboop`:

```ini
HTTPS_PROXY=http://10.77.0.1:8888
```

httpx reads the variable by itself — every HTTPS call the bot makes leaves
through the VPS. Local traffic must not: the relay appends `CAST_HOST` and
`TTS_HOST` to `NO_PROXY` on start, because the Cast device answers its
"what are you" request over HTTPS and, sent through the VPS, that request
only times out — 30 s of silence before every spoken line.

## The iPad (full-tunnel VPN)

New peer per device: `wg genkey`, add a `[Peer]` block on the server
(`AllowedIPs = 10.77.0.3/32`, next device 10.77.0.4 and so on), then a
client config with `AllowedIPs = 0.0.0.0/0` and `DNS = 1.1.1.1`, delivered
as a QR code:

```bash
qrencode -t ansiutf8 < ipad.conf     # scan from the WireGuard iOS app
```

## Watchdog

`ip_watch.py` in the relay knocks on the proxy's TCP port every minute and
alerts when the tunnel stops answering, and checks the external IP through
the proxy — so a dead tunnel or a changed address shows up in Telegram
within a minute, not as silent Bybit auth failures.

## Debugging

```bash
wg show                        # handshakes per peer (server or mac)
curl -x http://10.77.0.1:8888 https://api.ipify.org   # must answer <VPS_IP>
ssh root@<VPS_IP> 'systemctl status tinyproxy wg-quick@wg0'
```
