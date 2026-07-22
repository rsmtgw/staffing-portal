"""
app/agents/shared/skill_utils.py
---------------------------------
Shared skill parsing, enrichment, and equivalency utilities used by the matching engine and ingestion tools.

Ported and adapted from:
  - AI-Staffing-Matchmaker/ingestion_bench.py
  - AI-Staffing-Matchmaker/ingestion_demand.py
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Data file paths ───────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_CONF_DIR = Path(__file__).resolve().parents[3] / "conf"

# Module-level caches so files are read at most once per process.
_common_skills_cache: dict | None = None
_skill_equivalencies_cache: dict | None = None


def _load_json_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("[skill_utils] Could not load %s: %s", path, exc)
        return {}


def load_common_skills() -> dict:
    """Load (and cache) app/data/common_skills.json."""
    global _common_skills_cache
    if _common_skills_cache is None:
        _common_skills_cache = _load_json_file(_DATA_DIR / "common_skills.json")
    return _common_skills_cache


def load_skill_equivalencies() -> dict:
    """Load (and cache) conf/skill_equivalencies.json."""
    global _skill_equivalencies_cache
    if _skill_equivalencies_cache is None:
        _skill_equivalencies_cache = _load_json_file(_CONF_DIR / "skill_equivalencies.json")
    return _skill_equivalencies_cache


# ── String helpers ────────────────────────────────────────────────────────────

def clean_skill_string(s: str) -> str:
    """Collapse whitespace and strip leading/trailing space."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.strip())


# Matches the leading "N - " index prefix used in CSV columns
_NUM_PREFIX_RE = re.compile(r"^\d+\s*-\s*")
# Matches the trailing " (Px - Label)" proficiency suffix
_PROF_SUFFIX_RE = re.compile(r"\s*\(P\d\s*-[^)]+\)\s*$")
# Extracts the proficiency integer from "(P4 - Expert)"
_PROF_LEVEL_RE = re.compile(r"\(P(\d)\s*-", re.IGNORECASE)


def extract_skill_name(raw: str) -> str:
    """Strip leading 'N - ' index and trailing '(Px - Level)' from a skill string.

    Examples::

        '1 - Oracle HCM Cloud (P4 - Expert)'   → 'Oracle HCM Cloud'
        '3 - Spring Boot (P3 - Advanced)'       → 'Spring Boot'
        'Python'                                → 'Python'
    """
    s = clean_skill_string(raw)
    s = _NUM_PREFIX_RE.sub("", s)
    s = _PROF_SUFFIX_RE.sub("", s)
    return s.strip()


def extract_skill_proficiency(raw: str) -> int:
    """Return the P-level integer from a skill entry, defaulting to 2 (Intermediate)."""
    m = _PROF_LEVEL_RE.search(raw)
    return int(m.group(1)) if m else 2


# ── Skill-entry parsing ───────────────────────────────────────────────────────

# Matches: '1 - Java (P4 - Expert)'  or  '12 - Spring Boot (P3 - Advanced)'
_ENTRY_RE = re.compile(r"^\d+\s*-\s*(.+?)\s*\(P(\d)\s*-\s*.+?\)\s*$")


def parse_skill_entry(raw: str) -> tuple[str, int]:
    """Parse one Skill_List token into (skill_name, proficiency_int).

    Examples::

        '1 - Java (P4 - Expert)'            → ('Java', 4)
        '15 - SQL (General) (P2 - ...'      → ('SQL (General)', 2)

    Returns ('', 0) if the token cannot be parsed.
    """
    raw = raw.strip()
    m = _ENTRY_RE.match(raw)
    if m:
        return m.group(1).strip(), int(m.group(2))
    # Fallback: strip leading number and proficiency suffix
    name = extract_skill_name(raw)
    prof = extract_skill_proficiency(raw)
    if name:
        return name, prof if prof else 2
    return "", 0


def build_skill_profile(skill_list_str: str) -> list[dict]:
    """Parse a semicolon-separated Skill_List string into [{skill, proficiency}] list.

    Input::

        '1 - Java (P4 - Expert); 2 - Spring Boot (P4 - Expert)'

    Output::

        [{'skill': 'Java', 'proficiency': 4}, {'skill': 'Spring Boot', 'proficiency': 4}]
    """
    if not skill_list_str:
        return []
    entries = [e.strip() for e in skill_list_str.split(";") if e.strip()]
    profile: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        name, level = parse_skill_entry(entry)
        if name and name.lower() not in seen:
            profile.append({"skill": name, "proficiency": level})
            seen.add(name.lower())
    return profile


def parse_demand_skill_column(raw: str | list[str] | None) -> list[str]:
    """Parse demand CSV skill columns into clean skill names.

    Supports values like:
      '2 - Microsoft Azure AI foundry (P4 - Expert)'
      '3 - SQL (P4 - Expert) | 4 - Prompt Engineering (P4 - Expert)'
      ['Skill A', 'Skill B']
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        text = clean_skill_string(str(raw))
        if not text:
            return []
        items = [part.strip() for part in text.split("|") if part.strip()]

    parsed: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = extract_skill_name(str(item))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        parsed.append(name)
    return parsed


# ── Bench enrichment ──────────────────────────────────────────────────────────

def _existing_skill_names(skill_profile: list[dict]) -> set[str]:
    return {e["skill"].lower() for e in skill_profile}


def _resolve_role_key(primary_role: str, common_skills: dict) -> str | None:
    """Map a free-text Primary_Role to the best key in common_skills.json."""
    role_lower = primary_role.lower()
    for key in common_skills:
        if key.lower() == role_lower:
            return key
    for key in common_skills:
        if key.lower() in role_lower:
            return key
    for part in re.split(r"[/,]", role_lower):
        part = part.strip()
        if not part:
            continue
        for key in common_skills:
            if part in key.lower():
                return key
    return None


def enrich_bench_row(candidate: dict, common_skills: dict | None = None) -> dict:
    """Enrich a candidate dict with implicit skills derived from Primary_Role."""
    if common_skills is None:
        common_skills = load_common_skills()

    skill_list_raw = (
        candidate.get("Skill_List")
        or candidate.get("skill_list")
        or ""
    )
    primary_role = clean_skill_string(
        candidate.get("Primary_Role") or candidate.get("role") or ""
    )

    skill_profile = build_skill_profile(skill_list_raw)
    existing = _existing_skill_names(skill_profile)

    matched_key = _resolve_role_key(primary_role, common_skills)
    if matched_key:
        for implicit in common_skills[matched_key]:
            if implicit.lower() not in existing:
                skill_profile.append({"skill": implicit, "proficiency": 2})
                existing.add(implicit.lower())

    has_frontend_fw = any(
        e["skill"].lower() in {"angular", "react", "react.js", "angularjs"}
        and e["proficiency"] >= 2
        for e in skill_profile
    )
    if has_frontend_fw:
        for fe_skill in ("JavaScript", "HTML", "CSS"):
            if fe_skill.lower() not in existing:
                skill_profile.append({"skill": fe_skill, "proficiency": 2})
                existing.add(fe_skill.lower())

    candidate["skill_profile"] = skill_profile
    return candidate


# ── Demand equivalency lookup ─────────────────────────────────────────────────

def lookup_equivalencies(
    primary_skill_raw: str,
    skill_equivalencies: dict | None = None,
) -> list[str]:
    """Return skill names equivalent to ``primary_skill_raw``.

    Matches the logic from AI-Staffing-Matchmaker/ingestion_demand.py:

    - Strips leading 'N - ' and trailing '(Px - Level)' before matching.
    - Skips GENERIC_PLATFORMS (AWS, Azure, GCP, etc.) — too broad to be useful
      as demand equivalencies.
    - Handles two category shapes in skill_equivalencies.json:
        * dict categories  — ``{key: [equivalents]}`` — exact and word-boundary match
        * list categories  — all items are mutual equivalents; if the skill is
          a member, return the other members.
    """
    if skill_equivalencies is None:
        skill_equivalencies = load_skill_equivalencies()

    skill_name = extract_skill_name(primary_skill_raw)
    if not skill_name:
        return []

    skill_lower = skill_name.lower()

    # Build GENERIC_PLATFORMS exclusion set
    generic_platforms: set[str] = {
        p.lower() for p in skill_equivalencies.get("GENERIC_PLATFORMS", [])
        if isinstance(p, str)
    }

    valid_equivs: list[str] = []

    for category, mappings in skill_equivalencies.items():
        if category == "GENERIC_PLATFORMS":
            continue

        if isinstance(mappings, dict):
            # dict category: {key: [equivalent_values]}
            for key, values in mappings.items():
                key_lower = key.lower()
                if key_lower in generic_platforms:
                    continue
                # Exact match or word-boundary match
                matched = (
                    key_lower == skill_lower
                    or bool(re.search(
                        r"(?<![a-zA-Z0-9])" + re.escape(key_lower) + r"(?![a-zA-Z0-9])",
                        skill_lower,
                    ))
                )
                if matched and isinstance(values, list):
                    valid_equivs.extend(values)

        elif isinstance(mappings, list):
            # list category: all members are equivalent to each other
            members_lower = [m.lower() for m in mappings if isinstance(m, str)]
            if skill_lower in members_lower:
                valid_equivs.extend(
                    m for m in mappings
                    if isinstance(m, str) and m.lower() != skill_lower
                )

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for item in valid_equivs:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ── Demand skill columns parser ───────────────────────────────────────────────

def _split_skill_column(raw: str) -> list[str]:
    """Split a pipe-separated skill column and return clean skill name strings.

    Input: '2 - Business Requirements Analysis (P4 - Expert) | 3 - Oracle Cloud Integration (P3 - Advanced)'
    Output: ['Business Requirements Analysis', 'Oracle Cloud Integration']
    """
    if not raw:
        return []
    return [
        extract_skill_name(part)
        for part in raw.split("|")
        if extract_skill_name(part)
    ]


def _split_skill_column_with_levels(raw: str) -> list[dict]:
    """Split a pipe-separated skill column into [{skill, proficiency}] list.

    Preserves the proficiency level from each entry so the Matchmaker can
    use minimum-proficiency guards.
    """
    if not raw:
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for part in raw.split("|"):
        name = extract_skill_name(part)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        result.append({"skill": name, "proficiency": extract_skill_proficiency(part)})
    return result


# ── Demand skill analysis builder ─────────────────────────────────────────────

def parse_demand_skills(
    primary_skill_raw: str,
    secondary_skills_raw: str,
    other_skills_raw: str,
    role_career_level_from: str = "",
    skill_equivalencies: dict | None = None,
) -> dict:
    """Build a ``skill_analysis`` dict from demand CSV columns.

    Mirrors the schema produced by ``analyze_demand_skills()`` so the
    Matchmaker works identically with both LLM-analysed and CSV-ingested
    demands.  Follows AI-Staffing-Matchmaker ingestion_demand.py logic:

    - Strips 'N - ' prefix and '(Px - Level)' suffix from each skill entry.
    - Looks up valid equivalencies using the same dict/list aware algorithm.
    - Secondary and other skills are stored as clean name strings (proficiency
      preserved in ``secondary_skills_with_levels`` and
      ``other_skills_with_levels`` for future Matchmaker use).
    """
    if skill_equivalencies is None:
        skill_equivalencies = load_skill_equivalencies()

    # Primary skill: strip formatting, extract clean name
    primary_skill = extract_skill_name(primary_skill_raw)

    # Secondary skills: pipe-separated entries
    secondary_skills = _split_skill_column(secondary_skills_raw)
    secondary_skills_with_levels = _split_skill_column_with_levels(secondary_skills_raw)

    # Other skills: pipe-separated entries
    other_skills = _split_skill_column(other_skills_raw)
    other_skills_with_levels = _split_skill_column_with_levels(other_skills_raw)

    valid_equivalencies: list[str] = (
        lookup_equivalencies(primary_skill_raw, skill_equivalencies)
        if primary_skill else []
    )

    accenture_level = ""
    if role_career_level_from:
        try:
            cl_num = int(float(str(role_career_level_from).strip()))
            accenture_level = f"CL-{cl_num}"
        except (ValueError, TypeError):
            accenture_level = str(role_career_level_from).strip()

    return {
        "primary_skill": primary_skill,
        "secondary_skills": secondary_skills,
        "secondary_skills_with_levels": secondary_skills_with_levels,
        "other_skills": other_skills,
        "other_skills_with_levels": other_skills_with_levels,
        "inferred_skills": [],          # populated by extract_inferred_skills
        "valid_equivalencies": valid_equivalencies,
        "accenture_level": accenture_level,
        "seniority_indicator": "",
        "source": "csv_ingestion",
    }


# ---------------------------------------------------------------------------
# Extractive inferred-skills (GAP-003 / GAP-018)
# Scans role description for known skills not already in primary/secondary/other.
# ---------------------------------------------------------------------------

_skill_vocab_cache: set[str] | None = None
_NOISE_WORDS = {"go", "r", "c", "no", "git", "sql", "api", "bi", "ai", "ml", "qa",
                "cloud", "data", "security", "design", "lead"}  # too generic for inferred extraction


def _load_skill_vocabulary() -> set[str]:
    """Build a vocabulary of known skills from skill_equivalencies.json."""
    vocab: set[str] = set()
    raw = load_skill_equivalencies()
    for k, v in raw.get("_formal_aliases", {}).items():
        vocab.add(k.strip())
        if isinstance(v, str):
            vocab.add(v.strip())
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, list):
            continue
        vocab.add(k.strip())
        for sub in v:
            if isinstance(sub, str):
                vocab.add(sub.strip())
    return {s for s in vocab if len(s) >= 3 and s.lower() not in _NOISE_WORDS}


def extract_inferred_skills(
    description: str,
    primary: str,
    existing: list[str],
    max_skills: int = 8,
) -> list[str]:
    """Scan *description* for known skills not already in primary/secondary/other.

    Returns up to *max_skills* distinct skills sorted by position in text.
    Uses substring dedup: if a vocab skill is a substring of any already-known
    skill (or vice-versa), it is skipped to avoid noise.
    """
    global _skill_vocab_cache
    if _skill_vocab_cache is None:
        _skill_vocab_cache = _load_skill_vocabulary()

    if not description:
        return []

    already = {s.lower() for s in [primary] + existing if s}

    def _is_covered(skill_lower: str) -> bool:
        """Return True if *skill_lower* overlaps with any already-known skill."""
        for known in already:
            if skill_lower in known or known in skill_lower:
                return True
        return False

    desc_lower = description.lower()
    found: list[tuple[int, str]] = []

    for skill in sorted(_skill_vocab_cache, key=len, reverse=True):
        skill_lower = skill.lower()
        if _is_covered(skill_lower):
            continue
        pattern = r"(?<![a-zA-Z0-9])" + re.escape(skill_lower) + r"(?![a-zA-Z0-9])"
        m = re.search(pattern, desc_lower)
        if m:
            found.append((m.start(), skill))
            already.add(skill_lower)
            if len(found) >= max_skills:
                break

    return [skill for _, skill in sorted(found)]


# ---------------------------------------------------------------------------
# AI-based inferred-skills extraction (lightweight Gemini Flash)
# ---------------------------------------------------------------------------
# Uses gemini-2.0-flash-lite with a minimal prompt to keep token usage very
# low (~250 input + ~60 output tokens per call ≈ $0.00003/role).
# Falls back to the regex vocabulary scanner if the API key is missing or
# the call fails.
# ---------------------------------------------------------------------------

_INFER_SYSTEM = (
    "Extract ONLY specific technical skills, tools, platforms, and frameworks "
    "from a job description. Ignore soft skills. "
    "Do NOT return generic discipline/category names like Full Stack, Backend, "
    "Frontend, DevOps, QA, Data Engineering, Cloud, Security, etc. "
    "Return a JSON array of skill name strings."
)

_INFER_MODEL = "gemini-2.5-flash-lite"

# Generic discipline/category terms that should never appear as inferred skills.
# The matchmaker handles these via capability inference (GAP-040), not skill matching.
_INFERRED_BLOCKLIST = {
    "full stack", "fullstack", "full-stack",
    "backend", "back-end", "back end",
    "frontend", "front-end", "front end",
    "devops", "dev ops", "dev-ops",
    "qa", "quality assurance", "quality engineering",
    "data engineering", "data science", "data analytics",
    "site reliability engineering", "sre", "platform engineering",
    "software engineering", "software development",
    "cloud computing", "cloud engineering", "cloud architecture",
    "machine learning", "artificial intelligence",
    "cybersecurity", "information security",
    "project management", "program management",
    "agile", "scrum", "waterfall",
    "ci/cd", "continuous integration", "continuous delivery",
    "microservices", "microservices architecture",
    "web development", "mobile development",
    "api development", "api design",
    "system design", "system architecture",
    "threat detection", "threat response",
    "incident response", "vulnerability management",
    "cloud environments", "cloud infrastructure",
    "operational technology", "ot security",
}


def extract_inferred_skills_ai(
    description: str,
    role_title: str,
    primary: str,
    existing: list[str],
    max_skills: int = 8,
) -> list[str]:
    """Use Gemini Flash to extract inferred skills from *description*.

    Keeps token usage minimal:
    - System instruction is ~30 tokens (cached across calls by the API).
    - User prompt sends only role title + truncated description (≤800 chars).
    - Expects a tiny JSON array response (~60 tokens).

    Falls back to :func:`extract_inferred_skills` (regex) on any failure.
    """
    import os

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or not description or len(description) < 60:
        return extract_inferred_skills(description, primary, existing, max_skills)

    # Build the already-known set for dedup
    already = {s.lower() for s in [primary] + existing if s}

    # Truncate description to keep input tokens low (~200 words ≈ 800 chars)
    desc_truncated = description[:800]

    prompt = (
        f"Role: {role_title}\n"
        f"Known skills (exclude these): {', '.join([primary] + existing)}\n\n"
        f"Description:\n{desc_truncated}\n\n"
        "Return ONLY new technical skills as JSON array, max 8."
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_INFER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_INFER_SYSTEM,
                temperature=0.0,
                max_output_tokens=120,
                response_mime_type="application/json",
            ),
        )

        import json as _json

        text = response.text.strip()
        skills = _json.loads(text)
        if not isinstance(skills, list):
            raise ValueError(f"Expected list, got {type(skills)}")

        # Dedup against already-known skills (substring-aware) + blocklist
        result: list[str] = []
        for skill in skills:
            if not isinstance(skill, str) or not skill.strip():
                continue
            s = skill.strip()
            s_lower = s.lower()
            # Skip multi-word descriptive phrases (real skills are ≤4 words)
            if len(s_lower.split()) > 4:
                continue
            # Skip generic discipline/category names (exact or substring)
            if s_lower in _INFERRED_BLOCKLIST or any(b in s_lower for b in _INFERRED_BLOCKLIST):
                continue
            # Skip if overlaps with any already-known skill
            if any(s_lower in k or k in s_lower for k in already):
                continue
            result.append(s)
            already.add(s_lower)
            if len(result) >= max_skills:
                break

        logger.info("[infer_ai] %s → %d skills extracted", role_title[:40], len(result))
        return result

    except Exception as exc:
        logger.warning("[infer_ai] Gemini call failed for '%s': %s — falling back to regex", role_title[:40], exc)
        return extract_inferred_skills(description, primary, existing, max_skills)


# ---------------------------------------------------------------------------
# Combined AI extraction: infer skills aware of actual candidate roster
# ---------------------------------------------------------------------------
# One call per demand at scoring time.  Passes the demand description AND
# the unique skill vocabulary from the candidate roster so the AI only returns
# skills that candidates could actually have — zero wasted inferred entries.
# ---------------------------------------------------------------------------

_INFER_ROSTER_SYSTEM = (
    "You match job requirements to a candidate skill roster. "
    "Given a job description and a list of candidate skills that exist in our bench, "
    "return ONLY the candidate skills that are relevant to this role but NOT already "
    "listed as primary/secondary/other. Return a JSON array of skill name strings. "
    "Do NOT invent new skills — pick ONLY from the provided candidate skill list."
)


def infer_skills_from_roster(
    description: str,
    role_title: str,
    primary: str,
    existing: list[str],
    roster_skills: list[str],
    max_skills: int = 8,
) -> list[str]:
    """Extract inferred skills by matching demand description against actual roster skills.

    One AI call per demand.  The AI sees both the job description and the full
    roster skill vocabulary, so it returns only skills candidates actually have.

    Cost: ~300 input + ~60 output tokens ≈ $0.00004/demand.

    Falls back to :func:`extract_inferred_skills_ai` if the API call fails.
    """
    import os

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key or not description or len(description) < 60 or not roster_skills:
        return extract_inferred_skills_ai(description, role_title, primary, existing, max_skills)

    already = {s.lower() for s in [primary] + existing if s}

    # Deduplicate roster skills and exclude already-known ones
    unique_roster = sorted({s for s in roster_skills if s.lower() not in already})
    # Cap roster skill list to keep tokens low (~150 skills ≈ 300 tokens)
    roster_sample = unique_roster[:150]

    desc_truncated = description[:600]

    prompt = (
        f"Role: {role_title}\n"
        f"Already required (exclude): {', '.join([primary] + existing)}\n\n"
        f"Description:\n{desc_truncated}\n\n"
        f"Candidate skills available in our bench:\n{', '.join(roster_sample)}\n\n"
        "From the candidate skills list above, which are relevant to this role? "
        "Return ONLY matching skills as JSON array, max 8."
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_INFER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_INFER_ROSTER_SYSTEM,
                temperature=0.0,
                max_output_tokens=120,
                response_mime_type="application/json",
            ),
        )

        import json as _json

        text = response.text.strip()
        skills = _json.loads(text)
        if not isinstance(skills, list):
            raise ValueError(f"Expected list, got {type(skills)}")

        # Validate: only accept skills that were actually in the roster
        roster_lower = {s.lower() for s in roster_skills}
        result: list[str] = []
        for skill in skills:
            if not isinstance(skill, str) or not skill.strip():
                continue
            s = skill.strip()
            s_lower = s.lower()
            if len(s_lower.split()) > 4:
                continue
            if s_lower in _INFERRED_BLOCKLIST or any(b in s_lower for b in _INFERRED_BLOCKLIST):
                continue
            if any(s_lower in k or k in s_lower for k in already):
                continue
            # Must exist in the actual roster (exact or close match)
            if s_lower not in roster_lower and not any(s_lower in rs or rs in s_lower for rs in roster_lower):
                continue
            result.append(s)
            already.add(s_lower)
            if len(result) >= max_skills:
                break

        logger.info("[infer_roster] %s → %d skills (from %d roster skills)", role_title[:40], len(result), len(roster_sample))
        return result

    except Exception as exc:
        logger.warning("[infer_roster] Gemini failed for '%s': %s — falling back", role_title[:40], exc)
        return extract_inferred_skills_ai(description, role_title, primary, existing, max_skills)


# ---------------------------------------------------------------------------
# Full AI skill map: semantic matching of ALL demand skills against roster
# ---------------------------------------------------------------------------
# One AI call per demand at upload time.  Returns a mapping of each demand
# skill → list of semantically equivalent roster skills.  Stored in Cosmos
# so the dashboard can score both deterministic and non-deterministic matches
# without any AI calls at render time.
# ---------------------------------------------------------------------------

_SKILLMAP_SYSTEM = (
    "You are a technical recruiter matching job requirements to candidate skills. "
    "For each required skill, find semantically equivalent or closely related skills "
    "from the candidate roster. Only return genuine technical matches — a candidate "
    "skill must actually demonstrate competence in the required area. "
    "Also extract additional technical skills from the job description that aren't "
    "in the listed requirements. "
    "Return JSON with two keys:\n"
    '  "skill_map": {"required_skill": ["matching_roster_skill", ...]},\n'
    '  "inferred_skills": ["extra_skill_from_description", ...]'
)


def compute_ai_skill_map(
    description: str,
    role_title: str,
    primary: str,
    secondary: list[str],
    other: list[str],
    roster_skills: list[str],
    max_inferred: int = 8,
) -> dict:
    """Build a semantic skill map for a demand using AI.

    One call per demand.  Returns::

        {
            "skill_map": {
                "Oracle Database Vector Search": ["Vector Database", "Oracle DB"],
                "Python (Programming Language)": ["Python", "Python 3", "PySpark"],
                ...
            },
            "inferred_skills": ["Docker", "Kubernetes"],  # from description
            "source": "ai"
        }

    The matchmaker uses ``skill_map`` for non-deterministic matching:
    if a candidate has any skill listed in the map for a demand requirement,
    it counts as an AI match (separate from the deterministic match).

    Falls back to ``{"skill_map": {}, "inferred_skills": [], "source": "fallback"}``.
    """
    import os

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    all_demand_skills = [primary] + secondary + other
    all_demand_skills = [s for s in all_demand_skills if s]

    if not api_key or not all_demand_skills or not roster_skills:
        return {"skill_map": {}, "inferred_skills": [], "source": "fallback"}

    # Deduplicate roster skills, cap at 150 to control tokens
    unique_roster = sorted({s for s in roster_skills if s})[:150]

    desc_truncated = (description or "")[:600]

    demand_skills_str = ", ".join(all_demand_skills)
    roster_str = ", ".join(unique_roster)

    prompt = (
        f"Role: {role_title}\n\n"
        f"Required skills: {demand_skills_str}\n\n"
        f"Description:\n{desc_truncated}\n\n"
        f"Candidate skills in our bench:\n{roster_str}\n\n"
        "1) For each required skill, which candidate skills are semantically equivalent? "
        "(Only real technical matches, not generic overlaps.)\n"
        "2) What additional technical skills from the description aren't in the required list "
        "but exist in the candidate bench? (max 8)\n\n"
        "Return JSON with keys: skill_map, inferred_skills"
    )

    try:
        from google import genai
        from google.genai import types
        import json as _json

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_INFER_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SKILLMAP_SYSTEM,
                temperature=0.0,
                max_output_tokens=400,
                response_mime_type="application/json",
            ),
        )

        raw = _json.loads(response.text.strip())
        if not isinstance(raw, dict):
            raise ValueError(f"Expected dict, got {type(raw)}")

        # Validate skill_map: only keep roster skills that actually exist
        roster_lower = {s.lower(): s for s in roster_skills}
        demand_lower = {s.lower() for s in all_demand_skills}
        validated_map: dict[str, list[str]] = {}

        for demand_skill, matches in (raw.get("skill_map") or {}).items():
            if not isinstance(matches, list):
                continue
            valid = []
            for m in matches:
                if not isinstance(m, str) or not m.strip():
                    continue
                m_lower = m.strip().lower()
                # Must exist in roster (exact or fuzzy) and not be the demand skill itself
                if m_lower in demand_lower:
                    continue
                if m_lower in roster_lower:
                    valid.append(roster_lower[m_lower])  # use canonical casing
                else:
                    # Fuzzy: check substring overlap with roster
                    for rl, canonical in roster_lower.items():
                        if (m_lower in rl or rl in m_lower) and rl not in demand_lower:
                            valid.append(canonical)
                            break
            if valid:
                validated_map[demand_skill] = valid

        # Validate inferred_skills: must exist in roster, not in demand or blocklist
        inferred: list[str] = []
        for skill in (raw.get("inferred_skills") or []):
            if not isinstance(skill, str) or not skill.strip():
                continue
            s = skill.strip()
            s_lower = s.lower()
            if s_lower in demand_lower:
                continue
            if len(s_lower.split()) > 4:
                continue
            if s_lower in _INFERRED_BLOCKLIST or any(b in s_lower for b in _INFERRED_BLOCKLIST):
                continue
            # Must exist in roster
            if s_lower in roster_lower:
                inferred.append(roster_lower[s_lower])
            elif any(s_lower in rl or rl in s_lower for rl in roster_lower):
                for rl, canonical in roster_lower.items():
                    if s_lower in rl or rl in s_lower:
                        if canonical.lower() not in demand_lower:
                            inferred.append(canonical)
                        break
            if len(inferred) >= max_inferred:
                break

        result = {
            "skill_map": validated_map,
            "inferred_skills": inferred,
            "source": "ai",
        }
        logger.info(
            "[ai_skill_map] %s → %d mapped skills, %d inferred",
            role_title[:40], len(validated_map), len(inferred),
        )
        return result

    except Exception as exc:
        logger.warning("[ai_skill_map] Gemini failed for '%s': %s", role_title[:40], exc)
        return {"skill_map": {}, "inferred_skills": [], "source": "fallback"}

