import logging
import json
from typing import Optional, Dict, Any

from flask import current_app
import anthropic
import httpx  # Used to configure detailed network timeouts for Anthropic requests.

# Anthropic SDK compatibility:
# Some SDK versions do not expose OverloadedError as anthropic.OverloadedError.
# Try the public import first, then fall back to the internal exceptions module.
# OverloadedError represents Anthropic HTTP 529: service overloaded.
try:
    from anthropic import OverloadedError
except ImportError:
    from anthropic._exceptions import OverloadedError

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "claude-haiku-4-5-20251001" # Claude model
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0  # Maximum time allowed for the full LLM request
DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS = 10.0  # Maximum time allowed just to establish the network connection
DEFAULT_LLM_MAX_RETRIES = 0  # Disable Anthropic SDK retries. Celery will handle retries so we do not retry twice.

# LLM errors that are considered temporary and safe to retry.
# These should be caught before catching anthropic.APIError,
# because many of them inherit from Anthropic's generic APIError class.
TRANSIENT_LLM_ERRORS = (
    anthropic.APITimeoutError,      # Request timed out.
    anthropic.APIConnectionError,   # Network/transport failure.
    anthropic.RateLimitError,       # 429: too many requests / rate limited.
    anthropic.InternalServerError,  # 5xx: Anthropic server-side failure.
    OverloadedError                 # 529: Anthropic service overloaded.
)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM_FAKE success-mode canned responses
# ═══════════════════════════════════════════════════════════════════════════════
# ⚠️  MAINTENANCE — KEEP THESE SHAPES IN SYNC WITH THE PARSERS IN THIS MODULE.
#
#     These hardcoded payloads mirror what the parsers below currently expect:
#
#       * extract_interview_details() + _safe_json_load() (this file) parse the
#         extraction response as JSON with the exact 7 keys in _FAKE_EXTRACTION_JSON.
#         If a key there is renamed / added / removed, update _FAKE_EXTRACTION_JSON in
#         LOCKSTEP.
#       * the 5 enrichment methods (generate_* / suggest_*) return _call()'s output
#         VERBATIM (no parsing), so their success payloads are just non-empty prose and
#         are not shape-coupled.
#
#     If a parser's expected shape changes and this fake is NOT updated, the live
#     success test keeps passing against the STALE shape while real Anthropic responses
#     use the NEW shape — a green-for-the-wrong-reason failure. Only the extraction
#     entry is parser-coupled; keep it matching the schema in extract_interview_details().
_FAKE_EXTRACTION_JSON = json.dumps({
    "company_name":     "Google",
    "role_title":       "Software Engineer",
    "interview_stage":  "final",
    "interview_format": "onsite",
    "date_text":        "next Tuesday",
    "time_text":        "10:00 AM",
    "location_text":    "123 Main St, San Francisco",
})

# Ordered (unique prompt substring) -> canned response. First match wins.
_FAKE_SUCCESS_RESPONSES = (
    ("strict information extraction system", _FAKE_EXTRACTION_JSON),
    ("career coach",
     "1. Review core CS fundamentals and system design.\n"
     "2. Research Acme Corp's products and recent news.\n"
     "3. Prepare STAR stories for behavioral questions."),
    ("Generate 5 strong questions",
     "1. What does success look like in the first 90 days?\n"
     "2. How is the team structured?\n"
     "3. What are the biggest technical challenges right now?\n"
     "4. How do you support mentorship and growth?\n"
     "5. What are the next steps in the process?"),
    ("professional email reply",
     "Thank you for the invitation. I'm excited to confirm my availability for the "
     "final onsite interview and look forward to meeting the team."),
    ("Summarize this role",
     "A software engineering role at Acme Corp building backend services. Acme is a "
     "product company; expect a systems-design and coding-focused onsite interview."),
    ("single sentence archive summary",
     "Interview invitation from Acme Corp for the Software Engineer role."),
)


def _fake_success_payload(prompt: str) -> str:
    """
    Return a shape-appropriate canned response for LLM_FAKE success mode.

    Only the extraction call's payload is parsed (as JSON); enrichment payloads are
    returned verbatim. See the MAINTENANCE note above — the shapes must track the
    parsers in this module.
    """
    for marker, response in _FAKE_SUCCESS_RESPONSES:
        if marker in prompt:
            return response
    return "[fake-llm-success] canned response"   # fallback: non-empty


class LLMService:
    """
    Handles all LLM-based features using the Anthropic API.

    Design principle:
        LLM = text intelligence only
        Backend = logic, rules, DB, workflows
    """

    def __init__(self):
        self._client: Optional[anthropic.Anthropic] = None
        self._model: str = DEFAULT_LLM_MODEL    # frozen at load() — _call never reads current_app
        self._fake: bool = False                # LLM_FAKE mode (no external calls)
        self._fake_mode: str = "degrade"        # LLM_FAKE_MODE: "degrade" | "success"


    # ≈≈≈≈ Public method ≈≈≈≈
    def load(self, force: bool = False) -> None:
        """
        Initialize the Anthropic client and freeze LLM settings on this instance.

        _call() must not read current_app; Celery/background tasks may not have a Flask
        app context available.
        """
        if self._client is not None and not force:
            return

        self._model = current_app.config.get("LLM_MODEL", DEFAULT_LLM_MODEL)
        self._fake = bool(current_app.config.get("LLM_FAKE", False))
        self._fake_mode = (current_app.config.get("LLM_FAKE_MODE") or "degrade").strip().lower()

        if self._fake:
            # Deterministic offline mode for the live-worker test — no key, no calls.
            self._client = anthropic.Anthropic(api_key="fake-llm-api-key")
            logger.info(
                "LLM service initialized in FAKE mode (mode=%s, no external calls)",
                self._fake_mode,
            )
            return

        api_key = current_app.config.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("Anthropic API key not set")
            raise ValueError("ANTHROPIC_API_KEY must be set.")

        # Explicit per-call timeouts so a hung request raises APITimeoutError instead
        # of stalling the task. max_retries=0 → the Celery task is the sole retry
        # authority, keeping per-call time bounded/predictable.
        timeout = httpx.Timeout(
            current_app.config.get("LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS),
            connect=current_app.config.get("LLM_CONNECT_TIMEOUT_SECONDS", DEFAULT_LLM_CONNECT_TIMEOUT_SECONDS),
        )
        max_retries = int(current_app.config.get("LLM_MAX_RETRIES", DEFAULT_LLM_MAX_RETRIES))

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)
        logger.info(
            "LLM service initialized%s (timeout=%ss connect=%ss max_retries=%s)",
            " (forced reload)" if force else "",
            timeout.read,
            timeout.connect,
            max_retries
        )


    @property
    def is_loaded(self) -> bool:
        """Check if client is ready."""
        return self._client is not None


    # ≈≈≈≈ Core flow ≈≈≈≈
    def extract_interview_details(
            self, raw_text: str, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Extract structured interview details from raw email text.
        Always returns complete schema — missing fields are None.
        """
        prompt = f"""You are a strict information extraction system.

Extract interview details from the email below.

OUTPUT RULES:
- Return ONLY a JSON object
- No markdown, no explanation, no extra text
- Every key must appear exactly once
- Use null for any missing value — never omit a key
- Use double quotes for all strings

REQUIRED KEYS (all 7 must be present):
{{
  "company_name": string or null,
  "role_title": string or null,
  "interview_stage": string or null,
  "interview_format": string or null,
  "date_text": string or null,
  "time_text": string or null,
  "location_text": string or null
}}

EMAIL:
{raw_text}"""

        response = self._call(prompt, max_tokens=300, idempotency_key=idempotency_key)
        parsed   = self._safe_json_load(response)

        # enforce schema — always return complete dict
        return {
            "company_name":     parsed.get("company_name"),
            "role_title":       parsed.get("role_title"),
            "interview_stage":  parsed.get("interview_stage"),
            "interview_format": parsed.get("interview_format"),
            "date_text":        parsed.get("date_text"),
            "time_text":        parsed.get("time_text"),
            "location_text":    parsed.get("location_text"),
        }

    def generate_preparation_guidance(
        self,
        role_title:       Optional[str],
        company_name:     Optional[str],
        interview_format: Optional[str],
        job_field:        Optional[str],
    ) -> Optional[str]:
        """Generate preparation guidance tailored to the role and company."""

        prompt = f"""You are a career coach helping a job seeker prepare for an interview.

Role: {role_title or "Unknown"}
Company: {company_name or "Unknown"}
Format: {interview_format or "General"}
Field: {job_field or "General"}

Write a concise preparation guide including:
1. Key topics to review
2. Skills to demonstrate
3. Company research tips
4. Format-specific advice

Max 250 words. Be practical and specific."""

        return self._call(prompt, max_tokens=350)

    def suggest_candidate_questions(
        self,
        role_title:      Optional[str],
        company_name:    Optional[str],
        interview_stage: Optional[str],
        job_field:       Optional[str],
    ) -> Optional[str]:
        """Suggest 5 smart questions the candidate should ask the interviewer."""

        prompt = f"""Generate 5 strong questions a candidate should ask during an interview.

Role: {role_title or "Unknown"}
Company: {company_name or "Unknown"}
Stage: {interview_stage or "General"}
Field: {job_field or "General"}

Rules:
- Be specific to the role and stage
- No introduction or conclusion
- Numbered list only"""

        return self._call(prompt, max_tokens=250)

    def generate_reply_suggestions(
        self,
        raw_text:     str,
        category:     Optional[str],
        role_title:   Optional[str],
        company_name: Optional[str],
    ) -> Optional[str]:
        """Generate a professional draft reply for the candidate to send."""

        prompt = f"""Write a professional email reply for a job seeker.

Category: {category or "interview related"}
Role: {role_title or "the role"}
Company: {company_name or "the company"}

Original email:
{raw_text}

Rules:
- Confirm receipt
- Show enthusiasm
- If interview invitation — confirm or ask for scheduling
- No placeholder names like [your name]
- No signature block
- Max 120 words."""

        return self._call(prompt, max_tokens=250)

    def generate_role_summary(
        self,
        role_title:   Optional[str],
        company_name: Optional[str],
        job_field:    Optional[str],
        raw_text:     str,
    ) -> Optional[str]:
        """Generate a 2-3 sentence summary of the role."""

        prompt = f"""Summarize this role based on the email below.

Role: {role_title or "Unknown"}
Company: {company_name or "Unknown"}
Field: {job_field or "General"}

Email:
{raw_text}

Write exactly 2-3 sentences covering:
- Role responsibilities
- Company type
- What the candidate should expect."""

        return self._call(prompt, max_tokens=180)

    def generate_archive_summary(
        self,
        role_title:   Optional[str],
        company_name: Optional[str],
        category:     Optional[str],
        raw_text:     str,
    ) -> Optional[str]:
        """
        Generate a one-line summary for the archive list view.
        Example: 'Interview invitation from Google for Software Engineer role.'
        """

        prompt = f"""Write a single sentence archive summary for this email.

Role: {role_title or "Unknown"}
Company: {company_name or "Unknown"}
Category: {category or "Unknown"}

Email:
{raw_text}

Rules:
- One sentence only
- Include company and role if available
- Include the email type (interview invitation, rejection, offer, etc.)
- Example: "Interview invitation from Google for Software Engineer role."
- End with a period."""

        return self._call(prompt, max_tokens=60)

    # ≈≈≈≈ Private methods ≈≈≈≈
    def _call(
            self, prompt: str, max_tokens: int = 300, idempotency_key: Optional[str] = None
    ) -> Optional[str]:
        """
        Make one LLM request.

        This method is Flask-context-free: it uses settings saved by load(), not current_app.
        It does not catch Anthropic exceptions; callers decide whether to retry, fail, or
        degrade gracefully. Returns None only when the response succeeds but contains no text.
        """
        if not self._client:
            logger.warning("LLM service not initialized")
            return None

        if self._fake:
            if self._fake_mode == "success":
                return _fake_success_payload(prompt)
            return f"[fake-llm] {prompt.strip()[:60]}"  # degrade (default)

        extra_headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None

        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            extra_headers=extra_headers,
        )
        if not response.content:
            return None
        texts = [
            block.text.strip()
            for block in response.content
            if hasattr(block, "text") and block.text.strip()
        ]
        return " ".join(texts) if texts else None


    def _safe_json_load(self, text: Optional[str]) -> Dict[str, Any]:
        """
        Safely extract and parse JSON from LLM response.

        Strategy:
        1. Try direct json.loads — cleanest case
        2. Walk character by character tracking brace depth
           — finds correct closing brace regardless of nested
           objects or extra text around the JSON block
        """
        if not text:
            return {}

        # strategy 1 — direct parse
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # strategy 2 — brace depth walking
        try:
            start = text.index("{")
            depth = 0
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1])
        except (ValueError, json.JSONDecodeError):
            pass

        logger.warning("JSON parsing failed: %s", text[:100])
        return {}


# ≈≈≈≈ singleton pattern ≈≈≈≈
llm_service = LLMService()
