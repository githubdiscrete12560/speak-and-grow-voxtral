# Speak & Grow split deployment

Speak & Grow is a static six-tab browser application backed by FastAPI. The frontend is deployed to Netlify and calls the Cloud Run API directly over HTTPS/WSS. The backend connects to Mistral Voxtral, Google Calendar/Meet, and Google Secret Manager. No browser bundle contains API keys, OAuth credentials, tokens, therapist-private values, or consultation data, and the app does not store child or consultation data in `localStorage`.

## Repository layout

- `frontend/`: static site, public backend configuration, Netlify headers and SPA fallback
- `backend/`: FastAPI service, Python dependencies, Cloud Run container files
- `tests/`: API, OAuth, Calendar, origin, and storage tests

`render.yaml` is intentionally absent; Cloud Run is the selected backend deployment.

## Local development (PowerShell)

Create and activate a virtual environment from the project root, then install `backend/requirements.txt`. Copy `.env.example` to `.env` only if you do not already have a real `.env`; never overwrite an existing one.

Terminal 1:

```powershell
cd backend
uvicorn backend:app `
  --reload `
  --reload-dir . `
  --host 0.0.0.0 `
  --port 8000
```

Terminal 2:

```powershell
cd frontend
python -m http.server 5500
```

Keep `frontend/config.js` set to `http://localhost:8000`. Development server settings are:

```dotenv
ENVIRONMENT=development
FRONTEND_PUBLIC_URL=http://localhost:5500
BACKEND_PUBLIC_URL=http://localhost:8000
FRONTEND_ORIGINS=http://localhost:5500
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
GOOGLE_TOKEN_STORAGE=file
GOOGLE_TOKEN_FILE=google_token.json
```

Open `http://localhost:5500`. Authorize Calendar by navigating directly to `http://localhost:8000/auth/google/login`; this top-level navigation lets the signed, SameSite=Lax backend cookie survive the OAuth redirect. The frontend does not send this OAuth session cookie with API fetches.

## Cloud Run configuration

Set these values on the service (Console → Cloud Run → service → Edit and deploy new revision → Variables and secrets):

```dotenv
ENVIRONMENT=production
MISTRAL_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://YOUR_CLOUD_RUN_DOMAIN/auth/google/callback
GOOGLE_CALENDAR_ID=primary
GOOGLE_TOKEN_STORAGE=secret_manager
GOOGLE_TOKEN_SECRET_NAME=speak-and-grow-google-token
GCP_PROJECT_ID=
THERAPIST_NAME=
THERAPIST_EMAIL=
APP_TIMEZONE=Asia/Bangkok
SESSION_SECRET=
FRONTEND_PUBLIC_URL=https://YOUR_NETLIFY_SITE.netlify.app
BACKEND_PUBLIC_URL=https://YOUR_CLOUD_RUN_DOMAIN
FRONTEND_ORIGINS=https://YOUR_NETLIFY_SITE.netlify.app
```

Store `MISTRAL_API_KEY`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`, and OAuth token credentials in Secret Manager. `GOOGLE_CLIENT_ID` can also be a secret. Never put these values in Netlify or `config.js`; the public Cloud Run URL is the only frontend configuration.

The Cloud Run service account needs Secret Manager Secret Accessor. If the app creates the token secret or adds versions, grant a least-privilege custom role containing `secretmanager.secrets.get`, `secretmanager.secrets.create` (only if creation is desired), and `secretmanager.versions.add`. Do not grant Owner or Editor for this application.

Deploy from PowerShell:

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  calendar-json.googleapis.com

cd backend
gcloud run deploy speak-and-grow-api `
  --source . `
  --region asia-southeast1 `
  --allow-unauthenticated
```

Unauthenticated ingress is required for the public API, OAuth navigation, and WebSocket. Configure environment values only after deployment reveals the service URL, then deploy a new revision. Application origin checks are defense-in-depth, not authentication.

The included limiter is an in-memory, per-instance safety net for consultation creation, OAuth login, and WebSocket connection creation. Use Google Cloud Armor or a shared external rate-limit store for production scale; in-memory counters do not coordinate across Cloud Run instances.

Cloud Run WebSockets are limited by the service request timeout. Select a timeout suitable for short pronunciation attempts. The backend cancels receiver/transcriber tasks and closes connections after browser disconnect, stop, timeout, Mistral termination, or network failure. Production config derives `wss://` from the HTTPS backend URL; never use `ws://` in production.

## Google OAuth production setup

After Cloud Run deployment, add this exact authorized redirect URI to the Google OAuth Web client:

```text
https://YOUR_CLOUD_RUN_DOMAIN/auth/google/callback
```

Keep `http://localhost:8000/auth/google/callback` for development and set `GOOGLE_REDIRECT_URI` to the Cloud Run callback in production. Reauthorize using `https://YOUR_CLOUD_RUN_DOMAIN/auth/google/login`. The therapist/calendar owner must remain a test user while the consent screen is in Testing mode. Testing-mode refresh tokens may expire, requiring reauthorization. Both Calendar event and FreeBusy scopes remain required.

## Netlify

Git deployment:

1. Push this repository to GitHub and connect it in Netlify.
2. Set Base directory to `frontend`, Publish directory to `.`, and no build command.
3. Change or generate `config.js` with the deployed Cloud Run URL.

Manual deployment: set `frontend/config.js` to the Cloud Run URL, then drag the `frontend` folder into Netlify Deploys. `config.example.js` shows production syntax. The SPA fallback loads `index.html`; WebSockets are not proxied through Netlify.

## Verification

```powershell
python -m compileall backend/backend.py
pytest -q
```

For frontend JavaScript syntax, extract the inline script or open the site with browser developer tools; the test suite also checks URL configuration and absence of secret names/values. Manually verify all six tabs, consultation booking/polling/join window, guardian consent, notices, microphone cleanup, OAuth, Calendar invitations, and WSS against the deployed URLs. A deployment is complete only after both real URLs have been tested.
