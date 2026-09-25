#!/usr/bin/env python3
"""Streamlit front end for the existing funding-match backend."""

import json
import tempfile
from pathlib import Path

import streamlit as st

from funding_match.clients import utcnow
from funding_match.db import connect, upsert
from funding_match.pipeline import match_all


ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "quick_opportunities.json"


def split_terms(value):
    """Accept commas, semicolons, or new lines from a web form."""
    value = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def run_match(profile, themes):
    """Run one isolated match without changing the command-line database."""
    with tempfile.TemporaryDirectory(prefix="funding-match-") as tmp:
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
            "scopus_author_id": None,
            "orcid": None,
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

        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        for opportunity in snapshot["opportunities"]:
            upsert(conn, "opportunities", opportunity, ["opportunity_id"])
        conn.commit()
        match_all(conn)

        opportunity_rows = conn.execute("""
            SELECT m.scientific_fit, m.eligibility_status,
                   m.eligibility_reasons, m.matched_terms, m.explanation,
                   t.theme_name, o.opportunity_number, o.title, o.agency,
                   o.close_date, o.source_url
            FROM matches m
            JOIN opportunities o USING(opportunity_id)
            JOIN research_themes t USING(theme_id)
            WHERE m.researcher_id='web-form-researcher'
            ORDER BY CASE m.eligibility_status
                       WHEN 'eligible' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                     m.scientific_fit DESC
        """).fetchall()
        theme_rows = conn.execute("""
            SELECT tm.scientific_fit, tm.eligibility_status,
                   tm.eligibility_reasons, tm.matched_terms, tm.explanation,
                   t.theme_name, t.theme_id, o.opportunity_number, o.title,
                   o.agency, o.close_date, o.source_url
            FROM theme_matches tm
            JOIN opportunities o USING(opportunity_id)
            JOIN research_themes t USING(theme_id)
            WHERE tm.researcher_id='web-form-researcher'
            ORDER BY t.theme_name,
                     CASE tm.eligibility_status
                       WHEN 'eligible' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                     tm.scientific_fit DESC
        """).fetchall()
        return ([dict(row) for row in opportunity_rows],
                [dict(row) for row in theme_rows], snapshot["snapshot_date"])


def render_results(rows, snapshot_date):
    st.subheader("Ranked funding matches")
    st.caption(
        f"Funding snapshot: {snapshot_date}. Scientific fit is a transparent "
        "text-similarity baseline, not an application-success probability."
    )
    for rank, row in enumerate(rows, start=1):
        reasons = json.loads(row["eligibility_reasons"] or "[]")
        matched = json.loads(row["matched_terms"] or "[]")
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.markdown(f"### {rank}. {row['title']}")
                st.write(
                    f"**{row['opportunity_number']}** · {row['agency']} · "
                    f"Deadline: {row['close_date'] or 'verify'}"
                )
            with right:
                st.metric("Scientific fit", f"{row['scientific_fit']:.1f}/100")
            st.write(f"**Eligibility:** {row['eligibility_status'].upper()}")
            if reasons:
                st.info("Human review: " + "; ".join(reasons))
            st.write(f"**Best matching theme:** {row['theme_name']}")
            st.write("**Matched terms:** " + (", ".join(matched) or "None"))
            st.link_button("Open official announcement", row["source_url"])

    export_rows = []
    for row in rows:
        export_rows.append({
            **row,
            "eligibility_reasons": "; ".join(json.loads(row["eligibility_reasons"] or "[]")),
            "matched_terms": "; ".join(json.loads(row["matched_terms"] or "[]")),
        })
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
        "Each theme is scored against every opportunity. Results below are "
        "ranked independently within each theme."
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
        ][:top_k]
        st.markdown(f"### {theme_name}")
        if not eligible_rows:
            st.warning("No opportunities meet the selected minimum fit.")
            continue
        for rank, row in enumerate(eligible_rows, start=1):
            selected.append(row)
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
                st.write(f"**Eligibility:** {row['eligibility_status'].upper()}")
                if reasons:
                    st.info("Human review: " + "; ".join(reasons))
                st.write("**Matched terms:** " + (", ".join(matched) or "None"))
                st.link_button("Open official announcement", row["source_url"],
                               key=f"theme-link-{row['theme_id']}-{row['opportunity_number']}")

    if selected:
        import csv
        import io
        export_rows = []
        for row in selected:
            export_rows.append({
                **row,
                "eligibility_reasons": "; ".join(json.loads(row["eligibility_reasons"] or "[]")),
                "matched_terms": "; ".join(json.loads(row["matched_terms"] or "[]")),
            })
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

with st.form("researcher-form"):
    st.subheader("1. Researcher profile")
    col1, col2 = st.columns(2)
    with col1:
        name = st.text_input("Name *", "Xin Yuan")
        title = st.text_input("Current title", "Postdoctoral Researcher")
        organization = st.text_input("Organization", "Penn State College of Medicine")
        email = st.text_input("Email (optional)", "")
    with col2:
        career_stage = st.selectbox(
            "Career stage",
            ["postdoc", "faculty", "student", "staff scientist", "other"],
        )
        country = st.text_input("Country", "US")
        pi_answer = st.selectbox("Independent PI?", ["Unknown", "No", "Yes"])
        animal_answer = st.selectbox("Works with animal models?", ["Unknown", "No", "Yes"])

    st.subheader("2. Research themes")
    st.caption("The first theme is required. Leave theme 2 or 3 blank if not needed.")
    defaults = [
        (
            "Interpretable AI for longitudinal electronic health records",
            "Machine learning, language-model embeddings and interpretable representation learning for longitudinal EHR, phecodes, clinical trajectories and cancer risk prediction.",
            "EHR, electronic health records, machine learning, embeddings, cancer risk",
            "machine learning, artificial intelligence, natural language processing",
            "cancer", "patients", "EHR, clinical data",
        ),
        (
            "Statistical genetics and multi-omics",
            "GWAS, statistical genetics, causal inference and integration of multi-omics with phenotypes in population biobanks.",
            "GWAS, statistical genetics, functional genomics, multi-omics, biobank",
            "statistical genetics, causal inference, bioinformatics",
            "complex disease", "population biobank", "genomics, multi-omics, biobank",
        ),
        (
            "AI medical imaging for oral cancer and abdominal MRI",
            "Deep-learning classification and segmentation for oral cancer, potentially malignant disorders and quantitative abdominal MRI.",
            "medical imaging, segmentation, oral cancer, MRI, deep learning",
            "deep learning, imaging, segmentation",
            "oral cancer", "patients", "imaging, MRI",
        ),
    ]
    entered_themes = []
    for index, values in enumerate(defaults, start=1):
        with st.expander(f"Theme {index}", expanded=index == 1):
            theme_name = st.text_input("Theme name", values[0], key=f"name-{index}")
            summary = st.text_area("Research summary", values[1], key=f"summary-{index}")
            keywords = st.text_area("Keywords", values[2], key=f"keywords-{index}")
            methods = st.text_input("Methods", values[3], key=f"methods-{index}")
            diseases = st.text_input("Diseases/domains", values[4], key=f"diseases-{index}")
            populations = st.text_input("Populations", values[5], key=f"populations-{index}")
            data_types = st.text_input("Data types", values[6], key=f"data-{index}")
            entered_themes.append({
                "name": theme_name.strip(),
                "summary": summary.strip(),
                "keywords": split_terms(keywords),
                "methods": split_terms(methods),
                "diseases": split_terms(diseases),
                "populations": split_terms(populations),
                "data_types": split_terms(data_types),
            })

    st.subheader("3. Result settings")
    settings_col1, settings_col2 = st.columns(2)
    with settings_col1:
        top_k = st.selectbox("Recommendations per theme", [3, 5, 10], index=1)
    with settings_col2:
        minimum_fit = st.selectbox("Minimum scientific fit", [0, 10, 20, 30, 40], index=0)

    submitted = st.form_submit_button("Find funding opportunities", type="primary")

if submitted:
    valid_themes = [theme for theme in entered_themes if theme["name"] and theme["summary"]]
    if not name.strip():
        st.error("Name is required.")
    elif not valid_themes:
        st.error("Enter at least one research theme with a name and summary.")
    else:
        tri_state = {"Unknown": None, "No": 0, "Yes": 1}
        profile = {
            "name": name.strip(), "email": email.strip(), "title": title.strip(),
            "organization": organization.strip(), "career_stage": career_stage,
            "country": country.strip(), "independent_pi": tri_state[pi_answer],
            "works_with_animals": tri_state[animal_answer],
        }
        try:
            opportunity_rows, theme_rows, snapshot_date = run_match(profile, valid_themes)
            st.session_state["match_results"] = (
                opportunity_rows, theme_rows, snapshot_date, top_k, minimum_fit)
        except Exception as exc:
            st.exception(exc)

if "match_results" in st.session_state:
    result_rows, theme_rows, result_snapshot, result_top_k, result_minimum = st.session_state["match_results"]
    if result_rows:
        st.caption(f"Funding snapshot: {result_snapshot}")
        theme_tab, opportunity_tab = st.tabs([
            "Top matches by research theme", "Best theme for each opportunity"
        ])
        with theme_tab:
            render_theme_results(theme_rows, result_top_k, result_minimum)
        with opportunity_tab:
            render_results(result_rows, result_snapshot)
    else:
        st.warning("No matching opportunities were found in the current snapshot.")
