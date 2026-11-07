"""Apex Company community hub.

* Moderated chat (public rooms + private messages) with reports, mute and ban
* Community directory showing members' opt-in (approximate) location
* Dating (swipe / match) for verified adults
* Demo forex trading room: simulated prices and virtual money only
"""
import hashlib
import math
import os
import re
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, abort, flash, jsonify, redirect,
                   render_template_string, request, url_for)

bp = Blueprint("hub", __name__)
H = None  # helpers injected by app.py

SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room TEXT NOT NULL,
  sender_id INTEGER NOT NULL,
  recipient_id INTEGER,
  body TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_room ON messages(room, id);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id INTEGER NOT NULL,
  reported_id INTEGER NOT NULL,
  message_id INTEGER,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
  user_id INTEGER NOT NULL,
  blocked_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS swipes (
  user_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  liked INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, target_id)
);
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_a INTEGER NOT NULL,
  user_b INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (user_a, user_b)
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  lots REAL NOT NULL,
  open_price REAL NOT NULL,
  sl REAL,
  tp REAL,
  status TEXT NOT NULL DEFAULT 'open',
  close_price REAL,
  pnl REAL,
  opened_at TEXT NOT NULL,
  closed_at TEXT
);
"""


def db():
    return H.db()


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------
def member_required(api=False):
    """Logged in, not banned, and registration fee paid (admins always pass)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = H.current_user()
            if not u:
                if api:
                    return jsonify(ok=False, error="Please log in."), 401
                return redirect(url_for("login"))
            if u["banned"]:
                abort(403)
            if not (u["paid"] or u["is_admin"]):
                if api:
                    return jsonify(ok=False, error="Pay the registration fee first."), 402
                flash("Pay the registration fee to unlock this section.", "error")
                return redirect(url_for("pay"))
            return fn(*a, **kw)
        return wrapper
    return deco


def admin_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = H.current_user()
        if not u or not u["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def is_blocked(a, b):
    return db().execute(
        "SELECT 1 AS x FROM blocks WHERE (user_id=? AND blocked_id=?)"
        " OR (user_id=? AND blocked_id=?)", (a, b, b, a)).fetchone() is not None


def blocked_ids(uid):
    rows = db().execute("SELECT blocked_id AS o FROM blocks WHERE user_id=?"
                        " UNION SELECT user_id AS o FROM blocks WHERE blocked_id=?",
                        (uid, uid)).fetchall()
    return {r["o"] for r in rows}


# --------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------
BANNED = [w.strip().lower() for w in
          os.getenv("BANNED_WORDS", "fuck,shit,bitch,bastard,asshole").split(",") if w.strip()]
SCAM = ["western union", "send me money", "gift card", "double your money",
        "guaranteed profit", "recovery fee", "wire transfer", "bitcoin wallet",
        "send money first", "cashapp"]
URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|co|ke|io|xyz|me|ly)\b)", re.I)


def moderate(text):
    """Return a rejection reason, or None if the text is fine."""
    low = text.lower()
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in BANNED):
        return "Please keep it respectful."
    if any(p in low for p in SCAM):
        return "That looks like a scam or money request, which is not allowed."
    if URL_RE.search(text):
        return "Links are not allowed here."
    return None


ROOMS = {
    "general": "General lounge",
    "africa": "Africa Connect",
    "world": "Africans and foreigners",
    "forex": "Forex talk",
}


def resolve_room(room, me):
    """Return (storage key, other user id or None). Aborts on bad input."""
    if room in ROOMS:
        return room, None
    m = re.fullmatch(r"dm-(\d+)", room or "")
    if not m:
        abort(404)
    other = int(m.group(1))
    if other == me["id"]:
        abort(400)
    ou = db().execute("SELECT id, banned FROM users WHERE id=?", (other,)).fetchone()
    if not ou or ou["banned"]:
        abort(404)
    if is_blocked(me["id"], other):
        abort(403)
    a, b = sorted((me["id"], other))
    return f"dm:{a}:{b}", other


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------
@bp.route("/profile", methods=["GET", "POST"])
@member_required()
def profile():
    u = H.current_user()
    if request.method == "POST":
        f = request.form
        country = f.get("country", "")
        gender = f.get("gender", "")
        looking = f.get("looking_for", "any")
        bio = " ".join(f.get("bio", "").split())[:300]
        city = " ".join(f.get("city", "").split())[:60]
        share = 1 if f.get("share_location") else 0
        dating = 1 if f.get("dating_enabled") else 0
        lat = lon = None
        if share:
            try:
                lat = round(float(f.get("lat", "")), 1)   # ~11 km precision only
                lon = round(float(f.get("lon", "")), 1)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    lat = lon = None
            except ValueError:
                lat = lon = None
        problem = moderate(bio + " " + city) if bio or city else None
        if country not in H.COUNTRIES:
            flash("Choose your country.", "error")
        elif gender not in ("male", "female", "other") or looking not in ("any", "male", "female"):
            flash("Choose your gender and who you want to meet.", "error")
        elif problem:
            flash(f"Bio/city rejected: {problem}", "error")
        else:
            db().execute(
                "UPDATE users SET country=?, city=?, gender=?, looking_for=?, bio=?,"
                " share_location=?, dating_enabled=?, lat=?, lon=? WHERE id=?",
                (country, city, gender, looking, bio, share, dating, lat, lon, u["id"]))
            db().commit()
            flash("Profile saved.", "ok")
            return redirect(url_for("hub.profile"))
    return render_template_string(T["profile.html"], u=u)


# --------------------------------------------------------------------------
# Community directory + insights
# --------------------------------------------------------------------------
@bp.route("/community")
@member_required()
def community():
    me = H.current_user()
    region = request.args.get("region", "all")
    rows = db().execute(
        "SELECT id,name,country,city,bio,dob,gender,lat,lon FROM users"
        " WHERE is_admin=0 AND banned=0 AND paid=1 AND share_location=1 AND id<>?"
        " ORDER BY id DESC LIMIT 300", (me["id"],)).fetchall()
    hidden = blocked_ids(me["id"])
    people, by_country = [], {}
    for r in rows:
        if r["id"] in hidden:
            continue
        d = dict(r)
        d["age"] = H.age(r["dob"])
        d["africa"] = r["country"] in H.AFRICA
        people.append(d)
        by_country[r["country"]] = by_country.get(r["country"], 0) + 1
    shown = [p for p in people if region == "all"
             or (region == "africa" and p["africa"])
             or (region == "world" and not p["africa"])]
    insights = {
        "total": len(people),
        "africa": sum(1 for p in people if p["africa"]),
        "world": sum(1 for p in people if not p["africa"]),
        "top": sorted(by_country.items(), key=lambda kv: -kv[1])[:6],
    }
    return render_template_string(T["community.html"], people=shown, insights=insights,
                                  region=region)


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
@bp.route("/chat")
@member_required()
def chat_home():
    return redirect(url_for("hub.chat_room", room="general"))


@bp.route("/chat/<room>")
@member_required()
def chat_room(room):
    if room not in ROOMS:
        abort(404)
    return render_template_string(T["chat.html"], room=room, title=ROOMS[room],
                                  rooms=ROOMS, dm_user=None)


@bp.route("/chat/dm/<int:uid>")
@member_required()
def chat_dm(uid):
    me = H.current_user()
    resolve_room(f"dm-{uid}", me)
    other = db().execute("SELECT id, name FROM users WHERE id=?", (uid,)).fetchone()
    return render_template_string(T["chat.html"], room=f"dm-{uid}",
                                  title=f"Chat with {other['name']}", rooms=ROOMS,
                                  dm_user=other)


@bp.route("/api/chat/send", methods=["POST"])
@member_required(api=True)
def chat_send():
    me = H.current_user()
    key, other = resolve_room(request.form.get("room"), me)
    body = " ".join(request.form.get("body", "").split())
    if not body or len(body) > 500:
        return jsonify(ok=False, error="Message must be 1 to 500 characters."), 400
    if me["muted_until"] and me["muted_until"] > H.now():
        return jsonify(ok=False, error=f"You are muted until {me['muted_until']} UTC."), 403
    since = (datetime.utcnow() - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    recent = db().execute("SELECT COUNT(*) AS c FROM messages WHERE sender_id=? AND created_at>=?",
                          (me["id"], since)).fetchone()["c"]
    if recent >= 5:
        return jsonify(ok=False, error="Slow down a little."), 429
    if not me["is_admin"]:
        reason = moderate(body)
        if reason:
            return jsonify(ok=False, error=reason), 400
    db().execute("INSERT INTO messages(room,sender_id,recipient_id,body,created_at)"
                 " VALUES(?,?,?,?,?)", (key, me["id"], other, body, H.now()))
    db().commit()
    return jsonify(ok=True)


@bp.route("/api/chat/poll")
@member_required(api=True)
def chat_poll():
    me = H.current_user()
    key, _ = resolve_room(request.args.get("room"), me)
    after = request.args.get("after", type=int, default=0)
    rows = db().execute(
        "SELECT m.id, m.sender_id, m.body, m.created_at, u.name, u.country,"
        " u.share_location, u.is_admin FROM messages m JOIN users u ON u.id=m.sender_id"
        " WHERE m.room=? AND m.id>? AND m.deleted=0 ORDER BY m.id DESC LIMIT 50",
        (key, after)).fetchall()
    hidden = blocked_ids(me["id"])
    out, last = [], after
    for r in reversed(rows):
        last = max(last, r["id"])
        if r["sender_id"] in hidden:
            continue
        out.append(dict(id=r["id"], user_id=r["sender_id"], name=r["name"], body=r["body"],
                        time=r["created_at"][11:16], mod=bool(r["is_admin"]),
                        country=r["country"] if r["share_location"] else ""))
    return jsonify(ok=True, me=me["id"], last=last, messages=out)


@bp.route("/api/chat/report", methods=["POST"])
@member_required(api=True)
def chat_report():
    me = H.current_user()
    mid = request.form.get("message_id", type=int)
    msg = db().execute("SELECT id, sender_id FROM messages WHERE id=?", (mid,)).fetchone()
    if not msg or msg["sender_id"] == me["id"]:
        return jsonify(ok=False, error="You can't report that."), 400
    reason = " ".join(request.form.get("reason", "").split())[:200] or "Inappropriate"
    db().execute("INSERT INTO reports(reporter_id,reported_id,message_id,reason,created_at)"
                 " VALUES(?,?,?,?,?)", (me["id"], msg["sender_id"], msg["id"], reason, H.now()))
    db().commit()
    return jsonify(ok=True)


@bp.route("/api/block", methods=["POST"])
@member_required(api=True)
def block_user():
    me = H.current_user()
    uid = request.form.get("user_id", type=int)
    if not uid or uid == me["id"]:
        return jsonify(ok=False), 400
    try:
        db().execute("INSERT INTO blocks(user_id,blocked_id) VALUES(?,?)", (me["id"], uid))
        db().commit()
    except H.IntegrityError:
        db().rollback()
    return jsonify(ok=True)


@bp.route("/report-user", methods=["POST"])
@member_required()
def report_user():
    me = H.current_user()
    uid = request.form.get("user_id", type=int)
    exists = db().execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if exists and uid != me["id"]:
        reason = " ".join(request.form.get("reason", "").split())[:200] or "Inappropriate profile"
        db().execute("INSERT INTO reports(reporter_id,reported_id,reason,created_at)"
                     " VALUES(?,?,?,?)", (me["id"], uid, reason, H.now()))
        db().commit()
        flash("Thanks, a moderator will review this.", "ok")
    return redirect(url_for("hub.discover"))


# --------------------------------------------------------------------------
# Admin moderation
# --------------------------------------------------------------------------
@bp.route("/admin/moderation")
@admin_only
def admin_moderation():
    reports = db().execute(
        "SELECT r.id AS id, r.reason AS reason, r.created_at AS created_at,"
        " r.message_id AS message_id, r.reported_id AS reported_id,"
        " ru.name AS reported_name, pu.name AS reporter_name,"
        " m.body AS msg_body, m.deleted AS msg_deleted"
        " FROM reports r JOIN users ru ON ru.id=r.reported_id"
        " JOIN users pu ON pu.id=r.reporter_id LEFT JOIN messages m ON m.id=r.message_id"
        " WHERE r.status='open' ORDER BY r.id").fetchall()
    sanctioned = db().execute(
        "SELECT id, name, email, banned, muted_until FROM users"
        " WHERE is_admin=0 AND (banned=1 OR muted_until > ?)", (H.now(),)).fetchall()
    return render_template_string(T["moderation.html"], reports=reports, sanctioned=sanctioned)


@bp.route("/admin/mod/<int:rid>/<action>", methods=["POST"])
@admin_only
def admin_mod_action(rid, action):
    r = db().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r or action not in ("dismiss", "delete", "mute", "ban"):
        abort(400)
    if action == "delete" and r["message_id"]:
        db().execute("UPDATE messages SET deleted=1 WHERE id=?", (r["message_id"],))
    elif action == "mute":
        until = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        db().execute("UPDATE users SET muted_until=? WHERE id=? AND is_admin=0",
                     (until, r["reported_id"]))
    elif action == "ban":
        db().execute("UPDATE users SET banned=1 WHERE id=? AND is_admin=0", (r["reported_id"],))
    db().execute("UPDATE reports SET status='resolved' WHERE id=?", (rid,))
    db().commit()
    return redirect(url_for("hub.admin_moderation"))


@bp.route("/admin/user/<int:uid>/unban", methods=["POST"])
@admin_only
def admin_unban(uid):
    db().execute("UPDATE users SET banned=0, muted_until=NULL WHERE id=?", (uid,))
    db().commit()
    return redirect(url_for("hub.admin_moderation"))


# --------------------------------------------------------------------------
# Dating
# --------------------------------------------------------------------------
@bp.route("/discover")
@member_required()
def discover():
    me = H.current_user()
    if not me["dating_enabled"] or not me["gender"] or not me["dob"]:
        flash("Turn on dating and complete your profile first.", "error")
        return redirect(url_for("hub.profile"))
    sql = ("SELECT id,name,country,city,bio,dob,gender,share_location FROM users"
           " WHERE id<>? AND is_admin=0 AND banned=0 AND paid=1 AND dating_enabled=1"
           " AND id NOT IN (SELECT target_id FROM swipes WHERE user_id=?)"
           " AND id NOT IN (SELECT blocked_id FROM blocks WHERE user_id=?)"
           " AND id NOT IN (SELECT user_id FROM blocks WHERE blocked_id=?)"
           " AND (looking_for='any' OR looking_for=?)")
    params = [me["id"], me["id"], me["id"], me["id"], me["gender"]]
    if me["looking_for"] != "any":
        sql += " AND gender=?"
        params.append(me["looking_for"])
    cand = db().execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    card = None
    if cand:
        card = dict(cand)
        card["age"] = H.age(cand["dob"])
    return render_template_string(T["discover.html"], card=card)


@bp.route("/discover/swipe", methods=["POST"])
@member_required()
def swipe():
    me = H.current_user()
    target = request.form.get("target", type=int)
    liked = 1 if request.form.get("action") == "like" else 0
    t = db().execute("SELECT id, name FROM users WHERE id=? AND banned=0 AND dating_enabled=1",
                     (target,)).fetchone()
    if not t or t["id"] == me["id"] or is_blocked(me["id"], t["id"]):
        abort(400)
    try:
        db().execute("INSERT INTO swipes(user_id,target_id,liked,created_at) VALUES(?,?,?,?)",
                     (me["id"], t["id"], liked, H.now()))
        db().commit()
    except H.IntegrityError:
        db().rollback()
        return redirect(url_for("hub.discover"))
    if liked and db().execute("SELECT 1 AS x FROM swipes WHERE user_id=? AND target_id=? AND liked=1",
                              (t["id"], me["id"])).fetchone():
        a, b = sorted((me["id"], t["id"]))
        try:
            db().execute("INSERT INTO matches(user_a,user_b,created_at) VALUES(?,?,?)",
                         (a, b, H.now()))
            db().commit()
        except H.IntegrityError:
            db().rollback()
        flash(f"It's a match with {t['name']}! Say hello in Matches.", "ok")
    return redirect(url_for("hub.discover"))


@bp.route("/matches")
@member_required()
def matches():
    me = H.current_user()
    rows = db().execute(
        "SELECT u.id AS id, u.name AS name, u.country AS country, u.city AS city,"
        " u.share_location AS share_location FROM matches m JOIN users u"
        " ON u.id = CASE WHEN m.user_a=? THEN m.user_b ELSE m.user_a END"
        " WHERE (m.user_a=? OR m.user_b=?) AND u.banned=0 ORDER BY m.id DESC",
        (me["id"], me["id"], me["id"])).fetchall()
    return render_template_string(T["matches.html"], rows=rows)


# --------------------------------------------------------------------------
# Demo forex trading (simulated prices, virtual money)
# --------------------------------------------------------------------------
LEVERAGE = 100
SYMBOLS = {
    "EURUSD": dict(base=1.0850, vol=0.0012, spread=0.00012, digits=5, contract=100000, kind="usdq"),
    "GBPUSD": dict(base=1.2700, vol=0.0016, spread=0.00015, digits=5, contract=100000, kind="usdq"),
    "USDJPY": dict(base=151.20, vol=0.15, spread=0.015, digits=3, contract=100000, kind="jpy"),
    "XAUUSD": dict(base=2650.0, vol=8.0, spread=0.25, digits=2, contract=100, kind="usdq"),
    "BTCUSD": dict(base=68000.0, vol=600.0, spread=30.0, digits=2, contract=1, kind="usdq"),
}


def mid(sym, t):
    """Deterministic simulated price so every server instance agrees."""
    s = SYMBOLS[sym]
    seed = int(hashlib.md5(sym.encode()).hexdigest()[:4], 16)
    x = t + seed * 97
    wave = (0.5 * math.sin(x / 2400) + 0.3 * math.sin(x / 700 + 1.3)
            + 0.2 * math.sin(x / 150 + 2.1) + 0.1 * math.sin(x / 37 + 0.5))
    n = int(hashlib.md5(f"{sym}{int(t)}".encode()).hexdigest()[:6], 16) / 0xFFFFFF - 0.5
    return s["base"] + s["vol"] * (wave + 0.08 * n)


def quote(sym, t=None):
    t = time.time() if t is None else t
    s = SYMBOLS[sym]
    m, half = mid(sym, t), s["spread"] / 2
    return round(m - half, s["digits"]), round(m + half, s["digits"])


def floating(tr, bid, ask):
    s = SYMBOLS[tr["symbol"]]
    cp = bid if tr["side"] == "buy" else ask
    diff = cp - tr["open_price"] if tr["side"] == "buy" else tr["open_price"] - cp
    val = diff * tr["lots"] * s["contract"]
    if s["kind"] == "jpy":
        val /= cp
    return round(val, 2), cp


def margin_for(sym, lots, price):
    s = SYMBOLS[sym]
    notional = lots * s["contract"] * (1 if s["kind"] == "jpy" else price)
    return round(notional / LEVERAGE, 2)


def settle(me):
    """Auto-close trades that hit stop loss / take profit. Returns (open trades, balance)."""
    d = db()
    rows = d.execute("SELECT * FROM trades WHERE user_id=? AND status='open' ORDER BY id",
                     (me["id"],)).fetchall()
    bal, changed, out = float(me["demo_balance"]), False, []
    for tr in rows:
        bid, ask = quote(tr["symbol"])
        pnl, cp = floating(tr, bid, ask)
        sl, tp = tr["sl"], tr["tp"]
        if tr["side"] == "buy":
            hit = (sl is not None and bid <= sl) or (tp is not None and bid >= tp)
        else:
            hit = (sl is not None and ask >= sl) or (tp is not None and ask <= tp)
        if hit:
            d.execute("UPDATE trades SET status='closed', close_price=?, pnl=?, closed_at=? WHERE id=?",
                      (cp, pnl, H.now(), tr["id"]))
            bal += pnl
            changed = True
        else:
            out.append(dict(tr, pnl=pnl, price=cp, margin=margin_for(tr["symbol"], tr["lots"], tr["open_price"])))
    if changed:
        d.execute("UPDATE users SET demo_balance=? WHERE id=?", (round(bal, 2), me["id"]))
        d.commit()
    return out, round(bal, 2)


@bp.route("/trade")
@member_required()
def trade():
    return render_template_string(T["trade.html"], symbols={k: v["digits"] for k, v in SYMBOLS.items()})


@bp.route("/api/trade/state")
@member_required(api=True)
def trade_state():
    me = H.current_user()
    open_, bal = settle(me)
    equity = round(bal + sum(t["pnl"] for t in open_), 2)
    used = round(sum(t["margin"] for t in open_), 2)
    hist = db().execute("SELECT id,symbol,side,lots,open_price,close_price,pnl FROM trades"
                        " WHERE user_id=? AND status='closed' ORDER BY id DESC LIMIT 15",
                        (me["id"],)).fetchall()
    prices = {s: dict(zip(("bid", "ask"), quote(s))) for s in SYMBOLS}
    return jsonify(ok=True, prices=prices, balance=bal, equity=equity, margin=used,
                   free=round(equity - used, 2), leverage=LEVERAGE,
                   open=[{k: t[k] for k in ("id", "symbol", "side", "lots", "open_price", "sl", "tp", "pnl", "price")}
                         for t in open_],
                   history=[dict(h) for h in hist])


@bp.route("/api/trade/candles")
@member_required(api=True)
def trade_candles():
    sym = request.args.get("symbol", "EURUSD")
    if sym not in SYMBOLS:
        return jsonify(ok=False), 400
    now_t = time.time()
    first = int(now_t // 60) - 59
    candles = []
    for m in range(first, first + 60):
        pts = [mid(sym, min(now_t, m * 60 + k * 6)) for k in range(10)]
        candles.append(dict(o=pts[0], h=max(pts), l=min(pts), c=pts[-1]))
    return jsonify(ok=True, candles=candles)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        raise ValueError("Stop loss / take profit must be numbers.")


@bp.route("/api/trade/open", methods=["POST"])
@member_required(api=True)
def trade_open():
    me = H.current_user()
    f = request.form
    sym, side = f.get("symbol"), f.get("side")
    if sym not in SYMBOLS or side not in ("buy", "sell"):
        return jsonify(ok=False, error="Invalid order."), 400
    try:
        lots = round(float(f.get("lots", "")), 2)
        sl, tp = _num(f.get("sl")), _num(f.get("tp"))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc) if "Stop" in str(exc) else "Lots must be a number."), 400
    if not 0.01 <= lots <= 5:
        return jsonify(ok=False, error="Lots must be between 0.01 and 5."), 400
    bid, ask = quote(sym)
    price = ask if side == "buy" else bid
    if side == "buy" and ((sl is not None and sl >= price) or (tp is not None and tp <= price)):
        return jsonify(ok=False, error="For a buy: stop loss below and take profit above the price."), 400
    if side == "sell" and ((sl is not None and sl <= price) or (tp is not None and tp >= price)):
        return jsonify(ok=False, error="For a sell: stop loss above and take profit below the price."), 400
    open_, bal = settle(me)
    if len(open_) >= 10:
        return jsonify(ok=False, error="Maximum 10 open positions."), 400
    equity = bal + sum(t["pnl"] for t in open_)
    free = equity - sum(t["margin"] for t in open_)
    need = margin_for(sym, lots, price)
    if need > free:
        return jsonify(ok=False, error=f"Not enough free margin. Need {need}, have {round(free, 2)}."), 400
    db().execute("INSERT INTO trades(user_id,symbol,side,lots,open_price,sl,tp,opened_at)"
                 " VALUES(?,?,?,?,?,?,?,?)", (me["id"], sym, side, lots, price, sl, tp, H.now()))
    db().commit()
    return jsonify(ok=True)


@bp.route("/api/trade/close", methods=["POST"])
@member_required(api=True)
def trade_close():
    me = H.current_user()
    tr = db().execute("SELECT * FROM trades WHERE id=? AND user_id=? AND status='open'",
                      (request.form.get("id", type=int), me["id"])).fetchone()
    if not tr:
        return jsonify(ok=False, error="Position not found."), 404
    bid, ask = quote(tr["symbol"])
    pnl, cp = floating(tr, bid, ask)
    db().execute("UPDATE trades SET status='closed', close_price=?, pnl=?, closed_at=? WHERE id=?",
                 (cp, pnl, H.now(), tr["id"]))
    db().execute("UPDATE users SET demo_balance=ROUND(demo_balance + ?, 2) WHERE id=?",
                 (pnl, me["id"]))
    db().commit()
    return jsonify(ok=True, pnl=pnl)


@bp.route("/api/trade/reset", methods=["POST"])
@member_required(api=True)
def trade_reset():
    me = H.current_user()
    db().execute("DELETE FROM trades WHERE user_id=?", (me["id"],))
    db().execute("UPDATE users SET demo_balance=10000 WHERE id=?", (me["id"],))
    db().commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------
T = {}

T["profile.html"] = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Your profile</h2>
<p class="mut">You must be 18+. Only opt-in details are shown to others, and location is approximate (about 11 km).</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf_token() }}">
<label>Country</label><select name="country" required>{% for c in COUNTRIES %}<option {{ 'selected' if u['country']==c }}>{{ c }}</option>{% endfor %}</select>
<label>City</label><input name="city" value="{{ u['city'] or '' }}" maxlength="60">
<label>I am</label><select name="gender" required>{% for g in ('male','female','other') %}<option value="{{ g }}" {{ 'selected' if u['gender']==g }}>{{ g|capitalize }}</option>{% endfor %}</select>
<label>I want to meet</label><select name="looking_for">{% for g,l in (('any','Anyone'),('male','Men'),('female','Women')) %}<option value="{{ g }}" {{ 'selected' if u['looking_for']==g }}>{{ l }}</option>{% endfor %}</select>
<label>About me (300 characters, no links)</label><input name="bio" value="{{ u['bio'] or '' }}" maxlength="300">
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="share_location" id="share" style="width:auto" {{ 'checked' if u['share_location'] }}> Show my country, city and approximate location in the Community</label>
<input type="hidden" name="lat" id="lat" value="{{ u['lat'] if u['lat'] is not none else '' }}"><input type="hidden" name="lon" id="lon" value="{{ u['lon'] if u['lon'] is not none else '' }}">
<button type="button" class="btn sm" onclick="geo()">Use my approximate location</button> <span id="geomsg" class="mut"></span>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="dating_enabled" style="width:auto" {{ 'checked' if u['dating_enabled'] }}> Join the dating section</label>
<button class="btn">Save profile</button></form></div>
<script>function geo(){if(!navigator.geolocation){document.getElementById('geomsg').textContent='Not supported';return}
navigator.geolocation.getCurrentPosition(p=>{document.getElementById('lat').value=p.coords.latitude.toFixed(1);document.getElementById('lon').value=p.coords.longitude.toFixed(1);document.getElementById('geomsg').textContent='Saved (rounded). Press Save profile.'},()=>{document.getElementById('geomsg').textContent='Permission denied'})}</script>
{% endblock %}"""

T["community.html"] = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Community insights</h2>
<div class="grid"><div class="stat"><b>{{ insights.total }}</b>Sharing members</div><div class="stat"><b>{{ insights.africa }}</b>From Africa</div><div class="stat"><b>{{ insights.world }}</b>From elsewhere</div></div>
{% if insights.top %}<p class="mut">Top countries: {% for c,n in insights.top %}{{ c }} ({{ n }}){{ ', ' if not loop.last }}{% endfor %}</p>{% endif %}
<a class="btn sm" href="?region=all">All</a> <a class="btn sm" href="?region=africa">Africans</a> <a class="btn sm" href="?region=world">Foreigners</a></div>
{% for p in people %}<div class="card"><b>{{ p.name }}</b>{% if p.age %}, {{ p.age }}{% endif %}
<div class="mut">{{ p.city or '' }}{{ ', ' if p.city }}{{ p.country }}{% if p.lat is not none %} &middot; <a href="https://www.openstreetmap.org/?mlat={{ p.lat }}&mlon={{ p.lon }}#map=9/{{ p.lat }}/{{ p.lon }}" target="_blank" rel="noopener">approx. map</a>{% endif %}</div>
<p>{{ p.bio }}</p><a class="btn sm" href="{{ url_for('hub.chat_dm', uid=p.id) }}">Message</a></div>
{% else %}<div class="card mut">No members to show yet. Turn on location sharing in your Profile to appear here.</div>{% endfor %}
{% endblock %}"""

T["chat.html"] = """{% extends 'base.html' %}{% block body %}
<style>#box{height:55vh;overflow-y:auto;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fff}
.msg{margin-bottom:10px}.msg small{color:var(--mut)}.msg button{background:none;border:0;color:var(--mut);cursor:pointer;font-size:.75rem}
.badge{background:var(--brand);color:#fff;border-radius:6px;padding:0 6px;font-size:.7rem;margin-left:4px}
.rooms a{margin-right:8px;display:inline-block;margin-bottom:6px}</style>
<div class="card"><div class="rooms">{% for k,v in rooms.items() %}<a class="btn sm" href="{{ url_for('hub.chat_room', room=k) }}">{{ v }}</a>{% endfor %}</div>
<h2>{{ title }}</h2><p class="mut">Be respectful. No links, no money requests. Never share passwords or send money to strangers. Messages are moderated.</p>
<div id="box"></div>
<form id="f" style="display:flex;gap:8px;margin-top:10px"><input id="body" maxlength="500" autocomplete="off" placeholder="Type a message" required><button class="btn" style="margin:0">Send</button></form>
<div id="err" style="color:var(--bad);margin-top:6px"></div></div>
<script>
const room={{ room|tojson }}, csrf={{ csrf_token()|tojson }}; let after=0, me=0;
const box=document.getElementById('box');
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
async function post(u,d){const b=new URLSearchParams(d);b.append('csrf',csrf);try{const r=await fetch(u,{method:'POST',body:b});return await r.json()}catch(e){return{ok:false,error:'Request failed'}}}
function render(m){const d=el('div','msg');const h=el('div');h.appendChild(el('strong','',m.name));
if(m.mod)h.appendChild(el('span','badge','Mod'));h.appendChild(el('small','',' '+(m.country?m.country+' · ':'')+m.time+' UTC'));d.appendChild(h);d.appendChild(el('div','',m.body));
if(m.user_id!==me){const r=el('button','','Report');r.onclick=async()=>{const why=prompt('Why are you reporting this?');if(why===null)return;const x=await post('/api/chat/report',{message_id:m.id,reason:why});alert(x.ok?'Reported. Thank you.':x.error)};
const b=el('button','','Block');b.onclick=async()=>{if(confirm('Block '+m.name+'?')){await post('/api/block',{user_id:m.user_id});location.reload()}};d.appendChild(r);d.appendChild(b)}
return d}
async function poll(){try{const r=await fetch('/api/chat/poll?room='+encodeURIComponent(room)+'&after='+after);const d=await r.json();if(!d.ok)return;me=d.me;
const stick=box.scrollHeight-box.scrollTop-box.clientHeight<80;after=Math.max(after,d.last);d.messages.forEach(m=>box.appendChild(render(m)));if(d.messages.length&&stick)box.scrollTop=box.scrollHeight}catch(e){}}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const i=document.getElementById('body');const x=await post('/api/chat/send',{room,body:i.value});
document.getElementById('err').textContent=x.ok?'':x.error;if(x.ok){i.value='';poll()}};
poll();setInterval(poll,2000);
</script>{% endblock %}"""

T["discover.html"] = """{% extends 'base.html' %}{% block body %}
{% if card %}<div class="card" style="text-align:center"><h1>{{ card.name }}{% if card.age %}, {{ card.age }}{% endif %}</h1>
<div class="mut">{{ card.gender|capitalize }}{% if card.share_location %} &middot; {{ card.city or '' }}{{ ', ' if card.city }}{{ card.country }}{% endif %}</div>
<p>{{ card.bio or 'No bio yet.' }}</p>
<form method="post" action="{{ url_for('hub.swipe') }}" style="display:flex;gap:12px;justify-content:center"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input type="hidden" name="target" value="{{ card.id }}">
<button class="btn red" name="action" value="pass">Pass</button><button class="btn green" name="action" value="like">Like</button></form>
<form method="post" action="{{ url_for('hub.report_user') }}" style="margin-top:14px"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input type="hidden" name="user_id" value="{{ card.id }}"><input type="hidden" name="reason" value="Reported from dating"><button class="btn sm" style="background:none;color:var(--mut)">Report this profile</button></form></div>
{% else %}<div class="card"><h2>No more profiles right now</h2><p class="mut">Check back later, or widen who you want to meet in your Profile.</p></div>{% endif %}
<p><a class="btn sm" href="{{ url_for('hub.matches') }}">My matches</a></p>
<div class="card mut">Safety: meet in public places, tell a friend, and never send money to someone you met online.</div>
{% endblock %}"""

T["matches.html"] = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Your matches</h2>{% for r in rows %}<p><b>{{ r.name }}</b> <span class="mut">{% if r.share_location %}{{ r.city or '' }}{{ ', ' if r.city }}{{ r.country }}{% endif %}</span>
<a class="btn sm" href="{{ url_for('hub.chat_dm', uid=r.id) }}">Message</a></p>{% else %}<p class="mut">No matches yet. Keep swiping.</p>{% endfor %}</div>{% endblock %}"""

T["moderation.html"] = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2>Open reports</h2>{% for r in reports %}<div style="border-bottom:1px solid var(--line);padding:10px 0">
<b>{{ r.reported_name }}</b> reported by {{ r.reporter_name }} <span class="mut">{{ r.created_at }}</span><br>Reason: {{ r.reason }}
{% if r.msg_body %}<br><i>"{{ r.msg_body }}"</i>{{ ' (deleted)' if r.msg_deleted }}{% endif %}<br>
{% for a,c in (('dismiss',''),('delete',''),('mute',''),('ban','red')) %}<form method="post" action="{{ url_for('hub.admin_mod_action', rid=r.id, action=a) }}" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn sm {{ c }}">{{ {'dismiss':'Dismiss','delete':'Delete message','mute':'Mute 24h','ban':'Ban user'}[a] }}</button></form> {% endfor %}</div>
{% else %}<p class="mut">No open reports.</p>{% endfor %}</div>
<div class="card"><h2>Muted / banned</h2>{% for s in sanctioned %}<p>{{ s.name }} ({{ s.email }}) {{ 'BANNED' if s.banned else 'muted until ' ~ s.muted_until }}
<form method="post" action="{{ url_for('hub.admin_unban', uid=s.id) }}" style="display:inline"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn sm green">Restore</button></form></p>{% else %}<p class="mut">None.</p>{% endfor %}</div>
{% endblock %}"""

T["trade.html"] = """{% extends 'base.html' %}{% block body %}
<style>.pos{color:var(--ok)}.neg{color:var(--bad)}canvas{width:100%;height:auto;background:#fff;border:1px solid var(--line);border-radius:10px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:end}.row>div{flex:1;min-width:90px}select{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}</style>
<div class="row" style="margin-bottom:14px"><button class="btn" style="margin:0" id="tdemo" onclick="tab('demo')">Demo account</button>
<button class="btn" style="margin:0;background:var(--mut)" id="treal" onclick="tab('real')">Real account</button></div>
<div id="real" style="display:none"><div class="card"><h2>Trade with real money</h2>
<p>Apex Company does <b>not</b> hold client funds or place real trades. Real accounts (including MetaTrader 5) are opened with a <b>licensed broker</b>, and you deposit <b>directly to your own broker account</b>, never to Apex or its administrator.</p>
<a class="btn" href="{{ REAL_BROKER_URL }}" target="_blank" rel="noopener">Open a real account with our broker partner</a>
<p class="mut">Forex and CFDs are high risk and most retail traders lose money. Only trade what you can afford to lose. Practise on the demo account first. Questions? WhatsApp <a href="https://wa.me/{{ SUPPORT_WHATSAPP }}" target="_blank" rel="noopener">+{{ SUPPORT_WHATSAPP }}</a>.</p></div></div>
<div id="demo"><div class="card" style="background:#fffbeb;border-color:#fde68a"><b>Demo account.</b> Prices are simulated and the money is virtual. Nothing here is real trading or financial advice.</div>
<div class="card"><div class="grid"><div class="stat"><b id="bal">-</b>Balance</div><div class="stat"><b id="eq">-</b>Equity</div><div class="stat"><b id="free">-</b>Free margin</div></div></div>
<div class="card"><div class="row"><div><label>Symbol</label><select id="symbol">{% for s in symbols %}<option>{{ s }}</option>{% endfor %}</select></div>
<div><label>Lots</label><input id="lots" value="0.10"></div><div><label>Stop loss</label><input id="sl" placeholder="optional"></div><div><label>Take profit</label><input id="tp" placeholder="optional"></div></div>
<p class="big"><span id="bid">-</span> / <span id="ask">-</span> <span class="mut">(sell / buy)</span></p>
<canvas id="chart" width="760" height="300"></canvas>
<div class="row" style="margin-top:8px"><button class="btn red" style="margin:0" onclick="order('sell')">Sell</button><button class="btn green" style="margin:0" onclick="order('buy')">Buy</button>
<button class="btn sm" style="background:var(--mut);margin:0" onclick="resetDemo()">Reset demo account</button></div><div id="msg" style="margin-top:6px;color:var(--bad)"></div></div>
<div class="card"><h2>Open positions</h2><table id="open"></table></div>
<div class="card"><h2>Recent closed trades</h2><table id="hist"></table></div></div>
<script>
function tab(w){document.getElementById('demo').style.display=w==='demo'?'':'none';document.getElementById('real').style.display=w==='real'?'':'none';
document.getElementById('tdemo').style.background=w==='demo'?'':'var(--mut)';document.getElementById('treal').style.background=w==='real'?'':'var(--mut)'}
const csrf={{ csrf_token()|tojson }}, digits={{ symbols|tojson }};
const sel=document.getElementById('symbol');
async function post(u,d){const b=new URLSearchParams(d);b.append('csrf',csrf);try{return await (await fetch(u,{method:'POST',body:b})).json()}catch(e){return{ok:false,error:'Request failed'}}}
function fmt(v,d){return Number(v).toFixed(d)}
function table(id,head,rows){const t=document.getElementById(id);t.innerHTML='';const h=t.insertRow();head.forEach(x=>{const c=document.createElement('th');c.textContent=x;h.appendChild(c)});
rows.forEach(r=>{const tr=t.insertRow();r.forEach(v=>{const c=tr.insertCell();if(v instanceof Node)c.appendChild(v);else{c.textContent=v.t!==undefined?v.t:v;if(v.cls)c.className=v.cls}})})}
function draw(c){const cv=document.getElementById('chart'),x=cv.getContext('2d'),W=cv.width,Hh=cv.height;x.clearRect(0,0,W,Hh);if(!c.length)return;
let lo=Math.min(...c.map(k=>k.l)),hi=Math.max(...c.map(k=>k.h));const pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;const Y=v=>Hh-(v-lo)/(hi-lo)*Hh,bw=W/c.length;
c.forEach((k,i)=>{const up=k.c>=k.o;x.strokeStyle=x.fillStyle=up?'#047857':'#b91c1c';const cx=i*bw+bw/2;x.beginPath();x.moveTo(cx,Y(k.h));x.lineTo(cx,Y(k.l));x.stroke();
x.fillRect(i*bw+bw*0.2,Y(Math.max(k.o,k.c)),bw*0.6,Math.max(1,Math.abs(Y(k.o)-Y(k.c))))});}
async function refresh(){const s=sel.value;const [st,cd]=await Promise.all([fetch('/api/trade/state').then(r=>r.json()),fetch('/api/trade/candles?symbol='+s).then(r=>r.json())]).catch(()=>[{},{}]);
if(!st.ok)return;const d=digits[s];document.getElementById('bid').textContent=fmt(st.prices[s].bid,d);document.getElementById('ask').textContent=fmt(st.prices[s].ask,d);
document.getElementById('bal').textContent='$'+fmt(st.balance,2);document.getElementById('eq').textContent='$'+fmt(st.equity,2);document.getElementById('free').textContent='$'+fmt(st.free,2);
if(cd.ok)draw(cd.candles);
table('open',['Symbol','Side','Lots','Open','P/L',''],st.open.map(t=>{const b=document.createElement('button');b.className='btn sm';b.textContent='Close';b.onclick=async()=>{await post('/api/trade/close',{id:t.id});refresh()};
return [t.symbol,t.side,t.lots,fmt(t.open_price,digits[t.symbol]),{t:'$'+fmt(t.pnl,2),cls:t.pnl>=0?'pos':'neg'},b]}));
table('hist',['Symbol','Side','Lots','Open','Close','P/L'],st.history.map(t=>[t.symbol,t.side,t.lots,fmt(t.open_price,digits[t.symbol]),fmt(t.close_price,digits[t.symbol]),{t:'$'+fmt(t.pnl,2),cls:t.pnl>=0?'pos':'neg'}]))}
async function order(side){const x=await post('/api/trade/open',{symbol:sel.value,side,lots:document.getElementById('lots').value,sl:document.getElementById('sl').value,tp:document.getElementById('tp').value});
document.getElementById('msg').textContent=x.ok?'':x.error;refresh()}
async function resetDemo(){if(confirm('Reset to $10,000 and delete all trades?')){await post('/api/trade/reset',{});refresh()}}
sel.onchange=refresh;refresh();setInterval(refresh,3000);
</script>{% endblock %}"""


def register(app, helpers):
    """Called once by app.py."""
    global H
    H = helpers
    H.TEMPLATES.update(T)
    app.register_blueprint(bp)
