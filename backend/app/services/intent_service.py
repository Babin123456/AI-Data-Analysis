"""Robust intent classification and routing service for AI chat.

Supports 5 explicit intent categories:
1. sql_query: Computation, filtering, aggregation, or retrieval from the active dataset.
2. dataset_explanation: Questions about columns, schema, data types, and dataset summary.
3. greeting: Conversational greetings, compliments, and bot capabilities.
4. off_topic: Topics completely unrelated to data analysis or the dataset.
5. clarification: Highly ambiguous requests that require additional user detail.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from app.models.schemas import ChatIntent
from app.services.schema_service import DatasetSchema
from app.validation.prompt_guard import is_prompt_injection

logger = logging.getLogger(__name__)


# Common conversational greeting phrases
_GREETINGS_MAP: dict[str, str] = {
    "hi": "Hello! 👋 How can I help you analyze your data today?",
    "hello": "Hello! 👋 Feel free to ask any question about your uploaded dataset.",
    "hey": "Hey there! 👋 What insights would you like to explore in your data?",
    "hola": "¡Hola! How can I assist with your data analysis today?",
    "good morning": "Good morning! ☀️ Ready to analyze your data?",
    "good afternoon": "Good afternoon! How can I help with your data today?",
    "good evening": "Good evening! Let me know what data questions you have.",
    "how are you": "I'm doing great, thank you! How can I assist you with your data today?",
    "how r u": "I'm doing well! How can I help you with your dataset?",
    "who are you": "I am your AI Data Analysis assistant. I can query your data, summarize metrics, explain columns, and generate tables and charts for you!",
    "what can you do": "You can ask me questions about your uploaded dataset in natural language, and I'll generate SQL queries, fetch results, and build charts for you!",
    "help": "You can ask me questions about your dataset, such as: 'Show top 10 records', 'Which category has the highest sales?', or 'What columns are available?'.",
    "thanks": "You're very welcome! Let me know if you have any more questions about your data.",
    "thank you": "You're welcome! Happy to help with your data analysis.",
    "thank u": "You're welcome! Let me know if you need anything else.",
    "thx": "You're welcome!",
    "bye": "Goodbye! Have a great day and feel free to return whenever you need data insights.",
    "goodbye": "Goodbye! Feel free to upload more data or ask more questions anytime.",
}

# Regex patterns for fast rule-based classification
_OFF_TOPIC_PATTERNS = [
    r"^(tell me a joke|tell a joke|tell me a story)",
    r"\b(weather in|forecast for|current weather)\b",
    r"\b(recipe for|how to cook|bake a cake)\b",
    r"\b(who won the world cup|who is the president of|capital of)\b",
    r"\b(write a python script to scrape|write an essay on|write a poem about)\b",
]

_DATASET_EXPLANATION_PATTERNS = [
    r"\b(what columns|which columns|list columns|available columns|column names|show columns)\b",
    r"\b(dataset schema|table schema|database schema|data types|what data types)\b",
    r"\b(explain (the )?(dataset|data|table|schema))\b",
    r"\b(describe (the )?(dataset|data|table|schema|columns))\b",
    r"\b(overview of (the )?(dataset|data|table))\b",
    r"\b(how many columns|what is the structure of)\b",
    r"\b(what tables (are there|exist|are available)|list tables)\b",
]

_SQL_QUERY_PATTERNS = [
    r"\b(show|find|list|get|display|calculate|compute|give me)\b",
    r"\b(top|bottom|highest|lowest|maximum|minimum|max|min|average|avg|sum|total|count)\b",
    r"\b(how many|how much|percentage|distribution|trend|group by|order by|filter|where)\b",
    r"\b(select|from|where|having|join|limit)\b",
    r"\b(median|percentile|outliers|variance|standard deviation|stddev)\b",
]

_CLARIFICATION_PATTERNS = [
    r"^(why|how|filter it|filter that|sort it|sort that|do it|check it|compare them|more|details)\??$",
    r"^(better|which is better|is it good|is that right|what else)\??$",
]


class IntentService:
    """Classifies user messages and returns (intent, precomputed_reply_or_empty)."""

    def classify_fast(
        self,
        question: str,
        has_prior_context: bool = False,
    ) -> tuple[ChatIntent, str] | None:
        """Fast regex & dictionary matcher with zero LLM overhead."""
        clean = question.strip().lower()
        if not clean:
            return ("clarification", "Please enter a question about your dataset.")

        # Check injection guard
        is_inj, reason = is_prompt_injection(question)
        if is_inj:
            return ("off_topic", "I can only help you analyze and query your uploaded data.")

        # 1. Greetings lookup
        if clean in _GREETINGS_MAP:
            return ("greeting", _GREETINGS_MAP[clean])

        if len(clean) <= 30:
            for g_key, g_reply in _GREETINGS_MAP.items():
                if clean == g_key or clean.startswith(f"{g_key} ") or clean.endswith(f" {g_key}"):
                    return ("greeting", g_reply)

        # 2. Dataset explanation queries
        for pat in _DATASET_EXPLANATION_PATTERNS:
            if re.search(pat, clean, re.IGNORECASE):
                return ("dataset_explanation", "")

        # 3. Off-topic queries
        for pat in _OFF_TOPIC_PATTERNS:
            if re.search(pat, clean, re.IGNORECASE):
                return (
                    "off_topic",
                    "I am an AI Data Analyst specialized in querying and exploring your uploaded datasets. "
                    "Please ask a question related to your data.",
                )

        # 4. Ambiguous queries requiring clarification (only if no conversation context to anchor them)
        if not has_prior_context:
            for pat in _CLARIFICATION_PATTERNS:
                if re.match(pat, clean, re.IGNORECASE):
                    return (
                        "clarification",
                        "Could you please specify which columns or criteria you would like to analyze?",
                    )

        # 5. Clear data queries
        for pat in _SQL_QUERY_PATTERNS:
            if re.search(pat, clean, re.IGNORECASE):
                return ("sql_query", "")

        return None

    def classify_semantic(
        self,
        question: str,
        schemas: list[DatasetSchema],
        has_prior_context: bool = False,
    ) -> tuple[ChatIntent, str]:
        """Classify ambiguous intent using LLM with strict JSON schema."""
        from app.services.llm_service import llm_service

        col_names = []
        for s in schemas:
            col_names.extend([c.name for c in s.columns[:15]])
        sample_cols = ", ".join(col_names[:25])

        prompt = (
            "You are an intent classifier for an AI Data Analysis tool.\n"
            f"Available dataset columns: [{sample_cols}]\n\n"
            f"User Question: \"{question}\"\n"
            f"Has prior conversation context: {has_prior_context}\n\n"
            "Classify the user question into exactly ONE of these categories:\n"
            "1. sql_query: The user wants to retrieve, filter, sort, aggregate, or compute data from the dataset.\n"
            "2. dataset_explanation: The user is asking about the structure, columns, data types, or summary of the dataset.\n"
            "3. greeting: Greetings, pleasantries, small talk, or asking what the bot does.\n"
            "4. off_topic: Questions completely unrelated to data analysis or the dataset (e.g., weather, cooking, world facts).\n"
            "5. clarification: Extremely vague or ambiguous inputs where the requested analysis cannot be determined even with context.\n\n"
            "IMPORTANT RULES:\n"
            "- A dataset question must NEVER be classified as off_topic.\n"
            "- If the question references any column or requests numbers/trends/filtering, classify as sql_query.\n\n"
            "Respond ONLY with a JSON object in this format:\n"
            "{\"intent\": \"sql_query\" | \"dataset_explanation\" | \"greeting\" | \"off_topic\" | \"clarification\", \"reply\": \"string or empty\"}"
        )

        try:
            response = llm_service._create_completion(
                messages=[
                    {"role": "system", "content": "You are a precise intent classification engine."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=150,
            )
            raw = (response.choices[0].message.content or "").strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                intent = str(data.get("intent", "sql_query")).lower()
                reply = str(data.get("reply", ""))
                valid_intents = {"sql_query", "dataset_explanation", "greeting", "off_topic", "clarification"}
                if intent in valid_intents:
                    return (intent, reply)  # type: ignore
        except Exception as exc:
            logger.warning("LLM semantic classification failed: %s. Defaulting to sql_query.", exc)

        return ("sql_query", "")

    def classify(
        self,
        question: str,
        schemas: list[DatasetSchema],
        has_prior_context: bool = False,
    ) -> tuple[ChatIntent, str]:
        """Classify user intent via fast rules first, and LLM fallback when ambiguous."""
        fast = self.classify_fast(question, has_prior_context=has_prior_context)
        if fast is not None:
            return fast

        return self.classify_semantic(question, schemas, has_prior_context=has_prior_context)


intent_service = IntentService()
