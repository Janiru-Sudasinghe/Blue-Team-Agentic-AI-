"""
hermes.py — Autonomous SOC Detection Engineering Agent
=======================================================
Pipeline:
  MISP (random event from pool of 50)
    → Groq / LLaMA-3.3 (fast behavior extraction)
    → Groq / LLaMA-3.3 (YARA-L 2.0 rule generation)
    → Google SecOps Chronicle (rule deployment)

Configuration is loaded exclusively from environment variables.
No credentials are ever hard-coded in this file.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import textwrap
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
import urllib3
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from groq import Groq

# ---------------------------------------------------------------------------
# Suppress InsecureRequestWarning for self-signed MISP certs (internal only)
# ---------------------------------------------------------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ===========================================================================
# LOGGING
# ===========================================================================

def _build_logger() -> logging.Logger:
    """Configure a structured, levelled logger for the pipeline."""
    log = logging.getLogger("hermes")
    log.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)

    if not log.handlers:
        log.addHandler(handler)

    return log


logger = _build_logger()


# ===========================================================================
# CONFIGURATION  (100 % from environment — no hard-coded secrets)
# ===========================================================================

@dataclass(frozen=True)
class Config:
    """
    All runtime parameters.  Every secret is read from the process
    environment so the file can be committed safely to version control.

    Required env vars
    -----------------
    GROQ_API_KEY          – Groq cloud API key
    MISP_URL              – Base URL of your MISP instance  (e.g. https://192.168.1.182)
    MISP_KEY              – MISP automation/REST key
    SECOPS_CRED_PATH      – Absolute path to the SecOps service-account JSON

    Optional env vars
    -----------------
    MISP_POOL_SIZE        – How many recent MISP events to fetch before sampling (default: 50)
    MISP_VERIFY_TLS       – Set to "true" to enable TLS verification (default: false)
    GROQ_FAST_MODEL       – Model used for behavior extraction (default: llama-3.3-70b-versatile)
    GROQ_RULE_MODEL       – Model used for rule generation   (default: llama-3.3-70b-versatile)
    SECOPS_API_URL        – Chronicle rules endpoint (default: production URL)
    DRY_RUN               – Set to "true" to skip SecOps deployment (default: false)
    """

    # ---- Required ----
    groq_api_key: str
    misp_url: str
    misp_key: str
    secops_cred_path: str

    # ---- Optional with defaults ----
    misp_pool_size: int = 50
    misp_verify_tls: bool = False
    groq_fast_model: str = "llama-3.3-70b-versatile"
    groq_rule_model: str = "llama-3.3-70b-versatile"
    secops_api_url: str = (
        "https://backstory.googleapis.com/v2/detect/rules"
    )
    dry_run: bool = False

    # ---- Google SecOps OAuth scopes ----
    secops_scopes: tuple[str, ...] = field(
        default_factory=lambda: (
            "https://www.googleapis.com/auth/chronicle-backstory",
        )
    )

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config instance, failing fast on missing required vars."""
        missing: list[str] = []

        def _require(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                missing.append(name)
            return value

        def _optional(name: str, default: str) -> str:
            return os.getenv(name, default).strip()

        cfg = cls(
            groq_api_key=_require("GROQ_API_KEY"),
            misp_url=_require("MISP_URL").rstrip("/"),
            misp_key=_require("MISP_KEY"),
            secops_cred_path=_require("SECOPS_CRED_PATH"),
            misp_pool_size=int(_optional("MISP_POOL_SIZE", "50")),
            misp_verify_tls=_optional("MISP_VERIFY_TLS", "false").lower() == "true",
            groq_fast_model=_optional("GROQ_FAST_MODEL", "llama-3.3-70b-versatile"),
            groq_rule_model=_optional("GROQ_RULE_MODEL", "llama-3.3-70b-versatile"),
            secops_api_url=_optional(
                "SECOPS_API_URL",
                "https://backstory.googleapis.com/v2/detect/rules",
            ),
            dry_run=_optional("DRY_RUN", "false").lower() == "true",
        )

        if missing:
            raise EnvironmentError(
                f"Missing required environment variable(s): {', '.join(missing)}\n"
                "Set them before running Hermes."
            )

        return cfg


# ===========================================================================
# STAGE 1 — INTELLIGENCE GATHERING  (MISP)
# ===========================================================================

def get_threat_intel(cfg: Config) -> Optional[str]:
    """
    Fetch the last *pool_size* MISP events, select one at random, and
    return a human-readable intelligence summary string.

    Randomising the pick ensures each pipeline run exercises a different
    threat scenario — essential for realistic Detection Engineering testing.

    Returns None on any error so the caller can handle gracefully.
    """
    logger.info("Stage 1 — Fetching threat intel from MISP (pool=%d)", cfg.misp_pool_size)

    headers = {
        "Authorization": cfg.misp_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "limit": cfg.misp_pool_size,
        "returnFormat": "json",
    }
    endpoint = f"{cfg.misp_url}/events/restSearch"

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            verify=cfg.misp_verify_tls,
            timeout=30,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("MISP request timed out after 30 s.")
        return None
    except requests.exceptions.HTTPError as exc:
        logger.error("MISP HTTP error: %s — %s", exc.response.status_code, exc.response.text[:200])
        return None
    except requests.exceptions.ConnectionError as exc:
        logger.error("MISP connection error: %s", exc)
        return None

    raw = response.json()
    event_list = raw.get("response", [])

    if not event_list:
        logger.error("MISP returned an empty event list.")
        return None

    # ── Random selection from the pool ──────────────────────────────────────
    chosen_wrapper = random.choice(event_list)
    event = chosen_wrapper.get("Event", {})

    if not event:
        logger.error("Selected MISP object has no 'Event' key.")
        return None

    event_name = event.get("info", "Unknown Event")
    attributes = event.get("Attribute", [])

    logger.info(
        "Selected event: '%s'  |  Total pool: %d  |  Attributes: %d",
        event_name,
        len(event_list),
        len(attributes),
    )

    # ── Build human-readable summary for the AI stages ──────────────────────
    lines: list[str] = [f"Threat Event: {event_name}", "Indicators of Compromise:"]
    for attr in attributes[:20]:                  # cap at 20 IOCs for token efficiency
        lines.append(f"  - [{attr.get('type', '?')}]  {attr.get('value', '')}")

    return "\n".join(lines)


# ===========================================================================
# STAGE 2 — FAST BEHAVIOR EXTRACTION  (Groq / fast model)
# ===========================================================================

def extract_behavior(report: str, cfg: Config, client: Groq) -> str:
    """
    Use the fast Groq model to distil raw MISP text into a concise,
    structured list of TTPs and IOCs that the rule-writing stage can consume.
    """
    logger.info("Stage 2 — Extracting technical behavior with %s", cfg.groq_fast_model)

    system = (
        "You are a senior SOC analyst. "
        "Given a threat intelligence report, extract ONLY the technical "
        "indicators (IPs, domains, URLs, file hashes, registry keys, etc.) "
        "and adversary behaviors (e.g. lateral movement, C2 beaconing). "
        "Output a concise bulleted list. No prose, no headers, no markdown."
    )

    response = client.chat.completions.create(
        model=cfg.groq_fast_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": report},
        ],
        temperature=0.1,
        max_tokens=512,
    )
    return response.choices[0].message.content.strip()


# ===========================================================================
# STAGE 3 — YARA-L 2.0 RULE GENERATION  (Groq / rule model)
# ===========================================================================

_YARA_L_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert Google SecOps Detection Engineer who writes YARA-L 2.0 rules.

    HARD RULES — violating any of these will cause deployment failure:
    1. Only three sections are allowed: meta:, events:, condition:
       Do NOT add sections like target:, strings:, or filter:.
    2. Every IOC match MUST live inside the events: section.
    3. Use $e.target.hostname for domain/hostname matches.
    4. Use $e.target.url for full URL matches.
    5. Use $e.principal.ip or $e.target.ip for IP addresses.
    6. Combine multiple IOCs with the 'or' operator inside parentheses.
    7. The condition: section must reference at minimum one bound variable (e.g. $e).
    8. Rule names must be alphanumeric + underscores only — no spaces.
    9. Output ONLY the raw YARA-L code.
       No markdown fences, no backticks, no explanations, no preamble.

    CANONICAL FORMAT:
    rule <RuleName> {
      meta:
        description = "<short description>"
        author      = "Hermes Autonomous SOC"
        severity    = "HIGH"
      events:
        $e.metadata.event_type = "NETWORK_HTTP" or
        $e.metadata.event_type = "NETWORK_DNS"
        (
          $e.target.hostname = "malicious.example.com" or
          $e.target.url      = "http://malicious.example.com/payload"
        )
      condition:
        $e
    }
""")


def generate_yara_rule(behavior: str, cfg: Config, client: Groq) -> str:
    """
    Use the rule-writing model to generate a valid YARA-L 2.0 rule from
    the extracted behavior summary produced by Stage 2.
    """
    logger.info("Stage 3 — Generating YARA-L 2.0 rule with %s", cfg.groq_rule_model)

    response = client.chat.completions.create(
        model=cfg.groq_rule_model,
        messages=[
            {"role": "system", "content": _YARA_L_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Write a production-ready YARA-L 2.0 rule for the "
                    f"following extracted IOCs and behaviors:\n\n{behavior}"
                ),
            },
        ],
        temperature=0.0,           # deterministic for rule generation
        max_tokens=1024,
    )
    return response.choices[0].message.content.strip()


def _sanitise_rule(raw: str) -> str:
    """
    Strip any accidental markdown fences that the model might emit
    despite the system prompt, then return clean YARA-L source.
    """
    lines = raw.splitlines()

    # Drop leading ``` or ```yara-l fence
    if lines and lines[0].startswith("```"):
        lines = lines[1:]

    # Drop trailing ``` fence
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()


# ===========================================================================
# STAGE 4 — SECOPS DEPLOYMENT  (Chronicle REST API)
# ===========================================================================

def push_to_secops(rule_text: str, cfg: Config) -> bool:
    """
    Authenticate with a Google service-account JSON and POST the YARA-L rule
    to the Chronicle Rules API.

    Returns True on success, False on any failure.
    Respects cfg.dry_run to skip actual network calls during testing.
    """
    clean_rule = _sanitise_rule(rule_text)

    _banner = "=" * 60
    logger.info("Stage 4 — YARA-L rule ready for deployment\n%s\n%s\n%s", _banner, clean_rule, _banner)

    if cfg.dry_run:
        logger.warning("DRY_RUN=true — skipping actual SecOps deployment.")
        return True

    # ── Authenticate ────────────────────────────────────────────────────────
    logger.info("Authenticating with service account: %s", cfg.secops_cred_path)
    try:
        creds = service_account.Credentials.from_service_account_file(
            cfg.secops_cred_path,
            scopes=list(cfg.secops_scopes),
        )
        authed_session = AuthorizedSession(creds)
    except FileNotFoundError:
        logger.error("Service-account JSON not found: %s", cfg.secops_cred_path)
        return False
    except Exception as exc:
        logger.error("SecOps authentication failed: %s", exc)
        return False

    # ── Deploy ───────────────────────────────────────────────────────────────
    payload = {"ruleText": clean_rule}
    try:
        response = authed_session.post(cfg.secops_api_url, json=payload, timeout=30)
    except Exception as exc:
        logger.error("SecOps API network error: %s", exc)
        return False

    if response.status_code == 200:
        rule_id = response.json().get("ruleId", "<unknown>")
        logger.info("SUCCESS — rule deployed to Google SecOps  (ruleId: %s)", rule_id)
        return True

    logger.error(
        "Deployment failed — HTTP %d\n%s",
        response.status_code,
        response.text[:500],
    )
    return False


# ===========================================================================
# ORCHESTRATOR
# ===========================================================================

def run_pipeline(cfg: Config) -> int:
    """
    Execute the four-stage Hermes pipeline.

    Returns 0 on full success, 1 on any stage failure.
    """
    start = time.monotonic()
    logger.info("══════════  HERMES AUTONOMOUS SOC AGENT — START  ══════════")

    groq_client = Groq(api_key=cfg.groq_api_key)

    # Stage 1 — Intel gathering
    intel = get_threat_intel(cfg)
    if not intel:
        logger.error("Aborting: MISP returned no usable event.")
        return 1
    logger.debug("Intel summary:\n%s", intel)

    # Stage 2 — Behavior extraction
    behavior = extract_behavior(intel, cfg, groq_client)
    logger.info("Behavior extraction complete.\n%s", behavior)

    # Stage 3 — Rule generation
    raw_rule = generate_yara_rule(behavior, cfg, groq_client)

    # Stage 4 — Deployment
    success = push_to_secops(raw_rule, cfg)

    elapsed = time.monotonic() - start
    if success:
        logger.info("══════════  PIPELINE COMPLETE  (%.2f s)  ══════════", elapsed)
        return 0

    logger.error("══════════  PIPELINE FAILED   (%.2f s)  ══════════", elapsed)
    return 1


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main() -> None:
    try:
        cfg = Config.from_env()
    except EnvironmentError as exc:
        logger.critical("Configuration error:\n%s", exc)
        sys.exit(1)

    sys.exit(run_pipeline(cfg))


if __name__ == "__main__":
    main()