# S&P 500 Stock Scanner

เครื่องมือนี้คัดหุ้น S&P 500 ที่มีสัญญาณ “ต้นเทรนด์/ใกล้เบรกจากฐาน” และจัดอันดับ sector rotation เพื่อดูว่าตลาดกำลังเล่นกลุ่มไหน เช่น Energy, Technology, Financials หรือ Real Estate

> คะแนนนี้ไม่ใช่ความน่าจะเป็นจริงหรือคำแนะนำการลงทุน เป็นระบบจัดอันดับเพื่อสร้าง watchlist ก่อนดูกราฟและบริหารความเสี่ยง

## วิธีรันในเครื่อง

```bash
python3 -m pip install -r requirements.txt
python3 scripts/sp500_early_trend.py --csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv
python3 scripts/sector_rotation.py --stock-csv analyze_stocks_all.csv --csv sector_rotation.csv
python3 scripts/generate_report.py --stock-csv analyze_stocks_all.csv --passing-csv analyze_stocks_passing.csv --sector-csv sector_rotation.csv --output summary.txt
```

## GitHub Actions

Workflow อยู่ที่ `.github/workflows/daily-stock-scan.yml`

ตารางรันอัตโนมัติ:

- ทุกวันอังคาร-เสาร์ เวลา `05:30` กรุงเทพฯ
- เทียบเป็น UTC คือ จันทร์-ศุกร์ `22:30`
- กดรันเองได้จาก GitHub tab `Actions` ด้วย `workflow_dispatch`

ผลลัพธ์ถูกเก็บเป็น artifact:

- `analyze_stocks_all.csv`
- `analyze_stocks_passing.csv`
- `sector_rotation.csv`
- `summary.txt`

## ตั้งค่า Telegram

สร้าง Telegram bot ผ่าน `@BotFather` แล้วนำค่าไปตั้งใน GitHub repository:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

ต้องมี 2 secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

หา `TELEGRAM_CHAT_ID` ได้โดยส่งข้อความหา bot ก่อน แล้วรัน:

```bash
python3 scripts/telegram_chat_id.py --token "YOUR_BOT_TOKEN"
```

ถ้าไม่ได้ตั้ง secrets สองตัวนี้ workflow จะยังรันและเก็บ CSV เป็น artifact แต่จะไม่ส่ง Telegram

## เกณฑ์คัดหุ้นต้นเทรนด์

- `price > EMA50`
- `price > EMA200`
- `EMA50 > EMA200` หรือ `EMA50 slope 10D > 0`
- `close > EMA20`
- `RSI14 45-70`
- `price <= EMA20 * 1.08`
- `Relative Strength 10D > SPY` หรือ `RS 5D > SPY` และ `RS 20D improving`
- `close >= 20-day high * 0.95`
- `volume >= 0.8 * avg volume 20`
- `ATR10% <= ATR50% * 1.10`

แต่ละข้อคิด 10 คะแนน รวมเป็น `setup_score` เต็ม 100

## Sector Rotation

ใช้ ETF ตัวแทน 11 กลุ่มของ S&P 500 เทียบกับ `SPY` และผสมกับ breadth จากหุ้นรายตัว:

- 40% rank ของ sector ETF `RS 20D vs SPY`
- 25% rank ของ sector ETF `RS 10D vs SPY`
- 20% สัดส่วนหุ้นใน sector ที่ `setup_score >= 80`
- 15% สัดส่วนหุ้นใน sector ที่ `price > EMA50`

## ตัวเลือกสำคัญ

```bash
--csv path.csv             เขียนตารางทั้งหมดเป็น CSV
--passing-csv path.csv     เขียนเฉพาะหุ้นที่ผ่านครบสูตร
--workers 12               จำนวน parallel downloads
```
