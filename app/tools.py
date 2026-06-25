"""
Tool factory — make_tools(user_keys) returns a fresh set of LangChain tools
bound to that user's API credentials via closure. Called once per agent run.
"""
import re
import time

import requests
from langchain_core.tools import StructuredTool


ABSTRACT_PATTERN = re.compile(
    r'<blockquote class="abstract mathjax">\s*<span class="descriptor">Abstract:</span>\s*(.*?)\s*</blockquote>',
    re.DOTALL,
)


# ── shared helpers (used by ingestion.py too) ─────────────────────────────────

def embed_texts(texts: list, openai_key: str, model: str = "text-embedding-3-small") -> list:
    from openai import OpenAI
    client = OpenAI(api_key=openai_key)
    response = client.embeddings.create(model=model, input=texts)
    return [d.embedding for d in response.data]


def get_pinecone_index(pinecone_key: str, index_name: str, cloud: str = "aws", region: str = "us-east-1"):
    from pinecone import Pinecone, ServerlessSpec
    pc = Pinecone(api_key=pinecone_key)
    if index_name not in pc.list_indexes().names():
        pc.create_index(
            index_name,
            dimension=1536,
            metric="cosine",
            spec=ServerlessSpec(cloud=cloud, region=region),
        )
        while not pc.describe_index(index_name).status["ready"]:
            time.sleep(1)
    return pc.Index(index_name)


def format_rag_contexts(matches: list) -> str:
    if not matches:
        return "No relevant context found in the knowledge base. Try ingesting some papers first."
    parts = []
    for match in matches:
        meta = match.get("metadata", {}) if isinstance(match, dict) else match.metadata
        parts.append(
            f"Title: {meta.get('title', '')}\n"
            f"Chunk: {meta.get('chunk', '')}\n"
            f"ArXiv ID: {meta.get('arxiv_id', '')}\n"
        )
    return "\n---\n".join(parts)


# ── tool factory ──────────────────────────────────────────────────────────────

def make_tools(user_keys: dict) -> list:
    """
    Returns a fresh list of LangChain tools bound to the given user's API keys.
    Call once per agent run.
    """
    openai_key  = user_keys.get("openai_key", "")
    pinecone_key = user_keys.get("pinecone_key", "")
    serpapi_key  = user_keys.get("serpapi_key", "")
    index_name   = user_keys.get("pinecone_index", "langgraph-research-agent")
    cloud        = user_keys.get("pinecone_cloud", "aws")
    region       = user_keys.get("pinecone_region", "us-east-1")

    def _fetch_arxiv(arxiv_id: str) -> str:
        """Fetches the abstract of an ArXiv paper. Args: arxiv_id: e.g. '1706.03762'."""
        try:
            res = requests.get(f"https://arxiv.org/abs/{arxiv_id}", timeout=15)
        except requests.RequestException as exc:
            return f"Could not reach arXiv: {exc}"
        match = ABSTRACT_PATTERN.search(res.text)
        return match.group(1) if match else "Abstract not found."

    def _web_search(query: str) -> str:
        """Finds general knowledge using Google search. Args: query: the search string."""
        if not serpapi_key:
            return "Web search unavailable: SerpAPI key not configured."
        from serpapi import GoogleSearch
        search = GoogleSearch({"engine": "google", "api_key": serpapi_key, "q": query, "num": 5})
        results = search.get_dict().get("organic_results", [])
        if not results:
            return "No results found."
        return "\n---\n".join(
            "\n".join([r.get("title", ""), r.get("snippet", ""), r.get("link", "")]) for r in results
        )

    def _rag_search(query: str) -> str:
        """Searches the RAG knowledge base with a natural language query across all ingested papers. Args: query."""
        if not pinecone_key or not openai_key:
            return "RAG search unavailable: OpenAI or Pinecone key not configured."
        index = get_pinecone_index(pinecone_key, index_name, cloud, region)
        vector = embed_texts([query], openai_key)[0]
        result = index.query(vector=vector, top_k=5, include_metadata=True)
        return format_rag_contexts(result.get("matches", []))

    def _rag_search_filter(query: str, arxiv_id: str) -> str:
        """Searches the RAG knowledge base filtered to one specific paper. Args: query, arxiv_id."""
        if not pinecone_key or not openai_key:
            return "RAG search unavailable: OpenAI or Pinecone key not configured."
        index = get_pinecone_index(pinecone_key, index_name, cloud, region)
        vector = embed_texts([query], openai_key)[0]
        result = index.query(vector=vector, top_k=6, include_metadata=True, filter={"arxiv_id": arxiv_id})
        return format_rag_contexts(result.get("matches", []))

    def _final_answer(introduction: str, research_steps, main_body: str, conclusion: str, sources) -> str:
        """Compiles the final research report. Call this once enough information has been gathered."""
        return "Report compiled."

    return [
        StructuredTool.from_function(_fetch_arxiv,       name="fetch_arxiv",       description="Fetches the abstract of an ArXiv paper given its ID (e.g. '1706.03762')."),
        StructuredTool.from_function(_web_search,        name="web_search",        description="Finds general knowledge information using a Google search."),
        StructuredTool.from_function(_rag_search,        name="rag_search",        description="Finds specialist AI research information from the knowledge base using a natural language query."),
        StructuredTool.from_function(_rag_search_filter, name="rag_search_filter", description="Finds information from the knowledge base filtered to a single specific ArXiv paper."),
        StructuredTool.from_function(_final_answer,      name="final_answer",      description="Returns a research report. Use this once enough information has been gathered to answer the user's question."),
    ]
