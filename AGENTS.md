# Codex Project Guide

This repository powers the `หุ้นอเมริกา` S&P 500 stock scanner project.

## Project Goal

Generate a daily S&P 500 watchlist, rank early-trend stock setups, rank sector rotation, and optionally send a Thai summary report to Telegram.

## Common Commands

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the full local scan:

```bash
python3 scripts/sp500_early_trend.py --csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv
python3 scripts/sector_rotation.py --stock-csv analyze_stocks_all.csv --csv sector_rotation.csv
python3 scripts/generate_report.py --stock-csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv --sector-csv sector_rotation.csv --output summary.txt --top 30
```

Run a fast report smoke test from existing CSV outputs:

```bash
python3 scripts/generate_report.py --stock-csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv --sector-csv sector_rotation.csv --output /tmp/stock-summary-test.txt --top 3
```

Compile-check changed Python files:

```bash
python3 -m py_compile scripts/generate_report.py scripts/sp500_early_trend.py scripts/sector_rotation.py
```

## Collaboration Rules

- Keep generated CSV and summary outputs uncommitted; they are ignored by `.gitignore`.
- Commit source changes, docs, requirements, and GitHub workflow updates.
- Use pull requests for shared changes so the other company account can review or continue work.
- Do not commit secrets. Telegram values belong in GitHub Actions secrets:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

