# Browser VM

These files turn an Ubuntu 24.04 VM on your tailnet into Scout's browser host:

| File | Role |
| --- | --- |
| `setup.sh` | One-time, idempotent setup. Run it as root. |
| `scout-browser` | The helper Scout calls over SSH to start, stop and report browser profiles. |
| `authorized_keys` | **Not committed.** Copy Scout's public key here before copying the folder to the VM. |

The full procedure, with verification steps and troubleshooting, is in [docs/SETUP.md §5](../../docs/SETUP.md#5-the-browser-vm).

```
workstation (Scout) ──SSH (Scout's key)──▶ VM: scout-browser start research   (starts Brave with the profile)
                    ──SSH tunnel────────▶ VM: 127.0.0.1:9301                  (Brave's DevTools port)
you                 ──https (tailnet)───▶ VM: noVNC live view                 (watch, sign in, solve CAPTCHAs)
```

Nothing listens on the VM's network interfaces except SSH and Tailscale:

- DevTools ports, VNC, noVNC and Tor all bind to `127.0.0.1`.
- `tailscale serve` publishes only the live view, and only to the tailnet.

## Quick reference

```bash
# Install (from the workstation; <vm-admin> is your sudo user on the VM)
cp ~/.ssh/scout_ed25519.pub deploy/browser-vm/authorized_keys
scp -i ~/.ssh/scout_ed25519 -r deploy/browser-vm <vm-admin>@<browser-vm>:/tmp/ && rm deploy/browser-vm/authorized_keys
ssh -t -i ~/.ssh/scout_ed25519 <vm-admin>@<browser-vm> sudo bash /tmp/browser-vm/setup.sh

# Operate
ssh -i ~/.ssh/scout_ed25519 scout@<browser-vm> scout-browser status
ssh -i ~/.ssh/scout_ed25519 scout@<browser-vm> scout-browser stop <profile>
python scripts/browser_check.py research                     # start, attach, load a page
python scripts/browser_check.py --tor --search "query"        # search through Tor (--no-tor: direct)
```

**Requirements on the VM:**

- `openssh-server` must be installed.
- Tailscale SSH stays **off**. If your tailnet's SSH policy uses check mode, every unattended connection would wait for a browser approval.
- If `tailscale serve` fails, turn on HTTPS certificates in the Tailscale admin console (DNS page), then run `setup.sh` again.

**Live view:** `https://<browser-vm>.<tailnet>/vnc.html`. VNC has no password, so it relies on the tailnet being yours (see the security model in [docs/SETUP.md §10](../../docs/SETUP.md#10-security-model)).

**Profiles:** profile settings, the Tor profile and the Tor on/off switch are covered in [docs/SETUP.md §6–7](../../docs/SETUP.md#6-browser-profiles).
