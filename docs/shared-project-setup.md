# Shared Project Setup

Use this when a second company account needs to work on the same `หุ้นอเมริกา` project.

## What Will Be Shared

- Source code
- GitHub Actions workflow
- README and project instructions
- Scanner scripts and report generator

## What Is Personal To Each Account

- Codex sidebar layout
- Private chat history
- Local folder path
- Local Python environment
- Local generated CSV files

## Setup For Account 2

1. Add account 2 to the GitHub repository with `Write` access.
2. In Codex, create a new project from this repository:

   ```text
   git@github.com:jtanarungsuk-tech/sp500-stock-scanner.git
   ```

3. Use the project name `หุ้นอเมริกา`.
4. Open the repository in Codex and read `AGENTS.md`.
5. Run the smoke test:

   ```bash
   python3 scripts/generate_report.py --stock-csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv --sector-csv sector_rotation.csv --output /tmp/stock-summary-test.txt --top 3
   ```

## GitHub Actions Secrets

The scheduled scan sends Telegram only when these repository secrets exist:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

