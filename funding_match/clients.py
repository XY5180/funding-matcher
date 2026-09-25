import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def request_json(url, headers=None, body=None, retries=3):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if body is not None else "GET")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                detail = exc.read(500).decode(errors="replace")
                raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Cannot reach {url}: {exc}") from exc
        time.sleep(2 ** attempt)

def need_key(config, section):
    env_name = config[section]["api_key_env"]
    value = os.environ.get(env_name, "").strip()
    if not value:
        raise RuntimeError(f"Set environment variable {env_name}")
    return value

class PureClient:
    """Read-only Pure client; response normalization tolerates common API versions."""
    def __init__(self, config):
        self.cfg = config["pure"]
        base_url = str(self.cfg.get("base_url", "")).strip()
        parsed = urllib.parse.urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        if not base_url or "YOUR-INSTITUTION" in base_url or hostname.endswith(".example"):
            raise RuntimeError(
                "Pure is not configured. Copy config.example.json to config.json, "
                "replace pure.base_url with the real URL supplied by your internal "
                "Pure team, confirm the endpoint paths, set PURE_API_KEY, then run: "
                "python run_pipeline.py --config config.json sync-pure"
            )
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(
                f"Invalid pure.base_url: {base_url!r}. Use a complete URL beginning "
                "with https://"
            )
        self.key = need_key(config, "pure")
        self.headers = {"api-key": self.key, "Accept": "application/json"}

    def records(self, kind):
        endpoint = self.cfg["endpoints"][kind]
        page_size = int(self.cfg.get("page_size", 100))
        offset = 0
        while True:
            query = urllib.parse.urlencode({"size": page_size, "offset": offset})
            payload = request_json(self.cfg["base_url"].rstrip("/") + endpoint + "?" + query,
                                   self.headers)
            rows = payload.get("items") or payload.get("results") or payload.get("content") or []
            if not isinstance(rows, list):
                raise ValueError(f"Unexpected Pure {kind} response")
            yield from rows
            count = payload.get("count") or payload.get("total") or payload.get("totalItems")
            if len(rows) < page_size or (count is not None and offset + len(rows) >= int(count)):
                break
            offset += len(rows)

class ScopusClient:
    def __init__(self, config):
        self.cfg = config["scopus"]
        self.key = need_key(config, "scopus")
        self.headers = {"X-ELS-APIKey": self.key, "Accept": "application/json"}
        token = os.environ.get(self.cfg.get("institution_token_env", ""), "").strip()
        if token:
            self.headers["X-ELS-Insttoken"] = token
        self.base = self.cfg.get("base_url", "https://api.elsevier.com/content").rstrip("/")

    def author_search(self, query):
        url = self.base + "/search/author?" + urllib.parse.urlencode(
            {"query": query, "count": 25, "view": "STANDARD"})
        return request_json(url, self.headers)

    def author(self, author_id):
        return request_json(self.base + f"/author/author_id/{author_id}?view=ENHANCED",
                            self.headers)

    def publications(self, author_id):
        start = 0
        year = self.cfg.get("publication_start_year")
        query = f"AU-ID({author_id})" + (f" AND PUBYEAR AFT {int(year)-1}" if year else "")
        while True:
            url = self.base + "/search/scopus?" + urllib.parse.urlencode(
                {"query": query, "start": start, "count": 200, "view": "STANDARD",
                 "sort": "-coverDate"})
            payload = request_json(url, self.headers)
            block = payload.get("search-results", {})
            rows = block.get("entry", [])
            yield from rows
            total = int(block.get("opensearch:totalResults", 0))
            start += len(rows)
            if not rows or start >= total:
                break

    def abstract(self, eid):
        return request_json(self.base + f"/abstract/eid/{urllib.parse.quote(eid)}?view=FULL",
                            self.headers)

class SimplerGrantsClient:
    def __init__(self, config):
        self.cfg = config["simpler_grants"]
        self.key = need_key(config, "simpler_grants")
        self.headers = {"X-API-Key": self.key, "Content-Type": "application/json"}

    def opportunities(self, query=""):
        size = int(self.cfg.get("page_size", 100))
        max_pages = int(self.cfg.get("max_pages", 10))
        statuses = self.cfg.get("statuses", ["posted", "forecasted"])
        url = self.cfg["base_url"].rstrip("/") + "/v1/opportunities/search"
        for page in range(1, max_pages + 1):
            body = {
                "filters": {"opportunity_status": {"one_of": statuses}},
                "pagination": {
                    "page_offset": page, "page_size": size,
                    "sort_order": [{"order_by": "post_date", "sort_direction": "descending"}]
                }
            }
            if query:
                body["query"] = query[:100]
                body["query_operator"] = "OR"
            payload = request_json(url, self.headers, body)
            rows = payload.get("data", [])
            yield from rows
            pages = int(payload.get("pagination_info", {}).get("total_pages", page))
            if not rows or page >= pages:
                break
            time.sleep(1.05)
