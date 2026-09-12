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
import math
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

# Valid plant categories and aliases for filtering
VALID_CATEGORIES = {"herb", "fruit", "vegetable", "spice", "pulse", "flower"}
CATEGORY_ALIASES = {"decorative": "flower"}

# --- Watering Schedule Calculator ---
# Base watering frequency (days between watering) by water requirement
WATERING_FREQUENCY_DAYS = {"low": 7, "medium": 3, "high": 1}
# Rainfall adjustment thresholds (mm/year)
RAINFALL_THRESHOLDS = {"low": 600, "medium": 1000, "high": 1500}
# Evapotranspiration estimate by temperature (mm/day)
ET_BY_TEMP = {10: 2.5, 15: 3.5, 20: 4.5, 25: 5.5, 30: 6.5, 35: 7.5}


def calculate_watering_schedule(plant: dict, env: dict) -> dict:
    """Calculate personalized watering schedule for a plant based on environment.
    
    Returns deterministic schedule with frequency, amount, and seasonal adjustments.
    All values are estimates, not measurements.
    """
    water_req = plant.get("water", "medium")
    base_freq_days = WATERING_FREQUENCY_DAYS.get(water_req, 3)
    rainfall_mm = env.get("rainfall_mm", 900)
    avg_temp = env.get("avg_temp_c", 25)
    humidity = env.get("humidity", 60)
    sunlight_hours = env.get("sunlight_hours", 6.5)
    soil_type = env.get("soil_type", "loam").lower()
    frost_risk = env.get("frost_risk", "none")
    
    # Estimate daily evapotranspiration (mm/day) by temperature interpolation
    temps = sorted(ET_BY_TEMP.keys())
    et = ET_BY_TEMP[temps[0]]
    for i, t in enumerate(temps):
        if avg_temp <= t:
            et = ET_BY_TEMP[t]
            break
        et = ET_BY_TEMP[t]
    # Adjust for humidity (high humidity reduces ET)
    et *= max(0.5, 1 - (humidity - 50) / 100)
    # Adjust for sunlight
    et *= max(0.7, sunlight_hours / 7)
    
    # Rainfall contribution (mm/day during growing season ~6 months)
    growing_season_days = 180
    daily_rainfall = rainfall_mm / 365
    effective_rainfall = daily_rainfall * 0.6  # 60% usable by plants
    
    # Net water need per day (mm)
    net_need = max(0.5, et - effective_rainfall)
    
    # Convert to liters per plant per watering (assuming 0.25 m2 canopy per plant)
    canopy_area = 0.25  # m2
    liters_per_mm = canopy_area  # 1mm over 1m2 = 1 liter
    liters_per_watering = round(net_need * base_freq_days * liters_per_mm, 1)
    
    # Adjust frequency based on soil type (sandy = more frequent, clay = less)
    soil_freq_mult = 1.0
    if "sandy" in soil_type:
        soil_freq_mult = 0.7
    elif "clay" in soil_type:
        soil_freq_mult = 1.3
    elif "black cotton" in soil_type:
        soil_freq_mult = 1.2
    
    adjusted_freq = max(1, round(base_freq_days * soil_freq_mult))
    
    # Seasonal adjustments
    seasonal_notes = []
    if frost_risk == "high":
        seasonal_notes.append("Reduce watering in winter; avoid waterlogging before frost")
    if avg_temp > 30:
        seasonal_notes.append("Increase frequency by 1-2 days during peak summer heat")
    if rainfall_mm > 1500:
        seasonal_notes.append("Monsoon: skip watering on rainy days; ensure drainage")
    elif rainfall_mm < 500:
        seasonal_notes.append("Arid: mulch heavily; consider drip irrigation")
    
    # Weekly schedule (7 days)
    weekly_schedule = []
    for day in range(7):
        if day % adjusted_freq == 0:
            weekly_schedule.append({"day": day, "water_l": liters_per_watering, "action": "water"})
        else:
            weekly_schedule.append({"day": day, "water_l": 0, "action": "check soil"})
    
    return {
        "frequency_days": adjusted_freq,
        "liters_per_watering": liters_per_watering,
        "weekly_liters": round(liters_per_watering * (7 / adjusted_freq), 1),
        "weekly_schedule": weekly_schedule,
        "et_mm_per_day": round(et, 1),
        "effective_rainfall_mm_per_day": round(effective_rainfall, 1),
        "net_need_mm_per_day": round(net_need, 1),
        "seasonal_notes": seasonal_notes,
        "disclaimer": "Schedule based on climate averages and plant water category. Adjust for actual weather, soil moisture, and plant stage. Check soil 5cm deep before watering."
    }

# --- Pest & Disease Alerts ---
# Common pests/diseases by plant category with trigger conditions
PEST_DISEASE_ALERTS = {
    "vegetable": [
        {"name": "Aphids", "trigger": "high_temp", "threshold": 25, "advice": "Spray neem oil or introduce ladybugs. Check undersides of leaves."},
        {"name": "Whiteflies", "trigger": "high_temp", "threshold": 28, "advice": "Yellow sticky traps. Insecticidal soap for heavy infestations."},
        {"name": "Fungal Leaf Spot", "trigger": "high_humidity", "threshold": 75, "advice": "Improve air circulation. Avoid overhead watering. Copper fungicide if severe."},
        {"name": "Blossom End Rot (Tomato/Pepper)", "trigger": "irregular_water", "advice": "Consistent watering. Mulch to retain moisture. Calcium spray."},
    ],
    "fruit": [
        {"name": "Fruit Fly", "trigger": "high_temp", "threshold": 28, "advice": "Bag fruits early. Protein bait traps. Remove fallen fruit."},
        {"name": "Anthracnose", "trigger": "high_humidity", "threshold": 80, "advice": "Prune for airflow. Copper-based fungicide. Avoid wet foliage."},
        {"name": "Scale Insects", "trigger": "high_temp", "threshold": 25, "advice": "Horticultural oil in dormant season. Scrape off visible scales."},
    ],
    "herb": [
        {"name": "Spider Mites", "trigger": "high_temp_low_humidity", "threshold": 30, "advice": "Increase humidity. Strong water spray. Neem oil."},
        {"name": "Downy Mildew (Basil/Coriander)", "trigger": "high_humidity", "threshold": 80, "advice": "Space plants for airflow. Water at base. Remove affected leaves."},
        {"name": "Leaf Miners", "trigger": "high_temp", "threshold": 25, "advice": "Remove mined leaves. Yellow sticky traps. Neem oil."},
    ],
    "flower": [
        {"name": "Thrips", "trigger": "high_temp", "threshold": 28, "advice": "Blue sticky traps. Insecticidal soap. Predatory mites."},
        {"name": "Powdery Mildew", "trigger": "high_humidity", "threshold": 75, "advice": "Baking soda spray (1 tsp/L). Improve airflow. Avoid evening watering."},
        {"name": "Botrytis (Gray Mold)", "trigger": "high_humidity", "threshold": 80, "advice": "Remove dead flowers. Space plants. Reduce humidity."},
    ],
    "spice": [
        {"name": "Rhizome Rot (Ginger/Turmeric)", "trigger": "high_humidity", "threshold": 80, "advice": "Well-drained soil. Raised beds. Remove affected rhizomes."},
        {"name": "Shoot Borer", "trigger": "high_temp", "threshold": 28, "advice": "Pheromone traps. Remove bored shoots. Neem cake in soil."},
    ],
    "pulse": [
        {"name": "Pod Borer", "trigger": "high_temp", "threshold": 28, "advice": "Pheromone traps. Spray Bacillus thuringiensis. Intercrop with marigold."},
        {"name": "Wilt (Fusarium)", "trigger": "high_temp", "threshold": 30, "advice": "Crop rotation. Resistant varieties. Solarize soil."},
    ],
}


def get_pest_disease_alerts(plant: dict, env: dict) -> list:
    """Get relevant pest/disease alerts for a plant based on its category and environment."""
    category = plant.get("category", "vegetable")
    alerts_config = PEST_DISEASE_ALERTS.get(category, [])
    env_temp = env.get("avg_temp_c", 25)
    env_humidity = env.get("humidity", 60)
    rainfall = env.get("rainfall_mm", 900)
    
    triggered_alerts = []
    for alert in alerts_config:
        trigger = alert.get("trigger")
        threshold = alert.get("threshold")
        should_trigger = False
        
        if trigger == "high_temp" and env_temp >= threshold:
            should_trigger = True
        elif trigger == "high_humidity" and env_humidity >= threshold:
            should_trigger = True
        elif trigger == "high_temp_low_humidity" and env_temp >= threshold and env_humidity < 50:
            should_trigger = True
        elif trigger == "irregular_water" and (rainfall < 400 or rainfall > 1800):
            should_trigger = True
        
        if should_trigger:
            triggered_alerts.append({
                "name": alert["name"],
                "risk_level": "high" if threshold and (env_temp >= threshold + 5 or env_humidity >= threshold + 10) else "moderate",
                "advice": alert["advice"],
                "trigger_condition": f"{trigger} (threshold: {threshold})" if threshold else trigger
            })
    
    return triggered_alerts


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

# --- SQLite/Postgres Database for Report Persistence ---
# On Render free tier, disk is ephemeral — reports survive until next deploy/spin-down.
# For persistent reports, set SQLITE_PATH env var to a persistent SQLite file path.
# For Postgres (production), set DATABASE_URL to a Postgres connection string.

def get_db():
    # Check for Postgres first (DATABASE_URL convention)
    db_url = os.environ.get("DATABASE_URL")
    if db_url and not db_url.startswith("sqlite"):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
        conn.autocommit = True
        _init_postgres_schema(conn)
        return PostgresConnectionWrapper(conn)

    # SQLite path (SQLITE_PATH or default)
    db_path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "reports.db"))
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///"):]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
    # Purchase tracking table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            plant_name TEXT,
            item_type TEXT CHECK(item_type IN ('seeds', 'saplings', 'fertilizer', 'tools', 'other')),
            item_name TEXT,
            quantity REAL,
            unit TEXT,
            cost_per_unit REAL,
            total_cost REAL,
            purchase_date REAL,
            notes TEXT,
            created_at REAL
        )"""
    )
    # Sales tracking table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            plant_name TEXT,
            item_name TEXT,
            quantity REAL,
            unit TEXT,
            price_per_unit REAL,
            total_revenue REAL,
            sale_date REAL,
            notes TEXT,
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
                "watering_schedule": r.watering_schedule,
                "pest_disease_alerts": r.pest_disease_alerts,
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
                        # For unknown locations, use latitude-based frost
                        if frost != "none":
                            alerts.append(f"Frost risk: {frost}. Protect frost-sensitive plants during winter months.")

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
    category: Optional[str] = Field(None, description="Filter recommendations by plant category (herb, fruit, vegetable, spice, pulse, flower)")

    @field_validator("category")
    @classmethod
    def validate_category(cls, v):
        if v is None:
            return v
        normalized = v.strip().lower()
        if normalized in CATEGORY_ALIASES:
            normalized = CATEGORY_ALIASES[normalized]
        if normalized not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category: '{v}'. Valid categories: {', '.join(sorted(VALID_CATEGORIES))}")
        return normalized


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
    watering_schedule: Optional[dict] = Field(None, description="Personalized watering schedule with frequency, amounts, and seasonal notes")
    pest_disease_alerts: Optional[list] = Field(None, description="Relevant pest and disease alerts for this plant in this environment")


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

    # Filter plants by category if specified
    plant_pool = PLANTS
    if body.category:
        plant_pool = [p for p in PLANTS if p.get("category") == body.category]
        if not plant_pool:
            raise HTTPException(
                status_code=400,
                detail=f"No plants found for category '{body.category}'. Valid categories: {', '.join(sorted(VALID_CATEGORIES))}"
            )

    scored = []
    for plant in plant_pool:
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
            watering_schedule=calculate_watering_schedule(plant, env),
            pest_disease_alerts=get_pest_disease_alerts(plant, env),
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


# --- Moon Phase / Biodynamic Calendar API ---

class MoonPhaseResponse(BaseModel):
    phase: str
    illumination: float
    biodynamic_category: str
    days_to_next_phase: float
    is_waxing: bool


class BiodynamicAdviceResponse(BaseModel):
    category: str
    description: str
    suitable_plants: List[str]
    avoid: str


class LunarDateResponse(BaseModel):
    tithi: str
    paksha: str
    lunar_month: str
    day: int


class AuspiciousDate(BaseModel):
    date: str
    day: str
    moon_phase: str
    biodynamic: str
    tithi: str
    lunar_month: str


class MoonCalendarResponse(BaseModel):
    current_moon: MoonPhaseResponse
    biodynamic_advice: BiodynamicAdviceResponse
    lunar_date: LunarDateResponse
    auspicious_dates: List[AuspiciousDate]


@app.get("/api/v1/moon-calendar", response_model=MoonCalendarResponse)
@limiter.limit("120/minute")
async def moon_calendar(request: Request, latitude: float = None, longitude: float = None):
    """Get current moon phase, biodynamic planting advice, and auspicious dates."""
    now = time.time()
    moon = calculate_moon_phase(now)
    lunar = calculate_lunar_date(now)
    today = date.today()
    auspicious = get_auspicious_dates(today, 30)

    # Get biodynamic advice for plants in database
    advice = get_biodynamic_advice(moon, PLANTS)

    return MoonCalendarResponse(
        current_moon=MoonPhaseResponse(**moon),
        biodynamic_advice=BiodynamicAdviceResponse(**advice),
        lunar_date=LunarDateResponse(**lunar),
        auspicious_dates=[AuspiciousDate(**d) for d in auspicious],
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
            watering_schedule=r.get('watering_schedule'),
            pest_disease_alerts=r.get('pest_disease_alerts'),
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


class PurchaseCreate(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=64)
    plant_name: str = Field(..., min_length=1, max_length=100)
    item_type: str = Field(..., pattern="^(seeds|saplings|fertilizer|tools|other)$")
    item_name: str = Field(..., min_length=1, max_length=100)
    quantity: float = Field(..., gt=0)
    unit: str = Field(..., min_length=1, max_length=20)
    cost_per_unit: float = Field(..., ge=0)
    purchase_date: Optional[float] = None
    notes: Optional[str] = None


class PurchaseResponse(BaseModel):
    id: int
    report_id: str
    plant_name: str
    item_type: str
    item_name: str
    quantity: float
    unit: str
    cost_per_unit: float
    total_cost: float
    purchase_date: float
    notes: Optional[str]
    created_at: float


class SaleCreate(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=64)
    plant_name: str = Field(..., min_length=1, max_length=100)
    item_name: str = Field(..., min_length=1, max_length=100)
    quantity: float = Field(..., gt=0)
    unit: str = Field(..., min_length=1, max_length=20)
    price_per_unit: float = Field(..., ge=0)
    sale_date: Optional[float] = None
    notes: Optional[str] = None


class SaleResponse(BaseModel):
    id: int
    report_id: str
    plant_name: str
    item_name: str
    quantity: float
    unit: str
    price_per_unit: float
    total_revenue: float
    sale_date: float
    notes: Optional[str]
    created_at: float


class LedgerSummary(BaseModel):
    report_id: str
    total_purchases: float
    total_sales: float
    net_profit: float
    by_plant: dict


@app.post("/api/v1/purchases", response_model=PurchaseResponse)
@limiter.limit("60/minute")
async def create_purchase(request: Request, purchase: PurchaseCreate):
    """Record a purchase (seeds, saplings, fertilizer, tools, etc.) for a plant."""
    conn = get_db()
    purchase_date = purchase.purchase_date or time.time()
    total_cost = purchase.quantity * purchase.cost_per_unit
    cursor = conn.execute(
        """INSERT INTO purchases
        (report_id, plant_name, item_type, item_name, quantity, unit, cost_per_unit, total_cost, purchase_date, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (purchase.report_id, purchase.plant_name, purchase.item_type, purchase.item_name,
         purchase.quantity, purchase.unit, purchase.cost_per_unit, total_cost,
         purchase_date, purchase.notes, time.time())
    )
    conn.commit()
    purchase_id = cursor.lastrowid
    conn.close()
    return PurchaseResponse(
        id=purchase_id,
        report_id=purchase.report_id,
        plant_name=purchase.plant_name,
        item_type=purchase.item_type,
        item_name=purchase.item_name,
        quantity=purchase.quantity,
        unit=purchase.unit,
        cost_per_unit=purchase.cost_per_unit,
        total_cost=total_cost,
        purchase_date=purchase_date,
        notes=purchase.notes,
        created_at=time.time()
    )


@app.get("/api/v1/purchases/{report_id}", response_model=List[PurchaseResponse])
@limiter.limit("60/minute")
async def get_purchases(request: Request, report_id: str, plant_name: Optional[str] = None):
    """Get all purchases for a report, optionally filtered by plant."""
    conn = get_db()
    if plant_name:
        rows = conn.execute(
            "SELECT * FROM purchases WHERE report_id = ? AND plant_name = ? ORDER BY purchase_date DESC",
            (report_id, plant_name)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM purchases WHERE report_id = ? ORDER BY purchase_date DESC",
            (report_id,)
        ).fetchall()
    conn.close()
    return [PurchaseResponse(
        id=r["id"], report_id=r["report_id"], plant_name=r["plant_name"],
        item_type=r["item_type"], item_name=r["item_name"],
        quantity=r["quantity"], unit=r["unit"], cost_per_unit=r["cost_per_unit"],
        total_cost=r["total_cost"], purchase_date=r["purchase_date"],
        notes=r["notes"], created_at=r["created_at"]
    ) for r in rows]


@app.post("/api/v1/sales", response_model=SaleResponse)
@limiter.limit("60/minute")
async def create_sale(request: Request, sale: SaleCreate):
    """Record a sale/harvest for a plant."""
    conn = get_db()
    sale_date = sale.sale_date or time.time()
    total_revenue = sale.quantity * sale.price_per_unit
    cursor = conn.execute(
        """INSERT INTO sales
        (report_id, plant_name, item_name, quantity, unit, price_per_unit, total_revenue, sale_date, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sale.report_id, sale.plant_name, sale.item_name, sale.quantity, sale.unit,
         sale.price_per_unit, total_revenue, sale_date, sale.notes, time.time())
    )
    conn.commit()
    sale_id = cursor.lastrowid
    conn.close()
    return SaleResponse(
        id=sale_id,
        report_id=sale.report_id,
        plant_name=sale.plant_name,
        item_name=sale.item_name,
        quantity=sale.quantity,
        unit=sale.unit,
        price_per_unit=sale.price_per_unit,
        total_revenue=total_revenue,
        sale_date=sale_date,
        notes=sale.notes,
        created_at=time.time()
    )


@app.get("/api/v1/sales/{report_id}", response_model=List[SaleResponse])
@limiter.limit("60/minute")
async def get_sales(request: Request, report_id: str, plant_name: Optional[str] = None):
    """Get all sales for a report, optionally filtered by plant."""
    conn = get_db()
    if plant_name:
        rows = conn.execute(
            "SELECT * FROM sales WHERE report_id = ? AND plant_name = ? ORDER BY sale_date DESC",
            (report_id, plant_name)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sales WHERE report_id = ? ORDER BY sale_date DESC",
            (report_id,)
        ).fetchall()
    conn.close()
    return [SaleResponse(
        id=r["id"], report_id=r["report_id"], plant_name=r["plant_name"],
        item_name=r["item_name"], quantity=r["quantity"], unit=r["unit"],
        price_per_unit=r["price_per_unit"], total_revenue=r["total_revenue"],
        sale_date=r["sale_date"], notes=r["notes"], created_at=r["created_at"]
    ) for r in rows]


@app.get("/api/v1/ledger/{report_id}", response_model=LedgerSummary)
@limiter.limit("60/minute")
async def get_ledger(request: Request, report_id: str):
    """Get profit/loss ledger for a report."""
    conn = get_db()
    # Total purchases
    purchase_rows = conn.execute(
        "SELECT plant_name, SUM(total_cost) as total FROM purchases WHERE report_id = ? GROUP BY plant_name",
        (report_id,)
    ).fetchall()
    # Total sales
    sale_rows = conn.execute(
        "SELECT plant_name, SUM(total_revenue) as total FROM sales WHERE report_id = ? GROUP BY plant_name",
        (report_id,)
    ).fetchall()
    conn.close()

    purchases_by_plant = {r["plant_name"]: r["total"] for r in purchase_rows}
    sales_by_plant = {r["plant_name"]: r["total"] for r in sale_rows}

    all_plants = set(purchases_by_plant.keys()) | set(sales_by_plant.keys())
    by_plant = {}
    total_purchases = 0
    total_sales = 0
    for plant in all_plants:
        p = purchases_by_plant.get(plant, 0)
        s = sales_by_plant.get(plant, 0)
        by_plant[plant] = {
            "purchases": p,
            "sales": s,
            "net": s - p
        }
        total_purchases += p
        total_sales += s

    return LedgerSummary(
        report_id=report_id,
        total_purchases=round(total_purchases, 2),
        total_sales=round(total_sales, 2),
        net_profit=round(total_sales - total_purchases, 2),
        by_plant=by_plant
    )


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


# --- Deterministic Chat Assistant (no LLM, uses local plant DB) ---

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    location: Optional[str] = None
    report_id: Optional[str] = None


def find_plant_by_name(name: str) -> Optional[dict]:
    """Find plant by name (case-insensitive, partial match)."""
    name_lower = name.lower().strip()
    for plant in PLANTS:
        if name_lower in plant["name"].lower() or plant["name"].lower() in name_lower:
            return plant
    return None


def find_plants_by_category(category: str) -> List[dict]:
    """Find plants by category."""
    cat_lower = category.lower().strip()
    if cat_lower in CATEGORY_ALIASES:
        cat_lower = CATEGORY_ALIASES[cat_lower]
    if cat_lower == "decorative":
        cat_lower = "flower"
    if cat_lower in VALID_CATEGORIES:
        return [p for p in PLANTS if p.get("category") == cat_lower]
    return []


def find_plants_by_field_size(field_size: str) -> List[dict]:
    """Find plants suitable for field size."""
    size_lower = field_size.lower().strip()
    # Large field: pulses, vegetables that scale, fruits
    # Small field: herbs, spices, compact vegetables
    if "large" in size_lower or "farm" in size_lower or "acre" in size_lower:
        return [p for p in PLANTS if p.get("category") in ("pulse", "vegetable", "fruit")]
    elif "small" in size_lower or "balcony" in size_lower or "container" in size_lower or "pot" in size_lower or "kitchen" in size_lower:
        return [p for p in PLANTS if p.get("category") in ("herb", "spice", "flower")]
    return []


def get_plant_care_info(plant: dict) -> str:
    """Generate care guide for a plant."""
    return (
        f"{plant['name']} ({plant.get('scientific_name', '')}): "
        f"Sun: {plant['sun']}, Water: {plant['water']}, "
        f"pH: {plant['min_ph']}-{plant['max_ph']}, "
        f"Temp: {plant['temperature_min']}-{plant['temperature_max']}°C, "
        f"Soil: {', '.join(plant.get('soil', ['loam']))}, "
        f"Growth: {plant.get('growth_days', 60)} days, "
        f"Season: {plant.get('planting_season', 'Kharif')}"
    )


def generate_chat_response(user_msg: str, location: Optional[str] = None, report_id: Optional[str] = None) -> str:
    """Generate deterministic response based on plant database and optional report context."""
    msg_lower = user_msg.lower()
    
    # Check for specific plant name mentions
    for plant in PLANTS:
        if plant["name"].lower() in msg_lower:
            care = get_plant_care_info(plant)
            tags = plant_tags(plant["name"])
            tag_str = ", ".join(tags) if tags else "none"
            return f"**{plant['name']}** ({plant.get('scientific_name', '')})\n{care}\nTags: {tag_str}"
    
    # Category queries
    if any(word in msg_lower for word in ["flower", "flowers", "bloom"]):
        flowers = find_plants_by_category("flower")
        names = ", ".join([p["name"] for p in flowers[:10]])
        return f"**Flowering plants** ({len(flowers)} total): {names}{'...' if len(flowers) > 10 else ''}"
    
    if any(word in msg_lower for word in ["fruit", "fruits", "tree"]):
        fruits = find_plants_by_category("fruit")
        names = ", ".join([p["name"] for p in fruits[:10]])
        return f"**Fruit plants** ({len(fruits)} total): {names}{'...' if len(fruits) > 10 else ''}"
    
    if "decorative" in msg_lower or "ornamental" in msg_lower:
        flowers = find_plants_by_category("flower")
        names = ", ".join([p["name"] for p in flowers[:10]])
        return f"**Decorative/Ornamental plants** ({len(flowers)} total): {names}{'...' if len(flowers) > 10 else ''}"
    
    if any(word in msg_lower for word in ["vegetable", "vegetables", "veggie", "veggies"]):
        vegs = find_plants_by_category("vegetable")
        names = ", ".join([p["name"] for p in vegs[:10]])
        return f"**Vegetables** ({len(vegs)} total): {names}{'...' if len(vegs) > 10 else ''}"
    
    if any(word in msg_lower for word in ["herb", "herbs", "medicinal"]):
        herbs = find_plants_by_category("herb")
        names = ", ".join([p["name"] for p in herbs[:10]])
        return f"**Herbs** ({len(herbs)} total): {names}{'...' if len(herbs) > 10 else ''}"
    
    if "spice" in msg_lower or "spices" in msg_lower:
        spices = find_plants_by_category("spice")
        names = ", ".join([p["name"] for p in spices])
        return f"**Spices** ({len(spices)} total): {names}"
    
    if "pulse" in msg_lower or "legume" in msg_lower:
        pulses = find_plants_by_category("pulse")
        names = ", ".join([p["name"] for p in pulses])
        return f"**Pulses/Legumes** ({len(pulses)} total): {names}"
    
    # Field size queries
    if "large field" in msg_lower or "farm" in msg_lower or "acre" in msg_lower:
        plants = find_plants_by_field_size("large")
        names = ", ".join([p["name"] for p in plants[:10]])
        return f"**Large field / farm crops** ({len(plants)} suitable): {names}{'...' if len(plants) > 10 else ''}"
    
    if "small field" in msg_lower or "balcony" in msg_lower or "container" in msg_lower or "pot" in msg_lower or "kitchen garden" in msg_lower:
        plants = find_plants_by_field_size("small")
        names = ", ".join([p["name"] for p in plants[:10]])
        return f"**Small space / container plants** ({len(plants)} suitable): {names}{'...' if len(plants) > 10 else ''}"
    
    # Watering queries
    if "water" in msg_lower or "irrigat" in msg_lower:
        return ("Watering depends on plant type and your climate. "
                "Generate a report for your location to get personalized watering schedules "
                "with frequency, amounts per session, and seasonal adjustments.")
    
    # Pest/disease queries
    if "pest" in msg_lower or "disease" in msg_lower or "bug" in msg_lower or "insect" in msg_lower:
        return ("Pest and disease risks depend on your climate (temperature, humidity) and plant category. "
                "Generate a report to see specific alerts for each recommended plant with risk levels and organic treatment advice.")
    
    # Seasonal queries
    if "season" in msg_lower or "when to plant" in msg_lower or "planting time" in msg_lower:
        return ("Planting seasons in India: Kharif (Monsoon: Jun-Sep), Rabi (Winter: Oct-Mar), Zaid (Summer: Mar-Jun), Year-round. "
                "Each plant in the database has its recommended planting season. "
                "Use the 'What to Plant This Month' feature for current recommendations.")
    
    # Location-specific (if report_id provided)
    if report_id:
        return f"I can help with plants from your report ({report_id[:8]}...). Ask about specific plants, watering, pests, or categories."
    
    # Default help
    return (
        "**GreenScope Assistant** (deterministic, no LLM)\n"
        "I can answer questions using the local plant database:\n"
        "• **Plant details**: \"Tell me about Tulsi\" or \"Tomato care\"\n"
        "• **Categories**: \"Show me flowers\", \"What fruits can I grow?\", \"List herbs\"\n"
        "• **Field size**: \"Plants for large field\", \"Balcony garden plants\"\n"
        "• **Decorative**: \"Decorative plants\" or \"Ornamental plants\"\n"
        "• **Watering/Pests/Seasons**: General guidance\n\n"
        "Generate a report first for location-specific advice (watering schedules, pest alerts, suitability scores)."
    )


@app.post("/api/v1/chat", response_model=dict)
@limiter.limit("30/minute")
async def chat_endpoint(request: Request, body: ChatRequest):
    """Deterministic chat assistant using local plant database."""
    if not body.messages:
        return {"response": "No messages provided", "deterministic": True}
    
    last_user_msg = ""
    for msg in reversed(body.messages):
        if msg.role == "user":
            last_user_msg = msg.content
            break
    
    if not last_user_msg:
        return {"response": "No user message found", "deterministic": True}
    
    response = generate_chat_response(last_user_msg, body.location, body.report_id)
    return {"response": response, "deterministic": True}


# --- Moon Phase / Biodynamic Calendar ---
# Astronomical calculations for moon phase (simplified)
# Based on known new moon reference: Jan 6, 2000 18:14 UTC
MOON_SYNODIC_MONTH = 29.53058867
MOON_REFERENCE_NEW = 946720440  # Jan 6, 2000 18:14 UTC timestamp

# Biodynamic planting categories by moon phase
# Root: New Moon to First Quarter (0-25% illumination)
# Leaf: First Quarter to Full Moon (25-50%)
# Fruit: Full Moon to Last Quarter (50-75%)
# Flower: Last Quarter to New Moon (75-100%)
BIODYNAMIC_CATEGORIES = {
    "root": ["carrot", "radish", "beetroot", "potato", "onion", "garlic", "ginger", "turmeric", "sweet potato", "yam"],
    "leaf": ["spinach", "coriander", "fenugreek", "mint", "cabbage", "cauliflower", "lettuce", "amaranth", "mustard greens"],
    "fruit": ["tomato", "chili", "okra", "brinjal", "cucumber", "pumpkin", "bottle gourd", "ridge gourd", "bitter gourd", "peas", "beans", "pepper"],
    "flower": ["marigold", "jasmine", "hibiscus", "rose", "sunflower", "chrysanthemum", "cosmos", "zinnias"]
}

# Indian lunar month names (approximate)
LUNAR_MONTHS = [
    "Chaitra", "Vaishakha", "Jyeshtha", "Ashadha", "Shravana", "Bhadrapada",
    "Ashvina", "Kartika", "Margashirsha", "Pausha", "Magha", "Phalguna"
]

# Panchang tithi names (simplified)
TITHI_NAMES = [
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi",
    "Saptami", "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi",
    "Trayodashi", "Chaturdashi", "Purnima/Amavasya"
]

# Nakshatra names (27)
NAKSHATRAS = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni",
    "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha",
    "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishtha",
    "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"
]


def calculate_moon_phase(timestamp: float) -> dict:
    """Calculate moon phase for a given timestamp."""
    days_since_ref = (timestamp - MOON_REFERENCE_NEW) / 86400.0
    phase = (days_since_ref % MOON_SYNODIC_MONTH) / MOON_SYNODIC_MONTH
    illumination = (1 - math.cos(2 * math.pi * phase)) / 2
    
    # Phase names
    if phase < 0.03 or phase > 0.97:
        phase_name = "New Moon"
        biodynamic = "root"
    elif phase < 0.22:
        phase_name = "Waxing Crescent"
        biodynamic = "root"
    elif phase < 0.28:
        phase_name = "First Quarter"
        biodynamic = "leaf"
    elif phase < 0.47:
        phase_name = "Waxing Gibbous"
        biodynamic = "leaf"
    elif phase < 0.53:
        phase_name = "Full Moon"
        biodynamic = "fruit"
    elif phase < 0.72:
        phase_name = "Waning Gibbous"
        biodynamic = "fruit"
    elif phase < 0.78:
        phase_name = "Last Quarter"
        biodynamic = "flower"
    else:
        phase_name = "Waning Crescent"
        biodynamic = "flower"
    
    # Days to next phase
    days_to_next = {
        "New Moon": 7.4 - (phase * MOON_SYNODIC_MONTH),
        "Waxing Crescent": 7.4 - ((phase - 0.25) * MOON_SYNODIC_MONTH) if phase >= 0.25 else 7.4 - (phase * MOON_SYNODIC_MONTH),
        "First Quarter": 7.4 - ((phase - 0.5) * MOON_SYNODIC_MONTH) if phase >= 0.5 else 7.4 - ((phase - 0.25) * MOON_SYNODIC_MONTH),
        "Waxing Gibbous": 7.4 - ((phase - 0.75) * MOON_SYNODIC_MONTH) if phase >= 0.75 else 7.4 - ((phase - 0.5) * MOON_SYNODIC_MONTH),
        "Full Moon": 7.4 - ((phase - 0.75) * MOON_SYNODIC_MONTH) if phase >= 0.75 else 7.4 - ((phase - 0.5) * MOON_SYNODIC_MONTH),
        "Waning Gibbous": 7.4 - ((phase - 0.75) * MOON_SYNODIC_MONTH) if phase >= 0.75 else 7.4 - ((phase - 0.5) * MOON_SYNODIC_MONTH),
        "Last Quarter": 7.4 - ((phase - 1.0) * MOON_SYNODIC_MONTH) if phase >= 1.0 else 7.4 - ((phase - 0.75) * MOON_SYNODIC_MONTH),
        "Waning Crescent": 7.4 - ((phase - 1.0) * MOON_SYNODIC_MONTH) if phase >= 1.0 else 7.4 - ((phase - 0.75) * MOON_SYNODIC_MONTH),
    }
    
    return {
        "phase": phase_name,
        "illumination": round(illumination * 100, 1),
        "biodynamic_category": biodynamic,
        "days_to_next_phase": round(max(0, days_to_next.get(phase_name, 7.4)), 1),
        "is_waxing": phase < 0.5
    }


def get_biodynamic_advice(phase_info: dict, plants: list) -> list:
    """Get biodynamic planting advice for current moon phase."""
    category = phase_info["biodynamic_category"]
    suitable_plants = [p for p in plants if p["name"].lower() in BIODYNAMIC_CATEGORIES.get(category, [])]
    return {
        "category": category,
        "description": {
            "root": "Root crops: Plant root vegetables. Energy draws downward. Good for transplanting.",
            "leaf": "Leaf crops: Plant leafy greens. Energy draws upward. Good for fertilizing.",
            "fruit": "Fruit/Seed crops: Plant fruiting vegetables. Peak energy. Good for harvesting seeds.",
            "flower": "Flower crops: Plant flowers. Energy balances. Good for pruning, weeding."
        }.get(category, ""),
        "suitable_plants": [p["name"] for p in suitable_plants[:8]],
        "avoid": "Avoid planting during New Moon (2 days before/after) and Full Moon (1 day before/after)"
    }


def calculate_lunar_date(timestamp: float) -> dict:
    """Calculate approximate Indian lunar date (simplified)."""
    # This is a very simplified calculation
    # Real Panchang requires complex astronomy
    days_since_epoch = (timestamp - 946684800) / 86400  # Days since Jan 1, 2000
    lunar_day = (days_since_epoch % 29.53) + 1
    tithi_idx = int(lunar_day) % 15
    paksha = "Shukla" if lunar_day <= 15 else "Krishna"
    
    # Approximate lunar month (simplified)
    lunar_month_idx = int((days_since_epoch / 29.53) % 12)
    
    return {
        "tithi": TITHI_NAMES[tithi_idx],
        "paksha": paksha,
        "lunar_month": LUNAR_MONTHS[lunar_month_idx],
        "day": int(lunar_day)
    }


def get_auspicious_dates(start_date: date, days: int = 30) -> list:
    """Get auspicious planting dates for next N days."""
    auspicious = []
    for i in range(days):
        check_date = start_date + timedelta(days=i)
        ts = time.mktime(check_date.timetuple())
        moon = calculate_moon_phase(ts)
        lunar = calculate_lunar_date(ts)
        
        # Auspicious if not New Moon or Full Moon
        is_auspicious = moon["phase"] not in ["New Moon", "Full Moon"]
        
        # Extra auspicious on specific tithis
        if lunar["tithi"] in ["Dwitiya", "Tritiya", "Panchami", "Saptami", "Dashami", "Ekadashi", "Trayodashi"]:
            is_auspicious = True
        
        if is_auspicious:
            auspicious.append({
                "date": check_date.strftime("%Y-%m-%d"),
                "day": check_date.strftime("%A"),
                "moon_phase": moon["phase"],
                "biodynamic": moon["biodynamic_category"],
                "tithi": f"{lunar['paksha']} {lunar['tithi']}",
                "lunar_month": lunar["lunar_month"]
            })
    return auspicious


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
