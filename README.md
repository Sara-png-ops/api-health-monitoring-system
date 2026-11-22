# API Health Monitoring System

A Streamlit dashboard for checking whether API endpoints are reachable, how fast they respond, and how that changes over time. Every check sends a real HTTP GET request, and every result is stored in a local SQLite database.

**Checks run only when you press "Run checks now".** The app does not monitor in the background, so the history contains only the checks you started yourself.

## Features

- Add endpoints by URL, with validation before anything is requested. Remove endpoints at any time.
- Sample public endpoints, clearly labelled, one of which fails on purpose so you can see an unhealthy result.
- Real HTTP GET requests with a 10-second timeout.
- For each check: HTTP status code, result (Healthy, Unhealthy or Error), response time in milliseconds and a UTC timestamp.
- Dashboard cards for endpoints checked, healthy, unhealthy and error counts, a status table and a latest-response-time chart.
- Persistent history in SQLite, a response-time trend chart and a filter by endpoint.
- Downloadable CSV report with the endpoint URL, timestamp, HTTP status, response time and result.

## How results are classified

| Result | Meaning |
| --- | --- |
| Healthy | The final HTTP status is 200-399. Redirects are followed. |
| Unhealthy | The server replied with a 4xx or 5xx status. |
| Error | No response was received: timeout, DNS or connection failure, SSL problem, redirect loop or invalid URL. |

**Response time** is measured from just before the request is sent until the response headers arrive (DNS, connection, TLS and server processing). The response body is never downloaded. Failed requests have no response time.

No uptime percentages or other statistics are invented. The dashboard only counts and plots checks that are stored in the database.

## Technologies used

- Python 3.10 or newer
- Streamlit (dashboard)
- Requests (HTTP checks)
- SQLite (history, via Python's built-in `sqlite3`)
- Pandas (tables)
- Plotly (charts)
- Pytest (tests)

## Project structure

```
api-health-monitoring-system/
├── app.py                  Streamlit dashboard
├── api_monitor.py          URL validation, HTTP checks, CSV report
├── database.py             SQLite storage
├── requirements.txt
├── README.md
├── .gitignore
├── .streamlit/config.toml  Lavender theme
├── screenshots/            Put your screenshots here
└── tests/
    └── test_api_monitor.py
```

The database file `monitoring.db` is created in the project folder the first time you run the app. It is ignored by Git.

## Installation

```bash
git clone https://github.com/<your-username>/api-health-monitoring-system.git
cd api-health-monitoring-system

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
```

## Running the application

```bash
streamlit run app.py
```

Open the address Streamlit prints (normally http://localhost:8501), then:

1. Add an endpoint in the sidebar, or open **Sample public endpoints** and select **Add all sample endpoints**.
2. Press **Run checks now**.
3. Review the **Overview** tab, then open **History and report** to filter by endpoint, view the trend and download the CSV.

## Running the tests

```bash
python -m pytest -v
```

The tests mock HTTP responses and never contact external APIs. A few tests send real requests to a temporary server on `127.0.0.1` to confirm that genuine status codes, timeouts and timing are handled correctly. A dashboard test drives the Streamlit app through adding, checking and removing endpoints.

## Screenshots

After running the app, save screenshots into the `screenshots/` folder and link them here:

```markdown
![Overview](screenshots/overview.png)
![History and report](screenshots/history.png)
```

## Security notes

- URLs are stored in plain text, so the app rejects URLs containing a username, a password or a query parameter such as `api_key`, `token` or `secret`.
- Requests send only a `User-Agent` header. No custom headers or credentials are supported or stored.
- CSV cells that start with `=`, `+`, `-` or `@` are prefixed with an apostrophe so spreadsheet programs do not run them as formulas.

## Limitations

- Checks are manual. There is no scheduler or background monitoring.
- Only HTTP GET requests are supported, with no custom headers, authentication or request bodies. Endpoints that require credentials cannot be monitored.
- A healthy result means the server returned a 2xx or 3xx status. The response content is not inspected.
- Response time excludes downloading the response body and reflects your own network connection, not the endpoint's global performance.
- Timestamps are in UTC.
- The history table loads the 1,000 most recent checks. The CSV report contains the same rows.
- Removing an endpoint also deletes its history.
- The database is a local file, which suits one user on one machine and is not designed for several users at once.
- The app can request any HTTP or HTTPS address, including `localhost` and private network addresses. Run it only on a machine you trust and do not deploy it publicly without adding restrictions.
