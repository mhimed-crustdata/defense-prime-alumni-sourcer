#!/usr/bin/env python3
"""Defense Prime Alumni Sourcer.

Finds US engineers who used to work at a defense prime and now work at a small company,
scores them against the role you are hiring for, and writes a shortlist you can load into
an Ashby project or your ATS.
Data layer: Crustdata POST /person/search and POST /person/enrich (https://docs.crustdata.com).

  search()   -> one /person/search call: past prime + engineer title + US + small current employer
  qualify()  -> deterministic rules that re-check the SAME role (unit tested)
  enrich     -> /person/enrich on the shortlist to confirm the current role is still true
  score()    -> keyword score, or Claude (optional) against your role brief
  summary()  -> aggregate numbers
It never contacts anyone.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

# Prime label -> website domains used in the search filter and re-checked in code.
PRIMES = {
    "Lockheed Martin": ["lockheedmartin.com"],
    "RTX / Raytheon": ["rtx.com", "raytheon.com"],
    "Boeing": ["boeing.com"],
    "Northrop Grumman": ["northropgrumman.com"],
    "General Dynamics": ["gd.com", "generaldynamics.com"],
    "L3Harris": ["l3harris.com", "l3technologies.com"],
    "BAE Systems": ["baesystems.com"],
}
PRIME_NAME = re.compile(
    r"lockheed|raytheon|\brtx\b|boeing|northrop|general dynamics|l3harris|l3 technologies|bae systems", re.I)
SMALL_SIZES = ["2-10", "11-50", "51-200", "201-500"]
SENIORITY_EXCLUDED = ["CXO", "Owner / Partner"]  # founders are a different search
NOT_A_JOB = re.compile(r"\b(member|advisor|adviser|investor|angel|board|volunteer|mentor|ambassador|fellow)\b", re.I)
NOT_ENGINEERING = re.compile(r"\b(sales|support|field|customer)\s+engineer", re.I)
NOT_A_COMPANY = re.compile(r"nonprofit|educational|self-employed|government|partnership", re.I)
CLEARANCE = re.compile(r"clearance|ts/sci|\bsecret\b|\bpolygraph\b|\bsci\b|dod|itar", re.I)

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.headline",
    "basic_profile.location",
    "social_handles.professional_network_identifier",
    "experience.employment_details",
]
ENRICH_FIELDS = ["basic_profile", "experience.employment_details", "skills"]


def all_prime_domains() -> List[str]:
    return [d for domains in PRIMES.values() for d in domains]


def build_filters(sizes: Optional[List[str]] = None, title: str = "Engineer") -> Dict[str, Any]:
    """Every field name here is in the Person Search reference."""
    return {"op": "and", "conditions": [
        {"field": "experience.employment_details.past.company_website_domain", "type": "in", "value": all_prime_domains()},
        {"field": "basic_profile.location.country", "type": "=", "value": "United States"},
        {"field": "experience.employment_details.past.title", "type": "(.)", "value": title},
        {"field": "experience.employment_details.current.company_headcount_range", "type": "in", "value": sizes or SMALL_SIZES},
        {"field": "experience.employment_details.current.seniority_level", "type": "not_in", "value": SENIORITY_EXCLUDED},
    ]}


def role_domain(role: Dict[str, Any]) -> str:
    return normalize_domain(role.get("company_website") or role.get("company_website_domain"))


def prime_of(role: Dict[str, Any]) -> Optional[str]:
    """Which prime a role belongs to, by website domain first, then by company name."""
    d = role_domain(role)
    for label, domains in PRIMES.items():
        if any(d == x or d.endswith("." + x) for x in domains):
            return label
    m = PRIME_NAME.search(role.get("name") or "")
    if m:
        key = m.group(0).lower()
        for label in PRIMES:
            if key.split()[0] in label.lower():
                return label
        return "L3Harris" if key.startswith("l3") else "Other prime"
    return None


def is_real_job(role: Dict[str, Any]) -> bool:
    return bool(role.get("title")) and not NOT_A_JOB.search(role["title"])


def engineer_title(title: str) -> bool:
    return bool(re.search(r"engineer", title or "", re.I)) and not NOT_ENGINEERING.search(title or "")


def newest(roles: Iterable[Dict[str, Any]], key: str = "start_date") -> Optional[Dict[str, Any]]:
    roles = list(roles)
    return max(roles, key=lambda r: (bool(r.get("is_default")), r.get(key) or "")) if roles else None


def profile_url(profile: Dict[str, Any]) -> str:
    handles = profile.get("social_handles") or {}
    return ((handles.get("professional_network_identifier") or {}).get("profile_url")) or ""


def qualify(profile: Dict[str, Any], sizes: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Return a flat record or None. The search filters can each match a different role, so the
    prime, the engineer title and the end date are re-checked on ONE past role here."""
    sizes = sizes or SMALL_SIZES
    emp = (profile.get("experience") or {}).get("employment_details") or {}
    if ((profile.get("basic_profile") or {}).get("location") or {}).get("country") not in (None, "United States"):
        return None
    past = [r for r in emp.get("past") or []
            if prime_of(r) and engineer_title(r.get("title", "")) and r.get("end_date")]
    if not past:
        return None
    old = newest(past, key="end_date")
    current = [r for r in emp.get("current") or [] if is_real_job(r)]
    cur = newest(current)
    if not cur or prime_of(cur):
        return None  # no job, or back at a prime
    if cur.get("company_headcount_range") not in sizes:
        return None
    if NOT_A_COMPANY.search(cur.get("company_type") or ""):
        return None
    if cur.get("seniority_level") in SENIORITY_EXCLUDED:
        return None
    start, end = day(old.get("start_date")), day(old.get("end_date"))
    years = round(max(0, (int(end[:4]) - int(start[:4]))) if start and end else 0)
    loc = (profile.get("basic_profile") or {}).get("location") or {}
    return {
        "person_id": profile.get("crustdata_person_id"),
        "name": (profile.get("basic_profile") or {}).get("name", ""),
        "profile_url": profile_url(profile),
        "state": loc.get("state") or "",
        "headline": (profile.get("basic_profile") or {}).get("headline", ""),
        "prime": prime_of(old),
        "prime_title": old.get("title", ""),
        "prime_years": years,
        "prime_end": end,
        "prime_description": (old.get("description") or "")[:600],
        "current_company": cur.get("name", ""),
        "current_title": cur.get("title", ""),
        "current_start": day(cur.get("start_date")),
        "current_size": cur.get("company_headcount_range", ""),
        "current_industry": cur.get("company_professional_network_industry", ""),
        "current_seniority": cur.get("seniority_level", ""),
        "skills": [],
        "score": 0,
        "reason": "",
    }


def search(client: Crustdata, max_results: int, sizes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    rows, seen = [], set()
    for profile in client.person_search(build_filters(sizes), PERSON_FIELDS, max_results=max_results):
        rec = qualify(profile, sizes)
        if rec and rec["person_id"] not in seen:
            seen.add(rec["person_id"])
            rows.append(rec)
    return rows


# ---------- enrich: confirm the current role on the shortlist ----------

def enrich_shortlist(client: Crustdata, rows: List[Dict[str, Any]], sizes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Re-read each person with /person/enrich, refresh the current role and skills, and drop
    anyone whose fresh profile no longer passes the rules."""
    by_url = {r["profile_url"].rstrip("/"): r for r in rows if r["profile_url"]}
    kept = []
    for item in client.person_enrich(list(by_url), fields=ENRICH_FIELDS):
        if item.get("match_status") != "matched" or not item.get("matches"):
            continue
        old = by_url.get((item.get("matched_on") or "").rstrip("/"))
        if not old:
            continue
        person = item["matches"][0]["person_data"]
        person.setdefault("social_handles", {}).setdefault("professional_network_identifier", {})["profile_url"] = old["profile_url"]
        fresh = qualify(person, sizes)
        if not fresh:
            continue
        fresh["skills"] = ((person.get("skills") or {}).get("professional_network_skills") or [])[:25]
        kept.append(fresh)
    return kept


# ---------- scoring ----------

def keywords(brief: str) -> List[str]:
    stop = {"and", "the", "for", "with", "who", "you", "our", "are", "will", "engineer", "engineers", "senior", "a", "an", "of", "to", "in"}
    return sorted({w for w in re.findall(r"[a-z0-9+#.]{2,}", brief.lower()) if w not in stop})


def keyword_score(rec: Dict[str, Any], brief: str) -> Dict[str, Any]:
    """Deterministic 0-100 score: role keywords in titles, skills and prime work (60),
    years at the prime (20), clearance-style language (10), seniority (10)."""
    kws = keywords(brief)
    text = " ".join([rec["current_title"], rec["prime_title"], rec["prime_description"], rec["headline"],
                     " ".join(rec["skills"])]).lower()
    hits = [k for k in kws if k in text]
    kw_part = 60 * (len(hits) / len(kws)) if kws else 30
    years_part = min(rec["prime_years"], 5) * 4
    clearance_part = 10 if CLEARANCE.search(text) else 0
    seniority_part = {"Senior": 10, "Director": 8, "Vice President": 8, "Experienced Manager": 8}.get(rec["current_seniority"], 4)
    score = round(kw_part + years_part + clearance_part + seniority_part)
    return {"score": min(score, 100), "reason": ("matches: " + ", ".join(hits[:6])) if hits else "no keyword match"}


def claude_score(rec: Dict[str, Any], brief: str) -> Optional[Dict[str, Any]]:
    text = ask_claude(
        "Score this candidate 0-100 against the role below. Reply with JSON only: "
        '{"score": <int>, "reason": "<max 20 words>"}.\n'
        f"ROLE: {brief}\n"
        f"Past: {rec['prime_title']} at {rec['prime']} for {rec['prime_years']} years. {rec['prime_description']}\n"
        f"Now: {rec['current_title']} at {rec['current_company']} ({rec['current_size']} people).\n"
        f"Skills: {', '.join(rec['skills'])}", max_tokens=300)
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
        return {"score": max(0, min(100, int(data["score"]))), "reason": str(data.get("reason", ""))[:160]}
    except (ValueError, KeyError, TypeError):
        return None


def score_all(rows: List[Dict[str, Any]], brief: str, use_claude: bool = False) -> List[Dict[str, Any]]:
    for r in rows:
        s = (claude_score(r, brief) if use_claude else None) or keyword_score(r, brief)
        r.update(s)
    return sorted(rows, key=lambda r: -r["score"])


# ---------- state, output ----------

def only_new(rows: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    return [r for r in rows if str(r["person_id"]) not in seen]


def save_state(rows: List[Dict[str, Any]], state_path: str) -> None:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    seen.update(str(r["person_id"]) for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def bucket(counter: Counter) -> Dict[str, int]:
        merged: Counter = Counter()
        for k, v in counter.items():
            merged[k if v > 1 else "other"] += v
        return dict(merged.most_common())

    return {
        "candidates": len(rows),
        "from_prime": bucket(Counter(r["prime"] for r in rows)),
        "current_company_size": bucket(Counter(r["current_size"] for r in rows)),
        "score_80_plus": sum(1 for r in rows if r["score"] >= 80),
        "score_50_to_79": sum(1 for r in rows if 50 <= r["score"] < 80),
        "score_below_50": sum(1 for r in rows if r["score"] < 50),
    }


def slack_text(rows: List[Dict[str, Any]], limit: int = 10) -> str:
    lines = [f":dart: {len(rows)} defense-prime alumni for review"]
    for r in rows[:limit]:
        lines.append(f"• *{r['name']}* ({r['score']}) {r['prime_title']} at {r['prime']} -> "
                     f"{r['current_title']} at *{r['current_company']}* <{r['profile_url']}|profile>")
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more in out/shortlist.csv")
    return "\n".join(lines)


CSV_COLUMNS = ["name", "profile_url", "score", "reason", "prime", "prime_title", "prime_years",
               "current_title", "current_company", "current_size", "state"]


def write_outputs(rows: List[Dict[str, Any]], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "shortlist.json"), "w") as fh:
        json.dump({"summary": summary(rows), "candidates": rows}, fh, indent=1)
    with open(os.path.join(out_dir, "shortlist.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Defense Prime Alumni Sourcer (Crustdata data layer)")
    p.add_argument("--role", default=os.environ.get("ROLE_BRIEF", "embedded software engineer C++ real-time systems"),
                   help="the role you are hiring for, in plain words")
    p.add_argument("--max-results", type=int, default=200, help="profiles to read from search")
    p.add_argument("--top", type=int, default=25, help="shortlist size sent through /person/enrich")
    p.add_argument("--min-score", type=int, default=int(os.environ.get("MIN_SCORE", "50")))
    p.add_argument("--no-enrich", action="store_true", help="skip the /person/enrich check")
    p.add_argument("--claude", action="store_true", help="score with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack")
    p.add_argument("--all", action="store_true", help="include people already shortlisted")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    use_claude = args.claude and claude_available()
    if args.claude and not use_claude:
        print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): using keyword scoring", file=sys.stderr)

    client = Crustdata()
    rows = search(client, args.max_results)
    if not args.all:
        rows = only_new(rows, args.state)
    rows = score_all(rows, args.role, use_claude=False)
    shortlist = rows[:args.top]
    if not args.no_enrich and shortlist:
        shortlist = enrich_shortlist(client, shortlist)
    shortlist = [r for r in score_all(shortlist, args.role, use_claude=use_claude) if r["score"] >= args.min_score]

    write_outputs(shortlist, args.out)
    print(json.dumps(summary(shortlist), indent=1))
    if shortlist:
        print(send_slack(slack_text(shortlist), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(shortlist, args.state)
    else:
        print("No new candidates above the score threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
