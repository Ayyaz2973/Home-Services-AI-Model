"""HomeServe Local
Auto request capture -> AI triage (Ollama) -> auto-assign available technician
-> technician marks work done -> customer confirms -> payment released.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import json
import math
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

import gemini_helper

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE, "homeserve.db"))
PRIVATE_STORAGE = Path(os.getenv("PRIVATE_STORAGE_DIR", os.path.join(BASE, "private_uploads"))).resolve()
ADMIN_KEY = os.getenv("ADMIN_KEY", "Kayani_73")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()

_env_file = os.path.join(BASE, ".env")
if os.path.isfile(_env_file):
    with open(_env_file, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k == "GEMINI_API_KEY" and not GEMINI_API_KEY:
                    GEMINI_API_KEY = _v
                elif _k == "GEMINI_MODEL" and os.getenv("GEMINI_MODEL") is None:
                    GEMINI_MODEL = _v
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
COMMISSION_PCT = float(os.getenv("COMMISSION_PCT", "10"))
ASSIGN_TIMEOUT_MIN = int(os.getenv("ASSIGN_TIMEOUT_MIN", "10"))
ASSIGN_TIMEOUT_EMERGENCY_MIN = int(os.getenv("ASSIGN_TIMEOUT_EMERGENCY_MIN", "3"))
AUTO_CONFIRM_HOURS = 24
CHECK_IN_MAX_ACCURACY_METERS = 150
TERMS_VERSION = "phase1-draft-v1"
PRIVACY_VERSION = "phase1-draft-v1"
DOCUMENT_MAX_BYTES = int(os.getenv("DOCUMENT_MAX_BYTES", str(8 * 1024 * 1024)))
PHOTO_MAX_BYTES = int(os.getenv("PHOTO_MAX_BYTES", str(6 * 1024 * 1024)))
IMAGE_MAX_PIXELS = 20_000_000
IMAGE_MAX_DIMENSION = 2400

# Pilot focus: Jinnah Garden & surrounding Zone 5 towns
JINNAH_GARDEN_KEYWORDS = ["jinnah", "jinnah garden", "jinnah gardens", "fechs", "gagri"]

PILOT_AREAS = {
    "Jinnah Garden": {
        "lat": 33.5682,
        "lng": 73.1553,
        "description": "Hub & Main Pilot Zone (Phase 1, Phase 2, FECHS)",
        "keywords": ["jinnah", "jinnah garden", "jinnah gardens", "fechs", "gagri"],
    },
    "Naval Anchorage": {
        "lat": 33.5425,
        "lng": 73.1670,
        "description": "Naval Anchorage Islamabad",
        "keywords": ["naval", "navel", "anchorage", "naval anchorage"],
    },
    "Soan Gardens": {
        "lat": 33.5785,
        "lng": 73.1385,
        "description": "Soan Gardens Islamabad Expressway",
        "keywords": ["soan", "swan", "soan garden", "soan gardens", "swan garden", "swan gardens"],
    },
    "PWD": {
        "lat": 33.5850,
        "lng": 73.1350,
        "description": "PWD Housing Scheme",
        "keywords": ["pwd", "p.w.d", "pwd society", "pwd road"],
    },
    "Korang Town": {
        "lat": 33.5960,
        "lng": 73.1250,
        "description": "Korang Town Zone 5",
        "keywords": ["korang", "korrang", "korang town", "korrang town"],
    },
    "Pakistan Town": {
        "lat": 33.5880,
        "lng": 73.1280,
        "description": "Pakistan Town (Phase 1 & 2)",
        "keywords": ["pakistan town", "pak town"],
    },
}


def normalize_area(raw_area: Optional[str]) -> str:
    """Normalize raw user-input area to standard pilot area name if matching."""
    if not raw_area:
        return ""
    text = raw_area.strip().lower()
    for area_name, info in PILOT_AREAS.items():
        if any(kw in text for kw in info["keywords"]):
            return area_name
    return raw_area.strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in kilometers between two GPS coordinates."""
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_nearest_pilot_area(lat: float, lng: float) -> Optional[str]:
    """Find closest pilot area within 15 km threshold."""
    closest_area = None
    min_dist = float("inf")
    for name, info in PILOT_AREAS.items():
        dist = haversine_km(lat, lng, info["lat"], info["lng"])
        if dist < min_dist:
            min_dist = dist
            closest_area = name
    return closest_area if min_dist <= 15.0 else None


CATEGORIES = [
    "Electric Work", "Plumbing", "Water Tank Cleaning", "Air Conditioning", "Solar Solutions",
    "Home Renovation", "Home Cleaning", "Pest Control", "Washing Machine", "Fridge Repair",
    "Geyser Repair", "Carpentry", "Painting", "Locksmith", "CCTV Installation",
    "Water Filter / RO", "Welding Work", "Labour", "Other",
]

KEYWORDS = {
    "Electric Work": ["bijli", "electric", "wiring", "switch", "socket", "fan ", "breaker", "short circuit", "ups ", "بجلی"],
    "Plumbing": ["plumb", "nal ", "nalka", "pipe", "leak", "tap ", "drain", "commode", "washroom", "نل", "پانی"],
    "Water Tank Cleaning": ["tanki safai", "water tank", "tank cleaning", "overhead tank", "underground tank", "ٹینکی کی صفائی", "واٹر ٹینک", "sludge", "tank wash", "tanki"],
    "Air Conditioning": [" ac ", "a/c", "air condition", "cooling", "split", "اے سی"],
    "Solar Solutions": ["solar", "panel", "inverter", "سولر", "solar panel", "battery", "inverex", "growatt", "knox", "net metering", "plates", "plate", "solar system"],
    "Home Renovation": ["renovation", "remodel", "false ceiling", "civil work", "tile flooring", "makeover", "modular kitchen", "رینوویشن", "فالس سیلنگ", "bathroom tile"],
    "Home Cleaning": ["clean", "safai", "sofa wash", "صفائی"],
    "Pest Control": ["pest", "keera", "deemak", "termite", "cockroach", "مچھر", "khatmal", "spray", "دیمک", "کیڑے"],
    "Washing Machine": ["washing machine", "dryer", "kapray dhone", "واشنگ مشین", "spinner", "motor", "oven", "microwave", "fridge", "فریج"],
    "Fridge Repair": ["fridge", "refrigerator", "freezer", "فریج"],
    "Geyser Repair": ["geyser", "gyser", "گیزر", "water heater"],
    "Carpentry": ["carpent", "lakri", "door", "wardrobe", "cupboard", "darwaza", "کارپینٹر", "دروازہ", "لاک"],
    "Painting": ["paint", "rang", "colour", "color", "polish", "waterproof", "seepage", "chhat", "تپکن", "پینٹ", "سیم", "چھت"],
    "Locksmith": ["lock", "chabi", "key ", "تالا"],
    "CCTV Installation": ["cctv", "camera", "dvr", "nvr", "کیمرے", "سیکیورٹی", "security camera"],
    "Water Filter / RO": ["water filter", " ro ", "filter", "فلٹر", "پانی کا فلٹر", "dispenser"],
    "Welding Work": ["weld", "grill", "gate", "iron", "ویلڈنگ", "لوہا"],
    "Labour": ["labour", "mazdoor", "shifting", "مزدور"],
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
            latitude REAL, longitude REAL, preferred_slot TEXT DEFAULT 'anytime',
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
        CREATE TABLE IF NOT EXISTS workshops(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workshop_name TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            whatsapp TEXT DEFAULT '',
            category TEXT NOT NULL,
            area TEXT NOT NULL,
            address TEXT NOT NULL,
            technicians_count INTEGER DEFAULT 1,
            services_offered TEXT DEFAULT '',
            equipment_summary TEXT DEFAULT '',
            ntn TEXT DEFAULT '',
            cnic TEXT DEFAULT '',
            shop_front_photo TEXT DEFAULT '',
            workshop_photo TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            rating REAL DEFAULT 5.0,
            jobs_done INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            review_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            reviewed_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_workshops_status ON workshops(status, created_at);
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
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("customer_review", "TEXT"),
            ("warranty_until", "TEXT"),
            ("warranty_claimed_at", "TEXT"),
            ("preferred_slot", "TEXT DEFAULT 'anytime'"),
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
        for col, dtype in (
            ("payout_method", "TEXT DEFAULT 'easypaisa'"),
            ("payout_account", "TEXT DEFAULT ''"),
            ("payout_title", "TEXT DEFAULT ''"),
        ):
            if col not in technician_columns:
                con.execute(f"ALTER TABLE technicians ADD COLUMN {col} {dtype}")

        app_cols = {row["name"] for row in con.execute("PRAGMA table_info(technician_applications)")}
        for col, dtype in (
            ("payout_method", "TEXT DEFAULT 'easypaisa'"),
            ("payout_account", "TEXT DEFAULT ''"),
            ("payout_title", "TEXT DEFAULT ''"),
        ):
            if col not in app_cols:
                con.execute(f"ALTER TABLE technician_applications ADD COLUMN {col} {dtype}")

        PRIVATE_STORAGE.mkdir(parents=True, exist_ok=True)
        (PRIVATE_STORAGE / "documents").mkdir(exist_ok=True)
        (PRIVATE_STORAGE / "job_photos").mkdir(exist_ok=True)
        (PRIVATE_STORAGE / "workshop_photos").mkdir(exist_ok=True)

        if (
            not os.getenv("RAILWAY_ENVIRONMENT")
            and con.execute("SELECT COUNT(*) FROM workshops").fetchone()[0] == 0
        ):
            demo_workshops = [
                ("Al-Madina AC & Refrigeration Workshop", "Muhammad Irfan", "03009990001", "03009990001", "Air Conditioning", "Jinnah Garden", "Shop 4, Commercial Market, Phase 1, Jinnah Garden", 5, "Inverter AC bench testing, PCB repair, chemical jet washing, gas charging", "R32/R410 digital manifold gauges, Robinair vacuum pumps, high-pressure jet washers", "approved", 4.9, 142),
                ("Pak Solar Energy Solutions & Workshop", "Engr. Farhan Kayani", "03009990002", "03009990002", "Solar Solutions", "PWD", "Plot 18-B, Main Boulevard, PWD Housing Scheme", 8, "Complete 3kW-50kW On-Grid & Hybrid Solar installation, inverter earthing, Net Metering prep", "Hydraulic terminal crimpers, solar irradiance meters, thermal imaging camera, insulation tester", "approved", 5.0, 98),
                ("Rajput Motor Rewinding & Electric Center", "Tariq Mehmood Rajput", "03009990003", "03009990003", "Electric Work", "Soan Gardens", "Shop 12, Block C Commercial, Soan Gardens, Expressway", 4, "Submersible water pump rewinding, generator AVR repair, heavy breaker installation", "Motor winding lathe, automatic coil winder, megohmmeter, load test bench", "approved", 4.8, 86),
            ]
            for wname, oname, ph, wa, cat, ar, addr, cnt, srv, eq, st, rat, jd in demo_workshops:
                con.execute(
                    """INSERT INTO workshops(
                         workshop_name, owner_name, phone, whatsapp, category, area, address,
                         technicians_count, services_offered, equipment_summary, status, rating, jobs_done, created_at, reviewed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (wname, oname, ph, wa, cat, ar, addr, cnt, srv, eq, st, rat, jd, now(), now()),
                )
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
        if not os.getenv("RAILWAY_ENVIRONMENT"):
            pilot_seed = [
                ("Zubair Electrician", "03001110007", "Electric Work", "Jinnah Garden"),
                ("Rashid Plumber", "03001110008", "Plumbing", "PWD"),
                ("Naveed AC Specialist", "03001110009", "Air Conditioning", "Soan Gardens"),
                ("Sajid Handyman", "03001110010", "Home Cleaning", "Naval Anchorage"),
                ("Asif Carpenter", "03001110011", "Carpentry", "Pakistan Town"),
                ("Faisal Technician", "03001110012", "Electric Work", "Korang Town"),
            ]
            for n, p, c, a in pilot_seed:
                exists = con.execute("SELECT 1 FROM technicians WHERE phone=?", (p,)).fetchone()
                if not exists:
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


def ai_analyze_gemini(text: str) -> Optional[dict]:
    """Tier 1 AI triage using Google Gemini Free API."""
    if not GEMINI_API_KEY:
        return None
    return gemini_helper.gemini_triage(
        text=text,
        categories=CATEGORIES,
        api_key=GEMINI_API_KEY,
        model=GEMINI_MODEL or "gemini-2.5-flash",
    )


def ai_analyze(text: str) -> dict:
    """Multi-tier AI Triage:
    Tier 1: Google Gemini Free API (ultra-fast, accurate with Urdu/Roman Urdu)
    Tier 2: Local Ollama (if configured & running)
    Tier 3: Heuristic keyword guess (offline fallback)
    """
    # Tier 1: Gemini Free
    gemini_res = ai_analyze_gemini(text)
    if gemini_res:
        return gemini_res

    fallback = keyword_guess(text)

    # Tier 2: Ollama (if running)
    if OLLAMA_URL and not os.getenv("DISABLE_OLLAMA"):
        try:
            prompt = f"""You are a dispatcher for a home services company in Islamabad, Pakistan.
The customer message may be in Urdu, Roman Urdu or English.
Pick exactly ONE category from this list: {', '.join(CATEGORIES)}.
Pick urgency: "normal", "urgent" (needs same day) or "emergency" (danger: sparks, flooding, gas, shock).
Write a one-line English summary for the technician.
Reply with JSON only: {{"category": "...", "urgency": "...", "summary": "..."}}

Customer message: {text}"""
            r = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1},
                },
                timeout=2.0,
            )
            data = json.loads(r.json()["response"])
            cat = data.get("category")
            urg = data.get("urgency")
            return {
                "category": cat if cat in CATEGORIES else fallback["category"],
                "urgency": urg if urg in ("normal", "urgent", "emergency") else fallback["urgency"],
                "summary": (data.get("summary") or fallback["summary"])[:200],
                "engine": "ollama",
            }
        except Exception as e:
            print("Ollama AI fallback:", e)

    # Tier 3: Heuristic keywords
    res = dict(fallback)
    res["engine"] = "keywords"
    return res


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
    req_area = normalize_area(req["area"] or "").strip().lower()
    pilot_area_keys = [k.lower() for k in PILOT_AREAS]

    def rank_key(t):
        t_area = normalize_area(t["area"] or "").strip().lower()
        if req_area and t_area == req_area:
            area_score = 0
        elif req_area in pilot_area_keys and t_area in pilot_area_keys:
            area_score = 1
        elif not req_area:
            area_score = 1
        else:
            area_score = 2
        return (area_score, -t["rating"], t["jobs_done"])

    rows = sorted(rows, key=rank_key)
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
    emergency_cutoff = (datetime.now() - timedelta(minutes=ASSIGN_TIMEOUT_EMERGENCY_MIN)).isoformat(timespec="seconds")
    normal_cutoff = (datetime.now() - timedelta(minutes=ASSIGN_TIMEOUT_MIN)).isoformat(timespec="seconds")
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
    stale = con.execute(
        """SELECT * FROM requests
           WHERE status='assigned'
             AND (
               (urgency='emergency' AND assigned_at < ?)
               OR (urgency!='emergency' AND assigned_at < ?)
             )""",
        (emergency_cutoff, normal_cutoff),
    ).fetchall()
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
def release_payment(con, rid, rating=None, confirmation_source="customer", review_text=None):
    pay = con.execute("SELECT * FROM payments WHERE request_id=?", (rid,)).fetchone()
    if not pay or pay["status"] not in ("awaiting_confirmation", "disputed"):
        raise HTTPException(400, "Payment cannot be released in its current state")
    warranty_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    con.execute("UPDATE payments SET status='released', released_at=? WHERE id=?", (now(), pay["id"]))
    con.execute(
        "UPDATE requests SET status='confirmed', confirmed_at=?, warranty_until=?, customer_review=? WHERE id=?",
        (now(), warranty_date, review_text or "", rid),
    )
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
        message = f"Customer confirmed with {rating or 5}-star rating. Rs {pay['payout']:.0f} released. 7-day warranty active until {warranty_date}."
    else:
        message = f"Automatically confirmed ({confirmation_source}). Rs {pay['payout']:.0f} released. 7-day warranty active until {warranty_date}."
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


@app.get("/workshop")
def page_workshop():
    return FileResponse(os.path.join(BASE, "static", "workshop.html"))


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


PRICING_CATALOG = {
    "Electric Work": [
        {"item": "Switch / Socket Replacement or Repair", "price_range": "Rs. 400 - 700", "market_price": "Rs. 1,000", "savings": "Save Rs. 300+", "duration": "30 mins", "popular": True},
        {"item": "Ceiling Fan Installation & Balancing", "price_range": "Rs. 1,000 - 1,500", "market_price": "Rs. 2,200", "savings": "Save Rs. 700+", "duration": "45 mins", "popular": True},
        {"item": "Distribution Box / Breaker Repair", "price_range": "Rs. 1,500 - 2,500", "market_price": "Rs. 3,500", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
        {"item": "Complete Inverter / UPS Wiring Check", "price_range": "Rs. 2,500 - 4,500", "market_price": "Rs. 5,500", "savings": "Save Rs. 1,000+", "duration": "2 hours", "popular": False},
    ],
    "Plumbing": [
        {"item": "Tap / Mixer Replacement or Washer Fix", "price_range": "Rs. 600 - 1,000", "market_price": "Rs. 1,500", "savings": "Save Rs. 500+", "duration": "30 mins", "popular": True},
        {"item": "Muslim Shower / Commode Tank Flush Fix", "price_range": "Rs. 800 - 1,400", "market_price": "Rs. 2,000", "savings": "Save Rs. 600+", "duration": "45 mins", "popular": True},
        {"item": "Water Tank Cleaning & High Pressure Wash", "price_range": "Rs. 2,500 - 4,500", "market_price": "Rs. 5,500", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": True},
        {"item": "Water Motor / Booster Pump Installation", "price_range": "Rs. 2,000 - 3,500", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": False},
    ],
    "Air Conditioning": [
        {"item": "Split AC General Filter & Tray Service", "price_range": "Rs. 2,000 - 2,800", "market_price": "Rs. 3,500", "savings": "Save Rs. 700+", "duration": "45 mins", "popular": True},
        {"item": "Master Deep Chemical Jet Wash (Indoor + Outdoor)", "price_range": "Rs. 3,500 - 4,500", "market_price": "Rs. 6,000", "savings": "Save Rs. 1,500+", "duration": "75 mins", "popular": True},
        {"item": "Refrigerant Gas Top-up / Leak Test (R410/R32/R22)", "price_range": "Rs. 4,500 - 7,500", "market_price": "Rs. 9,000", "savings": "Save Rs. 1,500+", "duration": "60 mins", "popular": True},
        {"item": "AC Dismounting & Re-installation", "price_range": "Rs. 4,000 - 6,000", "market_price": "Rs. 7,500", "savings": "Save Rs. 1,500+", "duration": "2 hours", "popular": False},
    ],
    "Geyser Repair": [
        {"item": "Instant Gas Geyser Burner & Safety Valve Tune-up", "price_range": "Rs. 1,500 - 2,200", "market_price": "Rs. 3,000", "savings": "Save Rs. 800+", "duration": "45 mins", "popular": True},
        {"item": "Electric Geyser Thermostat & Heating Element Replace", "price_range": "Rs. 1,800 - 3,000", "market_price": "Rs. 4,000", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
        {"item": "Geyser Descaling & Sediment Tank Flush", "price_range": "Rs. 2,500 - 3,500", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": False},
    ],
    "Carpentry": [
        {"item": "Door Lock / Handle Fitting & Alignment", "price_range": "Rs. 800 - 1,500", "market_price": "Rs. 2,200", "savings": "Save Rs. 700+", "duration": "45 mins", "popular": True},
        {"item": "Kitchen Cabinet Hinge & Drawer Channel Repair", "price_range": "Rs. 1,200 - 2,200", "market_price": "Rs. 3,000", "savings": "Save Rs. 800+", "duration": "60 mins", "popular": True},
        {"item": "Bed / Wardrobe Assembly & Repair", "price_range": "Rs. 2,000 - 3,500", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": False},
    ],
    "Home Cleaning": [
        {"item": "Deep Sofa & Cushion Wash (5 Seater)", "price_range": "Rs. 2,500 - 4,000", "market_price": "Rs. 5,000", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": True},
        {"item": "Complete Bathroom Deep Disinfection", "price_range": "Rs. 1,800 - 3,000", "market_price": "Rs. 4,000", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
    ],
    "Solar Solutions": [
        {"item": "Complete Solar System Installation (3kW - 10kW On-Grid/Hybrid)", "price_range": "Rs. 25,000 - 65,000", "market_price": "Rs. 80,000", "savings": "Save Rs. 15,000+", "duration": "1-2 days", "popular": True},
        {"item": "Solar Inverter Setup, Programming & Earthing Check", "price_range": "Rs. 3,500 - 6,000", "market_price": "Rs. 8,000", "savings": "Save Rs. 2,000+", "duration": "2 hours", "popular": True},
        {"item": "Solar Panel Chemical Jet Wash & Dust Descaling (Up to 16 Panels)", "price_range": "Rs. 2,000 - 3,500", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
        {"item": "Tubular Battery Acid Gravity Balancing & Terminal Cleanup", "price_range": "Rs. 1,500 - 2,500", "market_price": "Rs. 3,500", "savings": "Save Rs. 1,000+", "duration": "45 mins", "popular": False},
        {"item": "Net Metering Green Meter Documentation & Pre-Inspection", "price_range": "Rs. 5,000 - 9,000", "market_price": "Rs. 15,000", "savings": "Save Rs. 6,000+", "duration": "3 hours", "popular": False},
    ],
    "CCTV Installation": [
        {"item": "HD / IP Camera Installation & Angle Tuning (Per Camera)", "price_range": "Rs. 800 - 1,400", "market_price": "Rs. 1,800", "savings": "Save Rs. 400+", "duration": "45 mins", "popular": True},
        {"item": "4/8-Channel DVR/NVR Mobile Remote Viewing Setup", "price_range": "Rs. 1,500 - 2,500", "market_price": "Rs. 3,500", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
        {"item": "Smart Video Doorbell / Intercom Fitting", "price_range": "Rs. 1,800 - 3,000", "market_price": "Rs. 4,000", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": False},
    ],
    "Water Filter / RO": [
        {"item": "3-Stage Water Filter 3-Candle Replacement & Flush", "price_range": "Rs. 800 - 1,500", "market_price": "Rs. 2,000", "savings": "Save Rs. 500+", "duration": "30 mins", "popular": True},
        {"item": "Domestic RO Plant Membrane Check & TDS Water Testing", "price_range": "Rs. 1,800 - 3,000", "market_price": "Rs. 4,000", "savings": "Save Rs. 1,000+", "duration": "60 mins", "popular": True},
        {"item": "Electric Water Cooler / Dispenser Servicing & Sanitization", "price_range": "Rs. 1,500 - 2,500", "market_price": "Rs. 3,500", "savings": "Save Rs. 1,000+", "duration": "45 mins", "popular": False},
    ],
    "Painting": [
        {"item": "Roof Seepage & Leakage Treatment (Chemical Bitumen Coating)", "price_range": "Rs. 4,500 - 9,500", "market_price": "Rs. 14,000", "savings": "Save Rs. 4,500+", "duration": "4 hours", "popular": True},
        {"item": "Single Room Paint & Putty Touch-up (Distemper/Emulsion)", "price_range": "Rs. 4,000 - 7,000", "market_price": "Rs. 10,000", "savings": "Save Rs. 3,000+", "duration": "1 day", "popular": True},
        {"item": "Exterior Weather Sheet Coating & Crack Sealing", "price_range": "Rs. 6,000 - 12,000", "market_price": "Rs. 18,000", "savings": "Save Rs. 6,000+", "duration": "1 day", "popular": False},
    ],
    "Washing Machine": [
        {"item": "Automatic Washing Machine Gear Box / Belt / Motor Repair", "price_range": "Rs. 1,500 - 3,000", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,500+", "duration": "60 mins", "popular": True},
        {"item": "Microwave Oven Magnetron / Heating Coil Replacement", "price_range": "Rs. 1,500 - 2,500", "market_price": "Rs. 3,500", "savings": "Save Rs. 1,000+", "duration": "45 mins", "popular": True},
        {"item": "Refrigerator Gas Refilling & Thermostat Replacement", "price_range": "Rs. 3,500 - 5,500", "market_price": "Rs. 7,500", "savings": "Save Rs. 2,000+", "duration": "75 mins", "popular": False},
    ],
    "Pest Control": [
        {"item": "Complete Home Anti-Termite (Deemak) Chemical Spray", "price_range": "Rs. 4,000 - 8,000", "market_price": "Rs. 12,000", "savings": "Save Rs. 4,000+", "duration": "2 hours", "popular": True},
        {"item": "Cockroach & Bed Bug (Khatmal) Targeted Gel Treatment", "price_range": "Rs. 2,500 - 4,500", "market_price": "Rs. 6,000", "savings": "Save Rs. 1,500+", "duration": "60 mins", "popular": True},
    ],
    "Water Tank Cleaning": [
        {"item": "Overhead Water Tank Chemical Deep Scrub & Wash (Up to 1,000 Gal)", "price_range": "Rs. 2,500 - 3,500", "market_price": "Rs. 4,500", "savings": "Save Rs. 1,000+", "duration": "90 mins", "popular": True},
        {"item": "Underground Concrete Tank Heavy Sludge Extraction & Disinfection", "price_range": "Rs. 4,500 - 6,500", "market_price": "Rs. 9,000", "savings": "Save Rs. 2,500+", "duration": "3 hours", "popular": True},
        {"item": "Twin Pack: Overhead + Underground Tank Dual Sanitization", "price_range": "Rs. 6,500 - 8,500", "market_price": "Rs. 12,000", "savings": "Save Rs. 3,500+", "duration": "4 hours", "popular": True},
    ],
    "Home Renovation": [
        {"item": "Turnkey Renovation & Interior Architecture (Free On-Site Survey)", "price_range": "Rs. 0 (Free Estimate)", "market_price": "Rs. 5,000", "savings": "Free Visit", "duration": "Survey 60 mins", "popular": True},
        {"item": "Gypsum / POP False Ceiling Installation with LED Grooves (Per Sq Ft)", "price_range": "Rs. 130 - 180 / sqft", "market_price": "Rs. 230 / sqft", "savings": "Save 25%", "duration": "Custom", "popular": True},
        {"item": "Modular Kitchen Cabinets & Countertop Fitting", "price_range": "Rs. 25,000 - 75,000", "market_price": "Rs. 95,000", "savings": "Save Rs. 20,000+", "duration": "3-5 days", "popular": False},
        {"item": "Bathroom Tile & Sanitary Complete Modernization", "price_range": "Rs. 20,000 - 45,000", "market_price": "Rs. 60,000", "savings": "Save Rs. 15,000+", "duration": "2-3 days", "popular": True},
    ],
}

SEASONAL_PACKAGES = [
    {
        "id": "solar-panel-deep-clean",
        "season": "Year-Round / Post-Dust Season",
        "badge": "Efficiency Booster · Zone 5",
        "title": "Solar Panel De-Dusting & Generation Restore Package",
        "price_pkr": 2500,
        "savings": "Save 20%",
        "checklist": [
            "De-ionized high-pressure jet wash for up to 16 solar plates",
            "Bird droppings, soot, and industrial grime chemical soft-sponge scrub",
            "Inverter DC input ampere & voltage verification before & after wash",
            "MC4 connector water-tight seal and earthing inspection"
        ],
        "category": "Solar Solutions",
        "default_desc": "Solar Panel jet wash and generation restore tune-up"
    },
    {
        "id": "pre-winter-geyser",
        "season": "Pre-Winter (October - November)",
        "badge": "Active Season · Islamabad & Zone 5",
        "title": "Winter Heating & Geyser Safety Package",
        "price_pkr": 2500,
        "savings": "Save 25%",
        "checklist": [
            "Instant gas geyser burner cleaning & pilot flame tune-up",
            "Electric water heater thermostat calibration & element safety check",
            "Gas leak & pipe seal pressure test (prevent winter gas accidents)",
            "Water supply valve & drain inspection"
        ],
        "category": "Geyser Repair",
        "default_desc": "Winter Heating & Geyser Safety Package (Pre-winter tune-up for Islamabad)"
    },
    {
        "id": "winter-solar-ups",
        "season": "Pre-Winter (October - November)",
        "badge": "Power Backup Prep",
        "title": "Solar & UPS Winter Battery Readiness",
        "price_pkr": 2200,
        "savings": "Save 20%",
        "checklist": [
            "Tubular battery specific gravity hydrometer testing",
            "Terminal acid descaling & distilled water top-up",
            "Inverter low-sunlight charge controller optimization",
            "Distribution board emergency load segregation"
        ],
        "category": "Solar Solutions",
        "default_desc": "Winter Solar & UPS battery readiness inspection and tuning"
    },
    {
        "id": "pre-summer-ac",
        "season": "Pre-Summer (March - May)",
        "badge": "Upcoming Season",
        "title": "AC Master Pre-Summer Chill Tune-Up",
        "price_pkr": 3800,
        "savings": "Save 30%",
        "checklist": [
            "High-pressure chemical jet wash for indoor & outdoor coils",
            "Ampere load & starting capacitor health verification",
            "Refrigerant standing & running pressure gauge check",
            "Anti-fungal blower sanitization"
        ],
        "category": "Air Conditioning",
        "default_desc": "Pre-Summer AC Master chemical wash and gas level tune-up"
    },
    {
        "id": "monsoon-waterproofing",
        "season": "Monsoon (July - August)",
        "badge": "Rain Protection",
        "title": "Monsoon Roof & Drain Waterproofing Audit",
        "price_pkr": 3000,
        "savings": "Save 20%",
        "checklist": [
            "Parapet & terrace rain drain blockage clearing",
            "Concealed joint leak inspection using moisture meter",
            "Main electrical panel waterproofing check against rain shorts"
        ],
        "category": "Plumbing",
        "default_desc": "Monsoon roof drainage inspection and electrical moisture protection"
    }
]

WORK_SHOWCASE = [
    {
        "id": "proj-1",
        "title": "Main Distribution Board Rewiring & Surge Protection",
        "area": "Jinnah Garden, Phase 1",
        "category": "Electric Work",
        "technician": "Zubair Electrician",
        "badge": "HomeServe Verified Pro",
        "before_img": "/static/images/db-wiring-before.svg",
        "after_img": "/static/images/db-wiring-after.svg",
        "before_alt": "Before: Tangled burnt distribution board wiring in Jinnah Garden",
        "after_alt": "HomeServe Fix: Clean Schneider breaker installation with color-coded trunking",
        "before_desc": "Hazardous tangled wiring, burnt main breaker, frequent tripping across Phase 1 house.",
        "after_desc": "Complete Schneider breaker installation, color-coded neutral trunking, and earthing verified.",
        "rating": 5.0,
        "review": "Bohot saaf kaam kiya Zubair bhai ne. Breaker bar bar trip hone ka masla mukammal hal ho gaya.",
    },
    {
        "id": "proj-2",
        "title": "Concealed PPR Bathroom Pipe Replacement",
        "area": "Soan Gardens, Block B",
        "category": "Plumbing",
        "technician": "Rashid Plumber",
        "badge": "HomeServe Verified Pro",
        "before_img": "/static/images/ppr-pipe-before.svg",
        "after_img": "/static/images/ppr-pipe-after.svg",
        "before_alt": "Before: Rusted leaking GI pipe causing dampness in Soan Gardens",
        "after_alt": "HomeServe Fix: Turkish PPR pipe heat-welded and 10-bar pressure tested",
        "before_desc": "Internal GI pipe rusted and leaking behind tiles causing dampness in bedroom wall.",
        "after_desc": "New Turkish PPR pipe line heat-welded and pressure-tested up to 10 bar without tile damage.",
        "rating": 4.9,
        "review": "Wall dampness stopped within 2 days. Very skilled and respectful plumber.",
    },
    {
        "id": "proj-3",
        "title": "Inverter AC Deep Jet-Wash & Gas Refill",
        "area": "Naval Anchorage, Sector B",
        "category": "Air Conditioning",
        "technician": "Naveed AC Specialist",
        "badge": "HomeServe Verified Pro",
        "before_desc": "AC blowing warm air, outdoor condenser heavily choked with dust, high electricity bill.",
        "after_desc": "Chemical foam coil wash, condenser fan oiling, topped up R410 refrigerant. Chilling at 16°C.",
        "rating": 5.0,
        "review": "Chilling perfectly now like brand new. Punctual technician arrived right on time.",
    },
    {
        "id": "proj-4",
        "title": "Hydraulic Hinge & Master Lock Installation",
        "area": "PWD Housing Scheme",
        "category": "Carpentry",
        "technician": "Asif Carpenter",
        "badge": "HomeServe Verified Pro",
        "before_desc": "Sagging wooden main door scratching the floor, jammed deadbolt lock.",
        "after_desc": "Planed door clearance, installed heavy-duty brass hinges and high-security 6-pin deadbolt.",
        "rating": 4.8,
        "review": "Smooth opening now, lock is rock solid. Great service in PWD.",
    }
]


@app.get("/api/pricing-catalog")
def pricing_catalog():
    return {"catalog": PRICING_CATALOG}


@app.get("/api/seasonal-packages")
def seasonal_packages():
    return {"packages": SEASONAL_PACKAGES}


@app.get("/api/work-showcase")
def work_showcase():
    return {"showcase": WORK_SHOWCASE}


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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    preferred_slot: Optional[str] = "anytime"


class DoneIn(BaseModel):
    amount: float = Field(gt=0, allow_inf_nan=False)
    note: str = ""


class ConfirmIn(BaseModel):
    rating: int = 5
    review_text: Optional[str] = None


class WarrantyClaimIn(BaseModel):
    issue_note: str = Field(..., min_length=5, max_length=500)


class DepositIn(BaseModel):
    amount: float = Field(..., gt=0)
    note: str = Field(default="Wallet top-up / EasyPaisa deposit", max_length=200)


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


class PayoutAccountIn(BaseModel):
    payout_method: str = Field(default="easypaisa", max_length=30)
    payout_account: str = Field(..., min_length=4, max_length=50)
    payout_title: str = Field(..., min_length=2, max_length=100)


class AvailIn(BaseModel):
    available: bool


class ResolveIn(BaseModel):
    action: str  # release | cancel


class ApplicationReviewIn(BaseModel):
    action: str
    review_note: str = ""


class WorkshopReviewIn(BaseModel):
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
                p_method = application["payout_method"] if "payout_method" in application.keys() and application["payout_method"] else "easypaisa"
                p_account = application["payout_account"] if "payout_account" in application.keys() and application["payout_account"] else application["phone"]
                p_title = application["payout_title"] if "payout_title" in application.keys() and application["payout_title"] else application["name"]
                con.execute(
                    """INSERT INTO technicians(
                         name,phone,pin_hash,category,area,available,active,created_at,application_id,
                         payout_method,payout_account,payout_title)
                       VALUES(?,?,?,?,?,0,1,?,?,?,?,?)""",
                    (application["name"], application["phone"], application["pin_hash"],
                     application["category"], application["area"], now(), application_id,
                     p_method, p_account, p_title),
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


# ---- workshops / service centers partner portal
@app.post("/api/workshops/register")
async def register_workshop(
    workshop_name: str = Form(...),
    owner_name: str = Form(...),
    phone: str = Form(...),
    whatsapp: str = Form(""),
    category: str = Form(...),
    area: str = Form(...),
    address: str = Form(...),
    technicians_count: int = Form(1),
    services_offered: str = Form(""),
    equipment_summary: str = Form(""),
    ntn: str = Form(""),
    cnic: str = Form(""),
    terms_accepted: bool = Form(False),
    shop_front_photo: Optional[UploadFile] = File(None),
    workshop_photo: Optional[UploadFile] = File(None),
):
    normalized_phone = norm_phone(phone)
    if not workshop_name.strip() or not owner_name.strip() or len(normalized_phone) < 10:
        raise HTTPException(400, "Enter workshop name, owner name, and a valid phone number")
    if category not in CATEGORIES and category != "Multi-Trade Center":
        raise HTTPException(400, "Choose a valid service trade category")
    if len(address.strip()) < 5 or len(area.strip()) < 2:
        raise HTTPException(400, "Enter your workshop address and area")
    if not terms_accepted:
        raise HTTPException(400, "Accept the workshop partner terms to submit registration")

    front_stored, interior_stored = "", ""
    stored_paths = []
    try:
        if shop_front_photo and shop_front_photo.filename:
            front_stored, _ = await _store_upload(shop_front_photo, "workshop_photos", PHOTO_MAX_BYTES)
            stored_paths.append(front_stored)
        if workshop_photo and workshop_photo.filename:
            interior_stored, _ = await _store_upload(workshop_photo, "workshop_photos", PHOTO_MAX_BYTES)
            stored_paths.append(interior_stored)

        with db() as con:
            existing = con.execute("SELECT 1 FROM workshops WHERE phone=?", (normalized_phone,)).fetchone()
            if existing:
                raise HTTPException(409, "A workshop with this phone number is already registered")

            cur = con.execute(
                """INSERT INTO workshops(
                     workshop_name, owner_name, phone, whatsapp, category, area, address,
                     technicians_count, services_offered, equipment_summary, ntn, cnic,
                     shop_front_photo, workshop_photo, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?)""",
                (
                    workshop_name.strip(), owner_name.strip(), normalized_phone,
                    norm_phone(whatsapp) if whatsapp else normalized_phone,
                    category, area.strip(), address.strip(), max(1, technicians_count),
                    services_offered.strip(), equipment_summary.strip(),
                    ntn.strip(), cnic.strip(), front_stored, interior_stored, now(),
                ),
            )
            wid = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        for p in stored_paths:
            (PRIVATE_STORAGE / p).unlink(missing_ok=True)
        raise HTTPException(409, "A workshop with this phone number is already registered") from exc
    except Exception:
        for p in stored_paths:
            (PRIVATE_STORAGE / p).unlink(missing_ok=True)
        raise

    return {
        "ok": True,
        "workshop_id": wid,
        "status": "pending",
        "message": "Workshop registration submitted successfully. Admin verification in progress.",
    }


@app.get("/api/workshops/status/{phone}")
def get_workshop_status(phone: str):
    normalized = norm_phone(phone)
    with db() as con:
        ws = con.execute(
            """SELECT id, workshop_name, owner_name, category, area, address, technicians_count,
                      status, review_note, created_at, reviewed_at
               FROM workshops WHERE phone=?""",
            (normalized,),
        ).fetchone()
    if not ws:
        raise HTTPException(404, "No workshop registration found for this phone number")
    return dict(ws)


@app.get("/api/workshops")
def public_workshops(category: Optional[str] = None, area: Optional[str] = None):
    with db() as con:
        query = """SELECT id, workshop_name, category, area, address, technicians_count,
                          services_offered, equipment_summary, rating, jobs_done
                   FROM workshops WHERE status='approved' AND active=1"""
        params = []
        if category:
            query += " AND category=?"
            params.append(category)
        if area:
            query += " AND area LIKE ?"
            params.append(f"%{area}%")
        query += " ORDER BY rating DESC, jobs_done DESC"
        rows = con.execute(query, params).fetchall()
        return {"workshops": [dict(r) for r in rows]}


@app.get("/api/admin/workshops")
def admin_workshops(x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        rows = con.execute(
            """SELECT * FROM workshops ORDER BY
                 CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC"""
        ).fetchall()
        return {"workshops": [dict(r) for r in rows]}


@app.post("/api/admin/workshops/{wid}/review")
def admin_review_workshop(
    wid: int, body: WorkshopReviewIn, x_admin_key: str = Header(None)
):
    admin_auth(x_admin_key)
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "Choose approve or reject")
    with db() as con:
        ws = con.execute("SELECT * FROM workshops WHERE id=?", (wid,)).fetchone()
        if not ws:
            raise HTTPException(404, "Workshop not found")
        status = "approved" if body.action == "approve" else "rejected"
        con.execute(
            "UPDATE workshops SET status=?, review_note=?, reviewed_at=? WHERE id=?",
            (status, body.review_note.strip(), now(), wid),
        )
    return {"ok": True, "workshop_id": wid, "status": status}


@app.get("/api/admin/workshops/{wid}/photos/{photo_type}")
def admin_workshop_photo(wid: int, photo_type: str, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    if photo_type not in ("front", "interior"):
        raise HTTPException(400, "Photo type must be front or interior")
    col = "shop_front_photo" if photo_type == "front" else "workshop_photo"
    with db() as con:
        ws = con.execute(f"SELECT {col} FROM workshops WHERE id=?", (wid,)).fetchone()
    if not ws or not ws[col]:
        raise HTTPException(404, "Workshop photo not found")
    return _private_file_response(ws[col], "image/jpeg")


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
        norm_area = normalize_area(body.area)
        if not norm_area and body.latitude is not None and body.longitude is not None:
            detected = find_nearest_pilot_area(body.latitude, body.longitude)
            if detected:
                norm_area = detected

        slot = (body.preferred_slot or "anytime").strip().lower()
        if slot not in ("morning", "afternoon", "evening", "emergency", "anytime"):
            slot = "anytime"
        urgency = "emergency" if slot == "emergency" else ai["urgency"]

        cur = con.execute(
            """INSERT INTO requests(token,customer_name,customer_phone,address,area,description,
               category,urgency,ai_summary,status,service_id,quoted_price,visit_fee,
               customer_terms_version,customer_terms_accepted_at,
               customer_privacy_version,customer_privacy_accepted_at,
               latitude,longitude,preferred_slot,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token, body.name.strip(), norm_phone(body.phone), body.address.strip(), norm_area,
             body.description.strip(), category, urgency, ai["summary"], "new",
             catalog_item["id"] if catalog_item else None,
             catalog_item["price"] if catalog_item else None,
             catalog_item["visit_fee"] if catalog_item else None,
             TERMS_VERSION, now(), PRIVACY_VERSION, now(),
             body.latitude, body.longitude, slot, now()),
        )
        rid = cur.lastrowid
        slot_label = f" [Preferred Slot: {slot.capitalize()}]" if slot != "anytime" else ""
        log(con, rid, f"Request received. Service: {category}. Urgency: {urgency}.{slot_label}")
        if category == "Other":
            con.execute("UPDATE requests SET status='unassigned' WHERE id=?", (rid,))
            log(con, rid, "Needs manual assignment by admin.")
        else:
            auto_assign(con, rid)
        status = con.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()[0]
    return {
        "id": rid, "token": token, "status": status, "category": category,
        "preferred_slot": slot,
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
        t = con.execute("SELECT name, phone, rating, jobs_done, area FROM technicians WHERE id=?", (r["technician_id"],)).fetchone()
        ev = con.execute("SELECT text, at FROM events WHERE request_id=? ORDER BY id", (r["id"],)).fetchall()
        d = dict(r)
        if r["status"] not in ("accepted", "work_done", "confirmed", "disputed"):
            d["address"] = (r["area"] or "Area only") + " (full address shared after technician accepts)"
        d.pop("token")
        d["tech_name"] = t["name"] if t else None
        d["tech_phone"] = t["phone"] if (t and r["status"] in ("accepted", "work_done", "confirmed", "disputed")) else None
        d["tech_rating"] = float(t["rating"]) if t and t["rating"] else 4.9
        d["tech_jobs_done"] = int(t["jobs_done"]) if t and t["jobs_done"] else 12
        d["tech_area"] = t["area"] if t else (r["area"] or "Islamabad")
        d["tech_is_verified"] = True
        d["tech_badge"] = "HomeServe Verified Pro"
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
        release_payment(con, r["id"], max(1, min(5, body.rating)), review_text=body.review_text)
    return {"ok": True}


@app.post("/api/track/{token}/claim-warranty")
def customer_claim_warranty(token: str, body: WarrantyClaimIn):
    with db() as con:
        r = req_by_token(con, token)
        if r["status"] != "confirmed":
            raise HTTPException(400, "Only confirmed completed jobs are covered by warranty")
        if not r["warranty_until"]:
            raise HTTPException(400, "No warranty period found for this job")
        today = datetime.now().strftime("%Y-%m-%d")
        if today > r["warranty_until"]:
            raise HTTPException(400, f"The 7-day warranty expired on {r['warranty_until']}")
        if r["warranty_claimed_at"]:
            raise HTTPException(400, "Warranty revisit claim has already been submitted for this job")
        claim_time = now()
        con.execute("UPDATE requests SET warranty_claimed_at=? WHERE id=?", (claim_time, r["id"]))
        log(con, r["id"], f"Customer filed a 7-Day Warranty Revisit claim: {body.issue_note.strip()}")
    return {
        "ok": True,
        "message": "Warranty revisit claim recorded. Our supervisor will contact you within 2 hours.",
        "claimed_at": claim_time,
    }


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
        me = {
            k: (t[k] if k in t.keys() else "")
            for k in (
                "id", "name", "phone", "category", "area", "available", "rating",
                "jobs_done", "wallet", "payout_method", "payout_account", "payout_title"
            )
        }
        return {"me": me, "jobs": jobs}


@app.post("/api/tech/payout-account")
def tech_update_payout_account(
    body: PayoutAccountIn, x_phone: str = Header(None), x_pin: str = Header(None)
):
    with db() as con:
        t = tech_auth(con, x_phone, x_pin)
        con.execute(
            """UPDATE technicians
               SET payout_method=?, payout_account=?, payout_title=?
               WHERE id=?""",
            (body.payout_method.lower().strip(), body.payout_account.strip(), body.payout_title.strip(), t["id"]),
        )
    return {"ok": True, "message": "Payout account updated successfully"}


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
            """SELECT p.*, t.name AS tech_name, t.phone AS tech_phone,
                      COALESCE(t.payout_method, 'easypaisa') AS tech_payout_method,
                      COALESCE(t.payout_account, t.phone) AS tech_payout_account,
                      COALESCE(t.payout_title, t.name) AS tech_payout_title
               FROM payments p
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


@app.post("/api/admin/technicians/{tid}/deposit")
def admin_tech_deposit(tid: int, body: DepositIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        tech = con.execute("SELECT * FROM technicians WHERE id=?", (tid,)).fetchone()
        if not tech:
            raise HTTPException(404, "Technician not found")
        deposit_paisa = int(round(body.amount * 100))
        con.execute(
            """INSERT INTO commission_ledger(request_id, technician_id, gross_amount, commission_amount,
               net_payout, commission_pct, status, note, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (0, tid, deposit_paisa, 0, deposit_paisa, 0, "deposit", body.note.strip(), now()),
        )
        con.execute("UPDATE technicians SET wallet=wallet+? WHERE id=?", (body.amount, tid))
        updated_wallet = con.execute("SELECT wallet FROM technicians WHERE id=?", (tid,)).fetchone()[0]
    return {"ok": True, "technician_id": tid, "amount": body.amount, "new_wallet": updated_wallet}


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


# ---- pilot areas and AI diagnostic endpoints
@app.get("/api/pilot-areas")
def get_pilot_areas():
    return {
        "hub": "Jinnah Garden",
        "areas": [
            {
                "name": name,
                "lat": info["lat"],
                "lng": info["lng"],
                "description": info["description"],
                "keywords": info["keywords"],
            }
            for name, info in PILOT_AREAS.items()
        ],
    }


@app.get("/api/admin/ai-status")
def ai_status(x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    return {
        "gemini_configured": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL,
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
    }


class AITestIn(BaseModel):
    text: str


@app.post("/api/admin/test-ai")
def test_ai(body: AITestIn, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    result = ai_analyze(body.text)
    return {
        "input": body.text,
        "result": result,
    }


@app.get("/api/admin/daily-ai-summary")
def get_daily_ai_summary(x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        recent_requests = con.execute(
            """SELECT id, category, area, status, urgency, created_at
               FROM requests ORDER BY id DESC LIMIT 20"""
        ).fetchall()
        total_requests = con.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        active_jobs = con.execute("SELECT COUNT(*) FROM requests WHERE status IN ('assigned', 'accepted')").fetchone()[0]
        completed_jobs = con.execute("SELECT COUNT(*) FROM requests WHERE status IN ('work_done', 'confirmed')").fetchone()[0]
        disputes_count = con.execute("SELECT COUNT(*) FROM disputes WHERE status='open'").fetchone()[0]
        rev = con.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status='released'").fetchone()[0]

        stats = {
            "total_requests": total_requests,
            "active_jobs": active_jobs,
            "completed_jobs": completed_jobs,
            "disputes_count": disputes_count,
            "total_revenue": rev,
        }
        jobs_list = [dict(r) for r in recent_requests]
        summary_text = gemini_helper.generate_daily_admin_summary(jobs_list, stats, api_key=GEMINI_API_KEY)
        return {
            "gemini_configured": bool(GEMINI_API_KEY),
            "stats": stats,
            "summary": summary_text or "Daily operations summary: All squads active across Islamabad & Jinnah Garden belt. Operations normal.",
        }


@app.get("/api/admin/disputes/{rid}/ai-analysis")
def get_dispute_ai_analysis(rid: int, x_admin_key: str = Header(None)):
    admin_auth(x_admin_key)
    with db() as con:
        disp = con.execute("SELECT * FROM disputes WHERE request_id=?", (rid,)).fetchone()
        if not disp:
            raise HTTPException(404, "Dispute not found")
        req = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        photos_count = con.execute("SELECT COUNT(*) FROM job_photos WHERE request_id=?", (rid,)).fetchone()[0]

        dispute_payload = {
            "request_id": rid,
            "category": req["category"] if req else "Unknown",
            "quoted_price": req["quoted_price"] if req else 0,
            "amount": req["amount"] if req else 0,
            "reason": disp["reason"],
            "description": req["description"] if req else "",
            "work_note": req["work_note"] if req else "",
            "photo_count": photos_count,
        }
        analysis = gemini_helper.draft_dispute_summary(dispute_payload, api_key=GEMINI_API_KEY)
        return {
            "dispute_id": disp["id"],
            "request_id": rid,
            "ai_analysis": analysis or {
                "assessment": "Customer disputed the service outcome or pricing. Review before/after photographic proof.",
                "fault": "unclear",
                "recommended_action": "partial_payout",
                "proposed_resolution_text": "Hold payout pending direct verification with both the customer and technician.",
            },
        }


@app.get("/api/track/{token}/whatsapp-draft")
def get_whatsapp_draft(token: str):
    with db() as con:
        r = con.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        tech_name = None
        if r["technician_id"]:
            t = con.execute("SELECT name FROM technicians WHERE id=?", (r["technician_id"],)).fetchone()
            if t:
                tech_name = t["name"]
        text = gemini_helper.draft_whatsapp_update(
            customer_name=r["customer_name"] or "Valued Customer",
            category=r["category"] or "Home Service",
            status=r["status"],
            tech_name=tech_name,
            eta_minutes=20,
        )
        return {
            "token": token,
            "status": r["status"],
            "whatsapp_text": text,
        }

