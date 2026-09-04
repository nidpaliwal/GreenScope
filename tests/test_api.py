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
    assert "GreenScope" in r.json()["message"]


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
    d = r.json()
    assert d["latitude"] == 30.2672


def test_geocode_unknown_defaults(client):
    r = client.post("/api/v1/geocode", json={"location": "Somewhere Unknown"})
    assert r.status_code == 200
    d = r.json()
    assert d["latitude"] == 37.7749


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


def test_generate_report_different_locations(client):
    recs_sf = client.post("/api/v1/generate-report", json={
        "location": {"location": "San Francisco, CA"},
    }).json()["recommendations"]

    recs_ny = client.post("/api/v1/generate-report", json={
        "location": {"location": "New York, NY"},
    }).json()["recommendations"]

    sf_names = {r["plant_name"] for r in recs_sf}
    ny_names = {r["plant_name"] for r in recs_ny}

    assert sf_names != ny_names or recs_sf[0]["confidence_tp_percent"] != recs_ny[0]["confidence_tp_percent"], \
        "Different locations should produce different recommendations"


def test_recommendation_has_score_breakdown(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Austin, TX"},
    })
    d = r.json()
    rec = d["recommendations"][0]
    assert "score_breakdown" in rec
    bd = rec["score_breakdown"]
    assert "climate" in bd
    assert "ph" in bd
    assert "sunlight" in bd
    assert "water" in bd
    assert "soil" in bd


def test_suitability_score_range(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Denver, CO"},
    })
    d = r.json()
    for rec in d["recommendations"]:
        assert 0 <= rec["confidence_tp_percent"] <= 100


def test_environment_data_present(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Seattle, WA"},
    })
    d = r.json()
    env = d["environment"]
    assert "avg_temp_c" in env
    assert "rainfall_mm" in env
    assert "ph" in env
    assert "sunlight_hours" in env
    assert "soil_type" in env


def test_num_recommendations_respected(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "San Francisco, CA"},
        "num_recommendations": 2
    })
    d = r.json()
    assert len(d["recommendations"]) == 2


def test_num_recommendations_max(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Austin, TX"},
        "num_recommendations": 10
    })
    d = r.json()
    assert len(d["recommendations"]) <= 10


def test_404_unknown_report(client):
    r = client.get("/api/v1/reports/nonexistent-id")
    assert r.status_code == 404


def test_report_persists_and_retrievable(client):
    r = client.post("/api/v1/generate-report", json={
        "location": {"location": "Denver, CO"},
    })
    report_id = r.json()["report_id"]
    r2 = client.get(f"/api/v1/reports/{report_id}")
    assert r2.status_code == 200
    assert r2.json()["report_id"] == report_id
