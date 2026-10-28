#!/usr/bin/env python3
"""Final filter for voxtype output: lowercase, then translate Swedish to English.

Why this exists (2026-10-26): whisper's own `translate` task is weak — on a test
clip it turned "Vad betyder det?" into something only barely English, and Adam's
verdict on real dictation was "the translation isn't great". So voxtype now
transcribes with `translate = false` (accurate Swedish text) and this script does
the translation.

Model (2026-10-26): google/translategemma-4b-it, resident on neo as
`translategemma:4b`. It is a dedicated translation model — 3.1 GiB, so it sits
in neo's 11 GB alongside the generalist instead of displacing it — and it is
prompted exactly the way its own chat template does it (see TRANSLATE below).
The generalist qwen3.5:9b it replaced measured fine on this path (8/8 clean,
0.47-0.79 s), so this is a swap for size and specialisation, not for a bug.

Nothing leaves the LAN: whisper.cpp runs on this box's GPU, the translation model
on neo. English dictation never pays the model call — a cheap Swedish heuristic
decides, and the common case (English) is a lowercase and return.

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
# host. Back on neo 2026-10-28: the old resident (gemma4:12b) was a generalist kept
# alive for fiona (now parked), and Ollama had copied its weights into host RAM on
# top of the GPU copy (8.5 GB + 5.9 GB) under host memory pressure. translategemma:4b
# is purpose-built for this: 2.93 GB, entirely in VRAM, runner RSS ~1 GB because it
# loads mmapped, 0.48 s warm, 5.9 s for the one cold load after boot — preloaded at
# boot by the ollama-warm-translategemma unit on neo.
HOST = "http://10.13.13.15:11434/api/chat"
MODEL = "translategemma:4b"
# No num_ctx pin on purpose: neo serves this model at its own OLLAMA_CONTEXT_LENGTH
# and asking for a different window forces a full reload (measured: 3.9 s vs 0.9 s).
# The pin was only ever needed on the PC, where KV cache competed with whisper.
# Adam wants this model resident, not cold-started per dictation (2026-10-26):
# -1 means never unload, and a request-level value overrides the daemon default.
KEEP_ALIVE = -1
# Budget below voxtype's timeout_ms (4000): python start + request + parse.
CALL_TIMEOUT_S = 3.2

# TranslateGemma's chat template is prose, not a system prompt, and the model
# takes no system role at all — its template maps system to user, which would
# emit two user turns back to back. So the instruction rides in the one user
# message, worded as the template words it. Extra rules ("keep hostnames") are
# deliberately NOT bolted on: this is a specialised translator, and the phrasing
# below is the one it was trained on.
TRANSLATE = """You are a professional Swedish (sv) to English (en) translator. Your goal is to accurately convey the meaning and nuances of the original Swedish text while adhering to English grammar, vocabulary, and cultural sensitivities.
Produce only the English translation, without any additional explanations or commentary. Please translate the following Swedish text into English:


{text}"""

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
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": TRANSLATE.format(text=text)}],
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
