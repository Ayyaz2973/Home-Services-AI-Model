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
    assert data["tech_rating"] >= 4.0



