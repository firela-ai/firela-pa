# firela-pa

**firela-pa is a Sovereign Personal AI — a FIRE financial advisor that lives on your own device: your device decides what ever leaves, its memory of you never leaves home, and the cloud is used only when needed — with the question redacted, the provider swappable, and your identity never on the cloud side.**

> **主权个人 AI（Sovereign Personal AI）= 个人服务 × 三权自持 × 零追踪底座**
>
> - **分级权**：什么不出门，由用户自持的本地路由器决定——不是 OS 厂商替你决定
> - **记忆权**：声音与代理记忆永在用户设备内；账本逐笔明细永不上云；聚合数据经字段级 egress 策略有条件进云腿
> - **云腿权**：上云必经涂黑网关，供应商可随时更换
> - **零追踪底座**：云端经令牌可见数据，但无你的身份可拼——「无身份」是机制（令牌隔离、无账号挂钩）

## What this repo is

Source snapshot of the **firela-pa PC package (v1.0)** — read the code, audit the privacy claims, build from source. This repo is a release snapshot: it carries the product source at each tagged version, without development history.

The runnable distribution lives on Hugging Face (one-line install, no build needed):

```bash
bash <(curl -fsSL https://huggingface.co/firela-ai/firela-pa-pc/resolve/main/install.sh)
```

| String you'll meet | What it is |
|---|---|
| `firela-pa` | the CLI command you get after install |
| [`firela-ai/firela-pa-pc`](https://huggingface.co/firela-ai/firela-pa-pc) | the HF distribution (app tarball + GGUF weights + installer) |
| this repo | the source face of the same version |
| `firela-router` | the fine-tuned routing model registered into Ollama |

## What it does (v1.0)

- **Five-branch local routing** — a fine-tuned 1.7B router (Qwen3-1.7B LoRA, GGUF via Ollama) decides where each question goes: local chat / ledger query / portfolio / market quote are answered locally or via your own token; only advice-type questions go to the cloud — redacted first.
- **Redaction gateway** — questions leaving the device pass a deterministic scrubber (CN ID / phone / address / person names, SSN, US phone, Japan My Number, email) before the cloud call. Provider is swappable.
- **Local agent memory** — FIRE assumptions and goals in a local SQLite store (0600), saved only on explicit "remember this", never leaving the device.
- **FIRE simulation engine** — real-return compounding, withdrawal-rate sensitivity sweep, and historical stress-sequence replay (US stock/bond rolling windows 1928–2025, worst-start 1966, window survival rates) — free tier, no paid SaaS needed.
- **Proactive audit** — overspend / large-expense / FIRE-milestone checks over your own ledger, rendered locally.
- **Local-only telemetry** — a local SQLite diary (0600) for your own instrument panel. It never uploads anything.

The egress story is mechanically enforced: all network primitives in `pc/` live behind a single relay exit (`send_to_relay`), asserted in CI by [`ci/egress-gate.py`](ci/egress-gate.py) (see [.github/workflows/egress-gate.yml](.github/workflows/egress-gate.yml)) — adding a second network exit fails the build.

## Repository layout

| Path | Contents |
|---|---|
| `pc/` | the app: orchestrator (routing + five branches), redaction gateway, FIRE simulation, memory, audit, verify, local server (web + OpenAI-compatible API), voice, telemetry |
| `pc/test_*.py` | unit tests, including the redaction attack fixtures |
| `finetune/session_resolver.py` | deterministic multi-turn resolution (context never enters the router) |
| `finetune/prompt-template.txt` | the router's prompt template |
| `ci/egress-gate.py` | CI gate: network egress only via the single relay exit |
| `pc/install.sh` | the one-line installer (same one HF serves) |

Requires Python 3 (stdlib only) and [Ollama](https://ollama.com) ≥ 0.34. macOS (Apple silicon) is the tested surface; the generation model default is `qwen2.5:3b-instruct`.

## Trademarks & names

- **"Firela" / "firela-pa"** are product names of the maintainer. The Apache-2.0 license grants copyright and patent rights to the code and model weights — it does **not** grant any right to use the Firela name or mark. Forks and modified redistributions must use their own name.
- **"Sovereign Personal AI"** is deliberately **not** claimed as a trademark. It is a category term — anyone is free to use the word and the three-rights definition above. We do not claim it, and we invite its use.

## Support boundary

This project is maintained by a single maintainer. Issues are welcome; there is no SLA and no promise of roadmap items. The design goal is that trust does not depend on support: routing, redaction, memory and telemetry run on your device, the egress surface is asserted in CI, and everything the installer fetches is hash-checked.

## License

Apache License 2.0 — see [LICENSE](LICENSE). The routing model is a LoRA fine-tune merge derivative of [Qwen3-1.7B](https://huggingface.co/Qwen/Qwen3-1.7B) (© 2024 Alibaba Cloud / Qwen team, Apache-2.0); see [NOTICE](NOTICE).

## Disclaimer

firela-pa is an AI tool, not a licensed financial advisor. Simulation results are historical scenario analyses, not predictions.
