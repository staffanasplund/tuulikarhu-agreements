"""
Tuulikarhu Oy — Agreements Dashboard
Flask server that reads/writes agreements_database.json from/to Google Drive.

Local:   python app.py
Deploy:  gunicorn app:app   (Render / any WSGI host)

Required environment variables:
    SERVICE_ACCOUNT_JSON   — full contents of the Google service-account key JSON
    DB_FILE_ID             — Google Drive file ID of agreements_database.json
    FOLDER_ID              — Google Drive folder ID (optional; informational)
    DASHBOARD_USERNAME     — HTTP Basic-Auth username
    DASHBOARD_PASSWORD     — HTTP Basic-Auth password
"""
import functools
import io
import json
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ── config ────────────────────────────────────────────────────────────────────
SCOPES   = ["https://www.googleapis.com/auth/drive"]
BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder="templates")

_service    = None
_db_file_id = os.environ.get("DB_FILE_ID", "")
_folder_id  = os.environ.get("FOLDER_ID", "")

USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")


# ── Google Drive (service account) ───────────────────────────────────────────
def get_service():
    global _service
    if _service:
        return _service

    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if sa_json:
        # Production: credentials supplied via environment variable
        info  = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Local fallback: read service_account.json from project folder
        sa_file = BASE_DIR / "service_account.json"
        if not sa_file.exists():
            raise FileNotFoundError(
                "No SERVICE_ACCOUNT_JSON env var and no service_account.json found. "
                "See README for Google Cloud setup."
            )
        creds = service_account.Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)

    _service = build("drive", "v3", credentials=creds)
    return _service


def _load_config_from_file():
    """Load DB_FILE_ID / FOLDER_ID from gdrive_config.json when env vars are absent."""
    global _db_file_id, _folder_id
    if _db_file_id and _folder_id:
        return  # already set via env vars
    cfg_path = BASE_DIR / "gdrive_config.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        _db_file_id = _db_file_id or cfg.get("database_file_id", "")
        _folder_id  = _folder_id  or cfg.get("folder_id", "")


def read_db() -> dict:
    buf = io.BytesIO()
    req = get_service().files().get_media(fileId=_db_file_id, supportsAllDrives=True)
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return json.loads(buf.getvalue().decode("utf-8"))


def write_db(data: dict):
    body  = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(body), mimetype="application/json", resumable=False)
    get_service().files().update(
        fileId=_db_file_id, media_body=media, supportsAllDrives=True
    ).execute()


# ── HTTP Basic Auth ───────────────────────────────────────────────────────────
def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != USERNAME or auth.password != PASSWORD:
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Tuulikarhu Agreements"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/api/data")
@require_auth
def get_data():
    data = read_db()
    return Response(
        json.dumps(data, ensure_ascii=False),
        mimetype="application/json"
    )


@app.route("/api/dismiss", methods=["POST"])
@require_auth
def dismiss_flag():
    body         = request.get_json(force=True)
    agreement_id = body.get("agreement_id", "").strip()
    flag_type    = body.get("flag_type",    "").strip()
    if not agreement_id or not flag_type:
        return jsonify({"ok": False, "error": "agreement_id and flag_type required"}), 400

    data      = read_db()
    dismissed = data.setdefault("dismissed_flags", [])
    exists    = any(
        d.get("agreement_id") == agreement_id and d.get("flag_type") == flag_type
        for d in dismissed
    )
    if not exists:
        dismissed.append({
            "agreement_id": agreement_id,
            "flag_type":    flag_type,
            "dismissed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        })
        write_db(data)
    return jsonify({"ok": True})


@app.route("/api/update", methods=["POST"])
@require_auth
def update_agreement():
    """Patch a single field on an agreement (for inline corrections)."""
    body         = request.get_json(force=True)
    agreement_id = body.get("agreement_id", "").strip()
    field        = body.get("field",        "").strip()
    value        = body.get("value")
    if not agreement_id or not field:
        return jsonify({"ok": False, "error": "agreement_id and field required"}), 400

    ALLOWED_FIELDS = {
        "title", "status", "signed_date", "effective_date", "expiry_date",
        "renewal_notice_deadline", "notes", "counterparties", "currency",
        "maturity_date", "long_stop_date", "closing_date", "principal",
        "transaction_value", "non_compete_period_months", "non_compete_end_date",
        "confidentiality_period_years", "summary", "payment_schedule",
    }
    if field not in ALLOWED_FIELDS:
        return jsonify({"ok": False, "error": f"Field '{field}' not editable"}), 400

    data  = read_db()
    found = False
    for agr in data.get("agreements", []):
        if agr.get("id") == agreement_id:
            agr[field] = value
            found = True
            break
    if not found:
        return jsonify({"ok": False, "error": "Agreement not found"}), 404

    data.setdefault("manual_corrections_log", []).append({
        "agreement_id": agreement_id,
        "field":        field,
        "new_value":    str(value)[:200],
        "updated_at":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
    })
    write_db(data)
    return jsonify({"ok": True})


# ── launch ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _load_config_from_file()
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Tuulikarhu Agreements Dashboard — http://localhost:{port}\n")
    app.run(debug=False, port=port, host="0.0.0.0")
