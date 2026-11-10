"""
Tests for supporting logic in the Bubble daemon.

Covers pure utility functions and instance methods that don't touch
D-Bus, HTTP, or threading — the kind of logic that's easy to break
silently when refactoring.
"""

import pytest
from conftest import bd


# ---------------------------------------------------------------------------
# Time formatting utilities
# ---------------------------------------------------------------------------

class TestFormatElapsed:
    def test_seconds(self):
        assert bd.BubbleDaemon._format_elapsed(0) == "0s"
        assert bd.BubbleDaemon._format_elapsed(30) == "30s"
        assert bd.BubbleDaemon._format_elapsed(59) == "59s"

    def test_minutes(self):
        assert bd.BubbleDaemon._format_elapsed(60) == "1m 0s"
        assert bd.BubbleDaemon._format_elapsed(125) == "2m 5s"
        assert bd.BubbleDaemon._format_elapsed(599) == "9m 59s"

    def test_hours(self):
        assert bd.BubbleDaemon._format_elapsed(3600) == "1h 0m"
        assert bd.BubbleDaemon._format_elapsed(7260) == "2h 1m"

    def test_days(self):
        assert bd.BubbleDaemon._format_elapsed(86400) == "1d 0h"
        assert bd.BubbleDaemon._format_elapsed(172800) == "2d 0h"

    def test_negative_clamped_to_zero(self):
        assert bd.BubbleDaemon._format_elapsed(-10) == "0s"


class TestDayPhase:
    def test_early_morning(self):
        assert bd.BubbleDaemon._day_phase(5) == "early morning"
        assert bd.BubbleDaemon._day_phase(8) == "early morning"

    def test_morning(self):
        assert bd.BubbleDaemon._day_phase(9) == "morning"
        assert bd.BubbleDaemon._day_phase(11) == "morning"

    def test_afternoon(self):
        assert bd.BubbleDaemon._day_phase(12) == "afternoon"
        assert bd.BubbleDaemon._day_phase(16) == "afternoon"

    def test_evening(self):
        assert bd.BubbleDaemon._day_phase(17) == "evening"
        assert bd.BubbleDaemon._day_phase(20) == "evening"

    def test_night(self):
        assert bd.BubbleDaemon._day_phase(21) == "night"
        assert bd.BubbleDaemon._day_phase(4) == "night"

    def test_midnight(self):
        assert bd.BubbleDaemon._day_phase(0) == "night"
        assert bd.BubbleDaemon._day_phase(1) == "night"


class TestTempoLabel:
    def test_rapid(self):
        assert bd.BubbleDaemon._tempo_label(30) == "rapid"
        assert bd.BubbleDaemon._tempo_label(89) == "rapid"

    def test_steady(self):
        assert bd.BubbleDaemon._tempo_label(90) == "steady"
        assert bd.BubbleDaemon._tempo_label(299) == "steady"

    def test_reflective(self):
        assert bd.BubbleDaemon._tempo_label(300) == "reflective"
        assert bd.BubbleDaemon._tempo_label(1199) == "reflective"

    def test_sporadic(self):
        assert bd.BubbleDaemon._tempo_label(1200) == "sporadic"
        assert bd.BubbleDaemon._tempo_label(3600) == "sporadic"


# ---------------------------------------------------------------------------
# Relevance & scoring
# ---------------------------------------------------------------------------

class TestRelevanceTerms:
    def test_basic_extraction(self):
        terms = bd.BubbleDaemon._relevance_terms("I like pizza and pasta")
        # "and" is 3 chars so it passes the minimum length filter
        assert terms == {"and", "like", "pizza", "pasta"}

    def test_ignores_short_tokens(self):
        terms = bd.BubbleDaemon._relevance_terms("I am Bob")
        assert "I" not in terms
        assert "am" not in terms
        assert "bob" in terms

    def test_underscore_preserved(self):
        terms = bd.BubbleDaemon._relevance_terms("user_identity")
        assert "user_identity" in terms

    def test_numeric_preserved(self):
        # Single digits are < 3 chars so filtered out; only words >= 3 chars survive
        terms = bd.BubbleDaemon._relevance_terms("version 2.0 beta")
        assert "version" in terms
        assert "beta" in terms
        assert "2" not in terms  # too short
        assert "0" not in terms  # too short

    def test_empty_returns_empty(self):
        assert bd.BubbleDaemon._relevance_terms("") == set()

    def test_punctuation_stripped(self):
        terms = bd.BubbleDaemon._relevance_terms("hello, world! How's it?")
        assert "hello" in terms
        assert "world" in terms
        assert "how" in terms
        assert "it" not in terms  # too short


class TestRecencyScore:
    def test_fresh(self):
        # Updated now → should score 8
        now = int(__import__("time").time())
        assert bd.BubbleDaemon._recency_score(str(now), now) == 8

    def test_one_day_old(self):
        now = int(__import__("time").time())
        # 24h ago → 8 - (24//24) = 7
        assert bd.BubbleDaemon._recency_score(str(now - 86400), now) == 7

    def test_one_week_old(self):
        now = int(__import__("time").time())
        # 7*24 = 168 hours → 8 - (168//24) = 1
        assert bd.BubbleDaemon._recency_score(str(now - 604800), now) == 1

    def test_very_old(self):
        now = int(__import__("time").time())
        # 100 days → clamped to 0
        assert bd.BubbleDaemon._recency_score(str(now - 8640000), now) == 0

    def test_invalid_timestamp(self):
        assert bd.BubbleDaemon._recency_score("not_a_number", 123) == 0

    def test_empty_string(self):
        assert bd.BubbleDaemon._recency_score("", 123) == 0


class TestSentimentStrengthScore:
    def test_positive(self):
        assert bd.BubbleDaemon._sentiment_strength_score("positive") == 4

    def test_negative(self):
        assert bd.BubbleDaemon._sentiment_strength_score("negative") == 4

    def test_very_positive(self):
        assert bd.BubbleDaemon._sentiment_strength_score("very_positive") == 4

    def test_very_negative(self):
        assert bd.BubbleDaemon._sentiment_strength_score("very_negative") == 4

    def test_strong(self):
        assert bd.BubbleDaemon._sentiment_strength_score("strong") == 4

    def test_curious(self):
        assert bd.BubbleDaemon._sentiment_strength_score("curious") == 2

    def test_excited(self):
        assert bd.BubbleDaemon._sentiment_strength_score("excited") == 2

    def test_concerned(self):
        assert bd.BubbleDaemon._sentiment_strength_score("concerned") == 2

    def test_mixed(self):
        assert bd.BubbleDaemon._sentiment_strength_score("mixed") == 2

    def test_neutral(self):
        assert bd.BubbleDaemon._sentiment_strength_score("neutral") == 0

    def test_unknown(self):
        assert bd.BubbleDaemon._sentiment_strength_score("wondering") == 1

    def test_empty(self):
        assert bd.BubbleDaemon._sentiment_strength_score("") == 0

    def test_none(self):
        assert bd.BubbleDaemon._sentiment_strength_score(None) == 0

    def test_case_insensitive(self):
        assert bd.BubbleDaemon._sentiment_strength_score("POSITIVE") == 4
        assert bd.BubbleDaemon._sentiment_strength_score("Curious") == 2


class TestUserIdentityScore:
    def test_named_identity(self):
        assert bd.BubbleDaemon._user_identity_score("Casey") == 2

    def test_anything_non_anonymous(self):
        assert bd.BubbleDaemon._user_identity_score("the_user") == 2

    def test_anonymous(self):
        assert bd.BubbleDaemon._user_identity_score("anonymous") == 0

    def test_anon(self):
        assert bd.BubbleDaemon._user_identity_score("anon") == 0

    def test_unknown(self):
        assert bd.BubbleDaemon._user_identity_score("unknown") == 0

    def test_guest(self):
        assert bd.BubbleDaemon._user_identity_score("guest") == 0

    def test_empty(self):
        assert bd.BubbleDaemon._user_identity_score("") == 0

    def test_none(self):
        assert bd.BubbleDaemon._user_identity_score(None) == 0

    def test_case_insensitive(self):
        assert bd.BubbleDaemon._user_identity_score("ANONYMOUS") == 0


# ---------------------------------------------------------------------------
# Vector math
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert bd.BubbleDaemon._cosine_similarity([1, 2], [1, 2]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert bd.BubbleDaemon._cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_opposite(self):
        assert bd.BubbleDaemon._cosine_similarity([1, 0], [-1, 0]) == -1.0

    def test_zero_vector_a(self):
        assert bd.BubbleDaemon._cosine_similarity([0, 0], [1, 2]) == 0.0

    def test_zero_vector_b(self):
        assert bd.BubbleDaemon._cosine_similarity([1, 2], [0, 0]) == 0.0

    def test_both_zero(self):
        assert bd.BubbleDaemon._cosine_similarity([0, 0], [0, 0]) == 0.0

    def test_empty(self):
        assert bd.BubbleDaemon._cosine_similarity([], []) == 0.0

    def test_different_lengths(self):
        # Uses min length (2) for comparison
        result = bd.BubbleDaemon._cosine_similarity([1, 2, 3], [1, 2])
        assert result == pytest.approx(1.0)

    def test_partial_overlap(self):
        a = [1.0, 1.0, 0.0]
        b = [1.0, 0.0, 1.0]
        sim = bd.BubbleDaemon._cosine_similarity(a, b)
        # Should be between 0 and 1
        assert 0.0 < sim < 1.0


# ---------------------------------------------------------------------------
# Second Voice utilities
# ---------------------------------------------------------------------------

class TestSecondVoiceProtocolHelpers:
    def test_extract_second_voice_with_focus(self):
        text = "Start [[BUBBLE:second_voice:stress-test this framing]] done"
        clean, req = bd.BubbleDaemon._extract_second_voice_request(text)
        assert req == "stress-test this framing"
        assert "second_voice" not in clean

    def test_extract_second_voice_without_focus(self):
        text = "Start [[BUBBLE:second_voice]] done"
        clean, req = bd.BubbleDaemon._extract_second_voice_request(text)
        assert req == ""
        assert "second_voice" not in clean

    def test_extract_second_voice_absent(self):
        text = "No marker"
        clean, req = bd.BubbleDaemon._extract_second_voice_request(text)
        assert clean == text
        assert req is None

    def test_strip_think_blocks(self):
        text = "before <think>hidden chain of thought</think> after"
        assert bd.BubbleDaemon._strip_think_blocks(text) == "before  after"

    def test_build_system_content_mentions_second_voice(self, daemon, monkeypatch):
        monkeypatch.setattr(daemon, "_second_voice_available", lambda: True)
        out = daemon._build_system_content(context={}, telemetry=None, message="")
        assert "Second Voice endpoint:" in out
        assert "available" in out

    def test_detect_second_voice_cutoff_no_marker(self):
        out, req, cut = bd.BubbleDaemon._detect_second_voice_cutoff("hello ", "world")
        assert out == "hello world"
        assert req is None
        assert cut is False

    def test_detect_second_voice_cutoff_with_focus_marker(self):
        out, req, cut = bd.BubbleDaemon._detect_second_voice_cutoff(
            "prefix ",
            "text [[BUBBLE:second_voice:review this]] trailing",
        )
        assert cut is True
        assert req == "review this"
        assert out == "prefix text"

    def test_detect_second_voice_cutoff_with_plain_marker(self):
        out, req, cut = bd.BubbleDaemon._detect_second_voice_cutoff(
            "alpha ",
            "beta [[BUBBLE:second_voice]] gamma",
        )
        assert cut is True
        assert req == ""
        assert out == "alpha beta"

    def test_second_voice_recent_context_uses_last_three_messages(self, daemon):
        daemon._history = [
            {"role": "system", "content": "ignore me"},
            {"role": "human", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "human", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
        out = daemon._second_voice_recent_context(limit_msgs=3, max_chars=1024)
        assert "first" not in out
        assert "second" in out
        assert "third" in out
        assert "fourth" in out

    def test_second_voice_recent_context_truncates_to_budget(self, daemon):
        daemon._history = [
            {"role": "human", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
            {"role": "human", "content": "c" * 100},
        ]
        out = daemon._second_voice_recent_context(limit_msgs=3, max_chars=80)
        assert len(out) <= 80
        assert "truncated" in out

    def test_second_voice_revision_prompt_mentions_human(self):
        out = bd.BubbleDaemon._second_voice_revision_prompt("note text")
        assert "facing the human" in out
        assert "no longer in the internal dialogue" in out
        assert "note text" in out


# ---------------------------------------------------------------------------
# Inline think tag drainage
# ---------------------------------------------------------------------------

class TestDrainInlineThinkContent:
    """Test that <think>...</think> blocks in normal content are properly
    separated into visible text and reasoning.

    The function is designed for incremental processing — it may hold back
    the last few characters of the buffer to check for tag boundaries.
    Use flush=True when the stream is complete."""

    def test_no_think_tags(self, daemon):
        text = "Just plain text, nothing special."
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        assert "plain text" in visible
        assert reasoning == ""

    def test_complete_think_block(self, daemon):
        text = "Hello <think>thinking about this</think> world"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        assert "Hello " in visible
        assert "thinking about this" in reasoning
        # " world" may be in visible or held in leftover buffer for next chunk
        assert " world" in visible or " world" in leftover or "world" in leftover

    def test_think_block_at_start(self, daemon):
        text = "<think>let me think</think> the answer"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        assert "let me think" in reasoning
        assert " the " in visible or " the " in leftover or " the" in leftover

    def test_think_block_at_end(self, daemon):
        text = "answer is here <think>still thinking"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        # Incomplete think block at end should be held in buffer
        assert in_think is True
        assert "still" in visible or "still" in leftover or "hinking" in leftover

    def test_nested_think_tags(self, daemon):
        text = "<think>first thought</think> and <think>second thought</think> end"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        assert "first thought" in reasoning
        assert "second thought" in reasoning
        assert " and " in visible
        # "end" may be in visible or leftover
        assert "end" in visible or "end" in leftover or " end" in leftover

    def test_empty_think_block(self, daemon):
        text = "before <think></think> after"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False
        )
        assert reasoning == ""
        assert "before " in visible
        # "after" may be in visible or leftover
        assert "after" in visible or "after" in leftover

    def test_think_block_with_flush(self, daemon):
        text = "partial <think>thinking"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False, flush=True
        )
        assert "partial " in visible
        assert "thinking" in reasoning
        # flush=True means buffer is exhausted; leftover should be empty
        assert leftover == ""

    def test_incremental_processing(self, daemon):
        """Drain in chunks, simulating streaming tokens."""
        all_visible = []
        all_reasoning = []
        in_think = False

        # First chunk: "Hello <think>th"
        v1, r1, buf1, in_think = daemon._drain_inline_think_content(
            "Hello <think>th", in_think
        )
        all_visible.append(v1)
        all_reasoning.append(r1)

        # Second chunk: prepend leftover + append "inking about this</think> world"
        v2, r2, buf2, in_think = daemon._drain_inline_think_content(
            buf1 + "inking about this</think> world", in_think
        )
        all_visible.append(v2)
        all_reasoning.append(r2)

        # Flush with empty string to drain remaining buffer
        v3, r3, buf3, in_think = daemon._drain_inline_think_content(
            buf2, in_think, flush=True
        )
        all_visible.append(v3)
        all_reasoning.append(r3)

        full_visible = "".join(all_visible)
        full_reasoning = "".join(all_reasoning)

        assert "Hello " in full_visible
        assert "world" in full_visible
        assert "inking about this" in full_reasoning or "thinking about this" in full_reasoning
        assert buf3 == ""  # flushed clean

    def test_think_content_not_in_visible(self, daemon):
        """Reasoning content should not leak into visible output."""
        text = "before <think>SECRET THOUGHTS</think> after"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False, flush=True
        )
        # Flush to get everything
        final_v, _, _, _ = daemon._drain_inline_think_content(leftover, in_think, flush=True)
        full_visible = visible + final_v

        assert "SECRET THOUGHTS" not in full_visible
        assert "SECRET THOUGHTS" in reasoning or "SECRET THOUGHTS" in final_r
        assert "before" in full_visible
        assert "after" in full_visible or "after" in leftover or "aft" in leftover

    def test_multiple_think_blocks(self, daemon):
        text = "A <think>thought 1</think> B <think>thought 2</think> C"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False, flush=True
        )
        assert "thought 1" in reasoning
        assert "thought 2" in reasoning
        assert "A " in visible
        assert "B " in visible
        assert "C" in visible or "C" in leftover

    def test_flush_drains_incomplete_tag(self, daemon):
        """flush=True should extract reasoning even without closing tag."""
        text = "start <think>incomplete"
        visible, reasoning, leftover, in_think = daemon._drain_inline_think_content(
            text, False, flush=True
        )
        assert "incomplete" in reasoning
        assert leftover == ""


# ---------------------------------------------------------------------------
# Prior context block extraction
# ---------------------------------------------------------------------------

class TestExtractPriorContextBlock:
    def test_well_formed_block(self):
        text = "Some intro text [[BUBBLE:prior_context]]summary line 1\nsummary line 2[[/BUBBLE:prior_context]] trailing text"
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert "summary line 1" in summary
        assert "summary line 2" in summary
        assert "Some intro text" in cleaned
        assert "trailing text" in cleaned

    def test_no_block(self):
        text = "Just regular text with no markers"
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert summary == ""
        assert cleaned == text

    def test_empty_block(self):
        text = "[[BUBBLE:prior_context]][[/BUBBLE:prior_context]]"
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert summary == ""

    def test_malformed_closing(self):
        """Tolerate common malformed closing markers."""
        text = "before [[BUBBLE:prior_context]]content[[/BUBBLE:prior_context]] after"
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert "content" in summary

        text2 = "before [[BUBBLE:prior_context]]content[/BUBBLE:prior_context] after"
        summary2, cleaned2 = bd.BubbleDaemon._extract_prior_context_block(text2)
        assert "content" in summary2

    def test_block_removes_template_lines(self):
        text = (
            "[[BUBBLE:prior_context]]"
            "<compressed context summary>\n"
            "real summary line\n"
            "[/BUBBLE:prior_context]\n"
            "bubble:prior_context noise\n"
            "<remember>ignore</remember>\n"
            "real content after"
        )
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert "compressed context summary" not in summary
        assert "real summary line" in summary
        assert "bubble:prior_context" not in summary
        assert "real content after" in cleaned

    def test_long_block_truncated(self):
        long_summary = "x" * 5000
        text = f"[[BUBBLE:prior_context]]{long_summary}[[/BUBBLE:prior_context]]"
        summary, cleaned = bd.BubbleDaemon._extract_prior_context_block(text)
        assert len(summary) <= 4000


# ---------------------------------------------------------------------------
# History trimming
# ---------------------------------------------------------------------------

class TestTrimHistory:
    """Test that history stays within MAX_HISTORY_TURNS * 2 messages."""

    def _make_daemon(self, history):
        """Create a daemon with pre-loaded history."""
        d = bd.BubbleDaemon()
        d._history = history
        return d

    def test_small_history_not_touched(self):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi back"},
        ]
        d = self._make_daemon(history)
        with d._lock:
            d._trim_history()
        assert len(d._history) == 2

    def test_trim_oldest_to_limit(self):
        # MAX_HISTORY_TURNS=50, so at most 100 non-system messages are kept.
        # Create 202 messages — should trim to 100.
        history = [
            {"role": "user", "content": f"msg{i}"} if i % 2 == 0
            else {"role": "assistant", "content": f"reply{i}"}
            for i in range(202)
        ]
        d = self._make_daemon(history)
        with d._lock:
            d._trim_history()
        assert len(d._history) == 100
        # Oldest 102 messages should be gone, starting from index 102
        assert d._history[0]["content"] == "msg102"

    def test_system_messages_not_counted_in_trim(self):
        history = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        d = self._make_daemon(history)
        with d._lock:
            d._trim_history()
        # System message should remain
        assert d._history[0]["role"] == "system"

    def test_empty_history(self):
        d = self._make_daemon([])
        with d._lock:
            d._trim_history()
        assert d._history == []


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty(self, daemon):
        est = daemon._estimate_tokens()
        assert est == 0

    def test_basic_history(self, daemon):
        daemon._history = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "hi there!"},
        ]
        est = daemon._estimate_tokens()
        # 11 + 9 = 20 chars / 4 = 5 tokens
        assert est == 5

    def test_memory_included(self, daemon):
        daemon._memory["fact"] = "a" * 16
        est = daemon._estimate_tokens()
        # 16 chars / 4 = 4 tokens from memory
        assert est == 4

    def test_history_and_memory(self, daemon):
        daemon._history = [{"role": "user", "content": "hello"}]  # 5 chars
        daemon._memory["fact"] = "abcde"  # 5 chars
        est = daemon._estimate_tokens()
        # (5 + 5) / 4 = 2 tokens
        assert est == 2

    def test_no_content_field(self, daemon):
        daemon._history = [{"role": "system"}]
        est = daemon._estimate_tokens()
        assert est == 0


# ---------------------------------------------------------------------------
# Parse value tags
# ---------------------------------------------------------------------------

class TestParseValueTags:
    def test_no_tags(self):
        base, tags = bd.BubbleDaemon._parse_value_tags("simple value")
        assert base == "simple value"
        assert tags == {}

    def test_intent_tag(self):
        base, tags = bd.BubbleDaemon._parse_value_tags("value | intent=question")
        assert base == "value"
        assert tags == {"intent": "question"}

    def test_multiple_tags(self):
        text = "value | intent=preference | topic=technical | sentiment=curious | user=casey"
        base, tags = bd.BubbleDaemon._parse_value_tags(text)
        assert base == "value"
        assert tags["intent"] == "preference"
        assert tags["topic"] == "technical"
        assert tags["sentiment"] == "curious"
        assert tags["user"] == "casey"

    def test_non_standard_tag_passthrough(self):
        text = "value | not_a_real_tag=stuff"
        base, tags = bd.BubbleDaemon._parse_value_tags(text)
        assert base == "value | not_a_real_tag=stuff"
        assert tags == {}

    def test_mixed_standard_and_passthrough(self):
        text = "value | intent=question | random=stuff"
        base, tags = bd.BubbleDaemon._parse_value_tags(text)
        assert base == "value | random=stuff"
        assert tags == {"intent": "question"}

    def test_empty_chunks(self):
        base, tags = bd.BubbleDaemon._parse_value_tags("")
        assert base == ""
        assert tags == {}

    def test_user_identity_alias(self):
        base, tags = bd.BubbleDaemon._parse_value_tags("val | user_identity=alice")
        assert tags.get("user_identity") == "alice"

    def test_tag_with_colon_separator(self):
        base, tags = bd.BubbleDaemon._parse_value_tags("val | intent:question")
        assert tags == {"intent": "question"}


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

class TestGetPort:
    """_get_port reads OFLM_SERVE_PORT env var with validation."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("OFLM_SERVE_PORT", raising=False)
        assert bd._get_port() == 52625

    def test_custom_valid(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "11434")
        assert bd._get_port() == 11434

    def test_custom_invalid_too_low(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "0")
        assert bd._get_port() == 52625

    def test_custom_invalid_too_high(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "99999")
        assert bd._get_port() == 52625

    def test_custom_invalid_not_number(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "abc")
        assert bd._get_port() == 52625

    def test_edge_valid_min(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "1")
        assert bd._get_port() == 1

    def test_edge_valid_max(self, monkeypatch):
        monkeypatch.setenv("OFLM_SERVE_PORT", "65535")
        assert bd._get_port() == 65535


class TestGetCtxSize:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BUBBLE_CTX", raising=False)
        assert bd._get_ctx_size() == 8192

    def test_custom(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_CTX", "16384")
        assert bd._get_ctx_size() == 16384

    def test_zero_ignored(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_CTX", "0")
        assert bd._get_ctx_size() == 8192

    def test_negative_ignored(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_CTX", "-100")
        assert bd._get_ctx_size() == 8192


class TestGetMemoryBackupLimit:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("BUBBLE_MEMORY_BACKUP_LIMIT", raising=False)
        assert bd._get_memory_backup_limit() == 50

    def test_custom(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_MEMORY_BACKUP_LIMIT", "100")
        assert bd._get_memory_backup_limit() == 100

    def test_too_low(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_MEMORY_BACKUP_LIMIT", "0")
        assert bd._get_memory_backup_limit() == 50

    def test_too_high(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_MEMORY_BACKUP_LIMIT", "501")
        assert bd._get_memory_backup_limit() == 50

    def test_edge_max(self, monkeypatch):
        monkeypatch.setenv("BUBBLE_MEMORY_BACKUP_LIMIT", "500")
        assert bd._get_memory_backup_limit() == 500
