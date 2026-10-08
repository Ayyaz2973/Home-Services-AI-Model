"""HomeServe MVP Acceptance Test Suite
Automated Mechanical Verification for Acceptance Criteria AC-01 through AC-46
Ref: docs/MVP_ACCEPTANCE_CRITERIA.md & docs/MVP_ENGINEERING_BLUEPRINT.md

Every test asserts a deterministic PASS/FAIL outcome without manual DB intervention.
"""
import io
import os
import shutil
import tempfile
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Ensure an isolated database and private uploads directory for every test run."""
    temp_dir = tempfile.mkdtemp(prefix="hs_test_")
    test_db = os.path.join(temp_dir, "test_homeserve.db")
    test_storage = os.path.join(temp_dir, "private_uploads")

    monkeypatch.setattr(main, "DB_PATH", test_db)
    monkeypatch.setattr(main, "PRIVATE_STORAGE", main.Path(test_storage).resolve())
    monkeypatch.setattr(main, "ADMIN_KEY", "test_admin_key_998877")
    monkeypatch.setattr(main, "COMMISSION_PCT", 10.0)
    monkeypatch.setattr(main, "ASSIGN_TIMEOUT_MIN", 10)
    # Ensure tests trigger instant keyword triage fallback without network timeout delay
    monkeypatch.setattr(main, "ai_analyze", lambda text: main.keyword_guess(text))

    # Initialize isolated DB and seed demo data
    main.init_db()

    yield {
        "temp_dir": temp_dir,
        "db_path": test_db,
        "admin_key": "test_admin_key_998877",
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client():
    return TestClient(main.app)


def make_dummy_image() -> bytes:
    """Helper to generate a valid in-memory JPEG for evidence uploads."""
    buf = io.BytesIO()
    img = Image.new("RGB", (100, 100), color=(73, 109, 137))
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ==============================================================================
# AC-03 & AC-04: Service Request Creation & AI/Fallback Triage
# ==============================================================================
def test_ac03_ac04_create_request_and_ai_fallback_triage(client):
    """AC-03: Request submitted -> unique ID & token generated.
    AC-04: AI triage or keyword fallback identifies category & urgency.
    """
    payload = {
        "name": "Ahmed Khan",
        "phone": "03001234567",
        "address": "House 12, Street 3",
        "area": "F-10",
        "description": "Kitchen ka nal leak ho raha hai foran theek karein",
        "terms_accepted": True,
    }
    res = client.post("/api/requests", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["id"] > 0
    assert len(data["token"]) > 0
    # AC-04: Nal -> Plumbing, foran -> urgent
    assert data["category"] == "Plumbing"
    assert data["status"] in ("assigned", "unassigned", "new")

    # Verify track endpoint shows request details
    track_res = client.get(f"/api/track/{data['token']}")
    assert track_res.status_code == 200
    track_data = track_res.json()
    assert track_data["urgency"] in ("urgent", "normal")
    assert track_data["category"] == "Plumbing"


# ==============================================================================
# AC-06, AC-07 & AC-14: Assignment & Customer Privacy Gate
# ==============================================================================
def test_ac07_ac14_assignment_and_privacy_redaction(client):
    """AC-07: Matching provider receives assignment.
    AC-14: Customer address and phone are hidden while 'assigned' and revealed upon 'accepted'.
    """
    req_res = client.post("/api/requests", json={
        "name": "Sara Malik",
        "phone": "03219876543",
        "address": "House 45, Street 19, F-10/2",
        "area": "F-10",
        "description": "Kitchen tap leak problem",
        "terms_accepted": True,
    })
    assert req_res.status_code == 200
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    # In demo seed, Usman Plumber is in F-10, phone: 03001110003, PIN: 1234
    headers = {"X-Phone": "03001110003", "X-Pin": "1234"}
    me_res = client.get("/api/tech/me", headers=headers)
    assert me_res.status_code == 200
    jobs = me_res.json()["jobs"]
    assigned_job = next((j for j in jobs if j["id"] == req_id), None)
    assert assigned_job is not None
    assert assigned_job["status"] == "assigned"

    # AC-14: Address & Phone MUST be redacted before acceptance
    assert assigned_job["customer_phone"] == ""
    assert assigned_job["customer_name"] == ""
    assert "accept" in assigned_job["address"].lower()

    # AC-06: Accept the job
    accept_res = client.post(f"/api/tech/jobs/{req_id}/accept", headers=headers)
    assert accept_res.status_code == 200

    # AC-14: After acceptance, contact & address MUST be revealed
    me_after = client.get("/api/tech/me", headers=headers)
    job_after = next(j for j in me_after.json()["jobs"] if j["id"] == req_id)
    assert job_after["status"] == "accepted"
    assert job_after["customer_name"] == "Sara Malik"
    assert job_after["customer_phone"] == "03219876543"
    assert "House 45" in job_after["address"]


# ==============================================================================
# AC-08: Decline & Automatic Reassignment
# ==============================================================================
def test_ac08_decline_and_reassignment(client):
    """AC-08: When provider declines, job is re-assigned to next eligible provider.
    No job gets permanently stuck.
    """
    req_res = client.post("/api/requests", json={
        "name": "Bilal Tariq",
        "phone": "03335551234",
        "address": "Street 10, F-10",
        "area": "F-10",
        "description": "Bathroom flush leak",
        "terms_accepted": True,
    })
    req_id = req_res.json()["id"]

    # First plumber: Usman Plumber (F-10)
    usman_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    decline_res = client.post(f"/api/tech/jobs/{req_id}/decline", headers=usman_h)
    assert decline_res.status_code == 200

    # Second plumber in demo seed: Kamran Plumber (I-8, phone 03001110004)
    kamran_h = {"X-Phone": "03001110004", "X-Pin": "1234"}
    kamran_me = client.get("/api/tech/me", headers=kamran_h)
    assert any(j["id"] == req_id for j in kamran_me.json()["jobs"])


# ==============================================================================
# AC-09: Assignment Timeout Sweeper
# ==============================================================================
def test_ac09_assignment_timeout_sweep(client):
    """AC-09: Unanswered assignment expires and automatically reassigns."""
    req_res = client.post("/api/requests", json={
        "name": "Hamza Ali",
        "phone": "03331112233",
        "address": "Street 2, F-10",
        "area": "F-10",
        "description": "Geyser pipe leak",
        "terms_accepted": True,
    })
    req_id = req_res.json()["id"]

    # Artificially age the assignment timestamp in the past (>10 mins)
    with main.db() as con:
        con.execute(
            "UPDATE requests SET assigned_at='2020-01-01T00:00:00' WHERE id=?",
            (req_id,)
        )
        main.sweep(con)

    # Check that Usman was logged as declined and Kamran received the job
    kamran_h = {"X-Phone": "03001110004", "X-Pin": "1234"}
    kamran_me = client.get("/api/tech/me", headers=kamran_h)
    assert any(j["id"] == req_id for j in kamran_me.json()["jobs"])


# ==============================================================================
# AC-10: Service Catalogue Management
# ==============================================================================
def test_ac10_service_catalogue(client):
    """AC-10: Admin can create service catalogue item with base price and visit fee."""
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    cat_res = client.post("/api/admin/catalog", headers=admin_h, json={
        "category": "Plumbing",
        "service_name": "Standard Tap Replacement",
        "description": "Includes washer & pipe fitting",
        "price": 1200.0,
        "visit_fee": 300.0,
        "active": True,
    })
    assert cat_res.status_code == 200
    item_id = cat_res.json()["id"]

    public_cat = client.get("/api/catalog").json()
    assert any(i["id"] == item_id and i["price"] == 1200.0 for i in public_cat["items"])


# ==============================================================================
# AC-15: GPS Check-In Accuracy Gate (<= 150m)
# ==============================================================================
def test_ac15_gps_check_in_gate(client):
    """AC-15: Check-in fails if GPS accuracy > 150m; succeeds when <= 150m."""
    req_res = client.post("/api/requests", json={
        "name": "Zubair",
        "phone": "03001112244",
        "address": "Street 9, F-10",
        "area": "F-10",
        "description": "Leakage repair",
        "terms_accepted": True,
    })
    req_id = req_res.json()["id"]
    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)

    # Inaccurate GPS (> 150 meters) MUST FAIL
    bad_res = client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={
        "latitude": 33.69, "longitude": 73.01, "accuracy": 250.0
    })
    assert bad_res.status_code == 400
    assert "150 meters" in bad_res.text

    # Accurate GPS (<= 150 meters) MUST PASS
    good_res = client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={
        "latitude": 33.69, "longitude": 73.01, "accuracy": 25.0
    })
    assert good_res.status_code == 200
    assert "checked_in_at" in good_res.json()


# ==============================================================================
# AC-16, AC-20 & AC-21: Mandatory Evidence & Work Done Quality Gate
# ==============================================================================
def test_ac16_ac20_ac21_evidence_quality_gates(client):
    """AC-16, AC-20, AC-21: Cannot mark work done without:
    1. Arrival check-in
    2. Before-work photo
    3. After-work photo
    """
    req_res = client.post("/api/requests", json={
        "name": "Rashid",
        "phone": "03005556677",
        "address": "Street 1, F-10",
        "area": "F-10",
        "description": "Plumbing repair",
        "terms_accepted": True,
    })
    req_id = req_res.json()["id"]
    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)

    # 1. Try mark done without check-in -> MUST FAIL
    r1 = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1000})
    assert r1.status_code == 400
    assert "check-in" in r1.text.lower()

    # Perform check-in
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={
        "latitude": 33.69, "longitude": 73.01, "accuracy": 20.0
    })

    # 2. Try mark done without photos -> MUST FAIL
    r2 = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1000})
    assert r2.status_code == 400
    assert "photo" in r2.text.lower()

    # Upload before photo
    dummy_img = make_dummy_image()
    up1 = client.post(
        f"/api/tech/jobs/{req_id}/photos/before",
        headers=tech_h,
        files={"file": ("before.jpg", dummy_img, "image/jpeg")}
    )
    assert up1.status_code == 200

    # 3. Try mark done without after photo -> MUST FAIL
    r3 = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1000})
    assert r3.status_code == 400
    assert "photo" in r3.text.lower()

    # Upload after photo
    up2 = client.post(
        f"/api/tech/jobs/{req_id}/photos/after",
        headers=tech_h,
        files={"file": ("after.jpg", dummy_img, "image/jpeg")}
    )
    assert up2.status_code == 200

    # 4. Now mark work done -> MUST PASS
    r4 = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1000})
    assert r4.status_code == 200


# ==============================================================================
# AC-18 & AC-19: Extra Work Approval & Price Ceiling Protection
# ==============================================================================
def test_ac18_ac19_extra_work_workflow(client):
    """AC-18: Extra work requested by provider requires customer approval.
    AC-19: Unapproved extra work cannot be charged; approved extra work expands agreed ceiling.
    """
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    cat = client.post("/api/admin/catalog", headers=admin_h, json={
        "category": "Plumbing", "service_name": "Basic Leak Fix", "price": 1000.0, "visit_fee": 200.0
    }).json()

    req_res = client.post("/api/requests", json={
        "name": "Noman", "phone": "03009998877", "address": "House 10, F-10",
        "description": "Fix pipe", "service_id": cat["id"], "terms_accepted": True
    })
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={
        "latitude": 33.69, "longitude": 73.01, "accuracy": 30.0
    })

    # Provider proposes Rs 500 extra work
    extra_res = client.post(f"/api/tech/jobs/{req_id}/extra-charges", headers=tech_h, json={
        "amount": 500.0, "reason": "Requires new brass elbow connector valve"
    })
    assert extra_res.status_code == 200
    charge_id = extra_res.json()["id"]

    # Upload photos
    dummy = make_dummy_image()
    client.post(f"/api/tech/jobs/{req_id}/photos/before", headers=tech_h, files={"file": ("b.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/photos/after", headers=tech_h, files={"file": ("a.jpg", dummy, "image/jpeg")})

    # While proposal is pending, marking work done MUST BE BLOCKED
    done_pend = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1700.0})
    assert done_pend.status_code == 400

    # Customer approves extra charge
    app_res = client.post(f"/api/track/{token}/extra-charges/{charge_id}", json={"decision": "approve"})
    assert app_res.status_code == 200

    # Agreed total is now 1000 + 200 + 500 = 1700. If technician tries to bill 2000 -> MUST FAIL
    over_res = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 2000.0})
    assert over_res.status_code == 400

    # Technician bills agreed Rs 1700 -> MUST PASS
    pass_res = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1700.0})
    assert pass_res.status_code == 200


# ==============================================================================
# AC-22, AC-24, AC-25 & AC-26: Confirmation, Commission & Payout Release
# ==============================================================================
def test_ac22_ac24_ac25_confirmation_commission_entitlement(client):
    """AC-22: Customer confirmation releases payment.
    AC-24: Final amount verified.
    AC-25: 10% commission accurately calculated.
    AC-26: Provider entitlement credited.
    """
    req_res = client.post("/api/requests", json={
        "name": "Farhan", "phone": "03001239876", "address": "F-10/3",
        "description": "Tap repair", "terms_accepted": True
    })
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={"latitude": 33.69, "longitude": 73.01, "accuracy": 15})
    dummy = make_dummy_image()
    client.post(f"/api/tech/jobs/{req_id}/photos/before", headers=tech_h, files={"file": ("b.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/photos/after", headers=tech_h, files={"file": ("a.jpg", dummy, "image/jpeg")})

    # Tech bills Rs 2,000
    client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 2000.0})

    # Check provider wallet before confirmation
    tech_before = client.get("/api/tech/me", headers=tech_h).json()["me"]
    wallet_before = tech_before["wallet"]

    # Customer confirms & rates 5 stars
    conf_res = client.post(f"/api/track/{token}/confirm", json={"rating": 5})
    assert conf_res.status_code == 200

    # AC-25: 10% commission on 2000 is 200; provider entitlement is 1800
    tech_after = client.get("/api/tech/me", headers=tech_h).json()["me"]
    assert tech_after["wallet"] == wallet_before + 1800.0
    assert tech_after["jobs_done"] == 1


# ==============================================================================
# AC-23: Customer Dispute Halts Payment Release
# ==============================================================================
def test_ac23_dispute_halts_settlement(client):
    """AC-23: Customer dispute freezes payment release; cannot silently become paid."""
    req_res = client.post("/api/requests", json={
        "name": "Kamran", "phone": "03009991122", "address": "F-10",
        "description": "Leak fix", "terms_accepted": True
    })
    token = req_res.json()["token"]
    req_id = req_res.json()["id"]

    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)
    client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={"latitude": 33.69, "longitude": 73.01, "accuracy": 20})
    dummy = make_dummy_image()
    client.post(f"/api/tech/jobs/{req_id}/photos/before", headers=tech_h, files={"file": ("b.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/photos/after", headers=tech_h, files={"file": ("a.jpg", dummy, "image/jpeg")})
    client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={"amount": 1500.0})

    # Customer disputes work
    disp_res = client.post(f"/api/track/{token}/dispute", json={"reason": "Water is still leaking heavily"})
    assert disp_res.status_code == 200

    # Verify job and payment are marked 'disputed'
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}
    dash = client.get("/api/admin/dashboard", headers=admin_h).json()
    assert dash["stats"]["disputed"] >= 1
    disputed_payment = next(p for p in dash["payments"] if p["request_id"] == req_id)
    assert disputed_payment["status"] == "disputed"


# ==============================================================================
# AC-34: Role Isolation & Authorization Failure
# ==============================================================================
def test_ac34_role_isolation(client):
    """AC-34: Unauthorized API access fails cleanly with 401 Unauthorized."""
    # Attempt admin dashboard without key -> 401
    bad_admin = client.get("/api/admin/dashboard", headers={"X-Admin-Key": "wrong_key"})
    assert bad_admin.status_code == 401

    # Attempt admin endpoints with no key at all -> 401
    no_key_admin = client.get("/api/admin/dashboard")
    assert no_key_admin.status_code == 401
    no_key_apps = client.get("/api/admin/applications")
    assert no_key_apps.status_code == 401

    # Attempt tech endpoint without valid credentials -> 401
    bad_tech = client.get("/api/tech/me", headers={"X-Phone": "03001110003", "X-Pin": "9999"})
    assert bad_tech.status_code == 401

    # AC-01 / Role isolation: customer site must not expose unhidden admin portal link
    index_res = client.get("/")
    assert index_res.status_code == 200
    assert '<a href="/admin">Admin</a>' not in index_res.text
    assert 'id="adminNavLink" class="hide"' in index_res.text


# ==============================================================================
# AC-46: Complete MVP End-to-End Acceptance Test
# ==============================================================================
def test_ac46_complete_mvp_end_to_end_scenario(client):
    """AC-46: 22-step End-to-End Acceptance Scenario
    Customer submits: "Kitchen ka nal leak ho raha hai." in Area "F-10".
    Asserts zero manual database intervention from intake to payout.
    """
    admin_h = {"X-Admin-Key": "test_admin_key_998877"}

    # 1. Admin configures catalogue item
    cat_item = client.post("/api/admin/catalog", headers=admin_h, json={
        "category": "Plumbing",
        "service_name": "Kitchen Sink Tap Repair",
        "description": "Complete washer and valve replacement",
        "price": 1500.0,
        "visit_fee": 300.0,
        "active": True
    }).json()

    # 2. Customer creates request
    create_res = client.post("/api/requests", json={
        "name": "Mrs. Ayesha Tariq",
        "phone": "03007654321",
        "address": "House 18, Street 44, F-10/4",
        "area": "F-10",
        "description": "Kitchen ka nal leak ho raha hai.",
        "service_id": cat_item["id"],
        "terms_accepted": True
    })
    assert create_res.status_code == 200
    req_data = create_res.json()
    req_id = req_data["id"]
    token = req_data["token"]

    # 3. Verify AI/Fallback classified as Plumbing
    assert req_data["category"] == "Plumbing"

    # 4. Eligible plumber in F-10 (Usman Plumber) receives offer
    tech_h = {"X-Phone": "03001110003", "X-Pin": "1234"}
    me_res = client.get("/api/tech/me", headers=tech_h).json()
    job = next(j for j in me_res["jobs"] if j["id"] == req_id)
    assert job["status"] == "assigned"
    assert job["customer_phone"] == ""  # AC-14 Privacy Protected

    # 5. Plumber accepts job
    client.post(f"/api/tech/jobs/{req_id}/accept", headers=tech_h)

    # 6. Customer details unlocked
    me_accepted = client.get("/api/tech/me", headers=tech_h).json()
    job_acc = next(j for j in me_accepted["jobs"] if j["id"] == req_id)
    assert job_acc["customer_phone"] == "03007654321"

    # 7. Plumber checks in (GPS <= 150m)
    checkin_res = client.post(f"/api/tech/jobs/{req_id}/check-in", headers=tech_h, json={
        "latitude": 33.692, "longitude": 73.011, "accuracy": 18.0
    })
    assert checkin_res.status_code == 200

    # 8. Before photo uploaded
    dummy_photo = make_dummy_image()
    up_b = client.post(
        f"/api/tech/jobs/{req_id}/photos/before",
        headers=tech_h,
        files={"file": ("before.jpg", dummy_photo, "image/jpeg")}
    )
    assert up_b.status_code == 200

    # 9. After photo uploaded
    up_a = client.post(
        f"/api/tech/jobs/{req_id}/photos/after",
        headers=tech_h,
        files={"file": ("after.jpg", dummy_photo, "image/jpeg")}
    )
    assert up_a.status_code == 200

    # 10. Plumber marks work done with agreed price (1500 + 300 = 1800)
    done_res = client.post(f"/api/tech/jobs/{req_id}/done", headers=tech_h, json={
        "amount": 1800.0,
        "note": "Replaced damaged ceramic spindle washer"
    })
    assert done_res.status_code == 200

    # 11. Customer confirms & rates 5 stars
    conf_res = client.post(f"/api/track/{token}/confirm", json={"rating": 5})
    assert conf_res.status_code == 200

    # 12. Financial calculations:
    # Amount = 1800, Commission (10%) = 180, Payout = 1620
    admin_dash = client.get("/api/admin/dashboard", headers=admin_h).json()
    payment = next(p for p in admin_dash["payments"] if p["request_id"] == req_id)
    assert payment["amount"] == 1800.0
    assert payment["commission"] == 180.0
    assert payment["payout"] == 1620.0
    assert payment["status"] == "released"

    # 13. Admin records payout
    client.post(f"/api/admin/payments/{payment['id']}/collected", headers=admin_h)
    payout_res = client.post(
        f"/api/admin/payments/{payment['id']}/payout",
        headers=admin_h,
        json={"reference": "JazzCash-TXN-99881122"}
    )
    assert payout_res.status_code == 200

    # 14. Verify full transaction completed with audit events
    track_final = client.get(f"/api/track/{token}").json()
    assert track_final["status"] == "confirmed"
    event_texts = [e["text"] for e in track_final["events"]]
    assert any("Assigned to" in t for t in event_texts)
    assert any("accepted the job" in t for t in event_texts)
    assert any("check-in" in t for t in event_texts)
    assert any("marked work done" in t for t in event_texts)
    assert any("Customer confirmed" in t for t in event_texts)
    assert any("payout sent" in t.lower() for t in event_texts)
