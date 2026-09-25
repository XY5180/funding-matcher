# Funding Matcher

Runnable backend prototype for:

1. syncing researcher data from Pure;
2. enriching publications with Scopus;
3. syncing posted and forecasted opportunities from Simpler.Grants.gov;
4. building multiple research themes per researcher;
5. separating eligibility review from scientific fit;
6. exporting ranked, explainable matches.

The project uses Python's standard library and SQLite. API credentials are read
from environment variables and are never written to the database.

## Quick start

```bash
python run_pipeline.py demo --reset
```

This creates:

- `data/funding_match.db`
- `output/demo_matches.csv`

## Key-free preview using Xin Yuan's profile

No API key is needed. The profile and a small, dated opportunity snapshot are
plain JSON files so they can be reviewed before connecting institutional data.

```bash
python run_pipeline.py quick-xin-demo --reset
```

Open `output/xin_quick_report.html` for a readable ranked report, or use
`output/xin_quick_matches.csv` for the best theme per opportunity and
`output/xin_theme_matches.csv` for multiple opportunities per theme. Edit `quick_profile.json` to
correct the profile. `quick_opportunities.json` is a 2026-09-25 snapshot from
official announcement pages and must be re-checked before applying.

Later, API sync replaces these two manual inputs; the matching and export
stages stay the same.

## Web interface

Install the small web dependency and start the interface:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The browser form supports up to three research themes, displays scientific fit
separately from eligibility review, links to each official announcement, and
downloads the results as CSV. It provides both a theme-centric Top-K view and
an opportunity-centric best-theme view. It uses the same `funding_match/` backend as
`run_pipeline.py`; neither entry point replaces the other.

The command-line version can export the Top 5 opportunities for every theme:

```bash
python run_pipeline.py export-theme-matches \
  --top-k 5 --minimum-fit 0 --output output/theme_matches.csv
```

## Real API configuration

```bash
cp config.example.json config.json
export PURE_API_KEY="..."
export SCOPUS_API_KEY="..."
export SIMPLER_GRANTS_API_KEY="..."
```

Edit `config.json`, especially `pure.base_url`. Pure installations differ by
version, so endpoint paths are configurable.

The `https://YOUR-INSTITUTION.example/...` value is a placeholder and cannot
be contacted. Do not run `sync-pure` until the internal Pure team supplies the
real base URL and confirms the API version. The demo and Grants.gov sync can
run independently while Pure access is pending.

Initialize and sync:

```bash
python run_pipeline.py init-db
python run_pipeline.py --config config.json sync-pure
python run_pipeline.py --config config.json sync-scopus
python run_pipeline.py --config config.json sync-grants --query "health OR biomedical"
python run_pipeline.py build-profiles
python run_pipeline.py match
python run_pipeline.py export --output output/matches.csv
```

The global `--config` option must appear before the command.

Import human review labels and calculate evaluation metrics:

```bash
python run_pipeline.py import-feedback --input feedback_template.csv
python run_pipeline.py evaluate --k 10
```

Supported labels are `strong_match`, `possible_match`, `not_relevant`,
`not_eligible`, and `unsure`.

## Required API access

Pure read-only categories:

- Persons
- Research outputs
- Organizations
- Projects

Scopus APIs:

- Author Search
- Author Retrieval
- Scopus Search
- Abstract Retrieval

Simpler.Grants.gov:

- `POST /v1/opportunities/search`

## Matching design

Eligibility is reported independently:

- `eligible`: encoded requirements pass;
- `review`: no conflict found, but one or more requirements need verification;
- `ineligible`: an encoded hard requirement fails.

Scientific fit is a 0–100 score:

- research theme text similarity: 45%;
- topic coverage: 20%;
- method coverage: 15%;
- disease/population coverage: 10%;
- prior output evidence: 10%.

Weights renormalize when an opportunity lacks a component. The implementation
is a transparent baseline. Replace `weighted_fit()` with an embedding model
after building a human-reviewed evaluation set.

## Pure endpoint assumptions

Defaults follow common Pure API naming:

- `/persons`
- `/research-outputs`
- `/organisational-units`
- `/projects`

Your Pure team should supply the exact base URL, version, pagination format,
and sample responses. Update only `config.json` or the normalization helpers
in `funding_match/clients.py`.

## Security and data handling

- Keep API keys in environment variables or a server secret manager.
- Run API ingestion as a scheduled backend job.
- Do not expose Scopus or Pure credentials in a website form.
- Confirm Elsevier data retention and LLM-processing terms before sending
  Scopus abstracts to an external model.
