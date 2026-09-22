# Computer Store — Operations Console (Angular UI)

Angular 20 single-page console for the FastAPI Computer Store API. Standalone
components, signals for state, no UI framework — the styling is a small token
system in `src/styles.scss` with full light/dark parity.

## Pages

| Route | Who it is for |
| --- | --- |
| `/` | Staff operations console — the whole order book |
| `/customer` | Customer support portal — sign in with a customer ID, then chat |

`/customer` takes an identifier of the form `CUS-028` (case-insensitive) and
keeps it in `sessionStorage`. It is **identity, not authentication**: there is no
password and the ID is not checked against any account. The chat posts to the
same streaming endpoint the console uses, and hides the internal diagnostics
(the stub tag and `X-Request-ID`) that staff see.

Because the router owns `/customer`, `app/main.py` serves `index.html` for
unknown paths so a direct visit or refresh works, while API routes keep priority.

## What the console shows

| Panel | API it uses |
| --- | --- |
| KPI cards (orders, booked revenue, open work, average order value) | derived from `GET /orders` |
| Status distribution bar + interactive legend (click to filter) | derived from `GET /orders` |
| Orders table: server-side status/limit, client-side search and column sort | `GET /orders?status=&limit=` |
| New order form (validation mirrors the Pydantic model) | `GET /products`, `POST /orders` |
| Support agent chat, streamed, showing the `X-Request-ID` of each reply | `POST /agent/chat/stream` (SSE), falling back to `POST /agent` on 404 |
| Header health pill | `GET /health` |

## Develop

The API must be running on port 8000:

```bash
uv run uvicorn app.main:app --reload    # from the repository root
```

Then, in this directory:

```bash
npm install
npm start            # http://localhost:4200, API calls proxied by proxy.conf.json
```

`proxy.conf.json` forwards `/orders`, `/products`, `/agent`, `/health`, `/docs`
and `/openapi.json` to `127.0.0.1:8000`, so the app uses relative URLs and needs
no CORS configuration.

## Streaming

The chat reads `POST /agent/chat/stream` as Server-Sent Events. `HttpClient`
buffers response bodies, so `ApiService.streamAgent` uses `fetch` with a
`ReadableStream` reader and pushes each frame back inside the Angular zone.
Frames are parsed by `parseSseFrame` (exported and unit-tested); an `event: done`
frame ends the turn, `event: error` surfaces the message. Text accumulates in a
single bubble with a blinking caret while it streams.

## Build

```bash
npm run build
```

The bundle is written to `../app/static`, which `app/main.py` mounts at `/`
(after every API route, so the API keeps priority). Once built, the console is
served by FastAPI itself at http://localhost:8000/.

## Test

```bash
npm test -- --watch=false --browsers=ChromeHeadless
```

## Layout

```
src/app/
  core/        models, HTTP client, signal store, theme, toasts, customer session
  shared/      inline SVG icon component
  components/  header, KPI cards, status distribution, orders table,
               order form, agent chat, toast host
  pages/       operations console (/), customer portal (/customer)
```
