# SerialLens

**Serial monitor that actually understands your bytes.** Auto baud-rate detection, protocol heuristics (NMEA 0183 / JSON / binary framing), and AI-powered analysis — turn a screen of garbage into "this is what it is, here's the frame layout, here's what to do next".

See [README.md](README.md) for the full documentation (Chinese).

## Quick start

```bash
pip install seriallens
seriallens demo              # no hardware needed — synthetic GPS stream
seriallens demo --garbled    # bit-shifted garbage, like a real baud mismatch
seriallens detect COM3       # find the right baud rate by scoring readability
seriallens analyze COM3 -b 9600 --ai   # sample + heuristics + LLM -> Markdown report
```

AI works with any OpenAI-compatible endpoint via env vars (`SERIALLENS_API_KEY`, `SERIALLENS_BASE_URL`, `SERIALLENS_MODEL`). Without a key, local heuristics still run and the AI section tells you how to configure.

Heuristic identification is honest: it reports evidence (lines starting with `$`, checksums passed, known frame headers), not verdicts. AI output is advisory — always verify on real hardware.

MIT License.
