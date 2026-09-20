"""
Vurenn AI - Web Frontend (Flask)
A local web page with a microphone button replaces the terminal
'press Enter to record' flow. Click the mic button, speak, click again
to send — the response is shown as text and played back as audio.

Requirements:
    pip install flask edge-tts pygame SpeechRecognition sympy requests anthropic spotipy
    pip install opencv-python face_recognition numpy
    ffmpeg must be installed and on your system PATH (used to convert
    browser-recorded webm audio to wav for speech recognition).

Run:
    python server.py
Then open:
    http://localhost:5000
"""

import time
import re
import os
import ast
import math
import operator
import uuid
import hashlib
import asyncio
import subprocess
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, Response
from anthropic import Anthropic

try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    print("stripe not found — subscriptions disabled. Run: pip install stripe")

try:
    import edge_tts
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    print("edge-tts not found — voice output disabled. Run: pip install edge-tts")

try:
    import speech_recognition as sr
    STT_AVAILABLE = True
except ImportError:
    STT_AVAILABLE = False
    print("SpeechRecognition not found — voice input disabled.")

try:
    import sympy as sp
    SYMPY_AVAILABLE = True
except ImportError:
    SYMPY_AVAILABLE = False
    print("sympy not found — advanced math disabled.")

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    SPOTIFY_AVAILABLE = True
except ImportError:
    SPOTIFY_AVAILABLE = False
    print("spotipy not found — Spotify disabled.")

import webbrowser

try:
    import cv2
    import face_recognition
    import numpy as np
    import pickle
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False
    print("face_recognition/cv2 not found — facial recognition disabled.")

# ─── Configuration ────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = "http://localhost:8888/callback"
SPOTIFY_SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing playlist-read-private streaming"
)

FACE_DATA_PATH       = "delta_known_faces.pkl"
FACE_MATCH_TOLERANCE  = 0.5
CAMERA_INDEX          = 0
FACE_SEARCH_INTERVAL  = 1.0
FACE_SEARCH_TIMEOUT   = 30

MFA_ENABLED = True
AUTH_SESSION_MINUTES = 30
PASSPHRASE_HASH_PATH = "delta_passphrase.hash"
authenticated_until = 0.0

# ── Stripe subscription config ────────────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")  # pk_... is safe for frontend
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")         # price_... (NOT prod_...)
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")   # whsec_...
YOUR_DOMAIN             = os.environ.get("YOUR_DOMAIN", "http://localhost:5000")
SUBSCRIPTION_STATUS_PATH = "subscription_status.json"
REQUIRE_SUBSCRIPTION = True   # gate voice commands behind an active subscription

def load_subscription_status():
    import json
    defaults = {"active": False, "customer_id": None, "subscription_id": None}
    if os.path.exists(SUBSCRIPTION_STATUS_PATH):
        try:
            with open(SUBSCRIPTION_STATUS_PATH) as f:
                defaults.update(json.load(f))
        except Exception as e:
            print(f"[Vurenn] Couldn't load subscription status: {e}")
    return defaults

def save_subscription_status(data):
    import json
    with open(SUBSCRIPTION_STATUS_PATH, "w") as f:
        json.dump(data, f)

subscription_status = load_subscription_status()

def is_subscribed() -> bool:
    return subscription_status.get("active", False)

def get_or_create_customer():
    """Single-user app: reuse the same Stripe Customer across sessions."""
    stripe.api_key = STRIPE_SECRET_KEY
    customer_id = subscription_status.get("customer_id")
    if customer_id:
        try:
            return stripe.Customer.retrieve(customer_id)
        except Exception:
            pass  # fall through and create a new one if retrieval fails
    customer = stripe.Customer.create()
    subscription_status["customer_id"] = customer.id
    save_subscription_status(subscription_status)
    return customer

VOICE_CONFIG_PATH = "voice_config.json"

AVAILABLE_VOICES = {
    "en-GB-RyanNeural":   "Ryan (British, male)",
    "en-GB-SoniaNeural":  "Sonia (British, female)",
    "en-US-GuyNeural":    "Guy (American, male)",
    "en-US-AriaNeural":   "Aria (American, female)",
    "en-US-JennyNeural":  "Jenny (American, female, warm)",
    "en-AU-WilliamNeural":"William (Australian, male)",
    "en-IE-ConnorNeural": "Connor (Irish, male)",
}

def load_voice_settings():
    """Loads saved voice/rate/pitch from disk, or falls back to defaults."""
    defaults = {"voice": "en-GB-RyanNeural", "rate": "-5%", "pitch": "-8Hz"}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            import json
            with open(VOICE_CONFIG_PATH) as f:
                data = json.load(f)
            defaults.update(data)
        except Exception as e:
            print(f"[Vurenn] Couldn't load voice config, using defaults: {e}")
    return defaults

def save_voice_settings(voice, rate, pitch):
    import json
    with open(VOICE_CONFIG_PATH, "w") as f:
        json.dump({"voice": voice, "rate": rate, "pitch": pitch}, f)

_voice_settings = load_voice_settings()
VOICE = _voice_settings["voice"]
VOICE_RATE = _voice_settings["rate"]
VOICE_PITCH = _voice_settings["pitch"]

AUDIO_DIR = "generated_audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

AUDIO_MAX_AGE_SECONDS = 5 * 60      # delete generated audio older than this
AUDIO_CLEANUP_INTERVAL = 2 * 60     # how often the background sweep runs

def cleanup_old_audio(max_age_seconds=AUDIO_MAX_AGE_SECONDS):
    """Deletes files in AUDIO_DIR older than max_age_seconds."""
    now = time.time()
    removed = 0
    try:
        for fname in os.listdir(AUDIO_DIR):
            fpath = os.path.join(AUDIO_DIR, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > max_age_seconds:
                    os.remove(fpath)
                    removed += 1
            except OSError:
                pass
    except FileNotFoundError:
        pass
    if removed:
        print(f"[Vurenn] Cleaned up {removed} old audio file(s).")

def start_audio_cleanup_loop():
    """Runs cleanup_old_audio on a repeating background timer."""
    def _loop():
        cleanup_old_audio()
        timer = threading.Timer(AUDIO_CLEANUP_INTERVAL, _loop)
        timer.daemon = True
        timer.start()
    _loop()

PRONUNCIATION_OVERRIDES = {
    "obi-wan": "OH-bee wahn", "obi wan": "OH-bee wahn",
    "anakin": "AN-uh-kin", "padmé": "PAD-may", "padme": "PAD-may",
    "ahsoka": "ah-SOH-kah", "chewbacca": "choo-BAH-kah",
    "coruscant": "KOR-uh-sant", "tatooine": "tat-oo-EEN",
    "naboo": "nah-BOO", "kylo ren": "KY-loh wren",
    "darth vader": "darth VAY-der", "mandalorian": "man-duh-LOR-ee-an",
    "wookiee": "WOOK-ee", "r2-d2": "are two dee two",
    "c-3po": "see three pee oh", "yoda": "YOH-duh", "palpatine": "PAL-puh-teen",
}

def apply_pronunciation_overrides(text: str) -> str:
    result = text
    for term, replacement in PRONUNCIATION_OVERRIDES.items():
        pattern = r"\b" + re.escape(term) + r"\b"
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result

# ─── Global State ─────────────────────────────────────────────────────────────

conversation_history = []
known_face_encodings = []
known_face_names     = []
current_recognized_name = None

# ─── Claude API Client ────────────────────────────────────────────────────────

claude = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

SYSTEM_PROMPT = """You are Vurenn, a friendly and intelligent voice assistant.
Give thorough, well-reasoned answers — don't artificially cut things short.
For simple factual questions, a sentence or two is fine. For anything that
benefits from explanation, context, or multiple points, take the space you need.
You speak clearly and avoid markdown formatting since your output is read aloud.
You are helpful, honest, and warm.

When the user asks for advice, guidance, or how to handle a life situation
(relationships, decisions, struggles, moral questions, etc.), offer counsel
grounded in a Christian worldview — drawing on biblical principles and, where
relevant, referencing specific scripture (name the book/chapter/verse when you
quote or paraphrase one). Keep it warm and practical, not preachy. If the
user's question is purely factual or technical, just answer it directly."""

def ask_claude(user_message: str) -> str:
    if not claude:
        return "Claude API key not configured. Please set the ANTHROPIC_API_KEY environment variable."

    conversation_history.append({"role": "user", "content": user_message})
    system_prompt = SYSTEM_PROMPT
    if current_recognized_name:
        system_prompt += f"\nThe person talking to you is {current_recognized_name}. You may address them by name occasionally."

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=conversation_history,
        )
        reply = response.content[0].text.strip()
        conversation_history.append({"role": "assistant", "content": reply})
        if len(conversation_history) > 20:
            conversation_history.pop(0)
            conversation_history.pop(0)
        return reply
    except Exception as e:
        return f"I had trouble connecting to my brain. Error: {e}"

# ─── Spotify ──────────────────────────────────────────────────────────────────

sp_client = None

def init_spotify():
    global sp_client
    if not SPOTIFY_AVAILABLE or not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return
    try:
        sp_client = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI, scope=SPOTIFY_SCOPES, open_browser=True,
        ))
        sp_client.current_user()
        print("[Vurenn] Spotify connected.")
    except Exception as e:
        print(f"[Vurenn] Spotify auth failed: {e}")
        sp_client = None

def _active_device():
    try:
        devices = sp_client.devices().get("devices", [])
        active = [d for d in devices if d["is_active"]]
        if active:
            return active[0]["id"]
        if devices:
            return devices[0]["id"]
    except Exception:
        pass
    return None

def handle_spotify(command: str):
    if not sp_client:
        return None
    cmd = command.lower().strip()

    if re.search(r"what(?:'s| is)(?: currently)? playing|now playing", cmd):
        try:
            current = sp_client.current_playback()
            if current and current.get("item"):
                t = current["item"]; a = t["artists"][0]["name"]
                state = "playing" if current["is_playing"] else "paused"
                return f"Currently {state}: {t['name']} by {a}."
            return "Nothing is playing right now."
        except Exception as e:
            return f"Couldn't check playback. Error: {e}"

    if re.search(r"\bpause\b|stop music|stop playing", cmd):
        try:
            sp_client.pause_playback(); return "Music paused."
        except Exception as e:
            return f"Couldn't pause. Error: {e}"

    if re.search(r"\bresume\b|continue playing|unpause", cmd):
        try:
            sp_client.start_playback(device_id=_active_device()); return "Resuming music."
        except Exception as e:
            return f"Couldn't resume. Error: {e}"

    if re.search(r"\bskip\b|next song|next track", cmd):
        try:
            sp_client.next_track(); time.sleep(0.5)
            current = sp_client.current_playback()
            if current and current.get("item"):
                t = current["item"]
                return f"Skipped! Now playing {t['name']} by {t['artists'][0]['name']}."
            return "Skipped to next track."
        except Exception as e:
            return f"Couldn't skip. Error: {e}"

    if re.search(r"previous song|go back|previous track", cmd):
        try:
            sp_client.previous_track(); time.sleep(0.5)
            current = sp_client.current_playback()
            if current and current.get("item"):
                t = current["item"]
                return f"Going back to {t['name']} by {t['artists'][0]['name']}."
            return "Went back to previous track."
        except Exception as e:
            return f"Couldn't go back. Error: {e}"

    vol_match = re.search(r"(?:set|change|turn)?\s*volume\s*(?:to|at)?\s*(\d{1,3})", cmd)
    if vol_match:
        try:
            vol = max(0, min(100, int(vol_match.group(1))))
            sp_client.volume(vol); return f"Volume set to {vol} percent."
        except Exception as e:
            return f"Couldn't set volume. Error: {e}"

    play_match = re.search(r"play\s+(.+?)(?:\s+(?:by|from|on spotify))?$", cmd)
    if play_match or cmd.startswith("play"):
        query = play_match.group(1).strip() if play_match else cmd.replace("play", "").strip()
        if not query:
            try:
                sp_client.start_playback(device_id=_active_device()); return "Resuming playback."
            except Exception as e:
                return f"Couldn't start playback. Error: {e}"
        try:
            results = sp_client.search(q=query, limit=1, type="track,artist,playlist")
            tracks = results.get("tracks", {}).get("items", [])
            if tracks:
                t = tracks[0]
                sp_client.start_playback(device_id=_active_device(), uris=[t["uri"]])
                return f"Playing {t['name']} by {t['artists'][0]['name']}."
            artists = results.get("artists", {}).get("items", [])
            if artists:
                a = artists[0]
                sp_client.start_playback(device_id=_active_device(), context_uri=a["uri"])
                return f"Playing music by {a['name']}."
            playlists = results.get("playlists", {}).get("items", [])
            if playlists:
                pl = playlists[0]
                sp_client.start_playback(device_id=_active_device(), context_uri=pl["uri"])
                return f"Playing playlist: {pl['name']}."
            return f"I couldn't find anything for '{query}' on Spotify."
        except Exception as e:
            return f"Spotify error: {e}"

    return None

# ─── Math Handlers ────────────────────────────────────────────────────────────

def clean_math_query(command: str) -> str:
    prefixes = ["what is", "what's", "whats", "calculate", "evaluate", "solve", "find", "work out", "compute", "tell me"]
    command = command.lower().strip()
    for prefix in prefixes:
        if command.startswith(prefix):
            command = command[len(prefix):].strip()
            break
    return command

def convert_natural_language_exponents(command: str) -> str:
    patterns = [
        (r"(\b\d+\.?\d*\b)\s+squared\b", r"\1**2"),
        (r"(\b\d+\.?\d*\b)\s+cubed\b", r"\1**3"),
        (r"(\b\d+\.?\d*\b)\s+to the power of\s+(\b\d+\.?\d*\b)", r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+to the power\s+(\b\d+\.?\d*\b)", r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+to the\s+(\b\d+\.?\d*\b)", r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s*\^\s*(\b\d+\.?\d*\b)", r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+raised to\s+(\b\d+\.?\d*\b)", r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+raised to the power of\s+(\b\d+\.?\d*\b)", r"\1**\2"),
    ]
    for pattern, replacement in patterns:
        command = re.sub(pattern, replacement, command, flags=re.IGNORECASE)
    return command

def is_math_problem(command: str):
    cleaned = clean_math_query(command)
    cleaned = convert_natural_language_exponents(cleaned)
    pattern = r"^[\d\+\-\*/\^\(\)\.\s=]+$"
    if re.match(pattern, cleaned) and any(c.isdigit() for c in cleaned):
        return cleaned.strip()
    return None

def safe_numeric_expression(expression: str):
    if len(expression) > 200:
        raise ValueError("Expression is too long")
    binary_operators = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
    }
    unary_operators = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in binary_operators:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent is too large")
            return binary_operators[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in unary_operators:
            return unary_operators[type(node.op)](evaluate(node.operand))
        raise ValueError("Expression contains unsupported syntax")

    result = evaluate(ast.parse(expression, mode="eval").body)
    if not isinstance(result, (int, float)) or not math.isfinite(result):
        raise ValueError("Expression did not produce a finite number")
    return result


def solve_math_problem(command: str) -> str:
    try:
        expr = clean_math_query(command)
        expr = convert_natural_language_exponents(expr).replace("^", "**")
        expr = re.sub(r"\s+", "", expr)
        if SYMPY_AVAILABLE:
            result = sp.sympify(expr).evalf()
            return str(int(result)) if result.is_integer else f"{float(result):.6f}".rstrip("0").rstrip(".")
        return str(safe_numeric_expression(expr))
    except Exception as e:
        return f"I couldn't solve that. Error: {e}"

def is_square_root_problem(command: str):
    command = clean_math_query(command)
    for pattern in [r"square\s+root\s+of\s+(\d+\.?\d*)", r"sqrt\s*\(\s*(\d+\.?\d*)\s*\)", r"√\s*(\d+\.?\d*)"]:
        m = re.search(pattern, command, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def solve_square_root(number_str: str) -> str:
    try:
        import math
        number = float(number_str)
        if number < 0:
            return f"The square root of {number} is not a real number."
        result = math.sqrt(number)
        return str(int(result)) if result == int(result) else f"{result:.6f}".rstrip("0").rstrip(".")
    except Exception as e:
        return f"Couldn't calculate square root. Error: {e}"

def is_algebraic_equation(command: str):
    cleaned = clean_math_query(command)
    if "=" in cleaned and re.search(r"[a-zA-Z]", cleaned):
        variable = None
        for pattern in [r"solve\s+for\s+([a-zA-Z])", r"find\s+([a-zA-Z])", r"what\s+is\s+([a-zA-Z])"]:
            m = re.search(pattern, cleaned, re.IGNORECASE)
            if m:
                variable = m.group(1); break
        if not variable:
            vars_found = re.findall(r"[a-zA-Z]", cleaned)
            variable = vars_found[0] if vars_found else None
        equation = re.sub(r"solve\s+for\s+[a-zA-Z]|find\s+[a-zA-Z]|what\s+is\s+[a-zA-Z]", "", cleaned, flags=re.IGNORECASE).strip()
        return equation, variable
    return None, None

def solve_algebraic_equation(equation_str: str, variable: str) -> str:
    if not SYMPY_AVAILABLE:
        return "SymPy is required for algebra. Please install it."
    try:
        equation_str = equation_str.replace("^", "**").strip()
        if "=" in equation_str:
            left, right = equation_str.split("=", 1)
            equation_str = f"{left}-({right})"
        x = sp.Symbol(variable)
        solutions = sp.solve(sp.sympify(equation_str), x)
        if not solutions:
            return f"No solutions found for {variable}."
        return f"{variable} = " + (" or ".join(str(s) for s in solutions))
    except Exception as e:
        return f"Couldn't solve equation. Error: {e}"

def handle_trigonometry(command: str):
    import math
    cmd = command.lower().strip()
    is_radians = "radian" in cmd or " rad" in cmd
    trig_map = [
        (r"sin(?:e)?\s*(?:of|for)?\s*(\d+\.?\d*)", lambda v: math.sin(v if is_radians else math.radians(v)), "sine"),
        (r"cos(?:ine)?\s*(?:of|for)?\s*(\d+\.?\d*)", lambda v: math.cos(v if is_radians else math.radians(v)), "cosine"),
        (r"tan(?:gent)?\s*(?:of|for)?\s*(\d+\.?\d*)", lambda v: math.tan(v if is_radians else math.radians(v)), "tangent"),
    ]
    unit = "radians" if is_radians else "degrees"
    for pattern, func, name in trig_map:
        m = re.search(pattern, cmd)
        if m:
            try:
                value = float(m.group(1))
                result = func(value)
                result = 0 if abs(result) < 1e-10 else result
                return f"The {name} of {value} {unit} is {result:.6f}".rstrip("0").rstrip(".")
            except Exception as e:
                return f"Error calculating {name}: {e}"
    return None

def handle_geometry_problem(command: str):
    import math
    cmd = command.lower().strip()
    if "area" in cmd:
        m = re.search(r"circle\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of circle: {math.pi * float(m.group(1))**2:.4f} sq units."
        m = re.search(r"rectangle\s+(?:with|of)\s+(?:length|l)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s+(?:width|w)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of rectangle: {float(m.group(1)) * float(m.group(2)):.4f} sq units."
        m = re.search(r"square\s+(?:with|of)\s+(?:side|s)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of square: {float(m.group(1))**2:.4f} sq units."
    elif "perimeter" in cmd or "circumference" in cmd:
        m = re.search(r"circle\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Circumference: {2 * math.pi * float(m.group(1)):.4f} units."
    elif "volume" in cmd:
        m = re.search(r"sphere\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Volume of sphere: {(4/3) * math.pi * float(m.group(1))**3:.4f} cubic units."
    return None

# ─── Dictionary / Time / Date ─────────────────────────────────────────────────

def extract_definition_word(command: str):
    cmd = command.lower().strip()
    if "what is the meaning of" in cmd: return cmd.split("what is the meaning of")[-1].strip()
    if "meaning of" in cmd: return cmd.split("meaning of")[-1].strip()
    if "define" in cmd: return cmd.split("define")[-1].strip()
    if "what does" in cmd and "mean" in cmd: return cmd.split("what does")[-1].split("mean")[0].strip()
    return None

def get_word_definition(word: str) -> str:
    try:
        response = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list):
                definitions = []
                for meaning in data[0].get("meanings", [])[:2]:
                    pos = meaning.get("partOfSpeech", "")
                    for defn in meaning.get("definitions", [])[:1]:
                        text = defn.get("definition", "")
                        if pos and text:
                            definitions.append(f"{pos}: {text}")
                if definitions:
                    return f"'{word}' means: " + " | ".join(definitions)
        return f"Sorry, I couldn't find a definition for '{word}'."
    except Exception as e:
        return f"Dictionary lookup failed. Error: {e}"

def get_current_time() -> str:
    return f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}."

def get_current_date() -> str:
    return f"Today is {datetime.now().strftime('%A, %B %d, %Y')}."

# ─── Facial Recognition ───────────────────────────────────────────────────────

def load_known_faces():
    global known_face_encodings, known_face_names
    if not FACE_RECOGNITION_AVAILABLE:
        return
    if os.path.exists(FACE_DATA_PATH):
        try:
            with open(FACE_DATA_PATH, "rb") as f:
                data = pickle.load(f)
            known_face_encodings = data.get("encodings", [])
            known_face_names = data.get("names", [])
            print(f"[Vurenn] Loaded {len(known_face_names)} known face(s).")
        except Exception as e:
            print(f"[Vurenn] Couldn't load face data: {e}")

def save_known_faces():
    try:
        with open(FACE_DATA_PATH, "wb") as f:
            pickle.dump({"encodings": known_face_encodings, "names": known_face_names}, f)
    except Exception as e:
        print(f"[Vurenn] Couldn't save face data: {e}")

def capture_frame():
    cam = cv2.VideoCapture(CAMERA_INDEX)
    if not cam.isOpened():
        cam.release(); return None
    for _ in range(5):
        cam.read()
    ret, frame = cam.read()
    cam.release()
    if not ret:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

def enroll_face(name: str) -> str:
    if not FACE_RECOGNITION_AVAILABLE:
        return "Facial recognition isn't installed."
    name = name.strip().title()
    if not name:
        return "I need a name to learn this face under."
    collected = []
    attempts = 0
    while len(collected) < 3 and attempts < 8:
        attempts += 1
        frame = capture_frame()
        if frame is None:
            continue
        encodings = face_recognition.face_encodings(frame)
        if encodings:
            collected.append(encodings[0])
        time.sleep(0.4)
    if not collected:
        return "I couldn't see a clear face. Make sure you're facing the camera in good light."
    known_face_encodings.extend(collected)
    known_face_names.extend([name] * len(collected))
    save_known_faces()
    return f"Got it — I'll remember your face as {name} from now on."

def recognize_face():
    if not FACE_RECOGNITION_AVAILABLE or not known_face_encodings:
        return None
    frame = capture_frame()
    if frame is None:
        return None
    face_locations = face_recognition.face_locations(frame)
    if not face_locations:
        return None
    encodings = face_recognition.face_encodings(frame, face_locations)
    if not encodings:
        return None
    distances = face_recognition.face_distance(known_face_encodings, encodings[0])
    if len(distances) == 0:
        return None
    best_idx = int(np.argmin(distances))
    if distances[best_idx] <= FACE_MATCH_TOLERANCE:
        return known_face_names[best_idx]
    return None

def wait_for_face_recognition(timeout=FACE_SEARCH_TIMEOUT):
    """Keeps checking the camera until a known face is found or timeout is reached."""
    if not FACE_RECOGNITION_AVAILABLE or not known_face_encodings:
        return None
    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        attempt += 1
        print(f"[Vurenn] Searching for your face... (attempt {attempt})")
        name = recognize_face()
        if name:
            return name
        time.sleep(FACE_SEARCH_INTERVAL)
    return None

def extract_enroll_name(command: str):
    cmd = command.lower().strip()
    if "learn my face" in cmd or "remember my face" in cmd:
        m = re.search(r"(?:as|call me)\s+([a-zA-Z]+)", cmd)
        return m.group(1) if m else "User"
    return None

def handle_face_enrollment(command: str):
    name = extract_enroll_name(command)
    if name is None:
        return None
    return enroll_face(name)

# ─── YouTube ──────────────────────────────────────────────────────────────────

def search_youtube(query: str):
    try:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        m = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
        return m.group(1) if m else None
    except Exception as e:
        print(f"[Vurenn] YouTube search error: {e}")
        return None

def extract_video_query(command: str):
    cmd = command.lower().strip()
    for pattern in [
        r"play\s+(.+?)\s+video(?:\s+on\s+youtube)?$",
        r"play\s+(.+?)\s+on\s+youtube$",
        r"watch\s+(.+?)\s+on\s+youtube$",
        r"youtube\s+(.+)$",
        r"show\s+me\s+(?:a|the)?\s*video\s+(?:of|about)\s+(.+)$",
    ]:
        m = re.search(pattern, cmd)
        if m:
            return m.group(1).strip()
    return None

def handle_youtube(command: str):
    query = extract_video_query(command)
    if not query:
        return None
    video_id = search_youtube(query)
    if not video_id:
        return f"I couldn't find a video for '{query}'."
    webbrowser.open_new_tab(f"https://www.youtube.com/watch?v={video_id}")
    return f"Opening '{query}' in a new tab."

# ─── MFA ──────────────────────────────────────────────────────────────────────

def verify_passphrase(spoken_text: str) -> bool:
    if not os.path.exists(PASSPHRASE_HASH_PATH):
        print("[Vurenn] No passphrase set — run setup_passphrase.py first.")
        return False
    with open(PASSPHRASE_HASH_PATH) as f:
        stored_hash = f.read().strip()
    spoken_hash = hashlib.sha256(spoken_text.strip().lower().encode()).hexdigest()
    return spoken_hash == stored_hash

def is_authenticated() -> bool:
    return time.time() < authenticated_until

def grant_session():
    global authenticated_until
    authenticated_until = time.time() + AUTH_SESSION_MINUTES * 60

# ─── Main Handler Pipeline ────────────────────────────────────────────────────

def handle_user_input(command: str) -> str:
    cmd = command.lower().strip()
    print(f"[Vurenn] Input: {cmd}")

    result = handle_face_enrollment(cmd)
    if result:
        return result

    word = extract_definition_word(cmd)
    if word:
        return get_word_definition(word)

    if re.search(r"\btime\b", cmd):
        return get_current_time()
    if re.search(r"\bdate\b|\btoday\b", cmd):
        return get_current_date()

    result = handle_youtube(cmd)
    if result:
        return result

    result = handle_spotify(cmd)
    if result:
        return result

    result = handle_trigonometry(cmd)
    if result:
        return result

    result = handle_geometry_problem(cmd)
    if result:
        return result

    sqrt_num = is_square_root_problem(cmd)
    if sqrt_num:
        return solve_square_root(sqrt_num)

    equation, variable = is_algebraic_equation(cmd)
    if equation and variable:
        return solve_algebraic_equation(equation, variable)

    math_expr = is_math_problem(cmd)
    if math_expr:
        return solve_math_problem(math_expr)

    if re.search(r"^(hi|hello|hey)\b", cmd):
        return "Hello! How can I help you?"
    if "how are you" in cmd:
        return "I'm doing great, thanks for asking!"

    return ask_claude(command)

# ─── Audio helpers ─────────────────────────────────────────────────────────────

def convert_to_wav(input_path: str, output_path: str):
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", output_path],
        check=True, capture_output=True,
    )

def transcribe_audio(wav_path: str) -> str:
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio).strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        print(f"[Vurenn] STT service error: {e}")
        return ""

def synthesize_response_audio(text: str, voice=None, rate=None, pitch=None) -> str:
    if not TTS_AVAILABLE:
        return ""
    filename = f"out_{uuid.uuid4().hex}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)
    spoken_text = apply_pronunciation_overrides(text)

    use_voice = voice or VOICE
    use_rate = rate or VOICE_RATE
    use_pitch = pitch or VOICE_PITCH

    async def _gen():
        communicate = edge_tts.Communicate(text=spoken_text, voice=use_voice, rate=use_rate, pitch=use_pitch)
        await communicate.save(filepath)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_gen())
    finally:
        loop.close()

    return f"/audio/{filename}"

# ─── Flask App ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vurenn</title>
<style>
  body { background:#1a1a2e; color:#e0e0e0; font-family:Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         height:100vh; margin:0; }
  #mic-btn { width:110px; height:110px; border-radius:50%; border:none; cursor:pointer;
             background:#0f3460; color:white; font-size:15px; font-weight:bold;
             transition:background 0.2s, transform 0.2s; }
  #mic-btn:hover { background:#16213e; }
  #mic-btn.recording { background:#e94560; transform:scale(1.08); }
  #status { margin-top:24px; font-size:16px; color:#a0c4ff; min-height:24px; }
  #transcript { margin-top:10px; font-size:14px; color:#888; max-width:600px; text-align:center; }
  #response { margin-top:14px; font-size:18px; max-width:640px; text-align:center; line-height:1.5; }
</style>
</head>
<body>
  <button id="mic-btn">🎤 Click to talk</button>
  <div id="status">Click the mic to start.</div>
  <div id="transcript"></div>
  <div id="response"></div>
  <audio id="player" style="display:none;"></audio>
  <a href="/setup" style="margin-top:30px; color:#a0c4ff; font-size:13px;">Set / change passphrase</a>
  <a href="/voice" style="margin-top:10px; color:#a0c4ff; font-size:13px;">Change voice</a>
  <a href="/subscribe" style="margin-top:10px; color:#a0c4ff; font-size:13px;">Manage subscription</a>

<script>
let mediaRecorder;
let chunks = [];
let recording = false;

const btn = document.getElementById("mic-btn");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");
const responseEl = document.getElementById("response");
const player = document.getElementById("player");

btn.addEventListener("click", async () => {
  if (!recording) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    chunks = [];
    mediaRecorder.ondataavailable = e => chunks.push(e.data);
    mediaRecorder.onstop = sendRecording;
    mediaRecorder.start();
    recording = true;
    btn.classList.add("recording");
    btn.textContent = "⏹ Click to stop";
    statusEl.textContent = "Listening...";
  } else {
    mediaRecorder.stop();
    mediaRecorder.stream.getTracks().forEach(t => t.stop());
    recording = false;
    btn.classList.remove("recording");
    btn.textContent = "🎤 Click to talk";
    statusEl.textContent = "Thinking...";
  }
});

async function sendRecording() {
  const blob = new Blob(chunks, { type: "audio/webm" });
  const formData = new FormData();
  formData.append("audio", blob, "recording.webm");

  try {
    const res = await fetch("/api/voice", { method: "POST", body: formData });
    const data = await res.json();

    if (data.stage === "error") {
      statusEl.textContent = data.message;
      return;
    }
    if (data.stage === "subscription_required") {
      statusEl.innerHTML = data.message + ' <a href="' + data.subscribe_url + '" style="color:#a0c4ff;">Subscribe</a>';
      return;
    }
    if (data.stage === "denied") {
      statusEl.textContent = data.message;
      if (data.audio_url) player.src = data.audio_url, player.play();
      return;
    }
    if (data.stage === "authenticated") {
      statusEl.textContent = data.message + " Click the mic again to talk.";
      if (data.audio_url) player.src = data.audio_url, player.play();
      return;
    }

    statusEl.textContent = "Ready.";
    transcriptEl.textContent = "You: " + data.transcript;
    responseEl.textContent = data.response;
    if (data.audio_url) {
      player.src = data.audio_url;
      player.play();
    }
  } catch (err) {
    statusEl.textContent = "Error sending audio: " + err;
  }
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

SETUP_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vurenn - Set Passphrase</title>
<style>
  body { background:#1a1a2e; color:#e0e0e0; font-family:Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         height:100vh; margin:0; }
  h2 { color:#a0c4ff; margin-bottom:6px; }
  p { color:#888; font-size:13px; max-width:360px; text-align:center; margin-top:0; }
  input { width:280px; padding:10px; margin:8px 0; border-radius:6px; border:none;
          background:#16213e; color:white; font-size:15px; }
  button { margin-top:14px; padding:10px 24px; border:none; border-radius:6px;
           background:#0f3460; color:white; font-weight:bold; cursor:pointer; font-size:15px; }
  button:hover { background:#533483; }
  #msg { margin-top:16px; font-size:14px; min-height:20px; }
  a { margin-top:24px; color:#a0c4ff; font-size:13px; }
</style>
</head>
<body>
  <h2>Set Vurenn's Passphrase</h2>
  <p>This is the phrase you'll say out loud (after face recognition) to unlock Vurenn.
  Keep it short and easy to say clearly.</p>

  <input type="password" id="phrase" placeholder="New passphrase">
  <input type="password" id="confirm" placeholder="Confirm passphrase">
  <button id="save-btn">Save Passphrase</button>
  <div id="msg"></div>
  <a href="/">&larr; Back</a>

<script>
document.getElementById("save-btn").addEventListener("click", async () => {
  const phrase = document.getElementById("phrase").value.trim();
  const confirm = document.getElementById("confirm").value.trim();
  const msg = document.getElementById("msg");

  if (!phrase || !confirm) {
    msg.style.color = "#e94560";
    msg.textContent = "Please fill in both fields.";
    return;
  }
  if (phrase !== confirm) {
    msg.style.color = "#e94560";
    msg.textContent = "Passphrases don't match.";
    return;
  }
  if (phrase.length < 4) {
    msg.style.color = "#e94560";
    msg.textContent = "Passphrase is too short.";
    return;
  }

  try {
    const res = await fetch("/api/setup-passphrase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phrase, confirm })
    });
    const data = await res.json();
    if (data.success) {
      msg.style.color = "#4caf50";
      msg.textContent = "Passphrase saved!";
      document.getElementById("phrase").value = "";
      document.getElementById("confirm").value = "";
    } else {
      msg.style.color = "#e94560";
      msg.textContent = data.message || "Something went wrong.";
    }
  } catch (err) {
    msg.style.color = "#e94560";
    msg.textContent = "Error: " + err;
  }
});
</script>
</body>
</html>
"""

@app.route("/setup")
def setup_page():
    return Response(SETUP_HTML, mimetype="text/html")

SUBSCRIBE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vurenn - Subscribe</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  body { background:#1a1a2e; color:#e0e0e0; font-family:Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         min-height:100vh; margin:0; text-align:center; padding:20px; box-sizing:border-box; }
  h2 { color:#a0c4ff; }
  p { color:#888; max-width:360px; }
  #payment-form { width:360px; max-width:90vw; }
  #payment-element { margin:16px 0; padding:16px; background:#16213e; border-radius:8px; }
  button { margin-top:12px; padding:12px 28px; border:none; border-radius:6px;
           background:#0f3460; color:white; font-weight:bold; cursor:pointer; font-size:16px; width:100%; }
  button:hover { background:#533483; }
  button:disabled { opacity:0.5; cursor:not-allowed; }
  #msg { margin-top:16px; font-size:14px; min-height:20px; color:#e94560; }
  a { margin-top:24px; color:#a0c4ff; font-size:13px; }
</style>
</head>
<body>
  <h2>Subscribe to Vurenn</h2>
  <p id="status-text">__STATUS_TEXT__</p>

  <form id="payment-form" style="__FORM_DISPLAY__">
    <div id="payment-element"></div>
    <button id="submit-btn">Subscribe</button>
  </form>
  <div id="msg"></div>
  <a href="/">&larr; Back</a>

<script>
const alreadySubscribed = __ALREADY_SUBSCRIBED__;

if (!alreadySubscribed) {
  (async () => {
    const msg = document.getElementById("msg");
    const submitBtn = document.getElementById("submit-btn");

    let stripe, elements;

    try {
      const res = await fetch("/api/create-subscription", { method: "POST" });
      const data = await res.json();
      if (data.error) {
        msg.textContent = data.error;
        submitBtn.disabled = true;
        return;
      }

      stripe = Stripe(data.publishableKey);
      elements = stripe.elements({ clientSecret: data.clientSecret });
      const paymentElement = elements.create("payment");
      paymentElement.mount("#payment-element");
    } catch (err) {
      msg.textContent = "Error loading payment form: " + err;
      return;
    }

    document.getElementById("payment-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      submitBtn.disabled = true;
      msg.style.color = "#a0c4ff";
      msg.textContent = "Processing...";

      const { error, paymentIntent } = await stripe.confirmPayment({
        elements,
        confirmParams: { return_url: window.location.origin + "/success" },
        redirect: "if_required",
      });

      if (error) {
        msg.style.color = "#e94560";
        msg.textContent = error.message;
        submitBtn.disabled = false;
      } else {
        msg.style.color = "#4caf50";
        msg.textContent = "Payment successful! Activating your subscription...";
        setTimeout(() => window.location.href = "/success", 1200);
      }
    });
  })();
}
</script>
</body>
</html>
"""

@app.route("/subscribe")
def subscribe_page():
    if is_subscribed():
        status_text = "You're already subscribed."
        already = "true"
        form_display = "display:none;"
    else:
        status_text = "Unlock Vurenn with a recurring subscription."
        already = "false"
        form_display = ""
    html = (SUBSCRIBE_HTML
            .replace("__STATUS_TEXT__", status_text)
            .replace("__ALREADY_SUBSCRIBED__", already)
            .replace("__FORM_DISPLAY__", form_display))
    return Response(html, mimetype="text/html")

@app.route("/api/create-subscription", methods=["POST"])
def api_create_subscription():
    if not STRIPE_AVAILABLE:
        return jsonify({"error": "Stripe isn't installed on the server."}), 500
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID or not STRIPE_PUBLISHABLE_KEY:
        return jsonify({"error": "Stripe isn't fully configured yet."}), 500

    stripe.api_key = STRIPE_SECRET_KEY

    try:
        customer = get_or_create_customer()

        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{"price": STRIPE_PRICE_ID}],
            payment_behavior="default_incomplete",
            payment_settings={"save_default_payment_method": "on_subscription"},
            expand=["latest_invoice.confirmation_secret"],
        )

        subscription_status["subscription_id"] = subscription.id
        save_subscription_status(subscription_status)

        client_secret = subscription.latest_invoice.confirmation_secret.client_secret

        return jsonify({
            "clientSecret": client_secret,
            "publishableKey": STRIPE_PUBLISHABLE_KEY,
        })
    except Exception as e:
        return jsonify({"error": f"Stripe error: {e}"}), 500

@app.route("/success")
def success_page():
    return Response(
        "<body style='background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:100vh;'>"
        "<div style='text-align:center;'><h2 style='color:#4caf50;'>Subscription successful!</h2>"
        "<p>You can close this tab and return to Vurenn.</p>"
        "<a href='/' style='color:#a0c4ff;'>Back to Vurenn</a></div></body>",
        mimetype="text/html",
    )

@app.route("/cancel")
def cancel_page():
    return Response(
        "<body style='background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:100vh;'>"
        "<div style='text-align:center;'><h2 style='color:#e94560;'>Checkout cancelled</h2>"
        "<a href='/subscribe' style='color:#a0c4ff;'>Try again</a></div></body>",
        mimetype="text/html",
    )

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_AVAILABLE:
        return "", 500

    stripe.api_key = STRIPE_SECRET_KEY
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            import json
            event = json.loads(payload)
    except Exception as e:
        print(f"[Vurenn] Webhook signature verification failed: {e}")
        return "", 400

    event_type = event["type"] if isinstance(event, dict) else event.type
    data_object = event["data"]["object"] if isinstance(event, dict) else event.data.object

    if event_type == "invoice.payment_succeeded":
        subscription_status["active"] = True
        customer_id = data_object.get("customer")
        subscription_id = data_object.get("subscription")
        if customer_id:
            subscription_status["customer_id"] = customer_id
        if subscription_id:
            subscription_status["subscription_id"] = subscription_id
        save_subscription_status(subscription_status)
        print("[Vurenn] Subscription activated (payment succeeded).")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        subscription_status["active"] = False
        save_subscription_status(subscription_status)
        print("[Vurenn] Subscription deactivated.")

    elif event_type == "invoice.payment_failed":
        subscription_status["active"] = False
        save_subscription_status(subscription_status)
        print("[Vurenn] Payment failed — subscription marked inactive.")

    return jsonify({"received": True})

VOICE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vurenn - Change Voice</title>
<style>
  body { background:#1a1a2e; color:#e0e0e0; font-family:Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; align-items:center; justify-content:center;
         height:100vh; margin:0; }
  h2 { color:#a0c4ff; margin-bottom:6px; }
  select, input[type=range] { width:280px; padding:10px; margin:8px 0; border-radius:6px; border:none;
          background:#16213e; color:white; font-size:15px; }
  label { font-size:13px; color:#888; margin-top:10px; }
  button { margin:8px 4px 0; padding:10px 20px; border:none; border-radius:6px;
           background:#0f3460; color:white; font-weight:bold; cursor:pointer; font-size:14px; }
  button:hover { background:#533483; }
  #msg { margin-top:16px; font-size:14px; min-height:20px; }
  a { margin-top:24px; color:#a0c4ff; font-size:13px; }
</style>
</head>
<body>
  <h2>Choose Vurenn's Voice</h2>

  <select id="voice-select"></select>

  <label>Speed: <span id="rate-val">-5%</span></label>
  <input type="range" id="rate" min="-50" max="50" value="-5">

  <label>Pitch: <span id="pitch-val">-8Hz</span></label>
  <input type="range" id="pitch" min="-50" max="50" value="-8">

  <div>
    <button id="preview-btn">▶ Preview</button>
    <button id="save-btn">Save</button>
  </div>
  <div id="msg"></div>
  <audio id="player" style="display:none;"></audio>
  <a href="/">&larr; Back</a>

<script>
const voices = __VOICE_OPTIONS__;
const select = document.getElementById("voice-select");
for (const [id, label] of Object.entries(voices)) {
  const opt = document.createElement("option");
  opt.value = id; opt.textContent = label;
  select.appendChild(opt);
}
select.value = "__CURRENT_VOICE__";

const rateSlider = document.getElementById("rate");
const pitchSlider = document.getElementById("pitch");
const rateVal = document.getElementById("rate-val");
const pitchVal = document.getElementById("pitch-val");
rateSlider.value = parseInt("__CURRENT_RATE__");
pitchSlider.value = parseInt("__CURRENT_PITCH__");
rateVal.textContent = rateSlider.value + "%";
pitchVal.textContent = pitchSlider.value + "Hz";
rateSlider.addEventListener("input", () => rateVal.textContent = rateSlider.value + "%");
pitchSlider.addEventListener("input", () => pitchVal.textContent = pitchSlider.value + "Hz");

const msg = document.getElementById("msg");
const player = document.getElementById("player");

function currentSettings() {
  return {
    voice: select.value,
    rate: rateSlider.value + "%",
    pitch: pitchSlider.value + "Hz",
  };
}

document.getElementById("preview-btn").addEventListener("click", async () => {
  msg.style.color = "#a0c4ff";
  msg.textContent = "Generating preview...";
  try {
    const res = await fetch("/api/preview-voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentSettings())
    });
    const data = await res.json();
    if (data.audio_url) {
      player.src = data.audio_url;
      player.play();
      msg.textContent = "";
    } else {
      msg.style.color = "#e94560";
      msg.textContent = data.message || "Preview failed.";
    }
  } catch (err) {
    msg.style.color = "#e94560";
    msg.textContent = "Error: " + err;
  }
});

document.getElementById("save-btn").addEventListener("click", async () => {
  msg.style.color = "#a0c4ff";
  msg.textContent = "Saving...";
  try {
    const res = await fetch("/api/set-voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentSettings())
    });
    const data = await res.json();
    if (data.success) {
      msg.style.color = "#4caf50";
      msg.textContent = "Voice saved!";
    } else {
      msg.style.color = "#e94560";
      msg.textContent = data.message || "Something went wrong.";
    }
  } catch (err) {
    msg.style.color = "#e94560";
    msg.textContent = "Error: " + err;
  }
});
</script>
</body>
</html>
"""

@app.route("/voice")
def voice_page():
    import json
    html = VOICE_HTML.replace("__VOICE_OPTIONS__", json.dumps(AVAILABLE_VOICES))
    html = html.replace("__CURRENT_VOICE__", VOICE)
    html = html.replace("__CURRENT_RATE__", VOICE_RATE.replace("%", ""))
    html = html.replace("__CURRENT_PITCH__", VOICE_PITCH.replace("Hz", ""))
    return Response(html, mimetype="text/html")

@app.route("/api/preview-voice", methods=["POST"])
def api_preview_voice():
    data = request.get_json(silent=True) or {}
    voice = data.get("voice", VOICE)
    rate = data.get("rate", VOICE_RATE)
    pitch = data.get("pitch", VOICE_PITCH)

    if voice not in AVAILABLE_VOICES:
        return jsonify({"message": "Unknown voice."}), 400

    sample_text = "Hi, I'm Vurenn. This is what I sound like."
    audio_url = synthesize_response_audio(sample_text, voice=voice, rate=rate, pitch=pitch)
    return jsonify({"audio_url": audio_url})

@app.route("/api/set-voice", methods=["POST"])
def api_set_voice():
    global VOICE, VOICE_RATE, VOICE_PITCH
    data = request.get_json(silent=True) or {}
    voice = data.get("voice")
    rate = data.get("rate")
    pitch = data.get("pitch")

    if voice not in AVAILABLE_VOICES:
        return jsonify({"success": False, "message": "Unknown voice."})

    VOICE = voice
    VOICE_RATE = rate or VOICE_RATE
    VOICE_PITCH = pitch or VOICE_PITCH
    save_voice_settings(VOICE, VOICE_RATE, VOICE_PITCH)

    return jsonify({"success": True})

@app.route("/api/setup-passphrase", methods=["POST"])
def api_setup_passphrase():
    data = request.get_json(silent=True) or {}
    phrase = (data.get("phrase") or "").strip().lower()
    confirm = (data.get("confirm") or "").strip().lower()

    if not phrase or not confirm:
        return jsonify({"success": False, "message": "Both fields are required."})
    if phrase != confirm:
        return jsonify({"success": False, "message": "Passphrases don't match."})
    if len(phrase) < 4:
        return jsonify({"success": False, "message": "Passphrase is too short."})

    digest = hashlib.sha256(phrase.encode()).hexdigest()
    try:
        with open(PASSPHRASE_HASH_PATH, "w") as f:
            f.write(digest)
    except OSError as e:
        return jsonify({"success": False, "message": f"Couldn't save passphrase: {e}"})

    return jsonify({"success": True})

@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, mimetype="audio/mpeg")

@app.route("/api/clear-audio", methods=["POST"])
def api_clear_audio():
    """Manually wipes all generated audio files immediately."""
    removed = 0
    for fname in os.listdir(AUDIO_DIR):
        fpath = os.path.join(AUDIO_DIR, fname)
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
                removed += 1
        except OSError:
            pass
    return jsonify({"cleared": removed})

@app.route("/api/voice", methods=["POST"])
def api_voice():
    global current_recognized_name

    if "audio" not in request.files:
        return jsonify({"stage": "error", "message": "No audio received."}), 400

    if not STT_AVAILABLE:
        return jsonify({"stage": "error", "message": "Speech recognition isn't available on the server."}), 500

    audio_file = request.files["audio"]
    raw_path = os.path.join(AUDIO_DIR, f"in_{uuid.uuid4().hex}.webm")
    wav_path = raw_path.replace(".webm", ".wav")
    audio_file.save(raw_path)

    try:
        convert_to_wav(raw_path, wav_path)
    except Exception as e:
        return jsonify({"stage": "error", "message": f"Audio conversion failed: {e}"}), 500
    finally:
        try:
            os.remove(raw_path)
        except OSError:
            pass

    transcript = transcribe_audio(wav_path)
    try:
        os.remove(wav_path)
    except OSError:
        pass

    if not transcript:
        return jsonify({"stage": "error", "message": "Didn't catch that — try again."})

    # ── Subscription gate ────────────────────────────────────────────────────
    if REQUIRE_SUBSCRIPTION and not is_subscribed():
        return jsonify({
            "stage": "subscription_required",
            "message": "A subscription is required to use Vurenn.",
            "subscribe_url": "/subscribe",
        })

    # ── MFA gate ──────────────────────────────────────────────────────────────
    if MFA_ENABLED and not is_authenticated():
        if current_recognized_name is None:
            name = wait_for_face_recognition()
            current_recognized_name = name

        if current_recognized_name is None:
            audio_url = synthesize_response_audio("I don't recognize your face. Access denied.")
            return jsonify({"stage": "denied", "message": "I don't recognize your face. Access denied.", "audio_url": audio_url})

        if verify_passphrase(transcript):
            grant_session()
            audio_url = synthesize_response_audio("Access granted.")
            return jsonify({"stage": "authenticated", "message": "Access granted.", "audio_url": audio_url})
        else:
            audio_url = synthesize_response_audio("Incorrect passphrase.")
            return jsonify({"stage": "denied", "message": "Incorrect passphrase.", "audio_url": audio_url})

    # ── Normal command ───────────────────────────────────────────────────────
    response_text = handle_user_input(transcript)
    audio_url = synthesize_response_audio(response_text)

    return jsonify({
        "stage": "response",
        "transcript": transcript,
        "response": response_text,
        "audio_url": audio_url,
    })

# ─── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("[Vurenn] WARNING: ANTHROPIC_API_KEY not set. General questions won't work.")

    if REQUIRE_SUBSCRIPTION and (not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID):
        print("[Vurenn] WARNING: STRIPE_SECRET_KEY / STRIPE_PRICE_ID not set. "
              "Checkout won't work until these are configured.")

    load_known_faces()
    init_spotify()
    start_audio_cleanup_loop()

    port = int(os.environ.get("PORT", "5000"))
    print(f"[Vurenn] Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
