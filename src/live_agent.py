import asyncio
import base64
import io
import json
import logging
import math
import os
import re
import threading
import time
import uuid

import httpx
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

from livekit import agents, rtc, api
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    stt,
    tts,
)
from livekit.agents.language import LanguageCode
from livekit.agents.types import NOT_GIVEN
from livekit.plugins import openai, silero

log = logging.getLogger("live_agent")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "google/gemma-4-E4B-it-qat-mobile-ct")
MIO_URL = os.environ.get("MIO_URL", "http://127.0.0.1:8003")
AGENT_NAME = os.environ.get("AGENT_NAME", "aegis-agent")
SR = 24000
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AEGIS_DATA_DIR", "/content")
SYS_FILE = os.environ.get("SYS_PROMPT_FILE", os.path.join(HERE, "prompts", "sys_prompt.txt"))
METRICS_LOG = os.path.join(DATA_DIR, "live_metrics.jsonl")
EVENT_LOG = os.path.join(DATA_DIR, "live_events.jsonl")
DEBUG_DIR = os.path.join(DATA_DIR, "tts_debug")

N8N_WHATSAPP_WEBHOOK = os.environ.get("N8N_WHATSAPP_WEBHOOK", "http://127.0.0.1:5678/webhook/send-b2b-link")
N8N_CRM_UPDATE_WEBHOOK = os.environ.get("N8N_CRM_UPDATE_WEBHOOK", "http://127.0.0.1:5678/webhook/update-twenty-crm")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")

INTERRUPT_MIN_SECS = float(os.environ.get("INTERRUPT_MIN_SECS", "0.6"))
BACKCHANNEL_MAX_SECS = float(os.environ.get("BACKCHANNEL_MAX_SECS", "1.6"))
RESUME_AFTER_SECS = float(os.environ.get("RESUME_AFTER_SECS", "0.8"))
# Only pure filler sounds are ignored while she speaks. "yes / haan / ok" must count as real answers.
BACKCHANNEL_WORDS = set("hmm hm hmmm mm हम्म हम्मम हूं हुं uh huh".split())

SESSIONS: dict = {}
UTTER: dict = {}
CALL_STATE: dict = {}

SYSTEM_PROMPT = open(SYS_FILE, encoding="utf-8").read() if os.path.exists(SYS_FILE) else ""

GEMMA_TRANSCRIBE = (
    "Transcribe accurately what the speaker says on this phone call in India (English, Hindi in Devanagari, or Hinglish). "
    "If the audio contains only silence, breathing, or line noise with no human words, output only [silence]. "
    "Output only the exact transcript."
)

FAREWELL_PHRASES = ("have a great day", "have a good day", "goodbye", "good bye", "आपका दिन शुभ हो")
CALLER_BYE = ("bye", "बाय", "रखता हूँ", "नमस्ते")
REPLY_THANKS = ("thank you", "धन्यवाद", "नमस्ते", "शुक्रिया")


def push_event(ev_type: str, data: dict):
    try:
        payload = {"timestamp": time.time(), "type": ev_type, **data}
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def to_e164(num: str) -> str:
    """09XXXXXXXXX / 9XXXXXXXXX / 919XXXXXXXXX / +91 9XXXX XXXXX -> +919XXXXXXXXX. "" if not an Indian number."""
    digits = re.sub(r"\D", "", num or "")
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return ""


def _n8n_headers() -> dict:
    return {"X-Api-Key": N8N_WEBHOOK_SECRET} if N8N_WEBHOOK_SECRET else {}


async def trigger_n8n_whatsapp(phone: str, customer_name: str, room_name: str):
    state = CALL_STATE.setdefault(room_name, {})
    state["b2b_requested"] = True
    if state.get("b2b_webhook_called"):
        return
    state["b2b_webhook_called"] = True
    e164 = to_e164(phone)
    if not re.fullmatch(r"\+91[6-9]\d{9}", e164):
        print(f"[n8n] no WhatsApp-capable number in {phone!r}; B2B link not sent", flush=True)
        push_event("lead_skipped", {"customer": customer_name, "phone": phone, "reason": "no_whatsapp_number"})
        return
    payload = {"phone": e164, "name": customer_name, "portal_url": "https://b2b.macreations.in", "timestamp": time.time()}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(N8N_WHATSAPP_WEBHOOK, json=payload, headers=_n8n_headers())
            r.raise_for_status()
        state["b2b_sent"] = True
        push_event("lead_converted", {"customer": customer_name, "phone": e164, "action": "b2b_link_sent"})
        print(f"[n8n] Triggered B2B WhatsApp link for {e164}", flush=True)
    except Exception as e:
        print(f"[n8n] WhatsApp webhook error (marked b2b_requested=True): {e}", flush=True)


async def trigger_transfer(room_name: str, participant_identity: str, ext: str = "101"):
    CALL_STATE.setdefault(room_name, {})["transferred"] = True
    push_event("call_transferred", {"room": room_name, "identity": participant_identity, "ext": ext})
    lk_url = os.environ["LIVEKIT_URL"].replace("ws://", "http://").replace("wss://", "https://")
    lkapi = api.LiveKitAPI(lk_url, os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    try:
        await lkapi.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=room_name,
                participant_identity=participant_identity,
                transfer_to=f"tel:{ext}",
                play_dialtone=False,
            )
        )
        print(f"[call] Transferred {participant_identity} to extension {ext}", flush=True)
    except Exception as e:
        print(f"[call] Transfer failed: {e}", flush=True)
    finally:
        await lkapi.aclose()


async def _transfer_after_speech(speech, room_name: str, participant_id: str, ext: str = "101"):
    """Let her finish saying "transferring ..." before the SIP transfer cuts the audio."""
    try:
        if speech is not None:
            await asyncio.wait_for(speech.wait_for_playout(), 20)
    except Exception:
        pass
    await asyncio.sleep(0.4)
    await trigger_transfer(room_name, participant_id, ext)


async def _hangup_after_speech(speech, room_name: str):
    """Wait for her goodbye to finish playing, then delete the room to hang up the SIP call."""
    try:
        if speech is not None:
            await asyncio.wait_for(speech.wait_for_playout(), 15)
    except Exception:
        pass
    await asyncio.sleep(0.8)
    lk_url = os.environ["LIVEKIT_URL"].replace("ws://", "http://").replace("wss://", "https://")
    lkapi = api.LiveKitAPI(lk_url, os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    try:
        await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
        print(f"[call] Hung up room {room_name} cleanly after goodbye.", flush=True)
    except Exception as e:
        print(f"[call] Hangup delete_room error: {e}", flush=True)
    finally:
        await lkapi.aclose()


async def post_call_outcome(summary: dict, callback_url: str = "") -> bool:
    """POST the final outcome to n8n (per-job callback_url first, then the default CRM webhook).
    If every attempt fails the summary is kept in the outbox folder so it can be replayed."""
    urls = list(dict.fromkeys(u for u in (callback_url, N8N_CRM_UPDATE_WEBHOOK) if u))
    for url in urls:
        delay = 1.0
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.post(url, json=summary, headers=_n8n_headers())
                    r.raise_for_status()
                print(f"[n8n] call outcome '{summary.get('outcome')}' delivered to {url}", flush=True)
                return True
            except Exception as e:
                print(f"[n8n] outcome post to {url} failed (attempt {attempt}): {e!r}", flush=True)
                await asyncio.sleep(delay)
                delay *= 2
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        name = summary.get("job_id") or summary.get("room_name") or uuid.uuid4().hex
        with open(os.path.join(OUTBOX_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
        print(f"[n8n] outcome NOT delivered; saved to {OUTBOX_DIR}/{name}.json", flush=True)
    except Exception as e:
        print(f"[n8n] could not save undelivered outcome: {e!r}", flush=True)
    return False


def _wav_b64(pcm16k: np.ndarray) -> str:
    buf = io.BytesIO()
    wavfile.write(buf, 16000, (np.clip(pcm16k, -1, 1) * 32767).astype(np.int16))
    return base64.b64encode(buf.getvalue()).decode()


def _audio_part(b64: str) -> dict:
    return {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}


def is_backchannel(text: str) -> bool:
    words = re.sub(r"[^\w\s\u0900-\u097f]", " ", (text or "").lower()).split()
    return 0 < len(words) <= 4 and all(w in BACKCHANNEL_WORDS for w in words)


async def _transcribe_now(b64: str, timeout: float):
    body = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": 30,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [{"type": "text", "text": GEMMA_TRANSCRIBE}, _audio_part(b64)]}]}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            j = (await c.post(f"{LLM_BASE_URL}/chat/completions", json=body)).json()
            return (j["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None


async def _gemma_transcribe(key: str) -> None:
    u, t0 = UTTER[key], time.time()
    body = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": 120,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [{"type": "text", "text": GEMMA_TRANSCRIBE}, _audio_part(u["b64"])]}]}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{LLM_BASE_URL}/chat/completions", json=body)
            raw_txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if "[silence]" in raw_txt.lower() or not raw_txt:
                u["text"] = ""
                u["silent"] = True
            else:
                u["text"] = raw_txt
                push_event("transcript", {"speaker": "caller", "text": u["text"]})
    except Exception as e:
        u["text"] = ""
        print(f"[heard] transcript failed: {e!r}", flush=True)
    print(f"[heard] {u['secs']:.1f}s -> {u['text']!r} ({time.time() - t0:.2f}s)", flush=True)


class GemmaEars(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._n = 0
        self._warm_up()

    @staticmethod
    def _warm_up() -> None:
        t0 = time.time()
        noise = (np.random.default_rng(0).standard_normal(16000 * 2) * 0.01).astype(np.float32)
        body = {"model": LLM_MODEL, "max_tokens": 8, "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Transcribe."}, _audio_part(_wav_b64(noise))]}]}
        try:
            httpx.post(f"{LLM_BASE_URL}/chat/completions", json=body, timeout=120)
            print(f"[load] Gemma audio path warmed up in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"[load] Gemma warm-up failed: {e!r}", flush=True)

    async def _recognize_impl(self, buffer, *, language=NOT_GIVEN, conn_options) -> stt.SpeechEvent:
        empty = stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=LanguageCode("hi"), text="")])
        frame = rtc.combine_audio_frames(buffer)
        pcm = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
        if frame.num_channels > 1:
            pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1)
        if frame.sample_rate != 16000:
            g = math.gcd(frame.sample_rate, 16000)
            pcm = resample_poly(pcm, 16000 // g, frame.sample_rate // g).astype(np.float32)
        pcm = pcm[-16000 * 29:]
        rms = float(np.sqrt(np.mean(pcm ** 2))) if pcm.size else 0.0
        if rms < 0.007:                      # quiet line noise: ignore immediately
            return empty
        b64, secs = _wav_b64(pcm), len(pcm) / 16000
        heard = None
        session = SESSIONS.get(id(asyncio.get_running_loop()))
        speech = session.current_speech if session is not None else None
        if speech is not None and not speech.done() and secs <= BACKCHANNEL_MAX_SECS:
            heard = await _transcribe_now(b64, timeout=1.5)
            if heard is not None and (not heard or "[silence]" in heard.lower() or is_backchannel(heard)):
                print(f"[heard] {secs:.1f}s -> {heard!r} backchannel ignored", flush=True)
                return empty
        self._n += 1
        key = f"<a{self._n}>"
        UTTER[key] = {"b64": b64, "secs": secs, "text": heard, "loop": id(asyncio.get_running_loop())}
        if heard is not None:
            push_event("transcript", {"speaker": "caller", "text": heard})
        else:
            UTTER[key]["task"] = asyncio.create_task(_gemma_transcribe(key))
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=LanguageCode("hi"), text=key)],
        )


async def _gemma_messages(chat_ctx, customer_name: str = "Buyer", caller_phone: str = "", room_name: str = "") -> tuple[list, str | None]:
    items = [i for i in chat_ctx.items if getattr(i, "type", "") == "message"]
    last_user = max((n for n, i in enumerate(items) if i.role == "user"), default=-1)
    system, turns, current = [], [], None
    for n, i in enumerate(items):
        text = (i.text_content or "").strip()
        if i.role in ("system", "developer") and text:
            system.append(text)
            continue
        if i.role not in ("user", "assistant") or not text:
            continue
        parts = [{"type": "text", "text": text}]
        if i.role == "user" and text in UTTER:
            u = UTTER[text]
            if n == last_user:
                parts = [
                    {"type": "text",
                     "text": ("The customer says (if they speak or ask for English, reply 100% in pure English with zero Hindi words; "
                              "if they speak Hindi, write 100% of your reply in Devanagari script):")},
                    _audio_part(u["b64"]),
                ]
                current = text
            elif u.get("silent"):
                continue
            else:
                if u["text"] is None and u.get("task"):
                    try:
                        await asyncio.wait_for(asyncio.shield(u["task"]), 2.0)
                    except Exception:
                        pass
                parts = [{"type": "text", "text": u["text"] or "(customer spoke)"}]
        if turns and turns[-1]["role"] == i.role:
            turns[-1]["content"] += parts
        else:
            turns.append({"role": i.role, "content": parts})

    clean_name = re.sub(r"\s+(ji|jee|जी)$", "", (customer_name or "").strip(), flags=re.I)
    name_label = clean_name if clean_name and clean_name != "Buyer" else "Sir"
    state = CALL_STATE.get(room_name, {})
    notes = [f"Customer Name: {name_label} (Phone: {caller_phone}). Do not ask for their name."]
    if state.get("b2b_requested") or state.get("b2b_sent"):
        notes.append("NOTE: You have ALREADY sent the WhatsApp B2B catalogue link during this call. Do NOT offer to send the catalogue again.")
    system.append("\n".join(notes))

    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The call has just connected.)"}]})
    return [{"role": "system", "content": "\n\n".join(system)}] + turns, current


def _finish_reply(session, full_reply, current, completed, first, n, t0,
                  caller_phone, customer_name, room_name, participant_id):
    """Runs when a reply ends, including when the caller barges in and the stream is cancelled."""
    if completed and "[silence]" in full_reply.lower() and current in UTTER:
        UTTER[current]["silent"] = True
        print(f"[llm ] Gemma stayed silent ({time.time() - t0:.2f}s)", flush=True)
        return
    if completed:
        speech = session.current_speech if session is not None else None
        if "[ACTION:SEND_B2B_LINK]" in full_reply:
            CALL_STATE.setdefault(room_name, {})["b2b_requested"] = True
            asyncio.create_task(trigger_n8n_whatsapp(caller_phone, customer_name, room_name))
        if "[ACTION:TRANSFER]" in full_reply:
            asyncio.create_task(_transfer_after_speech(speech, room_name, participant_id))
        # Hang up on [ACTION:HANGUP], on a spoken farewell, or when the caller said bye and she thanked them.
        low_reply = full_reply.lower()
        caller_last = (UTTER.get(current, {}).get("text") or "").lower() if current in UTTER else ""
        is_farewell = (
            "[action:hangup]" in low_reply
            or any(p in low_reply for p in FAREWELL_PHRASES)
            or (any(w in caller_last for w in CALLER_BYE) and any(w in low_reply for w in REPLY_THANKS))
        )
        if is_farewell:
            asyncio.create_task(_hangup_after_speech(speech, room_name))
    clean_full = re.sub(r"\[[^\]]*(\]|$)", "", full_reply).strip()
    if clean_full:
        push_event("transcript", {"speaker": "agent", "text": clean_full, "interrupted": not completed})
        print(f"[llm ] Gemma first token {first or 0:.2f}s | {n} tok in {time.time() - t0:.2f}s | {clean_full[:60]!r}"
              + ("" if completed else " (interrupted)"), flush=True)


async def gemma_reply(chat_ctx, caller_phone: str, customer_name: str, room_name: str, participant_id: str):
    """Streams reply text while buffering [...] tags so Indic-Mio never speaks [silence] or [ACTION:...]."""
    messages, current = await _gemma_messages(chat_ctx, customer_name=customer_name, caller_phone=caller_phone, room_name=room_name)
    session = SESSIONS.get(id(asyncio.get_running_loop()))
    # temperature 0.3, no repetition_penalty: penalties made Gemma invent emails and break grammar.
    body = {"model": LLM_MODEL, "messages": messages, "max_tokens": 110, "temperature": 0.3, "top_p": 0.9,
            "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}}
    t0, first, n = time.time(), None, 0
    full_reply, in_bracket, completed = "", False, False
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream("POST", f"{LLM_BASE_URL}/chat/completions", json=body) as r:
                if r.status_code != 200:
                    await r.aread()
                    print(f"[llm ] Gemma error {r.status_code}: {r.text[:300]}", flush=True)
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    j = json.loads(line[6:])
                    for ch in j.get("choices", []):
                        piece = (ch.get("delta") or {}).get("content")
                        if not piece:
                            continue
                        n += 1
                        first = first if first is not None else time.time() - t0
                        full_reply += piece
                        out_chars = []
                        for char in piece:
                            if char == "[":
                                in_bracket = True
                            elif in_bracket:
                                if char == "]":
                                    in_bracket = False
                            else:
                                out_chars.append(char)
                        if out_chars:
                            yield "".join(out_chars)
        completed = True
    finally:
        _finish_reply(session, full_reply, current, completed, first, n, t0,
                      caller_phone, customer_name, room_name, participant_id)


class MioVoice(tts.TTS):
    def __init__(self, url: str = "") -> None:
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=SR, num_channels=1)
        self._http = httpx.Client(timeout=httpx.Timeout(60.0, connect=3.0))
        self._n = 0
        self._url = (url or MIO_URL).rstrip("/")
        try:
            info = self._http.get(self._url + "/health").json()
        except Exception as e:
            raise SystemExit(f"Mio voice service not reachable at {self._url} ({e!r}). Run the Mio cell first.")
        print(f"[mio] connected: {info}", flush=True)

    def _chunks(self, text: str, stop=None):
        t0, first, parts, carry = time.time(), None, [], b""
        with self._http.stream("POST", self._url + "/tts", json={"text": text}) as r:
            r.raise_for_status()
            for data in r.iter_bytes():
                if stop is not None and stop.is_set():
                    break
                data = carry + data
                if len(data) % 2:
                    data, carry = data[:-1], data[-1:]
                else:
                    carry = b""
                if not data:
                    continue
                if first is None:
                    first = time.time() - t0
                parts.append(data)
                yield data
        if not parts:
            return
        self._n += 1
        full = np.frombuffer(b"".join(parts), dtype=np.int16)
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            wavfile.write(os.path.join(DEBUG_DIR, f"{self._n:03d}.wav"), SR, full)
        except Exception:
            pass
        print(f"[mio] n={self._n} first chunk {first:.2f}s | {len(full) / SR:.1f}s audio in {time.time() - t0:.1f}s", flush=True)

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return _VoiceStream(tts=self, input_text=text, conn_options=conn_options)


class _VoiceStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        eng, text = self._tts, self.input_text
        output_emitter.initialize(request_id=uuid.uuid4().hex, sample_rate=SR, num_channels=1, mime_type="audio/pcm")
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def work() -> None:
            try:
                for pcm in eng._chunks(text, stop):
                    loop.call_soon_threadsafe(q.put_nowait, pcm)
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(None, work)
        pushed = False
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    print(f"[mio] TTS stream failed: {item!r}", flush=True)
                    continue
                output_emitter.push(item)
                pushed = True
            if not pushed:
                print(f"[mio] no audio produced for {text[:60]!r}", flush=True)
                output_emitter.push(b"\x00\x00" * (SR // 10))
            output_emitter.flush()
        finally:
            stop.set()


class GemmaAssistant(Agent):
    def __init__(self, caller_phone: str, customer_name: str, room_name: str, participant_id: str) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)
        self.caller_phone = caller_phone
        self.customer_name = customer_name
        self.room_name = room_name
        self.participant_id = participant_id

    async def llm_node(self, chat_ctx, tools, model_settings):
        async for piece in gemma_reply(chat_ctx, self.caller_phone, self.customer_name, self.room_name, self.participant_id):
            yield piece


ENGINES: dict = {}
server = AgentServer(job_executor_type=JobExecutorType.THREAD, num_idle_processes=1, load_threshold=float("inf"))


def _on_metrics(ev) -> None:
    try:
        with open(METRICS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), **ev.metrics.model_dump()}, default=str, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def wait_until_answered(participant: rtc.RemoteParticipant, room: rtc.Room, timeout: float = 60.0) -> str:
    """Returns "answered", "not_answered" (hung up / rejected before answer) or "timeout" (nobody picked up)."""
    if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
        return "answered"
    if participant.attributes.get("sip.callStatus") == "active":
        return "answered"
    done: asyncio.Future = asyncio.get_running_loop().create_future()

    def finish(result: str) -> None:
        if not done.done():
            done.set_result(result)

    def on_attrs(changed: dict, p: rtc.Participant):
        if p.identity != participant.identity:
            return
        status = p.attributes.get("sip.callStatus")
        if status == "active":
            finish("answered")
        elif status == "hangup":
            finish("not_answered")

    def on_left(p: rtc.RemoteParticipant):
        if p.identity == participant.identity:
            finish("not_answered")

    room.on("participant_attributes_changed", on_attrs)
    room.on("participant_disconnected", on_left)
    try:
        if participant.attributes.get("sip.callStatus") == "active":
            finish("answered")
        return await asyncio.wait_for(done, timeout)
    except asyncio.TimeoutError:
        return "timeout"
    finally:
        room.off("participant_attributes_changed", on_attrs)
        room.off("participant_disconnected", on_left)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    # Dispatch metadata (set by dialer_service) arrives on ctx.job.metadata; room metadata is a fallback.
    metadata = {}
    for raw in (getattr(ctx.job, "metadata", "") or "", ctx.room.metadata or ""):
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and data:
            metadata = data
            break

    crm_person_id = metadata.get("twenty_person_id", "")
    lead_name = metadata.get("customer_name", "Buyer")
    job_id = metadata.get("job_id", "")
    callback_url = metadata.get("callback_url", "")
    dialed = metadata.get("phone", "")
    phone_e164 = metadata.get("phone_e164", "")

    session = AgentSession(
        stt=ENGINES["stt"],
        vad=silero.VAD.load(min_silence_duration=0.5, min_speech_duration=0.25, activation_threshold=0.72),
        llm=openai.LLM(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key="local"),
        tts=ENGINES["tts"],
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {"min_delay": 0.35},
            "interruption": {
                "mode": "vad",
                "min_duration": INTERRUPT_MIN_SECS,
                "resume_false_interruption": True,
                "false_interruption_timeout": RESUME_AFTER_SECS,
            },
            "preemptive_generation": {"enabled": False},
        },
    )
    loop_id = id(asyncio.get_running_loop())
    SESSIONS[loop_id] = session
    CALL_STATE[ctx.room.name] = {"b2b_requested": False, "b2b_sent": False, "transferred": False}
    session.on("metrics_collected", _on_metrics)
    session.on("close", lambda *_: ctx.shutdown(reason="session closed"))

    st = {"answered": False, "start": None, "posted": False, "reason": "", "sip_status": "",
          "phone": phone_e164 or to_e164(dialed) or dialed}

    async def _on_call_finished():
        """Runs once per call (answered or not) and reports the outcome back to n8n."""
        if st["posted"]:
            return
        st["posted"] = True
        SESSIONS.pop(loop_id, None)
        pending = [u["task"] for u in UTTER.values()
                   if u.get("loop") == loop_id and u.get("task") and not u["task"].done()]
        if pending:
            await asyncio.wait(pending, timeout=3.0)

        history = []
        if st["answered"]:
            chat_items = getattr(getattr(session, "history", None), "items", None) or getattr(assistant.chat_ctx, "items", [])
            for it in chat_items:
                if getattr(it, "type", "") == "message" and it.role in ("user", "assistant"):
                    txt = (it.text_content or "").strip()
                    if it.role == "user" and txt in UTTER:
                        if UTTER[txt].get("silent"):
                            continue
                        txt = UTTER[txt].get("text") or "(unclear audio)"
                    else:
                        txt = re.sub(r"\[[^\]]*(\]|$)", "", txt).strip()
                    if txt:
                        history.append(f"{it.role}: {txt}")
        full_transcript = "\n".join(history)

        for k in [k for k, u in UTTER.items() if u.get("loop") == loop_id]:   # free the per-call audio
            UTTER.pop(k, None)
        state = CALL_STATE.pop(ctx.room.name, {})

        if not st["answered"]:
            outcome = st["reason"] or "not_answered"
        elif state.get("transferred"):
            outcome = "transferred"
        elif state.get("b2b_sent") or state.get("b2b_requested"):
            outcome = "link_sent"
        else:
            outcome = "completed"
        call_summary = {
            "job_id": job_id,
            "twenty_person_id": crm_person_id,
            "phone": st["phone"],
            "customer_name": lead_name,
            "room_name": ctx.room.name,
            "status": "completed" if st["answered"] else "not_answered",
            "outcome": outcome,
            "sip_status": st["sip_status"],
            "duration": round(time.time() - st["start"], 1) if st["start"] else 0.0,
            "transcript": full_transcript,
            "b2b_link_sent": bool(state.get("b2b_sent") or state.get("b2b_requested")),
            "transferred_to_human": state.get("transferred", False),
            "ended_at": time.time(),
        }
        push_event("call_ended" if st["answered"] else "call_not_answered", call_summary)
        await post_call_outcome(call_summary, callback_url)

    ctx.add_shutdown_callback(_on_call_finished)

    assistant = GemmaAssistant(caller_phone=st["phone"], customer_name=lead_name, room_name=ctx.room.name, participant_id="")
    await session.start(room=ctx.room, agent=assistant)

    try:
        caller = await asyncio.wait_for(ctx.wait_for_participant(), timeout=90)
    except asyncio.TimeoutError:
        st["reason"] = "no_participant"
        ctx.shutdown(reason="no participant joined")
        return

    # Fallback: room / SIP participant metadata if the dispatch metadata was empty.
    if not metadata or lead_name == "Buyer":
        for raw in (ctx.room.metadata or "", getattr(caller, "metadata", "") or ""):
            try:
                d = json.loads(raw) if raw else {}
                if isinstance(d, dict) and d:
                    metadata.update({k: v for k, v in d.items() if v})
            except Exception:
                pass
        lead_name = metadata.get("customer_name") or getattr(caller, "name", "") or lead_name
        crm_person_id = metadata.get("twenty_person_id") or crm_person_id
        job_id = metadata.get("job_id") or job_id
        callback_url = metadata.get("callback_url") or callback_url
        dialed = metadata.get("phone") or dialed
        phone_e164 = metadata.get("phone_e164") or phone_e164

    phone = caller.attributes.get("sip.phoneNumber") or caller.identity.replace("sip-", "")
    st["phone"] = phone_e164 or to_e164(dialed or phone) or dialed or phone
    assistant.caller_phone = st["phone"]
    assistant.customer_name = lead_name
    assistant.participant_id = caller.identity
    print(f"[meta] caller_name={lead_name!r} phone={st['phone']!r} job_id={job_id!r}", flush=True)

    result = await wait_until_answered(caller, ctx.room, timeout=60)
    st["sip_status"] = caller.attributes.get("sip.callStatus", "")
    if result != "answered":
        st["reason"] = "no_answer" if result == "timeout" else "not_answered"
        print(f"[call] not answered: {st['phone']} ({st['reason']}, sip status {st['sip_status']!r})", flush=True)
        ctx.shutdown(reason="call not answered")
        return

    st["answered"] = True
    st["start"] = time.time()
    push_event("call_started", {"room": ctx.room.name, "caller": st["phone"], "customer_name": lead_name, "crm_id": crm_person_id})
    print(f"[call] connected: {st['phone']} ({lead_name}) in room {ctx.room.name}", flush=True)

    await asyncio.sleep(0.5)
    clean_lead = re.sub(r"\s+(ji|jee|जी)$", "", (lead_name or "").strip(), flags=re.I)
    greet_name = f"{clean_lead} जी" if clean_lead and clean_lead != "Buyer" else "जी"
    if phone == "8000" or "8000" in caller.identity:
        await session.generate_reply(
            instructions="Greet politely in one short sentence in Hindi (Devanagari), say you are the M.A. Creations AI assistant on extension 8000, and ask how you can help."
        )
    else:
        await session.generate_reply(
            instructions=(
                f"Greet {greet_name} in pure Devanagari script: "
                f"'नमस्ते {greet_name}, मैं नैना बात कर रही हूँ एम ए क्रिएशन्स, नोएडा से। "
                f"हम वुडन हैंडीक्राफ्ट्स और बोर्ड गेम्स होलसेल में बनाते हैं। क्या मैं आपको हमारा कैटलॉग व्हाट्सऐप पर भेज दूँ?'"
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"[load] Gemma hears and answers: {LLM_MODEL}", flush=True)
    ENGINES["stt"] = GemmaEars()
    print("[load] Indic-Mio voice (local service) ...", flush=True)
    ENGINES["tts"] = MioVoice()
    print(f"[ready] worker '{AGENT_NAME}' waiting for calls in LiveKit ...", flush=True)
    agents.cli.run_app(server)
