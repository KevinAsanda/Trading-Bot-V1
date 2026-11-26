# Demo testing

Use desktop MT5 logged into a dedicated **demo** account. The launcher uses that
session, so you do not need to put your password in the code, chat, or a file.

## Start

From PowerShell in this project:

```powershell
.\.venv\Scripts\python.exe demo_test.py --check
.\.venv\Scripts\python.exe demo_test.py --run
```

The first command checks connectivity, demo account type, trading permissions,
symbol, quotes, history, and existing positions. It never sends orders. The second
command repeats those checks and starts the strategy, which can place demo orders.
The default `EURUSD` matches the verified MetaQuotes-Demo server. For a different
broker, append `--symbol EURUSD#` (use its exact Market Watch symbol), or
`--terminal-path "C:\path\to\terminal64.exe"` to either command.

The quote check waits up to 15 seconds to observe a new tick. It displays the raw
tick/PC clock difference for diagnosis, but verifies freshness from actual tick
progression rather than assuming both clocks use the same offset. A frozen feed
still blocks startup; retry when quotes are updating in Market Watch.

In desktop MT5, enable **Algo Trading**. Under **Tools > Options > Expert Advisors**,
allow automated trading and clear **Disable automated trading via external Python API**.
Do this only on the demo account intended for this test.

## What the profile does

- One symbol, EURUSD by default; MA crossover + RSI, with the H1 trend filter.
- Fixed 0.25% risk per entry, at most one open test position; no Kelly sizing.
- Default strategy settings are frozen; optimized JSON files are not loaded.
- Original session/news/cooldown filters remain active. A passing preflight does
  not guarantee an immediate trade: the strategy still needs a qualifying signal.
- News uses Forex Factory's public JSON export with timezone-aware dates. A failed
  download retries after 15 minutes, rather than every loop. Missing or expired
  calendar data blocks new entries while existing positions remain managed.
- Daily entry loss/profit thresholds are 2% / 4% of equity at launcher startup;
  basket thresholds use the same amounts. Peak drawdown thresholds remain 5% / 10%.
- Separate per-account database, UTF-8 log, and profile under `demo_runs/`.
- Magic number 2601013 and order comment `DemoTest-v1` identify test orders.
- Account type, login, and server are checked in the loop and before each order.
- Duplicate demo engines for the same account are prevented by an OS file lock.
- Telegram is disabled for this profile. The original dashboard still targets
  the original database; use MT5 and the demo log for this run.

## Monitor on desktop and mobile

Sign into the **same account number and exact broker server** in both apps.
On desktop, open **View > Toolbox (Ctrl+T) > Trade**; use **History** for closes.
On mobile, use **Trade** for open positions and **History** for completed deals.
Check the symbol, ticket, volume, entry price, SL, TP, and profit/loss.
You can modify or close positions from mobile when logged in with trading access.
The Python process may subsequently manage stops or place new signals while running.

The Windows computer, desktop terminal, and Python process must remain running
with an internet connection. Mobile MT5 does not run this Python program.
Existing broker-side SL/TP remain after Python stops; Python trailing/breakeven
management stops when Python or the computer stops.

## First test session

1. Run `--check`, then `--run` during an open market and configured session.
2. Wait for a strategy entry; do not relax filters just to force a trade.
3. Confirm the same ticket/position in desktop and mobile, with SL and TP present.
4. Observe a close, and compare broker History with the local demo record.
5. Stop with **Ctrl+C**. This does not close positions. Close any remaining demo
   position in MT5 if you want the test fully flat.

Treat broker History as the reference. Known accounting limitations still need
work: delayed history after bot closes, incomplete exit history, and recovery of
loss-streak state after restart. This setup is for execution testing, not a claim
that the strategy or backtest is profitable.

## Offline regression checks

```powershell
.\.venv\Scripts\python.exe -m unittest test_demo_setup -v
```

These tests mock broker access and never place orders.
