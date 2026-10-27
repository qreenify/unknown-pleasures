# Status — unknown-pleasures

**Started:** 2026-10-27
**Phase:** next
**Last touched:** 2026-10-27
**Progress:** Working OSD with two renderers and the voxtype filter set; translation backend for en.py still unsettled after five 2026-10-26 commits.
**Reviewed:** 2026-10-27

## Where we left off

unknown-pleasures is a voice-reactive OSD for voxtype push-to-talk dictation: a black 400×100 panel on the focused monitor while recording, frozen during transcription, hidden when idle. Two renderers are chosen by the `STYLE` constant in `unknown_pleasures.py`, with `"siri"` the default: one bold carrier line with thinner mirrored echoes sharing its zero-crossings, pinched at the edges. `install.sh` and `assets/screenshot.png` are in place, and the 2026-07-03 commits fixed the mic-tap zombie (one `pw-record` per dictation) and made the gate respond to quiet speech (-46 -> -56 dB, softer response curve).

The `voxtype-filters/` directory holds `lowercase.py` and `en.py` (lowercase plus local sv→en translation). The five 2026-10-26 commits walk the translation model choice: qwen3.5:9b on neo with whisper turbo owning the 4080, then the PC's resident 9B (ctx 8192) with neo as fallback, then neo only, then translategemma-4b on neo, then neo's resident gemma4:12b. Branch main, 11 commits, clean tree, last commit 2026-10-27.

## Next step

Decide whether the gemma4:12b-on-neo translation in `voxtype-filters/en.py` is final; if so, write STATUS.md recording the model choice and the neo-only constraint, then commit.

## Blockers

_nothing._

## Open questions

- Which translation backend is final for `voxtype-filters/en.py`: neo's gemma4:12b, translategemma-4b, or the PC's resident 9B with neo fallback?
- Does the PC still lack a usable local model, keeping translation neo-only?
