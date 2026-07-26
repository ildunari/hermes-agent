"""Auto-generate short session titles from the early conversation transcript.

Runs asynchronously after a response is delivered so it never adds latency to
the user-facing reply.
"""

import logging
import re
import threading
from typing import Any, Callable, Optional

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]
RuntimeValidator = Callable[[], bool]

def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", (text or "").casefold())


def _content_text(content: Any) -> str:
    """Return human-visible text from persisted chat content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
                continue
            image_url = item.get("image_url")
            if image_url:
                parts.append("[image]")
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        if content.get("image_url"):
            return "[image]"
    return ""


def _first_user_text(conversation_history: list | None) -> str:
    for message in conversation_history or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = _content_text(message.get("content"))
        if content:
            return content
    return ""


def _user_texts(conversation_history: list | None, user_message: Any) -> list[str]:
    texts: list[str] = []
    for message in conversation_history or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text(message.get("content"))
        if text:
            texts.append(text)
    current = _content_text(user_message)
    if current:
        texts.append(current)
    return texts


def _trim_snippet(text: str, limit: int) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + "…"


def _title_transcript_context(
    user_message: Any,
    assistant_response: str,
    conversation_history: list | None = None,
    *,
    max_messages: int = 16,
    per_message_chars: int = 1200,
    max_chars: int = 12000,
) -> str:
    """Build a compact early transcript for title generation.

    The titler may run again during the first few turns after a bad/empty title.
    Use the real early transcript rather than only the latest user/assistant
    pair, otherwise vague openers like "What's going on?" become permanent
    titles even after the session's topic is obvious.
    """
    raw_messages = list(conversation_history or [])
    if not raw_messages:
        raw_messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_response},
        ]

    entries: list[str] = []
    seen: set[tuple[str, str]] = set()
    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        # Assistant messages that contain tool_calls are usually planning /
        # progress preambles ("I'll inspect...") rather than the substantive
        # answer. Tool *results* are role=tool and already excluded. Skipping
        # these preambles keeps early tool-heavy turns from crowding out the
        # user's actual task and the final assistant answer.
        if role == "assistant" and message.get("tool_calls"):
            continue
        text = _content_text(message.get("content"))
        if role == "user":
            text = _summarize_user_message(text)
        text = _trim_snippet(text, per_message_chars)
        if not text:
            continue
        key = (str(role), text)
        if key in seen:
            continue
        seen.add(key)
        label = "User" if role == "user" else "Assistant"
        entries.append(f"{label}: {text}")
        if len(entries) >= max_messages:
            break

    context = "\n\n".join(entries)
    if len(context) > max_chars:
        context = context[: max(0, max_chars - 1)].rstrip() + "…"
    return context


_GENERIC_PROMPT_FRAGMENT_TITLES = {
    "can you",
    "can you check",
    "help me",
    "what going",
    "what's going",
    "what is going",
}


def _title_words_in_prompt_window(title_words: list[str], prompt_words: list[str]) -> bool:
    if not title_words or not prompt_words or title_words[0] != prompt_words[0]:
        return False
    pos = 0
    for word in prompt_words:
        if pos < len(title_words) and word == title_words[pos]:
            pos += 1
    return pos == len(title_words)


def _is_prompt_fragment_title(title: str, *candidate_prompts: str) -> bool:
    """Return True when a title is just a first-prompt fragment.

    Auto-title sometimes runs after an uninformative first assistant turn
    (for example a progress update) and the auxiliary model parrots the user's
    opening words: "Can you check if...". Those titles are not user intent and
    should be replaceable by a better early-turn title. Real manual titles like
    "Hermes cleanup" should continue to be preserved.
    """
    clean_title = " ".join((title or "").split())
    title_casefold = clean_title.casefold()
    title_words = _words(clean_title)
    if len(title_words) < 2:
        return False

    if len(clean_title) < 8 and title_casefold not in _GENERIC_PROMPT_FRAGMENT_TITLES:
        return False
    if len(clean_title) > 80:
        return False

    for prompt in candidate_prompts:
        clean_prompt = " ".join((prompt or "").split())
        if not clean_prompt:
            continue

        if clean_prompt.casefold().startswith(title_casefold):
            return True

        prompt_words = _words(clean_prompt)[:16]
        if _title_words_in_prompt_window(title_words, prompt_words):
            return True

    return False

_TITLE_PROMPT_CORE = (
    "Generate a short, specific session label (3-6 words) for this early conversation transcript. "
    "The label appears in a sidebar/history list, so optimize for helping the user recognize which "
    "session this is next week. Describe the concrete thing being worked on: object + action, bug, "
    "decision, or outcome. Prefer plain user-facing words over internal implementation jargon. "
    "If the opening user message is vague, infer the real work from later turns. Do NOT derive the "
    "label from generic opening words such as 'what going', 'can you check', 'help me', or "
    "'goal implement'. Avoid vague labels such as 'Dynamic Tool Summary Automation', "
    "'Hermes Pre-Prompt Memory Injection', 'Casual Greeting to Kosta', or "
    "'Carlos Extraction and Setup Review'; write what the user would actually look for instead. "
)

_TITLE_PROMPT = (
    _TITLE_PROMPT_CORE
    + "Write the label in the same language the user is writing in. "
    + "Return ONLY the label text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

_TITLE_PROMPT_PINNED_LANGUAGE = (
    _TITLE_PROMPT_CORE
    + "Write the label in {language}. "
    + "Return ONLY the label text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

_TITLE_JARGON_REWRITES = (
    (re.compile(r"\bpre[- ]prompt\s+memory\s+injection\b", re.I), "memory prompt setup"),
    (re.compile(r"\bdynamic\s+tool\s+summary\s+automation\b", re.I), "tool summary job"),
    (re.compile(r"\bcasual\s+greeting\s+to\s+[^,;:]+", re.I), "quick greeting"),
    (re.compile(r"\bextraction\s+and\s+setup\s+review\b", re.I), "setup review"),
)


def _clean_generated_title(title: str) -> str:
    """Normalize common LLM title shapes into user-readable list labels."""
    # A title is one line. If the model answers the prompt instead of obeying
    # "return ONLY the label", keep the first non-empty line rather than joining
    # a shell transcript or bulleted plan into one long pseudo-title.
    first_line = next((line.strip() for line in (title or "").splitlines() if line.strip()), "")
    clean = " ".join(first_line.split()).strip('"\'')
    lower = clean.lower()
    for prefix in ("title:", "label:", "session label:"):
        if lower.startswith(prefix):
            clean = clean[len(prefix):].strip()
            lower = clean.lower()
            break
    for pattern, replacement in _TITLE_JARGON_REWRITES:
        clean = pattern.sub(replacement, clean)
    return clean.strip(" .!?;:—-")


def _title_language() -> str:
    """Return configured title language, or empty string to match the user."""
    try:
        from hermes_cli.config import load_config

        return str(
            ((load_config() or {}).get("auxiliary") or {})
            .get("title_generation", {})
            .get("language", "")
        ).strip()
    except Exception:
        return ""


def _auto_title_enabled() -> bool:
    """Return whether automatic session title generation is enabled."""
    try:
        # Lazy imports, matching _title_language(): title_generator is imported
        # from agent code paths where a module-level hermes_cli import risks
        # circularity, and the read-only loader avoids config-migration writes.
        from hermes_cli.config import load_config_readonly
        from utils import is_truthy_value

        config = load_config_readonly()
        title_config = (config.get("auxiliary") or {}).get("title_generation") or {}
        return is_truthy_value(title_config.get("enabled"), default=True)
    except Exception:
        logger.debug("Failed to read title_generation.enabled", exc_info=True)
        return True


def _summarize_user_message(user_message: str) -> str:
    """Collapse a slash-skill-expanded turn back to what the user typed.

    A ``/skill`` invocation expands into a message that embeds the whole skill
    body, so feeding it to the titler verbatim titles the session after the
    *skill's* prose — "Kick off a task in a fresh isolated git worktree" — not
    after the user's request. Reuse the canonical scaffolding parser so the
    model sees ``/work — fix the title leak`` instead.
    """
    if not user_message:
        return ""
    try:
        from agent.skill_commands import describe_skill_invocation

        described = describe_skill_invocation(user_message)
    except Exception:
        logger.debug("Skill-scaffolding summary failed; titling raw", exc_info=True)
        return user_message
    return described if described is not None else user_message


def generate_title(
    user_message: Any,
    assistant_response: str,
    timeout: Optional[float] = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    conversation_history: list | None = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> Optional[str]:
    """Generate a session title from the compact early transcript.

    Uses the main runtime's model when available, falling back to the
    auxiliary LLM client (cheapest/fastest available model).
    Returns the title string or None on failure.

    ``failure_callback`` is invoked with ``(task, exception)`` when the
    auxiliary call raises — the caller typically wires this to
    ``AIAgent._emit_auxiliary_failure`` so the user sees a warning instead
    of silently accumulating untitled sessions.

    ``runtime_validator`` is called right before the LLM request. If it
    returns False (e.g. the user's model was switched since the background
    thread captured its runtime snapshot), the call is skipped silently —
    no request is sent, so a stale title request can't reload a model the
    runtime already unloaded (#19027).
    """
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return None

    if runtime_validator is not None:
        try:
            if not runtime_validator():
                logger.debug("Title generation skipped: runtime validator returned False")
                return None
        except Exception:
            # Fail open: a broken validator must not disable titling.
            logger.debug("Title runtime validator raised; proceeding", exc_info=True)

    transcript_context = _title_transcript_context(
        user_message,
        assistant_response,
        conversation_history,
    )

    language = _title_language()
    prompt = _TITLE_PROMPT_PINNED_LANGUAGE.format(language=language) if language else _TITLE_PROMPT

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Transcript:\n{transcript_context}"},
    ]

    try:
        response = call_llm(
            task="title_generation",
            messages=messages,
            max_tokens=500,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        content = response.choices[0].message.content or ""
        # Strip thinking/reasoning blocks that think-enabled models
        # (MiniMax M2.7, DeepSeek, etc.) emit even for simple prompts like
        # title generation. Without this the raw <think>...</think> XML
        # leaks into session titles. Reuses the canonical scrubber so all
        # tag variants (unterminated blocks, orphan closes, mixed case)
        # are handled, not just a single literal <think> pair.
        from agent.agent_runtime_helpers import strip_think_blocks
        title = strip_think_blocks(None, content).strip()
        # Clean up: remove quotes, trailing punctuation, prefixes like "Title: ",
        # and common internal-jargon labels that are hard to scan in session lists.
        title = _clean_generated_title(title)
        # Enforce reasonable length
        if len(title) > 80:
            title = title[:77] + "..."
        if _is_prompt_fragment_title(title, *_user_texts(conversation_history, user_message)):
            logger.debug("Rejected prompt-fragment session title: %s", title)
            return None
        return title if title else None
    except Exception as e:
        # Log at WARNING so this shows up in agent.log without debug mode.
        # Full detail at debug level for operators who need the stack.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def _persist_session_title(session_db, session_id, title):
    """Persist a generated title, recovering from duplicate-title collisions.

    The write goes through ``set_auto_title_if_empty`` (predicate + write in
    one transaction) so a manual ``/title`` set while LLM generation was in
    flight is never overwritten — a plain ``set_session_title`` fallback keeps
    older stores working. ``set_session_title`` raises ValueError when the
    title would collide with another session (the unique-title index). Rather
    than swallow it and leave the session untitled (#50537), append a #N
    suffix via get_next_title_in_lineage() when the store supports lineage
    dedup; otherwise re-raise so the caller can decide.

    Returns the title actually persisted, or None when a concurrent manual
    title won the race (nothing was written).
    """
    atomic_fn = getattr(session_db, "set_auto_title_if_empty", None)

    def _set(t):
        if callable(atomic_fn):
            atomic_result = atomic_fn(session_id, t)
            if isinstance(atomic_result, bool):
                if not atomic_result:
                    # Predicate failed: a title appeared while generation was
                    # in flight (manual /title wins), or the session vanished.
                    logger.debug(
                        "Skipping auto-generated session title because a title "
                        "was set while generation was in flight"
                    )
                    return None
                return t
            # Dynamic proxy objects can fabricate a callable attribute for a
            # method the backing store does not implement. Only the documented
            # boolean result proves the atomic API is real; otherwise fall back
            # to the legacy setter below.
        ok = session_db.set_session_title(session_id, t)
        if ok is False:
            raise RuntimeError(
                f"session {session_id} not found when storing title"
            )
        return t

    try:
        return _set(title)
    except ValueError:
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        return _set(deduped)


def auto_title_session(
    session_db,
    session_id: str,
    user_message: Any,
    assistant_response: str,
    conversation_history: list | None = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:
    - session_db is None
    - session already has a title (user-set or previously auto-generated)
    - title generation fails
    - runtime_validator returns False (model was switched)

    Never lets an exception escape: this is a daemon-thread target, and an
    escaping exception would spray a raw traceback into the user's terminal
    via the default threading excepthook. The canonical trigger is the
    post-``hermes update`` stale-module window, where this function's lazy
    imports read NEW source from disk while already-cached modules
    (``agent.portal_tags`` etc.) are still the OLD version — the resulting
    ImportError repeats on every auto-title attempt until the long-running
    process restarts.
    """
    try:
        _auto_title_session(
            session_db,
            session_id,
            user_message,
            assistant_response,
            failure_callback=failure_callback,
            main_runtime=main_runtime,
            title_callback=title_callback,
            conversation_history=conversation_history,
            runtime_validator=runtime_validator,
        )
    except Exception as e:
        # WARNING (not debug) so operators see it in agent.log; the message
        # names the likely cause so "restart the process" is discoverable.
        logger.warning(
            "Auto-title failed (harmless; if this started after an update, "
            "restart the running Hermes process): %s",
            e,
        )
        logger.debug("Auto-title traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Auto-title failure_callback raised", exc_info=True)


def _auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list | None = None,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Body of :func:`auto_title_session` — see its docstring."""
    if not session_db or not session_id:
        return

    # Preserve real titles (especially /title and /new <title>), but let the
    # early retry window replace the common bad auto-title shape where the title
    # is merely a fragment of the first user prompt.
    try:
        existing = session_db.get_session_title(session_id)
        if existing and not _is_prompt_fragment_title(
            existing,
            _first_user_text(conversation_history),
            user_message,
        ):
            return
    except Exception:
        return

    # This runs on a bare daemon thread spawned AFTER the turn's ambient
    # conversation context was reset, so publish it here from the session id
    # we already hold — the title-generation LLM call then carries the same
    # ``conversation=`` Portal tag as the turn it titles. Root-of-lineage for
    # consistency with the agent loop (a no-op on first exchange, where
    # titling happens, but correct if this ever runs on a continuation).
    from agent.aux_accounting import set_accounting_context
    from agent.portal_tags import set_conversation_context

    conversation_id = session_id
    try:
        conversation_id = session_db.get_conversation_root(session_id) or session_id
    except Exception:
        pass
    set_conversation_context(conversation_id)
    # Same for the accounting context, so the title call's token usage is
    # recorded against this session (task='title_generation', #23270).
    set_accounting_context(session_db, session_id)

    title = generate_title(
        user_message,
        assistant_response,
        failure_callback=failure_callback,
        main_runtime=main_runtime,
        conversation_history=conversation_history,
        runtime_validator=runtime_validator,
    )
    if not title:
        return

    try:
        persisted = _persist_session_title(session_db, session_id, title)
        if persisted is None:
            return
        logger.debug("Auto-generated session title: %s", persisted)
        if title_callback is not None:
            try:
                title_callback(persisted)
            except Exception:
                logger.debug("Auto-title callback failed", exc_info=True)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: Any,
    assistant_response: str,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
    runtime_validator: Optional[RuntimeValidator] = None,
) -> None:
    """Fire-and-forget title generation after the first exchange.

    Only generates a title when:
    - This appears to be the first user→assistant exchange
    - No title is already set
    """
    if not session_db or not session_id or not user_message or not assistant_response:
        return

    # Count user messages in history to detect early exchanges.
    # conversation_history includes the exchange that just happened. Be
    # generous enough to survive a transient auxiliary-provider failure: keep
    # retrying while the session is still young and untitled, but do not keep
    # firing on long-running sessions.
    user_msg_count = sum(1 for m in (conversation_history or []) if m.get("role") == "user")
    if user_msg_count > 4:
        return

    # Config read comes after the cheap first-exchange guard so the file
    # isn't touched on every subsequent turn of a long session.
    if not _auto_title_enabled():
        logger.debug("Auto-title skipped: auxiliary.title_generation.enabled=false")
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "conversation_history": conversation_history,
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
            "runtime_validator": runtime_validator,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()
