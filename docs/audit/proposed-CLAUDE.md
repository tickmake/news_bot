# news_bot: project instructions (proposed)

This is a personal swing-trading **research assistant** for US, Norway and EU equities. It is not a trading bot and never places orders.

## Ground truth

- **Research verdict:** the screen, the factors and the scores have **no demonstrated edge**. See `docs/research/2026-10-analytics-findings.md` and `docs/audit/`. Output is labelled research or paper until the protocol in `docs/audit/decision-log.md` D4 passes.
- **Live settings:** the Pi's `.env` overrides the code defaults. Check both before making any claim about the stop multiple, risk, the hold, universes, feeds or the domain allowlist. Read keys selectively and never print secrets.

## Research safeguards

- A result that changes advice needs pass rules written **before** the run, a development/holdout split, and holdout data looked at once.
- Count independent dates, not rows. Holds overlap.
- State the universe and the survivorship status of every result.
- Costs are per market and versioned. Never rewrite `signal_archive` or `published_picks` rows. Add recalculated figures alongside.
- Any risk text must quote measurements taken at the **configured** stop multiple.
- The LLM (Ollama) only ranks or summarises text. It never produces prices, metrics, scores or buy/sell calls.

## Engineering

- Run the tests from a venv in the scratchpad: `python -W error::ResourceWarning -m coverage run -m unittest`. Lint with `ruff check news_bot.py newsbot tests`.
- Schema changes must be additive (`ADD COLUMN`) or versioned migrations, with a backup first.
- Change signal definitions only together with a new `rule_id`.
- Deploy with `scripts/deploy.sh`. Ask before reconfiguring the Pi.
