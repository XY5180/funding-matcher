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

### Public version without Pure

Pure is optional. The web app can use manually entered profile fields plus
Scopus publications and the funding source. Configure Scopus locally with:

```bash
export SCOPUS_API_KEY="..."
python -m streamlit run app.py
```

Then enter either an ORCID or a Scopus Author ID and select **Import
publications from Scopus**. For Streamlit Community Cloud, add
`SCOPUS_API_KEY="..."` under App settings → Secrets. Do not commit the key to
GitHub. Without the key, the same page continues to work with manually entered
research themes.

Scopus Search is requested in pages of 25 so the import also works with the
basic developer service level. Choosing 50 publications uses multiple pages.
When Scopus import is selected, the web app can generate themes automatically
or use three user-provided keyword groups to guide theme generation.

The web form has two profile modes. **Default example profile** fills the
existing demonstration researcher details, including its Scopus Author ID.
**New researcher** starts with blank profile and theme fields. In either mode,
an ORCID or Scopus Author ID can be used to import publications and generate
themes automatically.

After a researcher is matched, the three generated themes are shown for
review. Each generated theme also lists the Scopus publication title(s)
recorded as evidence for that theme.

The web workflow offers three theme modes. **Generate themes automatically from
Scopus** builds three themes from imported paper titles and abstracts.
**Generate themes using your keywords** accepts three comma-separated keyword
groups and generates one theme per group. **Enter themes manually** skips
Scopus theme generation and matches the entered themes directly.

When `OPENAI_API_KEY` is configured, the first two modes can use `gpt-4o-mini`
to produce evidence-grounded structured themes. Paper title/abstract
embeddings are clustered before the model sees them, so the model names and
explains fixed semantic paper groups rather than choosing papers freely.
Weak semantic outliers may remain unassigned. A second structured model pass
reviews and revises each theme for internal coherence, specificity, scientific
question clarity, evidence strength, logical next direction, funding relevance,
and distinctiveness.

The results show paper-to-theme similarity plus four theme diagnostics:
within-theme paper coherence, separation from the other themes, clustering
stability, and final LLM-reviewed theme quality. Supporting papers cannot be
moved between clusters by the language model.

All three modes can also use `text-embedding-3-small` semantic similarity and
`gpt-4o-mini` funding reranking. The local transparent algorithm remains
available and is used automatically if an OpenAI request fails. OpenAI is
called through HTTPS directly, so the deployment does not depend on a
particular version of the OpenAI Python package.

The default example profile is Dajiang Liu (faculty, Penn State College of
Medicine, United States, independent PI, no animal-model work) with the Scopus
Author ID shown in the web form. **Start a new search** clears the previous
profile, keywords, generated themes, and results, then opens a blank
new-researcher form.

For a public Streamlit deployment, API keys can be supplied either through
Streamlit Secrets or temporarily through the password fields under **API
access**. Session-entered keys are passed in a per-request configuration and
are not written to `.env`, the database, or the repository.
The web app also passes each session key explicitly to its API client, avoiding
deployment-specific environment/config propagation issues.

If `SIMPLER_GRANTS_API_KEY` is also configured, the page can search live
opportunities from Simpler.Grants.gov. Otherwise it uses the included dated
funding snapshot. Live mode builds a separate query and candidate pool for
each research theme (up to 50 candidates per theme); users do not need to
enter another funding-search query. Neither path requires Pure.

If the Simpler.Grants.gov key is invalid or the live service is temporarily
unavailable, the web app displays a warning and automatically falls back to
the included snapshot instead of stopping the whole match.

### Save API keys locally

The simplest safe setup is:

```bash
python setup_keys.py
python -m streamlit run app.py
```

`setup_keys.py` hides keyboard input, saves the API settings in a local `.env` file,
and restricts the file permissions. The application loads `.env`
automatically. `.gitignore` excludes `.env`, so it must never be uploaded to
GitHub. Alternatively, copy `.env.example` to `.env` and edit the values.

For Streamlit Community Cloud, do not upload `.env`. Add the same names under
App settings → Secrets:

```toml
SCOPUS_API_KEY = "..."
SIMPLER_GRANTS_API_KEY = "..."
OPENAI_API_KEY = "..."
OPENAI_MODEL = "gpt-4o-mini"
```

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

- without OpenAI, the existing transparent TF-IDF/topic, method, domain, and
  publication-evidence score is used;
- with OpenAI, the hybrid score emphasizes semantic similarity (45%), retains
  transparent topic similarity (25%), adds a scientific LLM review (20%), and
  uses smaller method, domain, and publication-evidence components;
- the LLM review explicitly checks whether an award supports the research
  rather than only infrastructure or coordinating-center work.

Weights renormalize when a component is unavailable. The CSV includes topic,
method, domain, evidence, semantic, and LLM component scores so each ranking
can be reviewed. Scientific fit measures alignment, not application success.

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
- Do not commit `OPENAI_API_KEY` or put it in browser-visible source code.
- Run API ingestion as a scheduled backend job.
- Do not expose Scopus or Pure credentials in a website form.
- Confirm Elsevier data retention and LLM-processing terms before sending
  Scopus abstracts to an external model.
