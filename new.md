# Voice Layer for Sali — One Pipeline, Two Input Modes

**Date:** 2026-09-18  
**Host Hardware:** Intel Core i9-11900K | RTX 2080 Ti (22 GB VRAM, ~1.7 GB free) | PipeWire / ALSA  
**Principle:** Voice is NOT a new Sali. It is a thin audio I/O skin that wraps the **existing** chat pipeline.

---

## 1. The Pipeline That Already Exists

Sali's chat works today through this exact flow:

```
    ┌─────────────────────────────────────────────────────────────────┐
    │                       iOS App / Web UI                          │
    └──────────────────┬──────────────────────────┬───────────────────┘
                       │                          │
           POST /api/conversation/message    WS /ws (subscribed)
                (text content)                    │
                       │                  streaming events back:
                       ▼                  token, tool, thinking, final
              ┌────────────────┐                  ▲
              │  AgentLoop     │                  │
              │  (runtime)     │──────────────────┘
              │  Sali's Brain  │    broadcasts via ConnectionManager
              └────────────────┘         (ws.py → broadcast_event)
```

The brain never changes. The REST endpoint and WebSocket are the two connectors.  
**Voice adds audio only on the edges:**

```
    ┌────────────────────────────────────────────────────────────────────────┐
    │                      VOICE I/O LAYER (new, thin)                       │
    │                                                                        │
    │  Microphone ──▶ VAD ──▶ Whisper STT ──▶  text   ──▶  POST /api/...   │
    │                                                         /conversation/ │
    │  Speakers   ◀── Kokoro TTS ◀──────────────────── text ◀   /message   │
    │                                         (from WS "final" event)        │
    └────────────────────────────────────────────────────────────────────────┘
```

Voice capture produces **text** → goes to the same `POST /conversation/message`.  
Sali's final response text arrives on the same **WebSocket** `agent.final` event → TTS plays it.  
Sali's brain, memory, tools, reasoning, and identity are **completely untouched**.

---

## 2. Why Gemini Live API Cannot Be the Main Voice Layer

Gemini Live API is a **speech-to-speech system** — you talk to it, it responds. It uses **Gemini's own LLM** as the brain. There is no way to route Gemini Live's audio processing through Sali's `AgentLoop`.

| Approach | What Happens | Problem |
| :--- | :--- | :--- |
| Use Gemini Live as Sali's voice | Gemini's model answers, not Sali | Sali's brain, memory and identity are bypassed entirely |
| Use Gemini TTS only (not Live) | Not available — no standalone audio-output-only endpoint | Not viable |
| Use Gemini Live for STT only | The Live API is stateful and tied to its own reasoning session | Cannot be decoupled from its own LLM |

**Conclusion:** Gemini Live would replace Sali's brain, not extend it.  
If you want cloud audio with zero VRAM and zero extra dependencies, the correct free alternative is **Google Cloud Text-to-Speech (Journey voices)** for output and **OpenAI-compatible Whisper API (or local)** for input — but the cleanest and completely offline path is the local approach below.

---

## 3. Hardware Assessment

```
nvidia-smi output:
  RTX 2080 Ti — Total: 22,528 MiB
  Ollama llama-server (35B): 20,148 MiB in use
  Free: ~1,758 MiB (~1.7 GB)
```

The i9-11900K has **AVX-512** support and 10 GB free system RAM.  
Both voice models run faster-than-real-time on the CPU, consuming **0 MB GPU VRAM** — the 35B model is completely untouched.

---

## 4. The Correct Local Stack

### 4.1. Listening: faster-whisper + Silero VAD

| Component | Role | Hardware | Memory | Latency |
| :--- | :--- | :--- | :--- | :--- |
| **Silero VAD** | Detects when you start/stop speaking | CPU | ~5 MB RAM | <5 ms |
| **faster-whisper `small.en`** | Transcribes audio to text | CPU (int8) | ~400 MB RAM | ~120 ms |

VAD handles the hard problem: it tells the system **when** a speech segment ends so it knows when to fire the transcription. Without it, the engine would either miss words or wait forever.

### 4.2. Speaking: Kokoro-82M

* **82M parameters**, ~327 MB weights — fits entirely in system RAM
* Real-Time Factor ~0.15–0.25x on the i9-11900K (generates audio **faster than it plays**)
* Multiple human-sounding personas: `am_adam` (male), `af_bella` (female), `bm_george` (British)
* Natural prosody, breathing, pauses — not robotic
* **Requires:** `apt install espeak-ng` (phonemizer dependency)

---

## 5. Integration Architecture

### 5.1. Where Voice Lives in Sali's Codebase

Voice must be a new, isolated package that sits **outside the existing layers**. It connects to the API exactly like the iOS app does — no internal coupling.

```
src/sali/audio/
    __init__.py
    voice_bridge.py       # Orchestrator: listens → Sali API → speaks
    stt.py                # faster-whisper + Silero VAD wrapper
    tts.py                # Kokoro-82M wrapper
    audio_io.py           # sounddevice capture/playback helpers
```

This placement:
- Does NOT break any `import-linter` layer contracts (it imports nothing from inside Sali)
- The voice bridge connects to Sali via `httpx` (same as the iPhone does)
- Receives responses via WebSocket subscription (same as the web UI does)

### 5.2. Unified Flow (Code Blueprint)

```python
# src/sali/audio/voice_bridge.py
"""
Voice bridge: microphone → Sali → speaker.
This is a client of Sali's API, not internal logic.
It authenticates with a device token just like the iPhone does.
"""

import asyncio
import httpx
import json
import websockets
import sounddevice as sd
import numpy as np

from sali.audio.stt import SpeechRecognizer  # faster-whisper + VAD
from sali.audio.tts import SpeechSynthesizer  # Kokoro-82M

BASE_URL = "http://127.0.0.1:8790"

class VoiceBridge:
    def __init__(self, token: str):
        self.token = token
        self.stt = SpeechRecognizer()   # runs on CPU
        self.tts = SpeechSynthesizer()  # runs on CPU
        self._speaking = asyncio.Event()

    async def run(self) -> None:
        """Main loop: listen → send → receive → speak."""
        ws_url = f"ws://127.0.0.1:8790/ws?token={self.token}"
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"type": "subscribe", "after_seq": 0}))

            # Run listening and response-watching in parallel
            await asyncio.gather(
                self._listen_loop(ws),
                self._speak_loop(ws),
            )

    async def _listen_loop(self, ws) -> None:
        """Capture mic audio, transcribe, post to Sali when a full utterance is ready."""
        async for utterance in self.stt.stream_utterances():
            if not utterance.strip():
                continue
            # Suppress listening while Sali is speaking (barge-in can be added later)
            if self._speaking.is_set():
                continue

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{BASE_URL}/api/conversation/message",
                    json={"content": utterance},
                    headers={"Authorization": f"Bearer {self.token}"},
                )

    async def _speak_loop(self, ws) -> None:
        """Watch WebSocket for Sali's final response text, then speak it."""
        async for raw in ws:
            msg = json.loads(raw)
            # The WS delivers typed events. "agent.final" is the complete response.
            if msg.get("type") == "event":
                payload = msg.get("payload", {})
                if msg.get("event_type") == "agent.final":
                    text = payload.get("text", "").strip()
                    if text:
                        self._speaking.set()
                        try:
                            await self.tts.speak(text)
                        finally:
                            self._speaking.clear()
```

```python
# src/sali/audio/stt.py
"""Speech-To-Text: faster-whisper + Silero VAD.
Runs entirely on CPU; 0 MB GPU VRAM."""

from __future__ import annotations
import asyncio
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import torch

SAMPLE_RATE = 16_000
CHUNK_MS = 30   # Silero VAD requires 30ms or 60ms chunks at 16kHz

class SpeechRecognizer:
    def __init__(self) -> None:
        self._model = WhisperModel("small.en", device="cpu", compute_type="int8")
        self._vad, _ = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
        self._chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)

    async def stream_utterances(self):
        """Async generator: yields complete speech utterances as text."""
        loop = asyncio.get_event_loop()
        q: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def _cb(indata, frames, time_, status):
            loop.call_soon_threadsafe(q.put_nowait, indata[:, 0].copy())

        buffer: list[np.ndarray] = []
        in_speech = False

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            dtype="float32", blocksize=self._chunk, callback=_cb):
            while True:
                chunk = await q.get()
                t = torch.from_numpy(chunk)
                prob = self._vad(t, SAMPLE_RATE).item()
                is_speech = prob > 0.5

                if is_speech:
                    in_speech = True
                    buffer.append(chunk)
                elif in_speech:
                    # End of utterance — transcribe what we captured
                    in_speech = False
                    audio = np.concatenate(buffer)
                    buffer.clear()
                    segments, _ = self._model.transcribe(audio, beam_size=1)
                    text = " ".join(s.text for s in segments).strip()
                    if text:
                        yield text
```

```python
# src/sali/audio/tts.py
"""Text-To-Speech: Kokoro-82M.
Runs entirely on CPU; 0 MB GPU VRAM."""

import sounddevice as sd
from kokoro import KPipeline

class SpeechSynthesizer:
    def __init__(self, voice: str = "am_adam") -> None:
        self._pipe = KPipeline(lang_code="a")   # American English
        self._voice = voice

    async def speak(self, text: str) -> None:
        """Synthesize text and play through system speakers (blocking until done)."""
        for _, _, audio in self._pipe(text, voice=self._voice, speed=1.05):
            sd.play(audio, 24_000)
            sd.wait()
```

### 5.3. How to Start Voice Mode

```bash
# Terminal 1: Sali daemon + API (already running)
sali daemon &
sali serve --host 127.0.0.1 --port 8790 &

# Terminal 2: Voice bridge (new)
# Enroll a local device token once, then:
python -m sali.audio.voice_bridge --token <device-token>
```

Or add `sali speak` as a new CLI command in `src/sali/cli/main.py` that starts the bridge.

---

## 6. Installation Requirements

```bash
# System dependency (required by Kokoro's phonemizer)
sudo apt install espeak-ng

# Python packages (add to pyproject.toml optional deps)
pip install kokoro faster-whisper sounddevice soundfile torch torchaudio
```

Suggested addition to `pyproject.toml`:
```toml
voice = [
    "kokoro>=0.9",
    "faster-whisper>=1.0",
    "sounddevice>=0.5",
    "soundfile>=0.12",
    "torch>=2.3",
]
```

---

## 7. Summary

| Question | Answer |
| :--- | :--- |
| Does this add a second brain? | **No.** Sali's AgentLoop is unchanged. |
| How does voice reach Sali? | `POST /api/conversation/message` — same endpoint the iPhone uses |
| How does Sali's answer get spoken? | WebSocket `agent.final` event text → Kokoro TTS |
| Does this use any GPU VRAM? | **No.** Both STT and TTS run on the i9-11900K CPU. |
| Can Gemini Live be used correctly? | **No** — Gemini Live has its own LLM and cannot route through Sali's brain. Gemini Cloud TTS (Chirp voices) could be used as a drop-in replacement for Kokoro if higher voice quality is needed and internet is acceptable, but Sali still does all the reasoning. |
