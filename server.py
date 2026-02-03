import sqlite3
import logging
from flask import Flask, render_template, request, jsonify, g
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests as http_requests

app = Flask(__name__)
DB_PATH = "automation.db"
scheduler = BackgroundScheduler()
log = logging.getLogger("automation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# --- Database ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            time TEXT NOT NULL,
            days TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
            action TEXT NOT NULL DEFAULT 'on',
            relay INTEGER NOT NULL DEFAULT 1,
            strip INTEGER NOT NULL DEFAULT 1,
            brightness INTEGER NOT NULL DEFAULT 255,
            color TEXT NOT NULL DEFAULT '#ffffff',
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db.execute(
        "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
        ("esp32_url", "http://192.168.1.100"),
    )
    db.commit()
    db.close()


def get_config(key):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    db.close()
    return row["value"] if row else None


# --- ESP32 Control ---

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def execute_schedule(schedule_id):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    db.close()
    if not row:
        log.warning("Schedule %d not found", schedule_id)
        return

    schedule = dict(row)
    base_url = get_config("esp32_url")
    if not base_url:
        log.error("ESP32 URL not configured")
        return

    log.info("Executing schedule '%s' (action=%s)", schedule["name"], schedule["action"])

    if schedule["relay"]:
        _set_relay(base_url, schedule["action"] == "on")

    if schedule["strip"]:
        _set_strip(base_url, schedule)


def _set_relay(base_url, turn_on):
    try:
        resp = http_requests.get(f"{base_url}/relaystatus", timeout=5)
        current = resp.text.strip()
        desired = "ON" if turn_on else "OFF"
        if current != desired:
            http_requests.get(f"{base_url}/relay", timeout=5)
            log.info("Relay toggled to %s", desired)
        else:
            log.info("Relay already %s", desired)
    except http_requests.RequestException as e:
        log.error("Relay control failed: %s", e)


def _set_strip(base_url, schedule):
    try:
        if schedule["action"] == "on":
            color = schedule["color"].lstrip("#")
            r = int(color[0:2], 16)
            g_val = int(color[2:4], 16)
            b = int(color[4:6], 16)
            params = {
                "on": 1,
                "brightness": schedule["brightness"],
                "r": r,
                "g": g_val,
                "b": b,
                "mode": "solid",
            }
        else:
            params = {"on": 0}
        http_requests.get(f"{base_url}/strip", params=params, timeout=5)
        log.info("Strip set: %s", params)
    except http_requests.RequestException as e:
        log.error("Strip control failed: %s", e)


# --- Scheduler ---

def schedule_job(schedule):
    job_id = f"schedule_{schedule['id']}"
    hour, minute = schedule["time"].split(":")
    day_nums = [int(d) for d in schedule["days"].split(",")]
    day_names = ",".join(DAY_NAMES[d] for d in day_nums)
    trigger = CronTrigger(day_of_week=day_names, hour=int(hour), minute=int(minute))

    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    if schedule["enabled"]:
        scheduler.add_job(
            execute_schedule,
            trigger,
            id=job_id,
            args=[schedule["id"]],
            replace_existing=True,
        )
        log.info("Scheduled job '%s' at %s on %s", schedule["name"], schedule["time"], day_names)


def load_all_schedules():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM schedules").fetchall()
    db.close()
    for row in rows:
        schedule_job(dict(row))


# --- API Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/schedules", methods=["GET"])
def list_schedules():
    db = get_db()
    rows = db.execute("SELECT * FROM schedules ORDER BY time").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    data = request.json
    db = get_db()
    cur = db.execute(
        """INSERT INTO schedules (name, time, days, action, relay, strip, brightness, color, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["name"],
            data["time"],
            data.get("days", "0,1,2,3,4,5,6"),
            data.get("action", "on"),
            data.get("relay", 1),
            data.get("strip", 1),
            data.get("brightness", 255),
            data.get("color", "#ffffff"),
            data.get("enabled", 1),
        ),
    )
    db.commit()
    schedule = dict(
        db.execute("SELECT * FROM schedules WHERE id = ?", (cur.lastrowid,)).fetchone()
    )
    schedule_job(schedule)
    return jsonify(schedule), 201


@app.route("/api/schedules/<int:sid>", methods=["PUT"])
def update_schedule(sid):
    data = request.json
    db = get_db()
    db.execute(
        """UPDATE schedules SET name=?, time=?, days=?, action=?, relay=?, strip=?,
           brightness=?, color=?, enabled=? WHERE id=?""",
        (
            data["name"],
            data["time"],
            data.get("days", "0,1,2,3,4,5,6"),
            data.get("action", "on"),
            data.get("relay", 1),
            data.get("strip", 1),
            data.get("brightness", 255),
            data.get("color", "#ffffff"),
            data.get("enabled", 1),
            sid,
        ),
    )
    db.commit()
    schedule = dict(
        db.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    )
    schedule_job(schedule)
    return jsonify(schedule)


@app.route("/api/schedules/<int:sid>", methods=["DELETE"])
def delete_schedule(sid):
    db = get_db()
    db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
    db.commit()
    job_id = f"schedule_{sid}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    return "", 204


@app.route("/api/config", methods=["GET"])
def get_config_route():
    db = get_db()
    rows = db.execute("SELECT * FROM config").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/config", methods=["POST"])
def save_config():
    data = request.json
    db = get_db()
    for key, value in data.items():
        db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
        )
    db.commit()
    return jsonify(data)


@app.route("/api/test/<action>", methods=["POST"])
def test_action(action):
    base_url = get_config("esp32_url")
    if not base_url:
        return jsonify({"error": "ESP32 URL not configured"}), 400

    data = request.json or {}
    schedule = {
        "action": action,
        "relay": data.get("relay", 1),
        "strip": data.get("strip", 1),
        "brightness": data.get("brightness", 255),
        "color": data.get("color", "#ffffff"),
    }

    if schedule["relay"]:
        _set_relay(base_url, action == "on")
    if schedule["strip"]:
        _set_strip(base_url, schedule)

    return jsonify({"ok": True, "action": action})


if __name__ == "__main__":
    init_db()
    load_all_schedules()
    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        scheduler.shutdown()
