"""Optional OpenAI-assisted research-theme generation."""

import hashlib
import json
import math
import statistics
import urllib.error
import urllib.request

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


def _post_openai(path, api_key, payload, timeout=90):
    """Call OpenAI directly so deployment does not depend on SDK versions."""
    request = urllib.request.Request(
        "https://api.openai.com/v1/" + path.lstrip("/"),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach OpenAI: {exc.reason}") from exc


def _structured_chat(api_key, model, messages, schema, name):
    request = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": schema},
        },
    }
    if model.startswith("gpt-6"):
        request["reasoning_effort"] = "medium"
    else:
        request["temperature"] = 0.1
    payload = _post_openai("chat/completions", api_key, request)
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)


def embed_texts(api_key, texts, model="text-embedding-3-large"):
    """Return normalized OpenAI embeddings for non-empty texts."""
    cleaned = [" ".join(str(text).split())[:12000] or "empty" for text in texts]
    payload = _post_openai("embeddings", api_key, {
        "model": model,
        "input": cleaned,
        "encoding_format": "float",
    })
    ordered = sorted(payload["data"], key=lambda item: item["index"])
    vectors = []
    for item in ordered:
        vector = item["embedding"]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        vectors.append([value / norm for value in vector])
    return vectors


RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity_id": {"type": "string"},
                    "relevance_score": {"type": "number"},
                    "alignment_label": {
                        "type": "string",
                        "enum": ["unrelated", "generic_overlap", "adjacent",
                                 "partial_match", "direct_match"],
                    },
                    "objective_match_score": {"type": "number"},
                    "disease_match_score": {"type": "number"},
                    "method_match_score": {"type": "number"},
                    "population_match_score": {"type": "number"},
                    "mechanism_fit_score": {"type": "number"},
                    "matched_dimensions": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "missing_dimensions": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "hard_mismatch": {"type": "boolean"},
                    "mismatch_reason": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": [
                    "opportunity_id", "relevance_score", "alignment_label",
                    "objective_match_score", "disease_match_score",
                    "method_match_score", "population_match_score",
                    "mechanism_fit_score", "hard_mismatch",
                    "matched_dimensions", "missing_dimensions",
                    "mismatch_reason", "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def _cosine_vector(left, right):
    return sum(a * b for a, b in zip(left, right))


def _centroid(vectors, indices):
    if not indices:
        return [0.0] * len(vectors[0])
    center = [sum(vectors[index][dim] for index in indices) / len(indices)
              for dim in range(len(vectors[0]))]
    norm = math.sqrt(sum(value * value for value in center)) or 1.0
    return [value / norm for value in center]


def _spherical_kmeans(vectors, k=3, initial_index=0, iterations=30):
    """Small deterministic spherical K-means implementation for <=50 papers."""
    if len(vectors) < k:
        raise ValueError(f"At least {k} papers are required for clustering")
    seeds = [initial_index % len(vectors)]
    while len(seeds) < k:
        candidates = [index for index in range(len(vectors)) if index not in seeds]
        seeds.append(min(
            candidates,
            key=lambda index: max(_cosine_vector(vectors[index], vectors[seed])
                                  for seed in seeds),
        ))
    centers = [vectors[index] for index in seeds]
    assignments = [-1] * len(vectors)
    for _ in range(iterations):
        updated = [max(range(k), key=lambda group: _cosine_vector(vector, centers[group]))
                   for vector in vectors]
        # Keep every theme populated. Move the least-confident paper from the
        # largest cluster when an empty cluster occurs.
        for group in range(k):
            if group not in updated:
                largest = max(range(k), key=lambda value: updated.count(value))
                members = [index for index, value in enumerate(updated) if value == largest]
                moved = min(members, key=lambda index: _cosine_vector(
                    vectors[index], centers[largest]))
                updated[moved] = group
        if updated == assignments:
            break
        assignments = updated
        centers = [_centroid(vectors, [index for index, value in enumerate(assignments)
                                      if value == group]) for group in range(k)]
    return assignments, centers


def _coassignment_stability(vectors, baseline, k=3):
    """Measure whether paper pairs remain together across different seeds."""
    if len(vectors) <= k:
        return 0.5
    agreements = []
    for initial_index in range(1, min(6, len(vectors))):
        alternative, _ = _spherical_kmeans(
            vectors, k=k, initial_index=initial_index)
        comparisons = []
        for left in range(len(vectors)):
            for right in range(left + 1, len(vectors)):
                comparisons.append(
                    (baseline[left] == baseline[right]) ==
                    (alternative[left] == alternative[right]))
        if comparisons:
            agreements.append(sum(comparisons) / len(comparisons))
    return statistics.mean(agreements) if agreements else 1.0


def cluster_papers(vectors, seed_vectors=None, k=3):
    """Cluster papers, reject weak outliers, and calculate coherence metrics."""
    if seed_vectors:
        if len(seed_vectors) != k:
            raise ValueError("Exactly three keyword seed vectors are required")
        assignments = [max(range(k), key=lambda group: _cosine_vector(
            vector, seed_vectors[group])) for vector in vectors]
        # Populate an empty guided theme with its closest paper.
        for group in range(k):
            if group not in assignments:
                candidate = max(range(len(vectors)), key=lambda index: _cosine_vector(
                    vectors[index], seed_vectors[group]))
                assignments[candidate] = group
        centers = [_centroid(vectors, [i for i, value in enumerate(assignments)
                                      if value == group]) for group in range(k)]
        margins = []
        for vector in vectors:
            scores = sorted((_cosine_vector(vector, seed) for seed in seed_vectors),
                            reverse=True)
            margins.append(max(0.0, scores[0] - scores[1]))
        stability = min(1.0, 0.5 + statistics.mean(margins))
    else:
        assignments, centers = _spherical_kmeans(vectors, k=k)
        stability = _coassignment_stability(vectors, assignments, k=k)

    clusters = []
    for group in range(k):
        members = [index for index, value in enumerate(assignments) if value == group]
        similarities = {index: _cosine_vector(vectors[index], centers[group])
                        for index in members}
        retained = list(members)
        if len(members) >= 3:
            threshold = max(0.30, statistics.median(similarities.values()) - 0.12)
            filtered = [index for index in members if similarities[index] >= threshold]
            if len(filtered) >= 2:
                retained = filtered
        center = _centroid(vectors, retained)
        similarities = {index: _cosine_vector(vectors[index], center)
                        for index in retained}
        pairwise = [_cosine_vector(vectors[left], vectors[right])
                    for position, left in enumerate(retained)
                    for right in retained[position + 1:]]
        coherence = statistics.mean(pairwise) if pairwise else 0.0
        clusters.append({
            "indices": sorted(retained, key=lambda index: similarities[index],
                              reverse=True),
            "outlier_indices": [index for index in members if index not in retained],
            "center": center,
            "similarities": similarities,
            "coherence": max(0.0, min(1.0, coherence)),
            "stability": max(0.0, min(1.0, stability)),
        })
    for group, cluster in enumerate(clusters):
        other_similarities = [_cosine_vector(cluster["center"], other["center"])
                              for index, other in enumerate(clusters) if index != group]
        cluster["separation"] = max(
            0.0, min(1.0, 1.0 - max(other_similarities, default=0.0)))
    return clusters


CLUSTER_THEME_ITEM = {
    "type": "object",
    "properties": {
        "cluster_id": {"type": "integer"},
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "core_question": {"type": "string"},
        "scientific_rationale": {"type": "string"},
        "next_research_direction": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "methods": {"type": "array", "items": {"type": "string"}},
        "diseases": {"type": "array", "items": {"type": "string"}},
        "populations": {"type": "array", "items": {"type": "string"}},
        "data_types": {"type": "array", "items": {"type": "string"}},
        "supporting_paper_ids": {"type": "array", "items": {"type": "string"}},
        "quality_score": {"type": "number"},
        "quality_notes": {"type": "string"},
    },
    "required": [
        "cluster_id", "name", "summary", "core_question",
        "scientific_rationale", "next_research_direction", "keywords",
        "methods", "diseases", "populations", "data_types",
        "supporting_paper_ids", "quality_score", "quality_notes",
    ],
    "additionalProperties": False,
}


CLUSTER_THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": CLUSTER_THEME_ITEM,
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}


def rerank_opportunities(api_key, theme, opportunities, model="gpt-6-astra"):
    """Scientifically rerank one theme's shortlisted opportunities in one call."""
    if not opportunities:
        return {}
    compact = [{
        "opportunity_id": item["opportunity_id"],
        "title": item["title"],
        "agency": item.get("agency") or "",
        "description": (item.get("description") or "")[:2200],
        "funding_categories": item.get("funding_categories") or "",
        "funding_instruments": item.get("funding_instruments") or "",
    } for item in opportunities]
    parsed = _structured_chat(api_key, model, [
        {"role": "system", "content": (
            "You are a conservative scientific funding analyst. Score research "
            "relevance, not application success, prestige, or eligibility."
        )},
        {"role": "user", "content": (
            "Score every opportunity for scientific alignment with this theme. "
            "A match on even one meaningful scientific dimension must be retained "
            "for human review; give it a low score rather than calling it unrelated. "
            "Score five dimensions separately: research objective/question, disease "
            "or domain, method, population/data context, and funding mechanism. "
            "Use 0 for a clear mismatch, 50 when the announcement is silent or only "
            "broadly adjacent, and 100 for a direct match. Populate matched_dimensions "
            "and missing_dimensions using only: objective, disease, method, population, "
            "mechanism. Do not reward generic words. Overall rubric: 0-14 means no "
            "meaningful scientific overlap; 15-34 means one limited but reviewable "
            "overlap; 35-49 means adjacent; 50-69 means possible; 70-84 means strong; "
            "85-100 means directly aligned across most dimensions. "
            "Set hard_mismatch=true when the primary disease, target population, "
            "or scientific objective conflicts with the theme. Infrastructure-only "
            "or coordinating-center awards must receive mechanism_fit_score <=25 "
            "unless the theme explicitly proposes that role. A hard mismatch limits "
            "a high recommendation but does not remove the opportunity from review. "
            "The explanation and numeric scores must agree.\n\nTHEME:\n"
            + json.dumps(theme, ensure_ascii=False)
            + "\n\nOPPORTUNITIES:\n"
            + json.dumps(compact, ensure_ascii=False)
        )},
    ], RERANK_SCHEMA, "funding_rerank")
    allowed = {item["opportunity_id"] for item in opportunities}
    output = {}
    for item in parsed.get("matches", []):
        opportunity_id = str(item.get("opportunity_id") or "")
        if opportunity_id not in allowed:
            continue
        output[opportunity_id] = {
            "score": max(0.0, min(100.0, float(item["relevance_score"]))),
            "alignment_label": item["alignment_label"],
            "objective_match_score": max(
                0.0, min(100.0, float(item["objective_match_score"]))),
            "disease_match_score": max(
                0.0, min(100.0, float(item["disease_match_score"]))),
            "method_match_score": max(
                0.0, min(100.0, float(item["method_match_score"]))),
            "population_match_score": max(
                0.0, min(100.0, float(item["population_match_score"]))),
            "mechanism_fit_score": max(
                0.0, min(100.0, float(item["mechanism_fit_score"]))),
            "matched_dimensions": _clean_list(item["matched_dimensions"], 5),
            "missing_dimensions": _clean_list(item["missing_dimensions"], 5),
            "hard_mismatch": bool(item["hard_mismatch"]),
            "mismatch_reason": " ".join(
                str(item["mismatch_reason"]).split())[:300],
            "explanation": " ".join(str(item["explanation"]).split())[:500],
        }
    return output


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
                               model="gpt-6-astra", seed_keywords=None,
                               max_themes=3,
                               embedding_model="text-embedding-3-large"):
    """Cluster papers first, then generate and quality-review three themes."""
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
    paper_texts = [
        f"Title: {paper['title']}\nAbstract: {paper['abstract']}\n"
        f"Journal: {paper['journal']}\nYear: {paper['publication_date']}"
        for paper in papers
    ]
    seed_texts = []
    if seed_keywords:
        seed_texts = [", ".join(_clean_list(group)) or f"research theme {index}"
                      for index, group in enumerate(seed_keywords[:3], start=1)]
        seed_texts += [f"research theme {index}"
                       for index in range(len(seed_texts) + 1, 4)]
    vectors = embed_texts(api_key, paper_texts + seed_texts, model=embedding_model)
    paper_vectors = vectors[:len(papers)]
    clusters = cluster_papers(
        paper_vectors,
        seed_vectors=vectors[len(papers):] if seed_texts else None,
        k=3,
    )

    researcher_payload = {
        "name": researcher["name"],
        "title": researcher["title"],
        "career_stage": researcher["career_stage"],
        "country": researcher["country"],
    }
    cluster_payload = []
    for cluster_index, cluster in enumerate(clusters, start=1):
        cluster_payload.append({
            "cluster_id": cluster_index,
            "keyword_guidance": seed_texts[cluster_index - 1] if seed_texts else "",
            "coherence_score": round(100 * cluster["coherence"], 1),
            "separation_score": round(100 * cluster["separation"], 1),
            "stability_score": round(100 * cluster["stability"], 1),
            "papers": [papers[index] for index in cluster["indices"]],
            "excluded_outlier_paper_ids": [papers[index]["paper_id"]
                                           for index in cluster["outlier_indices"]],
        })

    generation_prompt = f"""
Build three precise, research-worthy themes from precomputed semantic paper clusters.

RESEARCHER:
{json.dumps(researcher_payload, ensure_ascii=False)}

PAPER CLUSTERS:
{json.dumps(cluster_payload, ensure_ascii=False)}

INSTRUCTIONS:
- Return exactly one theme for each cluster_id, preserving cluster order.
- Do not move a paper between clusters and do not use excluded outlier papers.
- Formulate a coherent scientific question, not merely a list of shared terms.
- The theme should connect a problem or mechanism, a method or evidence type,
  and a disease/population when supported.
- Propose a plausible next research direction that follows from the papers;
  do not claim unpublished results.
- Make themes specific, distinct, and useful for funding searches.
- Keywords should normally be specific 2-6 word scientific phrases, such as
  "transcriptome-wide association studies" or "electronic health records".
- Avoid isolated generic tokens such as "use", "study", "analysis", "health",
  "learning", "model", or "prediction".
- Distinguish research topics from methods, diseases, populations, and data types.
- Use only evidence in that cluster and its optional keyword guidance.
- supporting_paper_ids may contain only paper IDs from the same cluster.
- quality_score is a preliminary 0-100 assessment of coherence, specificity,
  research question clarity, evidence, next-step logic, funding relevance,
  and distinctiveness.
"""
    draft = _structured_chat(
        api_key, model, [
            {"role": "system", "content": (
                "You are a conservative scientific research analyst. "
                "Return only evidence-grounded structured data."
            )},
            {"role": "user", "content": generation_prompt},
        ],
        CLUSTER_THEMES_SCHEMA, "clustered_research_themes")

    review_prompt = f"""
Audit and, when needed, revise these draft research themes.

RESEARCHER:
{json.dumps(researcher_payload, ensure_ascii=False)}

FIXED PAPER CLUSTERS:
{json.dumps(cluster_payload, ensure_ascii=False)}

DRAFT THEMES:
{json.dumps(draft, ensure_ascii=False)}

For every theme, review: internal coherence, scientific specificity, clarity
of the research question, strength of paper evidence, logic of the proposed
next direction, funding relevance, and distinctiveness from the other themes.
Revise vague or list-like themes. Preserve cluster_id and never move papers
between clusters. Give a conservative final quality_score from 0 to 100 and
brief quality_notes naming any remaining limitation.
"""
    parsed = _structured_chat(
        api_key, model, [
            {"role": "system", "content": (
                "You are a rigorous scientific program reviewer. Return only "
                "evidence-grounded structured data."
            )},
            {"role": "user", "content": review_prompt},
        ], CLUSTER_THEMES_SCHEMA, "reviewed_research_themes")
    themes = parsed.get("themes") or []
    if len(themes) != 3:
        raise ValueError(f"OpenAI returned {len(themes)} themes instead of 3")

    themes_by_cluster = {int(theme.get("cluster_id", 0)): theme for theme in themes}
    validated = []
    for index, cluster in enumerate(clusters, start=1):
        theme = themes_by_cluster.get(index)
        if not theme:
            raise ValueError(f"OpenAI omitted paper cluster {index}")
        name = " ".join(str(theme.get("name") or "").split()).strip()
        summary = " ".join(str(theme.get("summary") or "").split()).strip()
        if not name or not summary:
            raise ValueError(f"OpenAI theme {index} is missing a name or summary")
        evidence = [papers[paper_index]["paper_id"]
                    for paper_index in cluster["indices"]]
        evidence_scores = {
            papers[paper_index]["paper_id"]: round(
                100 * cluster["similarities"][paper_index], 1)
            for paper_index in cluster["indices"]
        }
        quality_score = max(0.0, min(100.0, float(
            theme.get("quality_score") or 0)))
        validated.append({
            "name": name,
            "summary": summary,
            "keywords": _clean_keywords(theme.get("keywords")),
            "methods": _clean_list(theme.get("methods"), 12),
            "diseases": _clean_list(theme.get("diseases"), 12),
            "populations": _clean_list(theme.get("populations"), 12),
            "data_types": _clean_list(theme.get("data_types"), 12),
            "evidence": evidence[:20],
            "evidence_scores": evidence_scores,
            "excluded": [papers[paper_index]["paper_id"]
                         for paper_index in cluster["outlier_indices"]],
            "coherence_score": round(100 * cluster["coherence"], 1),
            "separation_score": round(100 * cluster["separation"], 1),
            "stability_score": round(100 * cluster["stability"], 1),
            "quality_score": round(quality_score, 1),
            "quality_notes": " ".join(
                str(theme.get("quality_notes") or "").split())[:500],
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
            "evidence_scores": theme["evidence_scores"],
            "excluded_output_ids": theme["excluded"],
            "coherence_score": theme["coherence_score"],
            "separation_score": theme["separation_score"],
            "stability_score": theme["stability_score"],
            "quality_score": theme["quality_score"],
            "quality_notes": theme["quality_notes"],
            "confidence": round((theme["coherence_score"] +
                                 theme["stability_score"] +
                                 theme["quality_score"]) / 300, 3),
            "manually_verified": 0,
            "generated_at": utcnow(),
        }, ["theme_id"])
    conn.commit()
    return len(validated)
