#!/usr/bin/env python3
"""Refresh vendors.json for Abaka AI's Vendor Atlas.

Asks Claude (with live web search) to spot-check the existing raw-data
vendor list for changes (acquisitions, rebrands, shutdowns, new
certifications) and surface new vendors, then rewrites vendors.json.

Designed to run unattended on a weekly GitHub Actions schedule
(.github/workflows/scrape-vendors.yml), but works the same run locally:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/scrape_vendors.py
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
VENDORS_PATH = ROOT / "vendors.json"
MODEL = "claude-opus-5"
MAX_RESTARTS = 5  # cap pause_turn resumptions on long web-search turns

DATA_TYPES = [
    "image", "video", "audio/speech", "text/NLP", "sensor/LiDAR",
    "synthetic", "geospatial", "medical", "multilingual",
]
SERVICES = [
    "raw data collection", "data licensing/marketplace", "annotation/labeling",
    "synthetic data generation", "crowdsourcing platform", "managed workforce",
]
WORKFORCE_MODELS = ["crowd/gig", "managed team", "hybrid", "platform/self-serve", "unknown"]

SCHEMA_NOTE = f"""Each vendor object must have exactly these fields:
- name (string)
- website (string, root domain URL)
- hq_country (string)
- founded_year (number or null)
- data_types (array, subset of {DATA_TYPES})
- services (array, subset of {SERVICES})
- workforce_model (one of {WORKFORCE_MODELS})
- notable_clients_or_industries (short string)
- compliance_notes (short string, "" if unknown)
- employee_size_range (short string, e.g. "51-200", "unknown" if unknown)
- notes (1-2 sentence insight for a sourcing/procurement team: what makes them
  distinct, a strength, or a caveat worth knowing)
- source_url (a URL you verified this from)"""


def load_existing():
    if not VENDORS_PATH.exists():
        return []
    payload = json.loads(VENDORS_PATH.read_text())
    return payload.get("vendors", []) if isinstance(payload, dict) else payload


def build_prompt(existing):
    # Strip internal bookkeeping fields before showing the model its own prior output.
    trimmed = [
        {k: v for k, v in v.items() if k not in ("id", "created_at", "updated_at")}
        for v in existing
    ]
    return f"""You maintain a directory of raw training-data vendors — companies that
collect or license raw data used to train AI/ML models (image, video, speech,
text, sensor/LiDAR, synthetic, geospatial, medical, multilingual) — for the
sourcing team at an AI data-annotation company.

Here is the CURRENT list ({len(trimmed)} vendors), as JSON:
{json.dumps(trimmed, indent=2)}

Using web search, do the following:
1. Spot-check entries most likely to have changed (funding, acquisitions,
   rebrands, shutdowns, new certifications, leadership/ownership changes) and
   update those fields. You don't need to re-verify every single entry from
   scratch — focus your searches on what's likely stale.
2. If you find credible evidence a vendor has shut down or been absorbed with
   no successor brand, omit it from the output.
3. Add up to 8 new, real, currently-operating raw-data vendors not already in
   the list, if you find credible ones. Prioritize accuracy over quantity —
   verify each via search, don't rely on memory alone.

{SCHEMA_NOTE}

Respond with ONLY a JSON array of vendor objects (the full updated list) — no
markdown code fences, no commentary before or after."""


def extract_json_array(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in model output:\n{text[:500]}")
    return json.loads(text[start:end + 1])


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or f"vendor-{abs(hash(name))}"


def run_research(existing):
    client = anthropic.Anthropic()
    user_input = build_prompt(existing)
    messages = [{"role": "user", "content": user_input}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 40}]

    response = None
    for _ in range(MAX_RESTARTS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        # Long-running server-tool turn hit its iteration limit — resume it.
        messages = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": response.content},
        ]
    else:
        raise RuntimeError("Gave up: turn still paused after max restarts")

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError(f"No text in final response (stop_reason={response.stop_reason})")
    return text


def main():
    existing = load_existing()
    existing_by_id = {slugify(v["name"]): v for v in existing if v.get("name")}

    raw_text = run_research(existing)
    vendors = extract_json_array(raw_text)

    min_expected = max(5, len(existing) // 2)
    if not isinstance(vendors, list) or len(vendors) < min_expected:
        got = len(vendors) if isinstance(vendors, list) else type(vendors).__name__
        print(
            f"Refusing to write: model returned {got} vendors, expected at least "
            f"{min_expected}. Leaving vendors.json unchanged.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    seen_ids, cleaned = set(), []
    for v in vendors:
        if not v.get("name"):
            continue
        base_id = slugify(v["name"])
        vid, i = base_id, 2
        while vid in seen_ids:
            vid = f"{base_id}-{i}"
            i += 1
        seen_ids.add(vid)

        v["id"] = vid
        prior = existing_by_id.get(base_id)
        v["created_at"] = prior["created_at"] if prior and prior.get("created_at") else now
        v["updated_at"] = now
        cleaned.append(v)

    cleaned.sort(key=lambda v: v["name"].lower())

    payload = {
        "generated_at": now,
        "vendor_count": len(cleaned),
        "vendors": cleaned,
    }
    VENDORS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(cleaned)} vendors to {VENDORS_PATH}")


if __name__ == "__main__":
    main()
