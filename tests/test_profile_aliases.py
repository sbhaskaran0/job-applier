"""`get_profile_field` must not resolve phone METADATA labels to the number.

Live failure (Capital One Workday, 2026-11-13): resolve_fields mapped both
"Phone Device Type*" and "Country Phone Code*" to the profile `phone` value at
confidence "exact", because "phone" is a substring of both labels. Those are
pickers whose options are device types / countries, so an exact-confidence
phone number there is a guaranteed wrong fill.

Pure Python with a synthetic profile (config.load_user_profile is replaced);
the real profile is never read. Standalone, like the rest of tests/ --
`python tests/test_profile_aliases.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, data             # noqa: E402

PROFILE = {"phone": "310-555-0100", "country": "United States",
           "email": "someone@example.com", "state": "California"}
config.load_user_profile = lambda: PROFILE


def key(label):
    return data.get_profile_field(label)["matched_key"]


def test_phone_device_type_is_not_the_phone_number():
    r = data.get_profile_field("Phone Device Type*")
    assert r["matched_key"] != "phone", r
    assert r["value"] != PROFILE["phone"], r


def test_country_phone_code_is_not_the_phone_number():
    r = data.get_profile_field("Country Phone Code*")
    assert r["matched_key"] != "phone", r
    # It is a country picker ("United States of America (+1)"), so the
    # country value is the meaningful answer.
    assert r["matched_key"] == "country", r


def test_other_phone_metadata_labels():
    for label in ("Phone Extension", "Phone Type", "Country Code",
                  "Phone Number Country Code"):
        assert key(label) != "phone", label


def test_real_phone_labels_still_resolve():
    for label in ("Phone Number*", "Phone", "Mobile phone", "Telephone",
                  "Contact number"):
        r = data.get_profile_field(label)
        assert r["matched_key"] == "phone" and r["value"] == PROFILE["phone"], \
            (label, r)


ARMED = ("Are you currently serving, or have you served in the Armed Forces "
         "of the United States of America?*")
SPOUSE = ("Are you the spouse or domestic partner of a member of the Armed "
          "Forces of the United States of America?")


def test_united_states_in_a_military_question_is_not_the_state():
    # live Capital One resolve_fields: both mapped to state = "California"
    for label in (ARMED, SPOUSE, "Are you authorized to work in the United States?"):
        r = data.get_profile_field(label)
        assert r["matched_key"] != "state" and r["value"] != "California", (label, r)


def test_state_labels_still_resolve():
    for label in ("State*", "State/Province", "What state do you live in "
                  "(United States only)?", "State of residence"):
        assert key(label) == "state", label
    for label in ("Personal statement", "Real estate experience"):
        assert key(label) != "state", label


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
