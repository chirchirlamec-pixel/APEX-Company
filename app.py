"""Apex Company - full-stack Flask app with registration fee payments.

Payments go to the administrator's accounts:
  * M-Pesa:  0114354416
  * PayPal:  chirchirlamec@gmail.com
Registration fee: USD 0.65

Run:  ADMIN_PASSWORD='choose-a-strong-one' python app.py
"""
import base64
import json
import os
import re
import secrets
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect,
                   render_template_string, request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash
from types import SimpleNamespace

import features

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
COMPANY = "Apex Company"
MPESA_NUMBER = os.getenv("MPESA_NUMBER", "0114354416")
PAYPAL_EMAIL = os.getenv("PAYPAL_EMAIL", "chirchirlamec@gmail.com")
FEE_USD = float(os.getenv("FEE_USD", "0.65"))
USD_TO_KES = float(os.getenv("USD_TO_KES", "129"))
FEE_KES = max(1, round(FEE_USD * USD_TO_KES))  # M-Pesa needs whole shillings

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", PAYPAL_EMAIL).lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or secrets.token_urlsafe(10)
GENERATED_PASSWORD = "ADMIN_PASSWORD" not in os.environ

# Optional automatic M-Pesa STK push (Safaricom Daraja). Needs a Paybill/Till.
DARAJA_KEY = os.getenv("DARAJA_CONSUMER_KEY")
DARAJA_SECRET = os.getenv("DARAJA_CONSUMER_SECRET")
DARAJA_SHORTCODE = os.getenv("DARAJA_SHORTCODE")
DARAJA_PASSKEY = os.getenv("DARAJA_PASSKEY")
DARAJA_CALLBACK = os.getenv("DARAJA_CALLBACK_URL")  # https://yourdomain/mpesa/callback
DARAJA_BASE = os.getenv("DARAJA_BASE", "https://sandbox.safaricom.co.ke")
DARAJA_ON = all([DARAJA_KEY, DARAJA_SECRET, DARAJA_SHORTCODE, DARAJA_PASSKEY, DARAJA_CALLBACK])

# Optional automatic PayPal checkout (REST API). Pays the account that owns the keys.
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_BASE = ("https://api-m.paypal.com" if os.getenv("PAYPAL_ENV") == "live"
               else "https://api-m.sandbox.paypal.com")
PAYPAL_ON = bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)

DATABASE_URL = os.getenv("DATABASE_URL")  # Postgres (Neon etc). Unset = local SQLite.
USE_PG = bool(DATABASE_URL)
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "apex.db"))

SUPPORT_WHATSAPP = os.getenv("SUPPORT_WHATSAPP", "254783716820")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "chirchirlamec@gmail.com")
# Real trading happens on a licensed broker. Put your partner/referral link here.
REAL_BROKER_URL = os.getenv("REAL_BROKER_URL", "https://deriv.com")

AFRICA = ["Algeria", "Angola", "Benin", "Botswana", "Burkina Faso", "Burundi", "Cabo Verde",
          "Cameroon", "Central African Republic", "Chad", "Comoros", "Congo (DRC)",
          "Congo (Republic)", "Cote d'Ivoire", "Djibouti", "Egypt", "Equatorial Guinea",
          "Eritrea", "Eswatini", "Ethiopia", "Gabon", "Gambia", "Ghana", "Guinea",
          "Guinea-Bissau", "Kenya", "Lesotho", "Liberia", "Libya", "Madagascar", "Malawi",
          "Mali", "Mauritania", "Mauritius", "Morocco", "Mozambique", "Namibia", "Niger",
          "Nigeria", "Rwanda", "Sao Tome and Principe", "Senegal", "Seychelles",
          "Sierra Leone", "Somalia", "South Africa", "South Sudan", "Sudan", "Tanzania",
          "Togo", "Tunisia", "Uganda", "Zambia", "Zimbabwe"]
FOREIGN = ["United States", "United Kingdom", "Canada", "Australia", "Germany", "France",
           "Italy", "Spain", "Netherlands", "Sweden", "Norway", "Ireland", "UAE",
           "Saudi Arabia", "Qatar", "India", "Pakistan", "China", "Japan", "South Korea",
           "Brazil", "Mexico", "Turkey", "Russia", "Poland", "Other"]
COUNTRIES = AFRICA + FOREIGN

MIGRATIONS = [("dob", "TEXT"), ("gender", "TEXT"), ("country", "TEXT"), ("city", "TEXT"),
              ("bio", "TEXT DEFAULT ''"), ("looking_for", "TEXT DEFAULT 'any'"),
              ("share_location", "INTEGER NOT NULL DEFAULT 0"), ("lat", "REAL"), ("lon", "REAL"),
              ("dating_enabled", "INTEGER NOT NULL DEFAULT 0"),
              ("banned", "INTEGER NOT NULL DEFAULT 0"), ("muted_until", "TEXT"),
              ("demo_balance", "REAL NOT NULL DEFAULT 10000")]


def age_from_dob(dob):
    try:
        d = datetime.strptime(dob, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    t = datetime.utcnow().date()
    return t.year - d.year - ((t.month, t.day) < (d.month, d.day))


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  paid INTEGER NOT NULL DEFAULT 0,
  dob TEXT, gender TEXT, country TEXT, city TEXT, bio TEXT DEFAULT '',
  looking_for TEXT DEFAULT 'any',
  share_location INTEGER NOT NULL DEFAULT 0,
  lat REAL, lon REAL,
  dating_enabled INTEGER NOT NULL DEFAULT 0,
  banned INTEGER NOT NULL DEFAULT 0,
  muted_until TEXT,
  demo_balance REAL NOT NULL DEFAULT 10000,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  method TEXT NOT NULL,            -- mpesa | paypal
  reference TEXT,                  -- M-Pesa code or PayPal transaction/order id
  amount_usd REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
  checkout_id TEXT,                -- Daraja CheckoutRequestID
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pay_ref ON payments(method, reference)
  WHERE reference IS NOT NULL;
"""


if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
    IntegrityError = psycopg.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError


class Database:
    """Tiny wrapper so the same code runs on SQLite (local) and Postgres (Vercel)."""

    def __init__(self):
        if USE_PG:
            self.conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USE_PG:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def db():
    if "db" not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = Database()
    schema = SCHEMA + ";" + features.SCHEMA_EXTRA
    if USE_PG:
        schema = (schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                  .replace("REAL", "DOUBLE PRECISION"))
    for stmt in schema.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    for col, ddl in MIGRATIONS:  # upgrade databases created before these columns existed
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} "
                         + (ddl.replace("REAL", "DOUBLE PRECISION") if USE_PG else ddl))
            conn.commit()
        except Exception:
            conn.rollback()
    row = conn.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users(name,email,phone,password_hash,is_admin,paid,created_at)"
            " VALUES(?,?,?,?,1,1,?)",
            ("Administrator", ADMIN_EMAIL, MPESA_NUMBER,
             generate_password_hash(ADMIN_PASSWORD), now()))
        conn.commit()
        if GENERATED_PASSWORD:
            print(f"\n*** Admin login: {ADMIN_EMAIL} / {ADMIN_PASSWORD} "
                  "(set ADMIN_PASSWORD to choose your own) ***\n")
    conn.close()


# --------------------------------------------------------------------------
# Auth + CSRF helpers
# --------------------------------------------------------------------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or not u["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint != "mpesa_callback":
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
            abort(400, "Bad CSRF token")


app.jinja_env.globals.update(csrf_token=csrf_token, current_user=current_user,
                             COMPANY=COMPANY, FEE_USD=FEE_USD, FEE_KES=FEE_KES,
                             COUNTRIES=COUNTRIES, AFRICA=AFRICA,
                             SUPPORT_WHATSAPP=SUPPORT_WHATSAPP, SUPPORT_EMAIL=SUPPORT_EMAIL,
                             REAL_BROKER_URL=REAL_BROKER_URL)


def normalize_phone(raw):
    """Return 2547XXXXXXXX / 2541XXXXXXXX or None."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif digits.startswith("254") and len(digits) == 12:
        pass
    elif digits.startswith("7") or digits.startswith("1"):
        digits = "254" + digits if len(digits) == 9 else digits
    return digits if re.fullmatch(r"254[17]\d{8}", digits or "") else None


# --------------------------------------------------------------------------
# Payment provider helpers (standard library only)
# --------------------------------------------------------------------------
def http_json(url, data=None, headers=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def daraja_token():
    cred = base64.b64encode(f"{DARAJA_KEY}:{DARAJA_SECRET}".encode()).decode()
    r = http_json(f"{DARAJA_BASE}/oauth/v1/generate?grant_type=client_credentials",
                  headers={"Authorization": f"Basic {cred}"})
    return r["access_token"]


def stk_push(phone):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    pwd = base64.b64encode(f"{DARAJA_SHORTCODE}{DARAJA_PASSKEY}{ts}".encode()).decode()
    payload = {
        "BusinessShortCode": DARAJA_SHORTCODE, "Password": pwd, "Timestamp": ts,
        "TransactionType": "CustomerPayBillOnline", "Amount": FEE_KES,
        "PartyA": phone, "PartyB": DARAJA_SHORTCODE, "PhoneNumber": phone,
        "CallBackURL": DARAJA_CALLBACK, "AccountReference": "ApexReg",
        "TransactionDesc": "Apex Company registration fee",
    }
    return http_json(f"{DARAJA_BASE}/mpesa/stkpush/v1/processrequest", payload,
                     {"Authorization": f"Bearer {daraja_token()}",
                      "Content-Type": "application/json"})


def paypal_token():
    cred = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        f"{PAYPAL_BASE}/v1/oauth2/token", data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {cred}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


def mark_paid(payment_id, ref=None):
    d = db()
    p = d.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not p:
        return
    d.execute("UPDATE payments SET status='approved', reference=COALESCE(?,reference)"
              " WHERE id=?", (ref, payment_id))
    d.execute("UPDATE users SET paid=1 WHERE id=?", (p["user_id"],))
    d.commit()


# --------------------------------------------------------------------------
# Routes - public
# --------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template_string(TEMPLATES["home.html"])


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        raw_phone = request.form.get("phone", "")
        phone = normalize_phone(raw_phone)
        if not phone:  # foreign number: keep digits with a leading +
            digits = re.sub(r"\D", "", raw_phone)
            phone = "+" + digits if 8 <= len(digits) <= 15 else None
        pw = request.form.get("password", "")
        dob = request.form.get("dob", "")
        gender = request.form.get("gender", "")
        country = request.form.get("country", "")
        city = " ".join(request.form.get("city", "").split())[:60]
        age = age_from_dob(dob)
        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter your name and a valid email.", "error")
        elif not phone:
            flash("Enter a valid phone number (with country code if outside Kenya).", "error")
        elif age is None or age < 18 or age > 100:
            flash("You must be 18 or older to join.", "error")
        elif gender not in ("male", "female", "other") or country not in COUNTRIES:
            flash("Choose your gender and country.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            try:
                db().execute(
                    "INSERT INTO users(name,email,phone,password_hash,dob,gender,country,city,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (name, email, phone, generate_password_hash(pw), dob, gender, country,
                     city, now()))
                db().commit()
            except IntegrityError:
                db().rollback()
                flash("That email is already registered. Log in instead.", "error")
            else:
                u = db().execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                session.clear()
                session["uid"] = u["id"]
                return redirect(url_for("pay"))
    return render_template_string(TEMPLATES["register.html"])


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and u["banned"]:
            flash("This account has been suspended.", "error")
        elif u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            return redirect(url_for("admin" if u["is_admin"] else "pay"))
        flash("Wrong email or password.", "error")
    return render_template_string(TEMPLATES["login.html"])


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# --------------------------------------------------------------------------
# Routes - member payment
# --------------------------------------------------------------------------
@app.route("/pay")
@login_required
def pay():
    u = current_user()
    payments = db().execute("SELECT * FROM payments WHERE user_id=? ORDER BY id DESC",
                            (u["id"],)).fetchall()
    return render_template_string(
        TEMPLATES["pay.html"], u=u, payments=payments, MPESA=MPESA_NUMBER,
        PAYPAL_EMAIL=PAYPAL_EMAIL, DARAJA_ON=DARAJA_ON, PAYPAL_ON=PAYPAL_ON,
        PAYPAL_CLIENT_ID=PAYPAL_CLIENT_ID)


@app.route("/pay/mpesa/stk", methods=["POST"])
@login_required
def pay_mpesa_stk():
    if not DARAJA_ON:
        abort(404)
    u = current_user()
    if not u["phone"].startswith("254"):
        flash("M-Pesa prompts need a Kenyan number. Use PayPal instead.", "error")
        return redirect(url_for("pay"))
    try:
        r = stk_push(u["phone"])
        if r.get("ResponseCode") != "0":
            raise RuntimeError(r.get("errorMessage") or r.get("ResponseDescription"))
    except Exception as exc:  # network / credentials problem
        flash(f"Could not start M-Pesa prompt: {exc}", "error")
        return redirect(url_for("pay"))
    db().execute(
        "INSERT INTO payments(user_id,method,amount_usd,checkout_id,created_at)"
        " VALUES(?,?,?,?,?)", (u["id"], "mpesa", FEE_USD, r["CheckoutRequestID"], now()))
    db().commit()
    flash("Check your phone and enter your M-Pesa PIN.", "ok")
    return redirect(url_for("pay"))


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    """Called by Safaricom. Always answer 200 with the ack."""
    body = (request.get_json(silent=True) or {}).get("Body", {}).get("stkCallback", {})
    checkout_id = body.get("CheckoutRequestID")
    p = db().execute("SELECT * FROM payments WHERE checkout_id=?", (checkout_id,)).fetchone()
    if p and body.get("ResultCode") == 0:
        items = {i["Name"]: i.get("Value")
                 for i in body.get("CallbackMetadata", {}).get("Item", [])}
        if int(items.get("Amount", 0)) >= FEE_KES:
            mark_paid(p["id"], items.get("MpesaReceiptNumber"))
    elif p:
        db().execute("UPDATE payments SET status='rejected' WHERE id=?", (p["id"],))
        db().commit()
    return jsonify(ResultCode=0, ResultDesc="Accepted")


@app.route("/pay/manual", methods=["POST"])
@login_required
def pay_manual():
    """Member paid by sending money directly; admin verifies and approves."""
    u = current_user()
    method = request.form.get("method")
    ref = request.form.get("reference", "").strip().upper()
    if method == "mpesa" and not re.fullmatch(r"[A-Z0-9]{10}", ref):
        flash("M-Pesa codes are 10 letters/digits, e.g. QKH7A1B2C3.", "error")
    elif method == "paypal" and not re.fullmatch(r"[A-Z0-9]{8,20}", ref):
        flash("Enter the PayPal transaction ID from your receipt.", "error")
    elif method not in ("mpesa", "paypal"):
        abort(400)
    else:
        try:
            db().execute(
                "INSERT INTO payments(user_id,method,reference,amount_usd,created_at)"
                " VALUES(?,?,?,?,?)", (u["id"], method, ref, FEE_USD, now()))
            db().commit()
            flash("Received. The administrator will confirm your payment shortly.", "ok")
        except IntegrityError:
            db().rollback()
            flash("That transaction code has already been submitted.", "error")
    return redirect(url_for("pay"))


@app.route("/pay/paypal/create", methods=["POST"])
@login_required
def paypal_create():
    if not PAYPAL_ON:
        abort(404)
    order = http_json(
        f"{PAYPAL_BASE}/v2/checkout/orders",
        {"intent": "CAPTURE", "purchase_units": [{
            "description": "Apex Company registration fee",
            "amount": {"currency_code": "USD", "value": f"{FEE_USD:.2f}"}}]},
        {"Authorization": f"Bearer {paypal_token()}", "Content-Type": "application/json"})
    return jsonify(id=order["id"])


@app.route("/pay/paypal/capture/<order_id>", methods=["POST"])
@login_required
def paypal_capture(order_id):
    if not PAYPAL_ON or not re.fullmatch(r"[A-Z0-9]{8,30}", order_id):
        abort(404)
    r = http_json(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture", {},
                  {"Authorization": f"Bearer {paypal_token()}",
                   "Content-Type": "application/json"}, method="POST")
    cap = r["purchase_units"][0]["payments"]["captures"][0]
    ok = (r.get("status") == "COMPLETED" and cap["amount"]["currency_code"] == "USD"
          and float(cap["amount"]["value"]) >= FEE_USD)
    u = current_user()
    try:
        db().execute(
            "INSERT INTO payments(user_id,method,reference,amount_usd,status,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (u["id"], "paypal", cap["id"], FEE_USD, "pending", now()))
        db().commit()
    except IntegrityError:
        db().rollback()
        return jsonify(ok=False), 409
    if ok:
        pid = db().execute("SELECT id FROM payments WHERE reference=?",
                           (cap["id"],)).fetchone()["id"]
        mark_paid(pid)
    return jsonify(ok=ok)


# --------------------------------------------------------------------------
# Routes - administrator
# --------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin():
    d = db()
    stats = {
        "members": d.execute("SELECT COUNT(*) c FROM users WHERE is_admin=0").fetchone()["c"],
        "paid": d.execute("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND paid=1").fetchone()["c"],
        "revenue": d.execute("SELECT COALESCE(SUM(amount_usd),0) s FROM payments "
                             "WHERE status='approved'").fetchone()["s"],
    }
    pending = d.execute(
        "SELECT p.*, u.name, u.email, u.phone FROM payments p JOIN users u ON u.id=p.user_id"
        " WHERE p.status='pending' ORDER BY p.id").fetchall()
    members = d.execute("SELECT * FROM users WHERE is_admin=0 ORDER BY id DESC").fetchall()
    return render_template_string(TEMPLATES["admin.html"], stats=stats, pending=pending,
                                  members=members)


@app.route("/admin/payment/<int:pid>/<action>", methods=["POST"])
@admin_required
def admin_payment(pid, action):
    if action == "approve":
        mark_paid(pid)
    elif action == "reject":
        db().execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        db().commit()
    else:
        abort(400)
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------
# Templates (kept inline so the whole app is one file)
# --------------------------------------------------------------------------
BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}{{ COMPANY }}{% endblock %}</title>
<style>
:root{--ink:#0f172a;--mut:#64748b;--bg:#f8fafc;--card:#fff;--brand:#0b5fff;--ok:#047857;--bad:#b91c1c;--line:#e2e8f0}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,sans-serif;color:var(--ink);background:var(--bg)}
nav{display:flex;flex-wrap:wrap;gap:6px;justify-content:space-between;align-items:center;padding:14px 20px;background:var(--ink);color:#fff}
footer{text-align:center;padding:24px 16px;color:var(--mut);font-size:.9rem}footer a{color:var(--brand)}
nav a,nav button{color:#fff;text-decoration:none;margin-left:14px;background:none;border:0;font:inherit;cursor:pointer}
.logo{font-weight:800;letter-spacing:.5px;margin:0}
main{max-width:760px;margin:0 auto;padding:24px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}
h1{font-size:1.8rem;margin:.2em 0}h2{font-size:1.2rem;margin-top:0}
label{display:block;margin:12px 0 4px;font-weight:600}
input{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}
.btn{display:inline-block;background:var(--brand);color:#fff;border:0;border-radius:8px;padding:11px 18px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;margin-top:12px}
.btn.sm{padding:6px 12px;margin:0}.btn.red{background:var(--bad)}.btn.green{background:var(--ok)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px}.flash.error{background:#fee2e2;color:var(--bad)}.flash.ok{background:#d1fae5;color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:.92rem}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}
.pill{padding:2px 9px;border-radius:99px;font-size:.8rem;background:#e2e8f0}.pill.approved{background:#d1fae5;color:var(--ok)}.pill.rejected{background:#fee2e2;color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.stat{text-align:center}.stat b{font-size:1.6rem;display:block}
.mut{color:var(--mut)}.big{font-size:1.25rem;font-weight:700}
</style></head><body>
<nav><a class="logo" href="{{ url_for('home') }}">APEX COMPANY</a><div>
{% if current_user() %}
  {% if current_user()['is_admin'] %}<a href="{{ url_for('admin') }}">Admin</a><a href="{{ url_for('hub.admin_moderation') }}">Moderation</a>{% else %}<a href="{{ url_for('pay') }}">Account</a>{% endif %}
  <a href="{{ url_for('hub.community') }}">Community</a><a href="{{ url_for('hub.chat_room', room='general') }}">Chat</a><a href="{{ url_for('hub.discover') }}">Dating</a><a href="{{ url_for('hub.trade') }}">Trade</a><a href="{{ url_for('hub.profile') }}">Profile</a>
  <form method="post" action="{{ url_for('logout') }}" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button>Log out</button></form>
{% else %}<a href="{{ url_for('login') }}">Log in</a><a href="{{ url_for('register') }}">Register</a>{% endif %}
</div></nav><main>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for c,m in msgs %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}</main>
<footer>Questions? WhatsApp <a href="https://wa.me/{{ SUPPORT_WHATSAPP }}" target="_blank" rel="noopener">+{{ SUPPORT_WHATSAPP }}</a>
&middot; <a href="mailto:{{ SUPPORT_EMAIL }}">{{ SUPPORT_EMAIL }}</a><br>&copy; {{ COMPANY }}</footer></body></html>"""

TEMPLATES = {
    "base.html": BASE,
    "home.html": """{% extends 'base.html' %}{% block body %}
<div class="card"><h1>Welcome to Apex Company</h1>
<p class="mut">A moderated community where Africans and people from around the world meet.
Chat in live rooms, see who is nearby, find dating matches, and practise forex on a demo account.
Joining costs just <b>${{ '%.2f'|format(FEE_USD) }}</b> (about KES {{ FEE_KES }}), payable by M-Pesa or PayPal.</p>
<a class="btn" href="{{ url_for('register') }}">Register now</a></div>
<div class="card"><h2>What you get</h2><p>&#128172; Moderated chat rooms and private messages<br>&#127757; Community directory with opt-in locations<br>
&#10084; Dating: swipe, match and message (18+)<br>&#128200; Demo forex trading, plus a route to a licensed broker for real accounts</p></div>{% endblock %}""",
    "register.html": """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Create your account</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<label>Full name</label><input name="name" required>
<label>Email</label><input name="email" type="email" required>
<label>Phone (add country code if outside Kenya)</label><input name="phone" placeholder="0712345678 or +447700900123" required>
<label>Date of birth (18+ only)</label><input name="dob" type="date" required>
<label>Gender</label><select name="gender" required><option value="male">Male</option><option value="female">Female</option><option value="other">Other</option></select>
<label>Country</label><select name="country" required>{% for c in COUNTRIES %}<option>{{ c }}</option>{% endfor %}</select>
<label>City</label><input name="city" maxlength="60">
<label>Password (8+ characters)</label><input name="password" type="password" minlength="8" required>
<button class="btn">Continue to payment</button></form></div>{% endblock %}""",
    "login.html": """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Log in</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<label>Email</label><input name="email" type="email" required>
<label>Password</label><input name="password" type="password" required>
<button class="btn">Log in</button></form></div>{% endblock %}""",
    "pay.html": """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Hello, {{ u['name'] }}</h2>
{% if u['paid'] %}<p class="big" style="color:var(--ok)">&#10003; Registration complete. You are a verified member.</p>
{% else %}<p>Registration fee: <span class="big">${{ '%.2f'|format(FEE_USD) }}</span> <span class="mut">(KES {{ FEE_KES }})</span></p>{% endif %}</div>
{% if not u['paid'] %}
<div class="card"><h2>Pay with M-Pesa</h2>
{% if DARAJA_ON %}
<form method="post" action="{{ url_for('pay_mpesa_stk') }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<p>We will send a payment prompt to <b>{{ u['phone'] }}</b>.</p><button class="btn">Send M-Pesa prompt (KES {{ FEE_KES }})</button></form><hr>
{% endif %}
<p>Send <b>KES {{ FEE_KES }}</b> via <b>Send Money</b> to <span class="big">{{ MPESA }}</span>, then paste the M-Pesa confirmation code:</p>
<form method="post" action="{{ url_for('pay_manual') }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<input type="hidden" name="method" value="mpesa"><input name="reference" placeholder="e.g. QKH7A1B2C3" maxlength="10" required>
<button class="btn">Submit M-Pesa code</button></form></div>
<div class="card"><h2>Pay with PayPal</h2>
{% if PAYPAL_ON %}<div id="paypal-buttons"></div>
<script src="https://www.paypal.com/sdk/js?client-id={{ PAYPAL_CLIENT_ID }}&currency=USD"></script>
<script>
const csrf = "{{ csrf_token() }}";
paypal.Buttons({
  createOrder: () => fetch("{{ url_for('paypal_create') }}", {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"}, body:"csrf="+csrf}).then(r=>r.json()).then(d=>d.id),
  onApprove: d => fetch("/pay/paypal/capture/"+d.orderID, {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"}, body:"csrf="+csrf}).then(r=>r.json()).then(()=>location.reload())
}).render("#paypal-buttons");
</script><hr>{% endif %}
<p>Or send <b>${{ '%.2f'|format(FEE_USD) }}</b> to <span class="big">{{ PAYPAL_EMAIL }}</span> and paste the PayPal transaction ID:</p>
<form method="post" action="{{ url_for('pay_manual') }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<input type="hidden" name="method" value="paypal"><input name="reference" placeholder="Transaction ID" required>
<button class="btn">Submit PayPal ID</button></form></div>
{% endif %}
{% if payments %}<div class="card"><h2>Your payments</h2><table><tr><th>Date</th><th>Method</th><th>Reference</th><th>Status</th></tr>
{% for p in payments %}<tr><td>{{ p['created_at'] }}</td><td>{{ p['method'] }}</td><td>{{ p['reference'] or '-' }}</td><td><span class="pill {{ p['status'] }}">{{ p['status'] }}</span></td></tr>{% endfor %}</table></div>{% endif %}
{% endblock %}""",
    "admin.html": """{% extends 'base.html' %}{% block body %}
<div class="grid"><div class="card stat"><b>{{ stats.members }}</b>Members</div>
<div class="card stat"><b>{{ stats.paid }}</b>Paid</div>
<div class="card stat"><b>${{ '%.2f'|format(stats.revenue) }}</b>Revenue</div></div>
<div class="card"><h2>Payments awaiting your confirmation</h2>
{% if not pending %}<p class="mut">Nothing pending.</p>{% else %}
<p class="mut">Match each code against your M-Pesa SMS or PayPal activity before approving.</p>
<table><tr><th>Member</th><th>Method</th><th>Code</th><th></th></tr>
{% for p in pending %}<tr><td>{{ p['name'] }}<br><span class="mut">{{ p['email'] }}<br>{{ p['phone'] }}</span></td>
<td>{{ p['method'] }}</td><td>{{ p['reference'] or 'awaiting prompt' }}</td><td>
{% for a,c in (('approve','green'),('reject','red')) %}<form method="post" action="{{ url_for('admin_payment', pid=p['id'], action=a) }}" style="display:inline">
<input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn sm {{ c }}">{{ a|capitalize }}</button></form> {% endfor %}</td></tr>{% endfor %}</table>{% endif %}</div>
<div class="card"><h2>All members</h2><table><tr><th>Name</th><th>Email</th><th>Phone</th><th>Paid</th></tr>
{% for m in members %}<tr><td>{{ m['name'] }}</td><td>{{ m['email'] }}</td><td>{{ m['phone'] }}</td><td>{{ 'Yes' if m['paid'] else 'No' }}</td></tr>{% endfor %}</table></div>
{% endblock %}""",
}
features.register(app, SimpleNamespace(
    db=db, current_user=current_user, now=now, age=age_from_dob, TEMPLATES=TEMPLATES,
    COUNTRIES=COUNTRIES, AFRICA=set(AFRICA), IntegrityError=IntegrityError))
app.jinja_loader = DictLoader(TEMPLATES)

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
