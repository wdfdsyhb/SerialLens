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

## Benchmark: raw LLM vs SerialLens truth-binding

One controlled experiment (v0.2, Sep 2026): the same 4-frame sample of a synthetic sensor
stream (`AA55 | len | temp u16le ×0.01 | humi u16be ×0.1 | seq | CRC8`) was given to
(a) a raw LLM with the hex dump and heuristics, and (b) SerialLens `learn` with 2 truth labels.

| Result | Raw LLM (API call) | SerialLens v0.2 learn |
| --- | --- | --- |
| Header / frame length | ✅ | ✅ (incl. `byte@2+5` rule) |
| Temperature field | ❌ misread as sequence number | ✅ exact: `u16le@3 ×0.01` |
| Humidity field | ❌ misled by a coincidence | ✅ exact: `u16be@5 ×0.1` |
| seq / checksum | ✅ | ✅ / honestly marked unknown |
| Cost | 924 tokens / 4.4 s online | 0 tokens / <1 s offline |

The LLM's own conclusion: *"run a controlled experiment — change temperature alone to locate
its bytes"* — which is exactly what `learn` automates. Precise data-field location comes from
algorithms plus truth labels, not from guessing. See the Chinese README for the full write-up.

## The Lens series

SerialLens is part of the **Lens series** — one philosophy: face unknown hardware and
protocols, report only evidence, never claim certainty.

| Tool | Domain | Status |
| --- | --- | --- |
| **SerialLens** | Serial / UART protocol identification & decoding | ✅ available |
| [modbus-lens](https://github.com/wdfdsyhb/modbus-lens) | Modbus RTU slave discovery & register recon | ✅ available |
| can-lens | CAN bus traffic profiling | 🚧 planned |
| ble-lens | BLE GATT service recon | 💡 idea |

MIT License.
