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
        "photo": {"image_id": "test_1", "filename": "garden_photo.jpg", "base64": "abc123"},
    })
    assert r.status_code == 200
    d = r.json()
    assert d["photo_analysis"] is not None
    assert len(d["photo_analysis"]["features"]) > 0


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
    assert pa["shade_level"] == "full"


def test_photo_analysis_with_garden_filename(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Delhi, NCR"},
        "photo": {"image_id": "t2", "filename": "my_garden_green.jpg"},
    })
    pa = r.json()["photo_analysis"]
    assert pa is not None
    assert any("vegetation" in f.lower() for f in pa["features"])


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
