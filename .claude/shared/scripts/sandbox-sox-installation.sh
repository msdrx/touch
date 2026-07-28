#!/usr/bin/env bash
# sandbox-sox-installation.sh — the SANDBOX half of the voice-mode audio bridge.
# Installs SoX (what `claude` voice mode spawns to record) plus the ALSA→Pulse
# plumbing, and points audio at the host's sound server over TCP. The sandbox
# has no sound card, so recording only works via that bridge.
# Host half: host-sox-installation.sh — run it ON THE HOST, not in here.
# Safe to re-run.
set -euo pipefail

if [ -z "${SANDBOX_VM_ID:-}" ]; then
  echo "SANDBOX_VM_ID is not set — this looks like the host. Run host-sox-installation.sh there instead." >&2
  exit 1
fi

sudo apt-get update -qq
sudo apt-get install -y -qq sox libsox-fmt-all libasound2-plugins pulseaudio-utils

# Route the ALSA default device through PulseAudio (libasound2-plugins),
# whose server address comes from ~/.config/pulse/client.conf. This is what
# lets voice mode's sox child reach the host mic without any env vars.
sudo tee /etc/asound.conf >/dev/null <<'EOF'
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

mkdir -p "$HOME/.config/pulse"
cat > "$HOME/.config/pulse/client.conf" <<'EOF'
# Voice-mode bridge: send audio to the host's sound server over TCP.
default-server = tcp:host.docker.internal:4713
autospawn = no
EOF

echo "Sandbox bridge installed: sox -> ALSA default -> pulse -> tcp:host.docker.internal:4713"
echo "After the host side is up, verify with:  pactl info && rec /tmp/probe.wav trim 0 3"
