#!/usr/bin/env python3
"""Refresh vendors.json for Abaka AI's Vendor Atlas.

Asks Claude (with live web search) to spot-check the existing raw-data
vendor list for changes (acquisitions, rebrands, shutdowns, new
certifications) and surface new vendors, then rewrites vendors.json.
Appends a summary of each real run to refresh_log.json, which the
dashboard (index.html) displays.

Designed to run unattended on a weekly GitHub Actions schedule
(.github/workflows/scrape-vendors.yml), but works the same locally:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/scrape_vendors.py

COST CONTROL — every run prints its own token/search usage and an
estimated USD cost before touching any file. Two flags make test runs
cheap and safe:

    --dry-run       Print the result and its cost; never write
                     vendors.json or refresh_log.json. (Does NOT avoid
                     the API cost — the call is already billed by the
                     time you see the estimate. Use --sample to
                     actually shrink the cost.)
    --sample N      Only send the first N existing vendors as context,
                     skip adding new ones, and scale search budget and
                     thinking effort down accordingly. Even without
                     --dry-run, a sampled run only ever touches those
                     N vendors in vendors.json — the rest of the list
                     is left untouched, so it's safe to run for real.

A cheap smoke test of the full pipeline:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/scrape_vendors.py --sample 5 --dry-run

Typically a few cents (a handful of searches, a small prompt, low
thinking effort) — see the printed cost line for the actual number.
For a hard ceiling regardless of what this script estimates, also set
a spend limit on your key/workspace in the Claude Console
(Settings -> Billing -> Limits).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
VENDORS_PATH = ROOT / "vendors.json"
LOG_PATH = ROOT / "refresh_log.json"
LOG_KEEP = 20  # trailing runs kept in refresh_log.json

DEFAULT_MODEL = "claude-opus-5"
MAX_RESTARTS = 5  # cap pause_turn resumptions on long web-search turns
SEARCH_TOOL_TYPE = "web_search_20260209"

# $ per million tokens (input, output). Search cost is separate: $10 / 1,000
# searches (https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool).
PRICING = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
SEARCH_COST_PER_USE = 10.0 / 1000

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


def parse_args():
    p = argparse.ArgumentParser(
        description="Refresh vendors.json via Claude + web search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                    help="Print the result and its cost; don't write any file.")
    p.add_argument("--sample", type=int, default=0, metavar="N",
                    help="Only use the first N existing vendors as context and add none new — "
                         "a small, cheap, low-blast-radius test of the pipeline.")
    p.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Model override (default: {DEFAULT_MODEL}).")
    p.add_argument("--max-uses", type=int, default=None,
                    help="Cap on web searches for this run (default: 40, or auto-scaled down "
                         "with --sample).")
    p.add_argument("--effort", default=None,
                    choices=["low", "medium", "high", "xhigh", "max"],
                    help="Thinking effort (default: high, or 'low' automatically with --sample).")
    return p.parse_args()


def load_existing():
    if not VENDORS_PATH.exists():
        return []
    payload = json.loads(VENDORS_PATH.read_text())
    return payload.get("vendors", []) if isinstance(payload, dict) else payload


def load_log():
    if LOG_PATH.exists():
        return json.loads(LOG_PATH.read_text())
    return []


def append_log(entry):
    log = load_log()
    log.append(entry)
    LOG_PATH.write_text(json.dumps(log[-LOG_KEEP:], indent=2) + "\n")


def build_prompt(context_vendors, allow_new):
    # Strip internal bookkeeping fields before showing the model its own prior output.
    trimmed = [
        {k: v for k, v in v.items() if k not in ("id", "created_at", "updated_at")}
        for v in context_vendors
    ]
    new_vendor_instruction = (
        f"3. Add up to {allow_new} new, real, currently-operating raw-data vendors not "
        "already in the list, if you find credible ones. Prioritize accuracy over "
        "quantity — verify each via search, don't rely on memory alone."
        if allow_new > 0 else
        "3. Do not add any new vendors — this is a limited test run scoped to the list above."
    )
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
{new_vendor_instruction}

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


def estimate_cost(usage, model):
    rates = PRICING.get(model, PRICING[DEFAULT_MODEL])
    return (
        usage["input_tokens"] * rates["input"] / 1_000_000
        + usage["output_tokens"] * rates["output"] / 1_000_000
        + usage["cache_read_input_tokens"] * rates["input"] * 0.1 / 1_000_000
        + usage["cache_creation_input_tokens"] * rates["input"] * 1.25 / 1_000_000
        + usage["search_requests"] * SEARCH_COST_PER_USE
    )


def run_research(context_vendors, model, max_uses, effort, allow_new):
    client = anthropic.Anthropic()
    user_input = build_prompt(context_vendors, allow_new)
    messages = [{"role": "user", "content": user_input}]
    tools = [{"type": SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses}]

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "search_requests": 0}
    response = None
    for _ in range(MAX_RESTARTS):
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        )
        u = response.usage
        usage["input_tokens"] += u.input_tokens
        usage["output_tokens"] += u.output_tokens
        usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        usage["search_requests"] += getattr(stu, "web_search_requests", 0) or 0

        if response.stop_reason != "pause_turn":
            break
        # Long-running server-tool turn hit its iteration limit — resume it.
        messages = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": response.content},
        ]
    else:
        raise RuntimeError("Gave up: turn still paused after max restarts")

    # Claude narrates before/between tool calls ("I'll spot-check..."), so the
    # final JSON answer can be the LAST of several text blocks, not the first.
    # Concatenate them all — extract_json_array finds the outermost [...] in it.
    text = "\n".join(b.text for b in response.content if b.type == "text")
    if not text.strip():
        raise RuntimeError(f"No text in final response (stop_reason={response.stop_reason})")
    return text, usage


def main():
    args = parse_args()
    sample_mode = args.sample > 0

    existing = load_existing()
    existing_by_id = {slugify(v["name"]): v for v in existing if v.get("name")}
    context_vendors = existing[:args.sample] if sample_mode else existing

    max_uses = args.max_uses or (max(4, args.sample * 2) if sample_mode else 40)
    effort = args.effort or ("low" if sample_mode else "high")
    allow_new = 0 if sample_mode else 8

    print(f"Model: {args.model} | context vendors: {len(context_vendors)} | "
          f"max_uses: {max_uses} | effort: {effort} | sample mode: {sample_mode}")

    raw_text, usage = run_research(context_vendors, args.model, max_uses, effort, allow_new)
    cost = estimate_cost(usage, args.model)
    print(f"Usage: {usage['input_tokens']} in / {usage['output_tokens']} out / "
          f"{usage['search_requests']} searches | est. cost: ${cost:.4f}")

    vendors = extract_json_array(raw_text)
    min_expected = 1 if sample_mode else max(5, len(existing) // 2)
    if not isinstance(vendors, list) or len(vendors) < min_expected:
        got = len(vendors) if isinstance(vendors, list) else type(vendors).__name__
        print(f"Refusing to write: model returned {got} vendors, expected at least "
              f"{min_expected}. Leaving vendors.json unchanged.", file=sys.stderr)
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

    if sample_mode:
        # Merge into the full list — a sampled run must never drop or blank out
        # the vendors outside its own context, dry-run or not.
        merged = dict(existing_by_id)
        for v in cleaned:
            merged[v["id"]] = v
        final_vendors = sorted(merged.values(), key=lambda v: v["name"].lower())
    else:
        final_vendors = sorted(cleaned, key=lambda v: v["name"].lower())

    added = sorted(set(v["id"] for v in cleaned) - set(existing_by_id.keys()))
    removed = sorted(set(existing_by_id.keys()) - set(v["id"] for v in final_vendors)) if not sample_mode else []
    updated = sorted(v["id"] for v in cleaned if v["id"] in existing_by_id and v["id"] not in added)

    print(f"Diff: +{len(added)} added, -{len(removed)} removed, ~{len(updated)} updated")

    if args.dry_run:
        print(f"[dry run] Would write {len(final_vendors)} vendors. Preview of first 2:")
        print(json.dumps(final_vendors[:2], indent=2))
        return

    payload = {
        "generated_at": now,
        "vendor_count": len(final_vendors),
        "vendors": final_vendors,
    }
    VENDORS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(final_vendors)} vendors to {VENDORS_PATH}")

    append_log({
        "timestamp": now,
        "model": args.model,
        "sample_mode": sample_mode,
        "vendor_count": len(final_vendors),
        "added": len(added),
        "removed": len(removed),
        "updated": len(updated),
        "searches_used": usage["search_requests"],
        "est_cost_usd": round(cost, 4),
    })
    print(f"Appended run summary to {LOG_PATH}")


if __name__ == "__main__":
    main()
