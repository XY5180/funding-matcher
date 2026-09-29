"""Optional OpenAI-assisted research-theme generation."""

import hashlib
import json

from openai import OpenAI

from .clients import utcnow
from .db import upsert


THEME_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "methods": {"type": "array", "items": {"type": "string"}},
                    "diseases": {"type": "array", "items": {"type": "string"}},
                    "populations": {"type": "array", "items": {"type": "string"}},
                    "data_types": {"type": "array", "items": {"type": "string"}},
                    "supporting_paper_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
                },
                "required": [
                    "name", "summary", "keywords", "methods", "diseases",
                    "populations", "data_types", "supporting_paper_ids",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}


def _clean_list(values, limit=24):
    cleaned = []
    for value in values or []:
        text = " ".join(str(value).split()).strip()
        if text and text.casefold() not in {item.casefold() for item in cleaned}:
            cleaned.append(text)
    return cleaned[:limit]


def _clean_keywords(values, limit=24):
    generic_singletons = {
        "analysis", "data", "health", "identifies", "learning", "model",
        "models", "prediction", "research", "results", "study", "use", "using",
    }
    return [value for value in _clean_list(values, limit * 2)
            if not (len(value.split()) == 1 and value.casefold() in generic_singletons)][:limit]


def _paper_relevance(theme, paper):
    """Score an imported paper as evidence for one generated theme."""
    theme_text = " ".join([
        str(theme.get("name") or ""), str(theme.get("summary") or ""),
        " ".join(theme.get("keywords") or []),
    ]).casefold()
    paper_text = f"{paper['title']} {paper['abstract']}".casefold()
    theme_terms = {term for term in theme_text.replace("-", " ").split()
                   if len(term) > 3}
    paper_terms = set(paper_text.replace("-", " ").split())
    phrase_bonus = sum(
        4 for phrase in theme.get("keywords") or []
        if len(str(phrase).split()) > 1 and str(phrase).casefold() in paper_text
    )
    return len(theme_terms & paper_terms) + phrase_bonus


def build_profiles_with_openai(conn, researcher_id, api_key,
                               model="gpt-4o-mini", seed_keywords=None,
                               max_themes=3):
    """Generate and store three validated themes from imported publications."""
    if max_themes != 3:
        raise ValueError("The OpenAI theme generator currently requires three themes")
    researcher = conn.execute(
        "SELECT * FROM researchers WHERE researcher_id=?", (researcher_id,)
    ).fetchone()
    if not researcher:
        raise ValueError(f"Unknown researcher: {researcher_id}")
    rows = conn.execute("""
        SELECT o.output_id, o.title, o.abstract, o.publication_date, o.journal
        FROM research_outputs o
        JOIN researcher_outputs ro ON o.output_id=ro.output_id
        WHERE ro.researcher_id=?
        ORDER BY o.publication_date DESC
    """, (researcher_id,)).fetchall()
    if len(rows) < 3:
        raise ValueError("At least three imported publications are required")

    papers = [{
        "paper_id": row["output_id"],
        "title": row["title"] or "",
        "abstract": (row["abstract"] or "")[:1800],
        "publication_date": row["publication_date"] or "",
        "journal": row["journal"] or "",
    } for row in rows]
    researcher_payload = {
        "name": researcher["name"],
        "title": researcher["title"],
        "career_stage": researcher["career_stage"],
        "country": researcher["country"],
    }
    guidance = (
        "Generate three distinct themes directly from the publication evidence."
        if not seed_keywords else
        "Generate exactly one theme for each keyword group, preserving the group order: "
        + json.dumps(seed_keywords, ensure_ascii=False)
    )
    prompt = f"""
Build a precise scientific funding profile from a researcher's publications.

RESEARCHER:
{json.dumps(researcher_payload, ensure_ascii=False)}

PUBLICATIONS:
{json.dumps(papers, ensure_ascii=False)}

INSTRUCTIONS:
- {guidance}
- Return exactly three themes.
- Make the themes scientifically specific and useful for funding searches.
- Minimize conceptual and keyword overlap among themes.
- Keywords should normally be specific 2-6 word scientific phrases, such as
  "transcriptome-wide association studies" or "electronic health records".
- Avoid isolated generic tokens such as "use", "study", "analysis", "health",
  "learning", "model", or "prediction".
- Distinguish research topics from methods, diseases, populations, and data types.
- Use only information supported by the supplied publications and keyword groups.
- Every supporting_paper_id must exactly match a supplied paper_id.
- Assign each paper to at most one theme; do not repeat supporting paper IDs.
- Include the strongest supporting papers for each theme.
- Summaries should be concise complete sentences, not keyword fragments.
"""
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": (
                "You are a conservative scientific research analyst. "
                "Return only evidence-grounded structured data."
            )},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "research_themes",
                "strict": True,
                "schema": THEME_SCHEMA,
            },
        },
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)
    themes = parsed.get("themes") or []
    if len(themes) != 3:
        raise ValueError(f"OpenAI returned {len(themes)} themes instead of 3")

    allowed_ids = {paper["paper_id"] for paper in papers}
    paper_by_id = {paper["paper_id"]: paper for paper in papers}
    used_ids = set()
    validated = []
    for index, theme in enumerate(themes, start=1):
        name = " ".join(str(theme.get("name") or "").split()).strip()
        summary = " ".join(str(theme.get("summary") or "").split()).strip()
        if not name or not summary:
            raise ValueError(f"OpenAI theme {index} is missing a name or summary")
        evidence = []
        for paper_id in theme.get("supporting_paper_ids") or []:
            if paper_id in allowed_ids and paper_id not in used_ids:
                evidence.append(paper_id)
                used_ids.add(paper_id)
        if not evidence:
            unused = [paper for paper in papers if paper["paper_id"] not in used_ids]
            if unused:
                best = max(unused, key=lambda paper: _paper_relevance(theme, paper))
                evidence = [best["paper_id"]]
                used_ids.add(best["paper_id"])
        if not evidence:
            raise ValueError(f"OpenAI theme {index} has no available supporting paper")
        # Put the strongest valid evidence first even when the model supplied IDs.
        evidence.sort(
            key=lambda paper_id: _paper_relevance(theme, paper_by_id[paper_id]),
            reverse=True,
        )
        validated.append({
            "name": name,
            "summary": summary,
            "keywords": _clean_keywords(theme.get("keywords")),
            "methods": _clean_list(theme.get("methods"), 12),
            "diseases": _clean_list(theme.get("diseases"), 12),
            "populations": _clean_list(theme.get("populations"), 12),
            "data_types": _clean_list(theme.get("data_types"), 12),
            "evidence": evidence[:12],
        })

    conn.execute(
        "DELETE FROM research_themes WHERE researcher_id=? AND manually_verified=0",
        (researcher_id,),
    )
    for index, theme in enumerate(validated, start=1):
        digest = hashlib.sha1(
            f"{researcher_id}:openai:{index}:{theme['name']}".encode()
        ).hexdigest()[:10]
        upsert(conn, "research_themes", {
            "theme_id": f"openai-{index}-{digest}",
            "researcher_id": researcher_id,
            "theme_name": theme["name"],
            "summary": theme["summary"],
            "keywords": theme["keywords"],
            "methods": theme["methods"],
            "diseases": theme["diseases"],
            "populations": theme["populations"],
            "data_types": theme["data_types"],
            "evidence_output_ids": theme["evidence"],
            "confidence": min(0.95, 0.65 + 0.05 * len(theme["evidence"])),
            "manually_verified": 0,
            "generated_at": utcnow(),
        }, ["theme_id"])
    conn.commit()
    return len(validated)
