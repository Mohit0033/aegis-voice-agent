import asyncio, os, json, httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Aegis Call Cockpit")

EVENT_LOG = os.path.join(os.environ.get("AEGIS_DATA_DIR", "/content"), "live_events.jsonl")

class QuickDial(BaseModel):
    phone: str
    customer_name: str = "Buyer"
    twenty_person_id: str = ""

def read_all_events():
    if not os.path.exists(EVENT_LOG):
        return []
    out = []
    with open(EVENT_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try: out.append(json.loads(line))
            except Exception: pass
    return out

@app.get("/api/summary")
def get_summary():
    events = read_all_events()
    calls = [e for e in events if e.get("type") == "call_started"]
    leads = [e for e in events if e.get("type") == "lead_converted"]
    ended = [e for e in events if e.get("type") == "call_ended"]
    total_calls = max(len(calls), 1)
    rate = round((len(leads) / total_calls) * 100, 1) if calls else 0.0
    return {
        "total_calls": len(calls),
        "leads_captured": len(leads),
        "conversion_rate": f"{rate}%",
        "recent_leads": leads[-10:],
        "recent_summaries": ended[-5:],
        "history": events[-60:],
    }

@app.post("/api/dial")
async def proxy_dial(req: QuickDial):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("http://127.0.0.1:8060/dial", json=req.model_dump(),
                         headers={"X-Api-Key": os.environ.get("DIALER_API_KEY", "")})
        return JSONResponse(r.json(), status_code=r.status_code)

@app.get("/events")
async def event_stream(request: Request):
    async def tail():
        last_pos = os.path.getsize(EVENT_LOG) if os.path.exists(EVENT_LOG) else 0
        while True:
            if await request.is_disconnected():
                break
            if os.path.exists(EVENT_LOG):
                cur_size = os.path.getsize(EVENT_LOG)
                if cur_size > last_pos:
                    with open(EVENT_LOG, "r", encoding="utf-8") as f:
                        f.seek(last_pos)
                        lines = f.readlines()
                        last_pos = f.tell()
                        for line in lines:
                            if line.strip():
                                yield f"data: {line.strip()}\n\n"
            await asyncio.sleep(0.3)
    return StreamingResponse(tail(), media_type="text/event-stream")

@app.get("/", response_class=HTMLResponse)
def get_ui():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Aegis Voice Cockpit — M.A. Creations</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { margin:0; font-family: -apple-system, system-ui, sans-serif; background: #0b0f19; color: #e2e8f0; padding: 20px; }
            .top-bar { display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; border-bottom:1px solid #1e293b; padding-bottom:12px; }
            .dial-bar { display:flex; gap:8px; background:#151e33; padding:12px; border-radius:8px; border:1px solid #1e293b; margin-bottom:18px; }
            .dial-bar input { background:#0b0f19; border:1px solid #334155; color:#fff; padding:8px 12px; border-radius:6px; font-size:13px; }
            .dial-bar button { background:#0284c7; color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-weight:600; }
            .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 20px; }
            .card { background: #151e33; border: 1px solid #1e293b; border-radius: 8px; padding: 16px; }
            .card-title { font-size: 11px; text-transform: uppercase; color: #94a3b8; margin-bottom: 4px; }
            .card-val { font-size: 26px; font-weight: 700; color: #38bdf8; }
            .main { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
            .panel { background: #151e33; border: 1px solid #1e293b; border-radius: 8px; padding: 16px; height: 480px; display: flex; flex-direction: column; }
            .panel-h { margin: 0 0 12px 0; font-size: 14px; color: #cbd5e1; font-weight: 600; }
            .stream { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
            .msg { padding: 8px 12px; border-radius: 6px; font-size: 13px; line-height: 1.4; max-width: 85%; }
            .caller { background: #1e293b; align-self: flex-start; border-left: 3px solid #38bdf8; }
            .agent { background: #0f3d4c; align-self: flex-end; border-right: 3px solid #06b6d4; }
            .sys { background: #111827; align-self: center; font-size: 11px; color: #94a3b8; padding: 4px 10px; border-radius: 99px; }
            .lead-row { border-bottom: 1px solid #1e293b; padding: 8px 0; font-size: 13px; }
        </style>
    </head>
    <body>
        <div class="top-bar">
            <div>
                <h2 style="margin:0;">Aegis Voice Cockpit — M.A. Creations</h2>
                <div style="font-size:12px; color:#94a3b8;">Gemma 4 E4B + Indic-Mio | FreePBX + SIP trunk</div>
            </div>
            <span style="background:#064e3b; color:#34d399; padding:4px 10px; border-radius:99px; font-size:12px; font-weight:600;">● LIVE</span>
        </div>

        <div class="dial-bar">
            <input id="d_phone" placeholder="Phone (10-digit Indian number)">
            <input id="d_name" placeholder="Buyer name">
            <input id="d_crm" placeholder="Twenty CRM Person ID (optional)">
            <button onclick="dialNow()">Dial via Tata SIP</button>
            <span id="d_status" style="font-size:12px; color:#94a3b8; align-self:center;"></span>
        </div>

        <div class="grid">
            <div class="card"><div class="card-title">Total Calls</div><div class="card-val" id="cnt_calls">0</div></div>
            <div class="card"><div class="card-title">B2B Links Sent</div><div class="card-val" id="cnt_leads" style="color:#10b981;">0</div></div>
            <div class="card"><div class="card-title">Conversion Rate</div><div class="card-val" id="cnt_rate">0.0%</div></div>
        </div>
        <div class="main">
            <div class="panel">
                <div class="panel-h">Real-Time Call Transcription</div>
                <div class="stream" id="chat"></div>
            </div>
            <div class="panel">
                <div class="panel-h">Converted Leads & Actions</div>
                <div class="stream" id="leads"></div>
            </div>
        </div>

        <script>
            async function dialNow() {
                const st = document.getElementById('d_status');
                st.innerText = 'Dialing...';
                try {
                    const res = await fetch('/api/dial', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            phone: document.getElementById('d_phone').value,
                            customer_name: document.getElementById('d_name').value,
                            twenty_person_id: document.getElementById('d_crm').value
                        })
                    });
                    const j = await res.json();
                    st.innerText = j.status ? `Ringing (${j.room})` : JSON.stringify(j);
                } catch (e) { st.innerText = 'Error: ' + e; }
            }

            function updateStats() {
                fetch('/api/summary').then(r => r.json()).then(d => {
                    document.getElementById('cnt_calls').innerText = d.total_calls;
                    document.getElementById('cnt_leads').innerText = d.leads_captured;
                    document.getElementById('cnt_rate').innerText = d.conversion_rate;
                });
            }

            fetch('/api/summary').then(r => r.json()).then(data => {
                document.getElementById('cnt_calls').innerText = data.total_calls;
                document.getElementById('cnt_leads').innerText = data.leads_captured;
                document.getElementById('cnt_rate').innerText = data.conversion_rate;
                data.history.forEach(renderEvent);
            });

            const es = new EventSource('/events');
            es.onmessage = (e) => renderEvent(JSON.parse(e.data));

            function renderEvent(ev) {
                const chat = document.getElementById('chat');
                const leads = document.getElementById('leads');
                if (ev.type === 'call_started') {
                    const div = document.createElement('div');
                    div.className = 'sys';
                    div.innerText = `Call Connected: ${ev.caller} (${ev.customer_name || 'Buyer'})`;
                    chat.appendChild(div);
                    updateStats();
                } else if (ev.type === 'transcript') {
                    const div = document.createElement('div');
                    div.className = 'msg ' + (ev.speaker === 'caller' ? 'caller' : 'agent');
                    div.innerText = (ev.speaker === 'caller' ? 'Caller: ' : 'Mio: ') + ev.text;
                    chat.appendChild(div);
                    chat.scrollTop = chat.scrollHeight;
                } else if (ev.type === 'lead_converted') {
                    const row = document.createElement('div');
                    row.className = 'lead-row';
                    const who = document.createElement('strong');
                    who.textContent = ev.customer || 'Buyer';
                    const ph = document.createElement('span');
                    ph.textContent = ' (' + (ev.phone || '') + ')';
                    const act = document.createElement('div');
                    act.style.cssText = 'color:#10b981; font-size:11px;';
                    act.textContent = '\u2713 ' + (ev.action || '');
                    row.append(who, ph, act);
                    leads.prepend(row);
                    updateStats();
                } else if (ev.type === 'call_not_answered') {
                    const div = document.createElement('div');
                    div.className = 'sys';
                    div.textContent = 'Not answered: ' + (ev.phone || '') + ' (' + (ev.outcome || '') + ')';
                    chat.appendChild(div);
                }
            }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8050)
