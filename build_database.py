"""
build_database.py
=================
Scans zip files in a Google Drive folder, extracts contract PDFs, uses Claude
(claude-haiku-4-5) to extract metadata, and writes agreements_database.json
back to the same Drive folder.

Dependencies:
    pip install google-auth google-auth-oauthlib google-auth-httplib2
                google-api-python-client pypdf anthropic

Run:
    python build_database.py
"""

from __future__ import annotations

import io
import json
import os
import re
import uuid
import zipfile
from datetime import datetime, date
from pathlib import Path
from typing import Any

# ── Google Drive ──────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

# ── PDF text extraction ───────────────────────────────────────────────────────
import pypdf

# ── Claude AI ─────────────────────────────────────────────────────────────────
import anthropic

# =============================================================================
# Configuration
# =============================================================================

GDRIVE_FOLDER_ID = "1xgaXpvC2R2BWdLBjg1mUTd9OY9s5UOY_"
DATABASE_FILENAME = "agreements_database.json"
INBOX_FOLDER_NAME = "99 INBOX"
GDRIVE_CONFIG_PATH = Path(__file__).parent / "gdrive_config.json"
TOKEN_PATH = Path(__file__).parent / "token.json"
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"

SCOPES = ["https://www.googleapis.com/auth/drive"]

SELF_PARTIES = [
    "Tuulikarhu Oy",
    "Etha Wind Oy",
    "Etha Oy",
    "Aurinkokarhu Oy",
]

APPENDIX_KEYWORDS = {
    "liite", "bilaga", "appendix", "annex", "exhibit",
    "attachment", "schedule",
}

SKIP_EXTENSIONS = {".eml", ".msg"}

PDF_TEXT_LIMIT = 4500  # characters

CLAUDE_MODEL = "claude-haiku-4-5"

# Map zip filename patterns → category key
ZIP_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    ("NDA",           "NDA"),
    ("Framework",     "Framework"),
    ("Cooperation",   "Cooperation"),
    ("Commitment",    "Commitment"),
    ("Consultancy",   "Service"),
    ("Development",   "Development"),
    ("SHA",           "SHA"),
    ("SPA",           "SPA"),
    ("SPVs",          "SPV"),
    ("Loans",         "Loan"),
    ("Protocols",     "Protocol"),
    ("Admin",         "Admin"),
    ("Others",        "Other"),
]

# Folder-name keyword → category (used when folder contains these terms)
FOLDER_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("samarbets",      "Cooperation"),
    ("cooperation",    "Cooperation"),
    ("pöytäkirja",     "Protocol"),
    ("protokoll",      "Protocol"),
    ("protocol",       "Protocol"),
    ("nda",            "NDA"),
    ("framework",      "Framework"),
    ("commitment",     "Commitment"),
    ("consultancy",    "Service"),
    ("development",    "Development"),
    ("sha",            "SHA"),
    ("spa",            "SPA"),
    ("spv",            "SPV"),
    ("loan",           "Loan"),
    ("admin",          "Admin"),
    ("laina",          "Loan"),
    ("takaus",         "Loan"),
]

CATEGORIES: dict[str, dict[str, str]] = {
    "NDA":         {"label": "NDA"},
    "Framework":   {"label": "Framework Agreement"},
    "Cooperation": {"label": "Cooperation Agreement"},
    "Commitment":  {"label": "Commitment Letter"},
    "Service":     {"label": "Service / Consultancy"},
    "Development": {"label": "Development Agreement"},
    "SHA":         {"label": "Shareholder Agreement"},
    "SPA":         {"label": "Share Purchase Agreement"},
    "SPV":         {"label": "SPV / Corporate"},
    "Loan":        {"label": "Loan / Guarantee"},
    "Protocol":    {"label": "Protocol / Resolution"},
    "Admin":       {"label": "Administration"},
    "Other":       {"label": "Other"},
}

ATTENTION_RULES: dict[str, Any] = {
    "payment_due_within_days": 30,
    "term_ending_within_days": 90,
    "renewal_notice_within_days": 60,
    "non_compete_ending_within_days": 30,
    "recently_added_within_days": 7,
    "missing_metadata_flags": ["all_parties", "signed_date", "category"],
    "missing_metadata_flags_skip_categories": ["Protocol"],
}

# =============================================================================
# Helper utilities
# =============================================================================

_ZEFORT_ID_RE = re.compile(r"\s*\((bnd|ct)_[^)]+\)\s*$", re.IGNORECASE)


def strip_zefort_id(name: str) -> str:
    """Remove trailing Zefort IDs like '(bnd_xxx)' or '(ct_xxx)' from a name."""
    return _ZEFORT_ID_RE.sub("", name).strip()


def is_appendix(filename: str) -> bool:
    """Return True if the filename looks like an appendix/exhibit/attachment."""
    lower = filename.lower()
    return any(kw in lower for kw in APPENDIX_KEYWORDS)


def category_from_zip_name(zip_name: str) -> str:
    """Derive category key from a zip filename."""
    stem = Path(zip_name).stem  # e.g. "NDA" from "NDA.zip"
    for pattern, cat in ZIP_CATEGORY_PATTERNS:
        if pattern in stem:
            return cat
    return "Other"


def category_from_folder_name(folder_name: str) -> str | None:
    """Try to derive category from a contract folder name."""
    lower = folder_name.lower()
    for keyword, cat in FOLDER_CATEGORY_KEYWORDS:
        if keyword in lower:
            return cat
    return None


def today_str() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def blank_agreement() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "title": "",
        "category": "",
        "subcategory": None,
        "language": None,
        "status": "active",
        "signed_date": None,
        "effective_date": None,
        "expiry_date": None,
        "renewal_notice_deadline": None,
        "counterparties": [],
        "self_parties": [],
        "all_parties": [],
        "related_entities": [],
        "file_name": "",
        "file_path": "",
        "zip_internal_path": "",
        "source_zip": "",
        "appendices": [],
        "payment_schedule": [],
        "non_compete_period_months": None,
        "non_compete_end_date": None,
        "non_solicit_period_months": None,
        "confidentiality_period_years": None,
        "maturity_date": None,
        "long_stop_date": None,
        "closing_date": None,
        "currency": None,
        "principal": None,
        "transaction_value": None,
        "ip_assignment": None,
        "mutual": None,
        "summary": "",
        "key_obligations": [],
        "notes": "",
        "gdrive_file_id": "",
        "added_to_folder_date": today_str(),
    }


# =============================================================================
# Google Drive authentication & helpers
# =============================================================================

def get_drive_service():
    """Authenticate and return a Google Drive API service object.

    Priority:
      1. SERVICE_ACCOUNT_JSON env var  (server / Railway)
      2. service_account.json file     (local, preferred)
      3. Desktop OAuth via credentials.json / token.json  (legacy local)
    """
    from google.oauth2 import service_account as _sa

    # 1. Service account via env var
    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if sa_json:
        info  = json.loads(sa_json)
        creds = _sa.Credentials.from_service_account_info(info, scopes=SCOPES)
        return build("drive", "v3", credentials=creds)

    # 2. Service account file
    sa_file = Path(__file__).parent / "service_account.json"
    if sa_file.exists():
        creds = _sa.Credentials.from_service_account_file(str(sa_file), scopes=SCOPES)
        return build("drive", "v3", credentials=creds)

    # 3. Legacy Desktop OAuth (local only)
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  [Auth] Refreshing access token …")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"OAuth credentials file not found: {CREDENTIALS_PATH}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            print("  [Auth] Opening browser for OAuth2 login …")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        print(f"  [Auth] Token saved to {TOKEN_PATH}")

    return build("drive", "v3", credentials=creds)


def list_files_in_folder(service, folder_id: str) -> list[dict]:
    """Return all files (not folders) directly inside a Drive folder."""
    results = []
    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def list_items_in_folder(service, folder_id: str) -> list[dict]:
    """Return all items (files AND subfolders) directly inside a Drive folder."""
    results = []
    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def process_inbox_folder(
    service,
    inbox_folder_id: str,
    claude_client: anthropic.Anthropic,
) -> list[dict]:
    """
    Scan the 99 INBOX folder and return agreement dicts for each PDF found.

    Supported structures:
        99 INBOX/ContractName/file.pdf   — subfolder per contract (preferred)
        99 INBOX/file.pdf                — single file directly in INBOX
    """
    agreements: list[dict] = []
    items = list_items_in_folder(service, inbox_folder_id)

    FOLDER_MIME = "application/vnd.google-apps.folder"

    for item in sorted(items, key=lambda x: x["name"]):
        if item["mimeType"] == FOLDER_MIME:
            # Subfolder = one contract
            sub_items = list_items_in_folder(service, item["id"])
            pdfs = [f for f in sub_items if f["name"].lower().endswith(".pdf")]
            if not pdfs:
                continue
            main_pdf   = next((f for f in pdfs if not is_appendix(f["name"])), pdfs[0])
            appendices = [f for f in pdfs if f["id"] != main_pdf["id"]]
            title      = strip_zefort_id(item["name"])
            category   = category_from_folder_name(title) or "Other"
            agr = _make_inbox_agreement(
                service, main_pdf, title, category, appendices, claude_client
            )
            if agr:
                agreements.append(agr)

        elif item["name"].lower().endswith(".pdf"):
            # Single PDF directly in INBOX root
            title    = strip_zefort_id(Path(item["name"]).stem)
            category = category_from_folder_name(title) or "Other"
            agr = _make_inbox_agreement(
                service, item, title, category, [], claude_client
            )
            if agr:
                agreements.append(agr)

    print(f"  [INBOX] {len(agreements)} agreement(s) found")
    return agreements


def _make_inbox_agreement(
    service,
    file_meta: dict,
    title: str,
    category: str,
    appendices: list[dict],
    claude_client: anthropic.Anthropic,
) -> dict | None:
    """Build one agreement dict from a direct Google Drive PDF."""
    print(f"    [INBOX] {title}")
    print(f"            file : {file_meta['name']}")

    try:
        pdf_bytes = download_file(service, file_meta["id"])
        pdf_text  = extract_pdf_text(pdf_bytes)
    except Exception as exc:
        print(f"    [PDF] Error reading {file_meta['name']}: {exc}")
        pdf_text = ""

    agr = blank_agreement()
    agr["title"]            = title
    agr["category"]         = category
    agr["file_name"]        = file_meta["name"]
    agr["file_path"]        = f"https://drive.google.com/file/d/{file_meta['id']}/view"
    agr["gdrive_file_id"]   = file_meta["id"]
    agr["zip_internal_path"] = ""
    agr["source_zip"]       = INBOX_FOLDER_NAME
    agr["added_to_folder_date"] = (file_meta.get("createdTime") or today_str())[:10]
    agr["appendices"] = [
        {"file_name": a["name"], "gdrive_file_id": a["id"]}
        for a in appendices
    ]

    if pdf_text:
        print(f"            calling Claude …")
        meta = extract_metadata_via_claude(claude_client, pdf_text, title, category)
        _apply_claude_metadata(agr, meta)
    else:
        print(f"            [skip Claude – no text extracted]")

    agr["all_parties"] = sorted(
        set(agr["self_parties"]) | set(agr["counterparties"])
    )
    return agr


def download_file(service, file_id: str) -> bytes:
    """Download a Drive file by ID and return its raw bytes."""
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def upload_or_update_json(
    service,
    folder_id: str,
    filename: str,
    data: dict,
    existing_file_id: str | None = None,
) -> str:
    """Upload (or update) a JSON file in Drive. Returns the file ID."""
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(
        io.BytesIO(content), mimetype="application/json", resumable=False
    )

    if existing_file_id:
        updated = (
            service.files()
            .update(fileId=existing_file_id, media_body=media, supportsAllDrives=True)
            .execute()
        )
        return updated["id"]
    else:
        file_meta = {"name": filename, "parents": [folder_id]}
        created = (
            service.files()
            .create(body=file_meta, media_body=media, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]


def find_existing_database(service, folder_id: str, filename: str) -> str | None:
    """Return the file ID of an existing database file in the folder, or None."""
    files = list_files_in_folder(service, folder_id)
    for f in files:
        if f["name"] == filename:
            return f["id"]
    return None


# =============================================================================
# PDF text extraction
# =============================================================================

def extract_pdf_text(pdf_bytes: bytes, char_limit: int = PDF_TEXT_LIMIT) -> str:
    """Extract up to char_limit characters of text from a PDF byte string."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text_parts: list[str] = []
        total = 0
        for page in reader.pages:
            chunk = page.extract_text() or ""
            text_parts.append(chunk)
            total += len(chunk)
            if total >= char_limit:
                break
        return "".join(text_parts)[:char_limit]
    except Exception as exc:
        print(f"    [PDF] Warning – could not extract text: {exc}")
        return ""


# =============================================================================
# Claude metadata extraction
# =============================================================================

CLAUDE_SYSTEM_PROMPT = """You are a legal document analysis assistant.
Given the beginning of a contract document, extract structured metadata and
return it as a JSON object — nothing else.

Return exactly these keys (use null for unknown/not present):
{
  "title": string,
  "language": "fi" | "sv" | "en",
  "signed_date": "YYYY-MM-DD" | null,
  "effective_date": "YYYY-MM-DD" | null,
  "expiry_date": "YYYY-MM-DD" | null,
  "counterparties": [list of company/party names that are NOT the self-parties],
  "self_parties": [list of self-party names found in the document],
  "summary": "1-2 sentence plain-language summary",
  "non_compete_period_months": integer | null,
  "confidentiality_period_years": number | null,
  "key_obligations": [list of short obligation strings],
  "payment_schedule": [list of payment obligation strings, or empty list],
  "mutual": true | false | null,
  "currency": "EUR" | "USD" | "SEK" | "GBP" | null,
  "principal": number | null,
  "transaction_value": number | null,
  "ip_assignment": true | false | null
}

Self-parties (always classify these as self_parties, never counterparties):
""" + "\n".join(f"- {p}" for p in SELF_PARTIES)


def extract_metadata_via_claude(
    client: anthropic.Anthropic,
    pdf_text: str,
    title_hint: str,
    category_hint: str,
) -> dict[str, Any]:
    """Call Claude to extract structured metadata from PDF text."""
    if not pdf_text.strip():
        return {}

    user_msg = (
        f"Document title hint: {title_hint}\n"
        f"Category hint: {category_hint}\n\n"
        f"--- DOCUMENT TEXT (first ~{PDF_TEXT_LIMIT} chars) ---\n"
        f"{pdf_text}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"    [Claude] JSON parse error: {exc}")
        return {}
    except Exception as exc:
        print(f"    [Claude] API error: {exc}")
        return {}


# =============================================================================
# Core processing
# =============================================================================

def parse_zip(
    zip_bytes: bytes,
    zip_name: str,
    zip_file_id: str,
    claude_client: anthropic.Anthropic,
) -> list[dict[str, Any]]:
    """
    Parse one zip file and return a list of agreement dicts.

    Zip structure expected:
        {Category} (bnd_xxx)/{Contract Name} (ct_xxx)/filename.pdf
    """
    agreements: list[dict[str, Any]] = []

    base_category = category_from_zip_name(zip_name)
    zip_url = f"https://drive.google.com/file/d/{zip_file_id}/view"

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        print(f"  [ZIP] Cannot open {zip_name}: {exc}")
        return agreements

    all_names = zf.namelist()

    # Group files by their immediate parent folder (= contract folder)
    # We support 1-level (flat) and 2-level (category/contract) structures.
    contracts: dict[str, list[str]] = {}  # contract_folder → [file paths]

    for member in all_names:
        parts = Path(member.replace("\\", "/")).parts
        if len(parts) < 2:
            # Top-level file, treat parent as zip stem
            continue

        if len(parts) == 2:
            # Structure: {contract_folder}/{file}
            contract_folder = parts[0]
        else:
            # Structure: {category_folder}/{contract_folder}/{file} or deeper
            contract_folder = "/".join(parts[:-1])

        contracts.setdefault(contract_folder, []).append(member)

    if not contracts:
        print(f"  [ZIP] No contract folders found in {zip_name}")
        return agreements

    print(f"  [ZIP] {zip_name}: {len(contracts)} contract folder(s) found")

    for contract_folder, members in sorted(contracts.items()):
        # Determine clean title from the innermost folder name
        folder_parts = contract_folder.replace("\\", "/").split("/")
        raw_title = folder_parts[-1]
        clean_title = strip_zefort_id(raw_title)

        # Try to refine category from folder names
        category = base_category
        for fp in folder_parts:
            derived = category_from_folder_name(fp)
            if derived:
                category = derived
                break

        # Separate primary PDFs from appendices, skip .eml/.msg
        primary_files: list[str] = []
        appendix_files: list[str] = []

        for m in members:
            # Skip directories (entries ending with /)
            if m.endswith("/"):
                continue
            fname = Path(m.replace("\\", "/")).name
            ext = Path(fname).suffix.lower()

            if ext in SKIP_EXTENSIONS:
                continue

            if ext != ".pdf":
                continue  # Only process PDFs

            if is_appendix(fname):
                appendix_files.append(m)
            else:
                primary_files.append(m)

        if not primary_files and not appendix_files:
            print(f"    [Contract] No usable PDF in: {contract_folder}")
            continue

        # Pick the "main" file: first non-appendix PDF, else first appendix
        main_file_path = primary_files[0] if primary_files else appendix_files[0]
        main_file_name = Path(main_file_path.replace("\\", "/")).name

        print(f"    [Contract] {clean_title}")
        print(f"               main file  : {main_file_name}")
        if appendix_files:
            print(f"               appendices : {len(appendix_files)}")

        # Extract PDF text
        try:
            pdf_bytes = zf.read(main_file_path)
            pdf_text = extract_pdf_text(pdf_bytes)
        except Exception as exc:
            print(f"    [PDF] Error reading {main_file_name}: {exc}")
            pdf_text = ""

        # Build agreement record
        agr = blank_agreement()
        agr["title"] = clean_title
        agr["category"] = category
        agr["file_name"] = main_file_name
        agr["file_path"] = zip_url
        agr["zip_internal_path"] = main_file_path
        agr["source_zip"] = zip_name
        agr["appendices"] = [
            {
                "file_name": Path(ap.replace("\\", "/")).name,
                "zip_internal_path": ap,
            }
            for ap in appendix_files
        ]

        # Claude metadata extraction
        if pdf_text:
            print(f"               calling Claude …")
            meta = extract_metadata_via_claude(
                claude_client, pdf_text, clean_title, category
            )
            _apply_claude_metadata(agr, meta)
        else:
            print(f"               [skip Claude – no text extracted]")

        # Ensure self_parties default if Claude found nothing
        if not agr["self_parties"]:
            agr["self_parties"] = []

        # Build all_parties
        agr["all_parties"] = sorted(
            set(agr["self_parties"]) | set(agr["counterparties"])
        )

        agreements.append(agr)

    zf.close()
    return agreements


def _apply_claude_metadata(agr: dict[str, Any], meta: dict[str, Any]) -> None:
    """Merge Claude-extracted metadata into the agreement dict."""
    if not meta:
        return

    def get(key, default=None):
        return meta.get(key, default)

    if get("title"):
        agr["title"] = get("title")
    if get("language"):
        agr["language"] = get("language")
    if get("signed_date"):
        agr["signed_date"] = get("signed_date")
    if get("effective_date"):
        agr["effective_date"] = get("effective_date")
    if get("expiry_date"):
        agr["expiry_date"] = get("expiry_date")
    if get("counterparties"):
        agr["counterparties"] = get("counterparties", [])
    if get("self_parties"):
        agr["self_parties"] = get("self_parties", [])
    if get("summary"):
        agr["summary"] = get("summary")
    if get("non_compete_period_months") is not None:
        agr["non_compete_period_months"] = get("non_compete_period_months")
    if get("confidentiality_period_years") is not None:
        agr["confidentiality_period_years"] = get("confidentiality_period_years")
    if get("key_obligations"):
        agr["key_obligations"] = get("key_obligations", [])
    if get("payment_schedule"):
        agr["payment_schedule"] = get("payment_schedule", [])
    if get("mutual") is not None:
        agr["mutual"] = get("mutual")
    if get("currency"):
        agr["currency"] = get("currency")
    if get("principal") is not None:
        agr["principal"] = get("principal")
    if get("transaction_value") is not None:
        agr["transaction_value"] = get("transaction_value")
    if get("ip_assignment") is not None:
        agr["ip_assignment"] = get("ip_assignment")


# =============================================================================
# Database builder
# =============================================================================

def build_database_json(
    agreements: list[dict[str, Any]],
    dismissed_flags: list | None = None,
    manual_corrections_log: list | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "last_updated": now_iso(),
            "version": "2.0",
            "total": len(agreements),
        },
        "agreements": agreements,
        "categories": CATEGORIES,
        "attention_rules": ATTENTION_RULES,
        "dismissed_flags": dismissed_flags or [],
        "manual_corrections_log": manual_corrections_log or [],
    }


# =============================================================================
# Config persistence
# =============================================================================

def load_config() -> dict[str, Any]:
    if GDRIVE_CONFIG_PATH.exists():
        return json.loads(GDRIVE_CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(config: dict[str, Any]) -> None:
    GDRIVE_CONFIG_PATH.write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


# =============================================================================
# Core rebuild (callable from CLI or server)
# =============================================================================

def run_rebuild(service, claude_client, log=print) -> dict[str, Any]:
    """
    Scan Drive ZIPs + INBOX, extract metadata, write agreements_database.json.
    Returns {"agreement_count": int, "db_file_id": str}.

    `log` is a callable used for progress messages (default: print).
    On the server, pass a function that appends to a list.
    """

    # ── List Drive folder ─────────────────────────────────────────
    log(f"Listing Drive folder …")
    all_items  = list_items_in_folder(service, GDRIVE_FOLDER_ID)
    zip_files  = [f for f in all_items if f["name"].lower().endswith(".zip")]
    inbox_item = next((f for f in all_items if f["name"] == INBOX_FOLDER_NAME), None)
    log(f"Found {len(zip_files)} zip(s)" + (" + INBOX" if inbox_item else ""))

    # ── Process ZIPs ──────────────────────────────────────────────
    all_agreements: list[dict[str, Any]] = []
    for zf_meta in zip_files:
        log(f"Processing {zf_meta['name']} …")
        zip_bytes  = download_file(service, zf_meta["id"])
        agreements = parse_zip(zip_bytes, zf_meta["name"], zf_meta["id"], claude_client)
        log(f"  → {len(agreements)} agreement(s)")
        all_agreements.extend(agreements)

    # ── Process INBOX ─────────────────────────────────────────────
    if inbox_item:
        log(f"Processing {INBOX_FOLDER_NAME} …")
        inbox_agreements = process_inbox_folder(service, inbox_item["id"], claude_client)
        all_agreements.extend(inbox_agreements)

    log(f"Total: {len(all_agreements)} agreements")

    # ── Preserve IDs and user data ────────────────────────────────
    config         = load_config()
    existing_db_id = config.get("database_file_id")
    existing_dismissed:   list = []
    existing_corrections: list = []

    if existing_db_id:
        try:
            service.files().get(fileId=existing_db_id, fields="id", supportsAllDrives=True).execute()
            log("Loading existing database to preserve IDs …")
            existing_db  = json.loads(download_file(service, existing_db_id).decode("utf-8"))
            existing_dismissed   = existing_db.get("dismissed_flags", [])
            existing_corrections = existing_db.get("manual_corrections_log", [])

            existing_map: dict = {}
            for a in existing_db.get("agreements", []):
                if a.get("gdrive_file_id"):
                    existing_map[("drive", a["gdrive_file_id"])] = a
                elif a.get("source_zip") and a.get("zip_internal_path"):
                    existing_map[("zip", a["source_zip"], a["zip_internal_path"])] = a

            for agr in all_agreements:
                if agr.get("gdrive_file_id"):
                    key = ("drive", agr["gdrive_file_id"])
                elif agr.get("source_zip") and agr.get("zip_internal_path"):
                    key = ("zip", agr["source_zip"], agr["zip_internal_path"])
                else:
                    continue
                if key in existing_map:
                    prev = existing_map[key]
                    agr["id"] = prev["id"]
                    if prev.get("notes"):
                        agr["notes"] = prev["notes"]
                    if prev.get("status") and prev["status"] != "active":
                        agr["status"] = prev["status"]

            log(f"Preserved {len(existing_map)} IDs, "
                f"{len(existing_dismissed)} dismissed flag(s), "
                f"{len(existing_corrections)} correction(s)")
        except Exception as exc:
            log(f"Could not load existing database ({exc}) – starting fresh")
            existing_db_id = None

    # ── Write database ────────────────────────────────────────────
    log(f"Writing {DATABASE_FILENAME} to Drive …")
    database  = build_database_json(all_agreements, existing_dismissed, existing_corrections)
    new_db_id = upload_or_update_json(
        service, GDRIVE_FOLDER_ID, DATABASE_FILENAME, database,
        existing_file_id=existing_db_id,
    )
    config["folder_id"]       = GDRIVE_FOLDER_ID
    config["database_file_id"] = new_db_id
    save_config(config)
    log(f"Done — {len(all_agreements)} agreements written (file ID: {new_db_id})")
    return {"agreement_count": len(all_agreements), "db_file_id": new_db_id}


# =============================================================================
# CLI entry point
# =============================================================================

def main() -> None:
    print("=" * 60)
    print("Tuulikarhu Agreements Database Builder")
    print("=" * 60)

    print("\n[1/2] Authenticating …")
    service = get_drive_service()
    print("       OK")

    print("\n[2/2] Initialising Claude …")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    claude_client = anthropic.Anthropic(api_key=api_key)
    print("       OK\n")

    result = run_rebuild(service, claude_client)
    print("\n" + "=" * 60)
    print(f"Done! {result['agreement_count']} agreements written.")
    print("=" * 60)


if __name__ == "__main__":
    main()
