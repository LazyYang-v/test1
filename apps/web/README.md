# AgentForge web

This React and TypeScript demo is served by the `web` Compose service at `http://localhost:3000`. Start it with the API and worker using `docker compose up --build`. The Vite development server is available with `npm ci && npm run dev` from this directory; it proxies `/api` and `/health` to `http://localhost:8000`. It uses the system font stack and makes no third-party runtime requests.

Sign in with an active AgentForge account. Create an analysis with a published agent key (the default is `sales-report`), one of your granted regions, an ISO calendar date range, an IANA time zone, and the previous-period comparison. The date range follows `[start, end_exclusive)`. The UI sends a new `Idempotency-Key` with each create request and opens the resulting task details. The task list shows the latest 50 tasks available to the signed-in user.

## Authenticated event stream

The API authenticates requests with a bearer JWT in the `Authorization` header. Browser `EventSource` does not support custom request headers, so this UI consumes the event endpoint with authenticated `fetch` and a small SSE parser. It keeps the bearer token out of URLs, sends `Last-Event-ID` on reconnect, and deduplicates the at-least-once event delivery by the task-local `task_event_seq`. The API and static UI share one origin through the web container's `/api` reverse proxy, so the browser does not need a cross-origin credential workaround.

The access token is held in `sessionStorage` for the current browser tab and cleared on sign out. Reports present the API's complete structured report, including metrics and changes, evidence-linked findings, explicitly unverified hypotheses, recommendations, limitations, report version, and evidence references. API errors include their code and request ID when available, with guidance for authorization, conflict, rate-limit, and retryable failures.
