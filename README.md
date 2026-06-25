# Research Agent

A self-hosted research agent with a terminal-styled web UI. Ask it a
question and it routes itself through a knowledge base (RAG over papers you
ingest from ArXiv), a live ArXiv abstract lookup, and a Google web search,
then compiles everything into a structured report — all visible live as it
happens, like watching a terminal session.

This is a website wrapper around the original LangGraph + GPT-4o research
agent notebook: same oracle/tool/graph logic, now served by FastAPI with a
streaming UI instead of notebook cells.

## What it does

- **Research tab** — type a question, watch the oracle (GPT-4o) decide which
  tool to call next (`rag_search`, `rag_search_filter`, `fetch_arxiv`,
  `web_search`), see each tool's output as it streams in, and get a final
  structured report (introduction, research steps, main body, conclusion,
  sources).
- **Admin / knowledge base tab** — pull papers straight from the ArXiv API
  by search query, download + chunk the PDFs, embed them, and upsert them
  into a Pinecone index, with live progress.

## Requirements

- Python 3.10+
- An [OpenAI API key](https://platform.openai.com/api-keys) (required — powers the oracle and embeddings)
- A [Pinecone API key](https://app.pinecone.io) (required — stores the knowledge base)
- A [SerpAPI key](https://serpapi.com) (optional — without it, `web_search` is disabled but everything else still works)

These are paid APIs with free tiers. You're billed directly by OpenAI / Pinecone / SerpAPI for your own usage — this app doesn't add any markup or proxy.

## Setup

```bash
# 1. create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. configure your keys
cp .env.example .env
# now open .env and paste in your keys

# 4. run it
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000**.

If a key is missing, you'll see a banner at the top of the page telling you
which one — the server still boots so you can fix it without restarting.

## First run: populate the knowledge base

`rag_search` has nothing to search until you've ingested some papers. Go to
the **admin / knowledge-base** tab and run a query, for example:

- `cat:cs.AI` with max results `20` — recent AI papers
- `dynamic backtracking LLM agents` — a topical search
- `cat:cs.CL` — recent NLP papers

This fetches matching papers from ArXiv, downloads each PDF, splits it into
~512-character chunks, embeds them with `text-embedding-3-small`, and
upserts them into your Pinecone index. You'll see live progress per paper.
Re-run it any time to grow the knowledge base further.

**Query syntax:** ArXiv's search API requires a field prefix — `all:`,
`ti:` (title), `au:` (author), `abs:` (abstract), or `cat:` (category) — a
bare word on its own isn't valid query syntax there. If you type a plain
phrase like `agentic reasoning` with no prefix, the app automatically wraps
it as `all:agentic reasoning` for you (searches title, abstract, authors,
and comments at once) — you'll see the normalized query echoed in the log
so you can confirm what was actually sent. For a narrower search, use a
prefix yourself, e.g. `cat:cs.AI`, `au:hinton`, or
`ti:transformer AND au:vaswani`. Full syntax: the
[ArXiv API User's Manual](https://info.arxiv.org/help/api/user-manual.html).

`--max` is capped at 50 — each result is downloaded, chunked, and embedded,
so larger batches get slow and expensive fast. If you need more than that,
just run the ingest a few times with different queries.

## Project structure

```
app/
  main.py        FastAPI app, routes, SSE streaming
  agent.py        the oracle + LangGraph state machine
  tools.py        rag_search, rag_search_filter, fetch_arxiv, web_search, final_answer
  ingestion.py    ArXiv fetch -> PDF download -> chunk -> embed -> upsert
  config.py       env vars + lazy client setup (OpenAI, Pinecone)
  ratelimit.py    in-memory per-IP rate limiter
  static/         the terminal-themed frontend (HTML/CSS/JS, no build step)
requirements.txt
.env.example
```

There's no React/Node build step — the frontend is plain HTML/CSS/JS served
directly by FastAPI, so the whole thing runs as a single process.

## Deploying it for real

This is a normal FastAPI app, so it runs anywhere that runs Python:

- **Render / Railway / Fly.io** — point the start command at
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT` and set your env vars
  in the dashboard instead of a `.env` file.
- **Docker** — wrap the same command in a small `Dockerfile` (not included
  here, but it's just `FROM python:3.11-slim`, `pip install -r
  requirements.txt`, `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]`).

## Rate limiting

Every research query spends your OpenAI (and possibly SerpAPI) credits, and
ingestion downloads + embeds whole PDFs, so both are rate-limited per client
IP out of the box:

- `RESEARCH_RATE_LIMIT_PER_MINUTE` (default `5`) — for `/api/research/stream`
- `INGEST_RATE_LIMIT_PER_HOUR` (default `3`) — for `/api/ingest/stream`

Set either to `0` in `.env` to disable it. Going over the limit returns an
HTTP 429 with a `Retry-After` header, and the UI shows a message telling the
visitor to wait.

This limiter (`app/ratelimit.py`) is in-memory and per-process — perfect for
the common case of one app running on one machine. It is **not** enough if
you run multiple uvicorn workers or multiple instances behind a load
balancer, since each process counts independently (so your real limit
becomes `limit × process count`). If you scale out that way, swap it for a
shared store — Redis plus the [`slowapi`](https://github.com/laurentS/slowapi)
or [`limits`](https://limits.readthedocs.io) library is the standard choice,
keyed the same way (`client_key()` in `app/ratelimit.py` already extracts
the right IP, including behind a reverse proxy that sets `X-Forwarded-For`).

### Other things worth adding before exposing this publicly

There's still no login or access control built in — rate limiting controls
the *damage*, it doesn't restrict *who* can use it. At minimum, also consider:

- A reverse proxy with basic auth (Caddy, nginx, or your host's built-in option)
- A simple shared-secret check in `app/main.py` before the streaming routes
- Your host's environment-level access control (e.g. Render's IP allowlists)

## Customizing

- **Models** — change `CHAT_MODEL` / `EMBED_MODEL` in `.env`.
- **Chunk size / overlap** — edit `chunk_pdf_from_url()` in `app/ingestion.py`.
- **Oracle's tool-use rules** — edit `SYSTEM_PROMPT` in `app/agent.py`.
- **Look and feel** — everything is token-driven in `app/static/styles.css`
  (see the `:root` block at the top) if you want a different palette.

## How it relates to the original notebook

`app/agent.py`, `app/tools.py`, and `app/ingestion.py` are direct ports of
the notebook's oracle, tool definitions, and ArXiv/Pinecone ingestion
pipeline. The only structural addition is that `run_agent_stream()` walks
the compiled LangGraph's `.stream()` output node-by-node so the API can push
each decision and tool result to the browser over Server-Sent Events as it
happens, instead of waiting for `.invoke()` to return everything at once.
