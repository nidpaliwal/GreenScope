from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from functools import lru_cache
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from fastapi.responses import JSONResponse
import uuid
import time
import json
import os
import sqlite3
import base64
import httpx
from datetime import date, timedelta


# --- Plant Knowledge Base ---

def load_plant_db():
    db_path = os.path.join(os.path.dirname(__file__), "plant_db.json")
    with open(db_path, "r") as f:
        return json.load(f)["plants"]


PLANTS = load_plant_db()

# --- Scoring weights (single source of truth; must sum to 1.0) ---
SCORING_WEIGHTS = {
    "climate": 0.25,
    "ph": 0.20,
    "sunlight": 0.15,
    "water": 0.15,
    "soil": 0.15,
    "photo": 0.10,
}

# --- Static plant metadata (no new data source needed) ---
# NASA Clean Air Study / well-documented air-purifying houseplants in our DB
AIR_PURIFYING_PLANTS = {
    "Aloe Vera", "Neem", "Tulsi", "Money Plant", "Areca Palm", "Snake Plant",
    "Spider Plant", "Peace Lily", "Gerbera Daisy", "Marigold", "Mint",
}
# Flowering / nectar plants known to attract pollinators
POLLINATOR_PLANTS = {
    "Marigold", "Sunflower", "Hibiscus", "Jasmine", "Rose", "Lavender",
    "Mustard", "Coriander", "Tulsi", "Lemongrass", "Lotus",
}
# Well-documented companion pairs (tomato+marigold etc.)
COMPANIONS = {
    "Tomato": (["Marigold", "Tulsi", "Garlic"], ["Potato"]),
    "Marigold": (["Tomato", "Brinjal", "Chili"], []),
    "Tulsi": (["Tomato", "Chili", "Brinjal"], []),
    "Garlic": (["Tomato", "Carrot", "Rose"], ["Peas"]),
    "Onion": (["Carrot", "Beetroot"], ["Peas"]),
    "Carrot": (["Onion", "Garlic", "Tomato"], []),
    "Mint": (["Brinjal", "Cabbage"], []),
    "Coriander": (["Spinach", "Onion"], []),
    "Mustard": (["Peas", "Black Gram"], []),
    "Lemongrass": (["Tomato", "Brinjal"], []),
}
# Qualitative water need -> estimated liters/plant/week (approx, labeled as estimate)
WATER_L_PER_WEEK = {"low": 3.0, "medium": 8.0, "high": 18.0}
# Category -> rough CO2 sequestration kg/plant/year (order-of-magnitude estimate)
CO2_KG_PER_YEAR = {
    "fruit": 12.0, "vegetable": 1.0, "herb": 0.5,
    "spice": 0.8, "pulse": 0.8, "flower": 0.5,
}


def plant_tags(plant_name: str) -> List[str]:
    tags = []
    if plant_name in AIR_PURIFYING_PLANTS:
        tags.append("air-purifying")
    if plant_name in POLLINATOR_PLANTS:
        tags.append("pollinator-friendly")
    if plant_name in COMPANIONS:
        grows_with, _ = COMPANIONS[plant_name]
        tags.append("companion: " + ", ".join(grows_with[:2]))
    return tags


def build_garden_bed(recommendations: list) -> dict:
    """Reframe top picks as one garden bed that grows well together.

    Picks the top 5 recommendations sharing the majority sun requirement,
    sums estimated water footprint, and surfaces one companion tip.
    All numbers are labeled estimates, not measurements.
    """
    if not recommendations:
        return {"title": "Your garden bed", "plants": [], "tip": ""}
    suns = [r.sun_requirement or "full" for r in recommendations[:8]]
    majority_sun = max(set(suns), key=suns.count)
    bed = [r for r in recommendations if (r.sun_requirement or "full") == majority_sun][:5]
    if len(bed) < 3:
        bed = recommendations[:5]
    total_water = round(sum(r.estimated_water_l_per_week or 0 for r in bed), 1)
    total_co2 = round(sum(r.estimated_co2_kg_per_year or 0 for r in bed), 1)
    tip = ""
    for r in bed:
        grows_with, avoid = COMPANIONS.get(r.plant_name, ([], []))
        bed_names = {b.plant_name for b in bed}
        together = [c for c in grows_with if c in bed_names]
        if together:
            tip = f"{r.plant_name} grows well with {', '.join(together)} in this bed."
            break
    if not tip:
        tip = f"These {len(bed)} plants share {majority_sun}-sun needs and suit this spot's climate."
    return {
        "title": f"Your {majority_sun}-sun garden bed",
        "plants": [r.plant_name for r in bed],
        "estimated_water_l_per_week": total_water,
        "estimated_co2_kg_per_year": total_co2,
        "estimates_disclaimer": "Water and CO2 figures are rough estimates from plant categories, not measurements.",
        "tip": tip,
    }

# --- SQLite Database for Report Persistence ---
# On Render free tier, disk is ephemeral — reports survive until next deploy/spin-down.
# For persistent reports, set DATABASE_URL env var to a persistent path.

def get_db():
    db_path = os.environ.get("DATABASE_URL", os.path.join(os.path.dirname(__file__), "reports.db"))
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///"):]
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            location TEXT,
            latitude REAL,
            longitude REAL,
            environment TEXT,
            photo_analysis TEXT,
            recommendations TEXT,
            generated_at REAL,
            processing_time_ms REAL,
            rejected_plants TEXT,
            categories TEXT,
            garden_risk TEXT,
            comparison_table TEXT,
            garden_bed TEXT,
            data_source TEXT
        )"""
    )
    # Migrate older DBs that lack the newer columns
    for col in ("rejected_plants", "categories", "garden_risk",
                "comparison_table", "garden_bed", "data_source"):
        try:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            plant_name TEXT,
            vote TEXT CHECK(vote IN ('up', 'down')),
            created_at REAL
        )"""
    )
    conn.commit()
    return conn


def save_report_to_db(report):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO reports
        (report_id, location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms,
         rejected_plants, categories, garden_risk, comparison_table, garden_bed, data_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            report.report_id,
            report.location,
            report.latitude,
            report.longitude,
            json.dumps(report.environment),
            json.dumps(report.photo_analysis) if report.photo_analysis else None,
            json.dumps([{
                "plant_name": r.plant_name,
                "scientific_name": r.scientific_name,
                "reasoning": r.reasoning,
                "suitability_score": r.suitability_score,
                "care_guide": r.care_guide,
                "growth_duration": r.growth_duration,
                "score_breakdown": r.score_breakdown,
                "water_requirement": r.water_requirement,
                "sun_requirement": r.sun_requirement,
                "planting_season": r.planting_season,
                "tags": r.tags,
                "estimated_water_l_per_week": r.estimated_water_l_per_week,
                "estimated_co2_kg_per_year": r.estimated_co2_kg_per_year,
            } for r in report.recommendations]),
            report.generated_at,
            report.processing_time_ms,
            json.dumps(report.rejected_plants) if report.rejected_plants else None,
            json.dumps(report.categories) if report.categories else None,
            report.garden_risk.model_dump_json() if report.garden_risk else None,
            json.dumps(report.comparison_table) if report.comparison_table else None,
            json.dumps(report.garden_bed) if report.garden_bed else None,
            report.data_source,
        ),
    )
    conn.commit()
    conn.close()


def load_report_from_db(report_id):
    conn = get_db()
    row = conn.execute(
        "SELECT location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms,"
        " rejected_plants, categories, garden_risk, comparison_table, garden_bed, data_source"
        " FROM reports WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "location": row[0],
        "latitude": row[1],
        "longitude": row[2],
        "environment": json.loads(row[3]),
        "photo_analysis": json.loads(row[4]) if row[4] else None,
        "recommendations": json.loads(row[5]),
        "generated_at": row[6],
        "processing_time_ms": row[7],
        "rejected_plants": json.loads(row[8]) if len(row) > 8 and row[8] else None,
        "categories": json.loads(row[9]) if len(row) > 9 and row[9] else None,
        "garden_risk": json.loads(row[10]) if len(row) > 10 and row[10] else None,
        "comparison_table": json.loads(row[11]) if len(row) > 11 and row[11] else None,
        "garden_bed": json.loads(row[12]) if len(row) > 12 and row[12] else None,
        "data_source": row[13] if len(row) > 13 else "fallback",
    }


# --- Location Environmental Data (Indian Cities - IMD Climate Normals) ---

LOCATION_ENV = {
    "mumbai, maharashtra": {
        "avg_temp_c": 27, "rainfall_mm": 2200, "humidity": 73,
        "sunlight_hours": 7, "soil_type": "alluvial", "ph": 6.5,
        "frost_risk": "none", "agro_zone": "Western Coast",
    },
    "delhi, ncr": {
        "avg_temp_c": 25, "rainfall_mm": 800, "humidity": 55,
        "sunlight_hours": 7.5, "soil_type": "alluvial", "ph": 7.0,
        "frost_risk": "low", "agro_zone": "Indo-Gangetic Plains",
    },
    "bengaluru, karnataka": {
        "avg_temp_c": 24, "rainfall_mm": 970, "humidity": 65,
        "sunlight_hours": 6, "soil_type": "red laterite", "ph": 6.0,
        "frost_risk": "none", "agro_zone": "Deccan Plateau",
    },
    "chennai, tamil nadu": {
        "avg_temp_c": 29, "rainfall_mm": 1400, "humidity": 70,
        "sunlight_hours": 6.5, "soil_type": "coastal alluvial", "ph": 6.5,
        "frost_risk": "none", "agro_zone": "Coromandel Coast",
    },
    "kolkata, west bengal": {
        "avg_temp_c": 26, "rainfall_mm": 1600, "humidity": 72,
        "sunlight_hours": 6, "soil_type": "alluvial", "ph": 6.5,
        "frost_risk": "none", "agro_zone": "Gangetic Delta",
    },
    "hyderabad, telangana": {
        "avg_temp_c": 27, "rainfall_mm": 810, "humidity": 58,
        "sunlight_hours": 7, "soil_type": "black cotton", "ph": 7.5,
        "frost_risk": "none", "agro_zone": "Deccan Plateau",
    },
    "pune, maharashtra": {
        "avg_temp_c": 26, "rainfall_mm": 720, "humidity": 55,
        "sunlight_hours": 7, "soil_type": "black cotton", "ph": 7.0,
        "frost_risk": "none", "agro_zone": "Western Deccan",
    },
    "jaipur, rajasthan": {
        "avg_temp_c": 26, "rainfall_mm": 650, "humidity": 45,
        "sunlight_hours": 8, "soil_type": "sandy loam", "ph": 7.5,
        "frost_risk": "low", "agro_zone": "Thar Desert Fringe",
    },
    "ahmedabad, gujarat": {
        "avg_temp_c": 28, "rainfall_mm": 800, "humidity": 40,
        "sunlight_hours": 8.5, "soil_type": "alluvial", "ph": 7.5,
        "frost_risk": "none", "agro_zone": "Semi-Arid Western",
    },
    "lucknow, uttar pradesh": {
        "avg_temp_c": 25, "rainfall_mm": 900, "humidity": 60,
        "sunlight_hours": 7, "soil_type": "alluvial", "ph": 7.0,
        "frost_risk": "low", "agro_zone": "Indo-Gangetic Plains",
    },
    "bhopal, madhya pradesh": {
        "avg_temp_c": 25, "rainfall_mm": 1100, "humidity": 55,
        "sunlight_hours": 7, "soil_type": "black cotton", "ph": 7.0,
        "frost_risk": "none", "agro_zone": "Central Highlands",
    },
    "patna, bihar": {
        "avg_temp_c": 26, "rainfall_mm": 1200, "humidity": 65,
        "sunlight_hours": 6.5, "soil_type": "alluvial", "ph": 6.5,
        "frost_risk": "low", "agro_zone": "Indo-Gangetic Plains",
    },
    "shimla, himachal pradesh": {
        "avg_temp_c": 13, "rainfall_mm": 1575, "humidity": 65,
        "sunlight_hours": 6, "soil_type": "mountain", "ph": 5.5,
        "frost_risk": "high", "agro_zone": "Western Himalayas",
    },
    "srinagar, jammu & kashmir": {
        "avg_temp_c": 13, "rainfall_mm": 730, "humidity": 60,
        "sunlight_hours": 7, "soil_type": "alluvial", "ph": 6.5,
        "frost_risk": "high", "agro_zone": "Kashmir Valley",
    },
    "thiruvananthapuram, kerala": {
        "avg_temp_c": 27, "rainfall_mm": 1800, "humidity": 78,
        "sunlight_hours": 5.5, "soil_type": "coastal laterite", "ph": 5.5,
        "frost_risk": "none", "agro_zone": "Malabar Coast",
    },
    "udaipur, rajasthan": {
        "avg_temp_c": 24, "rainfall_mm": 580, "humidity": 42,
        "sunlight_hours": 8.5, "soil_type": "sandy loam", "ph": 7.5,
        "frost_risk": "low", "agro_zone": "Arid Western",
    },
}

DEFAULT_ENV = {
    "avg_temp_c": 25, "rainfall_mm": 900, "humidity": 60,
    "sunlight_hours": 6.5, "soil_type": "loam", "ph": 6.5,
    "frost_risk": "none", "agro_zone": "General",
}


def get_env_for_location(lat: float, lng: float, location_str: str) -> tuple:
    """Returns (env_dict, data_source) where data_source is 'live_api' or 'fallback'."""
    # Try Open-Meteo Climate API first
    try:
        import httpx
        with httpx.Client(timeout=5.0) as client:
            r = client.get(
                "https://climate-api.open-meteo.com/v1/climate",
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "start_date": "2020-01-01",
                    "end_date": "2020-12-31",
                    "models": "EC_Earth3P_HR",
                    "daily": "temperature_2m_mean,precipitation_sum",
                    "timezone": "Asia/Kolkata",
                }
            )
            if r.status_code == 200:
                data = r.json().get("daily", {})
                temps = [t for t in data.get("temperature_2m_mean", []) if t is not None]
                rains = [r for r in data.get("precipitation_sum", []) if r is not None]
                if temps and rains:
                    avg_temp = round(sum(temps) / len(temps), 1)
                    total_rain = round(sum(rains))

                    # Determine soil type based on region
                    soil = "loam"
                    ph = 6.5
                    humidity = 60
                    sunlight = 7.0
                    frost = "none"
                    heat_risk = "none"

                    if lat < 15:
                        humidity = 75
                        soil = "red laterite"
                        ph = 5.5
                    elif lat > 28:
                        humidity = 50
                        frost = "low"
                    if total_rain > 1500:
                        humidity = min(85, humidity + 15)
                    elif total_rain < 500:
                        humidity = max(30, humidity - 15)
                        soil = "sandy loam"

                    # Heat risk based on average temperature
                    if avg_temp > 32:
                        heat_risk = "high"
                    elif avg_temp > 28:
                        heat_risk = "moderate"

                    # Climate alerts
                    alerts = []
                    if frost != "none":
                        alerts.append(f"Frost risk: {frost}. Protect frost-sensitive plants during winter months.")
                    if heat_risk == "high":
                        alerts.append("Extreme heat: Provide shade cloth for heat-sensitive crops. Water deeply in early morning.")
                    elif heat_risk == "moderate":
                        alerts.append("Moderate heat: Mulch heavily to retain soil moisture. Avoid midday watering.")

                    # Merge with hardcoded fallback for known cities to preserve
                    # accurate frost_risk, agro_zone, soil_type (elevation-aware)
                    loc_key = location_str.lower().strip()
                    fallback_env = None
                    for key, env in LOCATION_ENV.items():
                        if key in loc_key:
                            fallback_env = env
                            break

                    if fallback_env:
                        frost = fallback_env.get("frost_risk", frost)
                        agro_zone = fallback_env.get("agro_zone", f"Lat {lat:.1f}, Lng {lng:.1f}")
                        soil = fallback_env.get("soil_type", soil)
                        ph = fallback_env.get("ph", ph)
                        sunlight = fallback_env.get("sunlight_hours", sunlight)
                        humidity = fallback_env.get("humidity", humidity)
                        if frost != "none":
                            alerts.append(f"Frost risk: {frost}. Protect frost-sensitive plants during winter months.")
                    else:
                        agro_zone = f"Lat {lat:.1f}, Lng {lng:.1f}"

                    return {
                        "avg_temp_c": avg_temp,
                        "rainfall_mm": total_rain,
                        "humidity": humidity,
                        "sunlight_hours": sunlight,
                        "soil_type": soil,
                        "ph": ph,
                        "frost_risk": frost,
                        "heat_risk": heat_risk,
                        "climate_alerts": alerts,
                        "agro_zone": agro_zone,
                    }, "live_api"
    except Exception:
        pass

    # Fallback to hardcoded Indian cities
    loc_key = location_str.lower().strip()
    for key, env in LOCATION_ENV.items():
        if key in loc_key:
            env = env.copy()
            env.setdefault("heat_risk", "none")
            env.setdefault("climate_alerts", [])
            return env, "fallback"
    env = DEFAULT_ENV.copy()
    if lat < 12:
        env["avg_temp_c"] = 28
        env["humidity"] = 75
        env["rainfall_mm"] = 1800
        env["frost_risk"] = "none"
    elif lat > 28:
        env["avg_temp_c"] = 18
        env["frost_risk"] = "low"
        env["rainfall_mm"] = 1200
    env.setdefault("heat_risk", "none")
    env.setdefault("climate_alerts", [])
    return env, "fallback"


# --- Scoring Engine ---

def score_plant(plant: dict, env: dict, photo_obs: dict) -> dict:
    scores = {}
    reasons = []

    # Temperature score (25%)
    t_min = plant["temperature_min"]
    t_max = plant["temperature_max"]
    t_avg = env["avg_temp_c"]
    if t_min <= t_avg <= t_max:
        t_mid = (t_min + t_max) / 2
        t_range = (t_max - t_min) / 2
        scores["climate"] = max(0, 100 - abs(t_avg - t_mid) / t_range * 50)
        reasons.append({"category": "Climate", "score": scores["climate"],
                        "detail": f"Temperature {t_avg}C fits {t_min}-{t_max}C range"})
    else:
        if t_avg < t_min:
            scores["climate"] = max(0, 100 - (t_min - t_avg) * 8)
            reasons.append({"category": "Climate", "score": scores["climate"],
                            "detail": f"Too cold: {t_avg}C vs minimum {t_min}C"})
        else:
            scores["climate"] = max(0, 100 - (t_avg - t_max) * 8)
            reasons.append({"category": "Climate", "score": scores["climate"],
                            "detail": f"Too hot: {t_avg}C vs maximum {t_max}C"})

    # pH score (20%)
    p_min = plant["min_ph"]
    p_max = plant["max_ph"]
    p_avg = env["ph"]
    if p_min <= p_avg <= p_max:
        scores["ph"] = 90 + 10 * (1 - abs(p_avg - (p_min + p_max) / 2) / ((p_max - p_min) / 2 + 0.1))
        reasons.append({"category": "pH", "score": scores["ph"],
                        "detail": f"Soil pH {p_avg} is ideal (range {p_min}-{p_max})"})
    else:
        diff = min(abs(p_avg - p_min), abs(p_avg - p_max))
        scores["ph"] = max(0, 100 - diff * 30)
        reasons.append({"category": "pH", "score": scores["ph"],
                        "detail": f"Soil pH {p_avg} is outside ideal {p_min}-{p_max}"})

    # Sunlight score (15%)
    sun_req = plant["sun"]
    sun_hrs = env["sunlight_hours"]
    if sun_req == "full":
        if sun_hrs >= 6:
            scores["sunlight"] = 85 + min(15, (sun_hrs - 6) * 5)
            reasons.append({"category": "Sunlight", "score": scores["sunlight"],
                            "detail": f"Full sun met ({sun_hrs}h available)"})
        else:
            scores["sunlight"] = max(0, 100 - (6 - sun_hrs) * 20)
            reasons.append({"category": "Sunlight", "score": scores["sunlight"],
                            "detail": f"Needs full sun but only {sun_hrs}h available"})
    else:
        if 3 <= sun_hrs <= 6:
            scores["sunlight"] = 85 + min(15, (6 - abs(sun_hrs - 4.5)) * 3)
            reasons.append({"category": "Sunlight", "score": scores["sunlight"],
                            "detail": f"Partial shade tolerated ({sun_hrs}h)"})
        else:
            scores["sunlight"] = max(0, 100 - abs(sun_hrs - 4.5) * 15)
            reasons.append({"category": "Sunlight", "score": scores["sunlight"],
                            "detail": f"Light conditions not ideal ({sun_hrs}h)"})

    # Water score (15%)
    water_req = plant["water"]
    rainfall = env["rainfall_mm"]
    if water_req == "low":
        if rainfall < 600:
            scores["water"] = 90
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": "Low water needs match dry climate"})
        elif rainfall < 1000:
            scores["water"] = 70
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": "Moderate rainfall; occasional watering needed"})
        else:
            scores["water"] = 50
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": "High rainfall may cause overwatering"})
    elif water_req == "medium":
        if 600 <= rainfall <= 1500:
            scores["water"] = 90
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": "Moderate rainfall matches water needs"})
        else:
            scores["water"] = max(40, 90 - abs(rainfall - 1000) * 0.03)
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": f"Rainfall {rainfall}mm; some adjustment needed"})
    else:
        if rainfall > 1000:
            scores["water"] = 90
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": "High rainfall suits water-intensive plants"})
        else:
            scores["water"] = max(30, 90 - (1000 - rainfall) * 0.06)
            reasons.append({"category": "Water", "score": scores["water"],
                            "detail": f"Needs more water than {rainfall}mm provides"})

    # Soil compatibility (15%)
    soil = env["soil_type"].lower()
    plant_soil = [s.lower() for s in plant.get("soil", [])]
    if any(ps in soil or soil in ps for ps in plant_soil):
        scores["soil"] = 90
        reasons.append({"category": "Soil", "score": scores["soil"],
                        "detail": f"Soil type '{soil}' matches preference"})
    else:
        scores["soil"] = 60
        reasons.append({"category": "Soil", "score": scores["soil"],
                        "detail": f"Soil type '{soil}' may need amendment"})

    # Photo observation bonus (10%)
    photo_score = 50
    if photo_obs:
        if photo_obs.get("has_sunlight"):
            photo_score += 20
        if photo_obs.get("soil_visible"):
            photo_score += 15
        if photo_obs.get("has_vegetation"):
            photo_score += 15
    scores["photo"] = min(100, photo_score)

    # Weighted total
    total = sum(scores[k] * SCORING_WEIGHTS[k] for k in SCORING_WEIGHTS)
    total = round(min(100, max(0, total)), 1)

    return {"total": total, "breakdown": scores, "reasons": reasons}


# --- Photo Analysis (Pillow-based with filename fallback) ---

def analyze_photo(photo) -> dict:
    if not photo:
        return None

    # Try Pillow-based analysis first
    if photo.base64:
        try:
            import base64
            from io import BytesIO
            from PIL import Image

            img_data = base64.b64decode(photo.base64.split(",")[1] if "," in photo.base64 else photo.base64)
            img = Image.open(BytesIO(img_data))
            # Explicit format check: reject non-image / exotic payloads instead
            # of silently falling back to filename heuristics on corrupt uploads.
            if img.format not in ("JPEG", "PNG", "WEBP", "GIF", "BMP"):
                raise ValueError(f"Unsupported image format: {img.format}")
            img = img.convert("RGB")
            # Downscale guard: pixel-by-pixel analysis over a huge image is
            # slow and unguarded, so cap at ~1MP before reading pixels.
            if img.width * img.height > 1_000_000:
                img.thumbnail((1000, 1000))
            pixels = list(img.getdata())
            total_pixels = len(pixels)

            avg_brightness = sum(sum(p) / 3 for p in pixels) / total_pixels
            avg_green = sum(p[1] for p in pixels) / total_pixels
            avg_red = sum(p[0] for p in pixels) / total_pixels
            green_ratio = avg_green / (avg_red + 1)

            has_sunlight = avg_brightness > 100
            shade_level = "full" if avg_brightness < 60 else "partial" if avg_brightness < 120 else "none"
            has_vegetation = green_ratio > 1.1

            soil_pixels = sum(1 for p in pixels if 80 < p[0] < 180 and 50 < p[1] < 130 and p[2] < 80)
            soil_ratio = soil_pixels / total_pixels
            soil_visible = soil_ratio > 0.05

            # Calculate overall confidence based on image quality and consistency
            confidence = 0.7  # Base confidence for pixel analysis
            if total_pixels > 100000:
                confidence += 0.1  # Higher resolution = more reliable
            if 0.3 < green_ratio < 3.0:
                confidence += 0.1  # Consistent color ratios
            confidence = min(confidence, 0.95)

            features = []
            if has_sunlight:
                features.append({"text": "Estimated good sunlight exposure", "confidence": round(confidence, 2)})
            else:
                features.append({"text": "Estimated low light conditions", "confidence": round(confidence, 2)})
            if has_vegetation:
                features.append({"text": "Green vegetation detected", "confidence": round(confidence * 0.9, 2)})
            if soil_visible:
                features.append({"text": "Exposed soil visible", "confidence": round(confidence * 0.85, 2)})

            return {
                "has_sunlight": has_sunlight,
                "soil_visible": soil_visible,
                "has_vegetation": has_vegetation,
                "shade_level": shade_level,
                "features": features,
                "analysis_method": "pixel_analysis",
                "overall_confidence": round(confidence, 2),
                "disclaimer": "Analysis based on pixel-level image heuristics. For accurate assessment, consult local gardening expertise.",
            }
        except Exception:
            pass

    # Fallback: filename-based heuristics (lower confidence)
    features = []
    confidence = 0.4
    if photo.filename:
        name_lower = photo.filename.lower()
        if "shade" in name_lower or "dark" in name_lower:
            features.append({"text": "Estimated low light (filename hint)", "confidence": 0.5})
        if "soil" in name_lower or "ground" in name_lower:
            features.append({"text": "Soil visible (filename hint)", "confidence": 0.5})
        if "plant" in name_lower or "garden" in name_lower or "green" in name_lower:
            features.append({"text": "Vegetation likely present (filename hint)", "confidence": 0.5})
    if not features:
        features = [
            {"text": "Open growing area estimated", "confidence": 0.4},
            {"text": "Moderate sunlight estimated", "confidence": 0.4},
        ]

    return {
        "has_sunlight": True,
        "soil_visible": True,
        "has_vegetation": True,
        "shade_level": "partial",
        "features": features,
        "analysis_method": "filename_heuristic",
        "overall_confidence": round(confidence, 2),
        "disclaimer": "Analysis based on filename heuristics only. Upload a clear garden photo for better estimates.",
    }


def photo_obs_to_str(photo_obs: dict) -> str:
    if not photo_obs:
        return "No photo analysis available"
    features = photo_obs.get("features", [])
    if not features:
        return "Photo analyzed"
    lines = [f["text"] if isinstance(f, dict) else f for f in features]
    return "; ".join(lines)


# --- FastAPI App ---

app = FastAPI(title="GreenScope API", version="0.1.0")

# Rate limiting: cheap insurance so one abusive client can't get our
# server IP banned by Nominatim (their usage policy is strict: ~1 req/s).
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please wait a moment and try again."},
    )

# CORS: wide open for hackathon demo (frontend + API served from same
# origin in prod; "*" lets judges hit the API from any client/notebook).
# To lock down: set ALLOWED_ORIGINS env var to a comma-separated list.
_allowed = os.environ.get("ALLOWED_ORIGINS", "*")
_origins = ["*"] if _allowed.strip() == "*" else [o.strip() for o in _allowed.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request / Response Models ---

class LocationInput(BaseModel):
    location: str = Field(..., min_length=1, max_length=200, description="Address, coordinates, or location description")
    latitude: Optional[float] = Field(None, description="Latitude if provided")
    longitude: Optional[float] = Field(None, description="Longitude if provided")


class PhotoUpload(BaseModel):
    image_id: str = Field(..., description="Uploaded image identifier")
    filename: str = Field(..., description="Original filename", max_length=255)
    base64: Optional[str] = Field(
        None,
        description="Base64 encoded image data",
        max_length=8_000_000  # ~8MB string cap; decoded bytes checked below
    )

    @field_validator("base64")
    @classmethod
    def check_decoded_size(cls, v):
        # Server-side size guard: client-side JS blocks >5MB, but that is
        # trivially bypassable (curl/Postman). Reject oversized payloads
        # here so a huge image can't spike memory/CPU in pixel analysis.
        if v is None:
            return v
        payload = v.split(",", 1)[1] if "," in v else v
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            raise ValueError("Invalid base64 image data")
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("Decoded image exceeds 5MB limit")
        return v


class ReportGenerateRequest(BaseModel):
    location: LocationInput
    photo: Optional[PhotoUpload] = Field(None, description="Optional uploaded garden photo")
    num_recommendations: int = Field(15, ge=1, le=20, description="Number of plant recommendations")


class ReportRecommendation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plant_name: str
    scientific_name: Optional[str] = None
    reasoning: str
    suitability_score: float
    care_guide: str
    growth_duration: Optional[str] = None
    score_breakdown: Optional[dict] = None
    water_requirement: Optional[str] = None
    sun_requirement: Optional[str] = None
    planting_season: Optional[str] = None
    tags: List[str] = Field(default_factory=list, description="Badges: air-purifying, pollinator-friendly, companion hints")
    estimated_water_l_per_week: Optional[float] = Field(None, description="Estimated liters/plant/week (approx)")
    estimated_co2_kg_per_year: Optional[float] = Field(None, description="Estimated CO2 kg/plant/year (approx)")


class GardenRisk(BaseModel):
    sunlight: str
    water: str
    temperature: str
    soil: str
    overall: str
    warnings: List[str]


class ReportGenerateResponse(BaseModel):
    report_id: str
    location: str
    latitude: float
    longitude: float
    environment: dict
    photo_analysis: Optional[dict] = None
    recommendations: List[ReportRecommendation]
    rejected_plants: Optional[List[dict]] = None
    categories: Optional[dict] = None
    garden_risk: Optional[GardenRisk] = None
    comparison_table: Optional[List[dict]] = None
    garden_bed: Optional[dict] = Field(
        default=None,
        description="Top picks reframed as one garden bed that grows well together"
    )
    generated_at: float
    processing_time_ms: float
    data_source: Optional[str] = Field(
        default="live_api",
        description="'live_api' if real Nominatim+Open-Meteo data, 'fallback' if hardcoded data used"
    )
    disclaimer: str = Field(
        default="Suitability scores are calculated from climate, soil, sunlight, water, and photo data. "
        "Results should be verified with local gardening advice."
    )


# In-memory store
reports_db = {}


@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"))


@app.get("/api/v1/health", response_model=dict)
async def health_check():
    return {"status": "healthy", "service": "GreenScope API"}


# --- Geocoding Cache (Nominatim rate-limit protection) ---
_geocode_cache = {}

async def geocode_location(location: LocationInput):
    cache_key = location.location.strip().lower()

    # Check cache first (Nominatim rate-limit protection)
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # Try Nominatim (OpenStreetMap) geocoding first
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location.location, "format": "json", "limit": 1},
                headers={"User-Agent": "GreenScope/0.1 (hackathon project; contact: github.com/nidpaliwal/GreenScope)"}
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    lat = float(data[0]["lat"])
                    lng = float(data[0]["lon"])
                    display = data[0].get("display_name", location.location)
                    result = {"latitude": lat, "longitude": lng, "formatted": display.split(",")[0]}
                    _geocode_cache[cache_key] = result
                    return result
            elif r.status_code == 429:
                # Nominatim rate-limited — fall through to hardcoded
                pass
    except Exception:
        pass

    # Fallback to hardcoded Indian cities
    mock_geocodes = {
        "mumbai, maharashtra": (19.0760, 72.8777),
        "delhi, ncr": (28.6139, 77.2090),
        "bengaluru, karnataka": (12.9716, 77.5946),
        "chennai, tamil nadu": (13.0827, 80.2707),
        "kolkata, west bengal": (22.5726, 88.3639),
        "hyderabad, telangana": (17.3850, 78.4867),
        "pune, maharashtra": (18.5204, 73.8567),
        "jaipur, rajasthan": (26.9124, 75.7873),
        "ahmedabad, gujarat": (23.0225, 72.5714),
        "lucknow, uttar pradesh": (26.8467, 80.9462),
        "bhopal, madhya pradesh": (23.2599, 77.4126),
        "patna, bihar": (25.6093, 85.1376),
        "shimla, himachal pradesh": (31.1048, 77.1734),
        "srinagar, jammu & kashmir": (34.0837, 74.7973),
        "thiruvananthapuram, kerala": (8.5241, 76.9366),
        "udaipur, rajasthan": (24.5854, 73.7125),
    }
    loc_key = location.location.lower()
    for key, (lat, lng) in mock_geocodes.items():
        if key in loc_key:
            return {"latitude": lat, "longitude": lng, "formatted": location.location}

    # If Nominatim didn't return results and not in hardcoded list,
    # still allow the report with approximate India center coordinates
    return {"latitude": 20.5937, "longitude": 78.9629, "formatted": location.location}


@app.post("/api/v1/geocode", response_model=dict)
@limiter.limit("120/minute")
async def geocode_endpoint(request: Request, location: LocationInput):
    return await geocode_location(location)


@app.post("/api/v1/generate-report", response_model=ReportGenerateResponse)
@limiter.limit("90/minute")
async def generate_report(request: Request, body: ReportGenerateRequest):
    start_time = time.time()
    report_id = str(uuid.uuid4())

    # Validate location string is not blank
    if not body.location.location or not body.location.location.strip():
        raise HTTPException(status_code=400, detail="Location cannot be empty")

    # Resolve location to coordinates using geocode function
    if body.location.latitude is None or body.location.longitude is None:
        geocoded = await geocode_location(body.location)
        lat = geocoded["latitude"]
        lng = geocoded["longitude"]
        location_str = geocoded["formatted"]
    else:
        lat = body.location.latitude
        lng = body.location.longitude
        location_str = body.location.location or "Unknown location"

    env, data_source = get_env_for_location(lat, lng, location_str)
    photo_obs = analyze_photo(body.photo)

    scored = []
    for plant in PLANTS:
        result = score_plant(plant, env, photo_obs)
        scored.append((plant, result))

    scored.sort(key=lambda x: x[1]["total"], reverse=True)

    top = scored[:body.num_recommendations]
    rejected = scored[body.num_recommendations:]

    recommendations = []
    for plant, score_result in top:
        days = plant.get("growth_days", 60)
        if days >= 365:
            growth_str = f"{days // 365} year(s)"
        elif days >= 30:
            growth_str = f"{days} days"
        else:
            growth_str = f"{days} days"

        care_guide = (
            f"Plant in {plant.get('soil', ['loam'])[0]} soil with pH {plant['min_ph']}-{plant['max_ph']}. "
            f"Requires {plant['sun']} sun ({env['sunlight_hours']}h available). "
            f"Water {plant['water']}ly (rainfall: {env['rainfall_mm']}mm/year). "
            f"Temperature range: {plant['temperature_min']}-{plant['temperature_max']}C."
        )

        reasons = score_result["reasons"]
        score_lines = "\n".join(
            f"  {r['category']}: {r['score']:.0f}/100 - {r['detail']}" for r in reasons
        )
        reasoning = (
            f"Suitability: {score_result['total']:.0f}/100\n"
            f"Environment: {env['avg_temp_c']}C avg, {env['rainfall_mm']}mm rainfall, "
            f"pH {env['ph']}, {env['sunlight_hours']}h sunlight\n\n"
            f"Score Breakdown:\n{score_lines}"
        )

        recommendations.append(ReportRecommendation(
            plant_name=plant["name"],
            scientific_name=plant.get("scientific_name"),
            reasoning=reasoning,
            suitability_score=score_result["total"],
            care_guide=care_guide,
            growth_duration=growth_str,
            score_breakdown=score_result["breakdown"],
            water_requirement=plant.get("water"),
            sun_requirement=plant.get("sun"),
            planting_season=plant.get("planting_season", "Kharif"),
            tags=plant_tags(plant["name"]),
            estimated_water_l_per_week=WATER_L_PER_WEEK.get(plant.get("water", "medium"), 8.0),
            estimated_co2_kg_per_year=CO2_KG_PER_YEAR.get(plant.get("category", "vegetable"), 1.0),
        ))

    # Categories
    categories = {}
    if recommendations:
        categories["best_overall"] = recommendations[0].plant_name
        low_water = [r for r in recommendations if r.water_requirement == "low"]
        if low_water:
            categories["best_low_water"] = low_water[0].plant_name
        fastest = min(recommendations, key=lambda r: _parse_days(r.growth_duration))
        categories["fastest_harvest"] = fastest.plant_name
        flowers = [r for r in recommendations if any(
            p["name"] == r.plant_name and p.get("category") == "flower"
            for p in PLANTS
        )]
        if flowers:
            categories["best_flower"] = flowers[0].plant_name
        herbs = [r for r in recommendations if any(
            p["name"] == r.plant_name and p.get("category") == "herb"
            for p in PLANTS
        )]
        if herbs:
            categories["best_herb"] = herbs[0].plant_name

    # Rejected plants with reasons
    rejected_list = []
    for plant, score_result in rejected:
        low_scores = [r for r in score_result["reasons"] if r["score"] < 60]
        rejection_reasons = [f"{r['category']}: {r['detail']}" for r in low_scores]
        if not rejection_reasons:
            rejection_reasons = ["Lower suitability than top picks"]
        rejected_list.append({
            "plant_name": plant["name"],
            "suitability": score_result["total"],
            "reasons": rejection_reasons,
        })

    # Garden Risk Assessment
    risk_warnings = []
    sunlight_status = "GOOD"
    if env["sunlight_hours"] < 5:
        sunlight_status = "LOW"
        risk_warnings.append("Limited sunlight may restrict full-sun plants")
    elif env["sunlight_hours"] > 8:
        sunlight_status = "HIGH"
        risk_warnings.append("Intense sunlight may cause heat stress without adequate watering")

    water_status = "MODERATE"
    if env["rainfall_mm"] < 500:
        water_status = "LOW"
        risk_warnings.append("Low rainfall requires regular irrigation")
    elif env["rainfall_mm"] > 1500:
        water_status = "HIGH"
        risk_warnings.append("High rainfall may cause waterlogging or fungal issues")

    temp_status = "GOOD"
    if env["avg_temp_c"] < 10:
        temp_status = "COLD"
        risk_warnings.append("Cold temperatures limit tropical and warm-season plants")
    elif env["avg_temp_c"] > 28:
        temp_status = "HOT"
        risk_warnings.append("Heat may stress cool-season plants")

    soil_status = "GOOD"
    if env["ph"] < 5.5 or env["ph"] > 8.0:
        soil_status = "EXTREME pH"
        risk_warnings.append(f"Soil pH {env['ph']} may need amendment")

    if env["frost_risk"] == "high":
        risk_warnings.append("High frost risk: protect tender plants in winter")

    risk_scores = [0, 0, 0, 0]
    for i, s in enumerate([sunlight_status, water_status, temp_status, soil_status]):
        if s == "GOOD":
            risk_scores[i] = 90
        elif s in ("MODERATE", "HIGH", "HOT"):
            risk_scores[i] = 70
        else:
            risk_scores[i] = 40
    avg_risk = sum(risk_scores) / 4
    overall_risk = "LOW" if avg_risk >= 80 else "MODERATE" if avg_risk >= 60 else "HIGH"

    garden_risk = GardenRisk(
        sunlight=sunlight_status,
        water=water_status,
        temperature=temp_status,
        soil=soil_status,
        overall=overall_risk,
        warnings=risk_warnings,
    )

    # Comparison table (all scored plants)
    comparison = []
    for plant, score_result in scored[:8]:
        days = plant.get("growth_days", 60)
        if days >= 365:
            growth_str = f"{days // 365} yr"
        else:
            growth_str = f"{days}d"
        comparison.append({
            "name": plant["name"],
            "suitability": score_result["total"],
            "water": plant.get("water", "medium"),
            "sun": plant.get("sun", "full"),
            "growth": growth_str,
        })

    processing_time_ms = (time.time() - start_time) * 1000

    garden_bed = build_garden_bed(recommendations)

    photo_analysis = None
    if photo_obs:
        photo_analysis = {
            "features": photo_obs.get("features", photo_obs.get("detected_features", [])),
            "shade_level": photo_obs.get("shade_level", "unknown"),
            "analysis_method": photo_obs.get("analysis_method", "unknown"),
            "overall_confidence": photo_obs.get("overall_confidence", 0.5),
            "disclaimer": photo_obs.get("disclaimer", ""),
        }

    response = ReportGenerateResponse(
        report_id=report_id,
        location=location_str,
        latitude=lat,
        longitude=lng,
        environment=env,
        photo_analysis=photo_analysis,
        recommendations=recommendations,
        rejected_plants=rejected_list,
        categories=categories,
        garden_risk=garden_risk,
        comparison_table=comparison,
        garden_bed=garden_bed,
        generated_at=time.time(),
        processing_time_ms=round(processing_time_ms, 2),
        data_source=data_source,
    )

    reports_db[report_id] = response
    # Save the response model so the persistence helper can access its fields.
    save_report_to_db(response)
    return response


def _parse_days(growth_str: str) -> int:
    if not growth_str:
        return 999
    if "year" in growth_str:
        num = int(growth_str.split()[0])
        return num * 365
    return int(growth_str.split()[0])


# --- Seasonal Planting Calendar (Indian Kharif/Rabi/Zaid) ---

PLANTING_CALENDAR = {
    "kharif": ["Tomato", "Okra", "Brinjal", "Chili", "Ridge Gourd", "Bottle Gourd", "Moong", "Cowpea", "Banana", "Turmeric", "Mango"],
    "rabi": ["Spinach", "Fenugreek", "Coriander", "Brinjal", "Chili", "Tomato", "Marigold"],
    "zaid": ["Cucumber", "Bottle Gourd", "Ridge Gourd", "Moong"],
    "year-round": ["Tulsi", "Neem", "Curry Leaf", "Aloe Vera", "Lemongrass", "Ashwagandha", "Brahmi", "Giloy", "Moringa"],
}

MONTH_TO_SEASON = {
    1: "rabi", 2: "rabi", 3: "zaid", 4: "zaid", 5: "zaid",
    6: "kharif", 7: "kharif", 8: "kharif", 9: "kharif", 10: "rabi", 11: "rabi", 12: "rabi",
}


def get_season_for_location(lat: float, month: int) -> str:
    return MONTH_TO_SEASON[month]


class SeasonalPlanting(BaseModel):
    plant_name: str
    scientific_name: Optional[str] = None
    best_planting_window: str
    days_to_harvest: Optional[str] = None
    suitability: float
    category: Optional[str] = None


class PlantThisMonthResponse(BaseModel):
    location: str
    latitude: float
    longitude: float
    current_month: str
    season: str
    plants: List[SeasonalPlanting]


@app.get("/api/v1/plant-this-month", response_model=PlantThisMonthResponse)
@limiter.limit("120/minute")
async def plant_this_month(request: Request, location: str = "Delhi, NCR", latitude: float = None, longitude: float = None):
    now = time.localtime()
    month = now.tm_mon
    month_names = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
    month_name = month_names[month]

    lat = latitude or 28.6139
    lng = longitude or 77.2090
    env, _ = get_env_for_location(lat, lng, location)

    season = get_season_for_location(lat, month)
    seasonal_plants = PLANTING_CALENDAR.get(season, []) + PLANTING_CALENDAR.get("year-round", [])

    plant_map = {p["name"]: p for p in PLANTS}
    scored = []
    for name in seasonal_plants:
        if name in plant_map:
            plant = plant_map[name]
            result = score_plant(plant, env, {})
            days = plant.get("growth_days", 60)
            if days >= 365:
                growth_str = f"{days // 365} year(s)"
            else:
                growth_str = f"{days} days"
            scored.append(SeasonalPlanting(
                plant_name=plant["name"],
                scientific_name=plant.get("scientific_name"),
                best_planting_window=f"Plant now in {month_name}",
                days_to_harvest=growth_str,
                suitability=result["total"],
                category=plant.get("category"),
            ))

    scored.sort(key=lambda x: x.suitability, reverse=True)

    return PlantThisMonthResponse(
        location=location,
        latitude=lat,
        longitude=lng,
        current_month=month_name,
        season=season,
        plants=scored,
    )


@app.get("/api/v1/reports/{report_id}", response_model=ReportGenerateResponse)
async def get_report(report_id: str):
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    recs = []
    for r in report.get('recommendations', []):
        rec = ReportRecommendation(
            plant_name=r['plant_name'],
            scientific_name=r.get('scientific_name'),
            reasoning=r['reasoning'],
            suitability_score=r['suitability_score'],
            care_guide=r['care_guide'],
            growth_duration=r.get('growth_duration'),
            score_breakdown=r.get('score_breakdown'),
            water_requirement=r.get('water_requirement'),
            sun_requirement=r.get('sun_requirement'),
            planting_season=r.get('planting_season'),
            tags=r.get('tags', []),
            estimated_water_l_per_week=r.get('estimated_water_l_per_week'),
            estimated_co2_kg_per_year=r.get('estimated_co2_kg_per_year'),
        )
        recs.append(rec)
    gr = report.get('garden_risk')
    return ReportGenerateResponse(
        report_id=report_id,
        location=report.get('location', ''),
        latitude=report.get('latitude', 0.0),
        longitude=report.get('longitude', 0.0),
        environment=report.get('environment', {}),
        photo_analysis=report.get('photo_analysis'),
        recommendations=recs,
        rejected_plants=report.get('rejected_plants'),
        categories=report.get('categories'),
        garden_risk=GardenRisk(**gr) if gr else None,
        comparison_table=report.get('comparison_table'),
        garden_bed=report.get('garden_bed'),
        generated_at=report.get('generated_at', time.time()),
        processing_time_ms=report.get('processing_time_ms', 0.0),
        data_source=report.get('data_source', 'fallback'),
        disclaimer=ReportGenerateResponse.model_fields['disclaimer'].default,
    )


@app.get("/report/{report_id}", response_class=FileResponse)
async def serve_report_page(report_id: str):
    report_path = os.path.join(os.path.dirname(__file__), "..", "static", "report.html")
    return FileResponse(report_path)


class FeedbackVote(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=64)
    plant_name: str = Field(..., min_length=1, max_length=100)
    vote: str = Field(..., pattern="^(up|down)$")


@app.post("/api/v1/feedback", response_model=dict)
@limiter.limit("60/minute")
async def submit_feedback(request: Request, vote: FeedbackVote):
    """Thumbs up/down per plant suggestion. Answers 'how would this improve
    over time' — votes accumulate per plant and are readable via summary."""
    conn = get_db()
    conn.execute(
        "INSERT INTO feedback (report_id, plant_name, vote, created_at) VALUES (?, ?, ?, ?)",
        (vote.report_id, vote.plant_name, vote.vote, time.time()),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/v1/feedback/summary", response_model=dict)
@limiter.limit("60/minute")
async def feedback_summary(request: Request):
    conn = get_db()
    rows = conn.execute(
        "SELECT plant_name,"
        " SUM(CASE WHEN vote='up' THEN 1 ELSE 0 END) AS up,"
        " SUM(CASE WHEN vote='down' THEN 1 ELSE 0 END) AS down,"
        " COUNT(*) AS total FROM feedback GROUP BY plant_name"
    ).fetchall()
    conn.close()
    return {
        r[0]: {"up": r[1], "down": r[2], "total": r[3],
               "helpful_pct": round(100 * r[1] / r[3], 1) if r[3] else 0}
        for r in rows
    }


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


# Representative sowing month per Indian planting season
SEASON_SOW_MONTH = {"kharif": 6, "monsoon": 6, "rabi": 10, "year-round": None}


def _season_event_date(season: str, today: date) -> date:
    month = SEASON_SOW_MONTH.get((season or "").lower(), None) or today.month
    year = today.year if month >= today.month else today.year + 1
    return date(year, month, 1)


@app.get("/api/v1/reports/{report_id}/calendar.ics")
@limiter.limit("60/minute")
async def report_calendar_ics(request: Request, report_id: str):
    """ICS download: one 'Plant {name}' event per top recommendation,
    dated to the next occurrence of that plant's sowing season
    (Kharif/Monsoon -> June, Rabi -> October, Year-round -> this month)."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    today = date.today()
    stamp = today.strftime("%Y%m%d")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//GreenScope//Planting Reminders//EN"]
    for i, r in enumerate(report.get("recommendations", [])[:8]):
        uid = f"{report_id}-{i}@greenscope"
        season = r.get("planting_season", "Year-round")
        event_date = _season_event_date(season, today)
        summary = f"Plant {r.get('plant_name', 'crop')} ({season} season)"
        desc = (f"Sow at the start of {season} season in {report.get('location', '')}. "
                f"Suitability {r.get('suitability_score', '?')}%. "
                f"{r.get('care_guide', '')} "
                f"Water: {r.get('water_requirement', '?')}, Sun: {r.get('sun_requirement', '?')}.")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}T000000Z",
            f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return PlainTextResponse(
        "\r\n".join(lines),
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="greenscope-{report_id[:8]}.ics"'},
    )


# --- Mount static files (after API routes) ---
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
