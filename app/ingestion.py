"""
Knowledge-base ingestion pipeline.
Accepts user_keys for all API calls.
"""
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET

import requests

from .tools import embed_texts, get_pinecone_index

ARXIV_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
_PREFIXED = re.compile(r"\b(ti|au|abs|cat|all|id|jr|co|cc|num)\s*:", re.IGNORECASE)


def normalize_query(raw: str) -> str:
    raw = raw.strip()
    return raw if _PREFIXED.search(raw) else f"all:{raw}"


def extract_from_arxiv(search_query: str, max_results: int = 20) -> list:
    query = normalize_query(search_query)
    response = requests.get(
        ARXIV_API_URL,
        params={"search_query": query, "max_results": max_results},
        headers={"User-Agent": "research-agent-website/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    papers = []
    for entry in root.findall(f"{ARXIV_NS}entry"):
        title = entry.find(f"{ARXIV_NS}title").text.strip()
        paper_url = entry.find(f"{ARXIV_NS}id").text
        arxiv_id = paper_url.split("/")[-1]
        pdf_link = next(
            (link.attrib["href"] for link in entry.findall(f"{ARXIV_NS}link")
             if link.attrib.get("title") == "pdf"),
            None,
        )
        papers.append({"title": title, "url": paper_url, "arxiv_id": arxiv_id, "pdf_link": pdf_link})
    return papers


def chunk_pdf_from_url(pdf_link: str, chunk_size: int = 512) -> list:
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    response = requests.get(pdf_link, headers={"User-Agent": "research-agent-website/1.0"}, timeout=60)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(response.content)
        tmp_path = f.name
    try:
        data = PyPDFLoader(tmp_path).load()
        chunks = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=64).split_documents(data)
        return [c.page_content for c in chunks]
    finally:
        os.remove(tmp_path)


def ingest_stream(search_query: str, max_results: int = 20, user_keys: dict = None, batch_size: int = 64):
    user_keys    = user_keys or {}
    openai_key   = user_keys.get("openai_key", "")
    pinecone_key = user_keys.get("pinecone_key", "")
    index_name   = user_keys.get("pinecone_index", "langgraph-research-agent")
    cloud        = user_keys.get("pinecone_cloud", "aws")
    region       = user_keys.get("pinecone_region", "us-east-1")

    normalized = normalize_query(search_query)
    yield {"type": "log", "message": f"querying arxiv api: '{normalized}' (max {max_results} results)"}

    try:
        papers = extract_from_arxiv(search_query, max_results)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 403:
            yield {"type": "error", "message": "arXiv returned 403 — your IP may be rate-limited. Wait a bit and try again."}
        else:
            yield {"type": "error", "message": f"arXiv query failed: {exc}"}
        return
    except Exception as exc:
        yield {"type": "error", "message": f"arXiv query failed: {exc}"}
        return

    yield {"type": "log", "message": f"found {len(papers)} paper(s)"}
    if not papers:
        yield {"type": "log", "message": "no matches — try cat:cs.AI, au:hinton, or a specific phrase"}
        return

    if not pinecone_key or not openai_key:
        yield {"type": "error", "message": "OpenAI and Pinecone keys required. Go to Settings to configure them."}
        return

    try:
        index = get_pinecone_index(pinecone_key, index_name, cloud, region)
    except Exception as exc:
        yield {"type": "error", "message": f"Pinecone connection failed: {exc}"}
        return

    total_chunks = 0
    for i, paper in enumerate(papers):
        if i > 0:
            time.sleep(1)
        if not paper.get("pdf_link"):
            yield {"type": "log", "message": f"skip {paper['arxiv_id']}: no pdf link"}
            continue
        try:
            yield {"type": "log", "message": f"downloading + chunking: {paper['title'][:70]}"}
            chunks = chunk_pdf_from_url(paper["pdf_link"])
        except Exception as exc:
            yield {"type": "log", "message": f"skip {paper['arxiv_id']}: {exc}"}
            continue
        if not chunks:
            continue
        for j in range(0, len(chunks), batch_size):
            batch    = chunks[j: j + batch_size]
            ids      = [f"{paper['arxiv_id']}-{j + k}" for k in range(len(batch))]
            metadata = [{"arxiv_id": paper["arxiv_id"], "title": paper["title"], "chunk": c} for c in batch]
            try:
                embeds = embed_texts(batch, openai_key)
                index.upsert(vectors=zip(ids, embeds, metadata))
            except Exception as exc:
                yield {"type": "error", "message": f"embed/upsert failed for {paper['arxiv_id']}: {exc}"}
                continue
            total_chunks += len(batch)
            yield {"type": "progress", "paper": paper["title"], "arxiv_id": paper["arxiv_id"], "chunks_indexed": total_chunks}

    try:
        stats = index.describe_index_stats()
        total_vector_count = stats.get("total_vector_count", 0)
    except Exception:
        total_vector_count = None

    yield {"type": "done", "total_papers": len(papers), "total_chunks": total_chunks,
           "index_stats": {"total_vector_count": total_vector_count}}
