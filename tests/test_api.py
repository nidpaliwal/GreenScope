"""Pytest tests for GreenScope API."""

import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


# --- Health & Root ---

def test_root_endpoint(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "GreenScope" in r.text


def test_health_check(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


# --- Geocode ---

def test_geocode_mumbai(client):
    r = client.post("/api/v1/geocode", json={"location": "Mumbai, Maharashtra"})
    assert r.status_code == 200
    d = r.json()
    assert abs(d["latitude"] - 19.08) < 0.5
    assert abs(d["longitude"] - 72.88) < 0.5


def test_geocode_delhi(client):
    r = client.post("/api/v1/geocode", json={"location": "Delhi, NCR"})
    assert r.status_code == 200
    d = r.json()
    assert abs(d["latitude"] - 28.61) < 0.5
    assert abs(d["longitude"] - 77.21) < 0.5


def test_geocode_bengaluru(client):
    r = client.post("/api/v1/geocode", json={"location": "Bengaluru, Karnataka"})
    assert r.status_code == 200
    assert abs(r.json()["latitude"] - 12.97) < 0.5


def test_geocode_chennai(client):
    r = client.post("/api/v1/geocode", json={"location": "Chennai, Tamil Nadu"})
    assert r.status_code == 200
    assert abs(r.json()["latitude"] - 13.08) < 0.5


def test_geocode_kolkata(client):
    r = client.post("/api/v1/geocode", json={"location": "Kolkata, West Bengal"})
    assert r.status_code == 200
    assert abs(r.json()["latitude"] - 22.57) < 0.5


def test_geocode_unknown_defaults(client):
    r = client.post("/api/v1/geocode", json={"location": "Somewhere Unknown"})
    assert r.status_code == 200
    d = r.json()
    assert "latitude" in d
    assert "longitude" in d


# --- Report Generation ---

def test_generate_report_basic(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Mumbai, Maharashtra"},
    })
    assert r.status_code == 200
    d = r.json()
    assert "report_id" in d
    assert "recommendations" in d
    assert len(d["recommendations"]) > 0
    assert "environment" in d
    assert "latitude" in d
    assert "longitude" in d


def test_generate_report_with_photo(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "test_1", "filename": "garden_photo.jpg", "base64": "aGVsbG8="},
    })
    assert r.status_code == 200
    d = r.json()
    assert d["photo_analysis"] is not None
    pa = d["photo_analysis"]
    features = pa.get("features", pa.get("detected_features", []))
    assert len(features) > 0


def test_generate_report_different_locations(client):
    mumbai = client.post("/api/v1/generate-report", json={"location": {"location": "Mumbai, Maharashtra"}}).json()
    delhi = client.post("/api/v1/generate-report", json={"location": {"location": "Delhi, NCR"}}).json()
    bengaluru = client.post("/api/v1/generate-report", json={"location": {"location": "Bengaluru, Karnataka"}}).json()
    jaipur = client.post("/api/v1/generate-report", json={"location": {"location": "Jaipur, Rajasthan"}}).json()

    mumbai_top = mumbai["recommendations"][0]["plant_name"]
    delhi_top = delhi["recommendations"][0]["plant_name"]
    bengaluru_top = bengaluru["recommendations"][0]["plant_name"]

    scores = [mumbai["recommendations"][0]["suitability_score"],
              delhi["recommendations"][0]["suitability_score"],
              bengaluru["recommendations"][0]["suitability_score"]]

    assert len(set(scores)) > 1 or len({mumbai_top, delhi_top, bengaluru_top}) > 1, \
        "Different locations should produce meaningfully different results"


def test_recommendation_has_score_breakdown(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Delhi, NCR"}})
    rec = r.json()["recommendations"][0]
    assert "score_breakdown" in rec
    bd = rec["score_breakdown"]
    for key in ["climate", "ph", "sunlight", "water", "soil"]:
        assert key in bd
        assert 0 <= bd[key] <= 100


def test_suitability_score_range(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Jaipur, Rajasthan"}})
    for rec in r.json()["recommendations"]:
        assert 0 <= rec["suitability_score"] <= 100


def test_environment_data_complete(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Chennai, Tamil Nadu"}})
    env = r.json()["environment"]
    for key in ["avg_temp_c", "rainfall_mm", "ph", "sunlight_hours", "soil_type", "frost_risk", "agro_zone"]:
        assert key in env


def test_num_recommendations_respected(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Mumbai, Maharashtra"}, "num_recommendations": 2})
    assert len(r.json()["recommendations"]) == 2


def test_num_recommendations_max(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Delhi, NCR"}, "num_recommendations": 20})
    assert len(r.json()["recommendations"]) <= 20


def test_404_unknown_report(client):
    r = client.get("/api/v1/reports/nonexistent-id")
    assert r.status_code == 404


def test_report_persists_and_retrievable(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Kolkata, West Bengal"}})
    report_id = r.json()["report_id"]
    r2 = client.get(f"/api/v1/reports/{report_id}")
    assert r2.status_code == 200
    assert r2.json()["report_id"] == report_id


# --- Categories ---

def test_categories_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Hyderabad, Telangana"}})
    cats = r.json()["categories"]
    assert "best_overall" in cats
    assert isinstance(cats["best_overall"], str)


def test_best_overall_matches_top_recommendation(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Mumbai, Maharashtra"}})
    d = r.json()
    assert d["categories"]["best_overall"] == d["recommendations"][0]["plant_name"]


def test_fastest_harvest_exists(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Pune, Maharashtra"}})
    cats = r.json()["categories"]
    assert "fastest_harvest" in cats


# --- Garden Risk ---

def test_garden_risk_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Jaipur, Rajasthan"}})
    risk = r.json()["garden_risk"]
    assert "sunlight" in risk
    assert "water" in risk
    assert "temperature" in risk
    assert "soil" in risk
    assert "overall" in risk
    assert "warnings" in risk
    assert isinstance(risk["warnings"], list)


def test_garden_risk_values_valid(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Shimla, Himachal Pradesh"}})
    risk = r.json()["garden_risk"]
    for field in ["sunlight", "water", "temperature", "soil"]:
        assert risk[field] in ["GOOD", "LOW", "MODERATE", "HIGH", "COLD", "HOT", "EXTREME pH"]
    assert risk["overall"] in ["LOW", "MODERATE", "HIGH"]


# --- Comparison Table ---

def test_comparison_table_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Ahmedabad, Gujarat"}})
    ct = r.json()["comparison_table"]
    assert isinstance(ct, list)
    assert len(ct) > 0
    for entry in ct:
        assert "name" in entry
        assert "suitability" in entry
        assert "water" in entry
        assert "sun" in entry
        assert "growth" in entry


def test_comparison_table_sorted(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Lucknow, Uttar Pradesh"}})
    ct = r.json()["comparison_table"]
    scores = [e["suitability"] for e in ct]
    assert scores == sorted(scores, reverse=True), "Comparison table should be sorted by suitability"


# --- Rejected Plants ---

def test_rejected_plants_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Bhopal, Madhya Pradesh"}, "num_recommendations": 3})
    rejected = r.json()["rejected_plants"]
    assert isinstance(rejected, list)
    assert len(rejected) > 0


def test_rejected_plant_structure(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Patna, Bihar"}, "num_recommendations": 2})
    rejected = r.json()["rejected_plants"]
    for rp in rejected:
        assert "plant_name" in rp
        assert "suitability" in rp
        assert "reasons" in rp
        assert isinstance(rp["reasons"], list)
        assert len(rp["reasons"]) > 0


# --- Recommendation Metadata ---

def test_recommendation_has_water_and_sun(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Srinagar, Jammu & Kashmir"}})
    for rec in r.json()["recommendations"]:
        assert "water_requirement" in rec
        assert "sun_requirement" in rec
        assert "planting_season" in rec
        assert rec["water_requirement"] in ["low", "medium", "high"]
        assert rec["sun_requirement"] in ["full", "partial"]


# --- Photo Analysis ---

def test_photo_analysis_with_shade_filename(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Mumbai, Maharashtra"},
        "photo": {"image_id": "t1", "filename": "dark_shade_garden.jpg"},
    })
    pa = r.json()["photo_analysis"]
    assert pa is not None
    assert pa.get("shade_level") in ("full", "partial")


def test_photo_analysis_with_garden_filename(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "t2", "filename": "my_garden_green.jpg"},
    })
    pa = r.json()["photo_analysis"]
    assert pa is not None
    features = pa.get("features", pa.get("detected_features", []))
    assert len(features) > 0


def test_no_photo_returns_null_analysis(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Thiruvananthapuram, Kerala"},
    })
    assert r.json()["photo_analysis"] is None


# --- Plant This Month ---

def test_plant_this_month_basic(client):
    r = client.get("/api/v1/plant-this-month?location=Delhi%2C%20NCR")
    assert r.status_code == 200
    d = r.json()
    assert "current_month" in d
    assert "season" in d
    assert "plants" in d
    assert len(d["plants"]) > 0


def test_plant_this_month_has_suitability(client):
    r = client.get("/api/v1/plant-this-month?location=Mumbai%2C%20Maharashtra")
    d = r.json()
    for plant in d["plants"]:
        assert "suitability" in plant
        assert 0 <= plant["suitability"] <= 100
        assert "best_planting_window" in plant
        assert "days_to_harvest" in plant


def test_plant_this_month_different_locations(client):
    mumbai = client.get("/api/v1/plant-this-month?location=Mumbai%2C%20Maharashtra").json()
    delhi = client.get("/api/v1/plant-this-month?location=Delhi%2C%20NCR").json()
    mumbai_scores = [p["suitability"] for p in mumbai["plants"]]
    delhi_scores = [p["suitability"] for p in delhi["plants"]]
    assert len(mumbai_scores) > 0
    assert len(delhi_scores) > 0


def test_plant_this_month_sorted_by_suitability(client):
    r = client.get("/api/v1/plant-this-month?location=Bengaluru%2C%20Karnataka")
    plants = r.json()["plants"]
    scores = [p["suitability"] for p in plants]
    assert scores == sorted(scores, reverse=True)


def test_plant_this_month_season_present(client):
    r = client.get("/api/v1/plant-this-month?location=Chennai%2C%20Tamil%20Nadu")
    d = r.json()
    assert d["season"] in ["kharif", "rabi", "zaid"]
    assert d["current_month"] in ["January", "February", "March", "April", "May", "June",
                                   "July", "August", "September", "October", "November", "December"]


# --- Report Page ---

def test_report_page_serves_html(client):
    r = client.get("/report/test-id")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "GreenScope" in r.text


def test_report_page_has_fetch_script(client):
    r = client.get("/report/test-id")
    assert "loadReport" in r.text
    assert "api/v1/reports/" in r.text


def test_homepage_has_plant_detail_links(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "plantDetail(" in r.text
    assert "plantModal" in r.text
    assert "wikipedia.org/wiki" in r.text


def test_report_page_has_plant_links(client):
    r = client.get("/report/test-id")
    assert "wikiLink(" in r.text
    assert "wikipedia.org/wiki" in r.text


# --- Scoring Weights ---

def test_scoring_weights_sum_to_one():
    from app.main import SCORING_WEIGHTS
    assert set(SCORING_WEIGHTS) == {"climate", "ph", "sunlight", "water", "soil", "photo"}
    assert abs(sum(SCORING_WEIGHTS.values()) - 1.0) < 1e-9


# --- Edge Cases ---

def test_ocean_coordinates_report(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Atlantic Ocean", "latitude": 0.0, "longitude": -20.0},
        "num_recommendations": 3,
    })
    assert r.status_code == 200
    d = r.json()
    assert len(d["recommendations"]) == 3
    assert all(0 <= x["suitability_score"] <= 100 for x in d["recommendations"])


def _solid_png_b64(rgb):
    import base64
    from io import BytesIO
    from PIL import Image
    img = Image.new("RGB", (10, 10), rgb)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_pure_black_photo(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "black", "filename": "black.png", "base64": _solid_png_b64((0, 0, 0))},
    })
    assert r.status_code == 200
    pa = r.json()["photo_analysis"]
    assert pa is not None
    assert pa["shade_level"] == "full"


def test_pure_white_photo(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "white", "filename": "white.png", "base64": _solid_png_b64((255, 255, 255))},
    })
    assert r.status_code == 200
    pa = r.json()["photo_analysis"]
    assert pa is not None
    texts = [f["text"] if isinstance(f, dict) else f for f in pa["features"]]
    assert any("sunlight" in t.lower() for t in texts)
    assert not any("vegetation" in t.lower() for t in texts)


def test_oversized_photo_rejected(client):
    import base64
    big = base64.b64encode(b"\x00" * int(5.5 * 1024 * 1024)).decode()
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "big", "filename": "big.jpg", "base64": big},
    })
    assert r.status_code == 422


# --- Garden Bed, Tags, Estimates ---

def test_garden_bed_present(client):
    d = client.post("/api/v1/generate-report", json={
        "location": {"location": "Mumbai, Maharashtra"}, "num_recommendations": 10,
    }).json()
    bed = d["garden_bed"]
    assert bed is not None
    assert len(bed["plants"]) >= 3
    assert bed["estimated_water_l_per_week"] > 0
    assert bed["estimated_co2_kg_per_year"] > 0
    assert bed["tip"]
    assert "estimate" in bed["estimates_disclaimer"].lower()
    for rec in d["recommendations"]:
        assert isinstance(rec["tags"], list)
        assert rec["estimated_water_l_per_week"] is not None
        assert rec["estimated_co2_kg_per_year"] is not None


# --- Feedback Loop ---

def test_feedback_flow(client):
    d = client.post("/api/v1/generate-report", json={
        "location": {"location": "Pune, Maharashtra"}, "num_recommendations": 2,
    }).json()
    rid, plant = d["report_id"], d["recommendations"][0]["plant_name"]
    assert client.post("/api/v1/feedback", json={
        "report_id": rid, "plant_name": plant, "vote": "up"}).status_code == 200
    assert client.post("/api/v1/feedback", json={
        "report_id": rid, "plant_name": plant, "vote": "down"}).status_code == 200
    s = client.get("/api/v1/feedback/summary").json()
    assert s[plant]["total"] >= 2
    assert s[plant]["up"] >= 1 and s[plant]["down"] >= 1
    bad = client.post("/api/v1/feedback", json={
        "report_id": rid, "plant_name": plant, "vote": "maybe"})
    assert bad.status_code == 422


# --- ICS Export ---

def test_ics_download(client):
    d = client.post("/api/v1/generate-report", json={
        "location": {"location": "Jaipur, Rajasthan"}, "num_recommendations": 3,
    }).json()
    r = client.get(f"/api/v1/reports/{d['report_id']}/calendar.ics")
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    body = r.text
    assert "BEGIN:VCALENDAR" in body
    assert "END:VCALENDAR" in body
    assert body.count("BEGIN:VEVENT") >= 3
    assert client.get("/api/v1/reports/nope/calendar.ics").status_code == 404
    # Events are dated to sowing seasons, not sequential placeholder days
    import re
    from datetime import date
    months = {m.group(1)[4:6] for m in re.finditer(r"DTSTART;VALUE=DATE:(\d{8})", body)}
    assert months <= {"06", "10", f"{date.today().month:02d}"}
    assert "season" in body.lower()


# --- Rate Limiter Wired ---

def test_rate_limiter_registered():
    from app.main import app as _app
    assert hasattr(_app.state, "limiter")


def test_rate_limit_returns_friendly_429(client):
    last = None
    for _ in range(65):
        last = client.post("/api/v1/feedback", json={
            "report_id": "loadtest", "plant_name": "Tulsi", "vote": "up"})
    assert last.status_code == 429
    assert "Too many requests" in last.json()["detail"]
