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

def test_geocode_san_francisco(client):
    r = client.post("/api/v1/geocode", json={"location": "San Francisco, CA"})
    assert r.status_code == 200
    d = r.json()
    assert d["latitude"] == 37.7749
    assert d["longitude"] == -122.4194


def test_geocode_new_york(client):
    r = client.post("/api/v1/geocode", json={"location": "New York, NY"})
    assert r.status_code == 200
    d = r.json()
    assert d["latitude"] == 40.7128
    assert d["longitude"] == -74.0060


def test_geocode_austin(client):
    r = client.post("/api/v1/geocode", json={"location": "Austin, TX"})
    assert r.status_code == 200
    assert r.json()["latitude"] == 30.2672


def test_geocode_seattle(client):
    r = client.post("/api/v1/geocode", json={"location": "Seattle, WA"})
    assert r.status_code == 200
    assert r.json()["latitude"] == 47.6062


def test_geocode_denver(client):
    r = client.post("/api/v1/geocode", json={"location": "Denver, CO"})
    assert r.status_code == 200
    assert r.json()["latitude"] == 39.7392


def test_geocode_unknown_defaults(client):
    r = client.post("/api/v1/geocode", json={"location": "Somewhere Unknown"})
    assert r.status_code == 400


# --- Report Generation ---

def test_generate_report_basic(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "San Francisco, CA"},
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
        "location": {"location": "Austin, TX"},
        "photo": {"image_id": "test_1", "filename": "garden_photo.jpg", "base64": "abc123"},
    })
    assert r.status_code == 200
    d = r.json()
    assert d["photo_analysis"] is not None
    assert len(d["photo_analysis"]["features"]) > 0


def test_generate_report_different_locations(client):
    sf = client.post("/api/v1/generate-report", json={"location": {"location": "San Francisco, CA"}}).json()
    ny = client.post("/api/v1/generate-report", json={"location": {"location": "New York, NY"}}).json()
    austin = client.post("/api/v1/generate-report", json={"location": {"location": "Austin, TX"}}).json()
    denver = client.post("/api/v1/generate-report", json={"location": {"location": "Denver, CO"}}).json()

    sf_top = sf["recommendations"][0]["plant_name"]
    ny_top = ny["recommendations"][0]["plant_name"]
    austin_top = austin["recommendations"][0]["plant_name"]

    scores = [sf["recommendations"][0]["suitability_score"],
              ny["recommendations"][0]["suitability_score"],
              austin["recommendations"][0]["suitability_score"]]

    assert len(set(scores)) > 1 or len({sf_top, ny_top, austin_top}) > 1, \
        "Different locations should produce meaningfully different results"


def test_recommendation_has_score_breakdown(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Austin, TX"}})
    rec = r.json()["recommendations"][0]
    assert "score_breakdown" in rec
    bd = rec["score_breakdown"]
    for key in ["climate", "ph", "sunlight", "water", "soil"]:
        assert key in bd
        assert 0 <= bd[key] <= 100


def test_suitability_score_range(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Denver, CO"}})
    for rec in r.json()["recommendations"]:
        assert 0 <= rec["suitability_score"] <= 100


def test_environment_data_complete(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Seattle, WA"}})
    env = r.json()["environment"]
    for key in ["avg_temp_c", "rainfall_mm", "ph", "sunlight_hours", "soil_type", "frost_risk", "usda_zone"]:
        assert key in env


def test_num_recommendations_respected(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "San Francisco, CA"}, "num_recommendations": 2})
    assert len(r.json()["recommendations"]) == 2


def test_num_recommendations_max(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Austin, TX"}, "num_recommendations": 10})
    assert len(r.json()["recommendations"]) <= 10


def test_404_unknown_report(client):
    r = client.get("/api/v1/reports/nonexistent-id")
    assert r.status_code == 404


def test_report_persists_and_retrievable(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Denver, CO"}})
    report_id = r.json()["report_id"]
    r2 = client.get(f"/api/v1/reports/{report_id}")
    assert r2.status_code == 200
    assert r2.json()["report_id"] == report_id


# --- Categories ---

def test_categories_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Austin, TX"}})
    cats = r.json()["categories"]
    assert "best_overall" in cats
    assert isinstance(cats["best_overall"], str)


def test_best_overall_matches_top_recommendation(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "San Francisco, CA"}})
    d = r.json()
    assert d["categories"]["best_overall"] == d["recommendations"][0]["plant_name"]


def test_fastest_harvest_exists(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "New York, NY"}})
    cats = r.json()["categories"]
    assert "fastest_harvest" in cats


# --- Garden Risk ---

def test_garden_risk_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Denver, CO"}})
    risk = r.json()["garden_risk"]
    assert "sunlight" in risk
    assert "water" in risk
    assert "temperature" in risk
    assert "soil" in risk
    assert "overall" in risk
    assert "warnings" in risk
    assert isinstance(risk["warnings"], list)


def test_garden_risk_values_valid(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Seattle, WA"}})
    risk = r.json()["garden_risk"]
    for field in ["sunlight", "water", "temperature", "soil"]:
        assert risk[field] in ["GOOD", "LOW", "MODERATE", "HIGH", "COLD", "HOT", "EXTREME pH"]
    assert risk["overall"] in ["LOW", "MODERATE", "HIGH"]


# --- Comparison Table ---

def test_comparison_table_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Austin, TX"}})
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
    r = client.post("/api/v1/generate-report", json={"location": {"location": "San Francisco, CA"}})
    ct = r.json()["comparison_table"]
    scores = [e["suitability"] for e in ct]
    assert scores == sorted(scores, reverse=True), "Comparison table should be sorted by suitability"


# --- Rejected Plants ---

def test_rejected_plants_present(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Denver, CO"}, "num_recommendations": 3})
    rejected = r.json()["rejected_plants"]
    assert isinstance(rejected, list)
    assert len(rejected) > 0


def test_rejected_plant_structure(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "New York, NY"}, "num_recommendations": 2})
    rejected = r.json()["rejected_plants"]
    for rp in rejected:
        assert "plant_name" in rp
        assert "suitability" in rp
        assert "reasons" in rp
        assert isinstance(rp["reasons"], list)
        assert len(rp["reasons"]) > 0


# --- Recommendation Metadata ---

def test_recommendation_has_water_and_sun(client):
    r = client.post("/api/v1/generate-report", json={"location": {"location": "Seattle, WA"}})
    for rec in r.json()["recommendations"]:
        assert "water_requirement" in rec
        assert "sun_requirement" in rec
        assert "planting_season" in rec
        assert rec["water_requirement"] in ["low", "medium", "high"]
        assert rec["sun_requirement"] in ["full", "partial"]


# --- Photo Analysis ---

def test_photo_analysis_with_shade_filename(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "San Francisco, CA"},
        "photo": {"image_id": "t1", "filename": "dark_shade_garden.jpg"},
    })
    pa = r.json()["photo_analysis"]
    assert pa is not None
    assert pa["shade_level"] == "full"


def test_photo_analysis_with_garden_filename(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Austin, TX"},
        "photo": {"image_id": "t2", "filename": "my_garden_green.jpg"},
    })
    pa = r.json()["photo_analysis"]
    assert pa is not None
    assert any("vegetation" in f.lower() for f in pa["features"])


def test_no_photo_returns_null_analysis(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Denver, CO"},
    })
    assert r.json()["photo_analysis"] is None


# --- Plant This Month ---

def test_plant_this_month_basic(client):
    r = client.get("/api/v1/plant-this-month?location=Austin%2C%20TX")
    assert r.status_code == 200
    d = r.json()
    assert "current_month" in d
    assert "season" in d
    assert "plants" in d
    assert len(d["plants"]) > 0


def test_plant_this_month_has_suitability(client):
    r = client.get("/api/v1/plant-this-month?location=San%20Francisco%2C%20CA")
    d = r.json()
    for plant in d["plants"]:
        assert "suitability" in plant
        assert 0 <= plant["suitability"] <= 100
        assert "best_planting_window" in plant
        assert "days_to_harvest" in plant


def test_plant_this_month_different_locations(client):
    sf = client.get("/api/v1/plant-this-month?location=San%20Francisco%2C%20CA").json()
    denver = client.get("/api/v1/plant-this-month?location=Denver%2C%20CO").json()
    sf_names = {p["plant_name"] for p in sf["plants"]}
    denver_names = {p["plant_name"] for p in denver["plants"]}
    sf_scores = [p["suitability"] for p in sf["plants"]]
    denver_scores = [p["suitability"] for p in denver["plants"]]
    assert sf_scores != denver_scores or sf_names != denver_names


def test_plant_this_month_sorted_by_suitability(client):
    r = client.get("/api/v1/plant-this-month?location=New%20York%2C%20NY")
    plants = r.json()["plants"]
    scores = [p["suitability"] for p in plants]
    assert scores == sorted(scores, reverse=True)


def test_plant_this_month_season_present(client):
    r = client.get("/api/v1/plant-this-month?location=Seattle%2C%20WA")
    d = r.json()
    assert d["season"] in ["spring", "summer", "fall", "winter"]
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
