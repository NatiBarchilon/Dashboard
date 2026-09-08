import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import requests
from botocore.config import Config
from flask import Flask, jsonify, redirect, render_template, request, session, url_for


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sensor-watch")
app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET", "development-only-change-me")

LOCK = threading.Lock()
STATE = {"checked_at": None, "systems": [], "error": None}
ALERTED = {}


@app.before_request
def require_dashboard_login():
    if request.path in ("/health", "/login"):
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        expected_user = os.getenv("DASHBOARD_USERNAME", "admin")
        expected_password = os.getenv("DASHBOARD_PASSWORD", "")
        valid_user = secrets.compare_digest(request.form.get("username", ""), expected_user)
        valid_password = bool(expected_password) and secrets.compare_digest(request.form.get("password", ""), expected_password)
        if valid_user and valid_password:
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "שם המשתמש או הסיסמה אינם נכונים"
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def sensor_config():
    raw = os.getenv("SENSOR_CONFIG_JSON", "")
    if raw:
        data = json.loads(raw)
        if not isinstance(data, list) or not data:
            raise ValueError("SENSOR_CONFIG_JSON must be a non-empty JSON array")
        return data
    raise ValueError("SENSOR_CONFIG_JSON is not configured")


def s3_client():
    kwargs = {
        "aws_access_key_id": os.environ["S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["S3_SECRET_ACCESS_KEY"],
        "region_name": os.getenv("S3_REGION", "eu-central-1"),
        "config": Config(signature_version=os.getenv("S3_SIGNATURE_VERSION", "s3v4")),
    }
    if os.getenv("S3_ENDPOINT_URL"):
        kwargs["endpoint_url"] = os.environ["S3_ENDPOINT_URL"]
    return boto3.client("s3", **kwargs)


def newest_object(client, bucket, prefix):
    newest = None
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/"):
                continue
            if newest is None or obj["LastModified"] > newest["LastModified"]:
                newest = obj
        if not page.get("IsTruncated"):
            return newest
        token = page["NextContinuationToken"]


def newest_sensor_object(client, bucket, root_prefix, now):
    """Check today's and yesterday's changing YYYY/MM/DD folders."""
    local_now = now.astimezone(ZoneInfo(os.getenv("DATA_TIMEZONE", "Asia/Jerusalem")))
    candidates = []
    for day in (local_now.date(), (local_now - timedelta(days=1)).date()):
        dated_prefix = f"{root_prefix.rstrip('/')}/{day:%Y/%m/%d}/"
        item = newest_object(client, bucket, dated_prefix)
        if item:
            candidates.append(item)
    return max(candidates, key=lambda item: item["LastModified"], default=None)


def send_alert(message):
    webhook = os.getenv("ALERT_WEBHOOK_URL")
    if webhook:
        response = requests.post(webhook, json={"text": message, "content": message}, timeout=15)
        response.raise_for_status()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID")
    if telegram_token and telegram_chat:
        response = requests.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={"chat_id": telegram_chat, "text": message}, timeout=15,
        )
        response.raise_for_status()


def run_check(send_notifications=True):
    now = datetime.now(timezone.utc)
    threshold = env_float("STALE_AFTER_HOURS", 2)
    bucket = os.environ.get("S3_BUCKET", "")
    results = []
    error = None
    try:
        if not bucket:
            raise RuntimeError("S3_BUCKET is not configured")
        client = s3_client()
        for sensor in sensor_config():
            item = newest_sensor_object(client, bucket, sensor["prefix"], now)
            if item:
                modified = item["LastModified"].astimezone(timezone.utc)
                age_hours = max(0, (now - modified).total_seconds() / 3600)
                stale = age_hours > threshold
                row = {**sensor, "status": "stale" if stale else "ok", "last_file": item["Key"],
                       "last_modified": modified.isoformat(), "age_hours": round(age_hours, 2)}
            else:
                stale = True
                row = {**sensor, "status": "missing", "last_file": None,
                       "last_modified": None, "age_hours": None}
            results.append(row)

            incident = row["status"] != "ok"
            if send_notifications and incident and not ALERTED.get(sensor["id"]):
                age = "לא נמצא קובץ" if row["age_hours"] is None else f"{row['age_hours']:.1f} שעות"
                send_alert(f"⚠️ FreezeM Sensor Watch: {sensor['name']} לא העלתה קובץ בזמן ({age}).")
                ALERTED[sensor["id"]] = True
            elif not incident and ALERTED.pop(sensor["id"], None) and send_notifications:
                send_alert(f"✅ FreezeM Sensor Watch: {sensor['name']} חזרה לפעילות תקינה.")
    except Exception as exc:
        log.exception("Monitoring check failed")
        error = str(exc)

    snapshot = {"checked_at": now.isoformat(), "systems": results, "error": error,
                "threshold_hours": threshold}
    with LOCK:
        STATE.update(snapshot)
    return snapshot


def monitor_loop():
    while True:
        run_check(send_notifications=True)
        time.sleep(max(60, int(env_float("CHECK_INTERVAL_MINUTES", 10) * 60)))


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/api/status")
def status():
    with LOCK:
        snapshot = dict(STATE)
    if snapshot["checked_at"] is None:
        snapshot = run_check(send_notifications=False)
    return jsonify(snapshot), 503 if snapshot.get("error") else 200


@app.get("/health")
def health():
    return jsonify({"ok": True})


if os.getenv("DISABLE_BACKGROUND_MONITOR", "false").lower() != "true":
    threading.Thread(target=monitor_loop, daemon=True, name="sensor-monitor").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
