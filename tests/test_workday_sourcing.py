"""Workday sourcing-provider tests: URL/slug parsing, the offset-clamp paging
rules, and the normalized row shape the store keys on.

Standalone on purpose — the project ships no test runner, so
`python tests/test_workday_sourcing.py` runs the whole file; the plain `test_*`
functions also collect under pytest if one is ever added.

Nothing here touches the network: `list_jobs` is exercised against a fake async
client whose `post` returns canned pages.
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store                      # noqa: E402
from src.providers import workday          # noqa: E402

COMPANY = {"name": "Workday", "ats": "workday", "slug": "workday:wd5:Workday"}

LIST_ROW = {
    "title": "Sr Product Manager - VNDLY",
    "externalPath": "/job/USA-CA-Pleasanton/Sr-Product-Manager---VNDLY_JR-0108551",
    "locationsText": "2 Locations",
    "postedOn": "Posted 30+ Days Ago",
    "bulletFields": ["JR-0108551"],
}

DETAIL = {
    "id": "b1e820ccaed010011fb3a67b172b0000",
    "title": "Sr Product Manager - VNDLY",
    "jobDescription": "<p>Build <b>great</b> products.</p>",
    "location": "USA, CA, Pleasanton",
    "additionalLocations": ["Canada, BC, Vancouver"],
    "postedOn": "Posted 30+ Days Ago",
    "startDate": "2026-09-09",
    "timeType": "Full Time",
    "jobReqId": "JR-0108551",
    "externalUrl": "https://workday.wd5.myworkdayjobs.com/Workday/job/"
                   "USA-CA-Pleasanton/Sr-Product-Manager---VNDLY_JR-0108551",
}

ROW_KEYS = {"company", "ats", "slug", "job_id", "title", "location", "remote",
            "salary_min", "salary_max", "posted", "url", "description"}


# --------------------------------------------------------------------------- #
# fake client (no network)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Serves canned list pages by offset (a separate set when appliedFacets is
    non-empty) and a canned detail body; records every request."""

    total = None
    facets = []
    family_pages = None       # {facet id: pages} for partitioned fetches
    family_totals = None      # {facet id: total}
    detail = None

    def __init__(self, pages, status_code=200):
        self.pages = pages              # {offset: [postings]} or a list of pages
        self.status_code = status_code
        self.calls = []                 # (url, body) per list POST
        self.gets = []                  # detail GET urls

    @staticmethod
    def _page_at(pages, offset):
        if isinstance(pages, dict):
            return pages.get(offset, [])
        idx = offset // 20
        return pages[idx] if idx < len(pages) else []

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        fids = ((json or {}).get("appliedFacets") or {}).get("jobFamilyGroup") or []
        offset = (json or {}).get("offset", 0)
        if fids:
            page = self._page_at((self.family_pages or {}).get(fids[0], []), offset)
            total = (self.family_totals or {}).get(fids[0])
        else:
            page = self._page_at(self.pages, offset)
            total = self.total
        body = {"jobPostings": page, "userAuthenticated": False}
        if offset == 0:
            body["total"] = total
            body["facets"] = self.facets
        else:
            body["total"] = 0           # deep offsets lie — must be ignored
        return _Resp(body, self.status_code)

    async def get(self, url, headers=None):
        self.gets.append(url)
        return _Resp({"jobPostingInfo": self.detail} if self.detail else {}, 200)

    def faceted_calls(self):
        return [c for c in self.calls if (c[1] or {}).get("appliedFacets")]


def _page(n, start=0, title="Product Manager"):
    return [{"title": "%s %d" % (title, i),
             "externalPath": "/job/Loc/Role-%d_JR%d" % (i, i),
             "locationsText": "USA, CA, Pleasanton",
             "bulletFields": ["JR%d" % i]}
            for i in range(start, start + n)]


def _facets(*families):
    """A jobFamilyGroup facet from (descriptor, id, count) triples."""
    return [{"facetParameter": "remoteType", "descriptor": "Remote Type",
             "values": [{"descriptor": "Flex", "id": "rt1", "count": 9}]},
            {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
             "values": [{"descriptor": d, "id": i, "count": c}
                        for d, i, c in families]}]


# --------------------------------------------------------------------------- #
# parse_workday_url
# --------------------------------------------------------------------------- #
def test_parse_board_root():
    got = workday.parse_workday_url(
        "https://workday.wd5.myworkdayjobs.com/Workday")
    assert got == {"tenant": "workday", "shard": "wd5", "site": "Workday",
                   "external_path": None,
                   "host": "workday.wd5.myworkdayjobs.com"}, got


def test_parse_locale_prefix_is_skipped():
    for url, site in [
        ("https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One", "Capital_One"),
        ("https://acme.wd1.myworkdayjobs.com/de-DE/Careers", "Careers"),
        ("https://acme.wd1.myworkdayjobs.com/en/Careers", "Careers"),
    ]:
        got = workday.parse_workday_url(url)
        assert got and got["site"] == site, url


def test_parse_detail_path():
    got = workday.parse_workday_url(
        "https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/"
        "USA-CA-Pleasanton/Sr-Product-Manager---VNDLY_JR-0108551")
    assert got["tenant"] == "workday" and got["shard"] == "wd5"
    assert got["site"] == "Workday", got["site"]
    assert got["external_path"] == \
        "/job/USA-CA-Pleasanton/Sr-Product-Manager---VNDLY_JR-0108551", got


def test_parse_api_path():
    got = workday.parse_workday_url(
        "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs")
    assert got["site"] == "Workday" and got["external_path"] is None, got


def test_parse_non_workday_url_is_none():
    for url in ["https://boards.greenhouse.io/acme",
                "https://jobs.lever.co/acme",
                "https://myworkdayjobs.com/Careers",          # no tenant/shard
                "https://acme.myworkdayjobs.com/Careers",     # no shard label
                ""]:
        assert workday.parse_workday_url(url) is None, url


def test_parse_host_with_no_site():
    got = workday.parse_workday_url("https://nvidia.wd5.myworkdayjobs.com/")
    assert got["tenant"] == "nvidia" and got["shard"] == "wd5"
    assert got["site"] == "" and got["external_path"] is None, got


# --------------------------------------------------------------------------- #
# slug round-trip
# --------------------------------------------------------------------------- #
def test_slug_round_trip():
    slug = workday.make_slug("workday", "wd5", "Workday")
    assert slug == "workday:wd5:Workday", slug
    assert workday.split_slug(slug) == ("workday", "wd5", "Workday")
    assert workday.board_url(slug) == \
        "https://workday.wd5.myworkdayjobs.com/Workday"


def test_split_slug_rejects_junk():
    for junk in ["", "workday", "workday:wd5", "workday:wd5:Workday:extra",
                 "workday::Workday", ":wd5:Workday", "workday:wd5: "]:
        try:
            workday.split_slug(junk)
        except ValueError:
            continue
        raise AssertionError("split_slug accepted %r" % junk)


# --------------------------------------------------------------------------- #
# status taxonomy
# --------------------------------------------------------------------------- #
def test_classify_status_covers_the_taxonomy():
    expected = {200: "ok", 404: "wrong_site", 422: "wrong_shard",
                401: "internal", 403: "blocked", 410: "retired",
                502: "tenant_casing"}
    for code, reason in expected.items():
        assert workday.classify_status(code) == reason, code
    assert workday.classify_status(429) == "http_429"
    assert workday.classify_status(500) == "http_500"


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #
def test_paginate_done_normal_end():
    # a full page that reaches `total`
    assert workday.paginate_done(_page(20), 380, 400, "/job/x") is True
    # a full page with rows still to come
    assert workday.paginate_done(_page(20), 0, 400, "/job/x") is False


def test_paginate_done_short_and_empty_pages():
    assert workday.paginate_done([], 40, 400, "/job/x") is True
    assert workday.paginate_done(_page(7), 40, None, "/job/x") is True


def test_paginate_done_offset_clamp():
    # offset 1980 + a full page hits the 2000 wall even if `total` says more
    assert workday.paginate_done(_page(20), 1980, 2614, "/job/x") is True


def test_paginate_done_detects_page0_replay():
    first = "/job/Loc/Product-Manager-0_JR0"
    assert workday.paginate_done(_page(20), 2000, None, first) is True
    # ...but the same check must not fire on page 0 itself
    assert workday.paginate_done(_page(20), 0, 400, first) is False


# --------------------------------------------------------------------------- #
# job_id_for
# --------------------------------------------------------------------------- #
def test_job_id_from_bullet_fields():
    assert workday.job_id_for(LIST_ROW) == "JR-0108551"


def test_job_id_falls_back_to_external_path_tail():
    item = {**LIST_ROW, "bulletFields": []}
    assert workday.job_id_for(item) == "Sr-Product-Manager---VNDLY_JR-0108551"
    item = {**LIST_ROW, "bulletFields": ["Posted Today"]}   # not a req id
    assert workday.job_id_for(item) == "Sr-Product-Manager---VNDLY_JR-0108551"


def test_job_id_keeps_hyphenated_req_ids():
    for req in ["R-50862-1", "2026-0013526", "JR2025072"]:
        assert workday.job_id_for({**LIST_ROW, "bulletFields": [req]}) == req


def test_job_id_empty_when_nothing_identifies_the_row():
    assert workday.job_id_for({"title": "x"}) == ""


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
def test_normalize_hydrated_row():
    row = workday.normalize(COMPANY, LIST_ROW, DETAIL)
    assert set(row) == ROW_KEYS, set(row) ^ ROW_KEYS
    assert row["company"] == "Workday" and row["ats"] == "workday"
    assert row["slug"] == "workday:wd5:Workday"
    assert row["job_id"] == "JR-0108551"
    assert row["title"] == "Sr Product Manager - VNDLY"
    # every city, joined — locations.py has to see them all
    assert row["location"] == "USA, CA, Pleasanton; Canada, BC, Vancouver", \
        row["location"]
    assert row["remote"] is False
    assert row["salary_min"] is None and row["salary_max"] is None
    # startDate, never the localized postedOn string
    assert row["posted"] == "2026-09-09", row["posted"]
    assert row["url"] == DETAIL["externalUrl"]
    assert row["description"] == "Build great products.", row["description"]


def test_normalize_accepts_the_full_detail_envelope():
    row = workday.normalize(COMPANY, LIST_ROW, {"jobPostingInfo": DETAIL})
    assert row["posted"] == "2026-09-09" and row["url"] == DETAIL["externalUrl"]


def test_normalize_unhydrated_fallback():
    row = workday.normalize(COMPANY, LIST_ROW, None)
    assert set(row) == ROW_KEYS
    assert row["description"] == ""
    assert row["posted"] is None
    assert row["location"] == "2 Locations"          # the useless list value
    assert row["url"] == ("https://workday.wd5.myworkdayjobs.com/Workday"
                          "/job/USA-CA-Pleasanton/"
                          "Sr-Product-Manager---VNDLY_JR-0108551"), row["url"]
    assert row["salary_min"] is None and row["salary_max"] is None


def test_normalize_flags_remote_from_the_location_text():
    detail = {**DETAIL, "location": "USA, Remote", "additionalLocations": []}
    assert workday.normalize(COMPANY, LIST_ROW, detail)["remote"] is True


def test_normalize_satisfies_the_stores_key_rule():
    # refresh_from_fetch keys on (ats, slug, job_id) and SKIPS empty parts
    for detail in (DETAIL, None):
        row = workday.normalize(COMPANY, LIST_ROW, detail)
        key = (row["ats"], row.get("slug", ""), row.get("job_id", ""))
        assert all(part for part in key), key


# --------------------------------------------------------------------------- #
# facets
# --------------------------------------------------------------------------- #
FACETS = [
    {"facetParameter": "remoteType", "descriptor": "Remote Type",
     "values": [{"descriptor": "Flex", "id": "rt1", "count": 300}]},
    {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [
        {"descriptor": "Product Strategy and Management", "id": "fid-prod", "count": 15},
        {"descriptor": "Field Sales", "id": "fid-sales", "count": 55},
        {"descriptor": "GTM Operations", "id": "fid-ops", "count": 18},
        {"descriptor": "Legal and Compliance", "id": "fid-legal", "count": 3},
    ]},
]
NVIDIA_FACETS = [
    {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [
        {"descriptor": "Hardware Engineering", "id": "n1", "count": 400},
        {"descriptor": "Silicon Design", "id": "n2", "count": 300},
    ]},
]


# --------------------------------------------------------------------------- #
# list_jobs against the fake client
# --------------------------------------------------------------------------- #
def test_list_jobs_pages_until_the_end():
    client = _FakeClient([_page(20, 0), _page(20, 20), _page(5, 40)])
    client.total = 45
    client.facets = FACETS
    rows, meta = asyncio.run(workday.list_jobs(client, "workday", "wd5", "Workday"))
    assert len(rows) == 45, len(rows)
    assert meta["total"] == 45 and meta["facets"] == FACETS
    assert meta["truncated"] is False
    assert len(client.calls) == 3, client.calls
    # total/facets came from offset 0 only; the deep pages' total:0 was ignored
    assert [c[1]["offset"] for c in client.calls] == [0, 20, 40]


def test_list_jobs_detects_the_offset_clamp_replay():
    page0 = _page(20, 0)
    client = _FakeClient({0: page0, 20: _page(20, 20), 40: page0})
    client.total = 2614
    rows, meta = asyncio.run(workday.list_jobs(client, "nvidia", "wd5", "Site"))
    assert len(rows) == 40, len(rows)          # the replayed page is discarded
    assert meta["truncated"] is True
    assert len(client.calls) == 3


def test_list_jobs_sends_applied_facets_and_the_capped_limit():
    client = _FakeClient([_page(3)])
    client.total = 3
    asyncio.run(workday.list_jobs(client, "workday", "wd5", "Workday",
                                  applied_facets={"jobFamilyGroup": ["fid-prod"]}))
    url, body = client.calls[0]
    assert url == ("https://workday.wd5.myworkdayjobs.com"
                   "/wday/cxs/workday/Workday/jobs"), url
    assert body == {"appliedFacets": {"jobFamilyGroup": ["fid-prod"]},
                    "limit": 20, "offset": 0, "searchText": ""}, body


def test_list_jobs_raises_the_taxonomy_reason_on_http_error():
    client = _FakeClient([[]], status_code=404)
    try:
        asyncio.run(workday.list_jobs(client, "workday", "wd5", "Nope"))
    except workday.WorkdayError as exc:
        assert str(exc) == "wrong_site", exc
    else:
        raise AssertionError("expected WorkdayError")


def test_list_jobs_reuses_a_supplied_page0():
    client = _FakeClient([_page(20, 0), _page(3, 20)])
    client.total = 23
    seeded = _page(20, 100)                      # distinct from client.pages[0]
    rows, meta = asyncio.run(workday.list_jobs(
        client, "workday", "wd5", "Workday", page0=(seeded, {"total": 23,
                                                             "facets": FACETS})))
    assert len(rows) == 23 and rows[0] is seeded[0]   # page 0 came from the probe
    assert meta["total"] == 23 and meta["facets"] == FACETS
    assert meta["requests"] == 1, meta           # only offset 20 was fetched
    assert [c[1]["offset"] for c in client.calls] == [20]


def test_list_jobs_stops_on_an_empty_board():
    client = _FakeClient([[]])
    client.total = 0
    rows, meta = asyncio.run(workday.list_jobs(client, "acme", "wd1", "Careers"))
    assert rows == [] and len(client.calls) == 1
    assert meta["truncated"] is False


# --------------------------------------------------------------------------- #
# fetch_workday strategy selection. A board under the 2000-row wall is paged in
# full; a walled one is PARTITIONED by every jobFamilyGroup value, because the
# facet counts are the board's true size and each family fits under the wall.
#
# These fixtures serve a family's rows in full, so a family's facet count, the
# `total` its filtered query reports, and the rows it yields all agree — the way
# the live API behaves. Anything less and list_jobs rightly calls it truncated.
# --------------------------------------------------------------------------- #
def _paged(rows):
    """Chunk a row list into 20-row API pages."""
    return [rows[i:i + 20] for i in range(0, len(rows), 20)] or [[]]


def test_fetch_workday_under_the_clamp_pages_everything():
    # 45 rows, and a jobFamilyGroup facet is present: partitioning would be
    # cheaper, and must still not happen — plain paging is already complete.
    client = _FakeClient([_page(20, 0), _page(20, 20), _page(5, 40)])
    client.total = 45
    client.facets = _facets(("Product", "fid-prod", 30), ("Sales", "fid-sales", 15))
    client.detail = DETAIL
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert client.faceted_calls() == [], client.faceted_calls()
    assert notes["strategy"] == "full", notes
    assert notes["truncated"] is False
    assert notes["partition_total"] is None and notes["families"] == {}
    # probe reused as page 0 -> 3 POSTs for 3 pages, not 4
    assert notes["requests"] == 3 and len(client.calls) == 3, notes
    assert len(rows) == 45 and notes["title_matched"] == 45
    assert notes["hydrated"] == 45 and len(client.gets) == 45


def test_fetch_workday_partitions_a_walled_board_across_every_family():
    # Counts sum to 2000 while the API admits only 2000 — the families ARE the
    # board. Legal replays three of Engineering's ids, so the union must dedupe.
    legal_rows = _page(3, 0, "Counsel") + _page(97, 4000, "Counsel")
    client = _FakeClient([_page(20, 0, "Hardware Engineer")])
    client.total = 2000
    client.facets = _facets(("Engineering", "fid-eng", 1200),
                            ("Operations", "fid-ops", 500),
                            ("Program Manager", "fid-pm", 200),
                            ("Legal", "fid-legal", 100))
    client.family_pages = {
        "fid-eng": _paged(_page(1200, 0, "Hardware Engineer")),
        "fid-ops": _paged(_page(500, 2000, "Field Technician")),
        "fid-pm": _paged(_page(200, 3000)),          # the only product titles
        "fid-legal": _paged(legal_rows),
    }
    client.family_totals = {"fid-eng": 1200, "fid-ops": 500,
                            "fid-pm": 200, "fid-legal": 100}
    client.detail = DETAIL
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]

    # every family was paged, one id per request, biggest first
    asked = [c[1]["appliedFacets"]["jobFamilyGroup"] for c in client.faceted_calls()]
    assert all(len(ids) == 1 for ids in asked), asked
    assert sorted({ids[0] for ids in asked}) == ["fid-eng", "fid-legal",
                                                 "fid-ops", "fid-pm"], asked
    assert [ids[0] for ids in asked][:1] == ["fid-eng"], asked   # biggest first

    assert notes["strategy"] == "partitioned", notes
    assert notes["truncated"] is False, notes
    assert notes["board_total"] == 2000 and notes["partition_total"] == 2000
    # 1 probe + 60 + 25 + 10 + 5 pages
    assert notes["requests"] == 101, notes
    assert notes["families"] == {
        "Engineering": {"count": 1200, "rows": 1200},
        "Operations": {"count": 500, "rows": 500},
        "Program Manager": {"count": 200, "rows": 200},
        "Legal": {"count": 100, "rows": 100},
    }, notes["families"]

    ids = [r["job_id"] for r in rows]
    assert len(ids) == len(set(ids)), len(ids) - len(set(ids))   # deduped
    assert notes["listed"] == 2000 and notes["deduped"] == 3, notes
    assert len(rows) == 1997, len(rows)
    # title_matched is only a note now — every row gets text, matched or not
    assert notes["title_matched"] == 200, notes
    assert notes["hydrated"] == 1997 and notes["hydrated_fetched"] == 1997, notes


def test_a_clean_partition_is_complete_so_the_removal_pass_is_not_suppressed():
    # store.refresh_from_fetch skips removals for any board reporting truncated,
    # so a fully-partitioned board MUST report False or its stale postings never
    # get marked gone.
    client = _FakeClient([_page(20, 0)])
    client.total = 2000
    client.facets = _facets(("Engineering", "fid-eng", 1000),
                            ("Operations", "fid-ops", 1000))
    client.family_pages = {
        "fid-eng": _paged(_page(1000, 0, "Hardware Engineer")),
        "fid-ops": _paged(_page(1000, 5000, "Field Technician")),
    }
    client.family_totals = {"fid-eng": 1000, "fid-ops": 1000}
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["strategy"] == "partitioned", notes
    assert notes["truncated"] is False, notes
    assert len(rows) == 2000, len(rows)


def test_partition_marks_a_family_that_hits_its_own_wall():
    page0 = _page(20, 0, "Hardware Engineer")
    client = _FakeClient([_page(20, 0, "Hardware Engineer")])
    client.total = 2000
    client.facets = _facets(("Engineering", "fid-eng", 2600),
                            ("Legal", "fid-legal", 100))
    client.family_pages = {
        # offset 40 replays page 0: this family is itself past the wall
        "fid-eng": {0: page0, 20: _page(20, 20, "Hardware Engineer"), 40: page0},
        "fid-legal": _paged(_page(100, 5000, "Counsel")),
    }
    client.family_totals = {"fid-eng": 2600, "fid-legal": 100}
    _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["strategy"] == "partitioned_truncated", notes
    assert notes["truncated"] is True
    assert notes["families"]["Engineering"]["truncated"] is True, notes["families"]
    assert "truncated" not in notes["families"]["Legal"], notes["families"]


def test_partition_caps_its_request_budget():
    # Three families needing 6 pages against a budget of 3: the two smallest are
    # skipped and recorded, never silently spent. (Budget shrunk for the test;
    # the real ceiling is _MAX_PARTITION_REQUESTS.)
    client = _FakeClient([_page(20, 0, "Hardware Engineer")])
    client.total = 2000
    client.facets = _facets(("Engineering", "fid-eng", 60),
                            ("Operations", "fid-ops", 40),
                            ("Legal", "fid-legal", 20))
    client.family_pages = {
        "fid-eng": _paged(_page(60, 0, "Hardware Engineer")),
        "fid-ops": _paged(_page(40, 100, "Field Technician")),
        "fid-legal": _paged(_page(20, 200, "Counsel")),
    }
    client.family_totals = {"fid-eng": 60, "fid-ops": 40, "fid-legal": 20}
    real_budget = workday._MAX_PARTITION_REQUESTS
    workday._MAX_PARTITION_REQUESTS = 3
    try:
        _fetch(client)
    finally:
        workday._MAX_PARTITION_REQUESTS = real_budget
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["strategy"] == "partitioned_truncated", notes
    skipped = {name: f for name, f in notes["families"].items() if "skipped" in f}
    assert sorted(skipped) == ["Legal", "Operations"], notes["families"]
    assert all(f["rows"] == 0 for f in skipped.values()), skipped
    assert notes["families"]["Engineering"]["rows"] == 60
    assert len(client.faceted_calls()) == 3        # only Engineering's 3 pages


def test_walled_board_with_no_family_facet_falls_back_to_full():
    # Nothing to partition by, so page until the API replays page 0 and say so.
    page0 = _page(20, 0)
    client = _FakeClient({0: page0, 20: _page(20, 20), 40: page0})
    client.total = 2614
    client.facets = [{"facetParameter": "remoteType", "descriptor": "Remote Type",
                      "values": [{"descriptor": "Flex", "id": "rt1", "count": 9}]}]
    client.detail = DETAIL
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert client.faceted_calls() == []
    assert notes["strategy"] == "full_truncated", notes
    assert notes["truncated"] is True and notes["partition_total"] is None
    assert notes["requests"] == 3, notes          # probe reused, then 2 more
    assert len(rows) == 40


def test_partition_flags_families_that_do_not_account_for_the_board():
    # Counts sum to 300 while the API admits 2000: 1700 rows belong to no family
    # and no partition can reach them.
    client = _FakeClient([_page(20, 0)])
    client.total = 2000
    client.facets = _facets(("Engineering", "fid-eng", 300))
    client.family_pages = {"fid-eng": _paged(_page(300, 0, "Hardware Engineer"))}
    client.family_totals = {"fid-eng": 300}
    _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["strategy"] == "partitioned_truncated", notes
    assert notes["truncated"] is True
    assert notes["board_total"] == 2000 and notes["partition_total"] == 300


def test_union_prefers_the_row_that_can_be_linked():
    # NVIDIA lists a job twice: once properly, once as an id-only stub. Whichever
    # pass saw the stub, the usable row must win.
    good = {"title": "Product Manager", "externalPath": "/job/Loc/PM_JR9",
            "bulletFields": ["JR9"]}
    stub = {"bulletFields": ["JR9"]}
    for passes in [([stub], [good]), ([good], [stub])]:
        merged = workday._union_by_job_id(*passes)
        assert len(merged) == 1 and merged[0] is good, passes


def test_family_values_reads_counts_and_ignores_other_facets():
    facets = _facets(("Engineering", "fid-eng", 1723), ("Legal", "fid-legal", 5))
    assert workday.family_values(facets) == [
        {"id": "fid-eng", "descriptor": "Engineering", "count": 1723},
        {"id": "fid-legal", "descriptor": "Legal", "count": 5}]
    assert workday.family_values([]) == []


def test_pages_for_caps_at_the_wall():
    assert workday._pages_for(0) == 1
    assert workday._pages_for(15) == 1
    assert workday._pages_for(41) == 3
    # a family claiming more than the wall can still only yield 2000 rows
    assert workday._pages_for(9999) == 100


def test_fetch_workday_survives_a_failed_detail_fetch():
    class _NoDetail(_FakeClient):
        async def get(self, url, headers=None):
            self.gets.append(url)
            raise RuntimeError("detail exploded")

    client = _NoDetail([_page(3, 0)])
    client.total = 3
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert len(rows) == 3, rows                   # board survives
    assert notes["hydrated"] == 0 and notes["detail_failed"] == 3, notes
    assert all(r["description"] == "" and r["job_id"] for r in rows)


def test_fetch_workday_drops_unusable_rows_without_killing_the_board():
    # NVIDIA really serves the second row: no title, no externalPath. It has a
    # req id, so it would key — but it can't be hydrated, linked or applied to,
    # and watchlist.prefilter used to index p["title"] directly (KeyError -> the
    # whole board lost).
    client = _FakeClient([[dict(LIST_ROW),
                           {"bulletFields": ["JR2001987"]},
                           {"title": "Product Manager", "bulletFields": []}]])
    client.total = 3
    client.detail = DETAIL
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert [r["job_id"] for r in rows] == ["JR-0108551"], rows
    assert notes["listed"] == 3 and notes["skipped_unusable"] == 2, notes
    assert notes["hydrated"] == 1


# --------------------------------------------------------------------------- #
# fetch-once descriptions. A detail GET is issued only for a job_id the store
# has no text for, so a backfilled board settles to ~0 GETs/day. Reused rows
# carry description, location AND posted forward — the list payload has none of
# them, and re-emitting its values would overwrite good data.
#
# Every fetch_workday test goes through _fetch(), which stubs the store seam:
# the suite must never read the user's real postings DB.
# --------------------------------------------------------------------------- #
class _stored:
    """Patch the one store seam for a test. `rows` is
    {job_id: {"description", "location", "posted"}}; `boom` makes it raise."""

    def __init__(self, rows=None, boom=False):
        self.rows = rows or {}
        self.boom = boom

    def __enter__(self):
        self._real = store.stored_detail_fields
        if self.boom:
            def explode(*a):
                raise RuntimeError("db locked")
            store.stored_detail_fields = explode
        else:
            store.stored_detail_fields = lambda ats, slug: dict(self.rows)
        return self

    def __exit__(self, *exc):
        store.stored_detail_fields = self._real
        return False


def _fetch(client, stored=None, boom=False):
    """fetch_workday against a stubbed store — no network, no real DB."""
    with _stored(stored, boom):
        return asyncio.run(workday.fetch_workday(client, COMPANY))


def _small_board(rows=3):
    client = _FakeClient([_page(rows, 0)])
    client.total = rows
    client.detail = DETAIL
    return client


def test_stored_rows_are_not_refetched():
    # JR0 already has text -> no GET for it, and the stored text comes back
    # verbatim (an empty string here would flip the content hash and wipe it).
    client = _small_board()
    rows = _fetch(client, stored={"JR0": {"description": "stored jd text"}})
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    by_id = {r["job_id"]: r for r in rows}
    assert by_id["JR0"]["description"] == "stored jd text"
    assert len(client.gets) == 2, client.gets           # JR1, JR2 only
    assert all("Role-0_JR0" not in u for u in client.gets), client.gets
    assert notes["hydrated_from_store"] == 1, notes
    assert notes["hydrated_fetched"] == 2, notes
    assert notes["hydrated"] == 3 and notes["hydrate_deferred"] == 0, notes


def test_rows_absent_from_the_store_are_fetched():
    client = _small_board()
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert len(client.gets) == 3, client.gets
    assert notes["hydrated_fetched"] == 3 and notes["hydrated_from_store"] == 0
    assert all(r["description"] for r in rows)


def test_hydration_ignores_the_title_gate():
    # The coverage change: a row nothing would ever title-match still gets text,
    # so widening acceptable_titles later doesn't leave it permanently blank.
    client = _FakeClient([_page(2, 0, "Warehouse Associate")])
    client.total = 2
    client.detail = DETAIL
    rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["title_matched"] == 0, notes       # nothing matched...
    assert notes["hydrated_fetched"] == 2, notes    # ...and both hydrated anyway
    assert all(r["description"] for r in rows)


def test_reused_rows_carry_location_and_posted_forward():
    # location is inside the content hash and posted is rewritten on every
    # ingest branch, so re-emitting the list values ("2 Locations", no date)
    # would overwrite the good stored ones. Measured live: 287/517 hydrated
    # Workday rows have a joined multi-city location, 517/517 a real date.
    client = _FakeClient([[dict(LIST_ROW)]])        # locationsText "2 Locations"
    client.total = 1
    rows = _fetch(client, stored={"JR-0108551": {
        "description": "kept text", "location": "San Jose; San Francisco",
        "posted": "2026-06-23"}})
    assert client.gets == [], client.gets
    row = rows[0]
    assert row["description"] == "kept text"
    assert row["location"] == "San Jose; San Francisco", row["location"]
    assert row["posted"] == "2026-06-23", row["posted"]


def test_reused_row_recomputes_remote_from_the_stored_location():
    client = _FakeClient([[dict(LIST_ROW)]])
    client.total = 1
    rows = _fetch(client, stored={"JR-0108551": {
        "description": "kept", "location": "USA - Remote",
        "posted": "2026-06-23"}})
    assert rows[0]["remote"] is True, rows[0]


def test_hydration_budget_defers_the_overflow():
    client = _FakeClient([_page(5, 0)])
    client.total = 5
    client.detail = DETAIL
    real = workday._MAX_HYDRATE_PER_RUN
    workday._MAX_HYDRATE_PER_RUN = 2
    try:
        rows = _fetch(client)
    finally:
        workday._MAX_HYDRATE_PER_RUN = real
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["hydrated_fetched"] == 2 and notes["hydrate_deferred"] == 3, notes
    assert len(client.gets) == 2, client.gets
    # deferred rows are still emitted, just without text — next run picks them up
    assert len(rows) == 5, len(rows)
    assert sum(1 for r in rows if not r["description"]) == 3


def test_a_broken_store_degrades_to_hydrating_everything():
    # A sourcing run must not depend on the DB being readable.
    client = _small_board()
    rows = _fetch(client, boom=True)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["hydrated_fetched"] == 3 and notes["hydrated_from_store"] == 0
    assert all(r["description"] for r in rows)


# --------------------------------------------------------------------------- #
# detail-fetch retry + circuit breaker. A 12k-request live backfill lost ~43% of
# its detail GETs to transient failures, and 40/40 of the losers returned 200
# when re-probed. So transient failures get two retries; a board that keeps
# failing gives up early rather than burning the run on doomed attempts.
# --------------------------------------------------------------------------- #
class _FlakyClient(_FakeClient):
    """Detail GETs follow a script: {job_id: [status, ...]} consumed in order,
    one entry per attempt. Anything past the end of a script returns 200."""

    def __init__(self, pages, script=None, always=None):
        super().__init__(pages)
        self.script = script or {}
        self.always = always            # status returned for every detail GET
        self.attempts = {}

    async def get(self, url, headers=None):
        self.gets.append(url)
        if self.always is not None:
            return _Resp({}, self.always)
        for job_id, statuses in self.script.items():
            if "_%s" % job_id in url:
                n = self.attempts.get(job_id, 0)
                self.attempts[job_id] = n + 1
                if n < len(statuses) and statuses[n] != 200:
                    return _Resp({}, statuses[n])
                break
        return _Resp({"jobPostingInfo": self.detail} if self.detail else {}, 200)

    def gets_for(self, job_id):
        return [u for u in self.gets if "_%s" % job_id in u]


class _tuning:
    """Temporarily retune the retry/concurrency knobs for one test."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self._real = {k: getattr(workday, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(workday, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._real.items():
            setattr(workday, k, v)
        return False


def test_a_transient_failure_succeeds_on_the_second_attempt():
    client = _FlakyClient([_page(3, 0)], script={"JR0": [503]})
    client.total = 3
    client.detail = DETAIL
    with _tuning(_RETRY_BASE_DELAY=0):
        rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert len(client.gets_for("JR0")) == 2, client.gets_for("JR0")
    assert notes["hydrate_retried"] == 1, notes
    assert notes["hydrate_failed"] == 0 and notes["detail_failed"] == 0, notes
    assert notes["hydrated_fetched"] == 3, notes
    assert all(r["description"] for r in rows)


def test_every_transient_status_is_retried():
    for status in [429, 500, 502, 503, 504]:
        client = _FlakyClient([_page(1, 0)], script={"JR0": [status]})
        client.total = 1
        client.detail = DETAIL
        with _tuning(_RETRY_BASE_DELAY=0):
            _fetch(client)
        notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
        assert notes["hydrate_retried"] == 1, (status, notes)
        assert notes["hydrated_fetched"] == 1, (status, notes)


def test_permanent_statuses_are_not_retried():
    # 404/410: the posting is gone. 401/403: retrying a block makes it worse.
    for status in [404, 410, 401, 403]:
        client = _FlakyClient([_page(1, 0)], always=status)
        client.total = 1
        with _tuning(_RETRY_BASE_DELAY=0):
            rows = _fetch(client)
        notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
        assert len(client.gets) == 1, (status, client.gets)
        assert notes["hydrate_retried"] == 0, (status, notes)
        assert notes["hydrate_failed"] == 1, (status, notes)
        # the row still ships, just without text — next run tries again
        assert len(rows) == 1 and rows[0]["description"] == "", rows


def test_retries_are_bounded():
    client = _FlakyClient([_page(1, 0)], always=503)
    client.total = 1
    with _tuning(_RETRY_BASE_DELAY=0):
        _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert len(client.gets) == workday._RETRY_ATTEMPTS == 3, client.gets
    assert notes["hydrate_retried"] == 2 and notes["hydrate_failed"] == 1, notes


def test_the_circuit_breaker_trips_and_defers_the_rest():
    # Serialised (concurrency 1) and single-attempt so the counts are exact:
    # the first 3 postings fail, the breaker trips, the other 17 never ask.
    client = _FlakyClient([_page(20, 0)], always=503)
    client.total = 20
    with _tuning(_RETRY_ATTEMPTS=1, _RETRY_BASE_DELAY=0, _BREAKER_THRESHOLD=3,
                 _GLOBAL_CONCURRENCY=1, _TENANT_CONCURRENCY=1):
        rows = _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["hydrate_failed"] == 3, notes
    assert notes["hydrate_deferred"] == 17, notes
    assert notes["hydrate_breaker"] is True, notes
    assert len(client.gets) == 3, len(client.gets)      # 17 requests never made
    # deferred rows still ship, unhydrated — next run picks them up
    assert len(rows) == 20 and all(r["description"] == "" for r in rows)


def test_a_success_resets_the_breakers_run_of_failures():
    # 2 fails, a success, 2 more fails: never 3 consecutive, so no trip.
    client = _FlakyClient([_page(5, 0)],
                          script={"JR0": [503], "JR1": [503],
                                  "JR3": [503], "JR4": [503]})
    client.total = 5
    client.detail = DETAIL
    with _tuning(_RETRY_ATTEMPTS=1, _RETRY_BASE_DELAY=0, _BREAKER_THRESHOLD=3,
                 _GLOBAL_CONCURRENCY=1, _TENANT_CONCURRENCY=1):
        _fetch(client)
    notes = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    assert notes["hydrate_breaker"] is False, notes
    assert notes["hydrate_failed"] == 4 and notes["hydrated_fetched"] == 1, notes
    assert notes["hydrate_deferred"] == 0, notes


def test_hydration_counters_account_for_every_row():
    client = _FlakyClient([_page(6, 0)], script={"JR0": [503], "JR1": [404]})
    client.total = 6
    client.detail = DETAIL
    with _tuning(_RETRY_BASE_DELAY=0):
        rows = _fetch(client, stored={"JR5": {"description": "kept"}})
    n = workday.LAST_RUN_NOTES[COMPANY["slug"]]
    # 6 rows: 1 reused, 4 fetched clean, 1 fetched after a retry, 1 hard-failed
    assert n["hydrated_from_store"] == 1, n
    assert n["hydrated_fetched"] == 4, n            # JR0 retried ok, JR1 failed
    assert n["hydrate_retried"] == 1 and n["hydrate_failed"] == 1, n
    assert n["hydrated"] == n["hydrated_fetched"] + n["hydrated_from_store"], n
    # every row is accounted for exactly once
    assert (n["hydrated_fetched"] + n["hydrated_from_store"]
            + n["hydrate_failed"] + n["hydrate_deferred"]) == len(rows), n


def test_retryable_classifies_transport_errors():
    assert workday._retryable(httpx.ConnectError("boom")) is True
    assert workday._retryable(httpx.ReadTimeout("slow")) is True
    assert workday._retryable(workday.WorkdayError("http_429", 429)) is True
    assert workday._retryable(workday.WorkdayError("wrong_site", 404)) is False
    assert workday._retryable(workday.WorkdayError("blocked", 403)) is False
    assert workday._retryable(RuntimeError("unexpected")) is False


def test_backoff_grows_and_is_jittered():
    with _tuning(_RETRY_BASE_DELAY=1.0):
        first = [workday._backoff(0) for _ in range(20)]
        second = [workday._backoff(1) for _ in range(20)]
    assert all(1.0 <= d <= 1.5 for d in first), first
    assert all(2.0 <= d <= 3.0 for d in second), second
    assert len(set(first)) > 1, "no jitter"


# --------------------------------------------------------------------------- #
# robots.txt site enumeration (pure parsing, fed a canned body)
# --------------------------------------------------------------------------- #
class _TextResp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _RobotsClient:
    def __init__(self, text, status_code=200):
        self._resp = _TextResp(text, status_code)

    async def get(self, url, headers=None):
        return self._resp


ROBOTS = """Sitemap: https://workday.wd5.myworkdayjobs.com/Workday/siteMap.xml
Sitemap: https://workday.wd5.myworkdayjobs.com/Workday_Jobs/siteMap.xml
Sitemap: https://workday.wd5.myworkdayjobs.com/Sitemap_Only_Site/siteMap.xml

User-agent: *
Allow: /Workday/
Allow: /Workday_Jobs/
Disallow: /Workday_Internal/
Disallow: /refreshFacet/
"""


def test_resolve_sites_from_robots_splits_allow_and_disallow():
    got = asyncio.run(workday.resolve_sites_from_robots(
        _RobotsClient(ROBOTS), "workday", "wd5"))
    assert got["allowed"] == ["Workday", "Workday_Jobs", "Sitemap_Only_Site"], got
    assert got["disallowed"] == ["Workday_Internal"], got
    for bucket in got.values():
        assert "refreshFacet" not in bucket


def test_resolve_sites_from_robots_tolerates_a_missing_file():
    got = asyncio.run(workday.resolve_sites_from_robots(
        _RobotsClient("", 404), "acme", "wd1"))
    assert got == {"allowed": [], "disallowed": []}, got


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  %s: %s" % (name, exc))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
