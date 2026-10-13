# Installing Sali on a new machine

Sali is **one living process** on **one machine**. He is not a service you scale — he is a resident.
Everything below assumes an **Ubuntu Desktop** (written and verified on Ubuntu 26.04 LTS) with an
NVIDIA GPU, and a single human user who owns the box.

The invariant that everything else protects: **one Sali, one model, one inference path.** A second
copy is not prevented by systemd — it is prevented by the runtime itself, which takes a machine-wide
file lock and a PostgreSQL advisory lock. A stray `sali daemon` fails safely instead of quietly
becoming a second mind.

---

## 1. What the machine needs

| Thing | Why | Check |
|---|---|---|
| Python 3.14+ | the runtime | `python3 -V` |
| PostgreSQL 18 + pgvector | everything durable — memory, tasks, events | `psql -V` |
| llama.cpp (`llama-server`) | the local reasoning engine, so nothing leaves the house | `llama-server --version` |
| An NVIDIA GPU (16 GB+) | the 35B-A3B model runs GPU-resident | `nvidia-smi` |
| `bubblewrap` | the learning sandbox, for trying tools safely | `bwrap --version` |
| Docker *(optional)* | self-hosted SearXNG so web search works without an API key | `docker ps` |
| `cloudflared` *(optional)* | reach him from the iPhone off your LAN | `cloudflared -v` |

```bash
sudo apt update
sudo apt install -y python3-venv postgresql postgresql-18-pgvector bubblewrap git curl
```

### The reasoning engine: llama.cpp, not Ollama

Sali's brain is served by **two llama-server processes**, both systemd-owned (see `systemd/`):

* `sali-llamacpp` — the reasoning server: **Qwen3.5-35B-A3B** (GGUF, UD-IQ4_XS quant) plus its
  `mmproj` vision projector, GPU-resident, one model, one slot (`--parallel 1`), loopback only
  (`127.0.0.1:8083`).
* `sali-llamacpp-embed` — the embedding server: `nomic-embed-text-v1.5` (768-dim), deliberately
  CPU-only (`-ngl 0` + `CUDA_VISIBLE_DEVICES=`), loopback only (`127.0.0.1:8084`).

There is exactly **one conversational model**, configured in the service and in
`settings.model.llamacpp_model_alias`. No fallback, no silent substitute: the runtime probes the
served model id on every cognition call and refuses to think if it is anything else.

```bash
# Prebuilt CUDA binaries (cudart-bundled; needs only the NVIDIA driver):
mkdir -p ~/.local/opt/llama.cpp
#   fetch llama-<build>-bin-ubuntu-cuda-12.8-x64.tar.gz AND cudart-llama-<build>-bin-ubuntu-cuda-12.8-x64.tar.gz
#   from https://github.com/ggml-org/llama.cpp/releases and extract both into ~/.local/opt/llama.cpp
LD_LIBRARY_PATH=~/.local/opt/llama.cpp ~/.local/opt/llama.cpp/llama-server --version

# Model artifacts live here:
mkdir -p ~/.local/share/sali/models
#   Qwen3.5-35B-A3B-UD-IQ4_XS.gguf  (unsloth/Qwen3.5-35B-A3B-GGUF — verify sha256 against the repo)
#   mmproj-BF16.gguf                (same repo — the vision projector)
#   nomic-embed-text-v1.5.Q8_0.gguf (nomic-ai/nomic-embed-text-v1.5-GGUF)
```

Record the build number, model checksums, and quantization you actually installed (the engineering
journal in `docs/engineering/` has this host's current values).

---

## 2. The datastore

Sali connects over the **unix socket with peer auth** — there is no database password anywhere,
because there is no password to leak. The DB user is your login name.

```bash
sudo bash scripts/bootstrap_db.sh
```

This creates the peer-auth role, the `sali` and `sali_test` databases, and the required extensions
(`vector`, `pg_trgm`, `pgcrypto`). Idempotent — safe to re-run.

---

## 3. Sali himself

```bash
git clone <your remote> ~/Desktop/sali && cd ~/Desktop/sali
make install          # venv + editable install
.venv/bin/sali init   # apply every migration
.venv/bin/sali doctor # Postgres reachable? migrations current? model server up?
```

`sali doctor` is the one command to run whenever something feels wrong. It answers the three
questions that cause most breakage, and it answers them about the *live* system rather than config.

---

## 4. The power envelope — do not skip this

A 35B MoE prefill runs every CPU core and the GPU flat out at the same moment. Uncapped, that
transient **powered this machine off mid-turn** (2026-09-01, twice). `sali.service` therefore
`Requires=` the envelope unit, *and* the runtime refuses to start without the caps in place.

```bash
sudo cp systemd/sali-power-envelope.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sali-power-envelope
```

Tune the watt values in `scripts/power_envelope.sh` to your card and PSU. This is a hardware-safety
setting, not an optimisation.

---

## 5. The model servers and the VRAM guard

```bash
sudo cp systemd/sali-llamacpp.service systemd/sali-llamacpp-embed.service \
        systemd/sali-vram-guard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sali-llamacpp sali-llamacpp-embed sali-vram-guard
curl -s http://127.0.0.1:8083/health   # 200 once the model is resident (~35s first load)
curl -s http://127.0.0.1:8084/health   # embeddings, CPU-only
```

The VRAM guard is a small root daemon that watches the GPU: a second chat-server process, or VRAM
over the ceiling with a significant second consumer, gets shed (SIGTERM→SIGKILL) before it browns
the machine out. It identifies Sali's server by its command line (`SALI_VRAM_SERVER_MARKER`), and
ignores the small desktop clients a workstation always has.

---

## 6. Run him

```bash
sudo cp systemd/sali.service /etc/systemd/system/
sudo sed -i "s|/home/almir|$HOME|g; s|User=almir|User=$USER|" /etc/systemd/system/sali.service
sudo systemctl daemon-reload && sudo systemctl enable --now sali

sali status            # who is THE Sali, and what the GPU is actually holding
journalctl -u sali -f  # watch him think
```

`sali status` proves the invariant rather than asserting it: it names the process that owns the
machine-wide lock and reports what is really resident on the card.

Nightly backups of the datastore, config and vault:

```bash
sudo cp systemd/sali-backup.service systemd/sali-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sali-backup.timer
```

---

## 7. The phone

```bash
sali change-password        # sets the password the app logs in with — DO THIS
sali enroll-code            # one-time pairing code, ~10 minutes
```

Type the host URL into the app (Settings), then the password. On your LAN the app finds him by
itself over Bonjour (`_sali._tcp`); no URL needed.

**Change the password before exposing him.** The repository ships a default so a fresh install can
start; leaving it in place on an internet-facing host means anyone who reads the source can log in.

### Reaching him from outside

```bash
cloudflared tunnel login
cloudflared tunnel create sali
# route a hostname to http://127.0.0.1:8080, then:
sudo cloudflared service install
```

In the tunnel config, set:

```yaml
edge-ip-version: 4
```

Without it, `cloudflared` dials the edge over a rotating temporary IPv6 address and drops repeatedly
with `EADDRNOTAVAIL`.

### Web search without an API key

```bash
docker run -d --name searxng --restart unless-stopped \
  -p 127.0.0.1:8888:8080 \
  -v ~/.local/share/sali/searxng:/etc/searxng searxng/searxng:latest
```

Then in `~/.local/share/sali/searxng/settings.yml` add `json` to `search.formats` — it is **off by
default**, and without it every programmatic query returns empty while looking like "no results".
Bound to loopback only.

---

## 8. Daily use

```bash
sali agent                       # talk to him (a window onto the daemon — loads no second model)
sali agent "what changed today?" # one-shot
sali status                      # ownership + GPU truth
sali doctor                      # the three things that break
sali tasks                       # work in progress, surviving restarts
sali memory / sali recall "x"    # what he knows, and what he'd retrieve for a query
sali tools                       # what he has discovered on this machine
sali twin                        # his structural picture of the box
sali runs / sali run <id>        # recent turns, and the full event trace of one
sali backup                      # datastore + config + vault, rotated
```

`sali agent` **attaches** to the running daemon. It never loads a second model. If no daemon is
running, that terminal becomes the one Sali for as long as it lives.

### Sudo

Sali can perform elevated work without ever holding your password as text. It lives in an encrypted
vault; when a command needs `sudo`, an askpass helper reads the vault and hands the password straight
to sudo. It never enters the model, the prompt, the logs, or memory.

```bash
sali sudo-pass          # store it once
```

### Erasing him

```
Settings → Erase everything          (in the app)
```

This clears memory, conversations, tasks, events and the work archive, and keeps only what would
otherwise lock you out: the schema, your enrolled phone, and your password. It is a reset to a fresh
mind, not an uninstall.

---

## 9. When something is wrong

| Symptom | First move |
|---|---|
| He does not answer | `systemctl status sali sali-llamacpp`, then `journalctl -u sali -n 100` |
| "not starting a second Sali" | one is already running — that is the invariant working |
| `llamacpp` FAIL in doctor | `journalctl -u sali-llamacpp -n 50` — usually still loading (~35s) |
| Model server keeps dying | `journalctl -u sali-vram-guard` — the guard sheds duplicates/hogs |
| Replies are slow to start | normal at long contexts: prefill is ~750 tok/s, first token grows with prompt size |
| Machine powers off under load | the power envelope is not applied — section 4 |
| Web search returns nothing | SearXNG down, or `json` missing from `search.formats` |
| Phone cannot connect | tunnel down (`systemctl status cloudflared`), or `edge-ip-version: 4` missing |
| Anything database-shaped | `sali doctor` |

Architecture and transformation history live in `docs/engineering/` and `docs/agi/`.

---

## 10. The iPhone app

Lives in `ios/`. It is an SPM library wrapped by `xcodegen` — build it on a Mac:

```bash
cd ios && xcodegen generate && open Sali.xcodeproj
```

Set your signing team in `project.yml`. Device builds must sign; do not add
`CODE_SIGNING_ALLOWED: NO` there — it breaks on-device installs. Regenerate the project whenever you
add files or assets. See `ios/docs/` for the API reference and build notes.
