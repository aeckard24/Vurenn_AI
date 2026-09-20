"""
Vurenn AI - Terminal Voice Assistant (no GUI)
Press Enter to start recording instead of using a wake word.
Uses Claude API for general intelligence, with fast local handlers for math/science.

Requirements:
    pip install edge-tts pygame SpeechRecognition sympy requests beautifulsoup4 anthropic spotipy
    pip install opencv-python face_recognition numpy
    pip install pyaudio  (or portaudio on Mac/Linux)

MFA setup:
    Run setup_passphrase.py once (separately) to create delta_passphrase.hash
    before using MFA_ENABLED = True.

Facial recognition setup:
    - face_recognition depends on dlib, which needs CMake + a C++ compiler.
    - Enroll a face by saying: "learn my face" or "learn my face as Sam"
      after pressing Enter to record.

Spotify setup:
    1. Go to developer.spotify.com/dashboard and create an app
    2. Set Redirect URI to http://localhost:8888/callback
    3. Copy Client ID and Client Secret below (or use env vars)
"""

import time
import re
import os
import ast
import math
import operator
import uuid
import hashlib
import requests
from datetime import datetime
from anthropic import Anthropic

# Optional imports — gracefully degrade if missing
try:
    import edge_tts
    import asyncio
    import pygame
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    print("edge-tts or pygame not found — voice output disabled.")
    print("Run: pip install edge-tts pygame")

try:
    import speech_recognition as sr
    STT_AVAILABLE = True
except ImportError:
    STT_AVAILABLE = False
    print("SpeechRecognition not found — microphone input disabled.")

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
    print("spotipy not found — Spotify disabled. Run: pip install spotipy")

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
    print("Run: pip install opencv-python face_recognition numpy")

# ─── Configuration ────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CONVERSATION_TIMEOUT = 12  # seconds to wait for a follow-up before requiring Enter again

# ── Spotify credentials ───────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID",     "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = "http://localhost:8888/callback"
SPOTIFY_SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "playlist-read-private "
    "streaming"
)

# ── Facial recognition config ────────────────────────────────────────────────
FACE_DATA_PATH       = "delta_known_faces.pkl"
FACE_MATCH_TOLERANCE  = 0.5
CAMERA_INDEX          = 0
current_recognized_name = None 


# ── MFA config ────────────────────────────────────────────────────────────────
MFA_ENABLED = True
AUTH_SESSION_MINUTES = 30
PASSPHRASE_HASH_PATH = "delta_passphrase.hash"
authenticated_until = 0.0

# ── Voice config ──────────────────────────────────────────────────────────────
VOICE = "en-GB-RyanNeural"
VOICE_RATE  = "-5%"
VOICE_PITCH = "-8Hz"

# ── Pronunciation overrides ───────────────────────────────────────────────────
PRONUNCIATION_OVERRIDES = {
    "obi-wan": "OH-bee wahn",
    "obi wan": "OH-bee wahn",
    "anakin": "AN-uh-kin",
    "padmé": "PAD-may",
    "padme": "PAD-may",
    "ahsoka": "ah-SOH-kah",
    "chewbacca": "choo-BAH-kah",
    "coruscant": "KOR-uh-sant",
    "tatooine": "tat-oo-EEN",
    "naboo": "nah-BOO",
    "kylo ren": "KY-loh wren",
    "darth vader": "darth VAY-der",
    "mandalorian": "man-duh-LOR-ee-an",
    "wookiee": "WOOK-ee",
    "r2-d2": "are two dee two",
    "c-3po": "see three pee oh",
    "yoda": "YOH-duh",
    "palpatine": "PAL-puh-teen",
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
For simple factual questions, a sentence or two is fine. For anything that benefits
from explanation, context, or multiple points, take the space you need.
You speak clearly and avoid markdown formatting since your output is read aloud.
You are helpful, honest, and warm.

When the user asks for advice, guidance, or how to handle a life situation
(relationships, decisions, struggles, moral questions, etc.), offer counsel
grounded in a Christian worldview — drawing on biblical principles and, where
relevant, referencing specific scripture (name the book/chapter/verse when you
quote or paraphrase one). Keep it warm and practical, not preachy. If the
user's question is purely factual or technical (math, weather, directions,
etc.), just answer it directly without inserting religious content."""

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

# ─── Spotify Client ──────────────────────────────────────────────────────────

sp_client = None

def init_spotify():
    global sp_client
    if not SPOTIFY_AVAILABLE:
        return
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("[Vurenn] Spotify credentials not set — Spotify disabled.")
        return
    try:
        sp_client = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=SPOTIFY_SCOPES,
            open_browser=True,
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

    if re.search(r"what(?:'s| is)(?: currently)? playing|current(?:ly playing)? song|now playing", cmd):
        try:
            current = sp_client.current_playback()
            if current and current.get("item"):
                track = current["item"]["name"]
                artist = current["item"]["artists"][0]["name"]
                state = "playing" if current["is_playing"] else "paused"
                return f"Currently {state}: {track} by {artist}."
            return "Nothing is playing right now."
        except Exception as e:
            return f"Couldn't check playback. Error: {e}"

    if re.search(r"\bpause\b|stop music|stop playing|stop spotify", cmd):
        try:
            sp_client.pause_playback()
            return "Music paused."
        except Exception as e:
            return f"Couldn't pause. Error: {e}"

    if re.search(r"\bresume\b|continue playing|unpause", cmd):
        try:
            device_id = _active_device()
            sp_client.start_playback(device_id=device_id)
            return "Resuming music."
        except Exception as e:
            return f"Couldn't resume. Error: {e}"

    if re.search(r"\bskip\b|next song|next track|skip song", cmd):
        try:
            sp_client.next_track()
            time.sleep(0.5)
            current = sp_client.current_playback()
            if current and current.get("item"):
                track = current["item"]["name"]
                artist = current["item"]["artists"][0]["name"]
                return f"Skipped! Now playing {track} by {artist}."
            return "Skipped to next track."
        except Exception as e:
            return f"Couldn't skip. Error: {e}"

    if re.search(r"previous song|go back|last song|previous track", cmd):
        try:
            sp_client.previous_track()
            time.sleep(0.5)
            current = sp_client.current_playback()
            if current and current.get("item"):
                track = current["item"]["name"]
                artist = current["item"]["artists"][0]["name"]
                return f"Going back to {track} by {artist}."
            return "Went back to previous track."
        except Exception as e:
            return f"Couldn't go back. Error: {e}"

    vol_match = re.search(r"(?:set|change|turn)?\s*volume\s*(?:to|at)?\s*(\d{1,3})(?:\s*percent)?", cmd)
    if vol_match:
        try:
            vol = max(0, min(100, int(vol_match.group(1))))
            sp_client.volume(vol)
            return f"Volume set to {vol} percent."
        except Exception as e:
            return f"Couldn't set volume. Error: {e}"

    if re.search(r"volume up|louder|increase volume|turn it up", cmd):
        try:
            current = sp_client.current_playback()
            if current:
                new_vol = min(100, (current.get("device", {}).get("volume_percent", 50) or 50) + 15)
                sp_client.volume(new_vol)
                return f"Volume up to {new_vol} percent."
        except Exception as e:
            return f"Couldn't raise volume. Error: {e}"

    if re.search(r"volume down|quieter|decrease volume|turn it down", cmd):
        try:
            current = sp_client.current_playback()
            if current:
                new_vol = max(0, (current.get("device", {}).get("volume_percent", 50) or 50) - 15)
                sp_client.volume(new_vol)
                return f"Volume down to {new_vol} percent."
        except Exception as e:
            return f"Couldn't lower volume. Error: {e}"

    play_match = re.search(r"play\s+(.+?)(?:\s+(?:by|from|on spotify))?$", cmd)
    if play_match or cmd.startswith("play"):
        query = play_match.group(1).strip() if play_match else cmd.replace("play", "").strip()
        if not query:
            try:
                device_id = _active_device()
                sp_client.start_playback(device_id=device_id)
                return "Resuming playback."
            except Exception as e:
                return f"Couldn't start playback. Error: {e}"
        try:
            results = sp_client.search(q=query, limit=1, type="track,artist,playlist")

            tracks = results.get("tracks", {}).get("items", [])
            if tracks:
                track = tracks[0]
                device_id = _active_device()
                sp_client.start_playback(device_id=device_id, uris=[track["uri"]])
                return f"Playing {track['name']} by {track['artists'][0]['name']}."

            artists = results.get("artists", {}).get("items", [])
            if artists:
                artist = artists[0]
                device_id = _active_device()
                sp_client.start_playback(device_id=device_id, context_uri=artist["uri"])
                return f"Playing music by {artist['name']}."

            playlists = results.get("playlists", {}).get("items", [])
            if playlists:
                pl = playlists[0]
                device_id = _active_device()
                sp_client.start_playback(device_id=device_id, context_uri=pl["uri"])
                return f"Playing playlist: {pl['name']}."

            return f"I couldn't find anything for '{query}' on Spotify."
        except Exception as e:
            return f"Spotify error: {e}"

    return None

# ─── Math Handlers ────────────────────────────────────────────────────────────

def clean_math_query(command: str) -> str:
    prefixes = [
        "what is", "what's", "whats", "calculate", "evaluate",
        "solve", "find", "work out", "compute", "tell me",
    ]
    command = command.lower().strip()
    for prefix in prefixes:
        if command.startswith(prefix):
            command = command[len(prefix):].strip()
            break
    return command


FACE_SEARCH_INTERVAL = 1.0   # seconds between capture attempts
FACE_SEARCH_TIMEOUT  = 30    # give up after this many seconds of searching

def wait_for_face_recognition(timeout=FACE_SEARCH_TIMEOUT):
    """
    Keeps capturing frames and checking against known faces until a match
    is found or the timeout is reached. Returns the matched name, or None
    if nothing was recognized within the timeout.
    """
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

def convert_natural_language_exponents(command: str) -> str:
    patterns = [
        (r"(\b\d+\.?\d*\b)\s+squared\b",                             r"\1**2"),
        (r"(\b\d+\.?\d*\b)\s+cubed\b",                               r"\1**3"),
        (r"(\b\d+\.?\d*\b)\s+to the power of\s+(\b\d+\.?\d*\b)",    r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+to the power\s+(\b\d+\.?\d*\b)",       r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+to the\s+(\b\d+\.?\d*\b)",             r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s*\^\s*(\b\d+\.?\d*\b)",                 r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+raised to\s+(\b\d+\.?\d*\b)",          r"\1**\2"),
        (r"(\b\d+\.?\d*\b)\s+raised to the power of\s+(\b\d+\.?\d*\b)", r"\1**\2"),
    ]
    for pattern, replacement in patterns:
        command = re.sub(pattern, replacement, command, flags=re.IGNORECASE)
    return command

def is_math_problem(command: str):
    cleaned = clean_math_query(command)
    cleaned = convert_natural_language_exponents(cleaned)
    if any(term in cleaned for term in ["squared", "cubed", "power", "raised to", "^"]):
        cleaned = convert_natural_language_exponents(command)
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
        expr = convert_natural_language_exponents(expr)
        expr = expr.replace("^", "**")
        expr = re.sub(r"\s+", "", expr)
        if SYMPY_AVAILABLE:
            result = sp.sympify(expr).evalf()
            return str(int(result)) if result.is_integer else f"{float(result):.6f}".rstrip("0").rstrip(".")
        else:
            result = safe_numeric_expression(expr)
            return str(result)
    except Exception as e:
        return f"I couldn't solve that. Error: {e}"

def is_square_root_problem(command: str):
    command = clean_math_query(command)
    patterns = [
        r"square\s+root\s+of\s+(\d+\.?\d*)",
        r"sqrt\s*\(\s*(\d+\.?\d*)\s*\)",
        r"√\s*(\d+\.?\d*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def solve_square_root(number_str: str) -> str:
    try:
        number = float(number_str)
        if number < 0:
            return f"The square root of {number} is not a real number."
        import math
        result = math.sqrt(number)
        return str(int(result)) if result == int(result) else f"{result:.6f}".rstrip("0").rstrip(".")
    except Exception as e:
        return f"Couldn't calculate square root. Error: {e}"

def is_algebraic_equation(command: str):
    cleaned = clean_math_query(command)
    if "=" in cleaned and re.search(r"[a-zA-Z]", cleaned):
        variable = None
        for pattern in [r"solve\s+for\s+([a-zA-Z])", r"find\s+([a-zA-Z])", r"what\s+is\s+([a-zA-Z])"]:
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                variable = match.group(1)
                break
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

# ─── Trigonometry ─────────────────────────────────────────────────────────────

def handle_trigonometry(command: str):
    import math
    cmd = command.lower().strip()
    is_radians = "radian" in cmd or " rad" in cmd

    trig_map = [
        (r"sin(?:e)?\s*(?:of|for)?\s*(\d+\.?\d*)",   lambda v: math.sin(v if is_radians else math.radians(v)), "sine"),
        (r"cos(?:ine)?\s*(?:of|for)?\s*(\d+\.?\d*)", lambda v: math.cos(v if is_radians else math.radians(v)), "cosine"),
        (r"tan(?:gent)?\s*(?:of|for)?\s*(\d+\.?\d*)",lambda v: math.tan(v if is_radians else math.radians(v)), "tangent"),
        (r"(?:arc\s*sin|asin)\s*(?:of|for)?\s*(-?\d+\.?\d*)", lambda v: math.degrees(math.asin(v)) if not is_radians else math.asin(v), "arcsine"),
        (r"(?:arc\s*cos|acos)\s*(?:of|for)?\s*(-?\d+\.?\d*)", lambda v: math.degrees(math.acos(v)) if not is_radians else math.acos(v), "arccosine"),
        (r"(?:arc\s*tan|atan)\s*(?:of|for)?\s*(-?\d+\.?\d*)", lambda v: math.degrees(math.atan(v)) if not is_radians else math.atan(v), "arctangent"),
    ]

    unit = "radians" if is_radians else "degrees"
    for pattern, func, name in trig_map:
        match = re.search(pattern, cmd)
        if match:
            try:
                value = float(match.group(1))
                result = func(value)
                result = 0 if abs(result) < 1e-10 else result
                return f"The {name} of {value} {unit} is {result:.6f}".rstrip("0").rstrip(".")
            except ValueError as e:
                return f"Math error for {name}: {e}"
            except Exception as e:
                return f"Error calculating {name}: {e}"
    return None

# ─── Geometry ─────────────────────────────────────────────────────────────────

def handle_geometry_problem(command: str):
    import math
    cmd = command.lower().strip()

    if "area" in cmd:
        m = re.search(r"circle\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of circle with radius {m.group(1)}: {math.pi * float(m.group(1))**2:.4f} sq units."

        m = re.search(r"rectangle\s+(?:with|of)\s+(?:length|l)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s+(?:width|w)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of rectangle: {float(m.group(1)) * float(m.group(2)):.4f} sq units."

        m = re.search(r"square\s+(?:with|of)\s+(?:side|s)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of square: {float(m.group(1))**2:.4f} sq units."

        m = re.search(r"triangle\s+(?:with|of)\s+(?:base|b)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s+(?:height|h)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Area of triangle: {0.5 * float(m.group(1)) * float(m.group(2)):.4f} sq units."

    elif "perimeter" in cmd or "circumference" in cmd:
        m = re.search(r"circle\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Circumference: {2 * math.pi * float(m.group(1)):.4f} units."

        m = re.search(r"rectangle\s+(?:with|of)\s+(?:length|l)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s+(?:width|w)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Perimeter of rectangle: {2 * (float(m.group(1)) + float(m.group(2))):.4f} units."

        m = re.search(r"square\s+(?:with|of)\s+(?:side|s)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Perimeter of square: {4 * float(m.group(1)):.4f} units."

    elif "volume" in cmd:
        m = re.search(r"cube\s+(?:with|of)\s+(?:side|s)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Volume of cube: {float(m.group(1))**3:.4f} cubic units."

        m = re.search(r"sphere\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Volume of sphere: {(4/3) * math.pi * float(m.group(1))**3:.4f} cubic units."

        m = re.search(r"cylinder\s+(?:with|of)\s+(?:radius|r)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s+(?:height|h)\s*=?\s*(\d+\.?\d*)", cmd)
        if m: return f"Volume of cylinder: {math.pi * float(m.group(1))**2 * float(m.group(2)):.4f} cubic units."

    elif "pythagorean" in cmd or ("right triangle" in cmd and "hypotenuse" in cmd):
        m = re.search(r"(?:sides?|legs?|a|b)\s*=?\s*(\d+\.?\d*)\s+(?:and)?\s*(\d+\.?\d*)", cmd)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            return f"Hypotenuse of right triangle with legs {a} and {b}: {math.sqrt(a**2 + b**2):.4f} units."

    return None

# ─── Dictionary ───────────────────────────────────────────────────────────────

def extract_definition_word(command: str):
    cmd = command.lower().strip()
    if "what is the meaning of" in cmd:
        return cmd.split("what is the meaning of")[-1].strip()
    if "meaning of" in cmd:
        return cmd.split("meaning of")[-1].strip()
    if "define" in cmd:
        return cmd.split("define")[-1].strip()
    if "what does" in cmd and "mean" in cmd:
        return cmd.split("what does")[-1].split("mean")[0].strip()
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

# ─── Time / Date ──────────────────────────────────────────────────────────────

def get_current_time() -> str:
    t = datetime.now().strftime("%I:%M %p").lstrip("0")
    return f"It's {t}."

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
            known_face_names     = data.get("names", [])
            print(f"[Vurenn] Loaded {len(known_face_names)} known face(s): {set(known_face_names)}")
        except Exception as e:
            print(f"[Vurenn] Couldn't load face data: {e}")
    else:
        print("[Vurenn] No saved faces yet. Say 'learn my face' after pressing Enter to enroll.")

def save_known_faces():
    try:
        with open(FACE_DATA_PATH, "wb") as f:
            pickle.dump({"encodings": known_face_encodings, "names": known_face_names}, f)
    except Exception as e:
        print(f"[Vurenn] Couldn't save face data: {e}")

def capture_frame():
    cam = cv2.VideoCapture(CAMERA_INDEX)
    if not cam.isOpened():
        cam.release()
        return None
    for _ in range(5):
        cam.read()
    ret, frame = cam.read()
    cam.release()
    if not ret:
        return None
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

def enroll_face(name: str) -> str:
    if not FACE_RECOGNITION_AVAILABLE:
        return "Facial recognition isn't installed. Run: pip install opencv-python face_recognition"

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
        return "I couldn't see a clear face. Make sure you're facing the camera in good light and try again."

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

    face_encoding = encodings[0]
    distances = face_recognition.face_distance(known_face_encodings, face_encoding)
    if len(distances) == 0:
        return None

    best_idx = int(np.argmin(distances))
    if distances[best_idx] <= FACE_MATCH_TOLERANCE:
        return known_face_names[best_idx]
    return None

def extract_enroll_name(command: str):
    cmd = command.lower().strip()
    if "learn my face" in cmd or "remember my face" in cmd:
        m = re.search(r"(?:as|call me)\s+([a-zA-Z]+)", cmd)
        if m:
            return m.group(1)
        return "User"
    return None

def handle_face_enrollment(command: str):
    name = extract_enroll_name(command)
    if name is None:
        return None
    print(f"[Vurenn] Look at the camera... learning your face as {name}.")
    return enroll_face(name)

FACE_SEARCH_INTERVAL = 1.0   # seconds between capture attempts
FACE_SEARCH_TIMEOUT  = 30    # give up after this many seconds of searching

def recognize_face_once():
    """Searches for a known face at startup until recognized or the search
    times out, then caches the result for the rest of the session."""
    global current_recognized_name
    if not FACE_RECOGNITION_AVAILABLE:
        return
    print("[Vurenn] Looking for a familiar face...")
    name = wait_for_face_recognition()
    current_recognized_name = name
    if name:
        print(f"[Vurenn] Recognized: {name}")
    else:
        print("[Vurenn] No known face recognized within the search window.")

# ─── YouTube ──────────────────────────────────────────────────────────────────

def search_youtube(query: str):
    try:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()

        match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        print(f"[Vurenn] YouTube search error: {e}")
        return None

def extract_video_query(command: str):
    cmd = command.lower().strip()
    patterns = [
        r"play\s+(.+?)\s+video(?:\s+on\s+youtube)?$",
        r"play\s+(.+?)\s+on\s+youtube$",
        r"watch\s+(.+?)\s+on\s+youtube$",
        r"youtube\s+(.+)$",
        r"show\s+me\s+(?:a|the)?\s*video\s+(?:of|about)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, cmd)
        if match:
            return match.group(1).strip()
    return None

def handle_youtube(command: str):
    query = extract_video_query(command)
    if not query:
        return None

    video_id = search_youtube(query)
    if not video_id:
        return f"I couldn't find a video for '{query}'."

    url = f"https://www.youtube.com/watch?v={video_id}"
    webbrowser.open_new_tab(url)
    return f"Opening '{query}' in a new tab."

# ─── MFA ──────────────────────────────────────────────────────────────────────

def verify_passphrase(spoken_text: str) -> bool:
    if not os.path.exists(PASSPHRASE_HASH_PATH):
        print("[Vurenn] No passphrase set — run setup_passphrase.py first.")
        return False
    with open(PASSPHRASE_HASH_PATH) as f:
        stored_hash = f.read().strip()
    normalized = spoken_text.strip().lower()
    spoken_hash = hashlib.sha256(normalized.encode()).hexdigest()
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

    face_enroll_result = handle_face_enrollment(cmd)
    if face_enroll_result:
        return face_enroll_result

    word = extract_definition_word(cmd)
    if word:
        return get_word_definition(word)

    if re.search(r"\btime\b", cmd):
        return get_current_time()
    if re.search(r"\bdate\b|\btoday\b", cmd):
        return get_current_date()

    youtube_result = handle_youtube(cmd)
    if youtube_result:
        return youtube_result

    spotify_result = handle_spotify(cmd)
    if spotify_result:
        return spotify_result

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

# ─── Speech ───────────────────────────────────────────────────────────────────

async def _synthesize(text: str, filepath: str):
    spoken_text = apply_pronunciation_overrides(text)
    communicate = edge_tts.Communicate(
        text=spoken_text,
        voice=VOICE,
        rate=VOICE_RATE,
        pitch=VOICE_PITCH,
    )
    await communicate.save(filepath)

def speak(text: str):
    if not TTS_AVAILABLE:
        print("[Vurenn] (voice output unavailable)")
        return

    temp_file = f"delta_tts_{uuid.uuid4().hex}.mp3"

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_synthesize(text, temp_file))
        finally:
            loop.close()

        pygame.mixer.music.load(temp_file)
        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            time.sleep(0.05)

        pygame.mixer.music.unload()

    except Exception as e:
        print(f"[Vurenn] TTS error: {e}")
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
    finally:
        try:
            os.remove(temp_file)
        except OSError:
            pass

def listen_long_question(recognizer, source, first_timeout=None) -> str:
    """Listens for a full question, collecting chunks until a pause or timeout."""
    SILENCE_TIMEOUT    = 2.5
    MAX_QUESTION_SECS  = 120
    CHUNK_LIMIT        = 30

    chunks = []
    total_elapsed = 0.0
    is_first_chunk = True
    print("[Vurenn] Listening... (speak as long as you need)")

    while total_elapsed < MAX_QUESTION_SECS:
        try:
            recognizer.pause_threshold = SILENCE_TIMEOUT
            recognizer.non_speaking_duration = SILENCE_TIMEOUT

            timeout = first_timeout if (is_first_chunk and first_timeout is not None) else SILENCE_TIMEOUT + 1
            is_first_chunk = False

            chunk_start = time.time()
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=CHUNK_LIMIT)
            elapsed = time.time() - chunk_start
            total_elapsed += elapsed

            try:
                text = recognizer.recognize_google(audio).strip()
                if text:
                    chunks.append(text)
                    print(f"[Vurenn] Heard: {' '.join(chunks)}")
            except sr.UnknownValueError:
                if chunks:
                    break

        except sr.WaitTimeoutError:
            break

    return " ".join(chunks)

# ─── Main Loop (button/keypress-driven, no wake word) ────────────────────────

def run():

    global current_recognized_name
    if not STT_AVAILABLE:
        print("[Vurenn] SpeechRecognition not available — voice input disabled. Exiting.")
        return

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    recognizer.energy_threshold = 300
    recognizer.pause_threshold = 0.8

    conversation_active = False

    print("=" * 60)
    print("Vurenn is ready.")
    print("Press ENTER to start recording. Press Ctrl+C to quit.")
    print("=" * 60)

    while True:
        try:
            if not conversation_active:
                input("\nPress ENTER to record...")

                if MFA_ENABLED and not is_authenticated():
                    if current_recognized_name is None:
                        print("[Vurenn] Searching for your face...")
                        speak("Let me look for your face.")
                        name = wait_for_face_recognition()
                        if name is None:
                            print("[Vurenn] I still don't recognize you. Access denied.")
                            speak("I still don't recognize you. Access denied.")
                            continue
                        current_recognized_name = name
                        print(f"[Vurenn] Recognized: {name}")

                    print("[Vurenn] Face recognized. Say the passphrase now.")
                    speak("Face recognized. What's the passphrase?")

                    with sr.Microphone() as source:
                        recognizer.adjust_for_ambient_noise(source, duration=1.0)
                        spoken = listen_long_question(recognizer, source, first_timeout=6)

                    if not spoken or not verify_passphrase(spoken):
                        print("[Vurenn] Incorrect passphrase. Access denied.")
                        speak("Incorrect passphrase. Access denied.")
                        continue

                    grant_session()
                    print("[Vurenn] Access granted.")
                    speak("Access granted.")

                print("[Vurenn] Go ahead, I'm listening.")
                first_timeout = 5
            else:
                print("\n[Vurenn] Listening for a follow-up...")
                first_timeout = CONVERSATION_TIMEOUT

            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=1.0)
                full_command = listen_long_question(recognizer, source, first_timeout=first_timeout)

            if full_command:
                print(f"[Vurenn] Full command: {full_command}")
                response = handle_user_input(full_command)
                print(f"[Vurenn] Response: {response}")
                speak(response)
                conversation_active = True
            else:
                conversation_active = False
                print("[Vurenn] Didn't hear anything. Press ENTER to try again.")

        except KeyboardInterrupt:
            print("\n[Vurenn] Shutting down. Goodbye!")
            break
        except sr.WaitTimeoutError:
            conversation_active = False
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            print(f"[Vurenn] STT service error: {e}")
        except Exception as e:
            print(f"[Vurenn] Error: {e}")
            time.sleep(1)


# ─── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if TTS_AVAILABLE:
        pygame.mixer.init(frequency=24000)

    if not ANTHROPIC_API_KEY:
        print("[Vurenn] WARNING: ANTHROPIC_API_KEY not set. General questions won't work.")

    load_known_faces()
    init_spotify()
    if FACE_RECOGNITION_AVAILABLE:
        recognize_face_once()

    run()

