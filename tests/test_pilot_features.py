"""Tests for the HomeServe Jinnah Garden Pilot Increment Features.
Verifies:
- Address privacy in customer tracking view
- Immutable commission ledger (integer paisa minor units)
- Technician statement endpoint
- Structured customer dispute and admin dispute resolution with ledger adjustment
- Quote revision history and admin endpoints
"""
import io
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from datetime import datetime, timedelta
import httpx
import main


def make_dummy_image() -> bytes:
    """Helper to generate a valid in-memory JPEG for evidence uploads."""
    buf = io.BytesIO()
    img = Image.new("RGB", (100, 100), color=(73, 109, 137))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_address_privacy_in_tracking(client):
    """Customer address should be masked in /api/track/{token} until accepted."""
    req_res = client.post("/api/requests", json={
        "name": "Zubair",
        "phone": "03110001122",
        "address": "House 10, Street 4, Sector G, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Ceiling fan installation",
        "terms_accepted": True,
    })
    assert req_res.status_code == 200
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    # Before acceptance: address should be masked
    track_res = client.get(f"/api/track/{token}")
    assert track_res.status_code == 200
    track_data = track_res.json()
    assert "House 10" not in track_data["address"]
    assert "Jinnah Garden" in track_data["address"]
    assert "full address shared after technician accepts" in track_data["address"]

    # Get the technician who was assigned this job
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    dash = client.get("/api/admin/dashboard", headers=admin_h).json()
    job_req = next(r for r in dash["requests"] if r["id"] == req_id)
    tech_id = job_req["technician_id"]
    assigned_tech = next(t for t in dash["technicians"] if t["id"] == tech_id)
    tech_h = {"X-Phone": assigned_tech["phone"], "X-Pin": "1234"}

    # Technician accepts
    accept_res = client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    assert accept_res.status_code == 200

    # After acceptance: full address is shared
    track_after = client.get(f"/api/track/{token}").json()
    assert "House 10, Street 4, Sector G, Jinnah Garden" in track_after["address"]


def test_commission_ledger_and_tech_statement(client):
    """Verify payment release writes to commission_ledger in paisa, and tech statement returns it."""
    req_res = client.post("/api/requests", json={
        "name": "Ali Raza",
        "phone": "03009998877",
        "address": "Flat 3, Jinnah Avenue",
        "area": "Jinnah Garden",
        "description": "Switch replacement",
        "terms_accepted": True,
    })
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    # Identify assigned technician
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    dash = client.get("/api/admin/dashboard", headers=admin_h).json()
    job_req = next(r for r in dash["requests"] if r["id"] == req_id)
    assigned_tech = next(t for t in dash["technicians"] if t["id"] == job_req["technician_id"])
    tech_h = {"X-Phone": assigned_tech["phone"], "X-Pin": "1234"}

    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={"latitude": 33.6, "longitude": 73.1, "accuracy": 15})
    dummy = make_dummy_image()
    client.post(f"/api/tech/jobs/{req_id}/photos/before", headers=tech_h, files={"file": ("b.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/photos/after", headers=tech_h, files={"file": ("a.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 2500.0})

    # Customer confirms
    conf_res = client.post(f"/api/track/{token}/confirm", json={"rating": 5})
    assert conf_res.status_code == 200

    # Statement check
    stmt_res = client.get("/api/tech/statement", headers=tech_h)
    assert stmt_res.status_code == 200
    stmt = stmt_res.json()
    assert stmt["technician"] == assigned_tech["name"]
    assert len(stmt["entries"]) >= 1

    entry = stmt["entries"][0]
    # 2500 PKR = 250000 paisa gross
    assert entry["gross_amount"] == 250000
    # 10% commission = 25000 paisa
    assert entry["commission_amount"] == 25000
    # Net payout = 225000 paisa (Rs 2250)
    assert entry["net_payout"] == 225000
    assert entry["status"] == "released"
    assert stmt["total_net_paisa"] >= 225000


def test_dispute_flow_and_admin_resolution(client):
    """Verify structured dispute flow and admin resolution with ledger adjustment."""
    req_res = client.post("/api/requests", json={
        "name": "Bilal",
        "phone": "03007776655",
        "address": "Street 9, FECHS",
        "area": "FECHS",
        "description": "Fan issue",
        "terms_accepted": True,
    })
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    dash = client.get("/api/admin/dashboard", headers=admin_h).json()
    job_req = next(r for r in dash["requests"] if r["id"] == req_id)
    assigned_tech = next(t for t in dash["technicians"] if t["id"] == job_req["technician_id"])
    tech_h = {"X-Phone": assigned_tech["phone"], "X-Pin": "1234"}

    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={"latitude": 33.6, "longitude": 73.1, "accuracy": 15})
    dummy = make_dummy_image()
    client.post(f"/api/tech/jobs/{req_id}/photos/before", headers=tech_h, files={"file": ("b.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/photos/after", headers=tech_h, files={"file": ("a.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1000.0})
    client.post(f"/api/track/{token}/confirm", json={"rating": 4})

    # Raise dispute
    disp_res = client.post(
        f"/api/track/{token}/dispute",
        json={"reason": "Fan speed regulator not working after repair", "evidence": "Noise and burning smell"},
    )
    assert disp_res.status_code == 200
    assert disp_res.json()["status"] == "disputed"

    # Duplicate dispute rejected
    dup_res = client.post(
        f"/api/track/{token}/dispute",
        json={"reason": "Second dispute attempt", "evidence": ""},
    )
    assert dup_res.status_code == 409

    # Admin resolves dispute with partial refund adjustment (Rs 300 = 30000 paisa)
    resolve_res = client.post(
        f"/api/admin/disputes/{req_id}/resolve",
        headers=admin_h,
        json={"resolution": "Agreed on Rs 300 compensation", "refund_amount": 300.0},
    )
    assert resolve_res.status_code == 200

    # Verify ledger adjustment
    stmt = client.get("/api/tech/statement", headers=tech_h).json()
    adjustment_entry = next((e for e in stmt["entries"] if e["status"] == "adjustment"), None)
    assert adjustment_entry is not None
    assert adjustment_entry["net_payout"] == -30000
    assert "Refund:" in adjustment_entry["note"]


def test_quote_revision_history(client):
    """Admin can record quote revisions and retrieve history."""
    req_res = client.post("/api/requests", json={
        "name": "Hamza",
        "phone": "03005554433",
        "address": "Block B, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Wiring check",
        "terms_accepted": True,
    })
    req_id = req_res.json()["id"]

    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    rev_res = client.post(
        f"/api/admin/requests/{req_id}/quote-revision",
        headers=admin_h,
        json={"quoted_price": 3500.0, "visit_fee": 500.0, "reason": "Complex wiring replacement needed"},
    )
    assert rev_res.status_code == 200

    # Retrieve revisions
    quotes_res = client.get(f"/api/requests/{req_id}/quotes", headers=admin_h)
    assert quotes_res.status_code == 200
    revisions = quotes_res.json()["revisions"]
    assert len(revisions) == 1
    assert revisions[0]["quoted_price"] == 3500.0
    assert revisions[0]["visit_fee"] == 500.0
    assert revisions[0]["reason"] == "Complex wiring replacement needed"


def test_jinnah_garden_keywords():
    """Verify Jinnah Garden pilot keywords constant."""
    assert "jinnah garden" in main.JINNAH_GARDEN_KEYWORDS
    assert "fechs" in main.JINNAH_GARDEN_KEYWORDS


def test_pilot_areas_configuration(client):
    """Verify all 6 pilot towns are configured in PILOT_AREAS and returned by /api/pilot-areas."""
    expected_areas = ["Jinnah Garden", "Naval Anchorage", "Soan Gardens", "PWD", "Korang Town", "Pakistan Town"]
    for area in expected_areas:
        assert area in main.PILOT_AREAS
        assert "lat" in main.PILOT_AREAS[area]
        assert "lng" in main.PILOT_AREAS[area]

    res = client.get("/api/pilot-areas")
    assert res.status_code == 200
    data = res.json()
    assert data["hub"] == "Jinnah Garden"
    names = [a["name"] for a in data["areas"]]
    for area in expected_areas:
        assert area in names


def test_area_normalization():
    """Verify user input variations normalize to canonical pilot areas."""
    assert main.normalize_area("Swan Garden") == "Soan Gardens"
    assert main.normalize_area("soan gardens") == "Soan Gardens"
    assert main.normalize_area("korrang town") == "Korang Town"
    assert main.normalize_area("naval anchorage") == "Naval Anchorage"
    assert main.normalize_area("navel") == "Naval Anchorage"
    assert main.normalize_area("PWD road") == "PWD"
    assert main.normalize_area("pak town") == "Pakistan Town"
    assert main.normalize_area("jinnah gardens phase 2") == "Jinnah Garden"


def test_request_with_gps_coordinates(client):
    """Customer request with map-pinned GPS coordinates is stored and returned."""
    req_res = client.post("/api/requests", json={
        "name": "Tariq Map User",
        "phone": "03331112233",
        "address": "Street 12, Phase 1, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Short circuit in main board",
        "terms_accepted": True,
        "latitude": 33.5682,
        "longitude": 73.1553,
    })
    assert req_res.status_code == 200
    token = req_res.json()["token"]

    track_res = client.get(f"/api/track/{token}")
    assert track_res.status_code == 200
    track_data = track_res.json()
    assert track_data["latitude"] == pytest.approx(33.5682, rel=1e-3)
    assert track_data["longitude"] == pytest.approx(73.1553, rel=1e-3)


def test_admin_ai_diagnostics(client, monkeypatch):
    """Verify /api/admin/ai-status and /api/admin/test-ai endpoints."""
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    status_res = client.get("/api/admin/ai-status", headers=admin_h)
    assert status_res.status_code == 200
    assert "gemini_configured" in status_res.json()

    test_res = client.post(
        "/api/admin/test-ai",
        headers=admin_h,
        json={"text": "kitchen ka nalka leak ho raha hai"},
    )
    assert test_res.status_code == 200
    result = test_res.json()["result"]
    assert result["category"] == "Plumbing"


def test_ai_analyze_gemini_integration(monkeypatch):
    """Verify Gemini AI parser correctly processes Gemini API responses."""
    class MockResponse:
        status_code = 200
        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"category": "Air Conditioning", "urgency": "urgent", "summary": "AC gas refill needed in Jinnah Garden"}'
                                }
                            ]
                        }
                    }
                ]
            }

    monkeypatch.setattr(main, "GEMINI_API_KEY", "fake_gemini_key_123")
    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: MockResponse())

    res = main.ai_analyze_gemini("AC cooling nahi kar raha gas khatam hai")
    assert res is not None
    assert res["category"] == "Air Conditioning"
    assert res["urgency"] == "urgent"
    assert "gemini" in res["engine"]
    assert "AC gas refill" in res["summary"]


def test_pricing_catalog_endpoint(client):
    """Verify transparent PKR upfront pricing catalog returns standard services."""
    res = client.get("/api/pricing-catalog")
    assert res.status_code == 200
    catalog = res.json()["catalog"]
    assert "Electric Work" in catalog
    assert "Plumbing" in catalog
    assert "Air Conditioning" in catalog
    assert "Geyser Repair" in catalog
    assert any("Ceiling Fan" in item["item"] for item in catalog["Electric Work"])
    assert any("Rs." in item["price_range"] for item in catalog["Electric Work"])


def test_seasonal_packages_endpoint(client):
    """Verify climate-adapted seasonal packages return active pre-winter tune-ups."""
    res = client.get("/api/seasonal-packages")
    assert res.status_code == 200
    packages = res.json()["packages"]
    assert len(packages) >= 3
    pre_winter = next(p for p in packages if "pre-winter" in p["id"])
    assert "Geyser" in pre_winter["title"]
    assert pre_winter["price_pkr"] > 0
    assert len(pre_winter["checklist"]) >= 3


def test_work_showcase_endpoint(client):
    """Verify before/after visual proof showcase endpoint."""
    res = client.get("/api/work-showcase")
    assert res.status_code == 200
    showcase = res.json()["showcase"]
    assert len(showcase) >= 4
    jinnah_project = next(p for p in showcase if "Jinnah Garden" in p["area"])
    assert "Verified Pro" in jinnah_project["badge"]
    assert jinnah_project["before_desc"]
    assert jinnah_project["after_desc"]
    assert jinnah_project["rating"] >= 4.8


def test_track_verified_badge_enrichment(client):
    """Verify /api/track/{token} includes verified pro trust badge and rating."""
    req_res = client.post("/api/requests", json={
        "name": "Hamza",
        "phone": "03335554433",
        "address": "House 12, Street 3, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Short circuit in distribution box",
        "terms_accepted": True,
    })
    assert req_res.status_code == 200
    token = req_res.json()["token"]

    track_res = client.get(f"/api/track/{token}")
    assert track_res.status_code == 200
    data = track_res.json()
    assert data["tech_is_verified"] is True
    assert data["tech_badge"] == "HomeServe Verified Pro"
def test_customer_review_and_warranty_flow(client):
    """Customer can confirm with review text and receives a 7-day warranty."""
    req_res = client.post("/api/requests", json={
        "name": "Suleman Khan",
        "phone": "03339998877",
        "address": "Street 9, Phase 1, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Short circuit in fan switch",
        "terms_accepted": True,
    })
    assert req_res.status_code == 200
    token = req_res.json()["token"]
    rid = req_res.json()["id"]

    # Technician accepts, checks in, uploads photos, marks done
    tech_h = {"X-Phone": "03001110007", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{rid}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{rid}/check-in", headers=tech_h, json={"latitude": 33.568, "longitude": 73.155, "accuracy": 20})
    
    # Upload minimal valid jpeg before & after
    img_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
    client.post(f"/api/tech/jobs/{rid}/photos/before", headers=tech_h, files={"file": ("before.jpg", img_bytes, "image/jpeg")})
    client.post(f"/api/tech/jobs/{rid}/photos/after", headers=tech_h, files={"file": ("after.jpg", img_bytes, "image/jpeg")})
    client.post(f"/api/tech/jobs/{rid}/done", headers=tech_h, json={"amount": 1200, "note": "Switch replaced"})

    # Customer confirms with 5 stars and written review
    conf_res = client.post(f"/api/track/{token}/confirm", json={
        "rating": 5,
        "review_text": "Zubair bhai did excellent work on time!"
    })
    assert conf_res.status_code == 200

    # Verify track returns warranty_until and customer_review
    track = client.get(f"/api/track/{token}").json()
    assert track["status"] == "confirmed"
    assert track["customer_review"] == "Zubair bhai did excellent work on time!"
    assert track["warranty_until"] is not None

    # Customer claims warranty revisit
    claim_res = client.post(f"/api/track/{token}/claim-warranty", json={
        "issue_note": "Switch sparked again after 2 days, need revisit."
    })
    assert claim_res.status_code == 200
    assert claim_res.json()["ok"] is True

    # Duplicate claim is rejected
    dup_res = client.post(f"/api/track/{token}/claim-warranty", json={
        "issue_note": "Second claim attempt."
    })
    assert dup_res.status_code == 400


def test_technician_deposit_wallet(client):
    """Admin can record a wallet deposit for a technician."""
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    res = client.post(
        "/api/admin/technicians/1/deposit",
        headers=admin_h,
        json={"amount": 2500, "note": "EasyPaisa cash deposit receipt EP-9821"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["amount"] == 2500
def test_emergency_dispatch_3min_timeout(client):
    """Emergency requests get swept and re-assigned after 3 minutes (vs 10 for normal)."""
    # Create emergency request
    res = client.post("/api/requests", json={
        "name": "Fire Risk Emergency",
        "phone": "03332221100",
        "address": "Street 1, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "sparking and aag short circuit breaker burning",
        "terms_accepted": True,
    })
    assert res.status_code == 200
    rid = res.json()["id"]

    # Manually backdate assigned_at by 4 minutes (which exceeds 3 min emergency timeout, but under 10 min normal)
    with main.db() as con:
        four_min_ago = (datetime.now() - timedelta(minutes=4)).isoformat(timespec="seconds")
        con.execute("UPDATE requests SET assigned_at=? WHERE id=?", (four_min_ago, rid))
        main.sweep(con)
        
        # Verify decline logged for emergency timeout
        row = con.execute("SELECT * FROM declines WHERE request_id=?", (rid,)).fetchone()
        assert row is not None


def test_workshop_registration_and_review_flow(client):
    """Workshop can register, check status, and be reviewed by admin."""
    # 1. Register new workshop
    form_data = {
        "workshop_name": "Al-Buraq AC & Solar Lab",
        "owner_name": "Rashid Mehmood",
        "phone": "03008887766",
        "whatsapp": "03008887766",
        "category": "Air Conditioning",
        "area": "Jinnah Garden",
        "address": "Shop 9, Main Commercial, Jinnah Garden",
        "technicians_count": "4",
        "services_offered": "Inverter PCB micro-soldering, split AC coil welding, chemical flush",
        "equipment_summary": "Digital manifold, vacuum pump, chemical wash rig",
        "ntn": "7182930-1",
        "cnic": "61101-9988776-1",
        "terms_accepted": "true",
    }
    reg_res = client.post("/api/workshops/register", data=form_data)
    assert reg_res.status_code == 200
    data = reg_res.json()
    assert data["ok"] is True
    assert data["status"] == "pending"
    wid = data["workshop_id"]

    # 2. Duplicate phone is rejected
    dup_res = client.post("/api/workshops/register", data=form_data)
    assert dup_res.status_code == 409

    # 3. Status lookup by phone
    status_res = client.get("/api/workshops/status/03008887766")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["workshop_name"] == "Al-Buraq AC & Solar Lab"
    assert status_data["status"] == "pending"

    # 4. Admin lists workshops
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    admin_list = client.get("/api/admin/workshops", headers=admin_h)
    assert admin_list.status_code == 200
    workshops = admin_list.json()["workshops"]
    assert any(w["id"] == wid for w in workshops)

    # 5. Admin approves workshop
    review_res = client.post(
        f"/api/admin/workshops/{wid}/review",
        headers=admin_h,
        json={"action": "approve", "review_note": "Physical facility and equipment verified in Jinnah Garden."},
    )
    assert review_res.status_code == 200
    assert review_res.json()["status"] == "approved"

    # 6. Verify in public workshops directory
    public_res = client.get("/api/workshops")
    assert public_res.status_code == 200
    pub_list = public_res.json()["workshops"]
    assert any(w["workshop_name"] == "Al-Buraq AC & Solar Lab" for w in pub_list)

    # 7. Public page route serves HTML
    page_res = client.get("/workshop")
    assert page_res.status_code == 200
    assert "Workshop" in page_res.text


def test_preferred_arrival_slots_and_emergency_routing(client):
    """Customer can specify preferred arrival slot (morning/afternoon/evening/emergency) and emergency elevates urgency."""
    # 1. Morning slot request
    morn_res = client.post("/api/requests", json={
        "name": "Noman Tariq",
        "phone": "03001239988",
        "address": "Street 5, Phase 2, Jinnah Garden",
        "area": "Jinnah Garden",
        "description": "Fix leaking bibcock tap in bathroom",
        "preferred_slot": "morning",
        "terms_accepted": True,
    })
    assert morn_res.status_code == 200
    morn_data = morn_res.json()
    assert morn_data["preferred_slot"] == "morning"
    token = morn_data["token"]

    # Verify track returns preferred_slot
    track = client.get(f"/api/track/{token}").json()
    assert track["preferred_slot"] == "morning"

    # 2. Emergency slot elevates urgency
    emerg_res = client.post("/api/requests", json={
        "name": "Kamran Alert",
        "phone": "03001239977",
        "address": "Main Commercial, Soan Gardens",
        "area": "Soan Gardens",
        "description": "Short circuit sparking in DB board",
        "preferred_slot": "emergency",
        "terms_accepted": True,
    })
    assert emerg_res.status_code == 200
    emerg_token = emerg_res.json()["token"]
    track_emerg = client.get(f"/api/track/{emerg_token}").json()
    assert track_emerg["preferred_slot"] == "emergency"
    assert track_emerg["urgency"] == "emergency"


def test_water_tank_cleaning_and_renovation_catalog(client):
    """Verify new high-demand categories (Water Tank Cleaning, Home Renovation) exist in categories, keywords, and pricing catalog."""
    assert "Water Tank Cleaning" in main.CATEGORIES
    assert "Home Renovation" in main.CATEGORIES

    # Keyword triage
    triage_tank = main.ai_analyze("hamari underground pani ki tanki safai karni hai bht kachra hai")
    assert triage_tank["category"] == "Water Tank Cleaning"

    triage_reno = main.ai_analyze("we need full house renovation and false ceiling in drawing room")
    assert triage_reno["category"] == "Home Renovation"

    # Pricing catalog verification
    res = client.get("/api/pricing-catalog")
    assert res.status_code == 200
    catalog = res.json()["catalog"]
    assert "Water Tank Cleaning" in catalog
    assert "Home Renovation" in catalog
    assert any("Overhead Water Tank" in item["item"] for item in catalog["Water Tank Cleaning"])
    assert any("Turnkey Renovation" in item["item"] for item in catalog["Home Renovation"])

    # Seasonal packages includes solar panel washing
    pkg_res = client.get("/api/seasonal-packages")
    assert pkg_res.status_code == 200
    packages = pkg_res.json()["packages"]
    assert any("solar-panel-deep-clean" in p["id"] for p in packages)






