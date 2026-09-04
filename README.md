# GreenScope - AI Gardening Intelligence

## Discover what you can grow based on your location, soil, and garden conditions

GreenScope combines environmental data, plant science, and garden analysis to generate explainable plant recommendations with suitability scores, garden risk assessment, and comparison tables.

## Quick Start

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000` in a browser (single URL serves both API and frontend).

## How It Works

```
Location + Photo
      |
      v
Environmental Data (temp, rainfall, pH, soil, sunlight)
      |
      v
Plant Database (15 plants with growing requirements)
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

- **Scoring Engine**: Deterministic scoring based on climate, pH, sunlight, water, soil, photo
- **Environment Card**: Shows avg temp, rainfall, sunlight, pH, soil type, frost risk
- **Garden Risk Assessment**: Sunlight, water, temperature, soil health with warnings
- **Best For You Categories**: Best Overall, Low-Water, Fastest Harvest, Flower, Herb
- **Comparison Table**: Side-by-side plant comparison
- **Why Not?**: Explains why rejected plants scored lower
- **Photo Analysis**: Detects garden features from uploaded images

## API Endpoints

| Method | Endpoint                      | Description              |
|--------|-------------------------------|--------------------------|
| `GET`  | `/`                           | Serve frontend           |
| `POST` | `/api/v1/geocode`             | Geocode location         |
| `POST` | `/api/v1/generate-report`     | Generate gardening report|
| `GET`  | `/api/v1/reports/{id}`        | Retrieve report data     |
| `GET`  | `/report/{id}`                | Share report page        |
| `GET`  | `/api/v1/plant-this-month`    | Seasonal planting calendar|
| `GET`  | `/api/v1/health`              | Health check             |

## Scoring Algorithm

| Factor   | Weight | What It Measures                              |
|----------|--------|-----------------------------------------------|
| Climate  | 25%    | Temperature range match                       |
| pH       | 20%    | Soil pH compatibility                         |
| Sunlight | 15%    | Sun hours vs requirement                      |
| Water    | 15%    | Rainfall vs water needs                       |
| Soil     | 15%    | Soil type match                               |
| Photo    | 10%    | Garden observation bonus                      |

## What's Real vs Mocked

| Feature              | Status   | Details                                    |
|----------------------|----------|--------------------------------------------|
| Scoring engine       | Real     | Deterministic scoring from plant DB        |
| Plant database       | Real     | 15 plants with full growing requirements   |
| Environment data     | Mocked   | 5 cities with climate data                 |
| Photo analysis       | Mocked   | Rule-based from filename heuristics         |
| Geocoding            | Mocked   | 5 hardcoded city coordinates               |
| Persistence          | SQLite   | Reports persist to `reports.db`             |

## Tech Stack

- **Backend:** FastAPI, Pydantic v2, Python 3.10+
- **Frontend:** HTML5, Tailwind CSS (CDN), Vanilla JS
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
    main.py           # FastAPI backend + scoring engine
    plant_db.json     # Plant knowledge base (15 plants)
  static/
    index.html        # Frontend with environment cards, categories, risk
  tests/
    test_api.py       # API tests (38 cases)
  requirements.txt
  .gitignore
  README.md
```

---

**GreenScope** - Explainable garden intelligence for your space.
