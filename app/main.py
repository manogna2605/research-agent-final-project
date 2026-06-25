"""
FastAPI application — user auth, per-user API key management,
protected streaming endpoints.
"""
import json
import os
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import run_agent_stream
from .auth import create_token, get_current_user, get_user_keys, hash_password, verify_password
from .config import settings
from .database import get_db, init_db
from .ingestion import ingest_stream
from .ratelimit import RateLimiter, client_key

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Research Agent")

research_limiter = RateLimiter(limit=settings.RESEARCH_RATE_LIMIT_PER_MINUTE, window_seconds=60)
ingest_limiter   = RateLimiter(limit=settings.INGEST_RATE_LIMIT_PER_HOUR,     window_seconds=3600)


@app.on_event("startup")
def startup():
    init_db()


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── pydantic bodies ───────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    username: str
    email: str
    password: str

class LoginBody(BaseModel):
    email: str
    password: str

class ApiKeysBody(BaseModel):
    openai_key:      str = ""
    pinecone_key:    str = ""
    serpapi_key:     str = ""
    pinecone_index:  str = "langgraph-research-agent"
    pinecone_cloud:  str = "aws"
    pinecone_region: str = "us-east-1"


# ── auth routes ───────────────────────────────────────────────────────────────

@app.post("/api/auth/register")
def register(body: RegisterBody):
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if len(body.username.strip()) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters.")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (body.username.strip(), body.email.strip().lower(), hash_password(body.password)),
        )
        db.commit()
        user_id = db.execute(
            "SELECT id FROM users WHERE email = ?", (body.email.strip().lower(),)
        ).fetchone()["id"]
    except sqlite3.IntegrityError as exc:
        db.close()
        if "username" in str(exc):
            raise HTTPException(status_code=409, detail="Username already taken.")
        raise HTTPException(status_code=409, detail="Email already registered. Try logging in.")
    finally:
        db.close()
    return {"token": create_token(user_id), "username": body.username.strip()}


@app.post("/api/auth/login")
def login(body: LoginBody):
    db = get_db()
    row = db.execute(
        "SELECT * FROM users WHERE email = ?", (body.email.strip().lower(),)
    ).fetchone()
    db.close()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    return {"token": create_token(row["id"]), "username": row["username"]}


@app.get("/api/auth/me")
def me(user=Depends(get_current_user)):
    keys = get_user_keys(user["id"])
    return {
        "id":       user["id"],
        "username": user["username"],
        "email":    user["email"],
        "has_keys": bool(keys and keys.get("openai_key")),
        "keys_status": {
            "openai":   bool(keys.get("openai_key")),
            "pinecone": bool(keys.get("pinecone_key")),
            "serpapi":  bool(keys.get("serpapi_key")),
        } if keys else {"openai": False, "pinecone": False, "serpapi": False},
    }


@app.post("/api/auth/keys")
def save_keys(body: ApiKeysBody, user=Depends(get_current_user)):
    db = get_db()
    db.execute("""
        INSERT INTO api_keys
            (user_id, openai_key, pinecone_key, serpapi_key,
             pinecone_index, pinecone_cloud, pinecone_region, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            openai_key      = excluded.openai_key,
            pinecone_key    = excluded.pinecone_key,
            serpapi_key     = excluded.serpapi_key,
            pinecone_index  = excluded.pinecone_index,
            pinecone_cloud  = excluded.pinecone_cloud,
            pinecone_region = excluded.pinecone_region,
            updated_at      = excluded.updated_at
    """, (user["id"], body.openai_key, body.pinecone_key, body.serpapi_key,
          body.pinecone_index, body.pinecone_cloud, body.pinecone_region))
    db.commit()
    db.close()
    return {"ok": True}


@app.delete("/api/auth/keys")
def reset_keys(user=Depends(get_current_user)):
    db = get_db()
    db.execute("DELETE FROM api_keys WHERE user_id = ?", (user["id"],))
    db.commit()
    db.close()
    return {"ok": True}


# ── protected streaming endpoints ─────────────────────────────────────────────

@app.get("/api/research/stream")
def research_stream(request: Request, query: str = Query(..., min_length=1), user=Depends(get_current_user)):
    research_limiter.check(client_key(request))
    user_keys = get_user_keys(user["id"])
    if not user_keys or not user_keys.get("openai_key"):
        return StreamingResponse(
            iter([sse({"type": "error", "message": "API keys not configured. Go to Settings (/setup) to add your keys."})]),
            media_type="text/event-stream",
        )
    def gen():
        try:
            for event in run_agent_stream(query, user_keys):
                yield sse(event)
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/ingest/stream")
def ingest_stream_endpoint(
    request: Request,
    query: str = Query(..., min_length=1),
    max_results: int = Query(20, ge=1, le=50),
    user=Depends(get_current_user),
):
    ingest_limiter.check(client_key(request))
    user_keys = get_user_keys(user["id"])
    if not user_keys or not user_keys.get("openai_key"):
        return StreamingResponse(
            iter([sse({"type": "error", "message": "API keys not configured. Go to Settings (/setup) to add your keys."})]),
            media_type="text/event-stream",
        )
    def gen():
        try:
            for event in ingest_stream(query, max_results, user_keys):
                yield sse(event)
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/kb/stats")
def kb_stats(user=Depends(get_current_user)):
    user_keys = get_user_keys(user["id"])
    if not user_keys or not user_keys.get("pinecone_key"):
        return {"error": "Pinecone key not configured"}
    try:
        from .tools import get_pinecone_index
        index = get_pinecone_index(
            user_keys["pinecone_key"],
            user_keys.get("pinecone_index", "langgraph-research-agent"),
            user_keys.get("pinecone_cloud",  "aws"),
            user_keys.get("pinecone_region", "us-east-1"),
        )
        stats = index.describe_index_stats()
        return {"total_vector_count": stats.get("total_vector_count", 0)}
    except Exception as exc:
        return {"error": str(exc)}


# ── static pages ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/auth")
def auth_page():
    return FileResponse(str(STATIC_DIR / "auth.html"))

@app.get("/setup")
def setup_page():
    return FileResponse(str(STATIC_DIR / "setup.html"))

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
