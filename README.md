# GreenScope - AI Gardening Report Generator

## 🌿 Turn a photo and pin drop into a personalized growing report

GreenScope generates AI-powered gardening reports based on your location, soil conditions, and garden photo. Select your location, upload a photo, and get tailored plant recommendations with confidence percentages and care guides.

## 📸 Live Demo

**Visit the demo:** The frontend is deployed at `static/index.html` - open it in any modern browser.

**How to use:**
1. Select a location from the dropdown (or enter custom coordinates)
2. optionally upload a garden photo
3. Click **"Generate My Report"** (button is now enabled when a valid location is selected)
4. View your personalized gardening report with plant recommendations

> ⚠️ **Note:** The submit button is now enabled/disabled based on location selection. If it appears disabled, please select a location first.

## 🏗️ Architecture

```text
+----------------------+       +----------------------+       +----------------------+
|  static/index.html    | <-->  |  app/main.py (FastAPI)| <-->  |  External APIs (TBD) |
|  - HTML + JS frontend|       |  - Pydantic models   |       |  - Nominatim geocoding|
|  - Tailwind CSS      |       |  - API endpoints     |       |  - PlantNet analysis  |
|  - Vanilla JS        |       |  - In-memory DB      |       |  - Claude LLM         |
+----------------------+       +----------------------+       +----------------------+
```

## 🛠️ Tech Stack

| Layer      | Technology                                |
| ---------- | ----------------------------------------- |
| Frontend   | HTML5, Tailwind CSS via CDN, Vanilla JS   |
| Backend    | FastAPI (Python)                          |
| Models     | Pydantic v2                               |
| Geocoding  | Mock data (5 cities) — Nominatim planned  |
| Report gen | Mock rule-based recommendations — LLM planned|
| Deployment | Static files served via any web server    |

## 📦 What's Mocked vs Real

| Feature         | Status        | Details                                |
| --------------- | ------------- | -------------------------------------- |
| **Geocoding**   | ⚠️ Mocked     | 5 hardcoded cities; others default to SF coords |
| **Photo analysis** | ⚠️ Mocked   | Placeholder vegetation info; not used in recommendations |
| **Recommendations** | ⚠️ Mocked   | Static plant list; same output regardless of input |
| **Confidence scores** | ⚠️ Static | Fixed percentages (87.5%, 72.3%, etc.) |
| **CORS**        | ✅ Fixed      | `allow_origins=["*"]` with `allow_credentials=False` |
| **Persistence** | ⚠️ In-memory  | `reports_db = {}` — lost on server restart |

## 🚀 Setup & Run

### Prerequisites

- Python 3.10+
- pip (Python package installer)

### Install & Start Backend

```bash
# 1. Clone and cd into project
cd GreenScope

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the FastAPI server
uvicorn app.main:app --reload --port 8000
```

### Run Frontend

Open `static/index.html` in any modern web browser. No backend needed for basic demo, but the form will connect to `http://localhost:8000` if the backend is running.

### Requirements (`requirements.txt`)

```
fastapi
uvicorn[standard]
pydantic[dotenv]
```

## 📡 API Endpoints

| Method | Endpoint                | Description                    |
| ------ | ----------------------- | ------------------------------ |
| `GET`  | `/`                     | Root health check              |
| `POST` | `/api/v1/geocode`       | Geocode location to lat/lng    |
| `POST` | `/api/v1/generate-report` | Generate gardening report      |
| `GET`  | `/api/v1/reports/{id}`  | Retrieve generated report      |
| `GET`  | `/api/v1/health`        | Health check                   |

## 📋 Roadmap

| Priority | Feature                          | Est. Effort |
| -------- | -------------------------------- | ----------- |
| 🔴 **High**    | Fix submit button + crash (done)       | 15 min      |
| 🔴 **High**    | Fix UUID bug + delete duplicate models | 10 min      |
| 🟠 **Medium**  | Fix CORS config (done)                 | 5 min       |
| 🟠 **Medium**  | Vary recommendations by input          | 30 min      |
| 🟡 **Low**     | Add pytest tests (3-4 cases)           | 20 min      |
| 🟡 **Low**     | Real geocoding via Nominatim/OSM       | 1-2 hours   |
| 🟢 **Low**     | Claude LLM-based report generation     | 2-3 hours   |
| 🟢 **Low**     | PDF export with real data              | 1 hour      |
| 🟢 **Low**     | User persistence (DB migration)        | 1-2 hours   |

## 🙏 Acknowledgments

- UI built with [Tailwind CSS](https://tailwindcss.com) via CDN
- Icons from [Heroicons](https://heroicons.com)
- FastAPI for the excellent Python API framework

---

**GreenScope** — Turning your garden into a data-driven growing guide. 🌱