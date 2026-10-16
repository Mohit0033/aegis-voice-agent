import glob
import os
import re
import sys
import threading
import time
import traceback

import numpy as np

SR = 24000
REF_WAV = os.environ.get("MIO_REF_WAV", "")
LLM_URL = os.environ.get("MIO_LLM_URL", "http://127.0.0.1:8002/v1").rstrip("/")
CODEC_ID = os.environ.get("MIO_CODEC", "Aratako/MioCodec-25Hz-24kHz")
TEMPERATURE = float(os.environ.get("MIO_TEMPERATURE", "0.8"))
MAX_TOKENS = int(os.environ.get("MIO_MAX_TOKENS", "700"))
SPLIT = os.environ.get("MIO_SPLIT", "1") == "1"
SPEED = float(os.environ.get("MIO_SPEED", "0.9"))
PAUSE = float(os.environ.get("MIO_PAUSE", "0.18"))
PORT = int(os.environ.get("MIO_PORT", "8003"))
TAGS = {"hi": {"happy", "sad", "angry", "disgust", "fear", "surprise"},
        "en": {"happy", "sad", "enunciated", "confused", "angry", "whisper"}}
STYLE = {"hi": os.environ.get("MIO_STYLE_HI", "happy").strip().strip("<>").lower(),
         "en": os.environ.get("MIO_STYLE_EN", "happy").strip().strip("<>").lower()}
for _lang, _tag in STYLE.items():
    if _tag and _tag not in TAGS[_lang]:
        sys.exit(f"MIO_STYLE_{_lang.upper()}={_tag!r} is not valid.")
TAG_RE = re.compile(r"<\s*/?\s*([A-Za-z_]+)\s*>")
FAKE = os.environ.get("MIO_FAKE") == "1"
TOKEN_RE = re.compile(r"<\|s_(\d+)\|>")
MIO_DEVICE = os.environ.get("MIO_DEVICE", "auto")
MIO_THREADS = int(os.environ.get("MIO_THREADS", "0"))

_HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
    "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस "
    "तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस "
    "चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास "
    "पचास इक्यावन बावन तिरेपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ "
    "साठ इकसठ बासठ तिरेसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर "
    "सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी "
    "अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी "
    "नब्बे इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()

def hindi_number(n: int) -> str:
    if n < 100:
        return _HI_0_99[n]
    if n >= 10**9:
        return " ".join(_HI_0_99[int(d)] for d in str(n))
    parts = []
    for size, name in ((10**7, "करोड़"), (10**5, "लाख"), (1000, "हज़ार"), (100, "सौ")):
        q, n = divmod(n, size)
        if q:
            parts.append(f"{hindi_number(q)} {name}")
    if n:
        parts.append(_HI_0_99[n])
    return " ".join(parts)

_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_HINGLISH_PHONETIC = [
    (r"\bb2b\s*\.\s*macreations\s*\.\s*in\b", "बी टू बी डॉट एम ए क्रिएशन्स डॉट इन"),
    (r"\bb2b\s+dot\s+ma\s+creations\s+dot\s+in\b", "बी टू बी डॉट एम ए क्रिएशन्स डॉट इन"),
    (r"\bm\.?\s*a\.?\s+creations\b", "एम ए क्रिएशन्स"),
    (r"\bb2b\b", "बी टू बी"),
    (r"\bwhatsapp\b", "व्हाट्सऐप"),
    (r"\bcatalogue\b", "कैटलॉग"),
    (r"\bcatalog\b", "कैटलॉग"),
    (r"\bwooden\s+handicrafts\b", "वुडन हैंडीक्राफ्ट्स"),
    (r"\bhandicrafts\b", "हैंडीक्राफ्ट्स"),
    (r"\bwooden\s+toys\b", "वुडन टॉयज़"),
    (r"\btoys\b", "टॉयज़"),
    (r"\bboard\s+games\b", "बोर्ड गेम्स"),
    (r"\bpuzzles\b", "पज़ल्स"),
    (r"\bpuzzle\b", "पज़ल"),
    (r"\bmusical\s+instruments\b", "म्यूज़िकल इंस्ट्रूमेंट्स"),
    (r"\bhome\s+decor\b", "होम डेकोर"),
    (r"\bdecor\b", "डेकोर"),
    (r"\bwholesale\b", "होलसेल"),
    (r"\bmanufacturer\b", "मैन्युफैक्चरर"),
    (r"\bmanufacturing\s+unit\b", "मैन्युफैक्चरिंग यूनिट"),
    (r"\blatest\b", "लेटेस्ट"),
    (r"\bportal\b", "पोर्टल"),
    (r"\blink\b", "लिंक"),
    (r"\bsamples\b", "सैंपल्स"),
    (r"\bsample\b", "सैंपल"),
    (r"\bpieces\b", "पीसेज़"),
    (r"\bpiece\b", "पीस"),
    (r"\border\b", "ऑर्डर"),
    (r"\bdelivery\b", "डिलीवरी"),
    (r"\bfactory\s+rate\b", "फैक्ट्री रेट"),
    (r"\bfactory\b", "फैक्ट्री"),
    (r"\brate\b", "रेट"),
    (r"\bvariety\b", "वैरायटी"),
    (r"\brequirement\b", "रिक्वायरमेंट"),
    (r"\bshop\b", "शॉप"),
    (r"\bbusiness\b", "बिज़नेस"),
    (r"\bdeal\b", "डील"),
    (r"\bnoida\b", "नोएडा"),
    (r"\bsector\b", "सेक्टर"),
    (r"\bgst\b", "जी एस टी"),
]

def _smooth_for_indic_tts(text: str) -> str:
    """Make English loanwords, acronyms and the portal URL speakable by Indic-Mio when the sentence is Hindi."""
    text = re.sub(r"\s*[—–]+\s*|\s+-\s+", ", ", text)   # dashes break clause splitting
    if not any("ऀ" <= c <= "ॿ" for c in text):
        return text
    for pat, rep in _HINGLISH_PHONETIC:
        text = re.sub(pat, rep, text, flags=re.I)
    return text

def prepare(text: str) -> str:
    text = _smooth_for_indic_tts(text)
    text = text.translate(_DEV_DIGITS).strip()
    if not any("ऀ" <= c <= "ॿ" for c in text):
        return text
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    def rep(m):
        whole, _, frac = m.group(0).partition(".")
        out = hindi_number(int(whole))
        if frac:
            out += " दशमलव " + " ".join(_HI_0_99[int(d)] for d in frac)
        return f" {out} "
    return re.sub(r"\s+", " ", re.sub(r"\d+(?:\.\d+)?", rep, text)).strip()

def split_clauses(text: str, min_chars: int = 18) -> list:
    out = []
    for part in re.split(r"(?<=[,;:।?!.])\s+", text.strip()):
        if not part:
            continue
        if out and len(out[-1]) < min_chars:
            out[-1] += " " + part
        else:
            out.append(part)
    if len(out) > 1 and len(out[-1]) < min_chars:
        last = out.pop()
        out[-1] += " " + last
    return out

def is_hindi(text: str) -> bool:
    return any("\u0900" <= c <= "\u097f" for c in text)

def take_tags(text: str):
    known = TAGS["hi"] | TAGS["en"]
    found = [t.lower() for t in TAG_RE.findall(text) if t.lower() in known]
    text = re.sub(r"\*{2,}", "*", TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip(), (found[0] if found else "")

def with_style(clause: str, asked: str = "") -> str:
    lang = "hi" if is_hindi(clause) else "en"
    tag = asked if asked in TAGS[lang] else STYLE[lang]
    return f"{clause} <{tag}>" if tag else clause

LOCK = threading.Lock()
STATE = {}

def find_reference() -> str:
    if REF_WAV and os.path.exists(REF_WAV):
        return REF_WAV
    cached = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--tryorato--orato-tts-hindi-v1/snapshots/*/voices/female.wav"))
    if cached:
        return cached[0]
    if os.environ.get("HF_TOKEN"):
        try:
            from huggingface_hub import snapshot_download
            p = snapshot_download("tryorato/orato-tts-hindi-v1", allow_patterns=["voices/*"])
            if os.path.exists(p + "/voices/female.wav"):
                return p + "/voices/female.wav"
        except Exception:
            pass
    from huggingface_hub import snapshot_download
    p = snapshot_download("SPRINGLab/Indic-Mio", allow_patterns=["samples/*"])
    clips = sorted(glob.glob(p + "/samples/*.wav") + glob.glob(p + "/samples/*.flac") + glob.glob(p + "/samples/*.mp3"))
    if clips:
        return clips[0]
    sys.exit("No reference voice found.")

def load_models() -> None:
    import httpx, soundfile as sf, torch
    from miocodec import MioCodecModel, load_audio
    http = httpx.Client(timeout=60)
    model = http.get(LLM_URL + "/models").json()["data"][0]["id"]
    ref = find_reference()
    dev = ("cuda" if torch.cuda.is_available() else "cpu") if MIO_DEVICE == "auto" else MIO_DEVICE
    codec = MioCodecModel.from_pretrained(CODEC_ID).eval().to(dev)
    sr = int(codec.config.sample_rate)
    with torch.inference_mode():
        emb = codec.encode(load_audio(ref, sample_rate=sr).to(dev), return_content=False, return_global=True).global_embedding
    STATE.update(http=http, model=model, codec=codec, emb=emb, torch=torch, dev=dev)
    t = time.time()
    synth(with_style("नमस्ते, मैं आपकी क्या मदद कर सकती हूँ?"))
    print(f"[mio] ready | style hi={STYLE['hi'] or 'none'} en={STYLE['en'] or 'none'} | speed {SPEED} | pause {PAUSE}s | voice {os.path.basename(ref)} ({sf.info(ref).duration:.1f}s) | warm-up {time.time() - t:.2f}s", flush=True)

def synth(clause: str) -> np.ndarray:
    torch = STATE["torch"]
    r = STATE["http"].post(LLM_URL + "/chat/completions", json={
        "model": STATE["model"], "messages": [{"role": "user", "content": clause}],
        "temperature": TEMPERATURE, "top_p": 1.0, "max_tokens": MAX_TOKENS, "skip_special_tokens": False,
    })
    r.raise_for_status()
    toks = [int(x) for x in TOKEN_RE.findall(r.json()["choices"][0]["message"]["content"] or "")]
    if not toks:
        return np.zeros(0, dtype=np.int16)
    target = int(len(toks) * SR / 25 / max(0.5, min(SPEED, 1.5))) if abs(SPEED - 1.0) > 1e-3 else None
    with torch.inference_mode():
        audio = STATE["codec"].decode(global_embedding=STATE["emb"],
                                      content_token_indices=torch.tensor(toks, dtype=torch.long, device=STATE["dev"]),
                                      target_audio_length=target)
    wav = np.nan_to_num(audio.squeeze().float().cpu().numpy())
    return (np.clip(wav, -1.0, 1.0) * 32767).astype(np.int16)

def stream(text: str):
    t0, first, total = time.time(), None, 0
    gap = np.zeros(int(PAUSE * SR), dtype=np.int16).tobytes()
    text, asked = take_tags(text)
    text = prepare(text)
    clauses = [with_style(c, asked) for c in (split_clauses(text) if SPLIT else [text]) if c.strip()]
    for clause in clauses:
        with LOCK:
            try:
                pcm = synth(clause)
            except Exception:
                print(f"[mio] synth failed for {clause!r}", flush=True)
                traceback.print_exc()
                pcm = np.zeros(0, dtype=np.int16)
        if not pcm.size:
            continue
        if first is None:
            first = time.time() - t0
        else:
            yield gap
        total += len(pcm)
        yield pcm.tobytes()

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

class Req(BaseModel):
    text: str

@app.get("/health")
def health():
    return {"ok": True, "engine": "indic-mio", "codec": CODEC_ID, "split": SPLIT, "speed": SPEED, "pause": PAUSE, "style": STYLE, "sample_rate": SR}

class Cfg(BaseModel):
    speed: float | None = None
    pause: float | None = None
    style_hi: str | None = None
    style_en: str | None = None

@app.post("/config")
def config(c: Cfg):
    global SPEED, PAUSE
    if c.speed is not None: SPEED = max(0.6, min(float(c.speed), 1.4))
    if c.pause is not None: PAUSE = max(0.0, min(float(c.pause), 1.0))
    for lang, v in (("hi", c.style_hi), ("en", c.style_en)):
        if v is not None:
            v = v.strip().strip("<>").lower()
            if v and v not in TAGS[lang]:
                return JSONResponse({"error": f"Invalid tag {v!r}"}, status_code=400)
            STYLE[lang] = v
    return health()

@app.post("/tts")
def tts(req: Req):
    if not req.text.strip():
        return JSONResponse({"error": "empty text"}, status_code=400)
    return StreamingResponse(stream(req.text), media_type="application/octet-stream", headers={"X-Sample-Rate": str(SR)})

if __name__ == "__main__":
    import uvicorn
    load_models()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
