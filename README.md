# GreenScope - AI Gardening Intelligence

## Discover what you can grow based on your location, soil, and garden conditions

GreenScope combines environmental data, plant science, and garden analysis to generate explainable plant recommendations with suitability scores.

## How It Works

```
Location + Photo
      |
      v
Environmental Data (temp, rainfall, pH, soil, sunlight)
      |
      v
Plant Database (10 plants with growing requirements)
      |
      v
Scoring Engine (climate 25%, pH 20%, sunlight 15%, water 15%, soil 15%, photo 10%)
      |
      v
Top 4 Candidates with suitability scores + breakdown
```

## Quick Start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open `static/index.html` in a browser and select a location.

## Architecture

```
static/index.html          app/main.py              app/plant_db.json
  (Frontend)        --->   (FastAPI API)        ---> (Plant Database)
                              |
                      Scoring Engine
                              |
                      Recommendation Response
                              +--- environment data
                              +--- photo analysis
                              +--- plant recommendations
                              +--- score breakdowns
```

## API Endpoints

| Method | Endpoint                | Description                      |
|--------|-------------------------|----------------------------------|
| `GET`  | `/`                     | Health check                     |
| `POST` | `/api/v1/geocode`       | Geocode location to lat/lng      |
| `POST` | `/api/v1/generate-report` | Generate gardening report        |
| `GET`  | `/api/v1/reports/{id}`  | Retrieve generated report        |
| `GET`  | `/api/v1/health`        | Health check                     |

## Scoring Algorithm

Each plant is scored against your location's environmental conditions:

| Factor      | Weight | What It Measures                              |
|-------------|--------|-----------------------------------------------|
| Climate     | 25%    | How well plant temperature range matches location |
| pH          | 20%    | Soil pH compatibility                         |
| Sunlight    | 15%    | Sun hours vs plant requirement (full/partial) |
| Water       | 15%    | Rainfall vs plant water needs                 |
| Soil        | 15%    | Soil type match                               |
| Photo       | 10%    | Garden observation bonus                      |

## What's Real vs Mocked

| Feature              | Status   | Details                                    |
|----------------------|----------|--------------------------------------------|
| **Scoring engine**   | Real     | Deterministic scoring based on plant DB    |
| **Plant database**   | Real     | 10 plants with full growing requirements   |
| **Environment data** | Mocked   | 5 cities with climate data                 |
| **Photo analysis**   | Mocked   | Rule-based from filename heuristics         |
| **Geocoding**        | Mocked   | 5 hardcoded city coordinates               |
| **Persistence**      | In-memory| Reports lost on server restart             |

## Tech Stack

- **Backend:** FastAPI, Pydantic v2, Python 3.10+
- **Frontend:** HTML5, Tailwind CSS (CDN), Vanilla JS
- **Data:** JSON plant database, in-memory report store
- **Tests:** pytest (15 tests passing)

## Tests

```bash
python -m pytest tests/ -v
```

## Project Structure

```
GreenScope/
  app/
    main.py           # FastAPI backend + scoring engine
    plant_db.json     # Plant knowledge base
  static/
    index.html        # Frontend
  tests/
    test_api.py       # API tests (15 cases)
  requirements.txt
  README.md
```

---

**GreenScope** - Explainable garden intelligence for your space.
