#!/usr/bin/env python3
"""Final filter for voxtype output: lowercase, then translate Swedish to English.

Why this exists (2026-10-26): whisper's own `translate` task is weak — on a test
clip it turned "Vad betyder det?" into something only barely English, and Adam's
verdict on real dictation was "the translation isn't great". So voxtype now
transcribes with `translate = false` (accurate Swedish text) and this script does
the translation with the local qwen3.6:35b-a3b on this box — measured 0.8 s warm
for a sentence, and the output reads like a person wrote it:

    "nä nu jävlar har plex kraschat igen, kolla loggarna och starta om containern"
 -> "Ah, fuck, Plex has crashed again. Check the logs and restart the container."

Nothing leaves the LAN: whisper.cpp runs on this box's GPU, the translation model on neo.
English dictation never pays the model call — a cheap Swedish heuristic decides,
and the common case (English) is a lowercase and return.

Contract: text on stdin, filtered text on stdout. voxtype falls back to the raw
transcription on timeout or error, so every failure path here echoes input. A
broken filter must never eat dictation.

Supersedes lowercase.py (kept on disk, no longer referenced by config).
"""
import json
import re
import sys
import urllib.request

# Translation runs on neo, never on this box. Adam's call 2026-10-26: no local
# weights on the PC at all — the PC is a client and a GPU for whisper, not a model
# host. neo's qwen3.5:9b answers in ~0.5-0.9 s over the LAN, which is fast enough
# that a cloud round trip would buy nothing and cost privacy.
HOST = "http://10.13.13.15:11434/api/chat"
MODEL = "qwen3.5:9b"
# No num_ctx pin on purpose: neo serves this model at its own OLLAMA_CONTEXT_LENGTH
# and asking for a different window forces a full reload (measured: 3.9 s vs 0.9 s).
# The pin was only ever needed on the PC, where KV cache competed with whisper.
KEEP_ALIVE = "24h"
# Budget below voxtype's timeout_ms (4000): python start + request + parse.
CALL_TIMEOUT_S = 3.2

SYSTEM = (
    "You translate Swedish speech transcripts into English. "
    "Output only the English translation: no quotes, no commentary, no explanation. "
    "Keep names, hostnames and technical terms as they are. "
    "Keep the speaker's tone and profanity. Do not add words that are not there."
)

# Swedish tells: the letters English does not use, plus high-frequency words that
# survive ASR even when a speaker drops diacritics.
LETTERS = re.compile(r"[åäöÅÄÖ]")
WORDS = set(
    "och att är jag det inte för med som på den till av en ett du har vi men om "
    "kan ska vill vad hur när nu då bara också från mig dig hon honom "
    "vad betyder heter här där mycket bra dålig tack hej nej eller aldrig alltid "
    "gör göra kör kolla starta om loggarna behöver hjälp något någon".split()
)


def looks_swedish(text: str) -> bool:
    if LETTERS.search(text):
        return True
    words = re.findall(r"[a-z]+", text.lower())
    # Three distinct tells, not two: short function words ("en", "det", "om") are
    # shared with German, Norwegian and Danish, and two of them fire on mixed
    # sentences that are mostly English.
    return len({w for w in words if w in WORDS}) >= 3


def translate(text: str) -> str | None:
    body = json.dumps(
        {
            "model": MODEL,
            "think": False,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            # Pin the context. Without it ollama serves the model at the box-wide
            # OLLAMA_CONTEXT_LENGTH (65536 here), whose KV cache makes the 9B eat
            # 7.5 GB of VRAM and starve whisper; 8192 measures 6.4 GB and leaves
            # the ~2.9 GB whisper needs. Translation inputs are one sentence.
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": text},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        HOST, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as res:
        payload = json.load(res)
    out = (payload.get("message") or {}).get("content", "")
    # Reasoning-capable models can still emit a stray tag; keep the prose.
    out = re.sub(r"^```.*?\n|```$", "", out.strip(), flags=re.S).strip()
    return out or None


def main() -> int:
    text = sys.stdin.read()
    if not text.strip():
        return 0
    try:
        text = text.lower()
    except Exception:
        pass  # never die on a weird codepoint

    if looks_swedish(text):
        try:
            english = translate(text)
            if english:
                text = english.lower()
        except Exception:
            pass  # timeout or model down: ship the Swedish, as before

    try:
        sys.stdout.write(text)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
