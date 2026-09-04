"""Pytest tests for GreenScope API."""

import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    """Test client for FastAPI app."""
    return TestClient(app)


def test_root_endpoint(client):
    """Root endpoint returns health check response."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert data["message"] == "GreenScope API is running"


def test_geocode_san_francisco(client):
    """Geocode returns correct coords for San Francisco."""
    response = client.post(
        "/api/v1/geocode",
        json={"location": "San Francisco, CA"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["latitude"] == 37.7749
    assert data["longitude"] == -122.4194


def test_geocode_new_york(client):
    """Geocode returns correct coords for New York."""
    response = client.post(
        "/api/v1/geocode",
        json={"location": "New York, NY"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["latitude"] == 40.7128
    assert data["longitude"] == -74.0060


def test_geocode_austin(client):
    """Geocode returns correct coords for Austin."""
    response = client.post(
        "/api/v1/geocode",
        json={"location": "Austin, TX"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["latitude"] == 30.2672
    assert data["longitude"] == -97.7431


def test_geocode_unknown_location(client):
    """Geocode defaults to SF coords for unknown location."""
    response = client.post(
        "/api/v1/geocode",
        json={"location": "Somewhere Unknown"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["latitude"] == 37.7749
    assert data["longitude"] == -122.4194


def test_generate_report_basic(client):
    """Basic report generation returns 200 with recommendations."""
    request = {
        "location": {"location": "San Francisco, CA"}
    }
    response = client.post("/api/v1/generate-report", json=request)
    assert response.status_code == 200
    data = response.json()
    assert "report_id" in data
    assert "location" in data
    assert "recommendations" in data
    assert len(data["recommendations"]) > 0
    # Check recommendation structure
    rec = data["recommendations"][0]
    assert "plant_name" in rec
    assert "confidence_tp_percent" in rec
    assert "reasoning" in rec


def test_generate_report_with_photo(client):
    """Report generation with photo included works."""
    request = {
        "location": {"location": "Austin, TX"},
        "photo": {"image_id": "temp_1", "filename": "test.jpg", "base64": "fake_base64"}
    }
    response = client.post("/api/v1/generate-report", json=request)
    assert response.status_code == 200
    data = response.json()
    assert data["report_id"]


def test_404_unknown_report(client):
    """404 returned for unknown report ID."""
    response = client.get("/api/v1/reports/nonexistent-id")
    assert response.status_code == 404


def test_num_recommendations_bounds(client):
    """num_recommendations respects min/max bounds."""
    # Test minimum (1)
    request = {"location": {"location": "San Francisco, CA"}}
    response = client.post("/api/v1/generate-report", json=request)
    assert response.status_code == 200
    data = response.json()
    assert len(data["recommendations"]) >= 1

    # Test maximum (10)
    request = {"location": {"location": "San Francisco, CA"}, "num_recommendations": 10}
    response = client.post("/api/v1/generate-report", json=request)
    assert response.status_code == 200
    data = response.json()
    assert len(data["recommendations"]) <= 10


def test_report_recommendation_has_required_fields(client):
    """Each recommendation has all required fields."""
    request = {"location": {"location": "Seattle, WA"}}
    response = client.post("/api/v1/generate-report", json=request)
    assert response.status_code == 200
    data = response.json()
    for rec in data["recommendations"]:
        assert "plant_name" in rec
        assert "reasoning" in rec
        assert "confidence_tp_percent" in rec
        assert 0 <= rec["confidence_tp_percent"] <= 100
        assert "care_guide" in rec