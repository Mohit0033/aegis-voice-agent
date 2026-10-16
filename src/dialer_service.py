import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from livekit import api

app = FastAPI(title="Aegis Outbound Dialer (FreePBX + Tata SIP)")

LIVEKIT_URL = os.environ["LIVEKIT_URL"].replace("ws://", "http://").replace("wss://", "https://")
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
DIALER_API_KEY = os.environ["DIALER_API_KEY"]
TATA_PILOT_CLI = os.environ.get("TATA_PILOT_CLI", "")   # caller-ID / pilot number of your SIP trunk (set it in .env)
AGENT_NAME = os.environ.get("AGENT_NAME", "aegis-agent")
SIP_TRUNK_ID = os.environ.get("SIP_TRUNK_ID", "")
MAX_CONCURRENT_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "2"))
CALL_WINDOW = os.environ.get("CALL_WINDOW", "").strip()
if CALL_WINDOW and not re.fullmatch(r"\d\d:\d\d-\d\d:\d\d", CALL_WINDOW):
    raise SystemExit(f"CALL_WINDOW must look like 09:00-21:00, got {CALL_WINDOW!r}")

# Hosts that may receive the call outcome (stops a caller of /dial from redirecting transcripts elsewhere).
_n8n_host = urlparse(os.environ.get("N8N_CRM_UPDATE_WEBHOOK", "")).hostname
ALLOWED_CALLBACK_HOSTS = {h.strip() for h in os.environ.get("CALLBACK_ALLOWED_HOSTS", "").split(",") if h.strip()}
ALLOWED_CALLBACK_HOSTS |= {h for h in (_n8n_host, "127.0.0.1", "localhost") if h}

JOBS: dict = {}   # job_id -> response already given, so an n8n retry never dials twice


def require_key(x_api_key: str = Header(default="")):
    if not secrets.compare_digest(x_api_key.encode(), DIALER_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="bad or missing X-Api-Key")


def normalize(phone: str):
    """Accepts 9XXXXXXXXX, 09XXXXXXXXX, 919XXXXXXXXX, +91 9XXXX XXXXX. Returns (digits_to_dial, e164) or None."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"[1-9]\d{9}", digits):
        return None
    return "0" + digits, "+91" + digits   # FreePBX Tata outbound route wants the leading 0


def in_call_window(now=None) -> bool:
    if not CALL_WINDOW:
        return True
    start, end = CALL_WINDOW.split("-")
    now = now or datetime.now(ZoneInfo("Asia/Kolkata"))
    return start <= now.strftime("%H:%M") < end


class DialRequest(BaseModel):
    phone: str
    customer_name: str = "Buyer"
    twenty_person_id: str = ""
    job_id: str = ""        # your own id (e.g. n8n execution id). Sending the same job_id again never dials twice.
    callback_url: str = ""  # where the final outcome + transcript is POSTed (e.g. n8n Wait-node resume URL)


@app.get("/health")
def health():
    return {"ok": True, "window_open": in_call_window(), "window": CALL_WINDOW or "off",
            "max_concurrent_calls": MAX_CONCURRENT_CALLS, "jobs_seen": len(JOBS)}


@app.post("/dial", status_code=202, dependencies=[Depends(require_key)])
async def trigger_call(req: DialRequest):
    if req.job_id and req.job_id in JOBS:
        return JOBS[req.job_id]

    num = normalize(req.phone)
    if not num:
        raise HTTPException(status_code=422, detail=f"invalid phone {req.phone!r}: need a 10-digit Indian number")
    target, e164 = num

    if not in_call_window():
        raise HTTPException(status_code=403, detail=f"outside calling window {CALL_WINDOW} IST")

    if req.callback_url:
        host = urlparse(req.callback_url).hostname
        if urlparse(req.callback_url).scheme not in ("http", "https") or host not in ALLOWED_CALLBACK_HOSTS:
            raise HTTPException(status_code=422, detail=f"callback host {host!r} not allowed (set CALLBACK_ALLOWED_HOSTS)")

    job_id = req.job_id or f"job-{uuid.uuid4().hex[:10]}"
    JOBS[job_id] = {"status": "pending", "job_id": job_id}   # claim it before any await
    if len(JOBS) > 2000:
        JOBS.pop(next(iter(JOBS)))

    ok = False
    lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        rooms = await lkapi.room.list_rooms(api.ListRoomsRequest())
        if len(rooms.rooms) >= MAX_CONCURRENT_CALLS:
            raise HTTPException(status_code=429, detail=f"{len(rooms.rooms)} calls already running (max {MAX_CONCURRENT_CALLS})",
                                headers={"Retry-After": "60"})

        trunk_id = SIP_TRUNK_ID
        if not trunk_id:
            trunks = await lkapi.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            if len(trunks.items) != 1:
                raise HTTPException(status_code=500,
                                    detail=f"found {len(trunks.items)} outbound SIP trunks; run provision.py or set SIP_TRUNK_ID")
            trunk_id = trunks.items[0].sip_trunk_id

        room_name = f"outbound-{uuid.uuid4().hex[:8]}"
        metadata = json.dumps({
            "job_id": job_id,
            "twenty_person_id": req.twenty_person_id,
            "customer_name": req.customer_name,
            "phone": target,
            "phone_e164": e164,
            "callback_url": req.callback_url,
        })
        try:
            # 0. Pre-create the room with the CRM metadata so ctx.room.metadata always has customer_name
            await lkapi.room.create_room(
                api.CreateRoomRequest(name=room_name, metadata=metadata, empty_timeout=120)
            )
            # 1. Dispatch the Aegis agent into the room with the CRM context (arrives as ctx.job.metadata)
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name, metadata=metadata)
            )
            # 2. Tell LiveKit SIP to dial through the FreePBX Tata trunk (name + metadata on the participant too)
            sip_part = await lkapi.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk_id,
                    sip_call_to=target,
                    sip_number=TATA_PILOT_CLI,
                    room_name=room_name,
                    participant_identity=f"sip-{target}",
                    participant_name=req.customer_name,
                    participant_metadata=metadata,
                )
            )
        except Exception as e:
            try:
                await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
            except Exception:
                pass
            raise HTTPException(status_code=502, detail=f"LiveKit/SIP error: {e}")

        resp = {"status": "dialing", "job_id": job_id, "room": room_name, "target": target, "phone": e164,
                "call_id": sip_part.sip_call_id, "started_at": time.time(),
                "callback": req.callback_url or "default CRM webhook"}
        JOBS[job_id] = resp
        ok = True
        return resp
    finally:
        if not ok:
            JOBS.pop(job_id, None)   # failed attempts can be retried with the same job_id
        await lkapi.aclose()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8060)   # reached from n8n through `tailscale serve`, see cell 6
