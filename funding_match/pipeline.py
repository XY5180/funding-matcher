import csv
import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .clients import PureClient, ScopusClient, SimplerGrantsClient, utcnow
from .db import upsert
from .llm import embed_texts, rerank_opportunities

STOP = set("""a an and are as at be been by can for from has have in into is it may
of on or our study studies that the their this to using was were will with research
project projects funding grant grants""".split())
METHODS = {"machine learning","deep learning","artificial intelligence","statistical genetics",
           "genomics","functional genomics","bioinformatics","causal inference","regression",
           "clinical trial","ehr","electronic health records","imaging","segmentation",
           "natural language processing","multi-omics","single cell","gwas"}
DATA_TYPES = {"ehr","electronic health records","genomics","imaging","survey","claims",
              "biobank","single cell","multi-omics","mri","ct","sequence","sequencing"}

def first(obj, *paths, default=""):
    for path in paths:
        value = obj
        for key in path.split("."):
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
            if value not in (None, "", []):
                break
        if value not in (None, "", []):
            if isinstance(value, dict):
                return value.get("en_GB") or value.get("en_US") or value.get("value") or str(value)
            return value
    return default

def identifiers(record):
    out = {}
    for item in record.get("identifiers", []) or []:
        key = str(first(item, "type.uri", "type.term.text", "type", default="")).lower()
        value = first(item, "id", "value", default="")
        if "orcid" in key: out["orcid"] = value
        if "scopus" in key: out["scopus_author_id"] = re.sub(r"\D", "", str(value))
    return out

def normalize_pure_person(raw):
    ids = identifiers(raw)
    pid = str(first(raw, "uuid", "id"))
    name = first(raw, "name.text", "name", "externalId", default=pid)
    if isinstance(raw.get("name"), dict):
        n = raw["name"]
        name = " ".join(filter(None, [n.get("firstName"), n.get("lastName")])) or name
    orgs = raw.get("organisationalUnits") or raw.get("organizations") or []
    org_id = str(first(orgs[0], "uuid", "id")) if orgs else ""
    emails = raw.get("emailAddresses") or []
    email = first(emails[0], "value", "email", default="") if emails else ""
    return {
        "researcher_id": "pure:" + pid, "pure_person_id": pid,
        "scopus_author_id": ids.get("scopus_author_id"), "orcid": ids.get("orcid"),
        "name": str(name), "email": email, "title": str(first(raw, "jobTitle.text", "jobTitle")),
        "career_stage": "", "country": "", "organization_id": org_id,
        "independent_pi": None, "works_with_animals": None,
        "profile_updated_at": utcnow(), "raw_json": raw
    }

def normalize_pure_output(raw):
    oid = str(first(raw, "uuid", "id"))
    ext = raw.get("electronicVersions") or []
    doi = ""
    for item in ext:
        doi = doi or first(item, "doi", default="")
    title = str(first(raw, "title.text", "title", default=oid))
    abstract = str(first(raw, "abstract.text", "abstract", default=""))
    date_value = first(raw, "publicationStatuses.0.publicationDate.year",
                       "publicationDate", default="")
    return {
        "output_id": "pure:" + oid, "pure_output_id": oid, "scopus_id": None,
        "eid": None, "doi": str(doi).lower().replace("https://doi.org/", ""),
        "pmid": None, "title": title, "abstract": abstract,
        "publication_date": str(date_value), "output_type": str(first(raw, "type.term.text", "type")),
        "journal": str(first(raw, "journalAssociation.journal.name.text", default="")),
        "citation_count": None, "source_updated_at": utcnow(), "raw_json": raw
    }

def related_person_ids(raw):
    """Extract person UUIDs from common Pure relation shapes without using names."""
    found = set()
    containers = []
    for key in ("personAssociations", "persons", "participants", "projectParticipants"):
        value = raw.get(key)
        if isinstance(value, list):
            containers.extend(value)
    def walk(value, parent_key=""):
        if isinstance(value, dict):
            for key, child in value.items():
                key_lower = key.lower()
                if key_lower == "uuid" and ("person" in parent_key.lower() or parent_key == ""):
                    found.add(str(child))
                elif key_lower in {"person", "personref", "personassociation"}:
                    walk(child, "person")
                else:
                    walk(child, key)
        elif isinstance(value, list):
            for child in value:
                walk(child, parent_key)
    for item in containers:
        walk(item)
    return found

def normalize_grant(raw):
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    oid = str(raw.get("opportunity_id", ""))
    applicants = raw.get("applicant_types") or summary.get("applicant_types") or []
    return {
        "opportunity_id": oid, "opportunity_number": raw.get("opportunity_number"),
        "title": raw.get("opportunity_title") or oid,
        "agency": raw.get("agency_name") or raw.get("agency_code"),
        "status": raw.get("opportunity_status"),
        "post_date": summary.get("post_date") or raw.get("post_date"),
        "close_date": summary.get("close_date") or raw.get("close_date"),
        "description": summary.get("summary_description") or raw.get("purpose_statement") or "",
        "applicant_types": applicants,
        "funding_instruments": raw.get("funding_instrument") or [],
        "funding_categories": raw.get("funding_category") or [],
        "award_floor": summary.get("award_floor") or raw.get("award_floor"),
        "award_ceiling": summary.get("award_ceiling") or raw.get("award_ceiling"),
        "expected_awards": summary.get("expected_number_of_awards") or raw.get("expected_number_of_awards"),
        "clinical_trial": "", "animal_required": "", "career_stages": [],
        "countries": [], "institution_types": applicants,
        "other_eligibility": "Review complete announcement for person-level and PI eligibility",
        "source_url": f"https://simpler.grants.gov/opportunity/{oid}",
        "source_updated_at": utcnow(), "raw_json": raw
    }

def sync_pure(conn, config):
    client = PureClient(config)
    counts = {}
    mapping = {"organizations": ("organizations", "organization_id"),
               "persons": ("researchers", "researcher_id"),
               "research_outputs": ("research_outputs", "output_id"),
               "projects": ("projects", "project_id")}
    for kind, (table, key) in mapping.items():
        n = 0
        for raw in client.records(kind):
            if kind == "persons":
                row = normalize_pure_person(raw)
            elif kind == "research_outputs":
                row = normalize_pure_output(raw)
            elif kind == "organizations":
                oid = str(first(raw, "uuid", "id"))
                parents = raw.get("parents") or raw.get("parentOrganisationalUnits") or []
                row = {"organization_id": oid, "name": str(first(raw, "name.text", "name", default=oid)),
                       "type": str(first(raw, "type.term.text", "type")),
                       "parent_organization_id": str(first(parents[0], "uuid", "id")) if parents else None,
                       "raw_json": raw, "updated_at": utcnow()}
            else:
                pid = str(first(raw, "uuid", "id"))
                row = {"project_id": "pure:"+pid, "pure_project_id": pid,
                       "title": str(first(raw, "title.text", "title", default=pid)),
                       "description": str(first(raw, "descriptions.0.value.text", "description")),
                       "start_date": str(first(raw, "period.startDate")), "end_date": str(first(raw, "period.endDate")),
                       "status": str(first(raw, "status.key", "status")), "raw_json": raw}
            upsert(conn, table, row, [key]); n += 1
            if kind in {"research_outputs", "projects"}:
                for pure_person_id in related_person_ids(raw):
                    person = conn.execute(
                        "SELECT researcher_id FROM researchers WHERE pure_person_id=?",
                        (pure_person_id,)).fetchone()
                    if not person:
                        continue
                    if kind == "research_outputs":
                        upsert(conn, "researcher_outputs",
                               {"researcher_id": person["researcher_id"],
                                "output_id": row["output_id"], "author_position": None,
                                "corresponding_author": None, "source": "pure"},
                               ["researcher_id", "output_id"])
                    else:
                        upsert(conn, "researcher_projects",
                               {"researcher_id": person["researcher_id"],
                                "project_id": row["project_id"], "researcher_role": ""},
                               ["researcher_id", "project_id"])
        counts[kind] = n
    conn.commit()
    return counts

def sync_scopus(conn, config):
    client = ScopusClient(config)
    researchers = conn.execute(
        "SELECT researcher_id,scopus_author_id,orcid,name,organization_id FROM researchers").fetchall()
    written = 0
    for researcher in researchers:
        aid = researcher["scopus_author_id"]
        if not aid:
            query = f"ORCID({researcher['orcid']})" if researcher["orcid"] else f'AUTHLASTNAME({researcher["name"].split()[-1]})'
            entries = client.author_search(query).get("search-results", {}).get("entry", [])
            if len(entries) == 1:
                aid = entries[0].get("dc:identifier", "").replace("AUTHOR_ID:", "")
                conn.execute("UPDATE researchers SET scopus_author_id=? WHERE researcher_id=?",
                             (aid, researcher["researcher_id"]))
            else:
                continue
        for entry in client.publications(aid):
            eid = entry.get("eid", "")
            sid = entry.get("dc:identifier", "").replace("SCOPUS_ID:", "")
            doi = (entry.get("prism:doi") or "").lower()
            output_id = "scopus:" + (sid or eid)
            abstract = ""
            if eid:
                detail = client.abstract(eid)
                core = detail.get("abstracts-retrieval-response", {}).get("coredata", {})
                abstract = core.get("dc:description") or ""
            row = {"output_id": output_id, "pure_output_id": None, "scopus_id": sid,
                   "eid": eid, "doi": doi, "pmid": entry.get("pubmed-id"),
                   "title": entry.get("dc:title") or output_id, "abstract": abstract,
                   "publication_date": entry.get("prism:coverDate"),
                   "output_type": entry.get("subtypeDescription"), "journal": entry.get("prism:publicationName"),
                   "citation_count": int(entry.get("citedby-count") or 0),
                   "source_updated_at": utcnow(), "raw_json": entry}
            try:
                upsert(conn, "research_outputs", row, ["output_id"])
            except Exception:
                existing = conn.execute("SELECT output_id FROM research_outputs WHERE doi=?", (doi,)).fetchone()
                output_id = existing["output_id"] if existing else output_id
            upsert(conn, "researcher_outputs",
                   {"researcher_id": researcher["researcher_id"], "output_id": output_id,
                    "author_position": None, "corresponding_author": None, "source": "scopus"},
                   ["researcher_id", "output_id"])
            written += 1
    conn.commit()
    return written

def import_scopus_researcher(conn, config, researcher_id, max_publications=20,
                             api_key=""):
    """Enrich one manually entered researcher without requiring Pure."""
    researcher = conn.execute(
        "SELECT * FROM researchers WHERE researcher_id=?", (researcher_id,)
    ).fetchone()
    if not researcher:
        raise ValueError(f"Unknown researcher: {researcher_id}")
    client = ScopusClient(config, api_key=api_key)
    author_id = (researcher["scopus_author_id"] or "").strip()
    if not author_id:
        orcid = (researcher["orcid"] or "").strip()
        if not orcid:
            raise ValueError("Enter an ORCID or Scopus Author ID before importing publications")
        entries = client.author_search(f"ORCID({orcid})").get("search-results", {}).get("entry", [])
        if len(entries) != 1:
            raise ValueError(
                f"Scopus author lookup returned {len(entries)} records for ORCID {orcid}; "
                "enter the Scopus Author ID to select the correct author"
            )
        author_id = entries[0].get("dc:identifier", "").replace("AUTHOR_ID:", "")
        conn.execute("UPDATE researchers SET scopus_author_id=? WHERE researcher_id=?",
                     (author_id, researcher_id))

    written = 0
    for entry in client.publications(author_id):
        if written >= max_publications:
            break
        eid = entry.get("eid", "")
        sid = entry.get("dc:identifier", "").replace("SCOPUS_ID:", "")
        doi = (entry.get("prism:doi") or "").lower()
        output_id = "scopus:" + (sid or eid)
        abstract = ""
        if eid:
            try:
                detail = client.abstract(eid)
                abstract = detail.get("abstracts-retrieval-response", {}).get(
                    "coredata", {}).get("dc:description") or ""
            except RuntimeError:
                # Search metadata is still useful when abstract entitlement is unavailable.
                abstract = ""
        row = {
            "output_id": output_id, "pure_output_id": None, "scopus_id": sid,
            "eid": eid, "doi": doi, "pmid": entry.get("pubmed-id"),
            "title": entry.get("dc:title") or output_id, "abstract": abstract,
            "publication_date": entry.get("prism:coverDate"),
            "output_type": entry.get("subtypeDescription"),
            "journal": entry.get("prism:publicationName"),
            "citation_count": int(entry.get("citedby-count") or 0),
            "source_updated_at": utcnow(), "raw_json": entry,
        }
        try:
            upsert(conn, "research_outputs", row, ["output_id"])
        except Exception:
            existing = conn.execute(
                "SELECT output_id FROM research_outputs WHERE doi=?", (doi,)
            ).fetchone()
            output_id = existing["output_id"] if existing else output_id
        upsert(conn, "researcher_outputs", {
            "researcher_id": researcher_id, "output_id": output_id,
            "author_position": None, "corresponding_author": None,
            "source": "scopus",
        }, ["researcher_id", "output_id"])
        written += 1
    conn.commit()
    return written, author_id

def sync_grants(conn, config, query="", api_key=""):
    n = 0
    for raw in SimplerGrantsClient(config, api_key=api_key).opportunities(query):
        row = normalize_grant(raw)
        if row["opportunity_id"]:
            upsert(conn, "opportunities", row, ["opportunity_id"]); n += 1
    conn.commit()
    return n


def theme_search_queries(theme, limit=6):
    """Build broad OR queries so one meaningful concept can enter review."""
    name = " ".join(str(theme["theme_name"] or "").split())
    keywords = decode_list(theme["keywords"])
    methods = decode_list(theme["methods"])
    diseases = decode_list(theme["diseases"])
    populations = decode_list(theme["populations"])
    data_types = decode_list(theme["data_types"])

    generic = {
        "analysis", "data", "health", "learning", "model", "models",
        "patients", "prediction", "research", "study", "using",
    }

    def useful(values, maximum=5):
        output = []
        for value in values:
            value = " ".join(str(value).split()).strip()
            tokens = re.findall(r"[a-z][a-z0-9-]+", value.casefold())
            if not value or not any(token not in generic for token in tokens):
                continue
            if value.casefold() not in {item.casefold() for item in output}:
                output.append(value)
            if len(output) >= maximum:
                break
        return output

    proposals = [
        useful(diseases + keywords[:3]),
        useful(methods + keywords[1:4]),
        useful(populations + data_types + keywords[:2]),
        useful(keywords, 6),
        useful([name] + diseases + methods, 5),
    ]

    selected = []
    for concepts in proposals:
        query = " ".join(concepts)[:100].strip()
        if query and query.casefold() not in {item.casefold() for item in selected}:
            selected.append(query)
        if len(selected) >= limit:
            break
    return selected or ["biomedical research"]


def theme_search_query(theme, keyword_limit=5):
    """Backward-compatible single-query helper."""
    return theme_search_queries(theme, limit=1)[0]


def sync_grants_by_theme(conn, config, researcher_id, api_key="",
                         max_per_theme=100):
    """Broadly retrieve and deduplicate reviewable candidates per theme."""
    themes = conn.execute(
        "SELECT * FROM research_themes WHERE researcher_id=? ORDER BY theme_id",
        (researcher_id,),
    ).fetchall()
    conn.execute(
        "DELETE FROM theme_opportunity_candidates WHERE researcher_id=?",
        (researcher_id,),
    )
    unique_opportunities = set()
    for theme in themes:
        queries = theme_search_queries(theme)
        theme_opportunities = set()
        query_hits = {}
        per_query = max(10, math.ceil(max_per_theme / len(queries)))
        for query in queries:
            query_written = 0
            for raw in SimplerGrantsClient(
                    config, api_key=api_key).opportunities(
                        query, query_operator="OR"):
                row = normalize_grant(raw)
                opportunity_id = row["opportunity_id"]
                if not opportunity_id:
                    continue
                query_hits.setdefault(opportunity_id, [])
                if query not in query_hits[opportunity_id]:
                    query_hits[opportunity_id].append(query)
                if opportunity_id in theme_opportunities:
                    continue
                upsert(conn, "opportunities", row, ["opportunity_id"])
                unique_opportunities.add(opportunity_id)
                theme_opportunities.add(opportunity_id)
                query_written += 1
                if query_written >= per_query or len(theme_opportunities) >= max_per_theme:
                    break
            if len(theme_opportunities) >= max_per_theme:
                break
            time.sleep(0.55)
        for opportunity_id in theme_opportunities:
            upsert(conn, "theme_opportunity_candidates", {
                "researcher_id": researcher_id,
                "theme_id": theme["theme_id"],
                "opportunity_id": opportunity_id,
                "query_text": query_hits.get(opportunity_id, []),
                "retrieved_at": utcnow(),
            }, ["researcher_id", "theme_id", "opportunity_id"])
    conn.commit()
    return len(unique_opportunities)

def terms(text):
    return [t for t in re.findall(r"[a-z][a-z0-9-]+", (text or "").lower())
            if len(t) > 2 and t not in STOP]

def phrases(text, vocabulary):
    lower = (text or "").lower()
    return sorted(p for p in vocabulary if p in lower)


GENERIC_THEME_TERMS = {
    "analysis", "associated", "association", "data", "effect", "findings",
    "health", "identifies", "learning", "method", "model", "models",
    "patients", "prediction", "research", "results", "study", "use", "using",
}
GENERIC_BOUNDARY_TERMS = {
    "associated", "findings", "identifies", "results", "study", "use", "using",
}


def rank_keyphrases(texts, titles=None, limit=24):
    """Rank readable scientific phrases, using titles more heavily than abstracts."""
    titles = titles or texts
    document_phrases = Counter()
    weighted = Counter()

    def add_ngrams(text, weight, max_n):
        tokens = terms(text)
        seen = set()
        for size in range(2, max_n + 1):
            for start in range(len(tokens) - size + 1):
                words = tokens[start:start + size]
                if (words[0] in GENERIC_BOUNDARY_TERMS
                        or words[-1] in GENERIC_BOUNDARY_TERMS):
                    continue
                phrase = " ".join(words)
                weighted[phrase] += weight * size
                seen.add(phrase)
        for phrase in seen:
            document_phrases[phrase] += 1

    for title in titles:
        add_ngrams(title, weight=4.0, max_n=4)
    for text in texts:
        add_ngrams(text, weight=0.7, max_n=3)

    candidates = Counter()
    for phrase, score in weighted.items():
        frequency = document_phrases[phrase]
        candidates[phrase] = score * (1 + math.log1p(frequency))

    # Specific single terms are only a fallback after multi-word phrases.
    unigrams = Counter(t for text in texts for t in terms(text)
                       if t not in GENERIC_THEME_TERMS)
    ordered = [phrase for phrase, _ in candidates.most_common()]
    ordered += [term for term, _ in unigrams.most_common()]

    selected = []
    for phrase in ordered:
        phrase_tokens = set(phrase.split())
        redundant = False
        for existing in selected:
            existing_tokens = set(existing.split())
            overlap = len(phrase_tokens & existing_tokens) / max(
                1, min(len(phrase_tokens), len(existing_tokens)))
            if overlap >= 0.8:
                redundant = True
                break
        if not redundant:
            selected.append(phrase)
        if len(selected) >= limit:
            break
    return selected

def build_profiles(conn, max_themes=3, seed_keywords=None, researcher_id=None):
    if researcher_id:
        researchers = conn.execute(
            "SELECT * FROM researchers WHERE researcher_id=?", (researcher_id,)
        ).fetchall()
    else:
        researchers = conn.execute("SELECT * FROM researchers").fetchall()
    written = 0
    for researcher in researchers:
        rows = conn.execute("""SELECT o.* FROM research_outputs o JOIN researcher_outputs ro
                              ON o.output_id=ro.output_id WHERE ro.researcher_id=?
                              ORDER BY o.publication_date DESC""",
                            (researcher["researcher_id"],)).fetchall()
        project_rows = conn.execute("""SELECT p.* FROM projects p JOIN researcher_projects rp
                                      ON p.project_id=rp.project_id WHERE rp.researcher_id=?""",
                                    (researcher["researcher_id"],)).fetchall()
        texts = [f"{r['title']} {r['abstract'] or ''}" for r in rows]
        texts += [f"{p['title']} {p['description'] or ''}" for p in project_rows]
        if not texts:
            raw = json.loads(researcher["raw_json"] or "{}")
            texts = [str(first(raw, "profileInformation.researchInterests.text",
                               "researchInterests", default=researcher["title"] or ""))]

        if seed_keywords:
            seeds = []
            for group in seed_keywords[:max_themes]:
                cleaned = [str(keyword).strip().lower() for keyword in group
                           if str(keyword).strip()]
                seeds.append(list(dict.fromkeys(cleaned)))
            seeds += [[] for _ in range(max_themes - len(seeds))]

            # Assign every publication/project to at most one theme. Exact
            # phrases receive more weight than individual token overlap, and
            # unmatched evidence is not forced into an unrelated theme.
            assignments = [[] for _ in range(max_themes)]
            for text_index, text in enumerate(texts):
                lower = text.lower()
                text_terms = set(terms(text))
                scores = []
                for group in seeds:
                    score = 0
                    for keyword in group:
                        keyword_terms = set(terms(keyword))
                        if keyword and keyword in lower:
                            score += 4 + len(keyword_terms)
                        score += len(keyword_terms & text_terms)
                    scores.append(score)
                best_score = max(scores, default=0)
                if best_score > 0:
                    assignments[scores.index(best_score)].append(text_index)

            conn.execute(
                "DELETE FROM research_themes WHERE researcher_id=? AND manually_verified=0",
                (researcher["researcher_id"],))
            for idx, group in enumerate(seeds):
                if not group:
                    continue
                assigned = assignments[idx]
                theme_texts = [texts[i] for i in assigned]
                analysis_texts = theme_texts or [" ".join(group)]
                assigned_titles = [rows[i]["title"] for i in assigned if i < len(rows)]
                top = rank_keyphrases(
                    analysis_texts, assigned_titles or analysis_texts, limit=24)
                keywords = list(dict.fromkeys(group + top))[:24]
                evidence = [rows[i]["output_id"] for i in assigned
                            if i < len(rows)][:12]
                combined = " ".join(theme_texts[:12])
                digest = hashlib.sha1(
                    f"{researcher['researcher_id']}:seeded:{idx}".encode()
                ).hexdigest()[:10]
                # Prefix preserves Theme 1/2/3 order when the UI reads rows.
                tid = f"seeded-{idx + 1}-{digest}"
                row = {
                    "theme_id": tid, "researcher_id": researcher["researcher_id"],
                    "theme_name": " / ".join(group[:3]),
                    "summary": "Research focused on " + ", ".join(keywords[:6]) + ".",
                    "keywords": keywords,
                    "methods": phrases(combined, METHODS),
                    "diseases": [], "populations": [],
                    "data_types": phrases(combined, DATA_TYPES),
                    "evidence_output_ids": evidence,
                    "confidence": min(1.0, 0.35 + 0.08 * len(evidence)),
                    "manually_verified": 0, "generated_at": utcnow(),
                }
                upsert(conn, "research_themes", row, ["theme_id"])
                written += 1
            continue

        # Phrase-based fallback: choose distinct phrase seeds, then assign each
        # paper to its strongest theme so evidence lists remain non-overlapping.
        titles = [r["title"] or "" for r in rows]
        ranked = rank_keyphrases(texts, titles or texts, limit=40)
        seeds = []
        for candidate in ranked:
            candidate_terms = set(candidate.split())
            if all(len(candidate_terms & set(seed.split())) /
                   max(1, len(candidate_terms | set(seed.split()))) < 0.5
                   for seed in seeds):
                seeds.append(candidate)
            if len(seeds) == max_themes:
                break
        seeds += [f"research theme {i + 1}" for i in range(len(seeds), max_themes)]
        assignments = [[] for _ in range(max_themes)]
        for text_index, text in enumerate(texts[:len(rows)]):
            lower = text.lower()
            text_terms = set(terms(text))
            scores = []
            for seed in seeds:
                seed_terms = set(terms(seed))
                score = len(seed_terms & text_terms)
                if seed in lower:
                    score += 4 + len(seed_terms)
                scores.append(score)
            best = scores.index(max(scores)) if max(scores, default=0) > 0 else min(
                range(max_themes), key=lambda idx: len(assignments[idx]))
            assignments[best].append(text_index)
        conn.execute("DELETE FROM research_themes WHERE researcher_id=? AND manually_verified=0",
                     (researcher["researcher_id"],))
        for idx, seed in enumerate(seeds):
            assigned = assignments[idx]
            theme_texts = [texts[i] for i in assigned]
            theme_titles = [titles[i] for i in assigned]
            keywords = rank_keyphrases(
                theme_texts or [seed], theme_titles or [seed], limit=20)
            keywords = list(dict.fromkeys([seed] + keywords))[:20]
            evidence = [rows[i]["output_id"] for i in assigned][:8]
            combined = " ".join(theme_texts[:10])
            tid = hashlib.sha1(f"{researcher['researcher_id']}:{idx}".encode()).hexdigest()[:16]
            row = {"theme_id": tid, "researcher_id": researcher["researcher_id"],
                   "theme_name": seed.title(),
                   "summary": "Research focused on " + ", ".join(keywords[:6]) + ".",
                   "keywords": keywords, "methods": phrases(combined, METHODS),
                   "diseases": [], "populations": [], "data_types": phrases(combined, DATA_TYPES),
                   "evidence_output_ids": evidence,
                   "confidence": min(1.0, 0.35 + 0.08 * len(evidence)),
                   "manually_verified": 0, "generated_at": utcnow()}
            upsert(conn, "research_themes", row, ["theme_id"]); written += 1
    conn.commit()
    return written

def decode_list(value):
    if not value: return []
    if isinstance(value, list): return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except Exception:
        return [x.strip() for x in str(value).split(";") if x.strip()]

def eligibility(researcher, opp, today=None):
    today = today or date.today()
    fail, review = [], []
    if opp["close_date"]:
        try:
            if date.fromisoformat(str(opp["close_date"])[:10]) < today:
                fail.append("deadline passed")
        except ValueError:
            review.append("verify deadline")
    else:
        review.append("deadline unavailable")
    allowed_stages = {x.lower() for x in decode_list(opp["career_stages"])}
    if allowed_stages:
        if not researcher["career_stage"]:
            review.append("career stage missing")
        elif researcher["career_stage"].lower() not in allowed_stages:
            fail.append("career stage outside allowed list")
    if str(opp["animal_required"]).lower() in {"true","yes","required"}:
        if researcher["works_with_animals"] == 0: fail.append("animal work required")
        elif researcher["works_with_animals"] is None: review.append("animal-work preference missing")
    if opp["other_eligibility"]:
        review.append(str(opp["other_eligibility"]))
    return ("ineligible", fail + review) if fail else (("review", review) if review else ("eligible", ["encoded checks passed"]))

def cosine(a, b, idf):
    dot = sum(a[t]*b[t]*idf.get(t,1)**2 for t in set(a)&set(b))
    na = math.sqrt(sum(v*v*idf.get(t,1)**2 for t,v in a.items()))
    nb = math.sqrt(sum(v*v*idf.get(t,1)**2 for t,v in b.items()))
    return dot/(na*nb) if na and nb else 0.0

def weighted_fit(theme, opp, idf, semantic=None, llm=None, review=None):
    ttext = " ".join(str(theme[k] or "") for k in ("theme_name","summary","keywords","methods","diseases","populations","data_types"))
    otext = " ".join(str(opp[k] or "") for k in ("title","description","funding_categories"))
    ta, ob = Counter(terms(ttext)), Counter(terms(otext))
    topic = cosine(ta, ob, idf)
    method_terms = set(terms(theme["methods"]))
    domain_terms = set(terms(" ".join(str(theme[k] or "") for k in ("diseases","populations","data_types"))))
    opp_terms = set(ob)
    method = len(method_terms & opp_terms)/len(method_terms) if method_terms else None
    domain = len(domain_terms & opp_terms)/len(domain_terms) if domain_terms else None
    evidence = min(1.0, len(decode_list(theme["evidence_output_ids"]))/5)
    if semantic is None:
        parts = [(0.45,topic),(0.20,topic)]
        if method is not None: parts.append((0.15,method))
        if domain is not None: parts.append((0.10,domain))
    else:
        # Semantic similarity carries most of the scientific signal. The
        # lexical score remains visible and reproducible, while the LLM (when
        # available) checks scientific and funding-mechanism alignment.
        parts = [(0.25, topic), (0.50, semantic)]
        if llm is not None: parts.append((0.20, llm))
        if method is not None: parts.append((0.03, method))
        if domain is not None: parts.append((0.02, domain))
    score = sum(w*s for w,s in parts)/sum(w for w,_ in parts)
    if review:
        # Human screening starts from the number and depth of scientifically
        # aligned dimensions. A single real overlap remains visible with a low
        # score; agreement across several dimensions raises the score.
        score = (
            0.30 * review["objective_match_score"]
            + 0.25 * review["disease_match_score"]
            + 0.20 * review["mechanism_fit_score"]
            + 0.15 * review["method_match_score"]
            + 0.10 * review["population_match_score"]
        ) / 100
    shared = matched_keyphrases(theme, opp, ta, ob, idf)
    return (100*score, 100*topic, None if method is None else 100*method,
            None if domain is None else 100*domain, 100*evidence,
            None if semantic is None else 100*semantic,
            None if llm is None else 100*llm, shared)


def apply_alignment_caps(score, theme, review):
    """Prevent generic overlap or core scientific mismatches from ranking highly."""
    if not review:
        return score
    label_caps = {
        "unrelated": 14.0,
        "generic_overlap": 34.0,
        "adjacent": 49.0,
        "partial_match": 69.0,
        "direct_match": 100.0,
    }
    score = min(score, label_caps.get(review.get("alignment_label"), 100.0))
    if review.get("hard_mismatch"):
        score = min(score, 49.0)
    if decode_list(theme["diseases"]) and review.get("disease_match_score", 50) <= 20:
        score = min(score, 49.0)
    if (decode_list(theme["populations"])
            and review.get("population_match_score", 50) <= 20):
        score = min(score, 55.0)
    if review.get("mechanism_fit_score", 100) <= 25:
        score = min(score, 49.0)
    return score


def matched_keyphrases(theme, opp, theme_terms, opportunity_terms, idf, limit=10):
    """Return readable shared scientific concepts without changing the fit score."""
    opportunity_text = " ".join(
        str(opp[key] or "") for key in ("title", "description", "funding_categories")
    ).lower()
    candidates = []
    for key in ("keywords", "methods", "diseases", "populations", "data_types"):
        candidates.extend(decode_list(theme[key]))
    candidates.insert(0, str(theme["theme_name"] or ""))

    ranked = []
    seen = set()
    for candidate in candidates:
        phrase = " ".join(terms(str(candidate)))
        tokens = phrase.split()
        if not phrase or phrase in seen:
            continue
        # Report scientific phrases rather than the isolated tokens used by
        # the transparent TF-IDF scorer internally.
        exact = phrase in opportunity_text
        overlap = set(tokens) & set(opportunity_terms)
        if exact or (len(tokens) >= 2 and len(overlap) / len(tokens) >= 0.67):
            if len(tokens) >= 2:
                score = sum(
                    theme_terms[token] * opportunity_terms[token] * idf.get(token, 1) ** 2
                    for token in overlap
                ) + (5 if exact else 0)
                ranked.append((score, phrase))
                seen.add(phrase)

    ranked.sort(reverse=True)
    selected = []
    for _, phrase in ranked:
        phrase_tokens = set(phrase.split())
        if any(
            len(phrase_tokens & set(existing.split()))
            / max(1, min(len(phrase_tokens), len(set(existing.split())))) >= 0.75
            for existing in selected
        ):
            continue
        selected.append(phrase)
        if len(selected) >= limit:
            break
    return selected

def _theme_text(theme):
    return " ".join(str(theme[key] or "") for key in (
        "theme_name", "summary", "keywords", "methods", "diseases",
        "populations", "data_types"))


def _opportunity_text(opportunity):
    return " ".join(str(opportunity[key] or "") for key in (
        "title", "description", "funding_categories", "funding_instruments"))


def _candidate_opportunities(conn, theme, all_opportunities):
    candidate_ids = {row[0] for row in conn.execute("""
        SELECT opportunity_id FROM theme_opportunity_candidates
        WHERE researcher_id=? AND theme_id=?
    """, (theme["researcher_id"], theme["theme_id"]))}
    if not candidate_ids:
        return all_opportunities
    return [opportunity for opportunity in all_opportunities
            if opportunity["opportunity_id"] in candidate_ids]


def match_all(conn, openai_api_key="", openai_model="gpt-6-astra",
              embedding_model="text-embedding-3-large", warnings=None):
    themes = conn.execute("SELECT * FROM research_themes").fetchall()
    opportunities = conn.execute("SELECT * FROM opportunities WHERE status IN ('posted','forecasted') OR status IS NULL").fetchall()
    researchers = {r["researcher_id"]: r for r in conn.execute("SELECT * FROM researchers")}
    docs = [Counter(terms(" ".join(str(x) for x in row if x))) for row in themes + opportunities]
    df = Counter(t for doc in docs for t in doc)
    idf = {t: math.log((1+len(docs))/(1+n))+1 for t,n in df.items()}
    semantic = {}
    llm_reviews = {}
    if openai_api_key and themes and opportunities:
        try:
            theme_keys = [(theme["researcher_id"], theme["theme_id"])
                          for theme in themes]
            opportunity_by_id = {item["opportunity_id"]: item for item in opportunities}
            opportunity_ids = list(opportunity_by_id)
            texts = [_theme_text(theme) for theme in themes]
            texts.extend(_opportunity_text(opportunity_by_id[oid]) for oid in opportunity_ids)
            vectors = []
            for start in range(0, len(texts), 64):
                vectors.extend(embed_texts(
                    openai_api_key, texts[start:start + 64], model=embedding_model))
            theme_vectors = dict(zip(theme_keys, vectors[:len(themes)]))
            opportunity_vectors = dict(zip(opportunity_ids, vectors[len(themes):]))
            for theme in themes:
                key = (theme["researcher_id"], theme["theme_id"])
                for opportunity in _candidate_opportunities(conn, theme, opportunities):
                    raw_cosine = sum(
                        left * right for left, right in
                        zip(theme_vectors[key], opportunity_vectors[opportunity["opportunity_id"]])
                    )
                    semantic[(key[0], key[1], opportunity["opportunity_id"])] = max(
                        0.0, min(1.0, raw_cosine))

            # Rerank only the strongest preliminary candidates to control cost
            # and latency. Each theme is handled in a single structured call.
            for theme in themes:
                candidates = _candidate_opportunities(conn, theme, opportunities)
                preliminary = []
                for opportunity in candidates:
                    sem = semantic.get((theme["researcher_id"], theme["theme_id"],
                                        opportunity["opportunity_id"]))
                    fit = weighted_fit(theme, opportunity, idf, semantic=sem)
                    preliminary.append((fit[0], opportunity))
                shortlist = [dict(item) for _, item in sorted(
                    preliminary, key=lambda pair: pair[0], reverse=True)[:15]]
                theme_payload = {
                    "name": theme["theme_name"], "summary": theme["summary"],
                    "keywords": decode_list(theme["keywords"]),
                    "methods": decode_list(theme["methods"]),
                    "diseases": decode_list(theme["diseases"]),
                    "populations": decode_list(theme["populations"]),
                    "data_types": decode_list(theme["data_types"]),
                }
                reviews = rerank_opportunities(
                    openai_api_key, theme_payload, shortlist, model=openai_model)
                for opportunity_id, review in reviews.items():
                    llm_reviews[(theme["researcher_id"], theme["theme_id"],
                                 opportunity_id)] = review
        except Exception as exc:
            # OpenAI is an enhancement. Preserve a complete transparent result
            # if embeddings or reranking are temporarily unavailable.
            semantic = {}
            llm_reviews = {}
            if warnings is not None:
                warnings.append(
                    "OpenAI semantic funding matching was unavailable, so the "
                    f"local matcher was used instead ({exc}).")
    best = {}
    conn.execute("DELETE FROM theme_matches")
    conn.execute("DELETE FROM matches")
    for theme in themes:
        researcher = researchers[theme["researcher_id"]]
        for opp in _candidate_opportunities(conn, theme, opportunities):
            match_key = (theme["researcher_id"], theme["theme_id"],
                         opp["opportunity_id"])
            sem = semantic.get(match_key)
            review = llm_reviews.get(match_key)
            llm_value = None if review is None else review["score"] / 100
            fit = weighted_fit(
                theme, opp, idf, semantic=sem, llm=llm_value, review=review)
            fit = (apply_alignment_caps(fit[0], theme, review), *fit[1:])
            status, reasons = eligibility(researcher, opp)
            score, topic, method, domain, evidence, semantic_score, llm_score, shared = fit
            explanation = (review["explanation"] if review else
                           f"Theme: {theme['theme_name']}. Shared evidence terms: "
                           f"{', '.join(shared) or 'none'}.")
            pair_row = {
                "researcher_id": researcher["researcher_id"],
                "opportunity_id": opp["opportunity_id"],
                "theme_id": theme["theme_id"],
                "eligibility_status": status,
                "eligibility_reasons": reasons,
                "scientific_fit": round(score, 1),
                "topic_score": round(topic, 1),
                "method_score": None if method is None else round(method, 1),
                "domain_score": None if domain is None else round(domain, 1),
                "evidence_score": round(evidence, 1),
                "semantic_score": None if semantic_score is None else round(semantic_score, 1),
                "llm_score": None if llm_score is None else round(llm_score, 1),
                "alignment_label": None if review is None else review["alignment_label"],
                "objective_match_score": None if review is None else review["objective_match_score"],
                "disease_match_score": None if review is None else review["disease_match_score"],
                "method_match_score": None if review is None else review["method_match_score"],
                "population_match_score": None if review is None else review["population_match_score"],
                "mechanism_fit_score": None if review is None else review["mechanism_fit_score"],
                "hard_mismatch": None if review is None else int(review["hard_mismatch"]),
                "mismatch_reason": None if review is None else review["mismatch_reason"],
                "matched_dimensions": [] if review is None else review["matched_dimensions"],
                "missing_dimensions": [] if review is None else review["missing_dimensions"],
                "matched_terms": shared,
                "explanation": explanation,
                "model_version": ("dimension-weighted-llm-v4" if semantic_score is not None
                                  else "transparent-tfidf-v1"),
                "scored_at": utcnow(),
            }
            upsert(conn, "theme_matches", pair_row,
                   ["researcher_id", "opportunity_id", "theme_id"])
            key = (researcher["researcher_id"], opp["opportunity_id"])
            if key not in best or fit[0] > best[key][0][0]:
                best[key] = (fit, theme, researcher, opp, explanation, review)
    for (rid, oid), (fit, theme, researcher, opp, explanation, review) in best.items():
        status, reasons = eligibility(researcher, opp)
        score, topic, method, domain, evidence, semantic_score, llm_score, shared = fit
        row = {"researcher_id": rid, "opportunity_id": oid, "theme_id": theme["theme_id"],
               "eligibility_status": status, "eligibility_reasons": reasons,
               "scientific_fit": round(score,1), "topic_score": round(topic,1),
               "method_score": None if method is None else round(method,1),
               "domain_score": None if domain is None else round(domain,1),
               "evidence_score": round(evidence,1),
               "semantic_score": None if semantic_score is None else round(semantic_score,1),
               "llm_score": None if llm_score is None else round(llm_score,1),
               "alignment_label": None if review is None else review["alignment_label"],
               "objective_match_score": None if review is None else review["objective_match_score"],
               "disease_match_score": None if review is None else review["disease_match_score"],
               "method_match_score": None if review is None else review["method_match_score"],
               "population_match_score": None if review is None else review["population_match_score"],
               "mechanism_fit_score": None if review is None else review["mechanism_fit_score"],
               "hard_mismatch": None if review is None else int(review["hard_mismatch"]),
               "mismatch_reason": None if review is None else review["mismatch_reason"],
               "matched_dimensions": [] if review is None else review["matched_dimensions"],
               "missing_dimensions": [] if review is None else review["missing_dimensions"],
               "matched_terms": shared, "explanation": explanation,
               "model_version": ("dimension-weighted-llm-v4" if semantic_score is not None
                                 else "transparent-tfidf-v1"),
               "scored_at": utcnow()}
        upsert(conn, "matches", row, ["researcher_id","opportunity_id"])
    conn.commit()
    return len(best)

def export_theme_matches(conn, output, top_k=None, minimum_fit=0):
    """Export ranked funding opportunities for every research theme."""
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("""SELECT r.name,t.theme_name,tm.*,o.opportunity_number,
                          o.title,o.agency,o.status,o.close_date,o.award_ceiling,o.source_url
                          FROM theme_matches tm
                          JOIN researchers r USING(researcher_id)
                          JOIN research_themes t USING(theme_id)
                          JOIN opportunities o USING(opportunity_id)
                          WHERE tm.scientific_fit >= ?
                          ORDER BY r.name,t.theme_name,
                          CASE tm.eligibility_status WHEN 'eligible' THEN 0
                          WHEN 'review' THEN 1 ELSE 2 END,
                          tm.scientific_fit DESC""", (minimum_fit,)).fetchall()
    selected, counts = [], defaultdict(int)
    for row in rows:
        key = (row["researcher_id"], row["theme_id"])
        if top_k is None or counts[key] < top_k:
            selected.append(row); counts[key] += 1
    if not selected: return 0
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=selected[0].keys())
        writer.writeheader(); writer.writerows(dict(r) for r in selected)
    return len(selected)

def export_matches(conn, output):
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("""SELECT r.name,m.*,o.opportunity_number,o.title,o.agency,o.status,
                          o.close_date,o.award_ceiling,o.source_url
                          FROM matches m JOIN researchers r USING(researcher_id)
                          JOIN opportunities o USING(opportunity_id)
                          ORDER BY r.name,
                          CASE m.eligibility_status WHEN 'eligible' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                          m.scientific_fit DESC""").fetchall()
    if not rows: return 0
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(dict(r) for r in rows)
    return len(rows)

def import_feedback(conn, path):
    required = {"researcher_id","opportunity_id","label"}
    allowed = {"strong_match","possible_match","not_relevant","not_eligible","unsure"}
    count = 0
    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Feedback CSV requires researcher_id, opportunity_id, label")
        for row in reader:
            label = row["label"].strip().lower()
            if label not in allowed:
                raise ValueError(f"Unsupported feedback label: {label}")
            upsert(conn, "match_feedback",
                   {"researcher_id":row["researcher_id"],"opportunity_id":row["opportunity_id"],
                    "label":label,"reason":row.get("reason",""),"created_at":utcnow()},
                   ["researcher_id","opportunity_id","created_at"])
            count += 1
    conn.commit()
    return count

def evaluation_report(conn, k=10):
    rows = conn.execute("""SELECT m.researcher_id,m.opportunity_id,m.scientific_fit,f.label
                           FROM matches m JOIN match_feedback f
                           ON m.researcher_id=f.researcher_id
                           AND m.opportunity_id=f.opportunity_id
                           ORDER BY m.researcher_id,m.scientific_fit DESC""").fetchall()
    grouped = defaultdict(list)
    for row in rows: grouped[row["researcher_id"]].append(row)
    precisions, recalls, ndcgs = [], [], []
    relevant = {"strong_match","possible_match"}
    gains = {"strong_match":3,"possible_match":1,"unsure":0,
             "not_relevant":0,"not_eligible":0}
    for items in grouped.values():
        top = items[:k]
        total_rel = sum(x["label"] in relevant for x in items)
        precisions.append(sum(x["label"] in relevant for x in top)/max(1,len(top)))
        recalls.append(sum(x["label"] in relevant for x in top)/max(1,total_rel))
        dcg = sum(gains[x["label"]]/math.log2(i+2) for i,x in enumerate(top))
        ideal = sorted((gains[x["label"]] for x in items), reverse=True)[:k]
        idcg = sum(g/math.log2(i+2) for i,g in enumerate(ideal))
        ndcgs.append(dcg/idcg if idcg else 0)
    eligibility_errors = conn.execute("""SELECT count(*) FROM matches m JOIN match_feedback f
      ON m.researcher_id=f.researcher_id AND m.opportunity_id=f.opportunity_id
      WHERE f.label='not_eligible' AND m.eligibility_status!='ineligible'""").fetchone()[0]
    return {"researchers":len(grouped),"labeled_pairs":len(rows),
            f"precision_at_{k}":round(sum(precisions)/len(precisions),4) if precisions else None,
            f"recall_at_{k}":round(sum(recalls)/len(recalls),4) if recalls else None,
            f"ndcg_at_{k}":round(sum(ndcgs)/len(ndcgs),4) if ndcgs else None,
            "eligibility_false_pass_count":eligibility_errors}
