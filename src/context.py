"""Knowledge-base retrieval over the local `context/` folder + the resume.

This is the "Claude project": a folder of markdown/text materials (background,
stories, preferences, ...) that the agent searches on demand to craft answers
when the profile and history don't already have one. Retrieval is a lightweight
keyword/overlap scorer (stdlib only) — Claude does the actual writing.
"""

import re
from difflib import SequenceMatcher

from . import config


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _split(source: str, text: str, chunks: list[dict]) -> None:
    """Split `text` on blank lines into non-trivial chunks tagged with source."""
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if len(block) >= 25:
            chunks.append({"source": source, "text": block})


def _load_chunks() -> list[dict]:
    """Load every context file (.md/.txt/.pdf) and the resume, as chunks."""
    chunks: list[dict] = []

    if config.CONTEXT_DIR.exists():
        for path in sorted(config.CONTEXT_DIR.iterdir()):
            suffix = path.suffix.lower()
            if suffix in (".md", ".txt"):
                try:
                    _split(path.name, path.read_text(encoding="utf-8"), chunks)
                except (OSError, UnicodeDecodeError):
                    continue
            elif suffix == ".pdf":
                _split(path.name, config.extract_pdf_text(path), chunks)

    # the resume (text file, or extracted from resume.pdf)
    try:
        _split("resume", config.load_resume(), chunks)
    except FileNotFoundError:
        pass

    return chunks


def search_context(query: str, top_k: int = 5) -> list[dict]:
    """Return the top-scoring context snippets for a query."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return []
    results = []
    for chunk in _load_chunks():
        c_tokens = _tokens(chunk["text"])
        if not c_tokens:
            continue
        overlap = len(q_tokens & c_tokens) / len(q_tokens)
        ratio = SequenceMatcher(None, _normalize(query), _normalize(chunk["text"])).ratio()
        score = round(0.7 * overlap + 0.3 * ratio, 3)
        if score > 0:
            results.append({"source": chunk["source"], "score": score,
                            "text": chunk["text"]})
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
