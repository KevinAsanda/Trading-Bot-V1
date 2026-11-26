from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import sqlite3
import pandas as pd
import uvicorn
import json
import os
import MetaTrader5 as mt5
from datetime import datetime

app = FastAPI()
DB_NAME = "trading_data.db"
LOG_FILE = "trading_bot.log"

@app.post("/control")
async def bot_control(command: str = Form(...)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE bot_control SET command = ? WHERE id = 1", (command,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    if not mt5.initialize():
        return "MT5 Init Failed"

    conn = sqlite3.connect(DB_NAME)
    equity_df = pd.read_sql_query("SELECT time, equity FROM equity_history ORDER BY time ASC", conn)
    trades_df = pd.read_sql_query("SELECT * FROM trades WHERE profit IS NOT NULL ORDER BY open_time DESC", conn)
    control_df = pd.read_sql_query("SELECT command, status FROM bot_control WHERE id = 1", conn)
    news_df = pd.read_sql_query("SELECT * FROM upcoming_news WHERE time > datetime('now') ORDER BY time ASC LIMIT 1", conn)
    conn.close()

    # Live Positions
    open_positions = mt5.positions_get()
    mt5.shutdown()

    chart_labels = equity_df['time'].tolist()
    chart_values = equity_df['equity'].tolist()
    current_cmd = control_df['command'].iloc[0] if not control_df.empty else "START"
    bot_status = control_df['status'].iloc[0] if not control_df.empty else "OFFLINE"
    
    # Performance
    win_rate, profit_factor = 0, 0
    symbol_stats = []
    if not trades_df.empty:
        wins = trades_df[trades_df['profit'] > 0]
        win_rate = (len(wins) / len(trades_df)) * 100
        gross_profit = wins['profit'].sum()
        gross_loss = abs(trades_df[trades_df['profit'] < 0]['profit'].sum())
        profit_factor = gross_profit / gross_loss if gross_loss != 0 else float('inf')
        for sym in trades_df['symbol'].unique():
            s_df = trades_df[trades_df['symbol'] == sym]
            symbol_stats.append({"symbol": sym, "win_rate": (len(s_df[s_df['profit']>0])/len(s_df))*100, "profit": s_df['profit'].sum(), "trades": len(s_df)})

    # Logs
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f: logs = f.readlines()[-30:]
    formatted_logs = "".join([f"<div style='color: {'#f6465d' if 'ERR' in l else '#0ecb81' if 'INFO' in l else '#f0b90b'}; font-family: monospace; font-size: 0.8rem; border-bottom: 1px solid #2d3339; padding: 4px 0;'>{l}</div>" for l in logs])

    next_news_time = news_df['time'].iloc[0] if not news_df.empty else None
    next_news_title = news_df['title'].iloc[0] if not news_df.empty else "None"

    html_content = f"""
    <html>
        <head>
            <title>MT5 Mission Control</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
            <style>
                body {{ background-color: #0b0e11; color: #e9ecef; padding: 20px; font-family: 'Inter', sans-serif; }}
                .card {{ background-color: #1e2329; border: none; border-radius: 12px; margin-bottom: 20px; padding: 20px; }}
                .metric-label {{ color: #848e9c; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px; }}
                .metric-value {{ font-size: 1.4rem; font-weight: 700; }}
                .status-active {{ color: #0ecb81; }}
                .news-alert {{ background: linear-gradient(90deg, #4b1a1a, #1e2329); border-left: 4px solid #f6465d; }}
                .log-container {{ height: 250px; overflow-y: auto; background: #000; padding: 15px; border-radius: 8px; }}
                .symbol-badge {{ background: #2b3139; padding: 4px 8px; border-radius: 4px; font-weight: 600; font-size: 0.85rem; }}
            </style>
            <meta http-equiv="refresh" content="15">
        </head>
        <body>
            <div class="container">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <h2 class="m-0">🚀 MT5 Mission Control</h2>
                    <div class="text-end">
                        <small class="metric-label">SYSTEM STATUS</small>
                        <div class="status-active fw-bold">● {bot_status}</div>
                    </div>
                </div>

                <div class="card news-alert d-flex flex-row justify-content-between align-items-center">
                    <div><span class="metric-label">NEXT HIGH IMPACT NEWS</span><div class="fw-bold">{next_news_title}</div></div>
                    <div class="text-end"><span class="metric-label">COUNTDOWN</span><div id="countdown" class="metric-value text-danger">--:--:--</div></div>
                </div>

                <div class="row">
                    <div class="col-md-3">
                        <div class="card h-100">
                            <h6 class="metric-label mb-3">Command Center</h6>
                            <form action="/control" method="post" class="d-grid gap-2">
                                <button type="submit" name="command" value="START" class="btn btn-sm btn-outline-success">RESUME</button>
                                <button type="submit" name="command" value="PAUSE" class="btn btn-sm btn-outline-warning">PAUSE</button>
                                <button type="submit" name="command" value="CLOSE_ALL" class="btn btn-sm btn-danger">PANIC EXIT</button>
                                <hr class="border-secondary">
                                <h6 class="metric-label mb-2 mt-1">Manual Override</h6>
                                <div class="d-flex gap-2">
                                    <button type="submit" name="command" value="MANUAL_BUY:EURUSD#" class="btn btn-sm btn-success w-100">BUY EURUSD#</button>
                                    <button type="submit" name="command" value="MANUAL_SELL:EURUSD#" class="btn btn-sm btn-danger w-100">SELL EURUSD#</button>
                                </div>
                            </form>
                        </div>
                    </div>
                    <div class="col-md-9">
                        <div class="card h-100">
                            <h6 class="metric-label mb-3">Live Open Positions ({len(open_positions) if open_positions else 0})</h6>
                            <table class="table table-dark table-sm m-0">
                                <thead><tr><th>TICKET</th><th>SYMBOL</th><th>TYPE</th><th>PROFIT</th><th>ACTION</th></tr></thead>
                                <tbody>
                                    {"".join([f"<tr><td>{p.ticket}</td><td><span class='symbol-badge'>{p.symbol}</span></td><td>{'BUY' if p.type==0 else 'SELL'}</td><td class='{('text-success' if p.profit>0 else 'text-danger')}'>${p.profit:.2f}</td><td><form action='/control' method='post' class='m-0'><input type='hidden' name='command' value='MANUAL_CLOSE:{p.ticket}'><button type='submit' class='btn btn-xs btn-danger py-0 px-2' style='font-size: 0.7rem;'>CLOSE</button></form></td></tr>" for p in open_positions]) if open_positions else "<tr><td colspan='5' class='text-muted text-center'>No open positions.</td></tr>"}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div class="row">
                    <div class="col-md-5">
                        <div class="card">
                            <h6 class="metric-label mb-3">Portfolio Performance</h6>
                            <table class="table table-dark table-sm m-0">
                                <thead><tr><th>SYMBOL</th><th>WIN%</th><th>PROFIT</th></tr></thead>
                                <tbody>
                                    {"".join([f"<tr><td><span class='symbol-badge'>{s['symbol']}</span></td><td>{s['win_rate']:.0f}%</td><td class='{('text-success' if s['profit']>0 else 'text-danger')}'>${s['profit']:.2f}</td></tr>" for s in symbol_stats])}
                                </tbody>
                            </table>
                        </div>
                        <div class="card">
                            <h6 class="metric-label mb-3">Live System Logs</h6>
                            <div class="log-container">{formatted_logs}</div>
                        </div>
                    </div>
                    <div class="col-md-7">
                        <div class="card h-100">
                            <h6 class="metric-label mb-3">Equity Growth</h6>
                            <canvas id="equityChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>
            <script>
                const newsTimeStr = "{next_news_time}";
                if (newsTimeStr === "None") {{
                    document.getElementById("countdown").innerHTML = "--:--:--";
                }} else {{
                    const nTime = new Date(newsTimeStr).getTime();
                    setInterval(() => {{
                        const d = nTime - new Date().getTime();
                        if (d < 0) {{ document.getElementById("countdown").innerHTML = "EVENT ACTIVE"; return; }}
                        const h = Math.floor(d / 3600000), m = Math.floor((d % 3600000) / 60000), s = Math.floor((d % 60000) / 1000);
                        document.getElementById("countdown").innerHTML = h + "h " + m + "m " + s + "s";
                    }}, 1000);
                }}
                new Chart(document.getElementById('equityChart'), {{
                    type: 'line',
                    data: {{
                        labels: {json.dumps(chart_labels)},
                        datasets: [{{ data: {json.dumps(chart_values)}, borderColor: '#0d6efd', backgroundColor: 'rgba(13, 110, 253, 0.05)', fill: true, tension: 0.3, pointRadius: 0 }}]
                    }},
                    options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ display: false }}, y: {{ grid: {{ color: '#2d3339' }} }} }} }}
                }});
            </script>
        </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
