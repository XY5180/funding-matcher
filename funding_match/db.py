import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS organizations(
  organization_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT,
  parent_organization_id TEXT, raw_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS researchers(
  researcher_id TEXT PRIMARY KEY, pure_person_id TEXT UNIQUE,
  scopus_author_id TEXT, orcid TEXT, name TEXT NOT NULL, email TEXT,
  title TEXT, career_stage TEXT, country TEXT, organization_id TEXT,
  independent_pi INTEGER, works_with_animals INTEGER,
  profile_updated_at TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS research_outputs(
  output_id TEXT PRIMARY KEY, pure_output_id TEXT UNIQUE, scopus_id TEXT,
  eid TEXT, doi TEXT, pmid TEXT, title TEXT NOT NULL, abstract TEXT,
  publication_date TEXT, output_type TEXT, journal TEXT,
  citation_count INTEGER, source_updated_at TEXT, raw_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_doi
  ON research_outputs(doi) WHERE doi IS NOT NULL AND doi != '';
CREATE TABLE IF NOT EXISTS researcher_outputs(
  researcher_id TEXT, output_id TEXT, author_position INTEGER,
  corresponding_author INTEGER, source TEXT,
  PRIMARY KEY(researcher_id, output_id));
CREATE TABLE IF NOT EXISTS projects(
  project_id TEXT PRIMARY KEY, pure_project_id TEXT UNIQUE, title TEXT,
  description TEXT, start_date TEXT, end_date TEXT, status TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS researcher_projects(
  researcher_id TEXT, project_id TEXT, researcher_role TEXT,
  PRIMARY KEY(researcher_id, project_id));
CREATE TABLE IF NOT EXISTS research_themes(
  theme_id TEXT PRIMARY KEY, researcher_id TEXT, theme_name TEXT,
  summary TEXT, keywords TEXT, methods TEXT, diseases TEXT, populations TEXT,
  data_types TEXT, evidence_output_ids TEXT, confidence REAL,
  manually_verified INTEGER DEFAULT 0, generated_at TEXT);
CREATE TABLE IF NOT EXISTS opportunities(
  opportunity_id TEXT PRIMARY KEY, opportunity_number TEXT, title TEXT NOT NULL,
  agency TEXT, status TEXT, post_date TEXT, close_date TEXT,
  description TEXT, applicant_types TEXT, funding_instruments TEXT,
  funding_categories TEXT, award_floor REAL, award_ceiling REAL,
  expected_awards INTEGER, clinical_trial TEXT, animal_required TEXT,
  career_stages TEXT, countries TEXT, institution_types TEXT,
  other_eligibility TEXT, source_url TEXT, source_updated_at TEXT, raw_json TEXT);
CREATE TABLE IF NOT EXISTS matches(
  researcher_id TEXT, opportunity_id TEXT, theme_id TEXT,
  eligibility_status TEXT, eligibility_reasons TEXT,
  scientific_fit REAL, topic_score REAL, method_score REAL,
  domain_score REAL, evidence_score REAL, matched_terms TEXT,
  explanation TEXT, model_version TEXT, scored_at TEXT,
  PRIMARY KEY(researcher_id, opportunity_id));
CREATE TABLE IF NOT EXISTS match_feedback(
  researcher_id TEXT, opportunity_id TEXT, label TEXT, reason TEXT,
  created_at TEXT, PRIMARY KEY(researcher_id, opportunity_id, created_at));
CREATE TABLE IF NOT EXISTS sync_log(
  source TEXT, started_at TEXT, completed_at TEXT, status TEXT,
  records_seen INTEGER, records_written INTEGER, message TEXT);
"""

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def upsert(conn, table, row, conflict):
    clean = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
             for k, v in row.items()}
    columns = list(clean)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in columns if c not in conflict)
    sql = (f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
           f"ON CONFLICT({','.join(conflict)}) DO UPDATE SET {updates}")
    conn.execute(sql, [clean[c] for c in columns])

