#!/usr/bin/env python3
"""BotForge - Premium Telegram Bot Hosting Platform"""

import os, sys, re, uuid, time, shutil, zipfile, tarfile, sqlite3, json
import subprocess, threading, ast, importlib, logging, secrets, hashlib
from datetime import datetime, timedelta
from functools import wraps

def _ensure(pkgs):
    for pip_name, imp in pkgs:
        try:
            __import__(imp)
        except ImportError:
            print(f"Installing {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--quiet"])

_ensure([("Flask", "flask"), ("Werkzeug", "werkzeug"), ("psutil", "psutil")])

from flask import (Flask, request, jsonify, session, redirect, url_for,
                   render_template, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psutil

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("botforge")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
BOTS_DIR = os.path.join(DATA_DIR, "bots")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")
DB_PATH  = os.path.join(DATA_DIR, "botforge.db")

for d in (DATA_DIR, BOTS_DIR, LOGS_DIR, TEMP_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "mdrakibulhoosain@gmail.com").lower()

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
dblock = threading.Lock()

def init_db():
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        username TEXT,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'free',
        api_key TEXT,
        referral_code TEXT UNIQUE,
        referred_by TEXT,
        credits REAL DEFAULT 0,
        created_at TEXT,
        last_login TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bots(
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        token TEXT NOT NULL,
        chat_id TEXT,
        code_path TEXT,
        log_path TEXT,
        type TEXT,
        status TEXT DEFAULT 'stopped',
        pid INTEGER,
        auto_restart INTEGER DEFAULT 0,
        restarts INTEGER DEFAULT 0,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, action TEXT, detail TEXT, at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT,
        status TEXT DEFAULT 'open', reply TEXT,
        created_at TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS announcements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, body TEXT, active INTEGER DEFAULT 1,
        created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit()

init_db()

def get_setting(k, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return row["value"] if row else default

def set_setting(k, v):
    with dblock:
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
        conn.commit()

def log_activity(user_id, action, detail=""):
    try:
        with dblock:
            conn.execute("INSERT INTO activity(user_id,action,detail,at) VALUES(?,?,?,?)",
                         (user_id, action, detail, datetime.utcnow().isoformat()))
            conn.commit()
    except Exception:
        pass

def make_ref_code(uid):
    return hashlib.md5(f"ref{uid}{secrets.token_hex(4)}".encode()).hexdigest()[:8]

# ═══════════════════════════════════════════════
#  BOT MANAGEMENT
# ═══════════════════════════════════════════════

procs = {}
plock = threading.Lock()

PIP_MAP = {
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "Crypto": "pycryptodome",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "aiogram": "aiogram",
    "telethon": "Telethon",
    "discord": "discord.py",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pandas": "pandas",
    "numpy": "numpy",
    "openai": "openai",
    "gtts": "gTTS",
    "pydub": "pydub",
    "psutil": "psutil",
    "pymongo": "pymongo",
    "sqlalchemy": "SQLAlchemy",
    "redis": "redis",
    "pyrogram": "pyrogram",
    "tgcrypto": "TgCrypto",
    "moviepy": "moviepy",
    "pytz": "pytz",
    "emoji": "emoji",
    "urllib3": "urllib3",
    "certifi": "certifi",
}

def get_file_type(fn):
    if not fn: return "unknown"
    n = fn.lower()
    if n.endswith(".py"): return "python"
    if n.endswith(".js"): return "javascript"
    if n.endswith(".zip"): return "zip"
    if any(n.endswith(e) for e in [".tar", ".tar.gz", ".tgz"]): return "archive"
    return "unknown"

def extract_archive(path, out):
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z: z.extractall(out)
        elif path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(path, "r:gz") as t: t.extractall(out)
        elif path.lower().endswith(".tar"):
            with tarfile.open(path, "r") as t: t.extractall(out)
        else: return False, "Unsupported archive"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(d):
    priority = ["main.py", "bot.py", "app.py", "server.py", "index.py",
                "main.js", "bot.js", "app.js", "index.js"]
    for f in priority:
        p = os.path.join(d, f)
        if os.path.isfile(p): return p
    for root, _, files in os.walk(d):
        for f in priority:
            if f in files: return os.path.join(root, f)
    for root, _, files in os.walk(d):
        for f in files:
            if f.endswith((".py", ".js")): return os.path.join(root, f)
    return None

def uninstall_fake_telegram():
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "telegram"],
            capture_output=True, timeout=30
        )
    except Exception:
        pass

def install_requirements_file(path):
    if not os.path.exists(path): return
    try:
        with open(path) as f:
            pkgs = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        for pkg in pkgs:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                    capture_output=True, timeout=300
                )
            except Exception:
                pass
    except Exception:
        pass

def extract_imports(fp):
    imps = set()
    try:
        with open(fp, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: imps.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imps.add(node.module.split(".")[0])
    except Exception:
        pass
    return imps

def install_missing_imports(imports):
    uninstall_fake_telegram()
    missing = []
    for m in imports:
        try:
            importlib.import_module(m)
        except ImportError:
            missing.append(m)
    if not missing:
        return True, "All available"
    ok = fail = 0
    for m in missing:
        pip_name = PIP_MAP.get(m, m)
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", pip_name, "--quiet"],
                capture_output=True, text=True, timeout=300
            )
            if r.returncode == 0: ok += 1
            else: fail += 1
        except Exception:
            fail += 1
    return fail == 0, f"Installed {ok}, failed {fail}"

def start_bot_process(bot_id, code_path, log_path, env_extra=None):
    with plock:
        if bot_id in procs and procs[bot_id].poll() is None:
            return False, "Already running"

    ext = os.path.splitext(code_path)[1].lower()
    workdir = os.path.dirname(code_path)

    if ext == ".py":
        uninstall_fake_telegram()
        req = os.path.join(workdir, "requirements.txt")
        install_requirements_file(req)
        imps = extract_imports(code_path)
        if imps:
            install_missing_imports(imps)

    if ext == ".py":   cmd = [sys.executable, "-u", code_path]
    elif ext == ".js": cmd = ["node", code_path]
    else: return False, f"Unsupported: {ext}"

    env = os.environ.copy()
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})

    log_f = open(log_path, "a")
    log_f.write(f"\n{'='*50}\n▶ START {datetime.utcnow().isoformat()}\n{'='*50}\n")
    log_f.flush()

    try:
        proc = subprocess.Popen(
            cmd, stdout=log_f, stderr=subprocess.STDOUT,
            cwd=workdir, env=env, text=True
        )
    except Exception as e:
        log_f.close()
        return False, f"Failed: {e}"

    with plock:
        procs[bot_id] = proc

    def watch():
        try:
            code = proc.wait()
        finally:
            with plock:
                procs.pop(bot_id, None)
            try:
                with dblock:
                    c = conn.cursor()
                    c.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
                    conn.commit()
            except Exception:
                pass
            log_f.write(f"\n■ EXIT code={code} {datetime.utcnow().isoformat()}\n")
            log_f.close()

    threading.Thread(target=watch, daemon=True).start()
    return True, "Started"

def stop_bot_process(bot_id):
    with plock:
        proc = procs.pop(bot_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

def is_running(bot_id):
    with plock:
        p = procs.get(bot_id)
    return p is not None and p.poll() is None

def read_logs(log_path, lines=300):
    try:
        with open(log_path) as f:
            return "".join(f.readlines()[-lines:])
    except FileNotFoundError:
        return "(no logs yet)"

logger.info("Part 1+2 loaded successfully")
