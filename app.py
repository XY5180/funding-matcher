#!/usr/bin/env python3
"""Streamlit front end for the existing funding-match backend."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

import streamlit as st

from funding_match.clients import utcnow
from funding_match.db import connect, upsert
from funding_match.llm import build_profiles_with_openai
from funding_match.pipeline import (build_profiles, import_scopus_researcher,
                                    match_all, sync_grants_by_theme)


ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "quick_opportunities.json"
CONFIG = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))


def split_terms(value):
    """Accept commas, semicolons, or new lines from a web form."""
    value = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def request_config(scopus_api_key="", simpler_grants_api_key=""):
    """Build a per-request config without changing process-wide environment variables."""
    config = deepcopy(CONFIG)
    if scopus_api_key.strip():
        config["scopus"]["api_key"] = scopus_api_key.strip()
    if simpler_grants_api_key.strip():
        config["simpler_grants"]["api_key"] = simpler_grants_api_key.strip()
    return config


def server_setting(name, default=""):
    """Read Streamlit Secrets first, then an environment variable."""
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, KeyError):
        value = ""
    return str(value or os.environ.get(name, default)).strip()


def run_match(profile, themes, use_scopus=False, max_publications=20,
              use_live_grants=False, funding_query="", seed_keywords=None,
              scopus_api_key="", simpler_grants_api_key="", use_llm=False,
              openai_api_key="", openai_model="gpt-4o-mini"):
    """Run one isolated match without changing the command-line database."""
    with tempfile.TemporaryDirectory(prefix="funding-match-") as tmp:
        run_config = request_config(scopus_api_key, simpler_grants_api_key)
        conn = connect(Path(tmp) / "web-demo.db")
        organization = {
            "organization_id": "web-form-org",
            "name": profile["organization"],
            "type": "higher_education",
            "parent_organization_id": None,
            "raw_json": {"source": "streamlit form"},
            "updated_at": utcnow(),
        }
        researcher = {
            "researcher_id": "web-form-researcher",
            "pure_person_id": None,
            "scopus_author_id": profile["scopus_author_id"] or None,
            "orcid": profile["orcid"] or None,
            "name": profile["name"],
            "email": profile["email"],
            "title": profile["title"],
            "career_stage": profile["career_stage"],
            "country": profile["country"],
            "organization_id": "web-form-org",
            "independent_pi": profile["independent_pi"],
            "works_with_animals": profile["works_with_animals"],
            "profile_updated_at": utcnow(),
            "raw_json": {"source": "streamlit form"},
        }
        upsert(conn, "organizations", organization, ["organization_id"])
        upsert(conn, "researchers", researcher, ["researcher_id"])

        for index, theme in enumerate(themes, start=1):
            upsert(conn, "research_themes", {
                "theme_id": f"web-theme-{index}",
                "researcher_id": "web-form-researcher",
                "theme_name": theme["name"],
                "summary": theme["summary"],
                "keywords": theme["keywords"],
                "methods": theme["methods"],
                "diseases": theme["diseases"],
                "populations": theme["populations"],
                "data_types": theme["data_types"],
                "evidence_output_ids": [],
                "confidence": 0.8,
                "manually_verified": 1,
                "generated_at": utcnow(),
            }, ["theme_id"])

        imported_publications = 0
        resolved_author_id = profile["scopus_author_id"]
        system_warnings = []
        theme_source = "Manually entered themes"
        if use_scopus:
            imported_publications, resolved_author_id = import_scopus_researcher(
                conn, run_config, "web-form-researcher",
                max_publications=max_publications, api_key=scopus_api_key)
            if use_llm and openai_api_key:
                try:
                    build_profiles_with_openai(
                        conn, "web-form-researcher", openai_api_key,
                        model=openai_model, seed_keywords=seed_keywords,
                        max_themes=3)
                    theme_source = (
                        f"Semantic paper clustering + OpenAI {openai_model}")
                except Exception as exc:
                    build_profiles(
                        conn, max_themes=3, seed_keywords=seed_keywords,
                        researcher_id="web-form-researcher")
                    theme_source = "Local algorithm fallback"
                    system_warnings.append(
                        "OpenAI theme generation was unavailable, so the local "
                        f"theme algorithm was used instead ({exc}).")
            else:
                build_profiles(
                    conn, max_themes=3, seed_keywords=seed_keywords,
                    researcher_id="web-form-researcher")
                theme_source = "Local theme algorithm"

        funding_warning = None

        def load_snapshot():
            snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            for opportunity in snapshot["opportunities"]:
                upsert(conn, "opportunities", opportunity, ["opportunity_id"])
            themes_for_snapshot = conn.execute("""
                SELECT theme_id FROM research_themes
                WHERE researcher_id='web-form-researcher'
            """).fetchall()
            for theme_row in themes_for_snapshot:
                for opportunity in snapshot["opportunities"]:
                    upsert(conn, "theme_opportunity_candidates", {
                        "researcher_id": "web-form-researcher",
                        "theme_id": theme_row["theme_id"],
                        "opportunity_id": opportunity["opportunity_id"],
                        "query_text": "included snapshot",
                        "retrieved_at": utcnow(),
                    }, ["researcher_id", "theme_id", "opportunity_id"])
            return (len(snapshot["opportunities"]),
                    f"Snapshot {snapshot['snapshot_date']}")

        if use_live_grants:
            try:
                opportunity_count = sync_grants_by_theme(
                    conn, run_config, "web-form-researcher",
                    api_key=simpler_grants_api_key, max_per_theme=100)
                if not opportunity_count:
                    raise ValueError(
                        "Simpler.Grants.gov returned no opportunities for this query")
                funding_source = "Live Simpler.Grants.gov search"
            except (RuntimeError, ValueError) as exc:
                opportunity_count, funding_source = load_snapshot()
                if "HTTP 401" in str(exc) or "Invalid API key" in str(exc):
                    funding_warning = (
                        "Simpler.Grants.gov rejected the API key (HTTP 401). "
                        "The included funding snapshot was used instead. Check "
                        "SIMPLER_GRANTS_API_KEY, then restart the app.")
                else:
                    funding_warning = (
                        "The live Simpler.Grants.gov search was unavailable, so "
                        "the included funding snapshot was used instead.")
        else:
            opportunity_count, funding_source = load_snapshot()
        if funding_warning:
            system_warnings.append(funding_warning)
        warning_message = " ".join(system_warnings) or None
        conn.commit()
        match_all(
            conn,
            openai_api_key=openai_api_key if use_llm else "",
            openai_model=openai_model,
            warnings=system_warnings,
        )
        warning_message = " ".join(system_warnings) or None

        generated_themes = []
        generated_rows = conn.execute("""
            SELECT * FROM research_themes
            WHERE researcher_id='web-form-researcher' AND manually_verified=0
            ORDER BY theme_id
        """).fetchall()
        for theme in generated_rows:
            evidence_ids = json.loads(theme["evidence_output_ids"] or "[]")
            evidence_scores = json.loads(theme["evidence_scores"] or "{}")
            excluded_ids = json.loads(theme["excluded_output_ids"] or "[]")
            evidence_papers = []
            excluded_papers = []
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                paper_rows = conn.execute(
                    f"SELECT output_id, title, publication_date FROM research_outputs "
                    f"WHERE output_id IN ({placeholders})", evidence_ids
                ).fetchall()
                by_id = {paper["output_id"]: {
                    **dict(paper),
                    "theme_similarity": evidence_scores.get(paper["output_id"]),
                } for paper in paper_rows}
                evidence_papers = [by_id[output_id] for output_id in evidence_ids
                                   if output_id in by_id]
            if excluded_ids:
                placeholders = ",".join("?" for _ in excluded_ids)
                excluded_rows = conn.execute(
                    f"SELECT output_id, title, publication_date FROM research_outputs "
                    f"WHERE output_id IN ({placeholders})", excluded_ids
                ).fetchall()
                excluded_by_id = {paper["output_id"]: dict(paper)
                                  for paper in excluded_rows}
                excluded_papers = [excluded_by_id[output_id] for output_id in excluded_ids
                                   if output_id in excluded_by_id]
            generated_themes.append({
                "name": theme["theme_name"] or "",
                "summary": theme["summary"] or "",
                "keywords": json.loads(theme["keywords"] or "[]"),
                "methods": json.loads(theme["methods"] or "[]"),
                "diseases": json.loads(theme["diseases"] or "[]"),
                "populations": json.loads(theme["populations"] or "[]"),
                "data_types": json.loads(theme["data_types"] or "[]"),
                "evidence_papers": evidence_papers,
                "excluded_papers": excluded_papers,
                "coherence_score": theme["coherence_score"],
                "separation_score": theme["separation_score"],
                "stability_score": theme["stability_score"],
                "quality_score": theme["quality_score"],
                "quality_notes": theme["quality_notes"] or "",
                "generation_source": theme_source,
                "generation_warning": next(
                    (warning for warning in system_warnings
                     if warning.startswith("OpenAI theme generation")), ""),
            })

        opportunity_rows = conn.execute("""
            SELECT m.scientific_fit, m.topic_score, m.method_score,
                   m.domain_score, m.evidence_score, m.semantic_score,
                   m.llm_score, m.model_version, m.eligibility_status,
                   m.eligibility_reasons, m.matched_terms, m.explanation,
                   m.alignment_label, m.objective_match_score,
                   m.disease_match_score, m.method_match_score,
                   m.population_match_score, m.mechanism_fit_score,
                   m.hard_mismatch, m.mismatch_reason,
                   m.matched_dimensions, m.missing_dimensions,
                   c.query_text AS retrieval_queries,
                   t.theme_name, t.coherence_score AS theme_coherence_score,
                   t.separation_score AS theme_separation_score,
                   t.stability_score AS theme_stability_score,
                   t.quality_score AS theme_quality_score,
                   o.opportunity_number, o.title, o.agency,
                   o.close_date, o.source_url
            FROM matches m
            JOIN opportunities o USING(opportunity_id)
            JOIN research_themes t USING(theme_id)
            LEFT JOIN theme_opportunity_candidates c
              ON c.researcher_id=m.researcher_id
             AND c.theme_id=m.theme_id
             AND c.opportunity_id=m.opportunity_id
            WHERE m.researcher_id='web-form-researcher'
            ORDER BY CASE m.eligibility_status
                       WHEN 'eligible' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                     m.scientific_fit DESC
        """).fetchall()
        theme_rows = conn.execute("""
            SELECT tm.scientific_fit, tm.topic_score, tm.method_score,
                   tm.domain_score, tm.evidence_score, tm.semantic_score,
                   tm.llm_score, tm.model_version, tm.eligibility_status,
                   tm.eligibility_reasons, tm.matched_terms, tm.explanation,
                   tm.alignment_label, tm.objective_match_score,
                   tm.disease_match_score, tm.method_match_score,
                   tm.population_match_score, tm.mechanism_fit_score,
                   tm.hard_mismatch, tm.mismatch_reason,
                   tm.matched_dimensions, tm.missing_dimensions,
                   c.query_text AS retrieval_queries,
                   t.theme_name, t.theme_id,
                   t.coherence_score AS theme_coherence_score,
                   t.separation_score AS theme_separation_score,
                   t.stability_score AS theme_stability_score,
                   t.quality_score AS theme_quality_score,
                   o.opportunity_number, o.title,
                   o.agency, o.close_date, o.source_url
            FROM theme_matches tm
            JOIN opportunities o USING(opportunity_id)
            JOIN research_themes t USING(theme_id)
            LEFT JOIN theme_opportunity_candidates c
              ON c.researcher_id=tm.researcher_id
             AND c.theme_id=tm.theme_id
             AND c.opportunity_id=tm.opportunity_id
            WHERE tm.researcher_id='web-form-researcher'
            ORDER BY t.theme_name,
                     CASE tm.eligibility_status
                       WHEN 'eligible' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                     tm.scientific_fit DESC
        """).fetchall()
        return ([dict(row) for row in opportunity_rows],
                [dict(row) for row in theme_rows], funding_source,
                imported_publications, resolved_author_id, opportunity_count,
                warning_message, generated_themes, theme_source)


def parse_list_value(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (TypeError, json.JSONDecodeError):
        return [str(value)]


def render_score_details(row):
    parts = []
    labels = [
        ("Topic", "topic_score"), ("Semantic", "semantic_score"),
        ("LLM review", "llm_score"), ("Method", "method_score"),
        ("Domain", "domain_score"),
    ]
    for label, key in labels:
        value = row.get(key)
        if value is not None:
            parts.append(f"{label} {float(value):.1f}")
    if parts:
        st.caption("Score components: " + " · ".join(parts))
    if row.get("evidence_score") is not None:
        st.caption(
            f"Research evidence strength: {float(row['evidence_score']):.1f}/100 "
            "(reported separately; not used in Scientific fit)."
        )
    alignment = row.get("alignment_label")
    alignment_parts = []
    for label, key in [
        ("Objective", "objective_match_score"),
        ("Disease", "disease_match_score"),
        ("Method", "method_match_score"),
        ("Population", "population_match_score"),
        ("Mechanism", "mechanism_fit_score"),
    ]:
        value = row.get(key)
        if value is not None:
            alignment_parts.append(f"{label} {float(value):.0f}/100")
    if alignment or alignment_parts:
        label_text = (alignment or "not reviewed").replace("_", " ").title()
        st.caption(
            f"Alignment class: {label_text}"
            + (" · " + " · ".join(alignment_parts) if alignment_parts else "")
        )
    matched_dimensions = parse_list_value(row.get("matched_dimensions"))
    missing_dimensions = parse_list_value(row.get("missing_dimensions"))
    if matched_dimensions:
        st.write("**Matched research dimensions:** " + ", ".join(matched_dimensions))
    if missing_dimensions:
        st.caption("Missing or weak dimensions: " + ", ".join(missing_dimensions))
    retrieval_queries = parse_list_value(row.get("retrieval_queries"))
    if retrieval_queries:
        st.caption("Found by OR searches: " + " · ".join(retrieval_queries))
    if row.get("hard_mismatch") or row.get("mismatch_reason"):
        st.warning(
            "Mismatch check: "
            + (row.get("mismatch_reason") or
               "A material disease or population mismatch was detected.")
        )
    if row.get("explanation"):
        st.write("**Why this match:** " + row["explanation"])


def is_reviewable(row):
    return bool(
        row["scientific_fit"] >= 15
        or parse_list_value(row.get("matched_dimensions"))
        or parse_list_value(row.get("matched_terms"))
    )


def match_band(score, meaningful_overlap=False):
    if score >= 70:
        return "Strong match"
    if score >= 50:
        return "Possible match"
    if score >= 35:
        return "Adjacent opportunity"
    if score >= 15:
        return "Limited overlap — review"
    if meaningful_overlap:
        return "Very limited overlap — review"
    return "No meaningful overlap"


def render_funding_card(row, rank, key_prefix, show_theme=False):
    reasons = json.loads(row["eligibility_reasons"] or "[]")
    matched = json.loads(row["matched_terms"] or "[]")
    with st.container(border=True):
        left, right = st.columns([4, 1])
        with left:
            st.markdown(f"**{rank}. {row['title']}**")
            st.write(
                f"{row['opportunity_number']} · {row['agency']} · "
                f"Deadline: {row['close_date'] or 'verify'}"
            )
        with right:
            st.metric("Scientific fit", f"{row['scientific_fit']:.1f}/100")
            st.caption(match_band(row["scientific_fit"], is_reviewable(row)))
        st.write(f"**Eligibility:** {row['eligibility_status'].upper()}")
        if reasons:
            st.info("Human review: " + "; ".join(reasons))
        if show_theme:
            st.write(f"**Best matching theme:** {row['theme_name']}")
        st.write("**Exact phrase overlap:** " + (", ".join(matched) or "None"))
        render_score_details(row)
        st.link_button(
            "Open official announcement", row["source_url"],
            key=f"{key_prefix}-{row.get('theme_id', 'best')}-{row['opportunity_number']}",
        )


def exportable_row(row):
    output = dict(row)
    for key in ("eligibility_reasons", "matched_terms", "matched_dimensions",
                "missing_dimensions", "retrieval_queries"):
        output[key] = "; ".join(parse_list_value(row.get(key)))
    return output


def render_results(rows, snapshot_date, minimum_fit):
    st.subheader("Ranked funding matches")
    st.caption(
        f"Funding source: {snapshot_date}. Scientific fit measures research "
        "alignment and is not an application-success probability."
    )
    display_rows = [
        row for row in rows
        if row["scientific_fit"] >= minimum_fit
        and row["eligibility_status"] != "ineligible"
    ]
    review_rows = [
        row for row in rows
        if row["scientific_fit"] < minimum_fit and is_reviewable(row)
        and row["eligibility_status"] != "ineligible"
    ]
    if not display_rows:
        st.warning("No strong match was found above the selected scientific-fit threshold.")
    for rank, row in enumerate(display_rows, start=1):
        render_funding_card(row, rank, "best", show_theme=True)
    if review_rows:
        with st.expander(
                f"Other opportunities to review ({len(review_rows)})", expanded=True):
            st.caption(
                "These opportunities match at least one meaningful research dimension "
                "but remain below the recommendation threshold."
            )
            for rank, row in enumerate(review_rows[:10], start=1):
                render_funding_card(row, rank, "best-review", show_theme=True)

    export_rows = []
    for row in display_rows + review_rows:
        export_rows.append(exportable_row(row))
    if not export_rows:
        return
    import csv
    import io
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=export_rows[0].keys())
    writer.writeheader()
    writer.writerows(export_rows)
    st.download_button(
        "Download results as CSV",
        output.getvalue(),
        file_name="funding_matches.csv",
        mime="text/csv",
    )


def render_theme_results(rows, top_k, minimum_fit):
    st.subheader("Top funding matches by research theme")
    st.caption(
        "Each theme has its own candidate search and ranking. Semantic and LLM "
        "scores appear when OpenAI is enabled."
    )
    themes = []
    for row in rows:
        if row["theme_name"] not in themes:
            themes.append(row["theme_name"])
    selected = []
    for theme_name in themes:
        eligible_rows = [
            row for row in rows
            if row["theme_name"] == theme_name
            and row["scientific_fit"] >= minimum_fit
            and row["eligibility_status"] != "ineligible"
        ][:top_k]
        review_rows = [
            row for row in rows
            if row["theme_name"] == theme_name
            and row["scientific_fit"] < minimum_fit and is_reviewable(row)
            and row["eligibility_status"] != "ineligible"
        ][:top_k]
        st.markdown(f"### {theme_name}")
        if not eligible_rows:
            st.warning("No strong match was found above the selected scientific-fit threshold.")
        for rank, row in enumerate(eligible_rows, start=1):
            selected.append(row)
            render_funding_card(row, rank, "theme")
        if review_rows:
            selected.extend(review_rows)
            with st.expander(
                    f"Other opportunities to review ({len(review_rows)})", expanded=True):
                for rank, row in enumerate(review_rows, start=1):
                    render_funding_card(row, rank, "theme-review")

    if selected:
        import csv
        import io
        export_rows = []
        for row in selected:
            export_rows.append(exportable_row(row))
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=export_rows[0].keys())
        writer.writeheader(); writer.writerows(export_rows)
        st.download_button(
            "Download theme-based results as CSV", output.getvalue(),
            file_name="funding_matches_by_theme.csv", mime="text/csv",
        )


st.set_page_config(page_title="Funding Match", page_icon="🔎", layout="wide")
st.title("Funding Match")
st.write("Enter a researcher profile, rank funding opportunities, and review eligibility separately.")

if st.button("Start a new search", help="Clear the previous profile, themes, and results."):
    st.session_state.clear()
    st.session_state["profile-mode"] = "New researcher"
    st.rerun()

with st.expander("API access", expanded=True):
    st.caption(
        "Keys entered here are used only for this browser session and are not "
        "saved to the repository or database. Leave blank to use server Secrets."
    )
    api_col1, api_col2, api_col3 = st.columns(3)
    with api_col1:
        scopus_key_input = st.text_input(
            "Scopus API key", type="password", key="session-scopus-key")
    with api_col2:
        grants_key_input = st.text_input(
            "Simpler.Grants.gov API key", type="password", key="session-grants-key")
    with api_col3:
        openai_key_input = st.text_input(
            "OpenAI API key", type="password", key="session-openai-key")

scopus_api_key = (scopus_key_input.strip()
                  or server_setting("SCOPUS_API_KEY"))
simpler_grants_api_key = (grants_key_input.strip()
                          or server_setting("SIMPLER_GRANTS_API_KEY"))
openai_api_key = openai_key_input.strip() or server_setting("OPENAI_API_KEY")
openai_model = server_setting("OPENAI_MODEL", "gpt-4o-mini")

profile_mode = st.radio(
    "Profile mode",
    ["Default example profile", "New researcher"],
    horizontal=True,
    key="profile-mode",
    help=("Use the existing demonstration profile, or start with a completely "
          "blank researcher profile."),
)
use_default_profile = profile_mode == "Default example profile"
mode_key = "default" if use_default_profile else "new"
generated_theme_store = st.session_state.get("generated_themes", {})

profile_defaults = {
    "name": "Dajiang Liu" if use_default_profile else "",
    "title": "",
    "organization": "Penn State College of Medicine" if use_default_profile else "",
    "email": "",
    "orcid": "",
    "country": "United States" if use_default_profile else "",
    "scopus_author_id": "55848832700" if use_default_profile else "",
}

# Use a normal container rather than st.form so changing the keyword source
# immediately reruns the page and reveals the correct controls.
with st.container(border=True):
    st.subheader("1. Researcher profile")
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name *", profile_defaults["name"], key=f"profile-name-{mode_key}")
        title = st.text_input("Current title", profile_defaults["title"], key=f"profile-title-{mode_key}")
        organization = st.text_input("Organization", profile_defaults["organization"], key=f"profile-org-{mode_key}")
        email = st.text_input("Email (optional)", profile_defaults["email"], key=f"profile-email-{mode_key}")
        orcid = st.text_input(
            "ORCID (optional)", profile_defaults["orcid"],
            placeholder="0000-0000-0000-0000", key=f"profile-orcid-{mode_key}")
    with col2:
        career_stage = st.selectbox(
            "Career stage",
            ["", "postdoc", "faculty", "student", "staff scientist", "other"],
            index=2 if use_default_profile else 0,
            key=f"profile-career-{mode_key}",
        )
        country = st.text_input("Country", profile_defaults["country"], key=f"profile-country-{mode_key}")
        pi_answer = st.selectbox(
            "Independent PI?", ["", "No", "Yes"],
            index=2 if use_default_profile else 0, key=f"profile-pi-{mode_key}")
        animal_answer = st.selectbox(
            "Works with animal models?", ["", "No", "Yes"],
            index=1 if use_default_profile else 0, key=f"profile-animal-{mode_key}")
        scopus_author_id = st.text_input(
            "Scopus Author ID (optional)", profile_defaults["scopus_author_id"],
            key=f"profile-scopus-{mode_key}")

    scopus_ready = bool(scopus_api_key)
    use_scopus = st.checkbox(
        "Import publications from Scopus",
        value=scopus_ready,
        disabled=not scopus_ready,
        key=f"use-scopus-{mode_key}",
        help=("Uses the session key entered above or SCOPUS_API_KEY from server Secrets."
              if scopus_ready else
              "Enter a Scopus API key above or configure Streamlit Secrets."),
    )
    if not scopus_ready:
        st.caption("Enter a Scopus API key above to enable publication import.")

    st.subheader("2. Choose how to create research themes")
    theme_mode = st.radio(
        "Theme creation method",
        [
            "Generate themes automatically from Scopus",
            "Generate themes using your keywords",
            "Enter themes manually",
        ],
        key=f"theme-mode-v2-{mode_key}",
    )
    automatic_theme_mode = theme_mode.startswith("Generate themes automatically")
    keyword_guided_mode = theme_mode.startswith("Generate themes using")
    manual_theme_mode = theme_mode == "Enter themes manually"
    theme_cache_key = f"{mode_key}:{theme_mode}"
    generated_theme_cache = generated_theme_store.get(theme_cache_key, [])
    theme_revision = st.session_state.get(f"theme_revision_{theme_cache_key}", 0)

    if automatic_theme_mode:
        seed_keywords = None
        st.info(
            "The app will import the selected number of Scopus papers and "
            "automatically generate three research themes from their titles "
            "and abstracts."
        )
    elif keyword_guided_mode:
        st.caption(
            "Enter one keyword group for each theme. The app will use these "
            "groups to assign Scopus papers and then generate three themes."
        )
        keyword_columns = st.columns(3)
        seed_keywords = []
        for seed_index, column in enumerate(keyword_columns, start=1):
            with column:
                keyword_value = st.text_area(
                    f"Theme {seed_index} keywords",
                    key=f"guided-keywords-{mode_key}-{seed_index}",
                    placeholder="keyword 1, keyword 2, keyword 3",
                    height=100,
                )
                seed_keywords.append(split_terms(keyword_value))
    else:
        seed_keywords = None
        st.info(
            "Enter complete research themes below. Scopus publications are "
            "not imported or used in this mode."
        )

    openai_ready = bool(openai_api_key)
    use_llm = st.checkbox(
        ("Use OpenAI for semantic funding matching" if manual_theme_mode else
         "Use OpenAI for theme generation and semantic funding matching"),
        value=openai_ready,
        disabled=not openai_ready,
        key=f"use-openai-{mode_key}",
        help=(
            f"Uses {openai_model} plus text embeddings and falls back to the "
            "transparent local matcher if needed."
            if openai_ready else
            "Enter an OpenAI API key above or configure Streamlit Secrets."
        ),
    )
    if not openai_ready:
        st.caption(
            "Add an OpenAI API key to enable semantic matching; the local "
            "matcher remains available."
        )

    st.subheader("3. Research themes")
    entered_themes = []
    if manual_theme_mode:
        st.caption("The first theme is required. Leave theme 2 or 3 blank if not needed.")
        blank_theme = ("", "", "", "", "", "", "")
        for index, values in enumerate([blank_theme] * 3, start=1):
            with st.expander(f"Theme {index}", expanded=index == 1):
                theme_name = st.text_input(
                    "Theme name", values[0], key=f"name-{mode_key}-{theme_revision}-{index}")
                summary = st.text_area(
                    "Research summary", values[1], key=f"summary-{mode_key}-{theme_revision}-{index}")
                keywords = st.text_area(
                    "Keywords", values[2], key=f"keywords-{mode_key}-{theme_revision}-{index}")
                methods = st.text_input(
                    "Methods", values[3], key=f"methods-{mode_key}-{theme_revision}-{index}")
                diseases = st.text_input(
                    "Diseases/domains", values[4], key=f"diseases-{mode_key}-{theme_revision}-{index}")
                populations = st.text_input(
                    "Populations", values[5], key=f"populations-{mode_key}-{theme_revision}-{index}")
                data_types = st.text_input(
                    "Data types", values[6], key=f"data-{mode_key}-{theme_revision}-{index}")
                entered_themes.append({
                    "name": theme_name.strip(),
                    "summary": summary.strip(),
                    "keywords": split_terms(keywords),
                    "methods": split_terms(methods),
                    "diseases": split_terms(diseases),
                    "populations": split_terms(populations),
                    "data_types": split_terms(data_types),
                })
    elif generated_theme_cache:
        st.caption("Themes generated during the latest search are shown below.")
        st.caption(
            "Theme generation: "
            + generated_theme_cache[0].get("generation_source", "Unknown")
        )
        generation_warning = generated_theme_cache[0].get("generation_warning", "")
        if generation_warning:
            st.warning(generation_warning)
        for index, theme in enumerate(generated_theme_cache[:3], start=1):
            with st.expander(f"Theme {index}: {theme.get('name', '')}", expanded=True):
                st.write(theme.get("summary", ""))
                st.write("**Keywords:** " + ", ".join(theme.get("keywords", [])))
                metric_values = [
                    ("Paper coherence", theme.get("coherence_score")),
                    ("Theme separation", theme.get("separation_score")),
                    ("Cluster stability", theme.get("stability_score")),
                    ("Theme quality", theme.get("quality_score")),
                ]
                available_metrics = [(label, value) for label, value in metric_values
                                     if value is not None]
                if available_metrics:
                    metric_columns = st.columns(len(available_metrics))
                    for metric_column, (label, value) in zip(
                            metric_columns, available_metrics):
                        metric_column.metric(label, f"{float(value):.1f}/100")
                if theme.get("quality_notes"):
                    st.caption("Quality review: " + theme["quality_notes"])
                papers = theme.get("evidence_papers", [])
                st.markdown("**Scopus papers supporting this theme**")
                for paper in papers:
                    date = paper.get("publication_date") or "date unavailable"
                    similarity = paper.get("theme_similarity")
                    similarity_text = (f" · similarity {float(similarity):.1f}/100"
                                       if similarity is not None else "")
                    st.markdown(
                        f"- {paper.get('title', 'Untitled')} ({date}){similarity_text}")
                if not papers:
                    st.caption("No individual evidence paper was recorded for this theme.")
                excluded_papers = theme.get("excluded_papers", [])
                if excluded_papers:
                    st.caption(
                        "Excluded as weak semantic outliers: " + "; ".join(
                            paper.get("title", "Untitled") for paper in excluded_papers))
    else:
        st.caption("The three generated themes and their supporting papers will appear here.")

    st.subheader("4. Result settings")
    settings_col1, settings_col2 = st.columns(2)
    with settings_col1:
        top_k = st.selectbox("Recommendations per theme", [3, 5, 10], index=1)
    with settings_col2:
        minimum_fit = st.selectbox(
            "Minimum scientific fit for recommendations",
            [0, 25, 35, 50, 70], index=2,
            help=("Matches below this threshold are not recommended. "
                  "The default threshold is 35/100."),
        )
    max_publications = st.selectbox("Maximum Scopus publications to import", [10, 20, 50], index=1,
                                    disabled=not use_scopus or manual_theme_mode)
    grants_ready = bool(simpler_grants_api_key)
    use_live_grants = st.checkbox(
        "Search live opportunities from Simpler.Grants.gov",
        value=False, disabled=not grants_ready,
        help=("Uses the session key entered above or SIMPLER_GRANTS_API_KEY from server Secrets."
              if grants_ready else
              "Enter a Simpler.Grants.gov API key above or configure Streamlit Secrets."),
    )
    if use_live_grants:
        st.caption(
            "Live funding search terms will be generated automatically from "
            "the three research themes."
        )

    submitted = st.button(
        "Generate themes and find funding" if not manual_theme_mode
        else "Find funding for the entered themes",
        type="primary", use_container_width=True)

tri_state = {"": None, "No": 0, "Yes": 1}
profile = {
    "name": name.strip(), "email": email.strip(), "title": title.strip(),
    "organization": organization.strip(), "career_stage": career_stage,
    "country": country.strip(), "independent_pi": tri_state[pi_answer],
    "works_with_animals": tri_state[animal_answer],
    "orcid": orcid.strip(), "scopus_author_id": scopus_author_id.strip(),
}
if submitted:
    # Do not leave an earlier result visible when a new submission is invalid.
    st.session_state.pop("match_results", None)
    valid_themes = [theme for theme in entered_themes if theme["name"] and theme["summary"]]
    themes_for_match = valid_themes if manual_theme_mode else []
    if not name.strip():
        st.error("Name is required.")
    elif not manual_theme_mode and not use_scopus:
        st.error("Enable Scopus publication import to generate research themes.")
    elif (not manual_theme_mode and not profile["orcid"]
          and not profile["scopus_author_id"]):
        st.error("Enter an ORCID or Scopus Author ID to import publications.")
    elif keyword_guided_mode and any(not group for group in seed_keywords):
        st.error("Select at least one keyword for each of the three themes.")
    elif keyword_guided_mode and len([item for group in seed_keywords for item in group]) != len({
            item for group in seed_keywords for item in group}):
        st.error("Assign each keyword to only one theme scope to avoid overlapping evidence.")
    elif manual_theme_mode and not valid_themes:
        st.error("Enter at least one research theme with a name and summary.")
    else:
        try:
            progress_message = (
                "Matching the entered themes with funding opportunities..."
                if manual_theme_mode else
                "Importing Scopus papers, generating themes, and matching funding..."
            )
            with st.spinner(progress_message):
                (opportunity_rows, theme_rows, snapshot_date, imported_count,
                 resolved_author_id, opportunity_count, funding_warning,
                 generated_themes, theme_source) = run_match(
                    profile, themes_for_match,
                    use_scopus=use_scopus and not manual_theme_mode,
                    max_publications=max_publications,
                    use_live_grants=use_live_grants,
                    funding_query="",
                    seed_keywords=seed_keywords if keyword_guided_mode else None,
                    scopus_api_key=scopus_api_key,
                    simpler_grants_api_key=simpler_grants_api_key,
                    use_llm=use_llm,
                    openai_api_key=openai_api_key,
                    openai_model=openai_model)
            st.session_state["match_results"] = (
                opportunity_rows, theme_rows, snapshot_date, top_k, minimum_fit,
                imported_count, resolved_author_id, opportunity_count,
                funding_warning, theme_source)
            if not manual_theme_mode and generated_themes:
                updated_store = dict(st.session_state.get("generated_themes", {}))
                updated_store[theme_cache_key] = generated_themes[:3]
                st.session_state["generated_themes"] = updated_store
                st.session_state[f"theme_revision_{theme_cache_key}"] = theme_revision + 1
                st.rerun()
        except Exception as exc:
            message = str(exc)
            if "APIKEY_INVALID" in message or ("HTTP 401" in message and "elsevier" in message):
                st.error(
                    "Scopus rejected SCOPUS_API_KEY. Create or copy a valid key, "
                    "update .env (or Streamlit Secrets), restart the app, and try again."
                )
            else:
                st.exception(exc)

if "match_results" in st.session_state:
    match_payload = st.session_state["match_results"]
    if len(match_payload) == 9:
        match_payload = (*match_payload, "Local theme algorithm")
    (result_rows, theme_rows, result_snapshot, result_top_k, result_minimum,
     imported_count, resolved_author_id, opportunity_count,
     funding_warning, theme_source) = match_payload
    if result_rows:
        if funding_warning:
            st.warning(funding_warning)
        if imported_count:
            st.success(
                f"Imported {imported_count} Scopus publications "
                f"(Author ID: {resolved_author_id})."
            )
        st.caption(f"Theme generation: {theme_source}")
        st.caption(f"Funding source: {result_snapshot} · {opportunity_count} opportunities")
        theme_tab, opportunity_tab = st.tabs([
            "Top matches by research theme", "Best theme for each opportunity"
        ])
        with theme_tab:
            render_theme_results(theme_rows, result_top_k, result_minimum)
        with opportunity_tab:
            render_results(result_rows, result_snapshot, result_minimum)
    else:
        st.warning("No matching opportunities were found in the current snapshot.")
