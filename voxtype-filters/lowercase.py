#!/usr/bin/env python3
"""Lowercase dictated text before voxtype outputs it.

Adam dictates in en+sv and wants everything lowercase. voxtype has no native
case option (the "lowercase" strings in the binary are Unicode tables from the
regex crate, not a setting), so this runs as [output.post_process].

Recreated 2026-08-19 after the original was lost with ~/.config/voxtype/.
Behaviour is intentionally minimal: lowercase, nothing else.

Contract: text arrives on stdin, processed text goes to stdout. On any error
the input is echoed back unchanged - a broken filter must never eat dictation.
str.lower() is Unicode-aware, so Swedish Å/Ä/Ö lowercase correctly.
"""
import sys

def main() -> int:
    try:
        text = sys.stdin.read()
    except Exception:
        return 0

    try:
        sys.stdout.write(text.lower())
    except Exception:
        # Never swallow the transcript; fall back to the original text.
        sys.stdout.write(text)
    return 0

if __name__ == "__main__":
    sys.exit(main())
