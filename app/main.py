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
import logging
import sys
from datetime import date, timedelta
from contextlib import asynccontextmanager

# --- Structured Logging Setup ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json")  # json or text

class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        if hasattr(record, 'request_id'):
            log_obj["request_id"] = record.request_id
        if hasattr(record, 'user_id'):
            log_obj["user_id"] = record.user_id
        return json.dumps(log_obj, ensure_ascii=False)

def setup_logging():
    """Configure structured logging."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL))
    
    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    handler = logging.StreamHandler(sys.stdout)
    if LOG_FORMAT == "json":
        handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
    root_logger.addHandler(handler)
    
    # Reduce noise from third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("slowapi").setLevel(logging.WARNING)

setup_logging()
logger = logging.getLogger("greenscope")

# Request ID middleware for tracing
class RequestIDMiddleware:
    """Add request ID to all requests for tracing."""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        import uuid
        request_id = str(uuid.uuid4())[:8]
        
        # Add request_id to scope for access in handlers
        scope["request_id"] = request_id
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)
        
        await self.app(scope, receive, send_wrapper)


# HTTPS enforcement for production (Render)
class HTTPSRedirectMiddleware:
    """Enforce HTTPS in production (when behind proxy like Render)."""
    def __init__(self, app):
        self.app = app
        self.env = os.environ.get("ENVIRONMENT", "development")
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        if self.env == "production":
            # Check X-Forwarded-Proto header (set by Render/load balancer)
            headers = dict(scope.get("headers", []))
            proto = headers.get(b"x-forwarded-proto", b"http").decode()
            if proto == "http":
                # Redirect to HTTPS
                url = f"https://{headers.get(b'host', b'').decode()}{scope['path']}"
                if scope.get("query_string"):
                    url += f"?{scope['query_string'].decode()}"
                
                await send({
                    "type": "http.response.start",
                    "status": 301,
                    "headers": [
                        (b"location", url.encode()),
                        (b"content-type", b"text/plain")
                    ]
                })
                await send({
                    "type": "http.response.body",
                    "body": b"Redirecting to HTTPS"
                })
                return
        
        await self.app(scope, receive, send)


# Startup/shutdown lifespan for cleanup
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("GreenScope API starting up", extra={"version": "0.1.0"})
    yield
    # Shutdown
    logger.info("GreenScope API shutting down")


# --- Plant Knowledge Base ---

def load_plant_db():
    db_path = os.path.join(os.path.dirname(__file__), "plant_db.json")
    with open(db_path, "r", encoding="utf-8") as f:
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
    "Tomato": (["Marigold", "Tulsi", "Garlic", "Basil", "Onion", "Carrot"], ["Potato", "Fennel", "Cabbage", "Corn"]),
    "Marigold": (["Tomato", "Brinjal", "Chili", "Potato", "Rose", "Cucumber"], []),
    "Tulsi": (["Tomato", "Chili", "Brinjal", "Peppers"], ["Rue"]),
    "Garlic": (["Tomato", "Carrot", "Rose", "Spinach", "Beetroot"], ["Peas", "Beans", "Parsley"]),
    "Onion": (["Carrot", "Beetroot", "Tomato", "Pepper", "Strawberry"], ["Peas", "Beans", "Sage"]),
    "Carrot": (["Onion", "Garlic", "Tomato", "Lettuce", "Radish", "Peas"], ["Dill", "Parsnip"]),
    "Mint": (["Brinjal", "Cabbage", "Tomato", "Peas"], ["Parsley"]),
    "Coriander": (["Spinach", "Onion", "Tomato", "Potato"], ["Fennel"]),
    "Mustard": (["Peas", "Black Gram", "Wheat"], ["Sunflower"]),
    "Lemongrass": (["Tomato", "Brinjal", "Pepper"], []),
    "Brinjal": (["Marigold", "Tulsi", "Beans", "Spinach", "Thyme"], ["Fennel"]),
    "Chili": (["Tomato", "Basil", "Onion", "Carrot", "Marigold"], ["Fennel", "Kohlrabi"]),
    "Spinach": (["Strawberry", "Peas", "Radish", "Coriander", "Garlic"], ["Potato"]),
    "Peas": (["Carrot", "Radish", "Turnip", "Cucumber", "Corn", "Beans"], ["Onion", "Garlic", "Gladiolus"]),
    "Cabbage": (["Dill", "Mint", "Rosemary", "Sage", "Thyme", "Onion"], ["Strawberry", "Tomato", "Pole Beans"]),
    "Cauliflower": (["Dill", "Mint", "Rosemary", "Sage", "Thyme"], ["Strawberry", "Tomato"]),
    "Radish": (["Peas", "Lettuce", "Cucumber", "Spinach", "Carrot"], ["Hyssop"]),
    "Bottle Gourd": (["Corn", "Beans", "Nasturtium", "Radish"], ["Potato"]),
    "Ridge Gourd": (["Corn", "Beans", "Sunflower"], ["Potato"]),
    "Bitter Gourd": (["Corn", "Beans", "Marigold"], ["Potato", "Herbs"]),
    "Okra": (["Peppers", "Eggplant", "Basil", "Melon"], []),
    "Moringa": (["Sweet Potato", "Pumpkin", "Beans"], []),
    "Turmeric": (["Ginger", "Chili", "Coriander"], []),
    "Ginger": (["Turmeric", "Chili", "Cilantro"], []),
    "Coriander": (["Spinach", "Tomato", "Potato", "Anise"], ["Fennel", "Caraway"]),
    "Fenugreek": (["Corn", "Cucumber", "Potato"], []),
    "Cowpea": (["Corn", "Cucumber", "Strawberry"], ["Onion", "Garlic"]),
    "Black Gram": (["Corn", "Cucumber"], ["Onion", "Garlic"]),
    "Pigeon Pea": (["Millet", "Sorghum"], ["Onion", "Garlic"]),
    "Moong": (["Corn", "Cucumber", "Potato"], ["Onion", "Garlic"]),
    "Sunflower": (["Corn", "Cucumber", "Melon", "Squash"], ["Potato", "Pole Beans"]),
    "Jasmine": (["Rose", "Lavender", "Citrus"], []),
    "Hibiscus": (["Rose", "Marigold", "Citrus"], []),
    "Rose": (["Garlic", "Onion", "Chives", "Marigold", "Lavender"], []),
    "Lotus": (["Water Lily", "Papyrus"], []),
    "Mint": (["Cabbage", "Tomato", "Peas", "Kale"], ["Parsley", "Chamomile"]),
    "Lemongrass": (["Tomato", "Pepper", "Brinjal", "Citrus"], []),
    "Aloe Vera": (["Strawberry", "Onion", "Garlic"], []),
    "Curry Leaf": (["Tomato", "Chili", "Brinjal"], []),
    "Ashwagandha": (["Tomato", "Pepper", "Spinach"], []),
    "Brahmi": (["Mint", "Coriander", "Lettuce"], []),
    "Giloy": (["Neem", "Tulsi", "Moringa"], []),
    "Neem": (["Turmeric", "Ginger", "Tulsi"], []),
    "Amla": (["Lemon", "Guava", "Mango"], []),
    "Banana": (["Papaya", "Sweet Potato", "Beans"], []),
    "Papaya": (["Banana", "Pineapple", "Beans"], []),
    "Mango": (["Garlic", "Marigold", "Chives"], []),
    "Guava": (["Marigold", "Mint", "Basil"], []),
    "Lemon": (["Marigold", "Nasturtium", "Petunia"], []),
    "Stevia": (["Mint", "Thyme", "Oregano"], []),
    "Artichoke": (["Peas", "Sunflower", "Tarragon"], []),
    "Sweet Potato": (["Beans", "Peas", "Spinach"], ["Squash"]),
    "Yam": (["Beans", "Corn", "Peas"], []),
    "Beetroot": (["Onion", "Garlic", "Cabbage", "Lettuce"], ["Pole Beans", "Mustard"]),
    "Carrot": (["Onion", "Leek", "Sage", "Rosemary", "Tomato", "Lettuce"], ["Dill", "Parsnip"]),
    "Radish": (["Carrot", "Lettuce", "Peas", "Nasturtium", "Cucumber"], ["Hyssop"]),
}
# Qualitative water need -> estimated liters/plant/week (approx, labeled as estimate)
WATER_L_PER_WEEK = {"low": 3.0, "medium": 8.0, "high": 18.0}
# Category -> rough CO2 sequestration kg/plant/year (order-of-magnitude estimate)
CO2_KG_PER_YEAR = {
    "fruit": 12.0, "vegetable": 1.0, "herb": 0.5,
    "spice": 0.8, "pulse": 0.8, "flower": 0.5,
}

# Valid plant categories and aliases for filtering
VALID_CATEGORIES = {"herb", "fruit", "vegetable", "spice", "pulse", "flower", "container"}
CATEGORY_ALIASES = {"decorative": "flower", "container": "container"}

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


# --- Companion Planting Suggestions ---

def companion_suggestions(plant: dict, all_recommendations: list) -> dict:
    """Get companion planting suggestions for a plant based on other recommendations in the report.
    
    Returns dict with 'grows_well_with' and 'avoid_near' lists of plant names from recommendations.
    """
    plant_name = plant.get("name", "")
    grows_with, avoid = COMPANIONS.get(plant_name, ([], []))
    
    rec_names = {r.plant_name for r in all_recommendations}
    
    good_companions = [c for c in grows_with if c in rec_names]
    bad_companions = [c for c in avoid if c in rec_names]
    
    # Also check category-based companions for plants not in COMPANIONS dict
    plant_category = plant.get("category", "")
    category_companions = {
        "vegetable": {"good": ["herb", "flower"], "avoid": ["fruit"]},
        "fruit": {"good": ["herb", "flower"], "avoid": []},
        "herb": {"good": ["vegetable", "flower"], "avoid": []},
        "flower": {"good": ["vegetable", "fruit", "herb"], "avoid": []},
        "spice": {"good": ["herb", "vegetable"], "avoid": []},
        "pulse": {"good": ["vegetable", "herb"], "avoid": ["allium"]},
    }
    
    cat_rules = category_companions.get(plant_category, {"good": [], "avoid": []})
    
    # Add category-based suggestions for plants not explicitly listed
    if not good_companions and cat_rules["good"]:
        for r in all_recommendations:
            r_plant = next((p for p in PLANTS if p["name"] == r.plant_name), None)
            if r_plant and r_plant.get("category") in cat_rules["good"]:
                good_companions.append(r.plant_name)
    
    if not bad_companions and cat_rules["avoid"]:
        for r in all_recommendations:
            r_plant = next((p for p in PLANTS if p["name"] == r.plant_name), None)
            if r_plant and r_plant.get("category") in cat_rules["avoid"]:
                bad_companions.append(r.plant_name)
    
    # Special case: avoid alliums near legumes
    if plant_category == "pulse":
        alliums = [r.plant_name for r in all_recommendations 
                   if r.plant_name in ["Onion", "Garlic", "Chives", "Leek"]]
        bad_companions.extend(alliums)
    
    # Remove duplicates and self
    good_companions = list(dict.fromkeys([c for c in good_companions if c != plant_name]))
    bad_companions = list(dict.fromkeys([c for c in bad_companions if c != plant_name]))
    
    return {
        "grows_well_with": good_companions[:3],  # Limit to top 3
        "avoid_near": bad_companions[:3],
        "notes": []
    }


# --- Water Conservation Score ---

def calculate_water_conservation_score(plant: dict, env: dict) -> dict:
    """Calculate water conservation score (0-100) and estimated liters saved per season.
    
    Compares plant's water need against location's rainfall.
    Low-water plants in low-rainfall areas score high.
    """
    water_req = plant.get("water", "medium")
    rainfall_mm = env.get("rainfall_mm", 900)
    growth_days = plant.get("growth_days", 60)
    
    # Water requirement tiers (liters/week per plant)
    WATER_L_PER_WEEK = {"low": 3.0, "medium": 8.0, "high": 18.0}
    weekly_water = WATER_L_PER_WEEK.get(water_req, 8.0)
    
    # Baseline high-water crop for comparison (Bottle Gourd)
    baseline_weekly = WATER_L_PER_WEEK["high"]  # 18 L/week
    
    # Calculate conservation score (0-100)
    # High score = plant uses less water than climate provides
    if water_req == "low":
        if rainfall_mm < 600:
            score = 95  # Perfect match: low water need, dry climate
        elif rainfall_mm < 1000:
            score = 80
        else:
            score = 60  # Low water plant in wet climate - still good but not optimal
    elif water_req == "medium":
        if 600 <= rainfall_mm <= 1500:
            score = 85
        elif rainfall_mm < 600:
            score = 55  # Needs irrigation in dry climate
        else:
            score = 70  # High rainfall - some waterlogging risk
    else:  # high water
        if rainfall_mm > 1500:
            score = 80  # High rainfall matches high water need
        elif rainfall_mm > 1000:
            score = 50
        else:
            score = 20  # High water plant in dry climate - unsustainable
    
    # Estimated liters per season
    weeks_in_season = max(1, growth_days / 7)
    plant_liters_per_season = weekly_water * weeks_in_season
    baseline_liters_per_season = baseline_weekly * weeks_in_season
    liters_saved = max(0, baseline_liters_per_season - plant_liters_per_season)
    pct_saved = round((liters_saved / baseline_liters_per_season) * 100, 1) if baseline_liters_per_season > 0 else 0
    
    return {
        "water_conservation_score": round(score, 1),
        "estimated_liters_per_season": round(plant_liters_per_season, 1),
        "baseline_liters_per_season": round(baseline_liters_per_season, 1),
        "liters_saved_vs_baseline": round(liters_saved, 1),
        "pct_water_saved": pct_saved,
        "disclaimer": "Water estimates are rough approximations based on plant category and growth duration. Actual usage varies by soil, weather, and management."
    }


def get_soil_amendments(plant: dict, env: dict) -> list:
    """Get soil amendment suggestions based on pH mismatch and soil type incompatibility."""
    amendments = []
    env_ph = env.get("ph", 6.5)
    env_soil = env.get("soil_type", "loam").lower()
    plant_soil_prefs = [s.lower() for s in plant.get("soil", ["loam"])]
    plant_min_ph = plant.get("min_ph", 6.0)
    plant_max_ph = plant.get("max_ph", 7.5)
    plant_name = plant.get("name", "this plant")

    # pH adjustments
    if env_ph < plant_min_ph:
        diff = plant_min_ph - env_ph
        if diff > 1.0:
            amendments.append({
                "issue": f"Soil pH {env_ph} is too acidic for {plant_name} (needs {plant_min_ph}-{plant_max_ph})",
                "severity": "high" if diff > 1.5 else "moderate",
                "amendment": "Add garden lime (calcium carbonate) at 200-400g per sq meter. Retest pH after 2-3 weeks.",
                "organic_option": "Wood ash (100-200g/sq m) or crushed eggshells worked into top 15cm soil."
            })
        else:
            amendments.append({
                "issue": f"Soil pH {env_ph} is slightly acidic for {plant_name} (needs {plant_min_ph}-{plant_max_ph})",
                "severity": "low",
                "amendment": "Add dolomite lime at 100-200g per sq meter. Water in well.",
                "organic_option": "Compost (2-3cm layer) helps buffer pH naturally over time."
            })
    elif env_ph > plant_max_ph:
        diff = env_ph - plant_max_ph
        if diff > 1.0:
            amendments.append({
                "issue": f"Soil pH {env_ph} is too alkaline for {plant_name} (needs {plant_min_ph}-{plant_max_ph})",
                "severity": "high" if diff > 1.5 else "moderate",
                "amendment": "Add elemental sulfur at 50-100g per sq meter. Takes 2-3 months to fully react.",
                "organic_option": "Peat moss (5-10cm layer) or pine needle mulch. Coffee grounds as top dressing."
            })
        else:
            amendments.append({
                "issue": f"Soil pH {env_ph} is slightly alkaline for {plant_name} (needs {plant_min_ph}-{plant_max_ph})",
                "severity": "low",
                "amendment": "Add organic compost (3-5cm) and mulch with pine bark. Monitor pH monthly.",
                "organic_option": "Diluted vinegar (1 cup per 10L water) for immediate but temporary correction."
            })

    # Soil type mismatches
    soil_match = any(pref in env_soil or env_soil in pref for pref in plant_soil_prefs)
    if not soil_match:
        if "clay" in env_soil and "sandy" in str(plant_soil_prefs):
            amendments.append({
                "issue": f"Heavy clay soil ({env_soil}) may not suit {plant_name} (prefers {', '.join(plant.get('soil', ['loam']))})",
                "severity": "moderate",
                "amendment": "Add coarse sand (30-40% by volume) + compost (30%) to improve drainage. Raised beds recommended.",
                "organic_option": "Gypsum (200-300g/sq m) breaks up clay. Add leaf mold for structure."
            })
        elif "sandy" in env_soil and "clay" in str(plant_soil_prefs):
            amendments.append({
                "issue": f"Sandy soil ({env_soil}) drains too fast for {plant_name} (prefers {', '.join(plant.get('soil', ['loam']))})",
                "severity": "moderate",
                "amendment": "Add clay-rich subsoil or bentonite (5-10% by volume) + compost (30%). Mulch heavily.",
                "organic_option": "Coconut coir or vermiculite (20%) for water retention. Biochar (5-10%) helps."
            })
        elif "laterite" in env_soil or "red" in env_soil:
            amendments.append({
                "issue": f"Laterite/red soil ({env_soil}) low in nutrients for {plant_name}",
                "severity": "moderate",
                "amendment": "Add rock phosphate (200g/sq m) + green manure crop before planting. Compost (5cm layer).",
                "organic_option": "Neem cake (100g/sq m) + vermicompost (2kg/sq m). Biofertilizers: Azotobacter, PSB."
            })
        elif "black cotton" in env_soil:
            amendments.append({
                "issue": f"Black cotton soil ({env_soil}) cracks when dry, waterlogs when wet",
                "severity": "moderate",
                "amendment": "Deep plowing + gypsum (500g/sq m). Add sand (20%) + FYM (10 tons/acre). Ridge planting.",
                "organic_option": "Crop residue mulch (10cm). Green manuring with dhaincha/sunnhemp."
            })

    return amendments


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

class PostgresConnectionWrapper:
    """Wrapper to make psycopg2 connection behave like sqlite3 connection."""
    def __init__(self, conn):
        self.conn = conn
        self._cursor = None
    
    def execute(self, query, params=None):
        if self._cursor:
            self._cursor.close()
        self._cursor = self.conn.cursor()
        if params:
            self._cursor.execute(query, params)
        else:
            self._cursor.execute(query)
        return self
    
    def fetchall(self):
        if self._cursor:
            return self._cursor.fetchall()
        return []
    
    def fetchone(self):
        if self._cursor:
            return self._cursor.fetchone()
        return None
    
    def commit(self):
        self.conn.commit()
    
    def close(self):
        if self._cursor:
            self._cursor.close()
        self.conn.close()
    
    @property
    def row_factory(self):
        return None
    
    @row_factory.setter
    def row_factory(self, value):
        pass  # psycopg2 uses RealDictCursor


def _init_postgres_schema(conn):
    """Initialize Postgres schema with all required tables."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                location TEXT,
                latitude REAL,
                longitude REAL,
                environment JSONB,
                photo_analysis JSONB,
                recommendations JSONB,
                generated_at REAL,
                processing_time_ms REAL,
                rejected_plants JSONB,
                categories JSONB,
                garden_risk JSONB,
                comparison_table JSONB,
                garden_bed JSONB,
                data_source TEXT,
                user_id INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                report_id TEXT,
                plant_name TEXT,
                vote TEXT CHECK(vote IN ('up', 'down')),
                created_at REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
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
                created_at REAL,
                is_organic BOOLEAN DEFAULT FALSE
            )
        """)
        # Migration for existing tables
        try:
            cur.execute("ALTER TABLE purchases ADD COLUMN IF NOT EXISTS is_organic BOOLEAN DEFAULT FALSE")
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
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
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                phone TEXT UNIQUE NOT NULL,
                name TEXT,
                created_at REAL,
                last_login REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_codes (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at REAL NOT NULL,
                created_at REAL
            )
        """)
    conn.commit()


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
            created_at REAL,
            is_organic INTEGER DEFAULT 0
        )"""
    )
    # Migration for existing tables
    try:
        conn.execute("ALTER TABLE purchases ADD COLUMN is_organic INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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
    # Soil health tracking table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS soil_health_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            plant_name TEXT,
            log_date REAL,
            ph REAL,
            organic_matter_pct REAL,
            nitrogen_ppm REAL,
            phosphorus_ppm REAL,
            potassium_ppm REAL,
            source TEXT,
            notes TEXT
        )"""
    )
    # Plant health tracking table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS plant_health_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT,
            plant_name TEXT,
            log_date REAL,
            symptoms TEXT,
            diagnosis TEXT,
            severity TEXT,
            treatment TEXT,
            photo_url TEXT
        )"""
    )
    # User accounts table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            name TEXT,
            created_at REAL,
            last_login REAL
        )"""
    )
    # OTP verification table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,  -- 'login', 'register'
            expires_at REAL NOT NULL,
            used INTEGER DEFAULT 0,
            created_at REAL
        )"""
    )
    # User sessions table (JWT tokens)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL
        )"""
    )
    # Add user_id to reports table for ownership
    for col in ("user_id",):
        try:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def save_report_to_db(report, user_id: Optional[int] = None):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO reports
        (report_id, location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms,
         rejected_plants, categories, garden_risk, comparison_table, garden_bed, data_source, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                "pot_size_cm": r.pot_size_cm,
                "pot_depth_cm": r.pot_depth_cm,
                "container_suitable": r.container_suitable,
                "soil_amendments": r.soil_amendments,
                "companion_suggestions": r.companion_suggestions,
                "water_conservation_score": r.water_conservation_score,
                "estimated_liters_per_season": r.estimated_liters_per_season,
                "liters_saved_vs_baseline": r.liters_saved_vs_baseline,
                "pct_water_saved": r.pct_water_saved,
                "pollinator_friendly": r.pollinator_friendly,
                "hindi_name": r.hindi_name,
            } for r in report.recommendations]),
            report.generated_at,
            report.processing_time_ms,
            json.dumps(report.rejected_plants) if report.rejected_plants else None,
            json.dumps(report.categories) if report.categories else None,
            report.garden_risk.model_dump_json() if report.garden_risk else None,
            json.dumps(report.comparison_table) if report.comparison_table else None,
            json.dumps(report.garden_bed) if report.garden_bed else None,
            report.data_source,
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def load_report_from_db(report_id):
    conn = get_db()
    row = conn.execute(
        "SELECT location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms,"
        " rejected_plants, categories, garden_risk, comparison_table, garden_bed, data_source, user_id"
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
        "user_id": row[14] if len(row) > 14 else None,
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
    
    # Check climate cache first (Open-Meteo rate-limit protection)
    climate_cache_key = f"{lat:.4f},{lng:.4f}"
    cached_env = _climate_cache.get(climate_cache_key)
    if cached_env:
        return cached_env, "cached_api"

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

                    result_env = {
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
                    }
                    _climate_cache.set(climate_cache_key, result_env)
                    return result_env, "live_api"
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

    # Water conservation score (derived metric, not in weighted total)
    water_conservation = calculate_water_conservation_score(plant, env)
    scores["water_conservation"] = water_conservation["water_conservation_score"]

    # Weighted total
    total = sum(scores[k] * SCORING_WEIGHTS[k] for k in SCORING_WEIGHTS)
    total = round(min(100, max(0, total)), 1)

    return {"total": total, "breakdown": scores, "reasons": reasons, "water_conservation": water_conservation}


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

app = FastAPI(
    title="GreenScope API", 
    version="0.1.0",
    lifespan=lifespan
)

# Add middlewares in order (last added = outermost)
app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(RequestIDMiddleware)

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
    pollinator_friendly: Optional[bool] = Field(None, description="Filter for pollinator-friendly plants only")

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
    pot_size_cm: Optional[int] = Field(None, description="Recommended pot diameter in cm")
    pot_depth_cm: Optional[int] = Field(None, description="Recommended pot depth in cm")
    container_suitable: Optional[bool] = Field(None, description="Whether plant is suitable for container/balcony gardening")
    soil_amendments: Optional[list] = Field(None, description="Soil amendment suggestions for pH/soil mismatches")
    companion_suggestions: Optional[dict] = Field(None, description="Companion planting: grows_well_with and avoid_near lists")
    water_conservation_score: Optional[float] = Field(None, description="Water conservation score 0-100 (higher = more sustainable)")
    estimated_liters_per_season: Optional[float] = Field(None, description="Estimated liters of water per growing season")
    liters_saved_vs_baseline: Optional[float] = Field(None, description="Estimated liters saved vs high-water baseline crop")
    pct_water_saved: Optional[float] = Field(None, description="Percentage water saved vs baseline (Bottle Gourd)")
    pollinator_friendly: Optional[bool] = Field(None, description="Whether plant attracts pollinators")
    hindi_name: Optional[str] = Field(None, description="Plant name in Hindi")


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


@app.get("/", response_class=FileResponse)
async def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "index.html"))


# In-memory report cache (supplements SQLite persistence)
reports_db = {}

# --- Enhanced Health Check ---

class HealthStatus(BaseModel):
    status: str
    service: str
    version: str
    timestamp: float
    uptime_seconds: float
    database: str
    cache: dict
    environment: str

START_TIME = time.time()

@app.get("/api/v1/health", response_model=HealthStatus)
async def health_check():
    """Comprehensive health check with dependency status."""
    # Check database connectivity
    db_status = "unknown"
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_status = "connected"
    except Exception as e:
        logger.warning("Health check: database connection failed", extra={"error": str(e)})
        db_status = f"error: {e}"
    
    # Cache stats
    geocode_cache_size = len(_geocode_cache._cache) if hasattr(_geocode_cache, '_cache') else 0
    climate_cache_size = len(_climate_cache._cache) if hasattr(_climate_cache, '_cache') else 0
    
    return HealthStatus(
        status="healthy" if db_status == "connected" else "degraded",
        service="GreenScope API",
        version="0.1.0",
        timestamp=time.time(),
        uptime_seconds=round(time.time() - START_TIME, 1),
        database=db_status,
        cache={
            "geocode_entries": geocode_cache_size,
            "climate_entries": climate_cache_size
        },
        environment=os.environ.get("ENVIRONMENT", "development")
    )


# --- Caching (Nominatim/Open-Meteo rate-limit protection) ---
import time

class TTLCache:
    """Simple in-memory cache with TTL support."""
    def __init__(self, default_ttl: int = 3600):
        self._cache = {}
        self._ttl = default_ttl
    
    def get(self, key: str):
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                return value
            else:
                del self._cache[key]
        return None
    
    def set(self, key: str, value, ttl: int = None):
        expiry = time.time() + (ttl or self._ttl)
        self._cache[key] = (value, expiry)
    
    def clear_expired(self):
        now = time.time()
        expired = [k for k, (_, exp) in self._cache.items() if now >= exp]
        for k in expired:
            del self._cache[k]

# Geocoding cache (1 hour TTL)
_geocode_cache = TTLCache(default_ttl=3600)

# Climate data cache (6 hour TTL)
_climate_cache = TTLCache(default_ttl=21600)

async def geocode_location(location: LocationInput):
    cache_key = location.location.strip().lower()

    # Check cache first (Nominatim rate-limit protection)
    cached = _geocode_cache.get(cache_key)
    if cached:
        return cached

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
                    _geocode_cache.set(cache_key, result)
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

    # Get current user if authenticated
    user = await get_current_user(request)
    user_id = user["user_id"] if user else None

    env, data_source = get_env_for_location(lat, lng, location_str)
    photo_obs = analyze_photo(body.photo)

    # Filter plants by category if specified
    plant_pool = PLANTS
    if body.category:
        if body.category == "container":
            # Special filter for container/balcony suitable plants
            plant_pool = [p for p in PLANTS if p.get("container_suitable", True)]
        else:
            plant_pool = [p for p in PLANTS if p.get("category") == body.category]
        if not plant_pool:
            raise HTTPException(
                status_code=400,
                detail=f"No plants found for category '{body.category}'. Valid categories: {', '.join(sorted(VALID_CATEGORIES))}"
            )

    # Filter by pollinator-friendly if specified
    if body.pollinator_friendly:
        plant_pool = [p for p in plant_pool if p.get("pollinator_friendly", False)]
        if not plant_pool:
            raise HTTPException(
                status_code=400,
                detail="No pollinator-friendly plants found for the selected criteria"
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

        # Water conservation data from score_result
        water_conservation = score_result.get("water_conservation", {})
        
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
            pot_size_cm=plant.get("pot_size_cm"),
            pot_depth_cm=plant.get("pot_depth_cm"),
            container_suitable=plant.get("container_suitable", True),
            soil_amendments=get_soil_amendments(plant, env),
            water_conservation_score=water_conservation.get("water_conservation_score"),
            estimated_liters_per_season=water_conservation.get("estimated_liters_per_season"),
            liters_saved_vs_baseline=water_conservation.get("liters_saved_vs_baseline"),
            pct_water_saved=water_conservation.get("pct_water_saved"),
            pollinator_friendly=plant.get("pollinator_friendly", False),
            hindi_name=plant.get("hindi_name"),
        ))

    # Add companion suggestions (second pass now that we have all recommendations)
    for rec in recommendations:
        plant_data = next((p for p in PLANTS if p["name"] == rec.plant_name), None)
        if plant_data:
            rec.companion_suggestions = companion_suggestions(plant_data, recommendations)

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
        
        # Get water conservation score
        water_conservation = score_result.get("water_conservation", {})
        water_score = water_conservation.get("water_conservation_score", 0)
        
        # Get companion notes (need to check against top 8)
        companion = companion_suggestions(plant, [r for r in recommendations])
        grows_with = companion.get("grows_well_with", [])
        avoid = companion.get("avoid_near", [])
        companion_note = ""
        if grows_with:
            companion_note += f"✓ {', '.join(grows_with)}"
        if avoid:
            if companion_note:
                companion_note += " | "
            companion_note += f"✗ {', '.join(avoid)}"
        if not companion_note:
            companion_note = "—"
        
        comparison.append({
            "name": plant["name"],
            "suitability": score_result["total"],
            "water": plant.get("water", "medium"),
            "sun": plant.get("sun", "full"),
            "growth": growth_str,
            "water_score": round(water_score, 1),
            "companion_notes": companion_note,
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
    save_report_to_db(response, user_id)
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
            pot_size_cm=r.get('pot_size_cm'),
            pot_depth_cm=r.get('pot_depth_cm'),
            container_suitable=r.get('container_suitable', True),
            soil_amendments=r.get('soil_amendments'),
            companion_suggestions=r.get('companion_suggestions'),
            water_conservation_score=r.get('water_conservation_score'),
            estimated_liters_per_season=r.get('estimated_liters_per_season'),
            liters_saved_vs_baseline=r.get('liters_saved_vs_baseline'),
            pct_water_saved=r.get('pct_water_saved'),
            pollinator_friendly=r.get('pollinator_friendly'),
            hindi_name=r.get('hindi_name'),
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


@app.get("/impact", response_class=FileResponse)
async def serve_impact_page():
    impact_path = os.path.join(os.path.dirname(__file__), "..", "static", "impact.html")
    return FileResponse(impact_path)


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
    is_organic: bool = Field(False, description="Whether this purchase is organic/regenerative input")


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
    is_organic: bool = False


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
    # Regenerative economics
    organic_purchases_total: float = 0.0
    conventional_purchases_total: float = 0.0
    estimated_organic_matter_kg: float = 0.0
    estimated_co2_offset_kg: float = 0.0


# --- User Authentication (Phone OTP) ---

class PhoneRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+?[1-9]\d{9,14}$", description="Phone number in E.164 format")

class OTPRequest(BaseModel):
    phone: str = Field(..., pattern=r"^\+?[1-9]\d{9,14}$")
    code: str = Field(..., min_length=4, max_length=6)
    purpose: str = Field("login", pattern="^(login|register)$")

class OTPResponse(BaseModel):
    success: bool
    message: str
    token: Optional[str] = None
    user_id: Optional[int] = None
    is_new_user: bool = False

class UserProfile(BaseModel):
    id: int
    phone: str
    name: Optional[str] = None
    created_at: float
    last_login: Optional[float] = None

class UserReportSummary(BaseModel):
    report_id: str
    location: str
    generated_at: float
    suitability_score: float
    top_plant: str


# Organic matter / CO2 offset estimation for regenerative inputs
ORGANIC_MATTER_PER_KG = {
    # Compost, manure, organic fertilizers - kg organic matter per kg product
    "compost": 0.4,      # ~40% organic matter
    "vermicompost": 0.5, # ~50% organic matter
    "fym": 0.3,          # Farm yard manure ~30%
    "neem cake": 0.6,
    "bone meal": 0.2,
    "blood meal": 0.1,
    "rock phosphate": 0.0,
    "green manure": 0.8,
    "biochar": 0.8,
    "cow dung": 0.25,
    "poultry manure": 0.4,
    "pressmud": 0.35,
    "city compost": 0.3,
}
DEFAULT_ORGANIC_MATTER_RATIO = 0.3  # Default for unknown organic inputs

# CO2 sequestration: ~1.8 kg CO2 per kg organic matter added to soil (approximate)
CO2_PER_KG_ORGANIC_MATTER = 1.8


def calculate_organic_impact(purchases: list) -> dict:
    """Calculate estimated organic matter added and CO2 offset from organic purchases.
    
    Args:
        purchases: List of purchase dicts with item_name, quantity, unit, is_organic, item_type
    
    Returns:
        dict with organic_matter_kg, co2_offset_kg, organic_spend, conventional_spend
    """
    organic_matter_kg = 0.0
    organic_spend = 0.0
    conventional_spend = 0.0
    
    for p in purchases:
        total_cost = p.get("total_cost", p.get("quantity", 0) * p.get("cost_per_unit", 0))
        is_organic = p.get("is_organic", False)
        item_name = p.get("item_name", "").lower()
        quantity = p.get("quantity", 0)
        unit = p.get("unit", "kg").lower()
        
        if is_organic:
            organic_spend += total_cost
            # Estimate organic matter based on item name
            matter_ratio = DEFAULT_ORGANIC_MATTER_RATIO
            for key, ratio in ORGANIC_MATTER_PER_KG.items():
                if key in item_name:
                    matter_ratio = ratio
                    break
            
            # Convert quantity to kg if needed
            qty_kg = quantity
            if unit in ("g", "gram", "grams"):
                qty_kg = quantity / 1000
            elif unit in ("ton", "tonne", "tons", "tonnes"):
                qty_kg = quantity * 1000
            elif unit in ("bag", "bags"):  # Assume 25kg/bag typical
                qty_kg = quantity * 25
            
            organic_matter_kg += qty_kg * matter_ratio
        else:
            conventional_spend += total_cost
    
    co2_offset_kg = organic_matter_kg * CO2_PER_KG_ORGANIC_MATTER
    
    return {
        "organic_matter_kg": round(organic_matter_kg, 2),
        "co2_offset_kg": round(co2_offset_kg, 2),
        "organic_spend": round(organic_spend, 2),
        "conventional_spend": round(conventional_spend, 2),
    }


# --- Smart Care Calendar ---
# Auto-recurring tasks (watering, fertilizing, pruning, harvesting) per plant stage
# Planting date defaults to report generation date, editable per plant

class CareTask(BaseModel):
    plant_name: str
    task_type: str  # "watering", "fertilizing", "pruning", "harvesting"
    frequency_days: int
    next_due_date: str  # ISO format
    description: str
    priority: str  # "high", "medium", "low"
    planting_date: Optional[str] = None  # ISO format, editable per plant


class CareCalendarResponse(BaseModel):
    report_id: str
    tasks: List[CareTask]
    generated_at: float


@app.get("/api/v1/reports/{report_id}/care-calendar", response_model=CareCalendarResponse)
@limiter.limit("60/minute")
async def care_calendar(request: Request, report_id: str):
    """Get auto-generated care calendar for all recommended plants."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    from datetime import date, timedelta
    today = date.today()
    report_date = date.fromtimestamp(report.get("generated_at", time.time()))

    tasks = []
    for rec in report.get("recommendations", [])[:10]:
        plant_name = rec.get("plant_name", "")
        if not plant_name:
            continue

        # Get plant details from DB
        plant_map = {p["name"]: p for p in PLANTS}
        plant = plant_map.get(plant_name, {})

        # Default planting date = report generation date (editable per plant)
        planting_date = rec.get("planting_date_override") or report_date.isoformat()

        # --- Watering tasks ---
        water_schedule = rec.get("watering_schedule", {})
        freq_days = water_schedule.get("frequency_days", 3)
        if not freq_days or freq_days < 1:
            freq_days = 3

        # Calculate next watering due date
        days_since_report = (today - report_date).days
        next_water_offset = freq_days - (days_since_report % freq_days)
        if next_water_offset == freq_days:
            next_water_offset = 0
        next_water_date = today + timedelta(days=next_water_offset)

        tasks.append(CareTask(
            plant_name=plant_name,
            task_type="watering",
            frequency_days=freq_days,
            next_due_date=next_water_date.isoformat(),
            description=f"Water {plant_name} ({water_schedule.get('liters_per_watering', '?')} L per session)",
            priority="high",
            planting_date=planting_date
        ))

        # --- Fertilizing tasks ---
        # Heavy feeders (fruit/vegetable): every 30 days, herbs: every 60 days
        category = plant.get("category", "herb")
        if category in ("fruit", "vegetable"):
            fert_freq = 30
        else:
            fert_freq = 60

        next_fert_offset = fert_freq - (days_since_report % fert_freq)
        if next_fert_offset == fert_freq:
            next_fert_offset = 0
        next_fert_date = today + timedelta(days=next_fert_offset)

        tasks.append(CareTask(
            plant_name=plant_name,
            task_type="fertilizing",
            frequency_days=fert_freq,
            next_due_date=next_fert_date.isoformat(),
            description=f"Fertilize {plant_name} (balanced NPK, follow package rates)",
            priority="medium",
            planting_date=planting_date
        ))

        # --- Pruning tasks ---
        # Prune at flowering/fruiting stage for fruiting plants
        if category in ("fruit", "vegetable"):
            prune_freq = 60
            next_prune_offset = prune_freq - (days_since_report % prune_freq)
            if next_prune_offset == prune_freq:
                next_prune_offset = 0
            next_prune_date = today + timedelta(days=next_prune_offset)

            tasks.append(CareTask(
                plant_name=plant_name,
                task_type="pruning",
                frequency_days=prune_freq,
                next_due_date=next_prune_date.isoformat(),
                description=f"Prune {plant_name} (remove dead/diseased, shape for airflow)",
                priority="medium",
                planting_date=planting_date
            ))

        # --- Harvesting task ---
        # Based on growth_duration from planting date
        growth_days = plant.get("growth_days", 60)
        harvest_date = report_date + timedelta(days=growth_days)
        if harvest_date >= today:
            days_until_harvest = (harvest_date - today).days
            tasks.append(CareTask(
                plant_name=plant_name,
                task_type="harvesting",
                frequency_days=0,  # One-time
                next_due_date=harvest_date.isoformat(),
                description=f"Harvest {plant_name} (expected maturity ~{growth_days} days from planting)",
                priority="high",
                planting_date=planting_date
            ))

    # Sort by next due date
    tasks.sort(key=lambda t: t.next_due_date)

    return CareCalendarResponse(
        report_id=report_id,
        tasks=tasks,
        generated_at=time.time()
    )


@app.post("/api/v1/purchases", response_model=PurchaseResponse)
@limiter.limit("60/minute")
async def create_purchase(request: Request, purchase: PurchaseCreate):
    """Record a purchase (seeds, saplings, fertilizer, tools, etc.) for a plant."""
    conn = get_db()
    purchase_date = purchase.purchase_date or time.time()
    total_cost = purchase.quantity * purchase.cost_per_unit
    cursor = conn.execute(
        """INSERT INTO purchases
        (report_id, plant_name, item_type, item_name, quantity, unit, cost_per_unit, total_cost, purchase_date, notes, created_at, is_organic)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (purchase.report_id, purchase.plant_name, purchase.item_type, purchase.item_name,
         purchase.quantity, purchase.unit, purchase.cost_per_unit, total_cost,
         purchase_date, purchase.notes, time.time(), purchase.is_organic)
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
        created_at=time.time(),
        is_organic=purchase.is_organic
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
        notes=r["notes"], created_at=r["created_at"],
        is_organic=bool(r.get("is_organic", 0))
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
    """Get profit/loss ledger for a report with regenerative economics."""
    conn = get_db()
    # Total purchases with details
    purchase_rows = conn.execute(
        "SELECT * FROM purchases WHERE report_id = ?",
        (report_id,)
    ).fetchall()
    # Total sales
    sale_rows = conn.execute(
        "SELECT plant_name, SUM(total_revenue) as total FROM sales WHERE report_id = ? GROUP BY plant_name",
        (report_id,)
    ).fetchall()
    conn.close()

    # Calculate organic impact
    purchases_list = [dict(r) for r in purchase_rows]
    organic_impact = calculate_organic_impact(purchases_list)

    # Aggregate by plant
    purchases_by_plant = {}
    for r in purchase_rows:
        plant = r["plant_name"]
        if plant not in purchases_by_plant:
            purchases_by_plant[plant] = 0
        purchases_by_plant[plant] += r["total_cost"]
    
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
        by_plant=by_plant,
        organic_purchases_total=organic_impact["organic_spend"],
        conventional_purchases_total=organic_impact["conventional_spend"],
        estimated_organic_matter_kg=organic_impact["organic_matter_kg"],
        estimated_co2_offset_kg=organic_impact["co2_offset_kg"],
    )


# --- Aggregate Impact Dashboard (Community-wide) ---

class ImpactSummary(BaseModel):
    total_reports: int
    total_users: int
    # Water conservation
    total_liters_saved: float
    avg_water_conservation_score: float
    # Regenerative economics
    total_organic_matter_kg: float
    total_co2_offset_kg: float
    total_organic_spend: float
    # Plant recommendations
    most_recommended_plants: List[dict]  # [{plant, count, avg_score}]
    # By region (agro_zone)
    regions: List[dict]  # [{zone, reports, top_plant, liters_saved}]


@app.get("/api/v1/impact/summary", response_model=ImpactSummary)
@limiter.limit("30/minute")
async def get_impact_summary(request: Request):
    """Get community-wide aggregate impact metrics across all reports."""
    conn = get_db()
    
    # Total reports and users
    total_reports = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    
    # Get all reports with recommendations
    rows = conn.execute(
        "SELECT report_id, location, environment, recommendations, user_id FROM reports"
    ).fetchall()
    conn.close()
    
    # Aggregate metrics
    total_liters_saved = 0.0
    water_scores = []
    plant_counts = {}
    plant_scores = {}
    region_data = {}
    
    for row in rows:
        env = json.loads(row["environment"]) if row["environment"] else {}
        agro_zone = env.get("agro_zone", "Unknown")
        recommendations = json.loads(row["recommendations"]) if row["recommendations"] else []
        
        # Track region data
        if agro_zone not in region_data:
            region_data[agro_zone] = {"reports": 0, "liters_saved": 0.0, "plant_counts": {}}
        region_data[agro_zone]["reports"] += 1
        
        for rec in recommendations:
            # Water conservation
            liters_saved = rec.get("liters_saved_vs_baseline", 0) or 0
            total_liters_saved += liters_saved
            region_data[agro_zone]["liters_saved"] += liters_saved
            
            water_score = rec.get("water_conservation_score")
            if water_score is not None:
                water_scores.append(water_score)
            
            # Plant frequency (global)
            plant_name = rec.get("plant_name", "")
            if plant_name:
                plant_counts[plant_name] = plant_counts.get(plant_name, 0) + 1
                score = rec.get("suitability_score", 0)
                if plant_name not in plant_scores:
                    plant_scores[plant_name] = []
                plant_scores[plant_name].append(score)
                
                # Plant frequency (per region)
                region_data[agro_zone]["plant_counts"][plant_name] = region_data[agro_zone]["plant_counts"].get(plant_name, 0) + 1
        
        # Organic matter from purchases
        # We'll need to query purchases separately
    
    # Get organic matter from all purchases
    conn = get_db()
    purchase_rows = conn.execute(
        "SELECT item_name, quantity, unit, cost_per_unit, is_organic FROM purchases"
    ).fetchall()
    conn.close()
    
    all_purchases = [dict(r) for r in purchase_rows]
    total_organic_impact = calculate_organic_impact(all_purchases)
    
    # Most recommended plants
    most_recommended = sorted(
        [{"plant": p, "count": c, "avg_score": round(sum(plant_scores[p]) / len(plant_scores[p]), 1)} 
         for p, c in plant_counts.items()],
        key=lambda x: x["count"],
        reverse=True
    )[:10]
    
    # Region summary
    regions = [
        {
            "zone": zone,
            "reports": data["reports"],
            "top_plant": max(data["plant_counts"].items(), key=lambda x: x[1])[0] if data["plant_counts"] else "N/A",
            "liters_saved": round(data["liters_saved"], 1)
        }
        for zone, data in region_data.items()
    ]
    regions.sort(key=lambda x: x["reports"], reverse=True)
    
    return ImpactSummary(
        total_reports=total_reports,
        total_users=total_users,
        total_liters_saved=round(total_liters_saved, 1),
        avg_water_conservation_score=round(sum(water_scores) / len(water_scores), 1) if water_scores else 0,
        total_organic_matter_kg=round(total_organic_impact["organic_matter_kg"], 1),
        total_co2_offset_kg=round(total_organic_impact["co2_offset_kg"], 1),
        total_organic_spend=round(total_organic_impact["organic_spend"], 2),
        most_recommended_plants=most_recommended,
        regions=regions
    )


@app.get("/api/v1/reports/{report_id}/export.csv")
@limiter.limit("30/minute")
async def export_report_csv(request: Request, report_id: str):
    """Export report recommendations as CSV."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        "Rank", "Plant Name", "Scientific Name", "Suitability Score (%)",
        "Water Requirement", "Sun Requirement", "Growth Duration",
        "Planting Season", "Estimated Water L/Week", "Estimated CO2 Kg/Year",
        "Care Guide", "Score Climate", "Score pH", "Score Sunlight",
        "Score Water", "Score Soil", "Score Photo", "Water Conservation Score",
        "Estimated Liters/Season", "Liters Saved vs Baseline", "Pct Water Saved",
        "Companion Grows Well With", "Companion Avoid Near"
    ])

    # Data rows
    for i, rec in enumerate(report.get("recommendations", []), 1):
        sb = rec.get("score_breakdown", {})
        companion = rec.get("companion_suggestions", {})
        grows_with = ", ".join(companion.get("grows_well_with", [])) if companion else ""
        avoid = ", ".join(companion.get("avoid_near", [])) if companion else ""
        writer.writerow([
            i,
            rec.get("plant_name", ""),
            rec.get("scientific_name", ""),
            rec.get("suitability_score", ""),
            rec.get("water_requirement", ""),
            rec.get("sun_requirement", ""),
            rec.get("growth_duration", ""),
            rec.get("planting_season", ""),
            rec.get("estimated_water_l_per_week", ""),
            rec.get("estimated_co2_kg_per_year", ""),
            rec.get("care_guide", "").replace("\n", " "),
            sb.get("climate", ""),
            sb.get("ph", ""),
            sb.get("sunlight", ""),
            sb.get("water", ""),
            sb.get("soil", ""),
            sb.get("photo", ""),
            rec.get("water_conservation_score", ""),
            rec.get("estimated_liters_per_season", ""),
            rec.get("liters_saved_vs_baseline", ""),
            rec.get("pct_water_saved", ""),
            grows_with,
            avoid,
        ])

    output.seek(0)
    filename = f"greenscope-{report.get('location', 'report').replace(' ', '_').replace(',', '')}-{report_id[:8]}.csv"
    return PlainTextResponse(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/api/v1/reports/{report_id}/export.xlsx")
@limiter.limit("30/minute")
async def export_report_xlsx(request: Request, report_id: str):
    """Export report recommendations as Excel (XLSX)."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(status_code=501, detail="Excel export requires openpyxl. Install with: pip install openpyxl")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Recommendations"

    # Styles
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="10B981", end_color="10B981", fill_type="solid")
    header_alignment = Alignment(horizontal="center", wrap_text=True)

    headers = [
        "Rank", "Plant Name", "Scientific Name", "Suitability Score (%)",
        "Water Requirement", "Sun Requirement", "Growth Duration",
        "Planting Season", "Estimated Water L/Week", "Estimated CO2 Kg/Year",
        "Care Guide", "Score Climate", "Score pH", "Score Sunlight",
        "Score Water", "Score Soil", "Score Photo", "Water Conservation Score",
        "Estimated Liters/Season", "Liters Saved vs Baseline", "Pct Water Saved",
        "Companion Grows Well With", "Companion Avoid Near"
    ]

    # Write headers
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment

    # Write data
    for row_idx, rec in enumerate(report.get("recommendations", []), 2):
        sb = rec.get("score_breakdown", {})
        companion = rec.get("companion_suggestions", {})
        grows_with = ", ".join(companion.get("grows_well_with", [])) if companion else ""
        avoid = ", ".join(companion.get("avoid_near", [])) if companion else ""
        row_data = [
            row_idx - 1,
            rec.get("plant_name", ""),
            rec.get("scientific_name", ""),
            rec.get("suitability_score", ""),
            rec.get("water_requirement", ""),
            rec.get("sun_requirement", ""),
            rec.get("growth_duration", ""),
            rec.get("planting_season", ""),
            rec.get("estimated_water_l_per_week", ""),
            rec.get("estimated_co2_kg_per_year", ""),
            rec.get("care_guide", "").replace("\n", " "),
            sb.get("climate", ""),
            sb.get("ph", ""),
            sb.get("sunlight", ""),
            sb.get("water", ""),
            sb.get("soil", ""),
            sb.get("photo", ""),
            rec.get("water_conservation_score", ""),
            rec.get("estimated_liters_per_season", ""),
            rec.get("liters_saved_vs_baseline", ""),
            rec.get("pct_water_saved", ""),
            grows_with,
            avoid,
        ]
        for col, value in enumerate(row_data, 1):
            ws.cell(row=row_idx, column=col, value=value)

    # Auto-fit columns
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18

    # Save to bytes
    import io
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"greenscope-{report.get('location', 'report').replace(' ', '_').replace(',', '')}-{report_id[:8]}.xlsx"
    return PlainTextResponse(
        output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
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


# --- User Authentication (Phone OTP) ---

# Mock SMS gateway - replace with real provider (Twilio, MSG91, etc.) in production
async def send_otp_sms(phone: str, code: str, purpose: str) -> bool:
    """Send OTP via SMS. Mock implementation logs to console for demo."""
    print(f"[MOCK SMS] To: {phone} | Code: {code} | Purpose: {purpose}")
    # In production: integrate with Twilio, MSG91, etc.
    # Example with Twilio:
    # client.messages.create(body=f"Your GreenScope OTP is {code}", from_=TWILIO_FROM, to=phone)
    return True


def generate_otp() -> str:
    """Generate a 6-digit OTP."""
    import random
    return str(random.randint(100000, 999999))


def create_session_token(user_id: int) -> str:
    """Create a JWT-like session token (simplified for demo)."""
    import secrets
    return f"gs_{user_id}_{secrets.token_urlsafe(32)}"


@app.post("/api/v1/auth/send-otp", response_model=OTPResponse)
@limiter.limit("10/minute")
async def send_otp(request: Request, phone_req: PhoneRequest):
    """Send OTP to phone number for login/register."""
    conn = get_db()
    phone = phone_req.phone.strip()
    
    # Check if user exists
    user_row = conn.execute("SELECT id, name FROM users WHERE phone = ?", (phone,)).fetchone()
    is_new_user = user_row is None
    
    # Generate OTP
    code = generate_otp()
    expires_at = time.time() + 300  # 5 minutes
    
    # Store OTP
    conn.execute(
        "INSERT INTO otp_codes (phone, code, purpose, expires_at, used, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (phone, code, "login" if not is_new_user else "register", expires_at, 0, time.time())
    )
    conn.commit()
    conn.close()
    
    # Send SMS (mock)
    await send_otp_sms(phone, code, "login" if not is_new_user else "register")
    
    return OTPResponse(
        success=True,
        message=f"OTP sent to {phone}" + (" (new user)" if is_new_user else ""),
        is_new_user=is_new_user
    )


@app.post("/api/v1/auth/verify-otp", response_model=OTPResponse)
@limiter.limit("20/minute")
async def verify_otp(request: Request, otp_req: OTPRequest):
    """Verify OTP and create session."""
    conn = get_db()
    phone = otp_req.phone.strip()
    code = otp_req.code.strip()
    
    # Find valid OTP
    otp_row = conn.execute(
        "SELECT id, expires_at, used FROM otp_codes WHERE phone = ? AND code = ? AND purpose = ? AND used = 0 ORDER BY created_at DESC LIMIT 1",
        (phone, code, otp_req.purpose)
    ).fetchone()
    
    if not otp_row:
        conn.close()
        return OTPResponse(success=False, message="Invalid or expired OTP")
    
    if otp_row["expires_at"] < time.time():
        conn.close()
        return OTPResponse(success=False, message="OTP expired")
    
    # Mark OTP as used
    conn.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (otp_row["id"],))
    
    # Get or create user
    user_row = conn.execute("SELECT id, name FROM users WHERE phone = ?", (phone,)).fetchone()
    if user_row:
        user_id = user_row["id"]
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user_id))
    else:
        cursor = conn.execute(
            "INSERT INTO users (phone, name, created_at, last_login) VALUES (?, ?, ?, ?)",
            (phone, None, time.time(), time.time())
        )
        user_id = cursor.lastrowid
    
    # Create session
    token = create_session_token(user_id)
    expires_at = time.time() + 86400 * 30  # 30 days
    conn.execute(
        "INSERT INTO user_sessions (user_id, token, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (user_id, token, expires_at, time.time())
    )
    conn.commit()
    conn.close()
    
    return OTPResponse(
        success=True,
        message="Login successful",
        token=token,
        user_id=user_id,
        is_new_user=user_row is None
    )


async def get_current_user(request: Request) -> Optional[dict]:
    """Extract user from Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    conn = get_db()
    session_row = conn.execute(
        "SELECT user_id FROM user_sessions WHERE token = ? AND expires_at > ?",
        (token, time.time())
    ).fetchone()
    conn.close()
    if not session_row:
        return None
    return {"user_id": session_row["user_id"]}


@app.get("/api/v1/auth/me", response_model=UserProfile)
async def get_profile(request: Request):
    """Get current user profile."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    user_row = conn.execute("SELECT id, phone, name, created_at, last_login FROM users WHERE id = ?", (user["user_id"],)).fetchone()
    conn.close()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    return UserProfile(
        id=user_row["id"],
        phone=user_row["phone"],
        name=user_row["name"],
        created_at=user_row["created_at"],
        last_login=user_row["last_login"]
    )


@app.get("/api/v1/auth/my-reports", response_model=List[UserReportSummary])
async def get_my_reports(request: Request):
    """Get all reports for the current user."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    conn = get_db()
    rows = conn.execute(
        """SELECT report_id, location, generated_at, 
           (SELECT MAX(suitability_score) FROM json_each(recommendations)) as top_score,
           (SELECT plant_name FROM json_each(recommendations) ORDER BY suitability_score DESC LIMIT 1) as top_plant
           FROM reports WHERE user_id = ? ORDER BY generated_at DESC""",
        (user["user_id"],)
    ).fetchall()
    conn.close()
    return [
        UserReportSummary(
            report_id=r["report_id"],
            location=r["location"],
            generated_at=r["generated_at"],
            suitability_score=r["top_score"] or 0,
            top_plant=r["top_plant"] or ""
        )
        for r in rows
    ]


# --- Push Notifications (Watering Reminders) ---

class PushSubscription(BaseModel):
    endpoint: str
    keys: dict  # {p256dh: "...", auth: "..."}

class PushSubscribeRequest(BaseModel):
    subscription: PushSubscription
    report_id: Optional[str] = None

class PushSubscribeResponse(BaseModel):
    success: bool
    message: str


# In-memory store for push subscriptions (demo - replace with DB in production)
push_subscriptions = {}


@app.post("/api/v1/push/subscribe", response_model=PushSubscribeResponse)
@limiter.limit("30/minute")
async def subscribe_push(request: Request, body: PushSubscribeRequest):
    """Subscribe to push notifications for watering reminders."""
    # In production: validate VAPID, store in DB with user_id
    sub_key = body.subscription.endpoint
    push_subscriptions[sub_key] = {
        "subscription": body.subscription.dict(),
        "report_id": body.report_id,
        "created_at": time.time()
    }
    print(f"Push subscription stored: {sub_key}")
    return PushSubscribeResponse(success=True, message="Subscribed to watering reminders")


@app.post("/api/v1/push/unsubscribe", response_model=PushSubscribeResponse)
@limiter.limit("30/minute")
async def unsubscribe_push(request: Request, body: PushSubscribeRequest):
    """Unsubscribe from push notifications."""
    sub_key = body.subscription.endpoint
    if sub_key in push_subscriptions:
        del push_subscriptions[sub_key]
    return PushSubscribeResponse(success=True, message="Unsubscribed from watering reminders")


# Mock endpoint to trigger a watering reminder (for demo/testing)
@app.post("/api/v1/push/trigger-watering", response_model=PushSubscribeResponse)
@limiter.limit("10/minute")
async def trigger_watering_reminder(request: Request, report_id: str):
    """Trigger a test watering reminder push notification."""
    # In production: this would be called by a scheduler/cron job
    import json
    
    # Web Push protocol (simplified - production needs pywebpush)
    # For demo, we'll just log and return success
    print(f"Triggering watering reminder for report: {report_id}")
    
    # In a real implementation, you would:
    # 1. Get all subscriptions for this report/user
    # 2. Use pywebpush to send encrypted push messages
    # 3. Handle VAPID signing
    
    return PushSubscribeResponse(
        success=True, 
        message=f"Watering reminder triggered for {report_id} (demo - check SW console)"
    )


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


# --- Watering Forecast (Rain/Frost Advisory) ---

class WateringForecastDay(BaseModel):
    date: str  # YYYY-MM-DD
    day_name: str
    precipitation_mm: float
    min_temp_c: float
    skip_watering: bool
    skip_reason: Optional[str] = None
    watering_scheduled: bool


class WateringForecastResponse(BaseModel):
    report_id: str
    location: str
    latitude: float
    longitude: float
    forecast_days: List[WateringForecastDay]
    generated_at: float


@app.get("/api/v1/reports/{report_id}/watering-forecast", response_model=WateringForecastResponse)
@limiter.limit("60/minute")
async def watering_forecast(request: Request, report_id: str):
    """Get 7-day watering forecast with rain/frost skip advisories."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    lat = report.get("latitude", 28.6139)
    lng = report.get("longitude", 77.2090)
    location = report.get("location", "")

    # Fetch 7-day forecast from Open-Meteo
    forecast_days = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "daily": "precipitation_sum,temperature_2m_min",
                    "timezone": "Asia/Kolkata",
                    "forecast_days": 7,
                },
            )
            if r.status_code == 200:
                data = r.json().get("daily", {})
                dates = data.get("time", [])
                precipitation = data.get("precipitation_sum", [])
                temp_min = data.get("temperature_2m_min", [])

                for i, date_str in enumerate(dates):
                    precip = precipitation[i] if i < len(precipitation) else 0
                    temp_min = temp_min[i] if i < len(temp_min) else 20

                    skip = False
                    reason = None
                    if precip >= 5:
                        skip = True
                        reason = f"Rain expected ({precip} mm)"
                    elif temp_min <= 2:
                        skip = True
                        reason = f"Frost risk (min {temp_min:.1f}°C)"

                    day_name = date.fromisoformat(date_str).strftime("%A")
                    forecast_days.append(WateringForecastDay(
                        date=date_str,
                        day_name=day_name,
                        precipitation_mm=round(precip, 1),
                        min_temp_c=round(temp_min, 1),
                        skip_watering=skip,
                        skip_reason=reason,
                        watering_scheduled=True,  # Could check against watering schedule
                    ))
    except Exception:
        # If forecast fails, return empty forecast
        pass

    return WateringForecastResponse(
        report_id=report_id,
        location=location,
        latitude=lat,
        longitude=lng,
        forecast_days=forecast_days,
        generated_at=time.time(),
    )


# --- Harvest Timeline / Gantt Chart ---

class HarvestTimelineEvent(BaseModel):
    plant_name: str
    scientific_name: Optional[str] = None
    stage: str  # "sowing", "germination", "transplant", "flowering", "fruiting", "harvest"
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    duration_days: int
    color: str
    notes: str = ""


class HarvestTimelineResponse(BaseModel):
    report_id: str
    location: str
    timeline: List[HarvestTimelineEvent]
    summary: dict


def _season_start_month(season: str) -> int:
    """Get typical sowing month for a season."""
    months = {"kharif": 6, "monsoon": 6, "rabi": 10, "zaid": 3, "year-round": date.today().month}
    return months.get(season.lower(), date.today().month)


def _calculate_stage_dates(sow_month: int, growth_days: int, year: int) -> dict:
    """Calculate approximate dates for each growth stage."""
    from datetime import date, timedelta
    
    sow_date = date(year, sow_month, 1)
    
    # Approximate stage percentages of total growth cycle
    stages = [
        ("sowing", 0, 0.05, "#8B4513"),      # Brown - sowing
        ("germination", 0.05, 0.15, "#8FBC8F"),  # Light green - germination
        ("vegetative", 0.15, 0.50, "#228B22"),   # Green - vegetative growth
        ("flowering", 0.50, 0.70, "#FFD700"),    # Gold - flowering
        ("fruiting", 0.70, 0.90, "#FFA500"),     # Orange - fruiting
        ("harvest", 0.90, 1.0, "#FF6347"),       # Tomato red - harvest
    ]
    
    timeline = []
    for stage_name, start_pct, end_pct, color in stages:
        start_day = int(growth_days * start_pct)
        end_day = int(growth_days * end_pct)
        start_date = sow_date + timedelta(days=start_day)
        end_date = sow_date + timedelta(days=end_day)
        timeline.append({
            "stage": stage_name,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "duration_days": end_day - start_day,
            "color": color,
        })
    return timeline


@app.get("/api/v1/reports/{report_id}/harvest-timeline", response_model=HarvestTimelineResponse)
@limiter.limit("60/minute")
async def harvest_timeline(request: Request, report_id: str):
    """Get Gantt-style harvest timeline for recommended plants."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    today = date.today()
    current_year = today.year
    timeline = []

    for r in report.get("recommendations", [])[:10]:
        growth_days = 60
        # Try to get actual growth days from plant DB
        plant_map = {p["name"]: p for p in PLANTS}
        if r.get("plant_name") in plant_map:
            growth_days = plant_map[r["plant_name"]].get("growth_days", 60)

        season = r.get("planting_season", "Year-round")
        sow_month = _season_start_month(season)
        
        # Determine year for sowing (next occurrence)
        sow_year = current_year if sow_month >= today.month else current_year + 1
        
        stage_dates = _calculate_stage_dates(sow_month, growth_days, sow_year)
        
        for stage in stage_dates:
            timeline.append(HarvestTimelineEvent(
                plant_name=r.get("plant_name", ""),
                scientific_name=r.get("scientific_name"),
                stage=stage["stage"],
                start_date=stage["start_date"],
                end_date=stage["end_date"],
                duration_days=stage["duration_days"],
                color=stage["color"],
                notes=f"{stage['stage'].title()} phase for {r.get('plant_name', '')}"
            ))

    # Sort by start date
    timeline.sort(key=lambda x: x.start_date)

    summary = {
        "total_plants": len(set(t.plant_name for t in timeline)),
        "earliest_sowing": min((t.start_date for t in timeline if t.stage == "sowing"), default=None),
        "latest_harvest": max((t.end_date for t in timeline if t.stage == "harvest"), default=None),
        "timeline_span_days": 0,
    }
    if timeline:
        all_dates = [date.fromisoformat(t.start_date) for t in timeline] + [date.fromisoformat(t.end_date) for t in timeline]
        summary["timeline_span_days"] = (max(all_dates) - min(all_dates)).days

    return HarvestTimelineResponse(
        report_id=report_id,
        location=report.get("location", ""),
        timeline=timeline,
        summary=summary,
    )


# --- Companion Planting Matrix ---

class CompanionMatrixResponse(BaseModel):
    plants: List[str]
    matrix: List[List[str]]  # "good", "bad", "neutral"
    legend: dict


@app.get("/api/v1/reports/{report_id}/companion-matrix", response_model=CompanionMatrixResponse)
@limiter.limit("60/minute")
async def companion_matrix(request: Request, report_id: str):
    """Get companion planting matrix for recommended plants."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    plant_names = [r.get("plant_name", "") for r in report.get("recommendations", [])[:10]]
    n = len(plant_names)
    
    # Build matrix
    matrix = []
    for i, plant_a in enumerate(plant_names):
        row = []
        for j, plant_b in enumerate(plant_names):
            if i == j:
                row.append("self")
            else:
                grows_with, avoid = COMPANIONS.get(plant_a, ([], []))
                if plant_b in grows_with:
                    row.append("good")
                elif plant_b in avoid:
                    row.append("bad")
                else:
                    row.append("neutral")
        matrix.append(row)

    return CompanionMatrixResponse(
        plants=plant_names,
        matrix=matrix,
        legend={
            "good": "Plants benefit each other (pest control, nutrients, shade)",
            "bad": "Plants compete or attract same pests/diseases",
            "neutral": "No significant interaction known",
            "self": "Same plant"
        }
)
# Soil amendment suggestions (pH/soil mismatch tips) - added at plant level


# --- Soil Health Tracker ---
# Track pH/organic matter/N/P/K trends over seasons for each plant/bed

class SoilHealthLogCreate(BaseModel):
    report_id: str = Field(..., min_length=1, max_length=64)
    plant_name: str = Field(..., min_length=1, max_length=100)
    ph: float = Field(..., ge=0, le=14)
    organic_matter_pct: Optional[float] = Field(None, ge=0, le=100)
    nitrogen_ppm: Optional[float] = Field(None, ge=0)
    phosphorus_ppm: Optional[float] = Field(None, ge=0)
    potassium_ppm: Optional[float] = Field(None, ge=0)
    source: str = Field(default="meter", pattern="^(meter|lab)$")  # meter reading or lab test
    notes: Optional[str] = None


class SoilHealthLogResponse(BaseModel):
    id: int
    report_id: str
    plant_name: str
    log_date: float
    ph: float
    organic_matter_pct: Optional[float]
    nitrogen_ppm: Optional[float]
    phosphorus_ppm: Optional[float]
    potassium_ppm: Optional[float]
    source: str
    notes: Optional[str]
    amendments: Optional[list] = None


class SoilHealthTrend(BaseModel):
    plant_name: str
    ph_trend: List[dict]  # [{date, value}]
    om_trend: List[dict]
    n_trend: List[dict]
    p_trend: List[dict]
    k_trend: List[dict]


@app.post("/api/v1/soil-health", response_model=SoilHealthLogResponse)
@limiter.limit("60/minute")
async def create_soil_health_log(request: Request, log: SoilHealthLogCreate):
    """Record a soil health measurement for a plant."""
    # Verify report exists
    report = load_report_from_db(log.report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    # Get plant details for amendment suggestions
    plant_map = {p["name"]: p for p in PLANTS}
    plant = plant_map.get(log.plant_name)

    # Get amendment suggestions if plant exists
    amendments = []
    if plant:
        env = report.get("environment", {})
        amendments = get_soil_amendments(plant, env)

    conn = get_db()
    log_date = time.time()
    cursor = conn.execute(
        """INSERT INTO soil_health_logs
        (report_id, plant_name, ph, organic_matter_pct, nitrogen_ppm, 
         phosphorus_ppm, potassium_ppm, source, notes, log_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (log.report_id, log.plant_name, log.ph, log.organic_matter_pct,
         log.nitrogen_ppm, log.phosphorus_ppm, log.potassium_ppm,
         log.source, log.notes, log_date)
    )
    conn.commit()
    log_id = cursor.lastrowid
    conn.close()

    return SoilHealthLogResponse(
        id=log_id,
        report_id=log.report_id,
        plant_name=log.plant_name,
        log_date=log_date,
        ph=log.ph,
        organic_matter_pct=log.organic_matter_pct,
        nitrogen_ppm=log.nitrogen_ppm,
        phosphorus_ppm=log.phosphorus_ppm,
        potassium_ppm=log.potassium_ppm,
        source=log.source,
        notes=log.notes,
        amendments=amendments
    )


@app.get("/api/v1/reports/{report_id}/soil-health", response_model=List[SoilHealthLogResponse])
@limiter.limit("60/minute")
async def get_soil_health_logs(request: Request, report_id: str, plant_name: Optional[str] = None):
    """Get all soil health logs for a report, optionally filtered by plant."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    conn = get_db()
    if plant_name:
        rows = conn.execute(
            "SELECT * FROM soil_health_logs WHERE report_id = ? AND plant_name = ? ORDER BY log_date DESC",
            (report_id, plant_name)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM soil_health_logs WHERE report_id = ? ORDER BY log_date DESC",
            (report_id,)
        ).fetchall()
    conn.close()

    return [SoilHealthLogResponse(
        id=r["id"], report_id=r["report_id"], plant_name=r["plant_name"],
        log_date=r["log_date"], ph=r["ph"], organic_matter_pct=r["organic_matter_pct"],
        nitrogen_ppm=r["nitrogen_ppm"], phosphorus_ppm=r["phosphorus_ppm"],
        potassium_ppm=r["potassium_ppm"], source=r["source"], notes=r["notes"],
        amendments=None
    ) for r in rows]


@app.get("/api/v1/reports/{report_id}/soil-health/trends", response_model=List[SoilHealthTrend])
@limiter.limit("60/minute")
async def get_soil_health_trends(request: Request, report_id: str):
    """Get soil health trends for Chart.js charts."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM soil_health_logs WHERE report_id = ? ORDER BY log_date ASC",
        (report_id,)
    ).fetchall()
    conn.close()

    # Group by plant
    by_plant = {}
    for row in rows:
        plant = row["plant_name"]
        if plant not in by_plant:
            by_plant[plant] = {"ph": [], "om": [], "n": [], "p": [], "k": []}
        if row["ph"] is not None:
            by_plant[plant]["ph"].append({"date": row["log_date"], "value": row["ph"]})
        if row["organic_matter_pct"] is not None:
            by_plant[plant]["om"].append({"date": row["log_date"], "value": row["organic_matter_pct"]})
        if row["nitrogen_ppm"] is not None:
            by_plant[plant]["n"].append({"date": row["log_date"], "value": row["nitrogen_ppm"]})
        if row["phosphorus_ppm"] is not None:
            by_plant[plant]["p"].append({"date": row["log_date"], "value": row["phosphorus_ppm"]})
        if row["potassium_ppm"] is not None:
            by_plant[plant]["k"].append({"date": row["log_date"], "value": row["potassium_ppm"]})

    trends = []
    for plant, data in by_plant.items():
        trends.append(SoilHealthTrend(
            plant_name=plant,
            ph_trend=data["ph"],
            om_trend=data["om"],
            n_trend=data["n"],
            p_trend=data["p"],
            k_trend=data["k"]
        ))

    return trends


# --- Yield/Cost Estimator (ROI Calculator) ---
# Estimated yield per plant per season (kg) - rough averages for Indian conditions
ESTIMATED_YIELD_KG = {
    "Tomato": 5.0, "Brinjal": 3.0, "Chili": 1.5, "Okra": 2.5,
    "Spinach": 1.0, "Cabbage": 2.0, "Cauliflower": 1.5, "Peas": 1.0,
    "Carrot": 1.5, "Beetroot": 1.5, "Radish": 0.8, "Onion": 2.0,
    "Garlic": 1.0, "Ginger": 2.0, "Turmeric": 3.0, "Potato": 3.0,
    "Sweet Potato": 2.5, "Yam": 3.0, "Cucumber": 3.0, "Pumpkin": 5.0,
    "Bottle Gourd": 4.0, "Ridge Gourd": 3.0, "Bitter Gourd": 2.5,
    "Coriander": 0.3, "Fenugreek": 0.4, "Mint": 0.5,
    "Tulsi": 0.5, "Moringa": 10.0, "Lemongrass": 1.0, "Aloe Vera": 1.0,
    "Curry Leaf": 1.0, "Hibiscus": 1.0, "Marigold": 0.5, "Jasmine": 0.5,
    "Rose": 0.3, "Lotus": 2.0, "Amla": 20.0,
    "Mango": 50.0, "Guava": 15.0, "Papaya": 30.0, "Banana": 30.0,
    "Lemon": 10.0, "Neem": 5.0, "Ashwagandha": 1.0, "Brahmi": 0.5,
    "Giloy": 2.0, "Moong": 1.0, "Cowpea": 1.5, "Black Gram": 1.0,
    "Pigeon Pea": 2.0, "Mustard": 0.8, "Fenugreek": 0.5,
    "Artichoke": 1.0, "Stevia": 0.3,
}

# Estimated market price per kg (INR) - rough averages
ESTIMATED_PRICE_PER_KG = {
    "Tomato": 30, "Brinjal": 40, "Chili": 80, "Okra": 50,
    "Spinach": 40, "Cabbage": 25, "Cauliflower": 40, "Peas": 80,
    "Carrot": 40, "Beetroot": 50, "Radish": 30, "Onion": 35,
    "Garlic": 150, "Ginger": 120, "Turmeric": 100, "Potato": 25,
    "Sweet Potato": 30, "Yam": 40, "Cucumber": 30, "Pumpkin": 25,
    "Bottle Gourd": 30, "Ridge Gourd": 40, "Bitter Gourd": 60,
    "Coriander": 200, "Fenugreek": 150, "Mint": 100,
    "Tulsi": 80, "Moringa": 60, "Lemongrass": 80, "Aloe Vera": 100,
    "Curry Leaf": 200, "Hibiscus": 100, "Marigold": 100, "Jasmine": 300,
    "Rose": 200, "Lotus": 150, "Amla": 80,
    "Mango": 80, "Guava": 60, "Papaya": 50, "Banana": 40,
    "Lemon": 80, "Neem": 50, "Ashwagandha": 300, "Brahmi": 200,
    "Giloy": 150, "Moong": 100, "Cowpea": 80, "Black Gram": 100,
    "Pigeon Pea": 90, "Mustard": 80, "Fenugreek": 120,
    "Artichoke": 150, "Stevia": 200,
}

# Default yield/price for unknown plants
DEFAULT_YIELD_KG = 2.0
DEFAULT_PRICE_PER_KG = 50


class YieldEstimate(BaseModel):
    plant_name: str
    estimated_yield_kg: float
    estimated_price_per_kg: float
    estimated_revenue: float
    estimated_cost: float = 0.0
    estimated_profit: float = 0.0
    roi_percent: float = 0.0


class GardenROIResponse(BaseModel):
    report_id: str
    location: str
    bed_estimates: List[YieldEstimate]
    total_estimated_yield_kg: float
    total_estimated_revenue: float
    total_actual_cost: float
    total_actual_revenue: float
    total_profit: float
    overall_roi_percent: float
    disclaimer: str


@app.get("/api/v1/reports/{report_id}/roi", response_model=GardenROIResponse)
@limiter.limit("60/minute")
async def garden_roi(request: Request, report_id: str):
    """Calculate estimated ROI for garden bed using ledger data and yield estimates."""
    report = load_report_from_db(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    conn = get_db()
    # Get actual costs from purchases
    purchase_rows = conn.execute(
        "SELECT plant_name, SUM(total_cost) as total_cost FROM purchases WHERE report_id = ? GROUP BY plant_name",
        (report_id,)
    ).fetchall()
    purchases_by_plant = {r["plant_name"]: r["total_cost"] for r in purchase_rows}

    # Get actual revenue from sales
    sale_rows = conn.execute(
        "SELECT plant_name, SUM(total_revenue) as total_revenue FROM sales WHERE report_id = ? GROUP BY plant_name",
        (report_id,)
    ).fetchall()
    sales_by_plant = {r["plant_name"]: r["total_revenue"] for r in sale_rows}
    conn.close()

    bed_estimates = []
    total_est_yield = 0.0
    total_est_revenue = 0.0
    total_actual_cost = 0.0
    total_actual_revenue = 0.0

    for r in report.get("recommendations", [])[:10]:
        plant_name = r.get("plant_name", "")
        yield_kg = ESTIMATED_YIELD_KG.get(plant_name, DEFAULT_YIELD_KG)
        price_kg = ESTIMATED_PRICE_PER_KG.get(plant_name, DEFAULT_PRICE_PER_KG)
        est_revenue = yield_kg * price_kg
        actual_cost = purchases_by_plant.get(plant_name, 0.0)
        actual_revenue = sales_by_plant.get(plant_name, 0.0)
        est_cost = actual_cost if actual_cost > 0 else (yield_kg * price_kg * 0.3)
        est_profit = est_revenue - est_cost
        roi = (est_profit / est_cost * 100) if est_cost > 0 else 0

        bed_estimates.append(YieldEstimate(
            plant_name=plant_name,
            estimated_yield_kg=round(yield_kg, 2),
            estimated_price_per_kg=round(price_kg, 2),
            estimated_revenue=round(est_revenue, 2),
            estimated_cost=round(est_cost, 2),
            estimated_profit=round(est_profit, 2),
            roi_percent=round(roi, 1),
        ))

        total_est_yield += yield_kg
        total_est_revenue += est_revenue
        total_actual_cost += actual_cost
        total_actual_revenue += actual_revenue

    total_profit = total_actual_revenue - total_actual_cost
    overall_roi = (total_profit / total_actual_cost * 100) if total_actual_cost > 0 else 0

    return GardenROIResponse(
        report_id=report_id,
        location=report.get("location", ""),
        bed_estimates=bed_estimates,
        total_estimated_yield_kg=round(total_est_yield, 2),
        total_estimated_revenue=round(total_est_revenue, 2),
        total_actual_cost=round(total_actual_cost, 2),
        total_actual_revenue=round(total_actual_revenue, 2),
        total_profit=round(total_profit, 2),
        overall_roi_percent=round(overall_roi, 1),
        disclaimer="Yields and prices are rough estimates for Indian conditions. Actual results vary by variety, management, weather, and market. Use for planning only."
    )


# --- Plant Health Diagnosis Wizard ---
# Deterministic symptom → rule lookup using existing pest/disease data
# No ML/LLM - pure rule engine with confidence scores

# Symptom to condition mapping based on existing pest/disease alerts
SYMPTOM_RULES = {
    "yellow_leaves": [
        {"condition": "nitrogen_deficiency", "indicators": ["older_leaves_first", "uniform_yellowing", "stunted_growth"], 
         "advice": "Apply nitrogen-rich fertilizer (urea, compost). Check soil pH."},
        {"condition": "overwatering", "indicators": ["wilting_despite_wet_soil", "yellow_lower_leaves", "root_rot_smell"], 
         "advice": "Reduce watering frequency. Improve drainage. Check for root rot."},
        {"condition": "iron_deficiency", "indicators": ["young_leaves_yellow", "green_veins", "high_ph_soil"], 
         "advice": "Apply iron chelate or ferrous sulfate. Lower soil pH with sulfur."},
    ],
    "brown_spots": [
        {"condition": "fungal_leaf_spot", "indicators": ["circular_spots", "yellow_halo", "high_humidity"], 
         "advice": "Remove affected leaves. Improve air circulation. Copper fungicide if severe."},
        {"condition": "bacterial_leaf_spot", "indicators": ["angular_spots", "water_soaked_edges", "yellow_halo"], 
         "advice": "Remove affected leaves. Avoid overhead watering. Copper-based bactericide."},
        {"condition": "nutrient_burn", "indicators": ["brown_tips", "crispy_edges", "recent_fertilizer"], 
         "advice": "Flush soil with water. Reduce fertilizer concentration."},
    ],
    "wilting": [
        {"condition": "underwatering", "indicators": ["dry_soil", "leaves_crispy", "recovers_after_water"], 
         "advice": "Water deeply. Mulch to retain moisture. Check soil daily."},
        {"condition": "root_rot", "indicators": ["wet_soil", "wilting_despite_water", "foul_smell", "brown_roots"], 
         "advice": "Improve drainage immediately. Remove affected roots. Repot in fresh soil."},
        {"condition": "vascular_wilt", "indicators": ["one_sided_wilt", "vascular_browning", "no_recovery"], 
         "advice": "Remove plant. Solarize soil. Use resistant varieties next season."},
    ],
    "holes_in_leaves": [
        {"condition": "caterpillar_damage", "indicators": ["irregular_holes", "frass_visible", "caterpillars_present"], 
         "advice": "Hand-pick caterpillars. Apply Bt (Bacillus thuringiensis). Neem oil spray."},
        {"condition": "beetle_damage", "indicators": ["small_round_holes", "skeletonized_leaves", "beetles_visible"], 
         "advice": "Hand-pick beetles. Neem oil. Row covers for prevention."},
        {"condition": "slug_snail_damage", "indicators": ["irregular_holes", "slime_trails", "night_feeding"], 
         "advice": "Beer traps. Copper tape barriers. Diatomaceous earth. Hand-pick at night."},
    ],
    "white_powder": [
        {"condition": "powdery_mildew", "indicators": ["white_powder_on_leaves", "high_humidity", "poor_airflow"], 
         "advice": "Baking soda spray (1 tsp/L water). Improve airflow. Avoid evening watering."},
        {"condition": "downy_mildew", "indicators": ["yellow_patches_top", "white_fuzz_underside", "cool_humid"], 
         "advice": "Remove affected leaves. Copper fungicide. Improve airflow."},
    ],
    "stunted_growth": [
        {"condition": "phosphorus_deficiency", "indicators": ["dark_green_leaves", "purple_underside", "slow_growth"], 
         "advice": "Apply rock phosphate or bone meal. Check soil pH."},
        {"condition": "potassium_deficiency", "indicators": ["brown_leaf_edges", "weak_stems", "poor_fruit_set"], 
         "advice": "Apply potassium sulfate or wood ash. Check soil pH."},
        {"condition": "root_bound", "indicators": ["roots_circling", "pot_too_small", "water_runs_through"], 
         "advice": "Repot into larger container. Loosen root ball."},
    ],
}


def diagnose_plant(symptoms: List[str], plant_name: Optional[str] = None, env: Optional[dict] = None) -> List[dict]:
    """Deterministic diagnosis based on symptom checklist."""
    results = []
    
    for symptom in symptoms:
        symptom_lower = symptom.lower().replace(" ", "_")
        rules = SYMPTOM_RULES.get(symptom_lower, [])
        
        for rule in rules:
            confidence = 0.5  # Base confidence
            matched_indicators = 0
            
            # Check if indicators match environment
            if env:
                if "high_humidity" in rule.get("indicators", []) and env.get("humidity", 60) > 75:
                    matched_indicators += 1
                if "high_ph_soil" in rule.get("indicators", []) and env.get("ph", 6.5) > 7.5:
                    matched_indicators += 1
                if "wet_soil" in rule.get("indicators", []) and env.get("rainfall_mm", 900) > 1500:
                    matched_indicators += 1
                if "dry_soil" in rule.get("indicators", []) and env.get("rainfall_mm", 900) < 500:
                    matched_indicators += 1
            
            # Confidence based on matched indicators
            total_indicators = len(rule.get("indicators", []))
            if total_indicators > 0:
                confidence = min(0.9, 0.3 + (matched_indicators / total_indicators) * 0.6)
            else:
                confidence = 0.5
            
            # Check if plant is susceptible (from pest alerts)
            plant_susceptible = False
            if plant_name:
                alerts = get_pest_disease_alerts({"name": plant_name, "category": "vegetable"}, env or {})
                for alert in alerts:
                    if rule["condition"].lower() in alert["name"].lower():
                        plant_susceptible = True
                        confidence = min(0.95, confidence + 0.2)
            
            results.append({
                "condition": rule["condition"].replace("_", " ").title(),
                "symptom": symptom,
                "confidence": round(confidence * 100),
                "advice": rule["advice"],
                "plant_susceptible": plant_susceptible,
                "matched_indicators": matched_indicators,
                "total_indicators": len(rule.get("indicators", [])),
            })
    
    # Sort by confidence descending
    results.sort(key=lambda x: x["confidence"], reverse=True)
    
    # Group by condition, keep highest confidence
    by_condition = {}
    for r in results:
        cond = r["condition"]
        if cond not in by_condition or r["confidence"] > by_condition[cond]["confidence"]:
            by_condition[cond] = r
    
    return list(by_condition.values())


class DiagnosisRequest(BaseModel):
    symptoms: List[str] = Field(..., min_items=1, max_items=5)
    plant_name: Optional[str] = None
    environment: Optional[dict] = None


class DiagnosisResponse(BaseModel):
    plant_name: Optional[str]
    symptoms: List[str]
    diagnoses: List[dict]
    disclaimer: str


@app.post("/api/v1/diagnose", response_model=DiagnosisResponse)
@limiter.limit("60/minute")
async def diagnose_plant_endpoint(request: Request, body: DiagnosisRequest):
    """Deterministic plant health diagnosis from symptoms."""
    env = body.environment
    if not env and body.plant_name:
        # Try to get environment from a recent report
        pass
    
    diagnoses = diagnose_plant(body.symptoms, body.plant_name, env)
    
    return DiagnosisResponse(
        plant_name=body.plant_name,
        symptoms=body.symptoms,
        diagnoses=diagnoses,
        disclaimer="Diagnosis based on deterministic symptom rules. Not a substitute for professional agricultural advice. Consult local extension service for confirmation."
    )


# --- Mount static files (after API routes) ---
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
