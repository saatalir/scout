# Serving Qwen3.8 with Strata for Online Scout

This guide covers one path end to end: from model files on a GPU server to Scout on a workstation calling the model. It is the procedure behind the endpoint that passed the Phase 0 eval gate ([SETUP.md §9](SETUP.md#9-reference-results)).

Placeholders:

| Placeholder | Meaning |
| --- | --- |
| `<strata-host>` | The GPU server's Tailscale machine name |
| `<tailnet>` | Your tailnet DNS suffix (`xxxx.ts.net`) |
| `<user>` | Your login on the server |
| `<strata-key>` | The API key Strata generates |
| `<preset>` | A saved Strata launch preset name |

---

## 1. The request path

```
Scout (workstation)
  │  OpenAI client → POST https://<strata-host>.<tailnet>/v1/chat/completions
  │  header: Authorization: Bearer <strata-key>
  ▼
Tailscale (WireGuard, tailnet only)
  ▼
<strata-host>:443  ── tailscaled: `tailscale serve` terminates HTTPS (Let's Encrypt cert for <strata-host>.<tailnet>)
  │                    rule:  /  →  proxy http://127.0.0.1:8080
  ▼
127.0.0.1:8080     ── Strata front server (python -m serve.server): OpenAI-compatible API, checks the API key (401 without it)
  ▼
Strata engine      ── strata --serve … (GPU, expert cache, KV, drafting) + strata-vision (image input)
  ▼
qwen3.8-flash-next-unsloth-ud-q4_k_xl  (262,144-token context; text + image in, text out)
```

Each layer has one job, and you can test each one on its own (§7):

| Layer | Listens on | Job | Fails as |
| --- | --- | --- | --- |
| Tailscale | the tailnet only | Encrypted transport and name resolution | DNS error, timeout |
| `tailscale serve` | `<strata-host>:443` on the tailnet | HTTPS with a real certificate; forwards to loopback | Connection refused, TLS error |
| Strata front server | `127.0.0.1:8080` | OpenAI API, API-key check, ready state | 401, 503 while loading |
| Strata engine | internal | Inference | Slow first tokens, errors in the engine log |

**Why this shape:**

- Strata never listens on a network interface. Only loopback is open, and Tailscale publishes it to your devices alone. Nothing is reachable from the internet or the LAN.
- HTTPS comes from Tailscale's certificate, so no certificate handling is needed.
- The API key is a second lock: a device on the tailnet still needs the key.

---

## 2. Reference deployment

These are the measured figures for the setup this guide describes.

| Item | Value |
| --- | --- |
| Server OS | Ubuntu 26.04 LTS, x86_64 |
| CPU / RAM | 28 cores / 125 GB (about 94 GB used while serving; about 31 GB available) |
| GPUs | 2 × NVIDIA RTX 3060, 12 GB each (both about 95% full). Roles: GPU 0 `helper`, GPU 1 `main`; layer split `auto` |
| NVIDIA driver | 595.99 (CUDA 13 engine build) |
| Strata | `strata 0.2.12` (build 0.1.58), the "terminal" install under `~/.local/share/strata` |
| Model files | Unsloth `Qwen3.8-Flash-Next-UD-Q4_K_XL` GGUF (4 shards), plus `mmproj-Qwen3.8-Flash-Next-BF16.gguf` for vision |
| Model id served | `qwen3.8-flash-next-unsloth-ud-q4_k_xl` |
| Context | 262,144 tokens; KV cache `int8`, 32,768 tokens resident in VRAM, the rest streamed from RAM |
| Vision | CPU reader (`strata-vision`, 14 threads). Strata picks the CPU reader on cards under 24 GB |
| Drafting | DFlash2 (a 1.2 GB drafter), with the MTP layer |
| Cold start | About 20 minutes from launch to ready (`engine_ready_s: 1230`): loading, then building the expert cache |
| Speed seen by Scout | Short prompt: first token 0.2–1.7 s, ~36 tok/s. New 8k prompt: first token 9.8 s. New 24k prompt: 21.6 s. Cached prefix: ~0.3 s |

**Memory rule:** Strata uses most of both GPUs and about 94 GB of RAM. Anything else on the server, such as the browser VM, must fit in the RAM left over. The server has no swap. Check `free -g` before adding load.

---

## 3. Install Strata (once)

Strata installs per user under `~/.local/share/strata`, and its launcher is `~/.local/bin/strata`. Install it with the official installer for your platform. Then make sure `~/.local/bin` is on your `PATH`; non-interactive SSH shells may not include it, so use the full path there.

```bash
strata --version                 # e.g. "strata 0.2.12 (build 0.1.58; python 3.12…)"
strata doctor                    # read-only check of the installed engine components
strata components                # what is installed
nvidia-smi                       # the driver sees every GPU you intend to use
```

**Model files.** Either let `strata launch` download them (the guided screens ask), or point it at files you already have with `--model-path` (a folder of shards or one `.gguf`). Vision needs the matching `mmproj-…gguf` next to the model.

Strata also builds a "pack" for each model (expert layout and tokenizer) under `~/.local/share/strata/Strata-data/packs/`. It does this the first time; you don't manage packs by hand.

---

## 4. Start the model: `strata launch`

### 4.1 First time: the guided screens

```bash
strata launch
```

The screens walk you through one decision at a time:

1. **Model:** `UD-Q4_K_XL`.
2. **Context:** 256k.
3. **KV format:** `int8`.
4. **Vision:** on (CPU on cards under 24 GB).
5. **Drafting:** auto / DFlash2.
6. **GPUs and their roles.**
7. **Optional tuning.**
8. **Access:** see below.
9. **API key**, then **save as a preset**.

It then serves and opens a live monitor.

**Choosing "Access":**

| Access | Strata binds | Who can reach it | Use |
| --- | --- | --- | --- |
| `local` | `127.0.0.1` | This server only, unless you publish it yourself (§5) | Pair with a manual `tailscale serve` rule. This is how the reference preset is set up. |
| `tailscale` | `127.0.0.1` plus Strata's own tailnet publishing | Your tailnet | The simplest option: Strata handles the publishing |
| `lan` | The LAN interface | Everyone on your network | Not recommended for Scout |
| `cloudflare` | A public quick tunnel | Anyone with the URL (and key) | Not for Scout: it would leave your tailnet |

Strata generates an API key when access reaches beyond the server. Keep it on: `--no-api-key` only prints a warning, and the endpoint would then be open to every device on the tailnet.

### 4.2 Every later start: the saved preset

The reference preset was saved as `Q4_K_XL 256K`. Starting it again, without the screens:

```bash
strata launch --list-presets                          # name, model, context, tuned?, saved at
strata launch --preset "<preset>" --no-tui            # serve and return; prints the address
strata launch --preset "<preset>" --access tailscale --no-tui   # same, and let Strata publish it on the tailnet
strata launch --status                                # e.g. "q4_k_xl-256k: ready at http://127.0.0.1:8080"
strata launch --show-key --preset "<preset>"          # print the API key (treat as a secret)
strata launch --stop                                  # stop what launch started
```

The same thing written out as flags, without a preset (run `strata launch --plan …` first to see what it would do without changing anything):

```bash
strata launch --model ud-q4_k_xl --context 256k --kv int8 --vision cpu --drafting dflash2 \
              --gpu 0,1 --access local --port 8080 --save-preset "<preset>" --no-tui
```

**Where things are stored:**

| What | Path (on `<strata-host>`) |
| --- | --- |
| Presets | `~/.config/strata/launch-presets/<preset-id>.json` (access, port, context, vision, drafting) |
| Resolved run config | `~/.local/state/strata/launch/<preset-id>.json` (engine args, GPU roles, port, `api_key`, model name) |
| Engine log | `~/.local/state/strata/launch/<preset-id>.engine.log` (expert-cache hit rate, KV streaming, drafting acceptance) |

The run config contains the API key, so never copy it off the server or into a repository.

### 4.3 What actually runs

`strata launch` starts and supervises three processes, all as your user:

| Process | Role |
| --- | --- |
| `python -m strata_control.runtime --host launch-<preset-id>` | Supervisor: start-up wait (up to ~20 min) and shutdown |
| `python -m serve.server --engine strata --host 127.0.0.1 --port 8080 …` | The OpenAI-compatible front server and API-key check |
| `strata --serve --pack … --native <model>.gguf --max-context 262144 --kv int8 --kv-resident 32768 --vision --draft dflash2 --spec 6 --layer-split auto …` and `strata-vision --mmproj … --threads 14` | Engine and image reader |

You don't run these by hand; `strata launch` derives them from the preset. Check them with `strata launch --status`, or with `ss -ltnp | grep 8080`, which should show `127.0.0.1:8080` owned by `python3.12`.

---

## 5. Publish it on the tailnet

The goal is one HTTPS address, reachable only on your tailnet, that forwards to `127.0.0.1:8080`.

**Prerequisites (once):**

- The server is on your tailnet (`tailscale up`).
- In the Tailscale admin console's DNS page, **MagicDNS** and **HTTPS Certificates** are both on.

**Option A — let Strata do it:** start with `--access tailscale`.

**Option B — a manual rule** (the reference setup: preset `access = local`, plus this rule):

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status
# https://<strata-host>.<tailnet> (tailnet only)
# |-- / proxy http://127.0.0.1:8080
```

How the manual rule behaves:

- **It persists.** `--bg` stores the rule in Tailscale's state. It survives reboots and Strata restarts, and does nothing useful until Strata listens on 8080 again.
- **It is not `funnel`.** `tailscale funnel` would publish to the internet. Never use it for Strata.
- **It is independent of Strata.** With `access = local`, Strata does not know about the rule. If the rule is ever removed (`tailscale serve reset`, or a reinstall), Strata keeps serving on loopback but the workstation loses access. That is the first thing to check when the endpoint suddenly refuses connections (§8).

**Result:** `https://<strata-host>.<tailnet>/v1` is the base URL. Port 443 is the only Strata-related port open on the tailnet; 8080 is not reachable from other machines.

---

## 6. Connect Scout (the workstation)

1. Tailscale is installed and signed in to the same tailnet, so `tailscale status` lists `<strata-host>`.
2. Get the key on the server with `strata launch --show-key --preset "<preset>"`. Copy it over a private channel; never paste it into chat, issues or commits.
3. Fill in `.env` (copied from `.env.example`):

   ```bash
   STRATA_BASE_URL=https://<strata-host>.<tailnet>/v1
   STRATA_API_KEY=<strata-key>
   STRATA_MODEL=qwen3.8-flash-next-unsloth-ud-q4_k_xl   # pinned: must equal the id in /v1/models
   SCOUT_ROUTER=direct
   SCOUT_REQUEST_TIMEOUT=300                            # first requests after a start can be slow
   ```

4. Scout's client (`scout/llm.py`) is the standard OpenAI Python client with `base_url=STRATA_BASE_URL` and `api_key=STRATA_API_KEY`. Every role (`planner`, `browser`, `vision`, `extractor`) resolves to `STRATA_MODEL`.

`strata connect <app>` prints ready-made settings for other clients (opencode, codex, claude-code, …). Scout needs only the three values above.

---

## 7. Verify, layer by layer

Run these from the workstation, in order. The first failure names the broken layer.

```bash
set -a; . ./.env; set +a                       # load the values (contains the key: run in a private shell)
H=${STRATA_BASE_URL#https://}; H=${H%%/*}

# 1. Tailscale: name and route
tailscale status | grep "${H%%.*}"             # the host is listed and not "offline"
tailscale ping "${H%%.*}"                      # "pong from …"

# 2. tailscale serve: HTTPS and certificate
curl -sS -o /dev/null -w "%{http_code}\n" "https://$H/"          # any HTTP code (200: Strata's page) = HTTPS works
echo | openssl s_client -connect "$H:443" -servername "$H" 2>/dev/null | openssl x509 -noout -subject -issuer
#   subject=CN=<strata-host>.<tailnet>   issuer=…Let's Encrypt…

# 3. Strata front server: the API key
curl -s -o /dev/null -w "%{http_code}\n" "$STRATA_BASE_URL/models"                                         # 401
curl -s -H "Authorization: Bearer $STRATA_API_KEY" "$STRATA_BASE_URL/models" | python3 -m json.tool       # 200
#   "id": "qwen3.8-flash-next-unsloth-ud-q4_k_xl", "status": {"value": "loaded"},
#   "meta": {"n_ctx": 262144}, "input_modalities": ["text", "image"]

# 4. The engine: chat, tools and images, through Scout's own client
python scripts/check_connection.py              # five OK lines
python -m evals.run --repeats 3                 # the full gate (~15 min warm)
```

On the server, the equivalent checks are:

```bash
strata launch --status                          # ready at http://127.0.0.1:8080
ss -ltnp | grep -E ':8080|:443'                 # 8080 on 127.0.0.1 only; 443 on the tailscale0 address
tailscale serve status                          # / → http://127.0.0.1:8080
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/v1/models   # 401 (the key is enforced locally too)
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
tail -f ~/.local/state/strata/launch/<preset-id>.engine.log
```

---

## 8. Troubleshooting

| Symptom (workstation) | Layer | Check | Fix |
| --- | --- | --- | --- |
| `Could not resolve host` / DNS error | Tailscale | `tailscale status` | Sign the workstation in to Tailscale; turn on MagicDNS |
| Timeout | Tailscale | `tailscale ping <strata-host>` | Server offline or Tailscale stopped on it |
| `Connection refused` on 443 | serve | `tailscale serve status` on the server | The rule is gone: `sudo tailscale serve --bg --https=443 http://127.0.0.1:8080`, or start with `--access tailscale` |
| TLS/certificate error | serve | Admin console → DNS | Turn on HTTPS Certificates; run `tailscale cert <strata-host>.<tailnet>` once if needed |
| `502` / `503` from serve | Strata | `strata launch --status` | Strata is stopped or still loading (cold start ~20 min). Start the preset and wait for "ready" |
| `401` | Key | `strata launch --show-key --preset "<preset>"` | Copy the current key into `STRATA_API_KEY`; `--new-key` replaces it, so update `.env` afterwards |
| `404 model not found` | Model id | `curl …/v1/models` | `STRATA_MODEL` must match the served id exactly; update it after switching presets |
| Empty answer, `finish_reason=length` | Model behaviour | Token budget | The model reasons first: use `max_tokens` ≥ 512 |
| Very slow first requests | Engine | Engine log | Normal after a start (expert cache warming). Keep `SCOUT_REQUEST_TIMEOUT=300` |
| Slow under load, swapping | Server RAM | `free -g`, `nvidia-smi` | Other workloads (VMs, browsers) are taking RAM; stop them or shrink them |
| Fine on the server, refused from the workstation, after a server reboot | Startup | `strata launch --status` | Strata does not start on boot (§9); start the preset |

---

## 9. Lifecycle and operations

**Starting after a reboot.** In the reference setup, Strata is started by hand: `strata launch` in a terminal of the desktop session. It does **not** start on boot. After a reboot:

```bash
strata launch --preset "<preset>" --no-tui      # then wait for "ready" (about 20 min cold)
```

The `tailscale serve` rule persists, so nothing else is needed.

To make Strata start automatically, run that same command from a systemd **user** service or a desktop autostart entry. Then confirm with `strata launch --status` after a test reboot.

**Changing settings safely:**

| Change | Command |
| --- | --- |
| Try a setting for one start | `strata launch --preset "<preset>" --set KEY=VALUE --no-tui` |
| Switch preset or model | `strata launch --preset "<other>" --restart --no-tui`, then check `/v1/models` and update `STRATA_MODEL` if the id changed, then rerun `check_connection.py` and the eval gate |
| Rotate the key | `strata launch --preset "<preset>" --new-key --restart --no-tui`, then `--show-key`, then update `.env` on every client |
| Update Strata | `strata update` (switches only after a test start), then the §7 checks and the eval gate |

**Security checklist:**

- [ ] `ss -ltnp` shows Strata on `127.0.0.1:8080` only.
- [ ] `tailscale serve status` (and `tailscale funnel status`) say "(tailnet only)" for the 443 address: not funnelled to the internet.
- [ ] The API key is on (`401` without it).
- [ ] The key lives only in the server's Strata config and in clients' `.env` files, which are git-ignored.
- [ ] Tailscale ACLs limit port 443 on `<strata-host>` to your own devices, if your tailnet has other users.
