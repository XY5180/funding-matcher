import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .clients import PureClient, ScopusClient, SimplerGrantsClient, utcnow
from .db import upsert

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

def sync_grants(conn, config, query=""):
    n = 0
    for raw in SimplerGrantsClient(config).opportunities(query):
        row = normalize_grant(raw)
        if row["opportunity_id"]:
            upsert(conn, "opportunities", row, ["opportunity_id"]); n += 1
    conn.commit()
    return n

def terms(text):
    return [t for t in re.findall(r"[a-z][a-z0-9-]+", (text or "").lower())
            if len(t) > 2 and t not in STOP]

def phrases(text, vocabulary):
    lower = (text or "").lower()
    return sorted(p for p in vocabulary if p in lower)

def build_profiles(conn, max_themes=3):
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
        # Transparent topic construction: top weighted terms, split by recent evidence.
        df = Counter(t for text in texts for t in set(terms(text)))
        scored = Counter()
        for text in texts:
            c = Counter(terms(text))
            for t, v in c.items():
                scored[t] += v * (math.log((1 + len(texts))/(1 + df[t])) + 1)
        top = [t for t, _ in scored.most_common(30)]
        chunks = [top[i::max_themes] for i in range(max_themes)]
        conn.execute("DELETE FROM research_themes WHERE researcher_id=? AND manually_verified=0",
                     (researcher["researcher_id"],))
        for idx, chunk in enumerate(chunks):
            if not chunk: continue
            relevant = [(i, text) for i, text in enumerate(texts)
                        if set(chunk[:8]) & set(terms(text))]
            evidence = [rows[i]["output_id"] for i, _ in relevant if i < len(rows)][:8]
            combined = " ".join(text for _, text in relevant[:10])
            tid = hashlib.sha1(f"{researcher['researcher_id']}:{idx}".encode()).hexdigest()[:16]
            row = {"theme_id": tid, "researcher_id": researcher["researcher_id"],
                   "theme_name": " / ".join(chunk[:4]), "summary": " ".join(chunk[:15]),
                   "keywords": chunk[:20], "methods": phrases(combined, METHODS),
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

def weighted_fit(theme, opp, idf):
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
    parts = [(0.45,topic),(0.20,topic),(0.10,evidence)]
    if method is not None: parts.append((0.15,method))
    if domain is not None: parts.append((0.10,domain))
    score = sum(w*s for w,s in parts)/sum(w for w,_ in parts)
    shared = sorted(set(ta)&set(ob), key=lambda t: -ta[t]*ob[t]*idf.get(t,1)**2)[:12]
    return 100*score, 100*topic, None if method is None else 100*method, None if domain is None else 100*domain, 100*evidence, shared

def match_all(conn):
    themes = conn.execute("SELECT * FROM research_themes").fetchall()
    opportunities = conn.execute("SELECT * FROM opportunities WHERE status IN ('posted','forecasted') OR status IS NULL").fetchall()
    researchers = {r["researcher_id"]: r for r in conn.execute("SELECT * FROM researchers")}
    docs = [Counter(terms(" ".join(str(x) for x in row if x))) for row in themes + opportunities]
    df = Counter(t for doc in docs for t in doc)
    idf = {t: math.log((1+len(docs))/(1+n))+1 for t,n in df.items()}
    best = {}
    conn.execute("DELETE FROM theme_matches")
    for theme in themes:
        researcher = researchers[theme["researcher_id"]]
        for opp in opportunities:
            fit = weighted_fit(theme, opp, idf)
            status, reasons = eligibility(researcher, opp)
            score, topic, method, domain, evidence, shared = fit
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
                "matched_terms": shared,
                "explanation": f"Theme: {theme['theme_name']}. Shared evidence terms: {', '.join(shared) or 'none'}.",
                "model_version": "transparent-tfidf-v1",
                "scored_at": utcnow(),
            }
            upsert(conn, "theme_matches", pair_row,
                   ["researcher_id", "opportunity_id", "theme_id"])
            key = (researcher["researcher_id"], opp["opportunity_id"])
            if key not in best or fit[0] > best[key][0][0]:
                best[key] = (fit, theme, researcher, opp)
    for (rid, oid), (fit, theme, researcher, opp) in best.items():
        status, reasons = eligibility(researcher, opp)
        score, topic, method, domain, evidence, shared = fit
        row = {"researcher_id": rid, "opportunity_id": oid, "theme_id": theme["theme_id"],
               "eligibility_status": status, "eligibility_reasons": reasons,
               "scientific_fit": round(score,1), "topic_score": round(topic,1),
               "method_score": None if method is None else round(method,1),
               "domain_score": None if domain is None else round(domain,1),
               "evidence_score": round(evidence,1), "matched_terms": shared,
               "explanation": f"Best theme: {theme['theme_name']}. Shared evidence terms: {', '.join(shared) or 'none'}.",
               "model_version": "transparent-tfidf-v1", "scored_at": utcnow()}
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
