# Google Review Automation 🤖⭐

An automated Google Maps review bot that uses **AI-generated reviews** (powered by Google Gemini) and **undetected browser automation** (SeleniumBase) to post reviews from multiple Google accounts to any Google Maps business listing — managed through a simple Flask web dashboard.

---

## Features

- **AI Review Generation** — Uses Gemini 2.5 Flash to write unique, natural-sounding reviews tailored to each business and star rating
- **Multi-Account Support** — Manage a pool of Google accounts, each with its own persistent Chrome profile to maintain login sessions
- **Per-Business Configuration** — Set custom star rating distributions, business descriptions, and review styles per business
- **Background Worker** — Standalone `worker.py` polls the database and processes review jobs independently from the web app
- **Web Dashboard** — Password-protected Flask UI to add businesses, manage email accounts, queue review jobs, and track status
- **Proxy Support** — Route all browser traffic through a configurable proxy

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Web App | Flask |
| Browser Automation | SeleniumBase (undetected Chrome) |
| AI Text Generation | Google Gemini 2.5 Flash |
| Database | SQLite (WAL mode) |
| Auth | Session-based with hashed password |

---

## Project Structure

```
.
├── app.py          # Flask web dashboard (auth, business/email management, job queuing)
├── bot.py          # Core automation: review generation + Selenium posting logic
├── db.py           # SQLite database layer
├── worker.py       # Background scheduler that processes pending review jobs
├── requirements.txt
├── chrome_profiles/  # Persistent Chrome profiles per Google account
├── templates/
│   ├── login.html
│   ├── dashboard.html
│   └── business.html
└── .env            # Environment variables (see below)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
SECRET_KEY=your_flask_secret_key
APP_PASSWORD=your_dashboard_password
GEMINI_API_KEY=your_google_gemini_api_key
PROXY=http://user:pass@host:port   # optional
```

### 3. Initialize the database

The database is auto-initialized on first run.

### 4. Run the web app

```bash
python app.py
```

Access the dashboard at `http://localhost:5000`.

### 5. Run the background worker

In a separate terminal:

```bash
python worker.py
```

The worker polls the database every 60 seconds and processes pending review jobs.

---

## How It Works

1. **Add a business** — Paste a Google Maps URL and configure star rating distribution and business description via the dashboard.
2. **Add email accounts** — Add Google account credentials to the shared email pool.
3. **Queue reviews** — Assign emails to a business to create pending review jobs.
4. **Worker picks up jobs** — The background worker generates a unique AI review for each job and posts it via an undetected Chrome browser logged into the corresponding Google account.
5. **Status tracking** — Each job transitions through `pending → reviewing → reviewed` (or back to `pending` on failure).

---

## Requirements

- Python 3.10+
- Google Chrome installed
- A valid Google Gemini API key
- Google accounts with access to post reviews

---

## Tags

`google-maps` `review-automation` `selenium` `seleniumbase` `flask` `google-gemini` `ai` `python` `bot` `web-scraping` `chrome-automation` `undetected-chromedriver` `sqlite` `review-bot` `ai-content-generation`

---

## Disclaimer

This project is for **educational purposes only**. Automating Google reviews may violate [Google's Terms of Service](https://policies.google.com/terms). Use responsibly.
