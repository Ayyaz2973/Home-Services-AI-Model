"""HomeServe Local
Auto request capture -> AI triage (Ollama) -> auto-assign available technician
-> technician marks work done -> customer confirms -> payment released.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE, "homeserve.db"))
PRIVATE_STORAGE = Path(os.getenv("PRIVATE_STORAGE_DIR", os.path.join(BASE, "private_uploads"))).resolve()
ADMIN_KEY = os.getenv("ADMIN_KEY", "Kayani_73")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
COMMISSION_PCT = float(os.getenv("COMMISSION_PCT", "10"))
ASSIGN_TIMEOUT_MIN = int(os.getenv("ASSIGN_TIMEOUT_MIN", "10"))
AUTO_CONFIRM_HOURS = 24
CHECK_IN_MAX_ACCURACY_METERS = 150
TERMS_VERSION = "phase1-draft-v1"
PRIVACY_VERSION = "phase1-draft-v1"
DOCUMENT_MAX_BYTES = int(os.getenv("DOCUMENT_MAX_BYTES", str(8 * 1024 * 1024)))
PHOTO_MAX_BYTES = int(os.getenv("PHOTO_MAX_BYTES", str(6 * 1024 * 1024)))
IMAGE_MAX_PIXELS = 20_000_000
IMAGE_MAX_DIMENSION = 2400

# Pilot focus: Jinnah Garden / FECHS Islamabad (area matching is still soft)
JINNAH_GARDEN_KEYWORDS = ["jinnah", "jinnah garden", "jinnah gardens", "fechs", "gagri"]

CATEGORIES = [
    "Electric Work", "Plumbing", "Air Conditioning", "Solar Solutions",
    "Home Cleaning", "Pest Control", "Washing Machine", "Fridge Repair",
    "Geyser Repair", "Carpentry", "Painting", "Locksmith", "CCTV Installation",
    "Water Filter / RO", "Welding Work", "Labour", "Other",
]

KEYWORDS = {
    "Electric Work": ["bijli", "electric", "wiring", "switch", "socket", "fan ", "breaker", "short circuit", "ups ", "بجلی"],
    "Plumbing": ["plumb", "nal ", "nalka", "pipe", "leak", "tap ", "tanki", "drain", "commode", "washroom", "نل", "پانی"],
    "Air Conditioning": [" ac ", "a/c", "air condition", "cooling", "split", "اے سی"],
    "Solar Solutions": ["solar", "panel", "inverter", "سولر"],
    "Home Cleaning": ["clean", "safai", "sofa wash", "صفائی"],
    "Pest Control": ["pest", "keera", "deemak", "termite", "cockroach", "مچھر"],
    "Washing Machine": ["washing machine", "dryer", "kapray dhone"],
    "Fridge Repair": ["fridge", "refrigerator", "freezer", "فریج"],
    "Geyser Repair": ["geyser", "gyser", "گیزر"],
    "Carpentry": ["carpent", "lakri", "door", "wardrobe", "cupboard", "darwaza"],
    "Painting": ["paint", "rang", "colour", "color", "polish"],
    "Locksmith": ["lock", "chabi", "key ", "تالا"],
    "CCTV Installation": ["cctv", "camera", "dvr"],
    "Water Filter / RO": ["water filter", " ro ", "filter"],
    "Welding Work": ["weld", "grill", "gate", "iron"],
    "Labour": ["labour", "mazdoor", "shifting"],
}
EMERGENCY_WORDS = ["spark", "aag", "fire", "flood", "shock", "gas leak", "current lag", "pani bhar"]
URGENT_WORDS = ["urgent", "foran", "abhi", "today", "aaj", "emergency", "jaldi"]

BUSY = ("assigned", "accepted")


# ---------------------------------------------------------------- database
@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def hp(pin: str) -> str:
    return hashlib.sha256(("hs-salt-" + str(pin)).encode()).hexdigest()


def norm_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")


def init_db():
    if os.getenv("RAILWAY_ENVIRONMENT") and (
        not os.getenv("ADMIN_KEY")
        or os.getenv("ADMIN_KEY") in {"admin123", "change-me-now"}
    ):
        raise RuntimeError("Set a unique, strong ADMIN_KEY before deploying on Railway")
    with db() as con:
        con.executescript(
            """
        CREATE TABLE IF NOT EXISTS technicians(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, pin_hash TEXT NOT NULL,
            category TEXT NOT NULL, area TEXT DEFAULT '',
            available INTEGER DEFAULT 1, active INTEGER DEFAULT 1,
            rating REAL DEFAULT 5.0, rating_count INTEGER DEFAULT 0,
            jobs_done INTEGER DEFAULT 0, wallet REAL DEFAULT 0, created_at TEXT);
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
            customer_name TEXT, customer_phone TEXT, address TEXT, area TEXT,
            description TEXT, category TEXT, urgency TEXT DEFAULT 'normal', ai_summary TEXT,
            status TEXT DEFAULT 'new', technician_id INTEGER, amount REAL, work_note TEXT,
            service_id INTEGER, quoted_price REAL, visit_fee REAL,
            created_at TEXT, assigned_at TEXT, completed_at TEXT, confirmed_at TEXT);
        CREATE TABLE IF NOT EXISTS declines(request_id INTEGER, technician_id INTEGER, at TEXT);
        CREATE TABLE IF NOT EXISTS technician_locations(
            request_id INTEGER PRIMARY KEY,
            technician_id INTEGER NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy REAL,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, text TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER UNIQUE, technician_id INTEGER,
            amount REAL, commission REAL, payout REAL, collected INTEGER DEFAULT 0,
            status TEXT DEFAULT 'awaiting_confirmation', reference TEXT,
            created_at TEXT, released_at TEXT, paid_at TEXT);
        CREATE TABLE IF NOT EXISTS commission_ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            technician_id INTEGER NOT NULL,
            gross_amount INTEGER NOT NULL,
            commission_amount INTEGER NOT NULL,
            net_payout INTEGER NOT NULL,
            commission_pct REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'released',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_commission_ledger_tech ON commission_ledger(technician_id, id);
        CREATE TABLE IF NOT EXISTS disputes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL UNIQUE,
            raised_by TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            resolution TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT);
        CREATE TABLE IF NOT EXISTS quote_revisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            quoted_price REAL NOT NULL,
            visit_fee REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS technician_applications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, phone TEXT NOT NULL, pin_hash TEXT NOT NULL,
            category TEXT NOT NULL, area TEXT DEFAULT '', experience TEXT DEFAULT '',
            terms_version TEXT NOT NULL, terms_accepted_at TEXT NOT NULL,
            privacy_version TEXT NOT NULL, privacy_accepted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            review_note TEXT DEFAULT '', created_at TEXT NOT NULL, reviewed_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_technician_applications_status
            ON technician_applications(status, created_at);
        CREATE TABLE IF NOT EXISTS private_documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL, document_type TEXT NOT NULL,
            stored_path TEXT NOT NULL, media_type TEXT NOT NULL, uploaded_at TEXT NOT NULL,
            FOREIGN KEY(application_id) REFERENCES technician_applications(id));
        CREATE TABLE IF NOT EXISTS service_catalog(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL, service_name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', price REAL NOT NULL,
            visit_fee REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS extra_charge_proposals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL, technician_id INTEGER NOT NULL,
            amount REAL NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, reviewed_at TEXT);
        CREATE TABLE IF NOT EXISTS job_photos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL, technician_id INTEGER NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('before','after')),
            stored_path TEXT NOT NULL, media_type TEXT NOT NULL,
            uploaded_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_job_photos_request ON job_photos(request_id, phase, id);
        """
        )
        request_columns = {row["name"] for row in con.execute("PRAGMA table_info(requests)")}
        for column, declaration in (
            ("service_id", "INTEGER"),
            ("quoted_price", "REAL"),
            ("visit_fee", "REAL"),
            ("customer_terms_version", "TEXT"),
            ("customer_terms_accepted_at", "TEXT"),
            ("customer_privacy_version", "TEXT"),
            ("customer_privacy_accepted_at", "TEXT"),
            ("arrival_checked_in_at", "TEXT"),
            ("arrival_latitude", "REAL"),
            ("arrival_longitude", "REAL"),
            ("arrival_accuracy", "REAL"),
        ):
            if column not in request_columns:
                con.execute(f"ALTER TABLE requests ADD COLUMN {column} {declaration}")
        application_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(technician_applications)")
        }
        for column in ("privacy_version", "privacy_accepted_at"):
            if column not in application_columns:
                con.execute(
                    f"ALTER TABLE technician_applications ADD COLUMN {column} TEXT"
                )
        technician_columns = {row["name"] for row in con.execute("PRAGMA table_info(technicians)")}
        if "application_id" not in technician_columns:
            con.execute("ALTER TABLE technicians ADD COLUMN application_id INTEGER")
        PRIVATE_STORAGE.mkdir(parents=True, exist_ok=True)
        (PRIVATE_STORAGE / "documents").mkdir(exist_ok=True)
        (PRIVATE_STORAGE / "job_photos").mkdir(exist_ok=True)
        if (
            not os.getenv("RAILWAY_ENVIRONMENT")
            and con.execute("SELECT COUNT(*) FROM technicians").fetchone()[0] == 0
        ):
            demo = [
                ("Ali Electrician", "03001110001", "Electric Work", "F-10"),
                ("Bilal Electrician", "03001110002", "Electric Work", "G-11"),
                ("Usman Plumber", "03001110003", "Plumbing", "F-10"),
                ("Kamran Plumber", "03001110004", "Plumbing", "I-8"),
                ("Tariq AC Tech", "03001110005", "Air Conditioning", "F-11"),
                ("Imran Painter", "03001110006", "Painting", "G-10"),
            ]
            for n, p, c, a in demo:
                con.execute(
                    "INSERT INTO technicians(name,phone,pin_hash,category,area,created_at) VALUES(?,?,?,?,?,?)",
                    (n, p, hp("1234"), c, a, now()),
                )


def log(con, rid, text):
    con.execute("INSERT INTO events(request_id,text,at) VALUES(?,?,?)", (rid, text, now()))


# ---------------------------------------------------------------- AI triage
def keyword_guess(text: str) -> dict:
    t = " " + text.lower() + " "
    cat = "Other"
    for c, words in KEYWORDS.items():
        if any(w in t for w in words):
            cat = c
            break
    urgency = "normal"
    if any(w in t for w in URGENT_WORDS):
        urgency = "urgent"
    if any(w in t for w in EMERGENCY_WORDS):
        urgency = "emergency"
    return {"category": cat, "urgency": urgency, "summary": text.strip()[:140]}


def ai_analyze(text: str) -> dict:
    prompt = f"""You are a dispatcher for a home services company in Islamabad, Pakistan.
The customer message may be in Urdu, Roman Urdu or English.
Pick exactly ONE category from this list: {', '.join(CATEGORIES)}.
Pick urgency: "normal", "urgent" (needs same day) or "emergency" (danger: sparks, flooding, gas, shock).
Write a one-line English summary for the technician.
Reply with JSON only: {{"category": "...", "urgency": "...", "summary": "..."}}

Customer message: {text}"""
    fallback = keyword_guess(text)
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1},
            },
            timeout=90,
        )
        data = json.loads(r.json()["response"])
        cat = data.get("category")
        urg = data.get("urgency")
        return {
            "category": cat if cat in CATEGORIES else fallback["category"],
            "urgency": urg if urg in ("normal", "urgent", "emergency") else fallback["urgency"],
            "summary": (data.get("summary") or fallback["summary"])[:200],
        }
    except Exception as e:  # Ollama off or bad JSON: keyword fallback
        print("AI fallback:", e)
        return fallback


# ---------------------------------------------------------------- dispatch
def pick_technician(con, req):
    rows = con.execute(
        """SELECT * FROM technicians
           WHERE active=1 AND available=1 AND category=?
             AND id NOT IN (SELECT technician_id FROM requests
                            WHERE technician_id IS NOT NULL AND status IN ('assigned','accepted'))
             AND id NOT IN (SELECT technician_id FROM declines WHERE request_id=?)""",
        (req["category"], req["id"]),
    ).fetchall()
    if not rows:
        return None
    area = (req["area"] or "").strip().lower()
    rows = sorted(
        rows,
        key=lambda t: (0 if area and (t["area"] or "").strip().lower() == area else 1, -t["rating"], t["jobs_done"]),
    )
    return rows[0]


def assign_to(con, rid, tech):
    current = con.execute(
        "SELECT status,technician_id FROM requests WHERE id=?",
        (rid,),
    ).fetchone()
    if (
        current and current["status"] == "accepted"
        and current["technician_id"] != tech["id"]
        and job_has_progress_evidence(con, rid)
    ):
        raise HTTPException(409, "This accepted job has check-in, photo, or price evidence and cannot be reassigned")
    if current and current["technician_id"] != tech["id"]:
        con.execute("DELETE FROM technician_locations WHERE request_id=?", (rid,))
    con.execute(
        "UPDATE requests SET status='assigned', technician_id=?, assigned_at=? WHERE id=?",
        (tech["id"], now(), rid),
    )
    log(con, rid, f"Assigned to {tech['name']}. Waiting for acceptance.")


def job_has_progress_evidence(con, rid):
    request = con.execute(
        "SELECT arrival_checked_in_at FROM requests WHERE id=?",
        (rid,),
    ).fetchone()
    if request and request["arrival_checked_in_at"]:
        return True
    return con.execute(
        """SELECT 1 FROM job_photos WHERE request_id=?
           UNION ALL
           SELECT 1 FROM extra_charge_proposals WHERE request_id=?
           LIMIT 1""",
        (rid, rid),
    ).fetchone() is not None


def auto_assign(con, rid):
    req = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not req:
        raise HTTPException(404, "Request not found")
    if (
        req["status"] == "accepted"
        and req["technician_id"] is not None
        and job_has_progress_evidence(con, rid)
    ):
        raise HTTPException(409, "This accepted job has check-in, photo, or price evidence and cannot be reassigned")
    tech = pick_technician(con, req)
    if tech:
        assign_to(con, rid, tech)
        return True
    con.execute("UPDATE requests SET status='unassigned', technician_id=NULL WHERE id=?", (rid,))
    con.execute("DELETE FROM technician_locations WHERE request_id=?", (rid,))
    log(con, rid, f"No free {req['category']} technician right now. Request is queued.")
    return False


def retry_unassigned(con):
    rows = con.execute(
        """SELECT * FROM requests WHERE status='unassigned' AND category!='Other'
           ORDER BY CASE urgency WHEN 'emergency' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END, id"""
    ).fetchall()
    for r in rows:
        tech = pick_technician(con, r)
        if tech:
            assign_to(con, r["id"], tech)


def sweep(con):
    cutoff = (datetime.now() - timedelta(minutes=ASSIGN_TIMEOUT_MIN)).isoformat(timespec="seconds")
    location_cutoff = (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds")
    con.execute(
        """DELETE FROM technician_locations
           WHERE updated_at < ?
              OR NOT EXISTS (
                  SELECT 1 FROM requests r
                  WHERE r.id=technician_locations.request_id
                    AND r.technician_id=technician_locations.technician_id
                    AND r.status='accepted'
              )""",
        (location_cutoff,),
    )
    stale = con.execute("SELECT * FROM requests WHERE status='assigned' AND assigned_at < ?", (cutoff,)).fetchall()
    for r in stale:
        con.execute("INSERT INTO declines VALUES(?,?,?)", (r["id"], r["technician_id"], now()))
        log(con, r["id"], "Technician did not respond in time. Reassigning.")
        auto_assign(con, r["id"])
    auto_confirm_cutoff = (datetime.now() - timedelta(hours=AUTO_CONFIRM_HOURS)).isoformat(timespec="seconds")
    completed = con.execute(
        """SELECT r.id FROM requests r
           JOIN payments p ON p.request_id=r.id AND p.status='awaiting_confirmation'
           WHERE r.status='work_done' AND r.completed_at IS NOT NULL AND r.completed_at<=?
             AND EXISTS (
                 SELECT 1 FROM job_photos
                 WHERE request_id=r.id AND technician_id=r.technician_id AND phase='before'
             )
             AND EXISTS (
                 SELECT 1 FROM job_photos
                 WHERE request_id=r.id AND technician_id=r.technician_id AND phase='after'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM extra_charge_proposals
                 WHERE request_id=r.id AND status='pending'
             )
           ORDER BY r.id""",
        (auto_confirm_cutoff,),
    ).fetchall()
    for request in completed:
        release_payment(con, request["id"], confirmation_source="automatic after 24 hours without a customer dispute")
    retry_unassigned(con)


async def sweeper():
    while True:
        await asyncio.sleep(60)
        try:
            with db() as con:
                sweep(con)
        except Exception as e:
            print("sweep error:", e)


# ---------------------------------------------------------------- payments
def release_payment(con, rid, rating=None, confirmation_source="customer"):
    pay = con.execute("SELECT * FROM payments WHERE request_id=?", (rid,)).fetchone()
    if not pay or pay["status"] not in ("awaiting_confirmation", "disputed"):
        raise HTTPException(400, "Payment cannot be released in its current state")
    con.execute("UPDATE payments SET status='released', released_at=? WHERE id=?", (now(), pay["id"]))
    con.execute("UPDATE requests SET status='confirmed', confirmed_at=? WHERE id=?", (now(), rid))
    con.execute("DELETE FROM technician_locations WHERE request_id=?", (rid,))
    t = con.execute("SELECT * FROM technicians WHERE id=?", (pay["technician_id"],)).fetchone()
    new_rating, cnt = t["rating"], t["rating_count"]
    if rating:
        new_rating = round((t["rating"] * cnt + rating) / (cnt + 1), 2)
        cnt += 1
    # Immutable ledger entry (amounts in paisa / integer minor units)
    gross_paisa = int(round(pay["amount"] * 100))
    commission_paisa = int(round(pay["commission"] * 100))
    net_paisa = int(round(pay["payout"] * 100))
    con.execute(
        """INSERT INTO commission_ledger(request_id, technician_id, gross_amount, commission_amount,
           net_payout, commission_pct, status, note, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (rid, pay["technician_id"], gross_paisa, commission_paisa, net_paisa,
         COMMISSION_PCT, "released", confirmation_source, now()),
    )
    con.execute(
        "UPDATE technicians SET wallet=wallet+?, jobs_done=jobs_done+1, rating=?, rating_count=? WHERE id=?",
        (pay["payout"], new_rating, cnt, t["id"]),
    )
    if confirmation_source == "customer":
        message = f"Customer confirmed. Rs {pay['payout']:.0f} released to technician."
    else:
        message = f"Automatically confirmed ({confirmation_source}). Rs {pay['payout']:.0f} released to technician."
    log(con, rid, message)
    retry_unassigned(con)


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(sweeper())
    yield
    task.cancel()


app = FastAPI(title="HomeServe Local", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.get("/")
def page_index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/tech")
def page_tech():
    return FileResponse(os.path.join(BASE, "static", "tech.html"))


@app.get("/terms")
def page_terms():
    return FileResponse(os.path.join(BASE, "static", "terms.html"))


@app.get("/admin")
def page_admin():
    return FileResponse(os.path.join(BASE, "static", "admin.html"))


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def page_favicon():
    return FileResponse(
        os.path.join(BASE, "static", "favicon.svg"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/meta")
def meta():
    return {"categories": CATEGORIES, "commission_pct": COMMISSION_PCT}


@app.get("/api/catalog")
def public_catalog():
    with db() as con:
        items = [dict(row) for row in con.execute(
            """SELECT id,category,service_name,description,price,visit_fee
               FROM service_catalog WHERE active=1 ORDER BY category,service_name""")]
    return {"items": items}


# ---- models
class RequestIn(BaseModel):
    name: str
    phone: str
    address: str
    area: str = ""
    description: str
    category: Optional[str] = None
    service_id: Optional[int] = None
    terms_accepted: bool = False


class DoneIn(BaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    note: str = ""


class ConfirmIn(BaseModel):
    rating: int = 5


class ReasonIn(BaseModel):
    reason: str = ""


class TechIn(BaseModel):
    name: str
    phone: str
    pin: str
    category: str
    area: str = ""


class TechUpdateIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    pin: Optional[str] = None
    category: Optional[str] = None
    area: Optional[str] = None


class LocationIn(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy: Optional[float] = Field(default=None, ge=0, le=10000, allow_inf_nan=False)


class AssignIn(BaseModel):
    technician_id: int


class PayoutIn(BaseModel):
    reference: str = ""


class AvailIn(BaseModel):
    available: bool


class ResolveIn(BaseModel):
    action: str  # release | cancel


class ApplicationReviewIn(BaseModel):
    action: str
    review_note: str = ""


class CatalogItemIn(BaseModel):
    category: str
    service_name: str
    description: str = ""
    price: float = Field(ge=0, allow_inf_nan=False)
    visit_fee: float = Field(default=0, ge=0, allow_inf_nan=False)
    active: bool = True


class ExtraChargeIn(BaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=5, max_length=500)


class ExtraChargeDecisionIn(BaseModel):
    decision: str


class DisputeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    evidence: str = ""


class DisputeResolveIn(BaseModel):
    resolution: str = Field(..., min_length=3, max_length=500)
    refund_amount: float = 0.0


class QuoteRevisionIn(BaseModel):
    quoted_price: float = Field(..., ge=0)
    visit_fee: float = Field(0, ge=0)
    reason: str = Field(..., min_length=3, max_length=300)


@app.post("/api/technician-applications")
async def technician_application(
    name: str = Form(...),
    phone: str = Form(...),
    pin: str = Form(...),
    category: str = Form(...),
    area: str = Form(...),
    experience: str = Form(""),
    terms_accepted: bool = Form(False),
    privacy_accepted: bool = Form(False),
    cnic_front: UploadFile = File(...),
    cnic_back: UploadFile = File(...),
    profile_photo: UploadFile = File(...),
    skill_certificate: Optional[UploadFile] = File(None),
    police_certificate: Optional[UploadFile] = File(None),
):
    normalized_phone = norm_phone(phone)
    if not name.strip() or len(normalized_phone) < 10 or len(pin) < 8:
        raise HTTPException(400, "Enter your name, a valid phone number, and a PIN of at least 8 characters")
    if category not in CATEGORIES or category == "Other":
        raise HTTPException(400, "Choose a specific service category")
    if len(area.strip()) < 2 or len(experience) > 2000:
        raise HTTPException(400, "Enter your service area and keep experience details under 2,000 characters")
    if not terms_accepted or not privacy_accepted:
        raise HTTPException(400, "Accept the provider terms and privacy notice to submit your application")
    uploads = {
        "cnic_front": cnic_front,
        "cnic_back": cnic_back,
        "profile_photo": profile_photo,
    }
    if skill_certificate and skill_certificate.filename:
        uploads["skill_certificate"] = skill_certificate
    if police_certificate and police_certificate.filename:
        uploads["police_certificate"] = police_certificate
    stored_files = []
    try:
        for document_type, upload in uploads.items():
            allow_pdf = document_type != "profile_photo"
            stored_path, media_type = await _store_upload(
                upload, "documents", DOCUMENT_MAX_BYTES, allow_pdf=allow_pdf,
            )
            stored_files.append(stored_path)
            uploads[document_type] = (stored_path, media_type)
        with db() as con:
            duplicate = con.execute(
                """SELECT 1 FROM technicians WHERE phone=?
                   UNION ALL
                   SELECT 1 FROM technician_applications
                   WHERE phone=? AND status='pending' LIMIT 1""",
                (normalized_phone, normalized_phone),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "This phone number already has an account or pending application")
            cur = con.execute(
                """INSERT INTO technician_applications(
                     name,phone,pin_hash,category,area,experience,terms_version,
                     terms_accepted_at,privacy_version,privacy_accepted_at,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?)""",
                (name.strip(), normalized_phone, hp(pin), category, area.strip(),
                 experience.strip(), TERMS_VERSION, now(), PRIVACY_VERSION, now(), now()),
            )
            application_id = cur.lastrowid
            for document_type, stored_file in uploads.items():
                stored_path, media_type = stored_file
                con.execute(
                    """INSERT INTO private_documents(
                         application_id,document_type,stored_path,media_type,uploaded_at)
                       VALUES(?,?,?,?,?)""",
                    (application_id, document_type, stored_path, media_type, now()),
                )
    except sqlite3.IntegrityError as exc:
        for stored_path in stored_files:
            (PRIVATE_STORAGE / stored_path).unlink(missing_ok=True)
        raise HTTPException(409, "This phone number already has an account or pending application") from exc
    except Exception:
        for stored_path in stored_files:
            (PRIVATE_STORAGE / stored_path).unlink(missing_ok=True)
        raise
    return {"ok": True, "application_id": application_id, "status": "pending"}


@app.get("/api/admin/applications")
def admin_applications(x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        applications = [dict(row) for row in con.execute(
            """SELECT id,name,phone,category,area,experience,status,review_note,created_at
               FROM technician_applications ORDER BY
                 CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC""")]
        for application in applications:
            application["documents"] = [
                dict(document) for document in con.execute(
                    """SELECT id,document_type,media_type,uploaded_at FROM private_documents
                       WHERE application_id=? ORDER BY id""",
                    (application["id"],),
                )
            ]
    return {"applications": applications}


@app.get("/api/admin/applications/{application_id}/documents/{document_id}")
def admin_application_document(
    application_id: int, document_id: int, x_admin_key: str = Header(None),
):
    admin_auth(x_admin_key)
    with db() as con:
        document = con.execute(
            """SELECT stored_path,media_type FROM private_documents
               WHERE id=? AND application_id=?""",
            (document_id, application_id),
        ).fetchone()
    if not document:
        raise HTTPException(404, "Application document not found")
    return _private_file_response(document["stored_path"], document["media_type"], download=True)


@app.post("/api/admin/applications/{application_id}/review")
def admin_review_application(
    application_id: int, body: ApplicationReviewIn, x_admin_key: str = Header(None),
):
    admin_auth(x_admin_key)
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject")
    if body.action == "reject" and not body.review_note.strip():
        raise HTTPException(400, "Add a reason before rejecting an application")
    try:
        with db() as con:
            application = con.execute(
                "SELECT * FROM technician_applications WHERE id=? AND status='pending'",
                (application_id,),
            ).fetchone()
            if not application:
                raise HTTPException(404, "Pending application not found")
            if body.action == "approve":
                required = {row["document_type"] for row in con.execute(
                    "SELECT document_type FROM private_documents WHERE application_id=?",
                    (application_id,),
                )}
                if not {"cnic_front", "cnic_back", "profile_photo"}.issubset(required):
                    raise HTTPException(400, "Required identity and profile documents are missing")
                con.execute(
                    """INSERT INTO technicians(
                         name,phone,pin_hash,category,area,available,active,created_at,application_id)
                       VALUES(?,?,?,?,?,0,1,?,?)""",
                    (application["name"], application["phone"], application["pin_hash"],
                     application["category"], application["area"], now(), application_id),
                )
                status = "approved"
            else:
                status = "rejected"
            con.execute(
                """UPDATE technician_applications SET status=?,review_note=?,reviewed_at=?
                   WHERE id=?""",
                (status, body.review_note.strip(), now(), application_id),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A technician account already uses this phone number") from exc
    return {"ok": True, "status": status}


@app.get("/api/admin/job-photos/{photo_id}")
def admin_job_photo(photo_id: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        photo = con.execute(
            "SELECT stored_path,media_type FROM job_photos WHERE id=?",
            (photo_id,),
        ).fetchone()
    if not photo:
        raise HTTPException(404, "Job photo not found")
    return _private_file_response(photo["stored_path"], photo["media_type"])


@app.get("/api/catalog/{item_id}")
def get_catalog_item(item_id: int):
    with db() as con:
        item = con.execute(
            """SELECT id,category,service_name,description,price,visit_fee
               FROM service_catalog WHERE id=? AND active=1""",
            (item_id,),
        ).fetchone()
    if not item:
        raise HTTPException(404, "Service is unavailable")
    return dict(item)


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    contents = await upload.read(max_bytes + 1)
    await upload.close()
    if len(contents) > max_bytes:
        raise HTTPException(413, f"File is too large. Maximum size is {max_bytes // (1024 * 1024)} MB")
    return contents


def _clean_image(contents: bytes) -> bytes:
    try:
        with Image.open(BytesIO(contents)) as image:
            if image.width * image.height > IMAGE_MAX_PIXELS:
                raise HTTPException(400, "Image dimensions are too large")
            image.verify()
        with Image.open(BytesIO(contents)) as image:
            image.load()
            image = ImageOps.exif_transpose(image)
            image.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION))
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            elif image.mode == "L":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Upload a valid JPEG, PNG, or WebP image") from exc


async def _store_upload(
    upload: UploadFile, folder: str, max_bytes: int, *, allow_pdf: bool = False,
) -> tuple[str, str]:
    contents = await _read_upload(upload, max_bytes)
    if contents.startswith(b"%PDF-"):
        if not allow_pdf:
            raise HTTPException(400, "This upload must be an image")
        extension, media_type = ".pdf", "application/pdf"
        safe_contents = contents
    else:
        safe_contents = _clean_image(contents)
        extension, media_type = ".jpg", "image/jpeg"
    target_dir = PRIVATE_STORAGE / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_path = target_dir / f"{secrets.token_hex(24)}{extension}"
    stored_path.write_bytes(safe_contents)
    return str(stored_path.relative_to(PRIVATE_STORAGE)), media_type


def _private_file_response(stored_path: str, media_type: str, *, download: bool = False):
    storage_root = PRIVATE_STORAGE.resolve()
    path = (storage_root / stored_path).resolve()
    if not path.is_relative_to(storage_root) or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


# ---- public: create + track
@app.post("/api/requests")
def create_request(body: RequestIn):
    if not body.terms_accepted:
        raise HTTPException(400, "Accept the customer terms and privacy notice to continue")
    if len(norm_phone(body.phone)) < 10:
        raise HTTPException(400, "Enter a valid phone number")
    if len(body.description.strip()) < 5 or not body.name.strip() or not body.address.strip():
        raise HTTPException(400, "Name, address and problem description are required")
    ai = ai_analyze(body.description)
    token = secrets.token_urlsafe(10)
    with db() as con:
        catalog_item = None
        if body.service_id is not None:
            catalog_item = con.execute(
                "SELECT * FROM service_catalog WHERE id=? AND active=1",
                (body.service_id,),
            ).fetchone()
            if not catalog_item:
                raise HTTPException(400, "The selected service is no longer available")
        category = (
            catalog_item["category"] if catalog_item
            else body.category if body.category in CATEGORIES
            else ai["category"]
        )
        cur = con.execute(
            """INSERT INTO requests(token,customer_name,customer_phone,address,area,description,
               category,urgency,ai_summary,status,service_id,quoted_price,visit_fee,
               customer_terms_version,customer_terms_accepted_at,
               customer_privacy_version,customer_privacy_accepted_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token, body.name.strip(), norm_phone(body.phone), body.address.strip(), body.area.strip(),
             body.description.strip(), category, ai["urgency"], ai["summary"], "new",
             catalog_item["id"] if catalog_item else None,
             catalog_item["price"] if catalog_item else None,
             catalog_item["visit_fee"] if catalog_item else None,
             TERMS_VERSION, now(), PRIVACY_VERSION, now(), now()),
        )
        rid = cur.lastrowid
        log(con, rid, f"Request received. Service: {category}. Urgency: {ai['urgency']}.")
        if category == "Other":
            con.execute("UPDATE requests SET status='unassigned' WHERE id=?", (rid,))
            log(con, rid, "Needs manual assignment by admin.")
        else:
            auto_assign(con, rid)
        status = con.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0]
    return {
        "id": rid, "token": token, "status": status, "category": category,
        "quoted_price": catalog_item["price"] if catalog_item else None,
        "visit_fee": catalog_item["visit_fee"] if catalog_item else None,
    }


def req_by_token(con, token):
    r = con.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
    if not r:
        raise HTTPException(404, "Request not found")
    return r


@app.get("/api/track/{token}")
def track(token: str):
    with db() as con:
        r = req_by_token(con, token)
        t = con.execute("SELECT name, phone FROM technicians WHERE id=?", (r["technician_id"],)).fetchone()
        ev = con.execute("SELECT text, at FROM events WHERE request_id=? ORDER BY id", (r["id"],)).fetchall()
        d = dict(r)
        if r["status"] not in ("accepted", "work_done", "confirmed", "disputed"):
            d["address"] = (r["area"] or "Area only") + " (full address shared after technician accepts)"
        d.pop("token")
        d["tech_name"] = t["name"] if t else None
        d["tech_phone"] = t["phone"] if (t and r["status"] in ("accepted", "work_done", "confirmed", "disputed")) else None
        d["extra_charges"] = [
            dict(charge) for charge in con.execute(
                """SELECT id,amount,reason,status,created_at,reviewed_at
                   FROM extra_charge_proposals WHERE request_id=? ORDER BY id""",
                (r["id"],),
            )
        ]
        d["photos"] = [
            {"id": photo["id"], "phase": photo["phase"], "uploaded_at": photo["uploaded_at"],
             "url": f"/api/track/{token}/photos/{photo['id']}"}
            for photo in con.execute(
                """SELECT id,phase,uploaded_at FROM job_photos
                   WHERE request_id=? ORDER BY id""",
                (r["id"],),
            )
        ]
        d["technician_location"] = None
        if r["status"] == "accepted":
            location = con.execute(
                """SELECT latitude,longitude,updated_at FROM technician_locations
                   WHERE request_id=? AND technician_id=? AND updated_at>=?""",
                (r["id"], r["technician_id"],
                 (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds")),
            ).fetchone()
            if location:
                d["technician_location"] = dict(location)
        d["events"] = [dict(e) for e in ev]
        return d


@app.get("/api/track/{token}/photos/{photo_id}")
def customer_job_photo(token: str, photo_id: int):
    with db() as con:
        request = req_by_token(con, token)
        photo = con.execute(
            """SELECT stored_path,media_type FROM job_photos
               WHERE id=? AND request_id=?""",
            (photo_id, request["id"]),
        ).fetchone()
    if not photo:
        raise HTTPException(404, "Job photo not found")
    return _private_file_response(photo["stored_path"], photo["media_type"])


@app.post("/api/track/{token}/confirm")
def customer_confirm(token: str, body: ConfirmIn):
    with db() as con:
        r = req_by_token(con, token)
        if r["status"] != "work_done":
            raise HTTPException(400, "Technician has not marked the work done yet")
        pending = con.execute(
            "SELECT 1 FROM extra_charge_proposals WHERE request_id=? AND status='pending' LIMIT 1",
            (r["id"],),
        ).fetchone()
        if pending:
            raise HTTPException(400, "Review or reject the pending extra-work charge before confirming")
        release_payment(con, r["id"], max(1, min(5, body.rating)))
    return {"ok": True}


@app.post("/api/track/{token}/extra-charges/{charge_id}")
def customer_review_extra_charge(token: str, charge_id: int, body: ExtraChargeDecisionIn):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject")
    with db() as con:
        request = req_by_token(con, token)
        if request["status"] != "accepted":
            raise HTTPException(400, "Extra work can only be reviewed while the job is active")
        charge = con.execute(
            """SELECT * FROM extra_charge_proposals
               WHERE id=? AND request_id=? AND status='pending'""",
            (charge_id, request["id"]),
        ).fetchone()
        if not charge:
            raise HTTPException(404, "Pending extra-work proposal not found")
        con.execute(
            "UPDATE extra_charge_proposals SET status=?,reviewed_at=? WHERE id=?",
            ("approved" if body.decision == "approve" else "rejected", now(), charge_id),
        )
        log(
            con, request["id"],
            f"Customer {body.decision}d the Rs {charge['amount']:.0f} extra-work proposal.",
        )
    return {"ok": True}


@app.get("/api/tech/jobs/{jid}/photos/{photo_id}")
def tech_job_photo(jid: int, photo_id: int, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        tech = tech_auth(con, x_phone, x_pin)
        photo = con.execute(
            """SELECT p.stored_path,p.media_type FROM job_photos p
               JOIN requests r ON r.id=p.request_id
               WHERE p.id=? AND p.request_id=? AND p.technician_id=? AND r.technician_id=?""",
            (photo_id, jid, tech["id"], tech["id"]),
        ).fetchone()
    if not photo:
        raise HTTPException(404, "Job photo not found")
    return _private_file_response(photo["stored_path"], photo["media_type"])


# Old simple dispute endpoint replaced by structured version below


@app.post("/api/track/{token}/cancel")
def customer_cancel(token: str):
    with db() as con:
        r = req_by_token(con, token)
        if r["status"] not in ("new", "unassigned", "assigned"):
            raise HTTPException(400, "Too late to cancel. Contact admin.")
        con.execute("UPDATE requests SET status='cancelled', technician_id=NULL WHERE id=?", (r["id"],))
        con.execute("DELETE FROM technician_locations WHERE request_id=?", (r["id"],))
        log(con, r["id"], "Cancelled by customer.")
    return {"ok": True}


# ---- technician portal
def tech_auth(con, phone, pin):
    t = con.execute(
        "SELECT * FROM technicians WHERE phone=? AND pin_hash=? AND active=1",
        (norm_phone(phone), hp(pin or "")),
    ).fetchone()
    if not t:
        raise HTTPException(401, "Wrong phone or PIN")
    return t


@app.get("/api/tech/me")
def tech_me(x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        jobs = [dict(r) for r in con.execute(
            """SELECT r.*,
                      CASE WHEN l.request_id IS NULL THEN 0 ELSE 1 END AS location_shared
               FROM requests r LEFT JOIN technician_locations l
                 ON l.request_id=r.id AND l.technician_id=r.technician_id AND r.status='accepted'
               WHERE r.technician_id=? ORDER BY r.id DESC LIMIT 100""", (t["id"],))]
        for j in jobs:
            j.pop("token", None)
            j["photos"] = [
                dict(photo) for photo in con.execute(
                    """SELECT id,phase,uploaded_at FROM job_photos
                       WHERE request_id=? AND technician_id=? ORDER BY id""",
                    (j["id"], t["id"]),
                )
            ]
            j["approved_extra_amount"] = con.execute(
                """SELECT COALESCE(SUM(amount),0) FROM extra_charge_proposals
                   WHERE request_id=? AND status='approved'""",
                (j["id"],),
            ).fetchone()[0]
            j["pending_extra_charge"] = con.execute(
                """SELECT 1 FROM extra_charge_proposals
                   WHERE request_id=? AND status='pending' LIMIT 1""",
                (j["id"],),
            ).fetchone() is not None
            if j["status"] == "assigned":  # hide contact details until accepted
                j["customer_phone"], j["customer_name"] = "", ""
                j["address"] = "(shown after you accept)"
        me = {k: t[k] for k in ("id", "name", "phone", "category", "area", "available", "rating", "jobs_done", "wallet")}
        return {"me": me, "jobs": jobs}


@app.post("/api/tech/availability")
def tech_availability(body: AvailIn, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        con.execute("UPDATE technicians SET available=? WHERE id=?", (1 if body.available else 0, t["id"]))
        if body.available:
            retry_unassigned(con)
    return {"ok": True}


def my_job(con, t, jid, status):
    r = con.execute("SELECT * FROM requests WHERE id=?", (jid,)).fetchone()
    if not r or r["technician_id"] != t["id"] or r["status"] != status:
        raise HTTPException(400, "Job not available for this action")
    return r


@app.post("/api/tech/jobs/{jid}/accept")
def tech_accept(jid: int, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        my_job(con, t, jid, "assigned")
        con.execute("UPDATE requests SET status='accepted' WHERE id=?", (jid,))
        log(con, jid, f"{t['name']} accepted the job.")
    return {"ok": True}


@app.post("/api/tech/jobs/{jid}/location")
def tech_update_location(
    jid: int, body: LocationIn, x_phone: str = Header(None), x_pin: str = Header(None),
):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        job = my_job(con, t, jid, "accepted")
        result = con.execute(
            """INSERT INTO technician_locations(request_id,technician_id,latitude,longitude,accuracy,updated_at)
               SELECT ?,?,?,?,?,?
               WHERE EXISTS (
                   SELECT 1 FROM requests
                   WHERE id=? AND technician_id=? AND status='accepted'
               )
               ON CONFLICT(request_id) DO UPDATE SET
                 technician_id=excluded.technician_id,
                 latitude=excluded.latitude,
                 longitude=excluded.longitude,
                 accuracy=excluded.accuracy,
                 updated_at=excluded.updated_at
               WHERE EXISTS (
                   SELECT 1 FROM requests
                   WHERE id=? AND technician_id=? AND status='accepted'
               )""",
            (job["id"], t["id"], body.latitude, body.longitude, body.accuracy, now(),
             job["id"], t["id"], job["id"], t["id"]),
        )
        if result.rowcount != 1:
            raise HTTPException(400, "Live location is available only while your job is accepted")
    return {"ok": True}


@app.post("/api/tech/jobs/{jid}/check-in")
def tech_check_in(
    jid: int, body: LocationIn, x_phone: str = Header(None), x_pin: str = Header(None),
):
    if body.accuracy is None or body.accuracy > CHECK_IN_MAX_ACCURACY_METERS:
        raise HTTPException(
            400,
            f"GPS accuracy must be {CHECK_IN_MAX_ACCURACY_METERS} meters or better to check in",
        )
    with db() as con:
        tech = tech_auth(con, x_phone, x_pin)
        job = my_job(con, tech, jid, "accepted")
        if job["arrival_checked_in_at"]:
            raise HTTPException(409, "You have already checked in for this job")
        checked_in_at = now()
        result = con.execute(
            """UPDATE requests SET arrival_checked_in_at=?,arrival_latitude=?,
                      arrival_longitude=?,arrival_accuracy=?
               WHERE id=? AND technician_id=? AND status='accepted'
                 AND arrival_checked_in_at IS NULL""",
            (checked_in_at, body.latitude, body.longitude, body.accuracy, jid, tech["id"]),
        )
        if result.rowcount != 1:
            raise HTTPException(409, "This job can no longer be checked in")
        log(con, jid, f"{tech['name']} recorded a GPS arrival check-in (accuracy about {body.accuracy:.0f} m).")
    return {"ok": True, "checked_in_at": checked_in_at}


@app.delete("/api/tech/jobs/{jid}/location")
def tech_stop_location(jid: int, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        job = con.execute(
            "SELECT * FROM requests WHERE id=? AND technician_id=?",
            (jid, t["id"]),
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        con.execute("DELETE FROM technician_locations WHERE request_id=?", (jid,))
    return {"ok": True}


@app.post("/api/tech/jobs/{jid}/decline")
def tech_decline(jid: int, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        my_job(con, t, jid, "assigned")
        con.execute("INSERT INTO declines VALUES(?,?,?)", (jid, t["id"], now()))
        log(con, jid, f"{t['name']} declined. Finding another technician.")
        auto_assign(con, jid)
    return {"ok": True}


@app.post("/api/tech/jobs/{jid}/done")
def tech_done(jid: int, body: DoneIn, x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        job = my_job(con, t, jid, "accepted")
        if not job["arrival_checked_in_at"]:
            raise HTTPException(400, "Record your GPS arrival check-in before marking this job done")
        photo_phases = {
            row["phase"] for row in con.execute(
                """SELECT DISTINCT phase FROM job_photos
                   WHERE request_id=? AND technician_id=?""",
                (jid, t["id"]),
            )
        }
        if not {"before", "after"}.issubset(photo_phases):
            raise HTTPException(400, "Upload at least one before-work and one after-work photo before marking the job done")
        if job["quoted_price"] is not None:
            pending = con.execute(
                """SELECT 1 FROM extra_charge_proposals
                   WHERE request_id=? AND status='pending' LIMIT 1""",
                (jid,),
            ).fetchone()
            if pending:
                raise HTTPException(400, "Wait for the customer to approve or reject the extra-work proposal")
            approved_extra = con.execute(
                """SELECT COALESCE(SUM(amount),0) FROM extra_charge_proposals
                   WHERE request_id=? AND status='approved'""",
                (jid,),
            ).fetchone()[0]
            agreed_total = round(job["quoted_price"] + (job["visit_fee"] or 0) + approved_extra, 2)
            if body.amount > agreed_total:
                raise HTTPException(
                    400,
                    f"Final amount cannot exceed the agreed Rs {agreed_total:.2f}; request customer approval for extra work first",
                )
        commission = round(body.amount * COMMISSION_PCT / 100, 2)
        payout = round(body.amount - commission, 2)
        con.execute(
            "UPDATE requests SET status='work_done', amount=?, work_note=?, completed_at=? WHERE id=?",
            (body.amount, body.note, now(), jid),
        )
        con.execute("DELETE FROM technician_locations WHERE request_id=?", (jid,))
        con.execute(
            """INSERT OR REPLACE INTO payments(request_id,technician_id,amount,commission,payout,status,created_at)
               VALUES(?,?,?,?,?,'awaiting_confirmation',?)""",
            (jid, t["id"], body.amount, commission, payout, now()),
        )
        log(con, jid, f"{t['name']} marked work done. Amount Rs {body.amount:.0f}. Waiting for customer confirmation.")
        retry_unassigned(con)
    return {"ok": True}


# ---- admin
def admin_auth(key):
    if not secrets.compare_digest(key or "", ADMIN_KEY):
        raise HTTPException(401, "Wrong admin key")



@app.get("/api/tech/statement")
def tech_statement(x_phone: str = Header(None), x_pin: str = Header(None)):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        rows = con.execute(
            """SELECT cl.*, r.token, r.category, r.area, r.created_at AS request_created
               FROM commission_ledger cl
               JOIN requests r ON r.id = cl.request_id
               WHERE cl.technician_id = ?
               ORDER BY cl.id DESC LIMIT 100""",
            (t["id"],),
        ).fetchall()
        total_net = sum(r["net_payout"] for r in rows) or 0
        return {
            "technician": t["name"],
            "wallet": t["wallet"],
            "entries": [dict(r) for r in rows],
            "total_net_paisa": total_net,
        }



@app.post("/api/track/{token}/dispute")
def raise_dispute(token: str, body: DisputeIn):
    with db() as con:
        r = req_by_token(con, token)
        if r["status"] not in ("work_done", "confirmed", "accepted", "disputed"):
            raise HTTPException(400, "Dispute can only be raised on active or recently completed jobs")
        existing = con.execute("SELECT id FROM disputes WHERE request_id=?", (r["id"],)).fetchone()
        if existing:
            raise HTTPException(409, "A dispute already exists for this request")
        con.execute(
            """INSERT INTO disputes(request_id, raised_by, reason, evidence, status, created_at)
               VALUES(?,?,?,?,?,?)""",
            (r["id"], "customer", body.reason.strip(), body.evidence.strip(), "open", now()),
        )
        con.execute("UPDATE requests SET status='disputed' WHERE id=?", (r["id"],))
        con.execute("DELETE FROM technician_locations WHERE request_id=?", (r["id"],))
        con.execute("UPDATE payments SET status='disputed' WHERE request_id=?", (r["id"],))
        log(con, r["id"], f"Customer raised dispute: {body.reason.strip()[:80]}")
    return {"ok": True, "status": "disputed"}


@app.post("/api/admin/disputes/{request_id}/resolve")
def admin_resolve_dispute(
    request_id: int, body: DisputeResolveIn, x_admin_key: str = Header(None)
):
    admin_auth(x_admin_key)
    with db() as con:
        d = con.execute("SELECT * FROM disputes WHERE request_id=? AND status='open'", (request_id,)).fetchone()
        if not d:
            raise HTTPException(404, "Open dispute not found")
        con.execute(
            "UPDATE disputes SET status='resolved', resolution=?, resolved_at=? WHERE request_id=?",
            (body.resolution.strip(), now(), request_id),
        )
        # Optional refund adjustment note in ledger if refund_amount > 0
        if body.refund_amount and body.refund_amount > 0:
            con.execute(
                """INSERT INTO commission_ledger(request_id, technician_id, gross_amount, commission_amount,
                   net_payout, commission_pct, status, note, created_at)
                   SELECT ?, technician_id, 0, 0, ?, commission_pct, 'adjustment', ?, ?
                   FROM commission_ledger WHERE request_id=? ORDER BY id DESC LIMIT 1""",
                (request_id, -int(round(body.refund_amount * 100)), f"Refund: {body.resolution[:100]}", now(), request_id),
            )
        con.execute("UPDATE requests SET status='confirmed' WHERE id=? AND status='disputed'", (request_id,))
        log(con, request_id, f"Dispute resolved: {body.resolution.strip()[:100]}")
    return {"ok": True}


@app.post("/api/admin/requests/{rid}/quote-revision")
def admin_quote_revision(
    rid: int, body: QuoteRevisionIn, x_admin_key: str = Header(None)
):
    admin_auth(x_admin_key)
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        con.execute(
            """INSERT INTO quote_revisions(request_id, quoted_price, visit_fee, reason, created_by, created_at)
               VALUES(?,?,?,?,?,?)""",
            (rid, body.quoted_price, body.visit_fee, body.reason.strip(), "admin", now()),
        )
        con.execute(
            "UPDATE requests SET quoted_price=?, visit_fee=? WHERE id=?",
            (body.quoted_price, body.visit_fee, rid),
        )
        log(con, rid, f"Quote revised to Rs {body.quoted_price:.0f} (+ visit {body.visit_fee:.0f}): {body.reason.strip()[:60]}")
    return {"ok": True}


@app.get("/api/requests/{rid}/quotes")
def list_quote_revisions(rid: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        rows = con.execute(
            "SELECT * FROM quote_revisions WHERE request_id=? ORDER BY id",
            (rid,),
        ).fetchall()
        return {"revisions": [dict(r) for r in rows]}


@app.get("/api/admin/dashboard")
def admin_dashboard(x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        sweep(con)
        reqs = [dict(r) for r in con.execute(
            """SELECT r.*, t.name AS tech_name,
                      l.latitude AS location_latitude,l.longitude AS location_longitude,
                      l.updated_at AS location_updated_at
               FROM requests r
               LEFT JOIN technicians t ON t.id=r.technician_id
               LEFT JOIN technician_locations l
                 ON l.request_id=r.id AND l.technician_id=r.technician_id
                 AND r.status='accepted' AND l.updated_at>=?
               ORDER BY r.id DESC LIMIT 200""",
            ((datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds"),))]
        techs = [dict(r) for r in con.execute("SELECT * FROM technicians ORDER BY category, name")]
        for t in techs:
            t.pop("pin_hash", None)
        pays = [dict(r) for r in con.execute(
            """SELECT p.*, t.name AS tech_name, t.phone AS tech_phone FROM payments p
               JOIN technicians t ON t.id=p.technician_id ORDER BY p.id DESC LIMIT 200""")]
        s = lambda q: con.execute(q).fetchone()[0] or 0
        stats = {
            "open": s("SELECT COUNT(*) FROM requests WHERE status IN ('new','unassigned','assigned','accepted')"),
            "unassigned": s("SELECT COUNT(*) FROM requests WHERE status='unassigned'"),
            "awaiting_customer": s("SELECT COUNT(*) FROM requests WHERE status='work_done'"),
            "disputed": s("SELECT COUNT(*) FROM requests WHERE status='disputed'"),
            "payout_due": s("SELECT SUM(payout) FROM payments WHERE status='released'"),
            "commission_earned": s("SELECT SUM(commission) FROM payments WHERE status IN ('released','paid_out')"),
        }
        for request in reqs:
            request["photos"] = [
                dict(photo) for photo in con.execute(
                    "SELECT id,phase,uploaded_at FROM job_photos WHERE request_id=? ORDER BY id",
                    (request["id"],),
                )
            ]
        catalog = [dict(row) for row in con.execute(
            """SELECT id,category,service_name,description,price,visit_fee,active,updated_at
               FROM service_catalog ORDER BY category,service_name""")]
        return {
            "requests": reqs, "technicians": techs, "payments": pays,
            "catalog": catalog, "stats": stats,
        }


@app.post("/api/admin/catalog")
def admin_add_catalog_item(body: CatalogItemIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    category = body.category.strip()
    service_name = body.service_name.strip()
    if category not in CATEGORIES or not service_name or len(service_name) > 100:
        raise HTTPException(400, "Choose a valid category and a service name under 100 characters")
    if len(body.description) > 500:
        raise HTTPException(400, "Keep the service description under 500 characters")
    with db() as con:
        cur = con.execute(
            """INSERT INTO service_catalog(category,service_name,description,price,visit_fee,active,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (category, service_name, body.description.strip(), body.price, body.visit_fee,
             1 if body.active else 0, now()),
        )
    return {"ok": True, "id": cur.lastrowid}


@app.put("/api/admin/catalog/{item_id}")
def admin_update_catalog_item(
    item_id: int, body: CatalogItemIn, x_admin_key: str = Header(None),
):
    admin_auth(x_admin_key)
    category = body.category.strip()
    service_name = body.service_name.strip()
    if category not in CATEGORIES or not service_name or len(service_name) > 100:
        raise HTTPException(400, "Choose a valid category and a service name under 100 characters")
    if len(body.description) > 500:
        raise HTTPException(400, "Keep the service description under 500 characters")
    with db() as con:
        result = con.execute(
            """UPDATE service_catalog SET category=?,service_name=?,description=?,price=?,
                      visit_fee=?,active=?,updated_at=? WHERE id=?""",
            (category, service_name, body.description.strip(), body.price, body.visit_fee,
             1 if body.active else 0, now(), item_id),
        )
        if result.rowcount != 1:
            raise HTTPException(404, "Service price not found")
    return {"ok": True}


@app.post("/api/tech/jobs/{jid}/extra-charges")
def tech_propose_extra_charge(
    jid: int, body: ExtraChargeIn, x_phone: str = Header(None), x_pin: str = Header(None),
):
    with db() as con:
        tech = tech_auth(con, x_phone, x_pin)
        my_job(con, tech, jid, "accepted")
        pending = con.execute(
            "SELECT 1 FROM extra_charge_proposals WHERE request_id=? AND status='pending' LIMIT 1",
            (jid,),
        ).fetchone()
        if pending:
            raise HTTPException(409, "Wait for the customer to review the current extra-work proposal")
        cur = con.execute(
            """INSERT INTO extra_charge_proposals(request_id,technician_id,amount,reason,created_at)
               VALUES(?,?,?,?,?)""",
            (jid, tech["id"], body.amount, body.reason.strip(), now()),
        )
        log(con, jid, f"{tech['name']} requested customer approval for Rs {body.amount:.0f} extra work.")
    return {"ok": True, "id": cur.lastrowid}


@app.post("/api/tech/jobs/{jid}/photos/{phase}")
async def tech_upload_job_photo(
    jid: int, phase: str, file: UploadFile = File(...),
    x_phone: str = Header(None), x_pin: str = Header(None),
):
    if phase not in ("before", "after"):
        raise HTTPException(400, "Photo phase must be before or after")
    stored_path = None
    try:
        with db() as con:
            tech = tech_auth(con, x_phone, x_pin)
            my_job(con, tech, jid, "accepted")
            count = con.execute(
                "SELECT COUNT(*) FROM job_photos WHERE request_id=? AND phase=?",
                (jid, phase),
            ).fetchone()[0]
            if count >= 5:
                raise HTTPException(400, f"Maximum of 5 {phase}-work photos per job")
        stored_path, media_type = await _store_upload(file, "job_photos", PHOTO_MAX_BYTES)
        with db() as con:
            tech = tech_auth(con, x_phone, x_pin)
            my_job(con, tech, jid, "accepted")
            cur = con.execute(
                """INSERT INTO job_photos(request_id,technician_id,phase,stored_path,media_type,uploaded_at)
                   VALUES(?,?,?,?,?,?)""",
                (jid, tech["id"], phase, stored_path, media_type, now()),
            )
            photo_id = cur.lastrowid
    except Exception:
        if stored_path:
            (PRIVATE_STORAGE / stored_path).unlink(missing_ok=True)
        raise
    return {"ok": True, "id": photo_id, "phase": phase}


@app.post("/api/admin/technicians")
def admin_add_tech(body: TechIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    if not body.name.strip() or body.category not in CATEGORIES or len(body.pin) < 4 or len(norm_phone(body.phone)) < 10:
        raise HTTPException(400, "Check name, category, phone (10+ digits) and PIN (4+ chars)")
    try:
        with db() as con:
            con.execute(
                "INSERT INTO technicians(name,phone,pin_hash,category,area,created_at) VALUES(?,?,?,?,?,?)",
                (body.name.strip(), norm_phone(body.phone), hp(body.pin), body.category, body.area.strip(), now()),
            )
            retry_unassigned(con)
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Phone number already registered")
    return {"ok": True}


@app.post("/api/admin/technicians/{tid}/toggle")
def admin_toggle_tech(tid: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        tech = con.execute("SELECT active FROM technicians WHERE id=?", (tid,)).fetchone()
        if not tech:
            raise HTTPException(404, "Technician not found")
        if tech["active"]:
            con.execute("UPDATE technicians SET active=0, available=0 WHERE id=?", (tid,))
            con.execute("DELETE FROM technician_locations WHERE technician_id=?", (tid,))
        else:
            con.execute("UPDATE technicians SET active=1 WHERE id=?", (tid,))
    return {"ok": True}


@app.put("/api/admin/technicians/{tid}")
def admin_update_tech(tid: int, body: TechUpdateIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        tech = con.execute("SELECT * FROM technicians WHERE id=?", (tid,)).fetchone()
        if not tech:
            raise HTTPException(404, "Technician not found")

        name = (body.name if body.name is not None else tech["name"]).strip()
        phone = norm_phone(body.phone if body.phone is not None else tech["phone"])
        category = body.category if body.category is not None else tech["category"]
        area = (body.area if body.area is not None else tech["area"] or "").strip()
        pin = body.pin

        if not name or category not in CATEGORIES or len(phone) < 10:
            raise HTTPException(400, "Check name, category, and phone (10+ digits)")
        if pin and len(pin) < 4:
            raise HTTPException(400, "PIN must be at least 4 characters")

        try:
            if pin:
                con.execute(
                    """UPDATE technicians SET name=?,phone=?,category=?,area=?,pin_hash=?
                       WHERE id=?""",
                    (name, phone, category, area, hp(pin), tid),
                )
            else:
                con.execute(
                    """UPDATE technicians SET name=?,phone=?,category=?,area=?
                       WHERE id=?""",
                    (name, phone, category, area, tid),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(400, "Phone number already registered")
    return {"ok": True}


@app.post("/api/admin/requests/{rid}/assign")
def admin_assign(rid: int, body: AssignIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        t = con.execute("SELECT * FROM technicians WHERE id=? AND active=1", (body.technician_id,)).fetchone()
        if not r or not t:
            raise HTTPException(404, "Request or technician not found")
        if r["status"] not in ("new", "unassigned", "assigned", "accepted"):
            raise HTTPException(400, "Request can no longer be reassigned")
        assign_to(con, rid, t)
    return {"ok": True}


@app.post("/api/admin/requests/{rid}/auto")
def admin_auto(rid: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        ok = auto_assign(con, rid)
    return {"assigned": ok}


@app.post("/api/admin/requests/{rid}/resolve")
def admin_resolve(rid: int, body: ResolveIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] not in ("disputed", "work_done", "new", "unassigned", "assigned", "accepted"):
            raise HTTPException(400, "Cannot resolve this request")
        if body.action == "release":
            release_payment(con, rid, None, confirmation_source="administrator resolution")
        elif body.action == "cancel":
            con.execute("UPDATE requests SET status='cancelled' WHERE id=?", (rid,))
            con.execute("DELETE FROM technician_locations WHERE request_id=?", (rid,))
            con.execute("UPDATE payments SET status='void' WHERE request_id=?", (rid,))
            log(con, rid, "Closed by admin. No payment released.")
        else:
            raise HTTPException(400, "Unknown action")
    return {"ok": True}


@app.post("/api/admin/payments/{pid}/collected")
def admin_collected(pid: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        con.execute("UPDATE payments SET collected=1 WHERE id=? AND status!='void'", (pid,))
    return {"ok": True}


@app.post("/api/admin/payments/{pid}/payout")
def admin_payout(pid: int, body: PayoutIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p or p["status"] != "released":
            raise HTTPException(400, "Payment is not released by the customer yet")
        if not p["collected"]:
            raise HTTPException(400, "Mark the customer's money as collected first")
        con.execute("UPDATE payments SET status='paid_out', reference=?, paid_at=? WHERE id=?",
                    (body.reference, now(), pid))
        con.execute("UPDATE technicians SET wallet=wallet-? WHERE id=?", (p["payout"], p["technician_id"]))
        log(con, p["request_id"], "Technician payout sent.")
    return {"ok": True}
