"""Answer-store hygiene: leaked tool-call markup, and persisted alternates.

Phase 5 (2026-09-16): three real records (indices 187-189 of a 192-row
history.json, all scope "evergreen", dated 2026-09-16) turned out to have a
genuine, complete answer immediately followed by the literal text

    </answer>
    <parameter name="scope">evergreen

i.e. the next tool-call's parameter got appended to the answer text instead
of being parsed as markup. `_sanitize_answer` stops this at the
`_upsert_answer` write boundary (covering both `save_answer` and
`capture_submission` from one call site); `scripts/repair_history_markup.py`
is the one-shot fix for records written before the guard existed.

Phase 6 (same date): history rows gained an optional "alternates" list,
populated only when a short (<= SHORT_ANSWER_CHARS) answer is re-saved as a
different short answer to the same normalized question. `search_history`
used to build a fresh dict per row, which would have silently dropped this
new key -- the test at the bottom pins that it does not.

Everything here runs against in-memory lists / monkeypatched config
functions and never touches a real profile's history.json.

Standalone, like the rest of tests/ -- `python tests/test_answer_hygiene.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, data  # noqa: E402

# The exact shape seen in the three real records: a complete sentence,
# straight into the closing tag and the next tool call's parameter, with no
# separator.
_REAL_LEAK = ('This is a complete, genuine answer that ends here.'
              '</answer>\n<parameter name="scope">evergreen')
_REAL_LEAK_CLEAN = 'This is a complete, genuine answer that ends here.'


def test_sanitize_truncates_real_leaked_markup_losslessly():
    cleaned = data._sanitize_answer(_REAL_LEAK)
    assert cleaned == _REAL_LEAK_CLEAN, cleaned


def test_sanitize_leaves_bare_angle_bracket_untouched():
    # A real answer can legitimately contain "<" (a comparison, an emoticon)
    # without ever meaning to invoke the leaked-markup path.
    answer = "Our latency target is < 200ms for the p99."
    assert data._sanitize_answer(answer) == answer


def test_sanitize_leaves_the_word_parameter_untouched():
    # Only the full "<parameter name=" tag shape should trip the guard --
    # not just the word appearing in ordinary prose.
    answer = "I tuned the learning-rate parameter until convergence improved."
    assert data._sanitize_answer(answer) == answer


def test_sanitize_happens_on_the_upsert_write_path():
    # Not just the standalone script -- the guard must be live in the same
    # function save_answer and capture_submission both funnel through.
    history: list = []
    data._upsert_answer(history, "Tell us about yourself", _REAL_LEAK,
                        "evergreen", "", keep_existing_scope=False)
    assert history[0]["answer"] == _REAL_LEAK_CLEAN, history[0]["answer"]


def test_sanitize_happens_via_save_answer():
    # save_answer is the public entry point real callers use; patch config's
    # load/save so nothing touches a real profile's history.json.
    store: list = []
    real_load, real_save = config.load_history, config.save_history
    config.load_history = lambda: store
    config.save_history = lambda h: store.__setitem__(slice(None), h)
    try:
        data.save_answer("Tell us about yourself", _REAL_LEAK, scope="evergreen")
    finally:
        config.load_history, config.save_history = real_load, real_save
    assert store[0]["answer"] == _REAL_LEAK_CLEAN, store[0]["answer"]


def test_alternates_union_two_different_short_answers():
    history = [{"question": "Are you open to relocating?", "answer": "Yes",
               "scope": "evergreen", "company": "", "date": "2026-09-01"}]
    data._upsert_answer(history, "Are you open to relocating?",
                        "Yes, for the right role", "evergreen", "",
                        keep_existing_scope=False)
    entry = history[0]
    assert entry["answer"] == "Yes, for the right role", entry
    assert entry["alternates"] == ["Yes"], entry["alternates"]


def test_alternates_not_accumulated_for_long_answers():
    long_a = "A" * 200
    long_b = "B" * 200
    history = [{"question": "Describe a hard project", "answer": long_a,
               "scope": "company", "company": "", "date": "2026-09-01"}]
    data._upsert_answer(history, "Describe a hard project", long_b, "company",
                        "", keep_existing_scope=False)
    entry = history[0]
    assert entry["answer"] == long_b, entry["answer"]
    assert "alternates" not in entry or entry["alternates"] == [], entry.get("alternates")


def test_alternates_dedupe_on_normalize():
    history = [{"question": "Work authorized?", "answer": "Yes", "scope": "evergreen",
               "company": "", "date": "2026-09-01"}]
    # Re-saving the "same" answer under different casing/punctuation must not
    # create a spurious alternate.
    data._upsert_answer(history, "Work authorized?", "yes!!", "evergreen", "",
                        keep_existing_scope=False)
    entry = history[0]
    assert entry["answer"] == "yes!!", entry["answer"]
    assert not entry.get("alternates"), entry.get("alternates")


def test_alternates_capped_at_five():
    history = [{"question": "Notice period?", "answer": "v0", "scope": "evergreen",
               "company": "", "date": "2026-09-01"}]
    for i in range(1, 8):
        data._upsert_answer(history, "Notice period?", "v%d" % i, "evergreen",
                            "", keep_existing_scope=False)
    entry = history[0]
    assert entry["answer"] == "v7", entry["answer"]
    assert len(entry["alternates"]) == 5, entry["alternates"]
    # Newest-displaced first: v6 was bumped by v7 last, so it leads.
    assert entry["alternates"][0] == "v6", entry["alternates"]


def test_record_with_no_alternates_key_round_trips():
    # No migration: every pre-Phase-6 record is missing the key outright, and
    # must keep working (and keep NOT having the key) when left alone.
    history = [{"question": "Email", "answer": "a@b.com", "scope": "evergreen",
               "company": "", "date": "2026-09-01"}]
    data._upsert_answer(history, "Email", "a@b.com", "evergreen", "",
                        keep_existing_scope=False)
    entry = history[0]
    assert "alternates" not in entry, entry


def test_search_history_returns_alternates_for_a_row_that_has_them():
    # This is the test that would have caught search_history rebuilding a
    # fresh dict per row and silently dropping the new key.
    store = [{"question": "Are you open to relocating?", "answer": "Yes, for the right role",
             "alternates": ["Yes"], "scope": "evergreen", "company": "",
             "date": "2026-09-01"}]
    real_load = config.load_history
    config.load_history = lambda: store
    try:
        rows = data.search_history("Are you open to relocating?", top_k=1)
    finally:
        config.load_history = real_load
    assert rows[0]["alternates"] == ["Yes"], rows[0]


def test_search_history_omits_alternates_key_when_absent():
    store = [{"question": "Email", "answer": "a@b.com", "scope": "evergreen",
             "company": "", "date": "2026-09-01"}]
    real_load = config.load_history
    config.load_history = lambda: store
    try:
        rows = data.search_history("Email", top_k=1)
    finally:
        config.load_history = real_load
    assert "alternates" not in rows[0], rows[0]


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
