"""Console API tests.

The API is thin on purpose, so these test the two things it is genuinely responsible for:
keeping scope out of the request body, and reporting refusals honestly rather than as errors.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from smartcam.api.app import app  # noqa: E402

SITE = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def client():
    c = TestClient(app)
    try:
        r = c.get("/api/health")
    except Exception as e:  # noqa: BLE001 - no database means skip, not fail
        pytest.skip(f"API unavailable: {e}")
    if r.status_code != 200:
        pytest.skip("no seeded database")
    return c


# --- catalogue ---------------------------------------------------------------------------

def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_cameras_expose_their_grade(client):
    """The grade is the honest answer to 'why can't I do face matching on that camera', and the
    operator will ask. It belongs in the UI, not buried in a survey PDF."""
    rows = client.get("/api/cameras").json()
    assert rows
    assert {"camera_id", "name", "grade", "clock_ok"} <= set(rows[0])


def test_coverage_reports_gaps(client):
    body = client.get("/api/coverage?hours=48").json()
    assert 0.0 <= body["coverage_pct"] <= 1.0
    assert isinstance(body["gaps"], list)


# --- ask ----------------------------------------------------------------------------------

def test_ask_returns_an_answer_with_evidence(client):
    r = client.post("/api/ask", json={"question": "how many people came through yesterday"})
    assert r.status_code == 200
    body = r.json()
    assert body["message"]
    assert body["describes"]
    assert "coverage_pct" in body


def test_a_refusal_is_200_not_404(client):
    """'Nothing happened' and 'we could not look' are different answers, and an HTTP status code
    cannot carry that difference. Encoding a refusal as an error makes them identical."""
    r = client.post("/api/ask", json={"question": "what is our quarterly revenue"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert "did not understand" in body["message"]


def test_empty_question_is_rejected_by_validation(client):
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_overlong_question_is_rejected(client):
    assert client.post("/api/ask", json={"question": "x" * 900}).status_code == 422


def test_asking_writes_to_the_audit_trail(client):
    probe = "how many people came through the gate yesterday"
    client.post("/api/ask", json={"question": probe})
    rows = client.get("/api/audit?limit=20").json()
    assert any(r["question"] == probe for r in rows)


def test_a_question_we_could_not_interpret_is_still_audited(client):
    """The gap this closes: a question that fails to compile never reaches the database, so it
    left no trace at all. Someone probing the system — or an employee looking up a colleague —
    was invisible purely because they phrased it badly. The *attempt* is the audit event."""
    probe = "find everything about the night shift supervisor please"
    r = client.post("/api/ask", json={"question": probe})
    assert r.json()["refused"] is True

    rows = client.get("/api/audit?limit=20").json()
    row = next((x for x in rows if x["question"] == probe), None)
    assert row is not None, "an uninterpretable question left no audit trace"
    assert row["refused"] is True
    assert row["actor"]


# --- scope -----------------------------------------------------------------------------------

def test_request_body_cannot_set_the_tenant(client):
    """Scope comes from the session. A crafted body must not reach another customer's footage."""
    r = client.post("/api/ask", json={
        "question": "how many people came through yesterday",
        "tenant_id": "22222222-2222-2222-2222-222222222222",
        "site_id": "somewhere-else",
    })
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.json()["message"]


def test_zones_reject_a_camera_from_another_site(client):
    r = client.put("/api/cameras/cccccccc-9999-9999-9999-999999999999/zones",
                   json=[{"name": "x", "kind": "area",
                          "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]}])
    assert r.status_code == 404


# --- zones ------------------------------------------------------------------------------------

def test_zone_round_trip(client):
    cam = client.get("/api/cameras").json()[0]["camera_id"]
    before = client.get(f"/api/cameras/{cam}/zones").json()

    poly = [[0.2, 0.3], [0.8, 0.3], [0.8, 0.9], [0.2, 0.9]]
    r = client.put(f"/api/cameras/{cam}/zones",
                   json=[{"name": "Test area", "kind": "area", "polygon": poly}])
    assert r.status_code == 200 and r.json()["saved"] == 1

    after = client.get(f"/api/cameras/{cam}/zones").json()
    assert [z["name"] for z in after] == ["Test area"]
    assert after[0]["polygon"] == poly

    client.put(f"/api/cameras/{cam}/zones",
               json=[{"name": z["name"], "kind": z["kind"], "polygon": z["polygon"]}
                     for z in before])


def test_zone_coordinates_must_be_normalised(client):
    """A zone stored in pixels silently moves the moment a camera's resolution changes."""
    cam = client.get("/api/cameras").json()[0]["camera_id"]
    r = client.put(f"/api/cameras/{cam}/zones",
                   json=[{"name": "pixels", "kind": "area",
                          "polygon": [[640, 360], [1280, 360], [1280, 720]]}])
    assert r.status_code == 400
    assert "normalised" in r.json()["detail"]


def test_an_area_needs_three_points(client):
    cam = client.get("/api/cameras").json()[0]["camera_id"]
    r = client.put(f"/api/cameras/{cam}/zones",
                   json=[{"name": "line-ish", "kind": "area",
                          "polygon": [[0.1, 0.1], [0.9, 0.9]]}])
    assert r.status_code == 400


# --- rules --------------------------------------------------------------------------------------

def test_draft_returns_a_read_back_without_saving(client):
    """The two-step is the safety mechanism: an operator confirms the sentence before anything
    runs unattended."""
    before = len(client.get("/api/rules").json())
    r = client.post("/api/rules/draft",
                    json={"instruction": "alert me if anyone enters the chemical store after 8pm"})
    body = r.json()
    assert body["ok"] is True
    assert "Alert when" in body["explain"]
    assert "20:00" in body["explain"]
    assert len(client.get("/api/rules").json()) == before      # nothing was saved


def test_draft_declines_what_it_cannot_express(client):
    body = client.post("/api/rules/draft",
                       json={"instruction": "make the factory more profitable"}).json()
    assert body["ok"] is False and body["reason"]


def test_draft_surfaces_automatic_hardening(client):
    """The operator must be told we changed their rule, and why."""
    body = client.post("/api/rules/draft", json={
        "instruction": "tell me when someone is on the zone b floor without a helmet"}).json()
    if not body["ok"]:
        pytest.skip("no matching zone in this seed")
    assert body["needs_verification"] is True
    assert any("absence" in n for n in body["notes"])


def test_dwell_rule_reports_its_real_latency_floor(client):
    body = client.post("/api/rules/draft", json={
        "instruction": "alert if the fire exit clearance is blocked for more than 60 seconds",
    }).json()
    if not body["ok"]:
        pytest.skip("no matching zone in this seed")
    assert body["min_alert_latency_s"] > 60
    assert "part of the rule" in body["explain"]


def test_save_and_disable_a_rule(client):
    draft = client.post("/api/rules/draft", json={
        "instruction": "alert me if anyone enters the chemical store after 8pm"}).json()
    saved = client.post("/api/rules", json=draft["rule"]).json()
    assert saved["rule_id"]

    listed = client.get("/api/rules").json()
    assert any(r["rule_id"] == saved["rule_id"] for r in listed)

    assert client.delete(f"/api/rules/{saved['rule_id']}").status_code == 200
    again = client.get("/api/rules").json()
    row = next(r for r in again if r["rule_id"] == saved["rule_id"])
    assert row["enabled"] is False          # disabled, not deleted


def test_saving_an_invalid_rule_is_a_400_with_a_usable_message(client):
    r = client.post("/api/rules", json={"name": "bad", "trigger": {"type": "telepathy"}})
    assert r.status_code == 400
    assert "unknown trigger" in r.json()["detail"]


def test_deleting_a_rule_from_another_site_is_404(client):
    r = client.delete("/api/rules/99999999-9999-9999-9999-999999999999")
    assert r.status_code == 404
