#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════╗
║   🚀  BotHost — Paid Telegram Bot Hosting     ║
╚══════════════════════════════════════════════╝
• New users get a one-time free trial (12h) — bot is auto-deleted afterwards
• Users buy a storage plan (UPI + screenshot) → admin approves → they can host
• Upload .py files + requirements.txt, run / stop / logs from inline buttons
• Admin panel: stats, payments, users, ban, free access, broadcast, running bots

Run on Linux / macOS (uses process groups + resource limits).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import wraps
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

BASE = Path(__file__).parent.resolve()

# ═════════════════════════ CONFIG — EDIT THIS ═════════════════════════
TOKEN = "8618559705:AAFNAFKajKN4-pAqCmbL65OZVgZREcog4Xs"        # token of THIS hosting bot (@BotFather)
ADMINS = {8679787798, 8679787798}            # Telegram user IDs of admins

UPI_ID = "8707210511@fam"                    # where users pay
UPI_NAME = "X HOST"
UPI_QR_PATH = BASE / "upi_qr.jpg"          # optional: put a QR image here and it is sent automatically
SUPPORT_CONTACT = "@tchbster"         # shown on rejection messages

CURRENCY = "₹"
PLAN_DAYS = 30                             # validity of every purchased plan
PLANS = {                                  # key → plan  (edit freely)
    "p100": {"label": "100 MB", "mb": 100,  "price": 10},
    "p300": {"label": "300 MB", "mb": 300,  "price": 30},
    "p1g":  {"label": "1 GB",   "mb": 1024, "price": 70},
}
ADMIN_QUOTA_MB = 10240                     # admins get this much, never expires
COUNT_VENV_IN_QUOTA = False                # False = installed packages don't use up the user's MB
MEM_LIMIT_MB = 1024                        # memory cap per hosted bot (0 = off)
MAX_PROJECTS_PER_USER = 10

TRIAL_ENABLED = True                       # free trial for brand-new users
TRIAL_HOURS = 12                           # after this the bot is stopped + deleted
TRIAL_MB = 50                              # storage during the trial
TRIAL_MAX_PROJECTS = 1
# ══════════════════════════════════════════════════════════════════════

DATA = BASE / "data"
PROJECTS = DATA / "projects"
TMP = DATA / "tmp"
DB_FILE = DATA / "db.json"

MB = 1024 * 1024
MAX_PY_SIZE = 1_000_000
MAX_REQ_SIZE = 100_000
PIP_TIMEOUT = 900
LOG_TAIL_CHARS = 3300
LOG_MAX_BYTES = 5 * MB
USERS_PER_PAGE = 8

for d in (PROJECTS, TMP):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bothost")
logging.getLogger("httpx").setLevel(logging.WARNING)

LINE = "━━━━━━━━━━━━━━━━━━━━"
HTML = ParseMode.HTML
STATUS_ICON = {"awaiting": "🕓", "pending": "⏳", "approved": "✅", "rejected": "❌", "cancelled": "➖"}
BG_TASKS: set[asyncio.Task] = set()


# ═════════════════════════ Formatting helpers ═════════════════════════
def fmt_size(b: float) -> str:
    if b >= 1024 * MB:
        return f"{b / (1024 * MB):.2f} GB"
    return f"{b / MB:.1f} MB"


def mb_label(mb: int) -> str:
    return f"{mb // 1024} GB" if mb >= 1024 and mb % 1024 == 0 else f"{mb} MB"


def bar(used: float, total: float, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, round(width * used / total))
    return "▰" * filled + "▱" * (width - filled)


def fmt_dur(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v)[:20] or "0s"


def fmt_date(ts: float) -> str:
    return time.strftime("%d %b %Y", time.localtime(ts))


def fmt_left(exp: float | None) -> str:
    if exp is None:
        return "lifetime ♾"
    s = int(exp - time.time())
    if s <= 0:
        return "expired"
    d, rem = divmod(s, 86400)
    return f"{d}d {rem // 3600}h left" if d else f"{rem // 3600}h {(rem % 3600) // 60}m left"


def money(x: float) -> str:
    return f"{CURRENCY}{x:,.0f}"


def day_start() -> float:
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))


def month_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


# ═════════════════════════ Database ═════════════════════════
class DB:
    """Tiny JSON database: users, projects, payments."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"users": {}, "projects": {}, "payments": {}, "pay_seq": 0}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except Exception:
                log.exception("Could not read DB, starting fresh")
        if not isinstance(self.data["users"], dict):
            self.data["users"] = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    # ── users
    def user(self, uid: int) -> dict | None:
        return self.data["users"].get(str(uid))

    def stub_user(self, uid: int) -> dict:
        u = self.data["users"].setdefault(str(uid), {
            "joined": time.time(), "banned": False, "blocked": False,
            "plan": None, "warned": False, "name": "Unknown", "username": ""})
        return u

    def ensure_user(self, tg) -> dict:
        new = str(tg.id) not in self.data["users"]
        u = self.stub_user(tg.id)
        name, uname = tg.first_name or "User", tg.username or ""
        if new or u.get("name") != name or u.get("username") != uname:
            u["name"], u["username"] = name, uname
            self.save()
        return u

    # ── projects
    def projects(self, uid: int) -> dict:
        return self.data["projects"].setdefault(str(uid), {})

    def get(self, uid: int, name: str) -> dict | None:
        return self.projects(uid).get(name)

    def put(self, uid: int, name: str, **fields):
        p = self.projects(uid).setdefault(
            name, {"created": time.time(), "autostart": False, "has_reqs": False,
                   "installed": False, "env": {}})
        p.update(fields)
        self.save()
        return p

    def remove(self, uid: int, name: str):
        self.projects(uid).pop(name, None)
        self.save()

    # ── payments
    def new_payment(self, uid: int, key: str) -> dict:
        self.data["pay_seq"] += 1
        pid = self.data["pay_seq"]
        plan = PLANS[key]
        p = {"id": pid, "uid": uid, "key": key, "mb": plan["mb"], "label": plan["label"],
             "amount": plan["price"], "status": "awaiting", "created": time.time(),
             "msgs": [], "utr": "", "file_id": "", "ftype": "photo"}
        self.data["payments"][str(pid)] = p
        self.save()
        return p

    def payment(self, pid: int) -> dict | None:
        return self.data["payments"].get(str(pid))

    def user_payments(self, uid: int) -> list[dict]:
        return sorted((p for p in self.data["payments"].values() if p["uid"] == uid),
                      key=lambda p: p["id"])


db = DB(DB_FILE)


# ═════════════════════════ Plans & quota ═════════════════════════
def get_plan(uid: int) -> dict | None:
    """Active plan of a user or None."""
    if uid in ADMINS:
        return {"mb": ADMIN_QUOTA_MB, "expires": None, "source": "admin"}
    u = db.user(uid)
    if not u:
        return None
    p = u.get("plan")
    if p and not (p.get("expires") and p["expires"] < time.time()):
        return p
    t = u.get("trial")
    if t and not t.get("done") and t["expires"] > time.time():
        return {"mb": TRIAL_MB, "expires": t["expires"], "source": "trial"}
    return None


def activate_paid(uid: int, key: str) -> dict:
    plan, now = PLANS[key], time.time()
    u = db.stub_user(uid)
    cur = u.get("plan")
    base = now
    # renewing the same size stacks on top of the remaining time
    if cur and cur.get("source") == "paid" and cur["mb"] == plan["mb"] and (cur.get("expires") or 0) > now:
        base = cur["expires"]
    u["plan"] = {"mb": plan["mb"], "source": "paid", "expires": base + PLAN_DAYS * 86400}
    u["ever_plan"] = True
    u["warned"] = False
    db.save()
    return u["plan"]


def grant_free(uid: int, mb: int, days: int) -> dict:
    u = db.stub_user(uid)
    u["plan"] = {"mb": mb, "source": "free", "expires": None if days == 0 else time.time() + days * 86400}
    u["ever_plan"] = True
    u["warned"] = False
    db.save()
    return u["plan"]


def trial_eligible(uid: int) -> bool:
    """One-time trial: only users who never had a trial or any plan."""
    if not TRIAL_ENABLED or uid in ADMINS:
        return False
    u = db.user(uid)
    if not u or u.get("trial") or u.get("ever_plan") or u.get("plan") or u.get("banned"):
        return False
    return not any(p["status"] == "approved" for p in db.user_payments(uid))


def start_trial(uid: int) -> dict:
    now = time.time()
    t = {"started": now, "expires": now + TRIAL_HOURS * 3600, "warned": False, "done": False}
    db.stub_user(uid)["trial"] = t
    db.save()
    return t


def dir_size(path: Path, skip_venv: bool) -> int:
    total = 0
    for root, dirs, files in os.walk(path):
        if skip_venv and "venv" in dirs:
            dirs.remove("venv")
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def used_bytes(uid: int) -> int:
    d = PROJECTS / str(uid)
    return dir_size(d, skip_venv=not COUNT_VENV_IN_QUOTA) if d.exists() else 0


# ═════════════════════════ Process manager ═════════════════════════
@dataclass
class Running:
    proc: subprocess.Popen
    started: float


procs: dict[tuple[int, str], Running] = {}


def pdir(uid: int, name: str) -> Path:
    return PROJECTS / str(uid) / name


def is_running(uid: int, name: str) -> bool:
    r = procs.get((uid, name))
    return bool(r and r.proc.poll() is None)


def venv_python(uid: int, name: str) -> str:
    py = pdir(uid, name) / "venv" / "bin" / "python"
    return str(py) if py.exists() else sys.executable


def _limits():
    try:
        if MEM_LIMIT_MB:
            b = MEM_LIMIT_MB * MB
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
    except Exception:
        pass


async def start_project(uid: int, name: str) -> tuple[bool, str]:
    if not get_plan(uid):
        return False, "💎 No active plan. Buy a plan to run bots."
    u = db.user(uid)
    if u and u.get("banned"):
        return False, "🚫 Banned."
    if is_running(uid, name):
        return False, "Already running."
    p = db.get(uid, name)
    if not p:
        return False, "Project not found."
    d = pdir(uid, name)
    if not (d / p["entry"]).exists():
        return False, "Entry file missing."

    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TZ") if k in os.environ}
    env.update({str(k): str(v) for k, v in p.get("env", {}).items()})
    env["PYTHONUNBUFFERED"] = "1"          # host token is NOT passed on

    logf = open(d / "run.log", "ab")
    logf.write(f"\n===== started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
    try:
        proc = subprocess.Popen(
            [venv_python(uid, name), "-u", p["entry"]], cwd=d,
            stdout=logf, stderr=subprocess.STDOUT, env=env,
            start_new_session=True, preexec_fn=_limits)
    except Exception as e:
        return False, f"Failed to launch: {e}"
    finally:
        logf.close()
    procs[(uid, name)] = Running(proc, time.time())
    return True, "Started."


async def stop_project(uid: int, name: str) -> bool:
    r = procs.get((uid, name))
    if not r or r.proc.poll() is not None:
        return False
    loop = asyncio.get_running_loop()
    try:
        os.killpg(r.proc.pid, signal.SIGTERM)
        await loop.run_in_executor(None, lambda: r.proc.wait(timeout=6))
    except subprocess.TimeoutExpired:
        os.killpg(r.proc.pid, signal.SIGKILL)
        await loop.run_in_executor(None, r.proc.wait)
    except ProcessLookupError:
        pass
    return True


async def stop_all(uid: int) -> int:
    n = 0
    for (u, name) in list(procs):
        if u == uid and await stop_project(u, name):
            n += 1
    return n


async def delete_all_projects(uid: int):
    """Stop everything a user runs and wipe all their files."""
    project_count = len(db.data["projects"].get(str(uid), {}))
    await stop_all(uid)
    project_dir = PROJECTS / str(uid)
    if project_dir.exists():
        await asyncio.to_thread(shutil.rmtree, project_dir)
    for k in [k for k in procs if k[0] == uid]:
        procs.pop(k, None)
    db.data["projects"][str(uid)] = {}
    db.save()
    return project_count


def hosted_user_ids() -> set[int]:
    """Return user IDs with registered projects or project directories."""
    user_ids = set()
    for raw_uid, projects in db.data["projects"].items():
        if str(raw_uid).isdigit() and isinstance(projects, dict) and projects:
            user_ids.add(int(raw_uid))
    for path in PROJECTS.iterdir():
        if path.is_dir() and path.name.isdigit():
            user_ids.add(int(path.name))
    return user_ids


REQ_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
    r"(\s*(===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+"
    r"(\s*,\s*(===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+)*)?"
    r"(\s*;\s*[A-Za-z0-9_.\"' <>=!~()and or not in]+)?$"
)


def validate_requirements(text: str) -> tuple[list[str], list[str]]:
    good, bad = [], []
    for raw in text.splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        (good if REQ_LINE.match(line) else bad).append(line)
    return good, bad


async def install_requirements(uid: int, name: str) -> tuple[bool, str]:
    d = pdir(uid, name)
    venv = d / "venv"
    try:
        if not venv.exists():
            p = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "venv", str(venv),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await p.communicate()
            if p.returncode != 0:
                return False, out.decode(errors="replace")
        p = await asyncio.create_subprocess_exec(
            str(venv / "bin" / "python"), "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "-r", "requirements.txt",
            cwd=d, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=PIP_TIMEOUT)
        text = out.decode(errors="replace")
        (d / "pip.log").write_text(text)
        return p.returncode == 0, text
    except asyncio.TimeoutError:
        return False, "pip timed out."
    except Exception as e:
        return False, str(e)


# ═════════════════════════ Telegram helpers ═════════════════════════
async def edit(q, text: str, kb: Kb | None = None):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def notify(bot, uid: int, text: str, kb: Kb | None = None) -> bool:
    try:
        await bot.send_message(uid, text, parse_mode=HTML, reply_markup=kb)
        return True
    except Forbidden:
        u = db.user(uid)
        if u:
            u["blocked"] = True
            db.save()
    except TelegramError as e:
        log.warning("notify %s failed: %s", uid, e)
    return False


def registered(fn):
    """Register the user, block banned users."""
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        tg = update.effective_user
        if not tg or tg.is_bot:
            return
        rec = db.ensure_user(tg)
        if rec.get("banned") and tg.id not in ADMINS:
            if update.callback_query:
                await update.callback_query.answer("🚫 You are banned.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("🚫 <b>You are banned</b> from this bot.", parse_mode=HTML)
            return
        if rec.get("blocked"):
            rec["blocked"] = False
        return await fn(update, ctx)
    return wrapper


def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMINS:
            if update.callback_query:
                await update.callback_query.answer("Admins only", show_alert=True)
            else:
                await update.effective_message.reply_text("⛔ Admins only.")
            return
        return await fn(update, ctx)
    return wrapper


# ═════════════════════════ User UI ═════════════════════════
GUIDE = (
    "<b>📖 Guide</b>\n" + LINE + "\n"
    "<b>1. Buy a plan</b>\nOpen 💎 <b>Plans</b>, pick a size, pay via UPI and send the screenshot. "
    "An admin approves it and your hosting is activated.\n\n"
    "<b>2. Upload a bot</b>\nSend any <code>.py</code> file. The file name becomes the project name.\n\n"
    "<b>3. Packages</b>\nSend a <code>requirements.txt</code> file. Only plain specs "
    "(e.g. <code>aiogram==3.4.1</code>) are accepted.\n\n"
    "<b>4. Env variables</b>\n<code>/setenv project KEY value</code>\n"
    "<code>/setenv project KEY</code> → removes it\n\n"
    "<i>Your storage plan limits the total size of your uploaded files and logs.</i>\n\n"
    f"<b>🆓 Free trial</b>\nNew users can test one bot for {TRIAL_HOURS} hours. "
    "After that it is stopped and deleted automatically."
)


def home_text(user) -> str:
    uid = user.id
    n = len(db.projects(uid))
    run = sum(1 for k in db.projects(uid) if is_running(uid, k))
    plan = get_plan(uid)
    if plan and plan["source"] == "trial":
        pl = f"🆓 Free trial: <b>{fmt_left(plan['expires'])}</b>\n      <i>(bot is deleted when it ends)</i>"
    elif plan:
        pl = f"💎 Plan: <b>{mb_label(plan['mb'])}</b> · {fmt_left(plan['expires'])}"
    else:
        pl = "💎 Plan: <b>none</b> — buy one to start hosting"
    return (
        f"<b>🚀 BotHost</b>\n{LINE}\n"
        f"Hi <b>{escape(user.first_name)}</b>! Host your Telegram bots 24/7.\n\n"
        f"{pl}\n📦 Projects: <b>{n}</b>   🟢 Running: <b>{run}</b>\n{LINE}\n"
        "1️⃣ Buy a hosting plan\n2️⃣ Send your <code>.py</code> file\n"
        "3️⃣ Send <code>requirements.txt</code> <i>(optional)</i>\n4️⃣ Tap <b>▶️ Start</b>"
    )


def home_kb(uid: int) -> Kb:
    rows = [
        [Btn("📂 My Projects", callback_data="list"), Btn("💎 Plans", callback_data="plans")],
        [Btn("📖 Guide", callback_data="guide"), Btn("🔄 Refresh", callback_data="home")],
    ]
    if trial_eligible(uid):
        rows.insert(0, [Btn(f"🆓 Start {TRIAL_HOURS}h Free Trial", callback_data="trial")])
    if uid in ADMINS:
        rows.append([Btn("👑 Admin Panel", callback_data="adm:home")])
    return Kb(rows)


def need_plan_kb(uid: int | None = None) -> Kb:
    rows = []
    if uid and trial_eligible(uid):
        rows.append([Btn(f"🆓 Free {TRIAL_HOURS}h Trial", callback_data="trial")])
    rows.append([Btn("💎 View Plans", callback_data="plans")])
    return Kb(rows)


NEED_PLAN = ("💎 <b>Hosting plan required</b>\n"
             "Buy a plan first, then you can upload and run your bots.")


async def plans_screen(uid: int) -> tuple[str, Kb]:
    plan = get_plan(uid)
    lines = ["<b>💎 Plans & Billing</b>", LINE]
    if plan:
        used = await asyncio.to_thread(used_bytes, uid)
        quota = plan["mb"] * MB
        src = {"paid": "Paid", "free": "Free 🎁", "admin": "Admin 👑", "trial": "Free trial 🆓"}[plan["source"]]
        lines += [f"Current: <b>{mb_label(plan['mb'])}</b> · {src}",
                  f"Validity: {fmt_left(plan['expires'])}",
                  f"Storage: {bar(used, quota)}",
                  f"<code>{fmt_size(used)} / {mb_label(plan['mb'])}</code>"]
    else:
        lines.append("You have no active plan.")
    lines += [LINE, f"<b>Hosting plans</b> · {PLAN_DAYS} days each"]
    lines += [f"▫️ {p['label']} — <b>{money(p['price'])}</b>" for p in PLANS.values()]
    pays = db.user_payments(uid)
    if pays:
        last = pays[-1]
        lines += [LINE, f"Last payment: #{last['id']} {STATUS_ICON[last['status']]} {last['status']}"]
    if trial_eligible(uid):
        lines.append(f"\n🆓 <b>New user?</b> Try hosting free for {TRIAL_HOURS} hours.")
    lines.append("\n<i>Tap a plan → pay via UPI → send screenshot. An admin will verify it.</i>")
    rows = [[Btn(f"{p['label']} — {money(p['price'])} / {PLAN_DAYS}d", callback_data=f"buy:{k}")]
            for k, p in PLANS.items()]
    if trial_eligible(uid):
        rows.insert(0, [Btn(f"🆓 Start {TRIAL_HOURS}h Free Trial", callback_data="trial")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return "\n".join(lines), Kb(rows)


def list_kb(uid: int) -> Kb:
    rows = []
    for name in db.projects(uid):
        icon = "🟢" if is_running(uid, name) else "🔴"
        rows.append([Btn(f"{icon}  {name}", callback_data=f"p:{name}")])
    rows.append([Btn("⬅️ Back", callback_data="home")])
    return Kb(rows)


def card_text(uid: int, name: str) -> str:
    p = db.get(uid, name)
    r = procs.get((uid, name))
    if is_running(uid, name):
        status = f"🟢 <b>Running</b> · {fmt_dur(time.time() - r.started)}"
    elif r:
        status = f"🔴 <b>Stopped</b> · exit code <code>{r.proc.returncode}</code>"
    else:
        status = "🔴 <b>Stopped</b>"
    pk = ("✅ installed" if p["installed"] else "⚠️ not installed") if p["has_reqs"] else "— none"
    return (
        f"<b>🤖 {escape(name)}</b>\n{LINE}\n"
        f"Status:      {status}\n"
        f"File:           <code>{escape(p['entry'])}</code>\n"
        f"Packages: {pk}\n"
        f"Env vars:   <b>{len(p.get('env', {}))}</b>\n"
        f"Auto-start: {'✅ on' if p['autostart'] else '❌ off'}\n{LINE}"
    )


def card_kb(uid: int, name: str) -> Kb:
    p = db.get(uid, name)
    main = [Btn("⏹ Stop", callback_data=f"stop:{name}")] if is_running(uid, name) \
        else [Btn("▶️ Start", callback_data=f"start:{name}")]
    main.append(Btn("🔄 Restart", callback_data=f"restart:{name}"))
    return Kb([
        main,
        [Btn("📜 Logs", callback_data=f"logs:{name}"), Btn("📦 Packages", callback_data=f"pkgs:{name}")],
        [Btn(f"⚡ Auto-start: {'ON' if p['autostart'] else 'OFF'}", callback_data=f"auto:{name}")],
        [Btn("🗑 Delete", callback_data=f"del:{name}"), Btn("⬅️ Back", callback_data="list")],
    ])


def clean_name(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", Path(filename).stem)[:24] or "bot"


# ═════════════════════════ User commands ═════════════════════════
@registered
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(home_text(update.effective_user), parse_mode=HTML,
                                    reply_markup=home_kb(update.effective_user.id))


@registered
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(GUIDE, parse_mode=HTML)


@registered
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("await", None)
    ctx.user_data.pop("bc", None)
    await update.message.reply_text("✖️ Cancelled.")


@registered
async def cmd_setenv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    a = ctx.args
    if len(a) < 2:
        await update.message.reply_text("Usage: <code>/setenv project KEY [value]</code>", parse_mode=HTML)
        return
    name, key, value = a[0], a[1], " ".join(a[2:])
    p = db.get(uid, name)
    if not p:
        await update.message.reply_text("❌ Project not found.")
        return
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
        await update.message.reply_text("❌ Invalid variable name.")
        return
    env = p.get("env", {})
    if value:
        env[key] = value
        msg = f"✅ <code>{escape(key)}</code> saved for <b>{escape(name)}</b>. Restart the bot to apply."
    else:
        env.pop(key, None)
        msg = f"🗑 <code>{escape(key)}</code> removed."
    db.put(uid, name, env=env)
    try:
        await update.message.delete()
    except Exception:
        pass
    await ctx.bot.send_message(update.effective_chat.id, msg, parse_mode=HTML)


# ═════════════════════════ Payments (user side) ═════════════════════════
def payment_caption(p: dict) -> str:
    u = db.user(p["uid"]) or {}
    uname = f" (@{u['username']})" if u.get("username") else ""
    return (
        f"💳 <b>Payment request #{p['id']}</b>\n{LINE}\n"
        f"👤 {escape(u.get('name', '?'))}{uname}\n🆔 <code>{p['uid']}</code>\n"
        f"📦 Plan: <b>{p['label']}</b> · {PLAN_DAYS} days\n"
        f"💰 Amount: <b>{money(p['amount'])}</b>\n"
        f"🧾 UTR/Note: <code>{escape(p['utr']) or '—'}</code>\n"
        f"🕒 {time.strftime('%d %b %H:%M', time.localtime(p.get('submitted', p['created'])))}"
    )


def pay_kb(pid: int) -> Kb:
    return Kb([[Btn("✅ Approve", callback_data=f"pay_ok:{pid}"),
                Btn("❌ Reject", callback_data=f"pay_no:{pid}")]])


async def send_payment(bot, chat_id: int, p: dict):
    if p["ftype"] == "photo":
        return await bot.send_photo(chat_id, p["file_id"], caption=payment_caption(p),
                                    parse_mode=HTML, reply_markup=pay_kb(p["id"]))
    return await bot.send_document(chat_id, p["file_id"], caption=payment_caption(p),
                                   parse_mode=HTML, reply_markup=pay_kb(p["id"]))


async def notify_admins_payment(bot, p: dict):
    for aid in ADMINS:
        try:
            m = await send_payment(bot, aid, p)
            p["msgs"].append([aid, m.message_id])
        except TelegramError as e:
            log.warning("Could not notify admin %s: %s", aid, e)
    db.save()


def find_payment(uid: int, status: str) -> dict | None:
    for p in reversed(db.user_payments(uid)):
        if p["status"] == status:
            return p
    return None


async def submit_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE, file_id: str, ftype: str):
    msg, uid = update.message, update.effective_user.id
    p = find_payment(uid, "awaiting")
    if not p:
        if find_payment(uid, "pending"):
            await msg.reply_text("⏳ You already have a payment waiting for admin approval.")
        else:
            await msg.reply_text("ℹ️ Pick a plan first, then send the payment screenshot.",
                                 reply_markup=need_plan_kb())
        return
    p.update(status="pending", file_id=file_id, ftype=ftype, submitted=time.time(),
             utr=(msg.caption or "").strip()[:60])
    db.save()
    await msg.reply_text(
        f"✅ <b>Request #{p['id']} submitted!</b>\nAn admin will verify your payment shortly. "
        "You'll get a message here as soon as it's approved.", parse_mode=HTML)
    await notify_admins_payment(ctx.bot, p)


# ═════════════════════════ File uploads ═════════════════════════
@registered
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, uid = update.message, update.effective_user.id
    doc = msg.document
    lower = (doc.file_name or "").lower()

    if uid in ADMINS and ctx.user_data.get("await") == "broadcast":
        return await capture_broadcast(update, ctx)

    # payment screenshot sent as a file
    if (doc.mime_type or "").startswith("image/"):
        return await submit_proof(update, ctx, doc.file_id, "document")

    # ---------- .py ----------
    if lower.endswith(".py"):
        plan = get_plan(uid)
        if not plan:
            return await msg.reply_text(NEED_PLAN, parse_mode=HTML, reply_markup=need_plan_kb(uid))
        if doc.file_size > MAX_PY_SIZE:
            return await msg.reply_text("❌ File too large (max 1 MB per file).")
        name = clean_name(doc.file_name)
        limit = TRIAL_MAX_PROJECTS if plan["source"] == "trial" else MAX_PROJECTS_PER_USER
        if not db.get(uid, name) and len(db.projects(uid)) >= limit:
            extra = "\n💎 Buy a plan to host more projects." if plan["source"] == "trial" else ""
            return await msg.reply_text(f"❌ Project limit reached ({limit}).{extra}", reply_markup=need_plan_kb())
        d, entry = pdir(uid, name), f"{name}.py"
        old = (d / entry).stat().st_size if (d / entry).exists() else 0
        used = await asyncio.to_thread(used_bytes, uid)
        quota = plan["mb"] * MB
        if used - old + doc.file_size > quota:
            return await msg.reply_text(
                f"💾 <b>Storage full</b>\n{bar(used, quota)}\n<code>{fmt_size(used)} / {mb_label(plan['mb'])}</code>\n"
                "Delete a project or upgrade your plan.", parse_mode=HTML, reply_markup=need_plan_kb())
        existed = db.get(uid, name) is not None
        d.mkdir(parents=True, exist_ok=True)
        await (await doc.get_file()).download_to_drive(d / entry)
        db.put(uid, name, entry=entry)
        note = "♻️ <b>Updated.</b> Restart to apply." if existed and is_running(uid, name) \
            else ("♻️ <b>File replaced.</b>" if existed else "✅ <b>Uploaded!</b>")
        tn = (f"\n🆓 <i>Trial ends in {fmt_left(plan['expires'])} — the bot will be stopped and deleted then.</i>"
              if plan["source"] == "trial" else "")
        return await msg.reply_text(
            f"{note}\n\n{card_text(uid, name)}\n<i>Need packages? Send a requirements.txt file.</i>{tn}",
            parse_mode=HTML, reply_markup=card_kb(uid, name))

    # ---------- requirements.txt ----------
    if lower.endswith(".txt") and "requirements" in lower:
        if not get_plan(uid):
            return await msg.reply_text(NEED_PLAN, parse_mode=HTML, reply_markup=need_plan_kb(uid))
        if doc.file_size > MAX_REQ_SIZE:
            return await msg.reply_text("❌ requirements file too large (max 100 KB).")
        if not db.projects(uid):
            return await msg.reply_text("📭 Upload a <code>.py</code> file first.", parse_mode=HTML)
        tmp = TMP / f"{uid}_{int(time.time())}.txt"
        await (await doc.get_file()).download_to_drive(tmp)
        try:
            text = tmp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            tmp.unlink(missing_ok=True)
            return await msg.reply_text("❌ File must be UTF-8 text.")
        good, bad = validate_requirements(text)
        if bad:
            tmp.unlink(missing_ok=True)
            shown = "\n".join(escape(b[:60]) for b in bad[:8])
            return await msg.reply_text(
                "❌ <b>Rejected lines</b>\n<pre>" + shown + "</pre>\nOnly plain package specs are allowed — "
                "no <code>-r</code>, <code>--index-url</code>, URLs, git links or paths.", parse_mode=HTML)
        if not good:
            tmp.unlink(missing_ok=True)
            return await msg.reply_text("❌ No packages found in that file.")
        tmp.write_text("\n".join(good) + "\n")

        target, names = (msg.caption or "").strip(), list(db.projects(uid))
        if target in names:
            return await apply_requirements(msg, uid, target, tmp)
        if len(names) == 1:
            return await apply_requirements(msg, uid, names[0], tmp)
        ctx.user_data["pending_req"] = str(tmp)
        rows = [[Btn(f"📦 {n}", callback_data=f"rq:{n}")] for n in names]
        rows.append([Btn("✖️ Cancel", callback_data="rqx")])
        return await msg.reply_text(
            f"📄 <b>{len(good)} package(s)</b> ready:\n<code>{escape(', '.join(good)[:300])}</code>\n\n"
            "Which project should get them?", parse_mode=HTML, reply_markup=Kb(rows))

    await msg.reply_text("🤔 Send a <code>.py</code> file or a <code>requirements.txt</code> file.", parse_mode=HTML)


async def apply_requirements(msg, uid: int, name: str, tmp: Path):
    shutil.move(str(tmp), pdir(uid, name) / "requirements.txt")
    db.put(uid, name, has_reqs=True, installed=False)
    status = await msg.reply_text(f"⏳ Installing packages for <b>{escape(name)}</b>…\n"
                                  "<i>This can take a minute.</i>", parse_mode=HTML)
    ok, out = await install_requirements(uid, name)
    db.put(uid, name, installed=ok)
    head = "✅ <b>Packages installed!</b>" if ok else "❌ <b>Installation failed</b>"
    await status.edit_text(f"{head}\n<pre>{escape(out.strip()[-700:])}</pre>\n\n{card_text(uid, name)}",
                           parse_mode=HTML, reply_markup=card_kb(uid, name))


@registered
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Photos (payment proof) + admin text/broadcast input."""
    msg, uid = update.message, update.effective_user.id
    state = ctx.user_data.get("await") if uid in ADMINS else None

    if state == "broadcast":
        return await capture_broadcast(update, ctx)
    if state == "find":
        ctx.user_data.pop("await", None)
        text = (msg.text or "").strip().lstrip("@")
        target = None
        if text.isdigit():
            target = int(text)
        else:
            for k, u in db.data["users"].items():
                if u.get("username", "").lower() == text.lower() and text:
                    target = int(k)
        if target is None or not db.user(target):
            return await msg.reply_text("❌ User not found. They must have started the bot "
                                        "(or use <code>/free ID</code>).", parse_mode=HTML)
        t, kb = await user_card(target)
        return await msg.reply_text(t, parse_mode=HTML, reply_markup=kb)

    if msg.photo:
        return await submit_proof(update, ctx, msg.photo[-1].file_id, "photo")
    if msg.text and find_payment(uid, "awaiting"):
        return await msg.reply_text("📸 Please send the <b>payment screenshot</b> (add your UTR as the caption).",
                                    parse_mode=HTML)
    if msg.text:
        await msg.reply_text("Use /start to open the menu.")


# ═════════════════════════ User callbacks ═════════════════════════
@registered
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    action, _, name = q.data.partition(":")

    if action not in ("start", "restart"):
        await q.answer()

    if action == "home":
        return await edit(q, home_text(update.effective_user), home_kb(uid))
    if action == "guide":
        return await edit(q, GUIDE, Kb([[Btn("⬅️ Back", callback_data="home")]]))
    if action == "plans":
        t, kb = await plans_screen(uid)
        return await edit(q, t, kb)
    if action == "list":
        if not db.projects(uid):
            return await edit(q, f"<b>📂 My Projects</b>\n{LINE}\nNothing here yet.\nSend me a <code>.py</code> file!",
                              Kb([[Btn("⬅️ Back", callback_data="home")]]))
        return await edit(q, f"<b>📂 My Projects</b>\n{LINE}\nSelect one:", list_kb(uid))

    # ── free trial
    if action == "trial":
        if not trial_eligible(uid):
            return await edit(q, "ℹ️ The free trial is a one-time offer for new users.",
                              Kb([[Btn("💎 View Plans", callback_data="plans")]]))
        text = (f"<b>🆓 Free Trial</b>\n{LINE}\n"
                f"Test your bot on our servers for <b>{TRIAL_HOURS} hours</b> — no payment needed.\n\n"
                f"▫️ {TRIAL_MB} MB storage · {TRIAL_MAX_PROJECTS} project\n▫️ One-time offer\n\n"
                f"⚠️ When the {TRIAL_HOURS} hours end, your bot is <b>stopped and deleted automatically</b>. "
                "You'll get a notification.\nBuy a plan any time to keep it online permanently.")
        return await edit(q, text, Kb([[Btn("✅ Activate trial", callback_data="trialgo")],
                                       [Btn("⬅️ Back", callback_data="home")]]))
    if action == "trialgo":
        if not trial_eligible(uid):
            return await edit(q, "ℹ️ The free trial is a one-time offer for new users.",
                              Kb([[Btn("💎 View Plans", callback_data="plans")]]))
        t = start_trial(uid)
        return await edit(q, f"🎉 <b>Trial activated!</b>\n{LINE}\n"
                             f"⏳ Valid until <b>{time.strftime('%d %b, %H:%M', time.localtime(t['expires']))}</b> "
                             f"({TRIAL_HOURS}h)\n\nNow send your bot's <code>.py</code> file to host it.",
                          Kb([[Btn("📂 My Projects", callback_data="list")]]))

    # ── buying
    if action == "buy":
        if name not in PLANS:
            return
        if find_payment(uid, "pending"):
            return await edit(q, "⏳ <b>You already have a payment pending approval.</b>\n"
                                 "Please wait for the admin to review it.",
                              Kb([[Btn("⬅️ Back", callback_data="plans")]]))
        for old in db.user_payments(uid):
            if old["status"] == "awaiting":
                old["status"] = "cancelled"
        p = db.new_payment(uid, name)
        text = (
            f"💳 <b>Payment #{p['id']}</b>\n{LINE}\n"
            f"Plan: <b>{p['label']}</b> · {PLAN_DAYS} days\nAmount: <b>{money(p['amount'])}</b>\n\n"
            f"Pay to UPI ID:\n<code>{escape(UPI_ID)}</code>\nName: {escape(UPI_NAME)}\n{LINE}\n"
            "1️⃣ Pay the <b>exact amount</b>\n2️⃣ Send the <b>screenshot</b> here as a photo\n"
            "    <i>(write your UTR/transaction ID in the caption)</i>\n\n"
            "An admin verifies it and activates your plan.")
        await edit(q, text, Kb([[Btn("✖️ Cancel payment", callback_data=f"paycancel:{p['id']}")]]))
        if UPI_QR_PATH.exists():
            with UPI_QR_PATH.open("rb") as f:
                await ctx.bot.send_photo(q.message.chat_id, f, caption=f"📲 Scan to pay <b>{money(p['amount'])}</b>",
                                         parse_mode=HTML)
        return
    if action == "paycancel":
        p = db.payment(int(name)) if name.isdigit() else None
        if p and p["uid"] == uid and p["status"] == "awaiting":
            p["status"] = "cancelled"
            db.save()
        t, kb = await plans_screen(uid)
        return await edit(q, t, kb)

    # ── requirements picker
    if action == "rqx":
        p = ctx.user_data.pop("pending_req", None)
        if p:
            Path(p).unlink(missing_ok=True)
        return await edit(q, "✖️ Cancelled.")
    if action == "rq":
        tmp = ctx.user_data.pop("pending_req", None)
        if not tmp or not Path(tmp).exists() or not db.get(uid, name):
            return await edit(q, "⌛ Request expired. Send the file again.")
        shutil.move(tmp, pdir(uid, name) / "requirements.txt")
        db.put(uid, name, has_reqs=True, installed=False)
        await edit(q, f"⏳ Installing packages for <b>{escape(name)}</b>…\n<i>This can take a minute.</i>")
        ok, out = await install_requirements(uid, name)
        db.put(uid, name, installed=ok)
        head = "✅ <b>Packages installed!</b>" if ok else "❌ <b>Installation failed</b>"
        return await edit(q, f"{head}\n<pre>{escape(out.strip()[-700:])}</pre>\n\n{card_text(uid, name)}",
                          card_kb(uid, name))

    # ── project actions
    if not db.get(uid, name):
        if action in ("start", "restart"):
            await q.answer()
        return await edit(q, "❓ Project no longer exists.", Kb([[Btn("⬅️ Back", callback_data="list")]]))

    if action == "p":
        return await edit(q, card_text(uid, name), card_kb(uid, name))

    if action in ("start", "restart"):
        if action == "restart":
            await stop_project(uid, name)
        ok, m = await start_project(uid, name)
        await q.answer(None if ok else m, show_alert=not ok)
        await asyncio.sleep(1.2)
        return await edit(q, card_text(uid, name), card_kb(uid, name))

    if action == "stop":
        await stop_project(uid, name)
        return await edit(q, card_text(uid, name), card_kb(uid, name))

    if action == "auto":
        db.put(uid, name, autostart=not db.get(uid, name)["autostart"])
        return await edit(q, card_text(uid, name), card_kb(uid, name))

    if action == "logs":
        lf = pdir(uid, name) / "run.log"
        text = lf.read_bytes()[-LOG_TAIL_CHARS:].decode(errors="replace") if lf.exists() else "(no logs yet)"
        kb = Kb([[Btn("🔄 Refresh", callback_data=f"logs:{name}"), Btn("📥 Full log", callback_data=f"dl:{name}")],
                 [Btn("⬅️ Back", callback_data=f"p:{name}")]])
        return await edit(q, f"<b>📜 Logs · {escape(name)}</b>\n<pre>{escape(text.strip()[-LOG_TAIL_CHARS:] or '(empty)')}</pre>", kb)

    if action == "dl":
        lf = pdir(uid, name) / "run.log"
        if lf.exists() and lf.stat().st_size:
            await ctx.bot.send_document(q.message.chat_id, lf.open("rb"), filename=f"{name}.log")
        return

    if action == "pkgs":
        rf = pdir(uid, name) / "requirements.txt"
        pk = escape(rf.read_text()) if rf.exists() else "No packages yet."
        rows = []
        if rf.exists():
            rows.append([Btn("🔁 Reinstall", callback_data=f"reinst:{name}")])
        rows.append([Btn("⬅️ Back", callback_data=f"p:{name}")])
        return await edit(q, f"<b>📦 Packages · {escape(name)}</b>\n<pre>{pk}</pre>\n"
                             "<i>Send a new requirements.txt to replace them.</i>", Kb(rows))

    if action == "reinst":
        await edit(q, f"⏳ Reinstalling packages for <b>{escape(name)}</b>…")
        ok, out = await install_requirements(uid, name)
        db.put(uid, name, installed=ok)
        head = "✅ <b>Done!</b>" if ok else "❌ <b>Failed</b>"
        return await edit(q, f"{head}\n<pre>{escape(out.strip()[-700:])}</pre>", card_kb(uid, name))

    if action == "del":
        kb = Kb([[Btn("✅ Yes, delete", callback_data=f"delok:{name}"), Btn("↩️ No", callback_data=f"p:{name}")]])
        return await edit(q, f"⚠️ Delete <b>{escape(name)}</b> and all its files?", kb)

    if action == "delok":
        await stop_project(uid, name)
        procs.pop((uid, name), None)
        shutil.rmtree(pdir(uid, name), ignore_errors=True)
        db.remove(uid, name)
        return await edit(q, f"🗑 <b>{escape(name)}</b> deleted.", Kb([[Btn("📂 Projects", callback_data="list")]]))


# ═════════════════════════ Admin: payments ═════════════════════════
async def decide_payment(ctx, q, pid: int, approve: bool):
    p = db.payment(pid)
    if not p:
        return await q.answer("Payment not found", show_alert=True)
    if p["status"] != "pending":
        await q.answer(f"Already {p['status']}", show_alert=True)
        try:
            await q.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return
    await q.answer("Approved ✅" if approve else "Rejected ❌")
    p.update(status="approved" if approve else "rejected", by=q.from_user.id, decided=time.time())
    plan = activate_paid(p["uid"], p["key"]) if approve else None
    db.save()

    stamp = f"\n\n{'✅ <b>Approved</b>' if approve else '❌ <b>Rejected</b>'} by {escape(q.from_user.first_name)}"
    for chat_id, mid in p["msgs"]:
        try:
            await ctx.bot.edit_message_caption(chat_id, mid, caption=payment_caption(p) + stamp,
                                                parse_mode=HTML, reply_markup=None)
        except TelegramError:
            pass

    if approve:
        await notify(ctx.bot, p["uid"],
                     f"🎉 <b>Payment approved!</b>\n{LINE}\n💎 Plan: <b>{p['label']}</b>\n"
                     f"📅 Valid until: <b>{fmt_date(plan['expires'])}</b>\n\n"
                     "Send your <code>.py</code> file to start hosting.",
                     Kb([[Btn("📂 My Projects", callback_data="list")]]))
    else:
        await notify(ctx.bot, p["uid"],
                     f"❌ <b>Payment #{p['id']} was rejected.</b>\nIf this is a mistake, contact {escape(SUPPORT_CONTACT)} "
                     "or try again from Plans.", need_plan_kb())


# ═════════════════════════ Admin: panel builders ═════════════════════════
def pending_payments() -> list[dict]:
    return [p for p in db.data["payments"].values() if p["status"] == "pending"]


async def admin_home() -> tuple[str, Kb]:
    pend = len(pending_payments())
    running = sum(1 for k in procs if is_running(*k))
    text = (f"<b>👑 Admin Panel</b>\n{LINE}\n"
            f"👥 Users: <b>{len(db.data['users'])}</b>\n"
            f"🟢 Running bots: <b>{running}</b>\n"
            f"💳 Pending payments: <b>{pend}</b>\n{LINE}\nChoose an option:")
    kb = Kb([
        [Btn("📊 Stats", callback_data="adm:stats"), Btn(f"💳 Payments ({pend})", callback_data="adm:pays")],
        [Btn("👥 Users", callback_data="adm:users:0"), Btn("🔎 Find user", callback_data="adm:find")],
        [Btn("📢 Broadcast", callback_data="adm:bc"), Btn("🤖 Running bots", callback_data="adm:run")],
        [Btn("🗑 Delete hosted bots", callback_data="adm:delall")],
        [Btn("⬅️ Back", callback_data="home")],
    ])
    return text, kb


async def stats_text() -> str:
    users, pays, now = db.data["users"], db.data["payments"].values(), time.time()
    today, month = day_start(), month_start()
    paid = free = trial_act = 0
    for u in users.values():
        t = u.get("trial")
        if t and not t.get("done") and t["expires"] > now:
            trial_act += 1
        p = u.get("plan")
        if p and (not p.get("expires") or p["expires"] > now):
            paid += p["source"] == "paid"
            free += p["source"] == "free"
    approved = [p for p in pays if p["status"] == "approved"]
    rev = lambda since: sum(p["amount"] for p in approved if p.get("decided", 0) >= since)
    projects = sum(len(v) for v in db.data["projects"].values())
    running = sum(1 for k in procs if is_running(*k))
    files = await asyncio.to_thread(dir_size, PROJECTS, True)
    full = await asyncio.to_thread(dir_size, PROJECTS, False)
    return (
        f"<b>📊 Statistics</b>\n{LINE}\n"
        f"👥 Users: <b>{len(users)}</b> (+{sum(1 for u in users.values() if u.get('joined', 0) >= today)} today)\n"
        f"🚫 Banned: <b>{sum(1 for u in users.values() if u.get('banned'))}</b>\n"
        f"💎 Paid active: <b>{paid}</b>\n🎁 Free active: <b>{free}</b>\n"
        f"🆓 Trial active: <b>{trial_act}</b> (claimed: {sum(1 for u in users.values() if u.get('trial'))})\n{LINE}\n"
        f"📦 Projects: <b>{projects}</b>\n🟢 Running: <b>{running}</b>\n"
        f"💾 Files: <b>{fmt_size(files)}</b>\n🐍 With packages: <b>{fmt_size(full)}</b>\n{LINE}\n"
        f"💳 Pending: <b>{len(pending_payments())}</b>\n"
        f"✅ Approved: <b>{len(approved)}</b>  ❌ Rejected: <b>{sum(1 for p in pays if p['status'] == 'rejected')}</b>\n"
        f"💰 Revenue\n    Today: <b>{money(rev(today))}</b>\n    This month: <b>{money(rev(month))}</b>\n"
        f"    All time: <b>{money(rev(0))}</b>"
    )


async def user_card(uid: int) -> tuple[str, Kb]:
    u = db.user(uid) or db.stub_user(uid)
    plan = get_plan(uid)
    used = await asyncio.to_thread(used_bytes, uid)
    projs = db.projects(uid)
    run = sum(1 for n in projs if is_running(uid, n))
    spent = sum(p["amount"] for p in db.user_payments(uid) if p["status"] == "approved")
    if plan:
        src = {"paid": "paid", "free": "free 🎁", "admin": "admin 👑", "trial": "trial 🆓"}[plan["source"]]
        pl = f"<b>{mb_label(plan['mb'])}</b> · {src} · {fmt_left(plan['expires'])}"
    else:
        pl = "none"
    uname = f"@{u['username']}" if u.get("username") else "—"
    banned = u.get("banned")
    text = (
        f"<b>👤 {escape(u.get('name', '?'))}</b>\n{LINE}\n"
        f"ID: <code>{uid}</code>\nUsername: {escape(uname)}\n"
        f"Joined: {fmt_date(u.get('joined', time.time()))}\n"
        f"Status: {'🚫 Banned' if banned else '🟢 Active'}{' · 📵 blocked bot' if u.get('blocked') else ''}\n{LINE}\n"
        f"💎 Plan: {pl}\n💾 Storage: <code>{fmt_size(used)}</code>\n"
        f"📦 Projects: <b>{len(projs)}</b> · 🟢 Running: <b>{run}</b>\n💰 Paid total: <b>{money(spent)}</b>"
    )
    rows = [
        [Btn("🎁 Free access", callback_data=f"adm:free:{uid}"), Btn("🗑 Revoke plan", callback_data=f"adm:rev:{uid}")],
        [Btn("✅ Unban" if banned else "🚫 Ban", callback_data=f"adm:{'unban' if banned else 'ban'}:{uid}"),
         Btn("⏹ Stop all bots", callback_data=f"adm:stopall:{uid}")],
        [Btn("🗑 Delete hosted bots", callback_data=f"adm:deluser:{uid}")],
        [Btn("⬅️ Users", callback_data="adm:users:0"), Btn("🏠 Panel", callback_data="adm:home")],
    ]
    return text, Kb(rows)


def users_page(page: int) -> tuple[str, Kb]:
    items = sorted(db.data["users"].items(), key=lambda kv: kv[1].get("joined", 0), reverse=True)
    pages = max(1, (len(items) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]
    rows = []
    for k, u in chunk:
        icon = "🚫" if u.get("banned") else ("💎" if get_plan(int(k)) else "👤")
        rows.append([Btn(f"{icon} {u.get('name', '?')[:20]} · {k}", callback_data=f"adm:u:{k}")])
    nav = []
    if page > 0:
        nav.append(Btn("◀️", callback_data=f"adm:users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page < pages - 1:
        nav.append(Btn("▶️", callback_data=f"adm:users:{page + 1}"))
    rows += [nav, [Btn("🏠 Panel", callback_data="adm:home")]]
    return f"<b>👥 Users</b> ({len(items)})\n{LINE}\nNewest first:", Kb(rows)


def running_screen() -> tuple[str, Kb]:
    live = [(u, n) for (u, n) in procs if is_running(u, n)]
    rows = []
    for u, n in live[:25]:
        owner = (db.user(u) or {}).get("name", "?")[:12]
        rows.append([Btn(f"⏹ {n} · {owner} ({u})", callback_data=f"adm:rs:{u}:{n}")])
    rows.append([Btn("🔄 Refresh", callback_data="adm:run"), Btn("🏠 Panel", callback_data="adm:home")])
    head = f"<b>🤖 Running bots</b> ({len(live)})\n{LINE}\n"
    return head + ("Tap one to stop it:" if live else "Nothing is running."), Kb(rows)


# ═════════════════════════ Admin: actions ═════════════════════════
async def do_ban(bot, uid: int, ban: bool) -> bool:
    if uid in ADMINS:
        return False
    db.stub_user(uid)["banned"] = ban
    db.save()
    if ban:
        await stop_all(uid)
        await notify(bot, uid, "🚫 <b>You have been banned</b> from this bot.")
    else:
        await notify(bot, uid, "✅ You have been unbanned. Welcome back! Use /start.")
    return True


async def do_revoke(bot, uid: int):
    db.stub_user(uid)["plan"] = None
    db.save()
    await stop_all(uid)
    await notify(bot, uid, "⚠️ Your hosting plan was removed by an admin. Your bots were stopped.",
                 need_plan_kb())


async def do_grant(bot, uid: int, mb: int, days: int):
    plan = grant_free(uid, mb, days)
    await notify(bot, uid,
                 f"🎁 <b>You received free hosting!</b>\n{LINE}\n💎 Storage: <b>{mb_label(mb)}</b>\n"
                 f"📅 Validity: <b>{fmt_left(plan['expires'])}</b>\n\nSend your <code>.py</code> file to begin.",
                 Kb([[Btn("📂 My Projects", callback_data="list")]]))


def count_targets() -> list[int]:
    return [int(k) for k, u in db.data["users"].items() if not u.get("banned") and not u.get("blocked")]


@admin_only
async def capture_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    ctx.user_data.pop("await", None)
    ctx.user_data["bc"] = (msg.chat_id, msg.message_id)
    kb = Kb([[Btn(f"🚀 Send to {len(count_targets())} users", callback_data="adm:bcgo")],
             [Btn("✖️ Cancel", callback_data="adm:bcx")]])
    await msg.reply_text("👆 <b>This message will be broadcast.</b>\nConfirm to send it to all users.",
                         parse_mode=HTML, reply_markup=kb)


async def run_broadcast(bot, admin_chat: int, status_mid: int, src_chat: int, src_mid: int):
    targets = count_targets()
    sent = failed = 0
    for i, uid in enumerate(targets, 1):
        try:
            await bot.copy_message(uid, src_chat, src_mid)
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, src_chat, src_mid)
                sent += 1
            except TelegramError:
                failed += 1
        except Forbidden:
            db.data["users"][str(uid)]["blocked"] = True
            failed += 1
        except TelegramError:
            failed += 1
        if i % 20 == 0:
            try:
                await bot.edit_message_text(f"📢 Sending… {i}/{len(targets)}", admin_chat, status_mid)
            except TelegramError:
                pass
        await asyncio.sleep(0.05)
    db.save()
    try:
        await bot.edit_message_text(
            f"📢 <b>Broadcast finished</b>\n{LINE}\n✅ Delivered: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            admin_chat, status_mid, parse_mode=HTML)
    except TelegramError:
        pass


@registered
@admin_only
async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    if parts[0] in ("pay_ok", "pay_no"):
        return await decide_payment(ctx, q, int(parts[1]), parts[0] == "pay_ok")

    await q.answer()
    sub = parts[1] if len(parts) > 1 else "home"
    back = Kb([[Btn("🏠 Panel", callback_data="adm:home")]])

    async def show_user(uid: int):
        t, kb = await user_card(uid)
        await edit(q, t, kb)

    if sub == "noop":
        return
    if sub == "home":
        t, kb = await admin_home()
        return await edit(q, t, kb)
    if sub == "stats":
        return await edit(q, await stats_text(), Kb([[Btn("🔄 Refresh", callback_data="adm:stats"),
                                                      Btn("🏠 Panel", callback_data="adm:home")]]))
    if sub == "pays":
        pend = sorted(pending_payments(), key=lambda p: p["id"])
        rows = [[Btn(f"#{p['id']} · {(db.user(p['uid']) or {}).get('name', '?')[:14]} · {p['label']} · {money(p['amount'])}",
                     callback_data=f"adm:pay:{p['id']}")] for p in pend[:20]]
        hist = [p for p in db.data["payments"].values() if p["status"] in ("approved", "rejected")]
        hist = sorted(hist, key=lambda p: p.get("decided", 0), reverse=True)[:5]
        lines = [f"<b>💳 Payments</b>\n{LINE}", f"⏳ Pending: <b>{len(pend)}</b>" + ("" if pend else " — all clear 🎉")]
        if hist:
            lines.append("\n<b>Recent decisions</b>")
            lines += [f"{STATUS_ICON[p['status']]} #{p['id']} · {escape((db.user(p['uid']) or {}).get('name', '?'))} · "
                      f"{p['label']} · {money(p['amount'])}" for p in hist]
        rows.append([Btn("🏠 Panel", callback_data="adm:home")])
        return await edit(q, "\n".join(lines), Kb(rows))
    if sub == "pay":
        p = db.payment(int(parts[2]))
        if p and p["status"] == "pending":
            m = await send_payment(ctx.bot, q.message.chat_id, p)
            p["msgs"].append([q.message.chat_id, m.message_id])
            db.save()
        return
    if sub == "users":
        t, kb = users_page(int(parts[2]) if len(parts) > 2 else 0)
        return await edit(q, t, kb)
    if sub == "u":
        return await show_user(int(parts[2]))
    if sub in ("ban", "unban"):
        uid = int(parts[2])
        if not await do_ban(ctx.bot, uid, sub == "ban"):
            await q.answer("Can't ban an admin", show_alert=True)
        return await show_user(uid)
    if sub == "rev":
        await do_revoke(ctx.bot, int(parts[2]))
        return await show_user(int(parts[2]))
    if sub == "stopall":
        n = await stop_all(int(parts[2]))
        await q.answer(f"Stopped {n} bot(s)", show_alert=True)
        return await show_user(int(parts[2]))
    if sub == "delall":
        user_ids = hosted_user_ids()
        project_count = sum(len(db.data["projects"].get(str(uid), {})) for uid in user_ids)
        running_count = sum(1 for uid, name in procs if is_running(uid, name))
        rows = [
            [Btn("⚠️ Yes, delete all bots", callback_data="adm:delallok")],
            [Btn("↩️ Cancel", callback_data="adm:home")],
        ]
        return await edit(
            q,
            f"⚠️ <b>Delete every hosted bot?</b>\n{LINE}\n"
            f"Users with hosted files: <b>{len(user_ids)}</b>\n"
            f"Registered projects: <b>{project_count}</b>\n"
            f"Currently running: <b>{running_count}</b>\n\n"
            "This stops bots and permanently deletes their project files, logs, and environments. "
            "User accounts, plans, and payment records are kept.",
            Kb(rows),
        )
    if sub == "delallok":
        deleted = 0
        failures = []
        for uid in sorted(hosted_user_ids()):
            try:
                deleted += await delete_all_projects(uid)
            except Exception:
                log.exception("Admin failed to delete hosted projects for user %s", uid)
                failures.append(uid)
        result = f"🗑 Deleted <b>{deleted}</b> registered project(s) for all users."
        if failures:
            result += f"\n❌ Cleanup failed for <code>{escape(', '.join(map(str, failures)))}</code>."
        return await edit(q, result, Kb([
            [Btn("🏠 Admin panel", callback_data="adm:home")],
        ]))
    if sub == "deluser":
        uid = int(parts[2])
        project_count = len(db.data["projects"].get(str(uid), {}))
        running_count = sum(
            1 for (owner_id, name) in procs
            if owner_id == uid and is_running(owner_id, name)
        )
        return await edit(
            q,
            f"⚠️ <b>Delete all hosted bots for this user?</b>\n{LINE}\n"
            f"User: <code>{uid}</code>\n"
            f"Registered projects: <b>{project_count}</b>\n"
            f"Currently running: <b>{running_count}</b>\n\n"
            "This stops their bots and permanently deletes project files, logs, and environments. "
            "Their account, plan, and payment records are kept.",
            Kb([
                [Btn("⚠️ Yes, delete this user's bots", callback_data=f"adm:deluserok:{uid}")],
                [Btn("↩️ Cancel", callback_data=f"adm:u:{uid}")],
            ]),
        )
    if sub == "deluserok":
        uid = int(parts[2])
        try:
            deleted = await delete_all_projects(uid)
        except Exception:
            log.exception("Admin failed to delete hosted projects for user %s", uid)
            await edit(q, f"❌ Could not delete hosted files for <code>{uid}</code>. "
                           "Check the bot logs for details.",
                       Kb([[Btn("⬅️ User", callback_data=f"adm:u:{uid}")]]))
            return
        return await edit(
            q, f"🗑 Deleted <b>{deleted}</b> hosted bot(s) for <code>{uid}</code>.",
            Kb([[Btn("⬅️ User", callback_data=f"adm:u:{uid}")]]),
        )

    # free access wizard: quota → duration
    if sub == "free":
        uid = parts[2]
        rows = [[Btn(p["label"], callback_data=f"adm:fq:{uid}:{p['mb']}")] for p in PLANS.values()]
        rows.append([Btn("⬅️ Cancel", callback_data=f"adm:u:{uid}")])
        return await edit(q, f"🎁 <b>Free access</b> for <code>{uid}</code>\nChoose storage:", Kb(rows))
    if sub == "fq":
        uid, mb = parts[2], parts[3]
        rows = [[Btn("7 days", callback_data=f"adm:fd:{uid}:{mb}:7"), Btn("30 days", callback_data=f"adm:fd:{uid}:{mb}:30")],
                [Btn("90 days", callback_data=f"adm:fd:{uid}:{mb}:90"), Btn("♾ Lifetime", callback_data=f"adm:fd:{uid}:{mb}:0")],
                [Btn("⬅️ Cancel", callback_data=f"adm:u:{uid}")]]
        return await edit(q, f"🎁 <b>{mb_label(int(mb))}</b> for <code>{uid}</code>\nChoose duration:", Kb(rows))
    if sub == "fd":
        uid, mb, days = int(parts[2]), int(parts[3]), int(parts[4])
        await do_grant(ctx.bot, uid, mb, days)
        return await show_user(uid)

    if sub == "find":
        ctx.user_data["await"] = "find"
        return await edit(q, "🔎 <b>Find user</b>\nSend a Telegram <b>user ID</b> or <b>@username</b>.\n/cancel to abort.", back)

    # broadcast
    if sub == "bc":
        ctx.user_data["await"] = "broadcast"
        return await edit(q, f"📢 <b>Broadcast</b>\n{LINE}\nSend the message you want to broadcast "
                             "(text, photo, video, file… anything).\n/cancel to abort.", back)
    if sub == "bcx":
        ctx.user_data.pop("bc", None)
        return await edit(q, "✖️ Broadcast cancelled.", back)
    if sub == "bcgo":
        src = ctx.user_data.pop("bc", None)
        if not src:
            return await edit(q, "⌛ Nothing to send. Start again.", back)
        await edit(q, "📢 Starting broadcast…")
        t = asyncio.create_task(run_broadcast(ctx.bot, q.message.chat_id, q.message.message_id, *src))
        BG_TASKS.add(t)
        t.add_done_callback(BG_TASKS.discard)
        return

    # running bots
    if sub == "run":
        t, kb = running_screen()
        return await edit(q, t, kb)
    if sub == "rs":
        await stop_project(int(parts[2]), parts[3])
        t, kb = running_screen()
        return await edit(q, t, kb)


# ═════════════════════════ Admin commands ═════════════════════════
@registered
@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t, kb = await admin_home()
    await update.message.reply_text(t, parse_mode=HTML, reply_markup=kb)


@registered
@admin_only
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ban = update.message.text.startswith("/ban")
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text(f"Usage: /{'ban' if ban else 'unban'} <user_id>")
    ok = await do_ban(ctx.bot, int(ctx.args[0]), ban)
    await update.message.reply_text(("🚫 Banned" if ban else "✅ Unbanned") + f" <code>{ctx.args[0]}</code>"
                                    if ok else "❌ Can't ban an admin.", parse_mode=HTML)


@registered
@admin_only
async def cmd_free(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = ctx.args
    if not a or not a[0].isdigit():
        return await update.message.reply_text("Usage: /free <user_id> [mb=1024] [days=0 (lifetime)]")
    mb = int(a[1]) if len(a) > 1 and a[1].isdigit() else 1024
    days = int(a[2]) if len(a) > 2 and a[2].isdigit() else 0
    await do_grant(ctx.bot, int(a[0]), mb, days)
    await update.message.reply_text(f"🎁 Granted {mb_label(mb)} ({'lifetime' if not days else f'{days}d'}) "
                                    f"to <code>{a[0]}</code>", parse_mode=HTML)


# ═════════════════════════ Background jobs / lifecycle ═════════════════════════
async def check_trial(bot, uid: int, u: dict, now: float):
    t = u.get("trial")
    if not t or t.get("done") or uid in ADMINS:
        return
    left = t["expires"] - now
    if left <= 0:
        t["done"] = True
        db.save()
        if get_plan(uid):      # user bought a plan meanwhile → keep everything
            await notify(bot, uid, "✅ Your free trial ended, but your hosting plan is active — your bots are safe.")
            return
        n = len(db.projects(uid))
        await delete_all_projects(uid)
        await notify(bot, uid,
                     f"⏰ <b>Your {TRIAL_HOURS}-hour free trial has ended.</b>\n{LINE}\n"
                     f"🛑 Your bot was stopped\n🗑 {n} project(s) deleted automatically\n\n"
                     "Liked it? Buy a plan to host your bot permanently.", need_plan_kb())
    elif left < 3600 and not t.get("warned"):
        t["warned"] = True
        db.save()
        await notify(bot, uid, "⏳ <b>Your free trial ends in less than 1 hour.</b>\n"
                               "After that your bot is stopped and deleted. Buy a plan to keep it online.",
                     need_plan_kb())


async def run_maintenance(bot):
    now = time.time()
    for k, u in list(db.data["users"].items()):
        uid, p = int(k), u.get("plan")
        await check_trial(bot, uid, u, now)
        if not p or not p.get("expires") or uid in ADMINS:
            continue
        left = p["expires"] - now
        if left <= 0:
            await stop_all(uid)
            u["plan"] = None
            db.save()
            await notify(bot, uid, "⌛ <b>Your hosting plan has expired.</b>\nYour bots were stopped and your files "
                                   "are kept. Renew to start them again.", need_plan_kb())
        elif left < 86400 and not u.get("warned"):
            u["warned"] = True
            db.save()
            await notify(bot, uid, "⏰ <b>Your plan expires in less than 24 hours.</b>\nRenew to keep your bots online.",
                         need_plan_kb())
    # keep logs small
    for lf in PROJECTS.glob("*/*/run.log"):
        try:
            if lf.stat().st_size > LOG_MAX_BYTES:
                with lf.open("r+b") as f:
                    f.seek(-100_000, os.SEEK_END)
                    tail = f.read()
                    f.seek(0)
                    f.write(tail)
                    f.truncate()
        except OSError:
            pass


async def maintenance_loop(app: Application):
    while True:
        try:
            await asyncio.sleep(60)
            await run_maintenance(app.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance failed")


async def post_init(app: Application):
    try:
        await run_maintenance(app.bot)   # clean up anything that expired while offline
    except Exception:
        log.exception("startup maintenance failed")
    n = 0
    for uid_s, projs in db.data["projects"].items():
        uid = int(uid_s)
        u = db.user(uid) or {}
        if u.get("banned") or not get_plan(uid):
            continue
        for name, p in projs.items():
            if p.get("autostart"):
                ok, _ = await start_project(uid, name)
                n += ok
    log.info("Auto-started %d project(s)", n)
    app.bot_data["maint"] = asyncio.create_task(maintenance_loop(app))


async def post_shutdown(app: Application):
    t = app.bot_data.get("maint")
    if t:
        t.cancel()
    for (uid, name) in list(procs):
        await stop_project(uid, name)


def main():
    if "your-bot-token" in TOKEN or 111111111 in ADMINS:
        sys.exit("Open bot.py and set TOKEN and ADMINS (and your UPI details) in the CONFIG block.")
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    pm = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=pm))
    app.add_handler(CommandHandler("help", cmd_help, filters=pm))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=pm))
    app.add_handler(CommandHandler("setenv", cmd_setenv, filters=pm))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=pm))
    app.add_handler(CommandHandler(["ban", "unban"], cmd_ban, filters=pm))
    app.add_handler(CommandHandler("free", cmd_free, filters=pm))
    app.add_handler(MessageHandler(pm & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(
        pm & filters.ALL & ~filters.COMMAND & ~filters.Document.ALL & ~filters.StatusUpdate.ALL, on_message))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^(adm|pay_ok|pay_no)"))
    app.add_handler(CallbackQueryHandler(on_callback))
    log.info("BotHost running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
