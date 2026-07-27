#!/usr/bin/env bash
# host-sox-installation.sh — the HOST half of the sandbox voice-mode audio bridge.
# Run this ON THE HOST (not inside the sandbox). It:
#   1. installs pactl if missing — the only host dependency; SoX itself is NOT
#      needed on the host, recording happens inside the sandbox,
#   2. exposes the running sound server (PipeWire or PulseAudio) on TCP 4713,
#      restricted to localhost + the sandbox's Docker subnets,
#   3. allows the sandbox firewall to reach that port (sbx network policy).
# Sandbox half: sandbox-sox-installation.sh. Safe to re-run.
# Undo the TCP exposure any time with:  pactl unload-module module-native-protocol-tcp
set -euo pipefail

if [ -n "${SANDBOX_VM_ID:-}" ]; then
  echo "You are inside the sandbox — run this on the host. (sandbox-sox-installation.sh is the in-sandbox half.)" >&2
  exit 1
fi

if ! command -v pactl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq pulseaudio-utils
  else
    echo "pactl not found. On Linux install pulseaudio-utils; on macOS there is no system PulseAudio —" >&2
    echo "either 'brew install pulseaudio' and run its daemon, or skip the bridge and run claude on the host for voice." >&2
    exit 1
  fi
fi

if ! pactl info >/dev/null 2>&1; then
  echo "No running sound server (PipeWire/PulseAudio). Start your desktop audio session first." >&2
  exit 1
fi

# The ACL replaces cookie auth: loopback (the sandbox proxy connects from the
# host itself) plus the sandbox's Docker IPv4/IPv6 ranges. Nothing routable
# from outside the machine is allowed.
ACL='127.0.0.1;::1;172.16.0.0/12;fdcd:b70:e762::/48;fe80::/10'
if pactl list short modules | grep -q module-native-protocol-tcp; then
  echo "module-native-protocol-tcp already loaded — leaving it as is."
else
  pactl load-module module-native-protocol-tcp port=4713 auth-ip-acl="$ACL"
  echo "Sound server now listening on tcp/4713 (loopback + sandbox subnets only)."
fi

if command -v sbx >/dev/null 2>&1; then
  sbx policy allow network localhost:4713 \
    || echo "sbx policy command failed — check 'sbx policy allow --help' and allow localhost:4713 manually." >&2
else
  echo "sbx CLI not found in PATH — allow localhost:4713 in the sandbox network policy manually." >&2
fi

echo "Host side ready. In the sandbox, verify with:  pactl info"
