# GreenScope - AI Gardening Intelligence for India

## Discover what you can grow based on your location, soil, and garden conditions

GreenScope combines real climate data, plant science, and garden analysis to generate explainable plant recommendations with suitability scores, garden risk assessment, and comparison tables. India-focused with religious and health-beneficial plants.

## Quick Start

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` in a browser (single URL serves both API and frontend).

## How It Works

```
Location (any Indian city/village) + Photo
      |
      v
Real APIs: Nominatim geocoding + Open-Meteo climate data
      |
      v
Plant Database (50 Indian plants with growing requirements)
      |
      v
Scoring Engine (climate 25%, pH 20%, sunlight 15%, water 15%, soil 15%, photo 10%)
      |
      v
Top 4 Candidates with suitability scores + breakdown
      |
      v
Categories | Garden Risk | Comparison Table | Why Not?
```

## Features

- **Real Geocoding**: Nominatim/OpenStreetMap — works with any location in India
- **Real Climate Data**: Open-Meteo API — temperature, rainfall by coordinates
- **Scoring Engine**: Deterministic scoring based on climate, pH, sunlight, water, soil, photo
- **Indian Plant Database**: 50 plants including Tulsi, Neem, Ashwagandha, Brahmi, Curry Leaf
- **Religious & Health Focus**: Sacred plants with Ayurvedic benefits
- **Photo Analysis**: Pillow-based image analysis (brightness, vegetation, soil detection)
- **Environment Card**: Shows avg temp, rainfall, sunlight, pH, soil type, frost risk
- **Garden Risk Assessment**: Sunlight, water, temperature, soil health with warnings
- **Best For You Categories**: Best Overall, Low-Water, Fastest Harvest, Flower, Herb
- **Comparison Table**: Side-by-side plant comparison
- **Why Not?**: Explains why rejected plants scored lower
- **Custom Locations**: Type any location name (not limited to dropdown)

## API Endpoints

| Method | Endpoint                      | Description              |
|--------|-------------------------------|--------------------------|
| `GET`  | `/`                           | Serve frontend           |
| `POST` | `/api/v1/geocode`             | Geocode location (Nominatim) |
| `POST` | `/api/v1/generate-report`     | Generate gardening report|
| `GET`  | `/api/v1/reports/{id}`        | Retrieve report data     |
| `GET`  | `/report/{id}`                | Share report page        |
| `GET`  | `/api/v1/plant-this-month`    | Seasonal planting calendar|
| `GET`  | `/api/v1/health`              | Health check             |

## What's Real vs Mocked

| Feature              | Status | Details                                         |
|----------------------|--------|-------------------------------------------------|
| Geocoding            | Real   | Nominatim/OpenStreetMap (any location in India) |
| Climate data         | Real   | Open-Meteo API (temperature, rainfall by coords)|
| Scoring engine       | Real   | Deterministic scoring from plant DB             |
| Plant database       | Real   | 50 Indian plants with full growing requirements |
| Photo analysis       | Real   | Pillow-based brightness/vegetation analysis     |
| Persistence          | Real   | SQLite (reports persist to `reports.db`)         |
| Soil type inference  | Mocked | Based on latitude/region heuristics              |

## Scoring Algorithm

| Factor   | Weight | What It Measures                              |
|----------|--------|-----------------------------------------------|
| Climate  | 25%    | Temperature range match                       |
| pH       | 20%    | Soil pH compatibility                         |
| Sunlight | 15%    | Sun hours vs requirement                      |
| Water    | 15%    | Rainfall vs water needs                       |
| Soil     | 15%    | Soil type match                               |
| Photo    | 10%    | Garden observation bonus (Pillow analysis)    |

## Supported Locations

Works with **any location in India** via Nominatim geocoding. Pre-configured with 16 major cities:

Mumbai, Delhi, Bengaluru, Chennai, Kolkata, Hyderabad, Pune, Jaipur, Ahmedabad, Lucknow, Bhopal, Patna, Shimla, Srinagar, Thiruvananthapuram, Udaipur.

Plus free-text input for any other location (Varanasi, Kochi, Indore, etc.)

## Religious & Health Plants

| Plant | Benefits |
|-------|----------|
| Tulsi | Sacred basil, immunity booster, respiratory health |
| Neem | Natural pest repellent, blood purifier, Ayurvedic |
| Ashwagandha | Adaptogen for stress, strength, vitality |
| Brahmi | Brain tonic, memory, cognition |
| Amla | Richest Vitamin C source, immunity |
| Giloy | Immunity booster, fever treatment |
| Curry Leaf | Digestive health, blood sugar control |
| Aloe Vera | Skin healer, digestive aid |
| Lemongrass | Mosquito repellent, tea, ceremonies |
| Moringa | Superfood, all parts edible |
| Turmeric | Anti-inflammatory, sacred in rituals |
| Coriander | Digestive, cholesterol control |
| Fenugreek | Diabetes control, fasting food |
| Marigold | Sacred flower, pest deterrent |

## Tech Stack

- **Backend:** FastAPI, Pydantic v2, Python 3.10+, Pillow, httpx
- **Frontend:** HTML5, Tailwind CSS (CDN), Vanilla JS
- **APIs:** Nominatim (geocoding), Open-Meteo (climate)
- **Data:** JSON plant database, SQLite persistence (`reports.db`)
- **Tests:** pytest (38 tests passing)

## Tests

```bash
python -m pytest tests/ -v
```

## Project Structure

```
GreenScope/
  app/
    main.py           # FastAPI backend + scoring engine + API integrations
    plant_db.json     # Indian plant knowledge base (50 plants)
  static/
    index.html        # Frontend with custom location input
    report.html       # Shareable report page
  tests/
    test_api.py       # API tests (38 cases)
  requirements.txt
  .gitignore
  README.md
```

---

**GreenScope** - Explainable garden intelligence for Indian spaces.
