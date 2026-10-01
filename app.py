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
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "telegram"],
                       capture_output=True, timeout=30)
    except Exception:
        pass

def install_requirements_file(path):
    if not os.path.exists(path): return
    try:
        with open(path) as f:
            pkgs = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        for pkg in pkgs:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                               capture_output=True, timeout=300)
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
            r = subprocess.run([sys.executable, "-m", "pip", "install", pip_name, "--quiet"],
                               capture_output=True, text=True, timeout=300)
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
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                 cwd=workdir, env=env, text=True)
    except Exception as e:
        log_f.close()
        return False, f"Failed: {e}"
    with plock:
        procs[bot_id] = proc
    def watch():
        try:
            code = proc.wait()
        finally:
            with plock: procs.pop(bot_id, None)
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

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return jsonify(ok=False, error="Not authenticated"), 401
        row = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not row or row["is_banned"]:
            session.clear()
            return jsonify(ok=False, error="Account banned"), 403
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return jsonify(ok=False, error="Not authenticated"), 401
        row = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not row or not row["is_admin"]:
            return jsonify(ok=False, error="Admin only"), 403
        return f(*a, **k)
    return w

@app.route("/")
def index():
    if "uid" in session:
        return redirect(url_for("dashboard_page"))
    return render_template("landing.html")

@app.route("/dashboard")
def dashboard_page():
    if "uid" not in session:
        return redirect(url_for("index"))
    return render_template("dashboard.html")

@app.route("/admin")
def admin_page():
    if "uid" not in session:
        return redirect(url_for("index"))
    row = conn.execute("SELECT is_admin FROM users WHERE id=?", (session["uid"],)).fetchone()
    if not row or not row["is_admin"]:
        return redirect(url_for("dashboard_page"))
    return render_template("admin.html")

@app.route("/profile")
def profile_page():
    if "uid" not in session:
        return redirect(url_for("index"))
    return render_template("profile.html")

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)

@app.route("/api/signup", methods=["POST"])
def signup():
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    ref = (request.form.get("ref") or "").strip()
    if not email or not pw: return jsonify(ok=False, error="Missing fields")
    if len(pw) < 6: return jsonify(ok=False, error="Password min 6 chars")
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return jsonify(ok=False, error="Invalid email")
    try:
        with dblock:
            c = conn.cursor()
            is_admin = 1 if email == ADMIN_EMAIL else 0
            c.execute("""INSERT INTO users(email,password_hash,is_admin,referral_code,
                         referred_by,created_at,last_login,api_key)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (email, generate_password_hash(pw), is_admin, "",
                       ref, datetime.utcnow().isoformat(),
                       datetime.utcnow().isoformat(), secrets.token_hex(16)))
            conn.commit()
            uid = c.lastrowid
            ref_code = make_ref_code(uid)
            c.execute("UPDATE users SET referral_code=? WHERE id=?", (ref_code, uid))
            conn.commit()
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error="Email already registered")
    session.permanent = True
    session["uid"] = uid
    session["email"] = email
    log_activity(uid, "signup", f"New signup: {email}")
    return jsonify(ok=True, is_admin=bool(is_admin))

@app.route("/api/login", methods=["POST"])
def login():
    email = (request.form.get("email") or "").strip().lower()
    pw = request.form.get("password") or ""
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], pw):
        return jsonify(ok=False, error="Invalid credentials")
    if row["is_banned"]:
        return jsonify(ok=False, error="Account banned")
    session.permanent = True
    session["uid"] = row["id"]
    session["email"] = row["email"]
    with dblock:
        conn.execute("UPDATE users SET last_login=? WHERE id=?",
                     (datetime.utcnow().isoformat(), row["id"]))
        conn.commit()
    log_activity(row["id"], "login", "User logged in")
    return jsonify(ok=True, is_admin=bool(row["is_admin"]))

@app.route("/api/me")
def me():
    if "uid" not in session: return jsonify(ok=False)
    row = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if not row: return jsonify(ok=False)
    return jsonify(ok=True, email=row["email"], uid=row["id"],
                   is_admin=bool(row["is_admin"]), plan=row["plan"],
                   api_key=row["api_key"], referral_code=row["referral_code"],
                   credits=row["credits"])

@app.route("/api/stats")
@login_required
def stats():
    rows = conn.execute("SELECT id FROM bots WHERE user_id=?", (session["uid"],)).fetchall()
    total = len(rows)
    running = sum(1 for r in rows if is_running(r["id"]))
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
    except Exception:
        cpu = mem = 0
    return jsonify(ok=True, total=total, running=running,
                   stopped=total - running, cpu=cpu, mem=mem)

@app.route("/api/bots")
@login_required
def list_bots():
    rows = conn.execute("SELECT * FROM bots WHERE user_id=? ORDER BY created_at DESC",
                        (session["uid"],)).fetchall()
    bots = []
    for r in rows:
        bots.append({"id": r["id"], "name": r["name"], "type": r["type"],
                     "created_at": r["created_at"], "running": is_running(r["id"]),
                     "status": r["status"], "restarts": r["restarts"]})
    return jsonify(ok=True, bots=bots)

@app.route("/api/deploy", methods=["POST"])
@login_required
def deploy():
    name    = (request.form.get("name") or "My Bot").strip()
    token   = (request.form.get("token") or "").strip()
    chat_id = (request.form.get("chat_id") or "").strip()
    code    = request.form.get("code") or ""
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        return jsonify(ok=False, error="Invalid bot token format")
    if not chat_id.lstrip("-").isdigit():
        return jsonify(ok=False, error="Invalid chat ID")
    if not code.strip():
        return jsonify(ok=False, error="Code is empty")
    bot_id = uuid.uuid4().hex[:10]
    user_dir = os.path.join(BOTS_DIR, str(session["uid"]), bot_id)
    os.makedirs(user_dir, exist_ok=True)
    code_path = os.path.join(user_dir, "main.py")
    with open(code_path, "w") as f:
        f.write(code)
    log_path = os.path.join(LOGS_DIR, f"{bot_id}.log")
    with dblock:
        c = conn.cursor()
        c.execute("""INSERT INTO bots(id,user_id,name,token,chat_id,
                     code_path,log_path,type,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                  (bot_id, session["uid"], name, token, chat_id,
                   code_path, log_path, "python", datetime.utcnow().isoformat()))
        conn.commit()
    ok, msg = start_bot_process(bot_id, code_path, log_path,
                                 env_extra={"BOT_TOKEN": token, "ADMIN_CHAT_ID": chat_id})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        log_activity(session["uid"], "deploy", f"Deployed {name}")
        return jsonify(ok=True, bot_id=bot_id)
    return jsonify(ok=False, error=msg)

@app.route("/api/upload", methods=["POST"])
@login_required
def upload():
    if "file" not in request.files:
        return jsonify(ok=False, error="No file")
    f = request.files["file"]
    token = (request.form.get("token") or "").strip()
    chat_id = (request.form.get("chat_id") or "").strip()
    name = (request.form.get("name") or f.filename).strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        return jsonify(ok=False, error="Invalid token")
    if not chat_id.lstrip("-").isdigit():
        return jsonify(ok=False, error="Invalid chat ID")
    bot_id = uuid.uuid4().hex[:10]
    user_dir = os.path.join(BOTS_DIR, str(session["uid"]), bot_id)
    os.makedirs(user_dir, exist_ok=True)
    fname = secure_filename(f.filename)
    tmp = os.path.join(TEMP_DIR, f"{bot_id}_{fname}")
    f.save(tmp)
    ftype = get_file_type(fname)
    if ftype in ("zip", "archive"):
        ok, err = extract_archive(tmp, user_dir)
        if not ok: return jsonify(ok=False, error=f"Extract failed: {err}")
        os.remove(tmp)
        main = find_main_file(user_dir)
        if not main: return jsonify(ok=False, error="No main .py/.js in archive")
        code_path = main
        ftype = get_file_type(main)
    elif ftype in ("python", "javascript"):
        code_path = os.path.join(user_dir, fname)
        os.rename(tmp, code_path)
    else:
        os.remove(tmp)
        return jsonify(ok=False, error="Only .py, .js, .zip, .tar.gz")
    log_path = os.path.join(LOGS_DIR, f"{bot_id}.log")
    with dblock:
        c = conn.cursor()
        c.execute("""INSERT INTO bots(id,user_id,name,token,chat_id,
                     code_path,log_path,type,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                  (bot_id, session["uid"], name, token, chat_id,
                   code_path, log_path, ftype, datetime.utcnow().isoformat()))
        conn.commit()
    ok, msg = start_bot_process(bot_id, code_path, log_path,
                                 env_extra={"BOT_TOKEN": token, "ADMIN_CHAT_ID": chat_id})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        log_activity(session["uid"], "upload", f"Uploaded {name}")
        return jsonify(ok=True, bot_id=bot_id)
    return jsonify(ok=False, error=msg)

def own_bot(bot_id):
    return conn.execute("SELECT * FROM bots WHERE id=? AND user_id=?",
                        (bot_id, session["uid"])).fetchone()

@app.route("/api/bot/<bot_id>/start", methods=["POST"])
@login_required
def api_start(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    ok, msg = start_bot_process(bot_id, row["code_path"], row["log_path"],
                                 env_extra={"BOT_TOKEN": row["token"], "ADMIN_CHAT_ID": row["chat_id"]})
    if ok:
        conn.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        log_activity(session["uid"], "start", f"Started {row['name']}")
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg)

@app.route("/api/bot/<bot_id>/stop", methods=["POST"])
@login_required
def api_stop(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    conn.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
    conn.commit()
    log_activity(session["uid"], "stop", f"Stopped {row['name']}")
    return jsonify(ok=True)

@app.route("/api/bot/<bot_id>/restart", methods=["POST"])
@login_required
def api_restart(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    time.sleep(1)
    ok, msg = start_bot_process(bot_id, row["code_path"], row["log_path"],
                                 env_extra={"BOT_TOKEN": row["token"], "ADMIN_CHAT_ID": row["chat_id"]})
    if ok:
        conn.execute("UPDATE bots SET status='running', restarts=restarts+1 WHERE id=?", (bot_id,))
        conn.commit()
        log_activity(session["uid"], "restart", f"Restarted {row['name']}")
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg)

@app.route("/api/bot/<bot_id>/delete", methods=["POST"])
@login_required
def api_delete(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    stop_bot_process(bot_id)
    try:
        shutil.rmtree(os.path.dirname(row["code_path"]), ignore_errors=True)
    except Exception:
        pass
    conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    conn.commit()
    log_activity(session["uid"], "delete", f"Deleted {row['name']}")
    return jsonify(ok=True)

@app.route("/api/bot/<bot_id>/logs")
@login_required
def api_logs(bot_id):
    row = own_bot(bot_id)
    if not row: return jsonify(ok=False, error="Not found")
    return jsonify(ok=True, logs=read_logs(row["log_path"], 300))

@app.route("/api/profile/change-password", methods=["POST"])
@login_required
def change_password():
    old = request.form.get("old") or ""
    new = request.form.get("new") or ""
    if len(new) < 6: return jsonify(ok=False, error="Min 6 chars")
    row = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if not row or not check_password_hash(row["password_hash"], old):
        return jsonify(ok=False, error="Wrong current password")
    with dblock:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                     (generate_password_hash(new), session["uid"]))
        conn.commit()
    log_activity(session["uid"], "password_change", "")
    return jsonify(ok=True)

@app.route("/api/profile/regen-api-key", methods=["POST"])
@login_required
def regen_api_key():
    new_key = secrets.token_hex(16)
    with dblock:
        conn.execute("UPDATE users SET api_key=? WHERE id=?", (new_key, session["uid"]))
        conn.commit()
    return jsonify(ok=True, api_key=new_key)

@app.route("/api/profile/activity")
@login_required
def my_activity():
    rows = conn.execute("SELECT * FROM activity WHERE user_id=? ORDER BY id DESC LIMIT 50",
                        (session["uid"],)).fetchall()
    return jsonify(ok=True, items=[dict(r) for r in rows])

@app.route("/api/tickets", methods=["GET", "POST"])
@login_required
def tickets_api():
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC",
                            (session["uid"],)).fetchall()
        return jsonify(ok=True, tickets=[dict(r) for r in rows])
    subj = (request.form.get("subject") or "").strip()
    msg = (request.form.get("message") or "").strip()
    if not subj or not msg: return jsonify(ok=False, error="Missing fields")
    with dblock:
        c = conn.cursor()
        c.execute("""INSERT INTO tickets(user_id,subject,message,created_at,updated_at)
                     VALUES(?,?,?,?,?)""",
                  (session["uid"], subj, msg,
                   datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
        conn.commit()
    log_activity(session["uid"], "ticket", f"Opened: {subj}")
    return jsonify(ok=True)

@app.route("/api/billing/plans")
@login_required
def billing_plans():
    return jsonify(ok=True, plans=[
        {"id": "free", "name": "Free", "price": 0, "bots": 3},
        {"id": "pro", "name": "Pro", "price": 499, "bots": 20},
        {"id": "business", "name": "Business", "price": 1999, "bots": 999}
    ])

@app.route("/api/billing/upgrade", methods=["POST"])
@login_required
def billing_upgrade():
    plan = (request.form.get("plan") or "").strip()
    if plan not in ("free", "pro", "business"):
        return jsonify(ok=False, error="Invalid plan")
    with dblock:
        conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, session["uid"]))
        conn.commit()
    log_activity(session["uid"], "upgrade", f"Changed plan to {plan}")
    return jsonify(ok=True, message="Plan updated")

@app.route("/api/referrals")
@login_required
def referrals():
    me_row = conn.execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    count = conn.execute("SELECT COUNT(*) c FROM users WHERE referred_by=?",
                         (me_row["referral_code"],)).fetchone()["c"]
    return jsonify(ok=True, code=me_row["referral_code"],
                   count=count, credits=me_row["credits"])

@app.route("/api/admin/users")
@admin_required
def admin_users():
    rows = conn.execute("""SELECT id,email,username,is_admin,is_banned,plan,
                           created_at,last_login FROM users ORDER BY id DESC""").fetchall()
    return jsonify(ok=True, users=[dict(r) for r in rows])

@app.route("/api/admin/user/<int:uid>/ban", methods=["POST"])
@admin_required
def admin_ban_user(uid):
    action = request.form.get("action", "ban")
    val = 1 if action == "ban" else 0
    with dblock:
        conn.execute("UPDATE users SET is_banned=? WHERE id=?", (val, uid))
        conn.commit()
    log_activity(session["uid"], "admin_ban", f"User {uid}: {action}")
    return jsonify(ok=True)

@app.route("/api/admin/user/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_delete_user(uid):
    if uid == session["uid"]:
        return jsonify(ok=False, error="Cannot delete yourself")
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.execute("DELETE FROM bots WHERE user_id=?", (uid,))
    conn.commit()
    log_activity(session["uid"], "admin_delete_user", f"Deleted user {uid}")
    return jsonify(ok=True)

@app.route("/api/admin/bots")
@admin_required
def admin_bots():
    rows = conn.execute("""SELECT b.*, u.email FROM bots b
                           LEFT JOIN users u ON b.user_id=u.id
                           ORDER BY b.created_at DESC""").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["running"] = is_running(r["id"])
        items.append(d)
    return jsonify(ok=True, bots=items)

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_bots = conn.execute("SELECT COUNT(*) c FROM bots").fetchone()["c"]
    open_tickets = conn.execute("SELECT COUNT(*) c FROM tickets WHERE status='open'").fetchone()["c"]
    running_bots = sum(1 for b in conn.execute("SELECT id FROM bots").fetchall() if is_running(b["id"]))
    try:
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
    except Exception:
        cpu = mem = disk = 0
    return jsonify(ok=True, total_users=total_users, total_bots=total_bots,
                   running_bots=running_bots, open_tickets=open_tickets,
                   cpu=cpu, mem=mem, disk=disk)

@app.route("/api/admin/tickets")
@admin_required
def admin_tickets():
    rows = conn.execute("""SELECT t.*, u.email FROM tickets t
                           LEFT JOIN users u ON t.user_id=u.id
                           ORDER BY t.id DESC""").fetchall()
    return jsonify(ok=True, tickets=[dict(r) for r in rows])

@app.route("/api/admin/ticket/<int:tid>/reply", methods=["POST"])
@admin_required
def admin_ticket_reply(tid):
    reply = (request.form.get("reply") or "").strip()
    if not reply: return jsonify(ok=False, error="Empty reply")
    with dblock:
        conn.execute("UPDATE tickets SET reply=?, status='answered', updated_at=? WHERE id=?",
                     (reply, datetime.utcnow().isoformat(), tid))
        conn.commit()
    return jsonify(ok=True)

@app.route("/api/admin/announcement", methods=["POST"])
@admin_required
def admin_announce():
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body: return jsonify(ok=False, error="Missing fields")
    with dblock:
        conn.execute("""INSERT INTO announcements(title,body,active,created_at)
                        VALUES(?,?,1,?)""",
                     (title, body, datetime.utcnow().isoformat()))
        conn.commit()
    return jsonify(ok=True)

@app.route("/api/announcements")
@login_required
def announcements():
    rows = conn.execute("""SELECT * FROM announcements WHERE active=1
                           ORDER BY id DESC LIMIT 5""").fetchall()
    return jsonify(ok=True, items=[dict(r) for r in rows])

@app.route("/api/admin/maintenance", methods=["POST"])
@admin_required
def admin_maintenance():
    val = "1" if request.form.get("on") == "1" else "0"
    set_setting("maintenance", val)
    return jsonify(ok=True)

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="Not found"), 404
    return redirect(url_for("index"))

@app.errorhandler(413)
def too_large(e):
    return jsonify(ok=False, error="File too large (max 100MB)"), 413

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"BotForge starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
