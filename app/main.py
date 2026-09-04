from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
import time

app = FastAPI(title="GreenScope API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request/Response Models

class LocationInput(BaseModel):
    location: str = Field(..., description="Address, coordinates, or location description")
    latitude: Optional[float] = Field(None, description="Latitude if provided")
    longitude: Optional[float] = Field(None, description="Longitude if provided")


class PhotoUpload(BaseModel):
    image_id: str = Field(..., description="Uploaded image identifier")
    filename: str = Field(..., description="Original filename")
    base64: Optional[str] = Field(None, description="Base64 encoded image data")


class Recommendation(BaseModel):
    plant_name: str = Field(..., description="Recommended plant/fruit/flower name")
    scientific_name: Optional[str] = Field(None, description="Scientific/binomial name")
    reasoning: str = Field(..., description="Why this plant suits the location")
    confidence_tp_percent: float = Field(..., ge=0, le=100, description="True-Positive confidence percentage")
    care_guide: str = Field(..., description="Basic care instructions")
    growth_duration: Optional[str] = Field(None, description="Approximate time to maturity/harvest")


class ReportGenerateRequest(BaseModel):
    location: LocationInput
    photo: Optional[PhotoUpload] = Field(None, description="Optional uploaded garden photo")
    num_recommendations: int = Field(3, ge=1, le=10, description="Number of plant recommendations")


class ReportRecommendation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plant_name: str
    scientific_name: Optional[str]
    reasoning: str
    confidence_tp_percent: float
    care_guide: str
    growth_duration: Optional[str]


class ReportGenerateResponse(BaseModel):
    report_id: str
    location: str
    recommendations: List[ReportRecommendation]
    generated_at: float
    processing_time_ms: float
    disclaimer: str = Field(
        default="True-Positive % is an estimated confidence metric, not a guaranteed outcome. "
        "Results should be verified with local gardening advice."
    )


# In-memory "database" for demo purposes
reports_db = {}


@app.get("/")
async def root():
    return {"message": "GreenScope API is running", "version": "1.0.0"}


@app.post("/api/v1/geocode", response_model=dict)
async def geocode_location(location: LocationInput):
    """Geocode a location string into latitude/longitude."""
    # In a real implementation, this would call Google Maps/OSM API
    # For demo, return mock data based on common locations
    mock_geocodes = {
        "san francisco, ca": (37.7749, -122.4194),
        "new york, ny": (40.7128, -74.0060),
        "austin, tx": (30.2672, -97.7431),
        "seattle, wa": (47.6062, -122.3321),
        "denver, co": (39.7392, -104.9903),
    }
    loc_key = location.location.lower()
    for key, (lat, lng) in mock_geocodes.items():
        if key in loc_key:
            return {"latitude": lat, "longitude": lng, "formatted": location.location}
    
    # Default to a generic location if not found
    return {
        "latitude": 37.7749,
        "longitude": -122.4194,
        "formatted": location.location or "Unknown location"
    }


@app.post("/api/v1/generate-report", response_model=ReportGenerateResponse)
async def generate_report(request: ReportGenerateRequest):
    """Generate a gardening report based on location and optional photo."""
    start_time = time.time()
    report_id = str(uuid.uuid4())
    
    lat = request.location.latitude or 37.7749
    lng = request.location.longitude or -122.4194
    location_str = request.location.location or "Unknown location"
    
    # Determine climate zone based on latitude for deterministic recommendation mapping
    # This ensures different locations produce visibly different outputs
    if lat >= 40:
        climate_zone = "cool_temperate"
    elif lat >= 30:
        climate_zone = "temperate"
    elif lat >= 20:
        climate_zone = "subtropical"
    else:
        climate_zone = "tropical"
    
    # Simulate fetching environmental data
    # In real implementation: SoilGrids, OpenWeatherMap, Sentinel Hub APIs
    soil_data = {
        "soil_type": "Loamy",
        "ph": 6.5,
        "rainfall": "Moderate",
    }
    
    # Simulate image analysis if photo provided
    vegetation_info = {}
    if request.photo:
        # In real implementation: PlantNet/Plant.id API
        vegetation_info = {
            "detected_shade": "Partial shade",
            "soil_visibility": "Good",
            "existing_vegetation": "Mixed grasses",
        }
    
    # Generate recommendations based on climate zone
    # Deterministic mapping: different locations produce different plant selections
    if climate_zone == "cool_temperate":
        # Cool climates (e.g., Northern US, Canada, Northern Europe)
        recommendations = [
            ReportRecommendation(
                plant_name="Lavender",
                scientific_name="Lavandula angustifolia",
                reasoning="Thrives in well-draining loamy soil with pH 6.5; drought-tolerant; loves full sun typical of your location's climate. Lavender's Mediterranean origin matches your area's rainfall patterns.",
                confidence_tp_percent=87.5,
                care_guide="Water moderately until established; then reduce watering. Prefers 6-8 hours of direct sunlight. Trim after flowering to maintain shape.",
                growth_duration="2-3 years for full maturity; flowers appear in summer year 1."
            ),
            ReportRecommendation(
                plant_name="Tomatoes",
                scientific_name="Solanum lycopersicum",
                reasoning="Requires full sun, warm climate, and well-draining soil with pH 6.5-6.8. Your location's moderate rainfall and loamy soil are ideal for tomato cultivation. Heat-tolerant varieties will perform well.",
                confidence_tp_percent=72.3,
                care_guide="Water deeply 1-2 inches per week; more during fruiting. Provide 6-8 hours minimum sunlight. stake plants as they grow. Harvest 60-85 days after transplanting.",
                growth_duration="60-85 days from transplant to first harvest."
            ),
            ReportRecommendation(
                plant_name="Marigolds",
                scientific_name="Tagetes erecta",
                reasoning="Hardy annual that adapts to various soil types including your loamy soil. Tolerates partial shade from your detected vegetation. Natural pest deterrent companion plant. Matches your area's rainfall pattern.",
                confidence_tp_percent=65.8,
                care_guide="Water regularly but allow soil to dry between waterings. Full sun to partial shade. Blooms summer through first frost. Easy to grow from seed.",
                growth_duration="8-10 weeks from seed to first bloom."
            ),
        ]
    elif climate_zone == "temperate":
        # Temperate climates (e.g., Central US, Western Europe, mild coastal)
        recommendations = [
            ReportRecommendation(
                plant_name="Roses",
                scientific_name="Rosa hybrida",
                reasoning="Thrives in well-draining loamy soil with pH 6.5; classic choice for temperate climates with moderate rainfall. Roses appreciate the balanced conditions your location provides.",
                confidence_tp_percent=82.1,
                care_guide="Water deeply 1-2 inches weekly; more during hot spells. Mulch around base to retain moisture. Prune in late winter or early spring. Feed with rose fertilizer every 4-6 weeks.",
                growth_duration="Perennial; blooms from late spring through fall."
            ),
            ReportRecommendation(
                plant_name="Basil",
                scientific_name="Ocimum basilicum",
                reasoning="Thrives in warm conditions with well-draining soil and pH near 6.5. Your location's moderate climate is excellent for basil production. Benefits from the same growing conditions as tomatoes.",
                confidence_tp_percent=78.4,
                care_guide="Water when top inch of soil is dry; prefers 6-8 hours sunlight. Pinch center growth for bushier plants. Harvest leaves before flowering for best flavor.",
                growth_duration="6-8 weeks from planting to first harvest of leaves."
            ),
            ReportRecommendation(
                plant_name="Kale",
                scientific_name="Brassica oleracea",
                reasoning="Cold-tolerant leafy green that thrives in your temperate climate. Kale appreciates the moderate rainfall and can withstand light frosts common in your area.",
                confidence_tp_percent=71.2,
                care_guide="Water consistently; soil should remain moist but not waterlogged. Harvest outer leaves first to encourage continued growth.",
                growth_duration="50-65 days from transplant to first harvest."
            ),
        ]
    elif climate_zone == "subtropical":
        # Subtropical climates (e.g., Southern US, parts of Australia, Southeast Asia)
        recommendations = [
            ReportRecommendation(
                plant_name="Basil",
                scientific_name="Ocimum basilicum",
                reasoning="Thrives in warm climates with well-draining soil and pH near 6.5. Your location's conditions are excellent for basil. Benefits from the same growing conditions as tomatoes, making it a great companion plant.",
                confidence_tp_percent=85.7,
                care_guide="Water when top inch of soil is dry; prefers 6-8 hours sunlight. Pinch center growth for bushier plants. Harvest leaves before flowering for best flavor.",
                growth_duration="6-8 weeks from planting to first harvest of leaves."
            ),
            ReportRecommendation(
                plant_name="Peppers",
                scientific_name="Capsicum annuum",
                reasoning="Heat-loving plants that excel in your subtropical climate. Peppers require full sun and well-draining soil; your location's conditions are ideal for both hot and sweet varieties.",
                confidence_tp_percent=79.3,
                care_guide="Water consistently; allow soil to dry slightly between waterings. Provide 6-8 hours minimum sunlight. Stake plants as they grow. Harvest when fruits reach desired size.",
                growth_duration="60-90 days from transplant to first harvest."
            ),
            ReportRecommendation(
                plant_name="Citrus",
                scientific_name="Citrus × sinensis",
                reasoning="Citrus trees thrive in warm, frost-free conditions. Your subtropical climate provides the ideal growing season for orange and lemon trees.",
                confidence_tp_percent=74.6,
                care_guide="Water deeply but infrequently; allow soil to dry between waterings. Fertilize with citrus-specific fertilizer in spring and summer. Protect from frost if temperatures drop below freezing.",
                growth_duration="3-5 years for significant harvest; flowers in spring."
            ),
        ]
    else:
        # Tropical climates (e.g., Southern Florida, Hawaii, Southeast Asia, Amazon)
        recommendations = [
            ReportRecommendation(
                plant_name="Heliconia",
                scientific_name="Heliconia rostrata",
                reasoning="Exotic tropical plant that thrives in your hot, humid climate. Heliconia adds vibrant color to gardens and attracts hummingbirds.",
                confidence_tp_percent=81.3,
                care_guide="Keep soil consistently moist; prefers partial to full shade. High humidity is beneficial. Protect from strong direct sunlight.",
                growth_duration="Perennial; blooms year-round in ideal conditions."
            ),
            ReportRecommendation(
                plant_name="Ginger",
                scientific_name="Zingiber officinale",
                reasoning="Tropical perennial that flourishes in your climate's heat and humidity. Ginger rhizomes can be harvested continuously once established.",
                confidence_tp_percent=76.8,
                care_guide="Keep soil consistently moist but well-draining. Partial shade is preferred. Harvest rhizomes after 8-10 months.",
                growth_duration="8-10 months to harvest rhizomes."
            ),
            ReportRecommendation(
                plant_name="Philodendron",
                scientific_name="Philodendron hederaceum",
                reasoning="Hardy tropical indoor/outdoor plant that thrives in your high-rainfall environment. Philodendron is nearly indestructible in tropical conditions.",
                confidence_tp_percent=73.1,
                care_guide="Water when top inch of soil dries. Tolerates low light but prefers indirect bright light. Wipe leaves occasionally to remove dust.",
                growth_duration="Perennial; grows continuously in suitable conditions."
            ),
        ]
    
    # If user requested more than 3, add additional recommendations for the zone
    # (subtropical and tropical have only 3 by design; temperate/cool can add more)
    base_count = len(recommendations)
    if request.num_recommendations > base_count:
        if climate_zone == "cool_temperate":
            recommendations.append(ReportRecommendation(
                plant_name="Marigolds",
                scientific_name="Tagetes erecta",
                reasoning="Hardy annual that adapts to various soil types including your loamy soil. Tolerates partial shade from your detected vegetation. Natural pest deterrent companion plant. Matches your area's rainfall pattern.",
                confidence_tp_percent=65.8,
                care_guide="Water regularly but allow soil to dry between waterings. Full sun to partial shade. Blooms summer through first frost. Easy to grow from seed.",
                growth_duration="8-10 weeks from seed to first bloom."
            ))
        elif climate_zone == "temperate":
            recommendations.append(ReportRecommendation(
                plant_name="Lavender",
                scientific_name="Lavandula angustifolia",
                reasoning="Thrives in well-draining loamy soil with pH 6.5; drought-tolerant; loves full sun typical of your location's climate.",
                confidence_tp_percent=87.5,
                care_guide="Water moderately until established; then reduce watering. Prefers 6-8 hours of direct sunlight. Trim after flowering to maintain shape.",
                growth_duration="2-3 years for full maturity; flowers appear in summer year 1."
            ))
        elif climate_zone == "subtropical":
            recommendations.append(ReportRecommendation(
                plant_name="Marigolds",
                scientific_name="Tagetes erecta",
                reasoning="Hardy annual that adapts to various soil types. Natural pest deterrent companion plant.",
                confidence_tp_percent=65.8,
                care_guide="Water regularly but allow soil to dry between waterings. Full sun to partial shade. Blooms summer through first frost. Easy to grow from seed.",
                growth_duration="8-10 weeks from seed to first bloom."
            ))
        elif climate_zone == "tropical":
            recommendations.append(ReportRecommendation(
                plant_name="Heliconia",
                scientific_name="Heliconia rostrata",
                reasoning="Exotic tropical plant that thrives in your hot, humid climate. Adds vibrant color and attracts hummingbirds.",
                confidence_tp_percent=81.3,
                care_guide="Keep soil consistently moist; prefers partial to full shade. High humidity is beneficial.",
                growth_duration="Perennial; blooms year-round in ideal conditions."
            ))
    
    processing_time_ms = (time.time() - start_time) * 1000
    
    response = ReportGenerateResponse(
        report_id=report_id,
        location=location_str,
        recommendations=recommendations,
        generated_at=time.time(),
        processing_time_ms=round(processing_time_ms, 2),
    )
    
    reports_db[report_id] = response
    return response


@app.get("/api/v1/reports/{report_id}", response_model=ReportGenerateResponse)
async def get_report(report_id: str):
    """Retrieve a previously generated report."""
    if report_id not in reports_db:
        raise HTTPException(status_code=404, detail="Report not found")
    return reports_db[report_id]


@app.get("/api/v1/health", response_model=dict)
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "GreenScope API"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)