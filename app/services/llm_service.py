import logging
import json
from typing import Optional, Dict, Any

from flask import current_app
import anthropic

logger = logging.getLogger(__name__)


class LLMService:
    """
    Handles all LLM-based features using the Anthropic API.

    Design principle:
        LLM = text intelligence only
        Backend = logic, rules, DB, workflows
    """

    def __init__(self):
        self._client: Optional[anthropic.Anthropic] = None

    # ≈≈≈≈ Public method ≈≈≈≈
    def load(self, force: bool = False) -> None:
        """
        Initialize Anthropic client once.
        Use force=True to reload if API key changes.
        """
        if self._client is not None and not force:
            return

        api_key = current_app.config.get("ANTHROPIC_API_KEY")

        if not api_key:
            logger.error("Anthropic API key not set")
            raise ValueError("ANTHROPIC_API_KEY must be set.")

        self._client = anthropic.Anthropic(api_key=api_key)
        logger.info(
            "LLM service initialized%s",
            " (forced reload)" if force else ""
        )

    @property
    def is_loaded(self) -> bool:
        """Check if client is ready."""
        return self._client is not None

    # ≈≈≈≈ Core flow ≈≈≈≈
    def extract_interview_details(self, raw_text: str) -> Dict[str, Any]:
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

        response = self._call(prompt, max_tokens=300)
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
    def _call(self, prompt: str, max_tokens: int = 300) -> Optional[str]:
        """Single LLM call with full error handling."""
        if not self._client:
            logger.warning("LLM service not initialized")
            return None

        try:
            model = current_app.config.get(
                "LLM_MODEL",
                "claude-haiku-4-5-20251001"
            )

            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )

            if not response.content:
                return None

            # collect all text blocks — not just first
            texts = [
                block.text.strip()
                for block in response.content
                if hasattr(block, "text") and block.text.strip()
            ]

            return " ".join(texts) if texts else None

        except anthropic.APIConnectionError:
            logger.error("LLM connection error")
        except anthropic.RateLimitError:
            logger.error("LLM rate limit hit")
        except anthropic.APIStatusError as e:
            logger.error("LLM API error: %s", e.status_code)
        except Exception:
            logger.exception("Unexpected LLM error")

        return None

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