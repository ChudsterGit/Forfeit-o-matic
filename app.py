"""Forfeit-o-matic: stop the counter as close to 1000 as you can.

Run:  python app.py      (then open http://<your-ip>:5000 on phones / desktop)
"""
import os
import random
import socket
import sqlite3
import threading
import time

from flask import Flask, g, jsonify, redirect, render_template, request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forfeit.db")
TARGET = 1000
MAX_VALUE = 2000
MIN_SECS, MAX_SECS = 3.0, 5.0   # time for the counter to travel 1 -> 1000
TOLERANCE_SECS = 0.4            # allowed client/server clock disagreement on stop
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

app = Flask(__name__)
lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS players(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS games(
    code TEXT PRIMARY KEY,
    created REAL NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    lead_id TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0,
    next_code TEXT);
CREATE TABLE IF NOT EXISTS game_players(
    code TEXT NOT NULL,
    player_id TEXT NOT NULL,
    joined REAL NOT NULL,
    score INTEGER,
    started_at REAL,
    duration REAL,
    PRIMARY KEY(code, player_id));
CREATE TABLE IF NOT EXISTS results(
    player_id TEXT NOT NULL,
    code TEXT NOT NULL,
    round INTEGER NOT NULL,
    score INTEGER NOT NULL,
    diff INTEGER NOT NULL,
    outcome TEXT,
    at REAL NOT NULL);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def value_at(elapsed, duration):
    """Counter value after `elapsed` seconds; rises ~linearly, capped at 2000."""
    return max(1, min(MAX_VALUE, int(round(1 + 999 * elapsed / duration))))


def fail(msg, code=400):
    return jsonify(error=msg), code


def new_code(conn):
    while True:
        code = "".join(random.choice(CODE_CHARS) for _ in range(4))
        if not conn.execute("SELECT 1 FROM games WHERE code=?", (code,)).fetchone():
            return code


def create_game(conn, lead_id):
    code = new_code(conn)
    now = time.time()
    conn.execute("INSERT INTO games(code,created,lead_id) VALUES(?,?,?)", (code, now, lead_id))
    conn.execute("INSERT INTO game_players(code,player_id,joined) VALUES(?,?,?)", (code, lead_id, now))
    return code


def settle(conn, code):
    """Auto-stop anyone whose counter has run off the end, then finalise if all are done."""
    now = time.time()
    rows = conn.execute(
        "SELECT player_id, started_at, duration FROM game_players "
        "WHERE code=? AND score IS NULL AND started_at IS NOT NULL", (code,)).fetchall()
    for r in rows:
        if value_at(now - r["started_at"], r["duration"]) >= MAX_VALUE:
            conn.execute("UPDATE game_players SET score=? WHERE code=? AND player_id=?",
                         (MAX_VALUE, code, r["player_id"]))
    game = conn.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
    if not game["finalized"]:
        pending = conn.execute(
            "SELECT COUNT(*) FROM game_players WHERE code=? AND score IS NULL", (code,)).fetchone()[0]
        if pending == 0:
            finalize(conn, code)
    conn.commit()


def finalize(conn, code):
    """Record round results (once) and mark the game round as finished."""
    game = conn.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
    if game["finalized"]:
        return
    rows = conn.execute(
        "SELECT player_id, score FROM game_players WHERE code=? AND score IS NOT NULL", (code,)).fetchall()
    diffs = {r["player_id"]: abs(r["score"] - TARGET) for r in rows}
    best = min(diffs.values()) if diffs else None
    worst = max(diffs.values()) if diffs else None
    now = time.time()
    for r in rows:
        d = diffs[r["player_id"]]
        outcome = None
        if len(rows) > 1 and best != worst:
            outcome = "win" if d == best else "lose" if d == worst else None
        conn.execute("INSERT INTO results VALUES(?,?,?,?,?,?,?)",
                     (r["player_id"], code, game["round"], r["score"], d, outcome, now))
    conn.execute("UPDATE games SET finalized=1 WHERE code=?", (code,))


def game_state(conn, code, pid):
    game = conn.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
    rows = conn.execute(
        "SELECT gp.*, p.name FROM game_players gp JOIN players p ON p.id=gp.player_id "
        "WHERE gp.code=? ORDER BY gp.joined", (code,)).fetchall()
    now = time.time()
    players, me = [], None
    for r in rows:
        if r["score"] is not None:
            status = "done"
        elif r["started_at"] is not None:
            status = "running"
        else:
            status = "waiting"
        p = {"id": r["player_id"], "name": r["name"], "is_lead": r["player_id"] == game["lead_id"],
             "status": status, "score": r["score"],
             "diff": abs(r["score"] - TARGET) if r["score"] is not None else None}
        players.append(p)
        if r["player_id"] == pid:
            me = {"status": status, "score": r["score"], "duration": r["duration"],
                  "elapsed": (now - r["started_at"]) if r["started_at"] is not None else None}
    scored = [p for p in players if p["score"] is not None]
    winner = loser = None
    if len(scored) > 1:
        lo = min(p["diff"] for p in scored)
        hi = max(p["diff"] for p in scored)
        if lo != hi:
            winner = [p["id"] for p in scored if p["diff"] == lo]
            loser = [p["id"] for p in scored if p["diff"] == hi]
    scored.sort(key=lambda p: p["diff"])
    for i, p in enumerate(scored):
        p["rank"] = i + 1
    return {"code": code, "round": game["round"], "lead_id": game["lead_id"],
            "finalized": bool(game["finalized"]), "next_code": game["next_code"],
            "players": players, "me": me, "winner": winner, "loser": loser,
            "total": len(players), "done": len(scored)}


# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    ua = request.headers.get("User-Agent", "").lower()
    mobile = any(k in ua for k in ("android", "iphone", "ipad", "mobile"))
    return redirect("/m" if mobile else "/d")


@app.route("/m")
def mobile():
    return render_template("index.html", layout="mobile")


@app.route("/d")
def desktop():
    return render_template("index.html", layout="desktop")


# ---------------------------------------------------------------- API

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:20]
    if not name:
        return fail("Please enter a name.")
    with lock:
        conn = db()
        pid = data.get("pid")
        existing = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone() if pid else None
        clash = conn.execute("SELECT id FROM players WHERE name=?", (name,)).fetchone()
        if clash and (not existing or clash["id"] != existing["id"]):
            return fail("That name is already taken - pick another.", 409)
        if existing:
            conn.execute("UPDATE players SET name=? WHERE id=?", (name, pid))
        else:
            pid = os.urandom(8).hex()
            conn.execute("INSERT INTO players VALUES(?,?,?)", (pid, name, time.time()))
        conn.commit()
    return jsonify(pid=pid, name=name)


@app.post("/api/me")
def me():
    """Lets a client check its stored identity still exists on the server."""
    data = request.get_json(silent=True) or {}
    row = db().execute("SELECT id,name FROM players WHERE id=?", (data.get("pid"),)).fetchone()
    if not row:
        return fail("Unknown player", 404)
    return jsonify(pid=row["id"], name=row["name"])


def require_player(data):
    return db().execute("SELECT * FROM players WHERE id=?", (data.get("pid"),)).fetchone()


@app.post("/api/create")
def api_create():
    data = request.get_json(silent=True) or {}
    with lock:
        conn = db()
        if not require_player(data):
            return fail("Unknown player", 404)
        code = create_game(conn, data["pid"])
        conn.commit()
    return jsonify(code=code)


@app.post("/api/join")
def api_join():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    with lock:
        conn = db()
        if not require_player(data):
            return fail("Unknown player", 404)
        if not conn.execute("SELECT 1 FROM games WHERE code=?", (code,)).fetchone():
            return fail("No game with that code.", 404)
        conn.execute("INSERT OR IGNORE INTO game_players(code,player_id,joined) VALUES(?,?,?)",
                     (code, data["pid"], time.time()))
        conn.commit()
    return jsonify(code=code)


@app.get("/api/game/<code>")
def api_game(code):
    code = code.upper()
    pid = request.args.get("pid")
    with lock:
        conn = db()
        if not conn.execute("SELECT 1 FROM games WHERE code=?", (code,)).fetchone():
            return fail("No game with that code.", 404)
        if not conn.execute("SELECT 1 FROM game_players WHERE code=? AND player_id=?", (code, pid)).fetchone():
            return fail("Not in this game.", 403)
        settle(conn, code)
        return jsonify(game_state(conn, code, pid))


@app.post("/api/start")
def api_start():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").upper()
    with lock:
        conn = db()
        row = conn.execute("SELECT * FROM game_players WHERE code=? AND player_id=?",
                           (code, data.get("pid"))).fetchone()
        game = conn.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
        if not row or not game:
            return fail("Not in this game.", 403)
        if game["finalized"]:
            return fail("This round is over.", 409)
        if row["started_at"] is not None:
            return fail("You've already had your go this round!", 409)
        duration = random.uniform(MIN_SECS, MAX_SECS)
        conn.execute("UPDATE game_players SET started_at=?, duration=? WHERE code=? AND player_id=?",
                     (time.time(), duration, code, data["pid"]))
        conn.commit()
    return jsonify(duration=duration)


@app.post("/api/stop")
def api_stop():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").upper()
    with lock:
        conn = db()
        row = conn.execute("SELECT * FROM game_players WHERE code=? AND player_id=?",
                           (code, data.get("pid"))).fetchone()
        if not row or row["started_at"] is None:
            return fail("You haven't started.", 409)
        if row["score"] is None:
            server_val = value_at(time.time() - row["started_at"], row["duration"])
            tol = 999 * TOLERANCE_SECS / row["duration"]
            try:
                client_val = int(data.get("value"))
            except (TypeError, ValueError):
                client_val = server_val
            score = client_val if abs(client_val - server_val) <= tol else server_val
            score = max(1, min(MAX_VALUE, score))
            conn.execute("UPDATE game_players SET score=? WHERE code=? AND player_id=?",
                         (score, code, data["pid"]))
            conn.commit()
        settle(conn, code)
        return jsonify(game_state(conn, code, data["pid"]))


@app.post("/api/reset")
def api_reset():
    """Lead only. mode='scores' keeps players; mode='new' makes a fresh game; mode='finish' ends the round."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").upper()
    mode = data.get("mode")
    with lock:
        conn = db()
        game = conn.execute("SELECT * FROM games WHERE code=?", (code,)).fetchone()
        if not game:
            return fail("No game with that code.", 404)
        if game["lead_id"] != data.get("pid"):
            return fail("Only the lead can do that.", 403)
        if mode == "scores":
            conn.execute("UPDATE game_players SET score=NULL, started_at=NULL, duration=NULL WHERE code=?", (code,))
            conn.execute("UPDATE games SET round=round+1, finalized=0 WHERE code=?", (code,))
            conn.commit()
            return jsonify(code=code)
        if mode == "new":
            new = create_game(conn, data["pid"])
            conn.execute("UPDATE games SET next_code=? WHERE code=?", (new, code))
            conn.commit()
            return jsonify(code=new)
        if mode == "finish":
            finalize(conn, code)
            conn.commit()
            return jsonify(code=code)
        return fail("Bad mode.")


@app.get("/api/leaderboard")
def api_leaderboard():
    rows = db().execute("""
        SELECT p.id, p.name,
               COUNT(r.player_id) AS rounds,
               SUM(r.outcome='win') AS wins,
               SUM(r.outcome='lose') AS losses,
               MIN(r.diff) AS best,
               AVG(r.diff) AS avg
        FROM players p JOIN results r ON r.player_id=p.id
        GROUP BY p.id""").fetchall()
    out = [{"id": r["id"], "name": r["name"], "rounds": r["rounds"], "wins": r["wins"] or 0,
            "losses": r["losses"] or 0, "best": r["best"], "avg": round(r["avg"], 1)} for r in rows]
    out.sort(key=lambda x: (-x["wins"], x["avg"], x["name"].lower()))
    return jsonify(out)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Forfeit-o-matic running.\n  Desktop: http://localhost:{port}/d\n"
          f"  Phones on the same Wi-Fi: http://{lan_ip()}:{port}/m\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
