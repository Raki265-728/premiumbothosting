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

logger.info("Part 1 loaded successfully")
