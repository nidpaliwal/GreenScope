from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from functools import lru_cache
import uuid
import time
import json
import os
import sqlite3
import httpx


# --- Plant Knowledge Base ---

def load_plant_db():
    db_path = os.path.join(os.path.dirname(__file__), "plant_db.json")
    with open(db_path, "r") as f:
        return json.load(f)["plants"]


PLANTS = load_plant_db()

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
            processing_time_ms REAL
        )"""
    )
    conn.commit()
    return conn


def save_report_to_db(report):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO reports 
        (report_id, location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            } for r in report.recommendations]),
            report.generated_at,
            report.processing_time_ms,
        ),
    )
    conn.commit()
    conn.close()


def load_report_from_db(report_id):
    conn = get_db()
    row = conn.execute(
        "SELECT location, latitude, longitude, environment, photo_analysis, recommendations, generated_at, processing_time_ms FROM reports WHERE report_id = ?",
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

                    return {
                        "avg_temp_c": avg_temp,
                        "rainfall_mm": total_rain,
                        "humidity": humidity,
                        "sunlight_hours": sunlight,
                        "soil_type": soil,
                        "ph": ph,
                        "frost_risk": frost,
                        "agro_zone": f"Lat {lat:.1f}, Lng {lng:.1f}",
                    }, "live_api"
    except Exception:
        pass

    # Fallback to hardcoded Indian cities
    loc_key = location_str.lower().strip()
    for key, env in LOCATION_ENV.items():
        if key in loc_key:
            return env.copy(), "fallback"
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
    weights = {"climate": 0.25, "ph": 0.20, "sunlight": 0.15, "water": 0.15, "soil": 0.15, "photo": 0.10}
    total = sum(scores[k] * weights[k] for k in weights)
    total = round(min(100, max(0, total)), 1)

    return {"total": total, "breakdown": scores, "reasons": reasons}


# --- Photo Analysis (Pillow-based with filename fallback) ---

def analyze_photo(photo) -> dict:
    if not photo:
        return {}

    # Try Pillow-based analysis first
    if photo.base64:
        try:
            import base64
            from io import BytesIO
            from PIL import Image

            img_data = base64.b64decode(photo.base64.split(",")[1] if "," in photo.base64 else photo.base64)
            img = Image.open(BytesIO(img_data)).convert("RGB")
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

            features = []
            if has_sunlight:
                features.append("Good sunlight detected")
            else:
                features.append("Low light conditions detected")
            if has_vegetation:
                features.append("Green vegetation present")
            if soil_visible:
                features.append("Exposed soil visible")

            return {
                "has_sunlight": has_sunlight,
                "soil_visible": soil_visible,
                "has_vegetation": has_vegetation,
                "shade_level": shade_level,
                "detected_features": features or ["Open growing area", "Moderate sunlight", "Visible soil"],
            }
        except Exception:
            pass

    # Fallback: filename-based heuristics
    obs = {"has_sunlight": True, "soil_visible": True, "has_vegetation": True,
           "shade_level": "partial", "detected_features": []}
    if photo.filename:
        name_lower = photo.filename.lower()
        if "shade" in name_lower or "dark" in name_lower:
            obs["has_sunlight"] = False
            obs["shade_level"] = "full"
            obs["detected_features"].append("Low light conditions detected")
        if "soil" in name_lower or "ground" in name_lower:
            obs["detected_features"].append("Exposed soil visible")
        if "plant" in name_lower or "garden" in name_lower or "green" in name_lower:
            obs["detected_features"].append("Existing vegetation detected")
    if not obs["detected_features"]:
        obs["detected_features"] = [
            "Open growing area", "Green vegetation", "Moderate sunlight",
            "Visible soil", "No obvious standing water"
        ]
    return obs


def photo_obs_to_str(photo_obs: dict) -> str:
    if not photo_obs:
        return "No photo analysis available"
    lines = photo_obs.get("detected_features", [])
    return "; ".join(lines) if lines else "Photo analyzed"


# --- FastAPI App ---

app = FastAPI(title="GreenScope API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
        max_length=10_000_000  # ~10MB max
    )


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
async def geocode_endpoint(location: LocationInput):
    return await geocode_location(location)


@app.post("/api/v1/generate-report", response_model=ReportGenerateResponse)
async def generate_report(request: ReportGenerateRequest):
    start_time = time.time()
    report_id = str(uuid.uuid4())

    # Validate location string is not blank
    if not request.location.location or not request.location.location.strip():
        raise HTTPException(status_code=400, detail="Location cannot be empty")

    # Resolve location to coordinates using geocode function
    if request.location.latitude is None or request.location.longitude is None:
        geocoded = await geocode_location(request.location)
        lat = geocoded["latitude"]
        lng = geocoded["longitude"]
        location_str = geocoded["formatted"]
    else:
        lat = request.location.latitude
        lng = request.location.longitude
        location_str = request.location.location or "Unknown location"

    env, data_source = get_env_for_location(lat, lng, location_str)
    photo_obs = analyze_photo(request.photo)

    scored = []
    for plant in PLANTS:
        result = score_plant(plant, env, photo_obs)
        scored.append((plant, result))

    scored.sort(key=lambda x: x[1]["total"], reverse=True)

    top = scored[:request.num_recommendations]
    rejected = scored[request.num_recommendations:]

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

    photo_analysis = None
    if photo_obs:
        photo_analysis = {
            "features": photo_obs.get("detected_features", []),
            "shade_level": photo_obs.get("shade_level", "unknown"),
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
async def plant_this_month(location: str = "Delhi, NCR", latitude: float = None, longitude: float = None):
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
        )
        recs.append(rec)
    return ReportGenerateResponse(
        report_id=report_id,
        location=report.get('location', ''),
        latitude=report.get('latitude', 0.0),
        longitude=report.get('longitude', 0.0),
        environment=report.get('environment', {}),
        photo_analysis=report.get('photo_analysis'),
        recommendations=recs,
        generated_at=report.get('generated_at', time.time()),
        processing_time_ms=report.get('processing_time_ms', 0.0),
        disclaimer=ReportGenerateResponse.model_fields['disclaimer'].default,
    )


@app.get("/report/{report_id}", response_class=FileResponse)
async def serve_report_page(report_id: str):
    report_path = os.path.join(os.path.dirname(__file__), "..", "static", "report.html")
    return FileResponse(report_path)


# --- Mount static files (after API routes) ---
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
