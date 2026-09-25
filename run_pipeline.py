#!/usr/bin/env python3
import argparse
import html
import json
import shutil
from pathlib import Path

from funding_match.db import connect, upsert
from funding_match.pipeline import (build_profiles, export_matches, export_theme_matches, match_all,
                                    sync_grants, sync_pure, sync_scopus,
                                    import_feedback, evaluation_report)
from funding_match.clients import utcnow

ROOT = Path(__file__).resolve().parent

def load_config(path):
    source = Path(path) if path else ROOT / "config.example.json"
    cfg = json.loads(source.read_text())
    db = Path(cfg["database"])
    cfg["database"] = str(db if db.is_absolute() else ROOT / db)
    return cfg

def seed_demo(conn):
    org = {"organization_id":"ORG1","name":"Example College of Medicine",
           "type":"college","parent_organization_id":None,"raw_json":{},"updated_at":utcnow()}
    upsert(conn,"organizations",org,["organization_id"])
    people = [
      {"researcher_id":"R1","pure_person_id":"P1","scopus_author_id":None,"orcid":None,
       "name":"Demo EHR Researcher","email":"demo1@example.org","title":"Postdoctoral Scholar",
       "career_stage":"postdoc","country":"US","organization_id":"ORG1","independent_pi":0,
       "works_with_animals":0,"profile_updated_at":utcnow(),"raw_json":{"researchInterests":"Interpretable machine learning with longitudinal electronic health records for cancer risk prediction"}},
      {"researcher_id":"R2","pure_person_id":"P2","scopus_author_id":None,"orcid":None,
       "name":"Demo Genomics Faculty","email":"demo2@example.org","title":"Associate Professor",
       "career_stage":"faculty","country":"US","organization_id":"ORG1","independent_pi":1,
       "works_with_animals":1,"profile_updated_at":utcnow(),"raw_json":{"researchInterests":"Statistical genetics, functional genomics, GWAS and multi-omics for complex disease"}}
    ]
    outputs = [
      ("O1","R1","Interpretable patient embeddings from longitudinal electronic health records","Machine learning models characterize cancer trajectories and clinical risk from EHR data."),
      ("O2","R1","Deep learning for cancer risk prediction","Artificial intelligence and electronic health records support early cancer detection."),
      ("O3","R2","Statistical genetics of complex human disease","GWAS and functional genomics identify mechanisms of complex traits."),
      ("O4","R2","Multi-omics integration in population biobanks","Genomics and causal inference integrate sequencing and biobank data.")
    ]
    for p in people: upsert(conn,"researchers",p,["researcher_id"])
    for oid,rid,title,abstract in outputs:
        upsert(conn,"research_outputs",{"output_id":oid,"pure_output_id":oid,"scopus_id":None,
          "eid":None,"doi":"","pmid":None,"title":title,"abstract":abstract,
          "publication_date":"2026-01-01","output_type":"article","journal":"Demo Journal",
          "citation_count":0,"source_updated_at":utcnow(),"raw_json":{}},["output_id"])
        upsert(conn,"researcher_outputs",{"researcher_id":rid,"output_id":oid,
          "author_position":1,"corresponding_author":1,"source":"demo"},
          ["researcher_id","output_id"])
    grants = [
      {"opportunity_id":"G1","opportunity_number":"DEMO-R21-1","title":"Computational methods for cancer EHR research",
       "agency":"NIH","status":"posted","post_date":"2026-08-01","close_date":"2027-06-01",
       "description":"Develop interpretable machine learning and artificial intelligence methods using longitudinal electronic health records for cancer risk prediction.",
       "applicant_types":["higher_education"],"funding_instruments":["grant"],"funding_categories":["health"],
       "award_floor":100000,"award_ceiling":500000,"expected_awards":5,"clinical_trial":"optional",
       "animal_required":"false","career_stages":["postdoc","faculty"],"countries":["US"],
       "institution_types":["higher_education"],"other_eligibility":"Verify PI appointment in full announcement",
       "source_url":"https://example.org/G1","source_updated_at":utcnow(),"raw_json":{}},
      {"opportunity_id":"G2","opportunity_number":"DEMO-R01-2","title":"Functional genomics of complex traits",
       "agency":"NIH","status":"posted","post_date":"2026-08-15","close_date":"2027-09-01",
       "description":"Statistical genetics, GWAS, sequencing and multi-omics studies of complex human disease.",
       "applicant_types":["higher_education"],"funding_instruments":["grant"],"funding_categories":["health"],
       "award_floor":250000,"award_ceiling":1000000,"expected_awards":8,"clinical_trial":"not_allowed",
       "animal_required":"false","career_stages":["faculty"],"countries":["US"],
       "institution_types":["higher_education"],"other_eligibility":"",
       "source_url":"https://example.org/G2","source_updated_at":utcnow(),"raw_json":{}}
    ]
    for g in grants: upsert(conn,"opportunities",g,["opportunity_id"])
    conn.commit()

def seed_quick_profile(conn, profile_path, opportunities_path):
    """Load one editable researcher profile and a key-free opportunity snapshot."""
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    opportunities = json.loads(Path(opportunities_path).read_text(encoding="utf-8"))
    researcher = profile["researcher"]
    upsert(conn, "organizations", profile["organization"], ["organization_id"])
    upsert(conn, "researchers", researcher, ["researcher_id"])
    conn.execute("DELETE FROM research_themes WHERE researcher_id=?", (researcher["researcher_id"],))
    for theme in profile["themes"]:
        theme = {**theme, "researcher_id": researcher["researcher_id"],
                 "manually_verified": 1, "generated_at": utcnow()}
        upsert(conn, "research_themes", theme, ["theme_id"])
    for opportunity in opportunities["opportunities"]:
        upsert(conn, "opportunities", opportunity, ["opportunity_id"])
    conn.commit()
    return len(profile["themes"]), len(opportunities["opportunities"])

def export_quick_html(conn, output):
    rows = conn.execute("""SELECT r.name,m.*,o.opportunity_number,o.title,o.agency,
                          o.close_date,o.source_url
                          FROM matches m JOIN researchers r USING(researcher_id)
                          JOIN opportunities o USING(opportunity_id)
                          WHERE r.researcher_id='xin-yuan-quick-demo'
                          ORDER BY CASE m.eligibility_status WHEN 'eligible' THEN 0
                          WHEN 'review' THEN 1 ELSE 2 END, m.scientific_fit DESC""").fetchall()
    cards = []
    for row in rows:
        reasons = "; ".join(json.loads(row["eligibility_reasons"] or "[]"))
        terms = ", ".join(json.loads(row["matched_terms"] or "[]"))
        cards.append(f"""<article><div class='score'>{row['scientific_fit']:.1f}</div>
        <div><h2>{html.escape(row['title'])}</h2>
        <p><b>{html.escape(row['opportunity_number'] or '')}</b> · {html.escape(row['agency'] or '')} · deadline {html.escape(row['close_date'] or 'verify')}</p>
        <p><span class='tag'>{html.escape(row['eligibility_status'])}</span> {html.escape(reasons)}</p>
        <p><b>Matched terms:</b> {html.escape(terms or 'none')}</p>
        <p>{html.escape(row['explanation'])}</p>
        <a href='{html.escape(row['source_url'] or '')}'>Open official announcement</a></div></article>""")
    document = f"""<!doctype html><meta charset='utf-8'><title>Xin Yuan funding matches</title>
    <style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#17202a}}
    header{{background:#eef6ff;padding:24px;border-radius:16px}}article{{display:grid;grid-template-columns:90px 1fr;gap:20px;padding:22px 0;border-bottom:1px solid #ddd}}
    .score{{font-size:34px;font-weight:800;color:#1261a0}}.tag{{background:#fff1c7;padding:4px 9px;border-radius:12px}}a{{color:#1261a0}}</style>
    <header><h1>Key-free funding match preview</h1><p>Researcher: Xin Yuan · snapshot: 2026-09-25</p>
    <p>Scientific fit is a transparent text-similarity baseline. “review” means the full PI/citizenship/appointment rules still require human verification.</p></header>{''.join(cards)}"""
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return len(rows)

def main():
    parser=argparse.ArgumentParser(description="Funding match data and ranking pipeline")
    parser.add_argument("--config",default=None)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("init-db")
    demo=sub.add_parser("demo"); demo.add_argument("--reset",action="store_true")
    quick=sub.add_parser("quick-xin-demo"); quick.add_argument("--reset",action="store_true")
    sub.add_parser("sync-pure")
    sub.add_parser("sync-scopus")
    grants=sub.add_parser("sync-grants"); grants.add_argument("--query",default="")
    sub.add_parser("build-profiles")
    sub.add_parser("match")
    export=sub.add_parser("export"); export.add_argument("--output",default="output/matches.csv")
    theme_export=sub.add_parser("export-theme-matches")
    theme_export.add_argument("--output",default="output/theme_matches.csv")
    theme_export.add_argument("--top-k",type=int,default=5)
    theme_export.add_argument("--minimum-fit",type=float,default=0)
    feedback=sub.add_parser("import-feedback"); feedback.add_argument("--input",required=True)
    evaluate=sub.add_parser("evaluate"); evaluate.add_argument("--k",type=int,default=10)
    args=parser.parse_args()
    cfg=load_config(args.config)
    db=Path(cfg["database"])
    if args.command in {"demo","quick-xin-demo"} and args.reset and db.exists(): db.unlink()
    conn=connect(db)
    try:
        if args.command=="init-db": result=f"Initialized {db}"
        elif args.command=="demo":
            seed_demo(conn)
            themes=build_profiles(conn); pairs=match_all(conn)
            out=ROOT/"output/demo_matches.csv"; export_matches(conn,out)
            result=f"Demo complete: {themes} themes, {pairs} pairs, {out}"
        elif args.command=="quick-xin-demo":
            themes, opportunities = seed_quick_profile(
                conn, ROOT/"quick_profile.json", ROOT/"quick_opportunities.json")
            pairs=match_all(conn)
            csv_out=ROOT/"output/xin_quick_matches.csv"
            theme_csv_out=ROOT/"output/xin_theme_matches.csv"
            html_out=ROOT/"output/xin_quick_report.html"
            export_matches(conn,csv_out)
            export_theme_matches(conn,theme_csv_out,top_k=5,minimum_fit=0)
            export_quick_html(conn,html_out)
            result=(f"Quick demo complete: {themes} verified themes, {opportunities} opportunities, "
                    f"{pairs} opportunity-level matches\nBest-theme CSV: {csv_out}"
                    f"\nTheme Top-K CSV: {theme_csv_out}\nReport: {html_out}")
        elif args.command=="sync-pure": result=sync_pure(conn,cfg)
        elif args.command=="sync-scopus": result=f"{sync_scopus(conn,cfg)} publication links"
        elif args.command=="sync-grants": result=f"{sync_grants(conn,cfg,args.query)} opportunities"
        elif args.command=="build-profiles": result=f"{build_profiles(conn)} themes"
        elif args.command=="match": result=f"{match_all(conn)} pairs"
        elif args.command=="import-feedback":
            result=f"{import_feedback(conn,args.input)} feedback labels imported"
        elif args.command=="evaluate":
            result=json.dumps(evaluation_report(conn,args.k),indent=2)
        elif args.command=="export-theme-matches":
            target=Path(args.output)
            if not target.is_absolute(): target=ROOT/target
            result=(f"{export_theme_matches(conn,target,args.top_k,args.minimum_fit)} rows "
                    f"written to {target}")
        else:
            target=Path(args.output)
            if not target.is_absolute(): target=ROOT/target
            result=f"{export_matches(conn,target)} rows written to {target}"
    except (RuntimeError, ValueError) as exc:
        parser.exit(2, f"Configuration/API error: {exc}\n")
    print(result)

if __name__=="__main__":
    main()
