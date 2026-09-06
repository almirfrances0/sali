#!/usr/bin/env bash
# Sali's POWER ENVELOPE — the cap that keeps this machine alive while a 35B MoE thinks.
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-01 this machine hard-powered-off mid-inference. It was NOT a duplicate model load:
# ollama had exactly one runner, one slot, one task. It died on a single legitimate turn, and the
# journal simply stops mid-prompt-processing — no thermal event, no MCE, no OOM. A clean power trip.
#
# sali:latest is a 35B MoE with ~3B active. "3B active" is true of GENERATION and dangerously
# misleading about PREFILL: a 512-token prefill batch routes across essentially every expert, so
# prompt processing touches all 35B of weights. With 40% of layers on CPU, prefill streams ~6.4GB of
# expert weights through every core at full tilt WHILE the GPU runs its 60% at full tilt. That is the
# highest combined draw this box can produce, and it held it for 20+ seconds at ~506 tok/s.
#
# The 140W GPU cap did not save it because the GPU is only half the load. The CPU half was uncapped:
# this board runs the i9-11900K at a 255W package limit — DOUBLE its 125W rating. 255W + 140W + the
# platform is more than the PSU can deliver, and the rail collapsed.
#
# So the envelope caps BOTH halves, with margin, in the one place that is authoritative — the
# hardware itself. Software gates cannot help here: by the time prefill is running, the draw is
# already committed.
#
#   sudo cp systemd/sali-power-envelope.service /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now sali-power-envelope
#
# `sali.service` Requires= this unit, so Sali can never load the model onto an uncapped machine.
set -euo pipefail

# ── CPU package (Intel RAPL) ──────────────────────────────────────────────────────────────────────
# Sustained below the chip's own 125W rating; the short-term window gets the rating itself. Sali is a
# resident, not a benchmark — prefill gets slower and the machine stays on.
CPU_LONG_W=${SALI_CPU_LONG_W:-95}
CPU_SHORT_W=${SALI_CPU_SHORT_W:-125}
# ── GPU (nvidia-smi) ──────────────────────────────────────────────────────────────────────────────
# Below the previous 140W, which was chosen when the CPU side was believed to be small.
GPU_W=${SALI_GPU_W:-100}
GPU_CLK_MAX=${SALI_GPU_CLK_MAX:-900}   # MHz; stock boost ~3100. Halving the clock ~halves peak current.

rapl=/sys/class/powercap/intel-rapl:0
if [ -d "$rapl" ]; then
  rated_uw=$(cat "$rapl/constraint_0_max_power_uw" 2>/dev/null || echo 0)
  echo "cpu: rated $((rated_uw / 1000000))W, was long=$(($(cat "$rapl/constraint_0_power_limit_uw") / 1000000))W"
  echo $((CPU_LONG_W * 1000000))  > "$rapl/constraint_0_power_limit_uw"
  echo $((CPU_SHORT_W * 1000000)) > "$rapl/constraint_1_power_limit_uw" 2>/dev/null || true
  echo "cpu: capped long=${CPU_LONG_W}W short=${CPU_SHORT_W}W"
else
  echo "cpu: no intel-rapl on this host — CPU package power is UNCAPPED" >&2
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -pm 1 >/dev/null
  nvidia-smi -pl "$GPU_W" >/dev/null
  # The transient lever. If -lgc is unsupported, fall back to a low locked APPLICATION clock (-lockac
  # on older drivers) — either way the clock must not be allowed to boost to stock.
  nvidia-smi -lgc "210,${GPU_CLK_MAX}" >/dev/null 2>&1 || nvidia-smi -lgc "${GPU_CLK_MAX}" >/dev/null 2>&1 || true
  echo "gpu: persistence on, capped ${GPU_W}W, clock ceiling ${GPU_CLK_MAX}MHz (transient guard)"
else
  echo "gpu: no nvidia-smi — GPU power is UNCAPPED" >&2
fi
