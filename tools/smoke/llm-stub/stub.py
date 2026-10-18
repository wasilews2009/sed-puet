"""ЛИШЕ ДЛЯ СМОУК-ТЕСТУ ЗБІРКИ: OpenAI-сумісна заглушка /v1/chat/completions і /v1/embeddings.
У цьому середовищі ваги LLM і bge-m3 недоступні (Hugging Face / реєстр Ollama закриті). Відповіді детерміновані
евристики з тексту документа, щоб пройти потік; це не модель і в реліз не входить."""
import hashlib, json, math, re
from fastapi import FastAPI, Request

app = FastAPI()
DIM = 1024

def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w[:6].encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 20) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]

@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "stub", "object": "model"}]}

@app.post("/v1/embeddings")
async def embeddings(req: Request):
    b = await req.json()
    inp = b["input"] if isinstance(b["input"], list) else [b["input"]]
    return {"object": "list", "model": b.get("model", "stub"),
            "data": [{"object": "embedding", "index": i, "embedding": embed(t)} for i, t in enumerate(inp)],
            "usage": {"prompt_tokens": 1, "total_tokens": 1}}

def line_with(doc: str, pat: str):
    m = re.search(pat, doc, re.I | re.M)
    return m

def extraction(doc: str) -> dict:
    low = doc.lower()
    kind = ("memo" if "службова записка" in low else "order_main" if "наказ" in low else
            "application" if "заява" in low else "directive" if "розпорядження" in low else "other")
    d = line_with(doc, r"\b(\d{2}\.\d{2}\.\d{4})\b")
    n = line_with(doc, r"№\s*([\w\-/]+)")
    t = line_with(doc, r"^\s*(Про\s.+)$")
    a = line_with(doc, r"^\s*((?:Завідувач|Директор|Ректор|Проректор|Начальник)[^\n]{0,80})$")
    ev = lambda m: m.group(0).strip()[:300] if m else None
    return {
        "document_type": {"value": kind, "evidence": kind != "other" and doc.strip().splitlines()[0][:300] or None, "confidence": 0.9},
        "document_date": {"value": d.group(1) if d else None, "evidence": ev(d), "confidence": 0.93 if d else 0.5},
        "registration_number": {"value": n.group(1) if n else None, "evidence": ev(n), "confidence": 0.7 if n else 0.9},
        "author": {"value": {"full_name": None, "position": a.group(1).strip()[:200], "org_unit": None, "evidence": ev(a)} if a else None,
                   "confidence": 0.6},
        "title": {"value": t.group(1).strip()[:300] if t else None, "evidence": ev(t), "confidence": 0.92 if t else 0.4},
        "summary": {"value": (t.group(1).strip() if t else doc.strip()[:200])[:280], "confidence": 0.7},
        "interested_parties": {"value": [], "confidence": 0.8},
    }

@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json()
    props = set(b.get("response_format", {}).get("json_schema", {}).get("schema", {}).get("properties", {}))
    user = "\n".join(m["content"] for m in b["messages"] if m["role"] == "user")
    if "document_type" in props:
        doc = user.split("<<<ДОКУМЕНТ>>>", 1)[-1].split("<<<КІНЕЦЬ ДОКУМЕНТА>>>", 1)[0]
        out = extraction(doc)
    elif "queries" in props:
        out = {"queries": []}
    elif "issues" in props:
        out = {"applicable_norm_ids": ["N1"] if "N1" in user else [], "issues": [], "no_issue_confidence": 0.9}
    elif "verdict" in props:
        out = {"verdict": "uncertain", "confidence": 0.5, "reason": "Заглушка смоук-тесту."}
    else:
        out = {}
    return {"id": "stub", "object": "chat.completion", "model": b.get("model", "stub"),
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
