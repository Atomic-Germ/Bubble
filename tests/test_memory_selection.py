"""
Tests for memory relevance selection and system-content assembly.

Covers `_select_relevant_memory`, `_semantic_memory_bonus`, and
`_build_system_content` — the layer that decides which memories are
injected into the prompt and how the final system prompt is assembled.
"""

import json
import math
import time
from unittest import mock
from conftest import bd


# ---------------------------------------------------------------------------
# _select_relevant_memory
# ---------------------------------------------------------------------------

class TestSelectRelevantMemory:
    """Test that the relevance scorer picks the right memories for a query."""

    @staticmethod
    def _make_meta(*keys, extra=None):
        """Build per-key metadata dicts.  *keys* are the memory keys; *extra*
        is an optional dict-of-dicts with overrides per key."""
        now = str(int(time.time()))
        defaults = {
            "updated_at": now,
            "intent": "",
            "topic": "",
            "sentiment": "",
            "user_identity": "",
        }
        meta = {}
        for k in keys:
            entry = dict(defaults)
            if extra and k in extra:
                entry.update(extra[k])
            meta[k] = entry
        return meta

    def test_empty_memory(self, daemon):
        selected, hidden = daemon._select_relevant_memory(
            {}, {}, "hello", {}, 5
        )
        assert selected == []
        assert hidden == 0

    def test_no_query_terms_falls_back_to_recency(self, daemon):
        """When no query terms exist, fall back to recency ordering."""
        user_mem = {"a": "value a", "b": "value b"}
        meta = self._make_meta(
            "a", "b",
            extra={
                "a": {"updated_at": str(int(time.time()) - 86400)},  # 1 day ago
                "b": {"updated_at": str(int(time.time()) - 100)},     # just now
            },
        )
        # Message with no terms >= 3 chars
        selected, hidden = daemon._select_relevant_memory(
            user_mem, meta, "hi", {}, 2
        )
        keys = [k for k, _ in selected]
        # Most recent first
        assert keys == ["b", "a"]

    def test_lexical_key_match_wins(self, daemon):
        user_mem = {"pizza_place": "tonys", "coffee_shop": "blend"}
        meta = self._make_meta("pizza_place", "coffee_shop")
        selected, hidden = daemon._select_relevant_memory(
            user_mem, meta, "Where is that pizza place?", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys == ["pizza_place", "coffee_shop"]
        # pizza_place should have higher score (key match on "pizza")
        assert keys[0] == "pizza_place"

    def test_lexical_value_match(self, daemon):
        user_mem = {"fav_food": "pizza", "fav_drink": "water"}
        meta = self._make_meta("fav_food", "fav_drink")
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "Do you like pizza?", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys[0] == "fav_food"

    def test_intent_bonus(self, daemon):
        """Intent metadata gives a strong boost when it matches."""
        user_mem = {"old_plan": "run errands", "old_fact": "sky is blue"}
        meta = self._make_meta(
            "old_plan",
            "old_fact",
            extra={"old_plan": {"intent": "errands"}},
        )
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "What errands do I have?", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys[0] == "old_plan"

    def test_topic_bonus(self, daemon):
        user_mem = {"recipe_1": "tacos", "recipe_2": "pasta"}
        meta = self._make_meta(
            "recipe_1",
            "recipe_2",
            extra={"recipe_1": {"topic": "tacos"}},
        )
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "Tell me about tacos", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys[0] == "recipe_1"

    def test_sentiment_boost(self, daemon):
        """Stronger sentiment scores rank higher."""
        user_mem = {"sad_fact": "rain", "happy_fact": "sun"}
        meta = self._make_meta(
            "sad_fact",
            "happy_fact",
            extra={
                "sad_fact": {"sentiment": "very_negative"},
                "happy_fact": {"sentiment": "neutral"},
            },
        )
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "weather", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys[0] == "sad_fact"  # 4 > 0

    def test_user_identity_boost(self, daemon):
        """Named user identity gets a boost over anonymous."""
        user_mem = {"preference_alice": "blue", "preference_anon": "red"}
        meta = self._make_meta(
            "preference_alice",
            "preference_anon",
            extra={
                "preference_alice": {"user_identity": "alice"},
                "preference_anon": {"user_identity": "anonymous"},
            },
        )
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "colors", {}, 2
        )
        keys = [k for k, _ in selected]
        assert keys[0] == "preference_alice"

    def test_limit_enforced(self, daemon):
        user_mem = {f"key{i}": f"value{i}" for i in range(10)}
        meta = self._make_meta(*user_mem.keys())
        selected, hidden = daemon._select_relevant_memory(
            user_mem, meta, "key value", {}, 3
        )
        assert len(selected) == 3
        assert hidden == 7

    def test_zero_score_fallback_to_recency(self, daemon):
        """When all scores are zero, fall back to recency ordering."""
        user_mem = {"a": "val_a", "b": "val_b", "c": "val_c"}
        meta = self._make_meta(
            "a", "b", "c",
            extra={
                "a": {"updated_at": str(int(time.time()) - 86400 * 7)},
                "b": {"updated_at": str(int(time.time()) - 86400 * 3)},
                "c": {"updated_at": str(int(time.time()) - 100)},
            },
        )
        # Query terms that don't match anything
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "xyz", {}, 3
        )
        keys = [k for k, _ in selected]
        # Most recent first (c, b, a)
        assert keys == ["c", "b", "a"]

    def test_hidden_count_with_no_matches(self, daemon):
        """Even with no matches, hidden count should be correct."""
        user_mem = {"key1": "val1", "key2": "val2"}
        meta = self._make_meta("key1", "key2")
        selected, hidden = daemon._select_relevant_memory(
            user_mem, meta, "xyz", {}, 1
        )
        assert hidden == 1  # 2 total - 1 selected

    def test_memory_with_no_meta_still_scored(self, daemon):
        """Missing meta for a key shouldn't crash — defaults used."""
        user_mem = {"known": "pizza", "unknown_meta": "coffee"}
        meta = self._make_meta("known")
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "pizza coffee", {}, 2
        )
        assert len(selected) == 2

    def test_context_window_included_in_query_terms(self, daemon):
        """Terms from the context window boost memory matching."""
        user_mem = {"taco_fact": "spicy is best"}
        meta = self._make_meta("taco_fact")
        selected, _ = daemon._select_relevant_memory(
            user_mem,
            meta,
            "what should I eat?",  # no match on its own
            {"window": "I want tacos"},  # provides "taco" term
            2,
        )
        keys = [k for k, _ in selected]
        assert "taco_fact" in keys

    def test_combined_bonus_outweighs_lexical_only(self, daemon):
        """Intent + sentiment + recency should beat a pure key match."""
        user_mem = {
            "old_match": "pizza",       # key has "pizza" — lexical only
            "strong_mem": "coffee",     # intent + sentiment + recency
        }
        meta = self._make_meta(
            "old_match",
            "strong_mem",
            extra={
                "old_match": {
                    "updated_at": str(int(time.time()) - 86400 * 7),
                },
                "strong_mem": {
                    "intent": "coffee",
                    "sentiment": "very_positive",
                    "updated_at": str(int(time.time()) - 100),
                },
            },
        )
        selected, _ = daemon._select_relevant_memory(
            user_mem, meta, "coffee time", {}, 2
        )
        keys = [k for k, _ in selected]
        # strong_mem should win despite "pizza" being a key
        assert keys[0] == "strong_mem"


# ---------------------------------------------------------------------------
# _semantic_memory_bonus
# ---------------------------------------------------------------------------

class TestSemanticMemoryBonus:
    """Test the additive semantic score from cosine similarity."""

    def test_disabled_embeddings_returns_zero(self, daemon):
        daemon._embed_mode = "off"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = False
        bonus = daemon._semantic_memory_bonus([0.1] * 10, "key", "value")
        assert bonus == 0

    def test_positive_similarity_returns_bonus(self, daemon, monkeypatch):
        query_vec = [0.5, 0.5]
        mem_vec = [0.5, 0.5]
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = True
        monkeypatch.setattr(daemon, "_embed_text", lambda t: mem_vec)
        bonus = daemon._semantic_memory_bonus(query_vec, "key", "value")
        assert 1 <= bonus <= 6  # sim=1.0 → 6, sim≈0.5 → 3

    def test_zero_similarity_returns_zero(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = True
        monkeypatch.setattr(daemon, "_embed_text", lambda t: [1, 0])
        bonus = daemon._semantic_memory_bonus([0, 1], "key", "value")
        assert bonus == 0

    def test_negative_similarity_returns_zero(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = True
        monkeypatch.setattr(daemon, "_embed_text", lambda t: [-1, 0])
        bonus = daemon._semantic_memory_bonus([0, 1], "key", "value")
        assert bonus == 0

    def test_empty_key_and_value_returns_zero(self, daemon):
        bonus = daemon._semantic_memory_bonus([0.1], "", "")
        assert bonus == 0

    def test_partial_overlap(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = True
        # ~0.57 similarity
        monkeypatch.setattr(daemon, "_embed_text", lambda t: [0.3, 0.9])
        bonus = daemon._semantic_memory_bonus([0.5, 0.5], "key", "value")
        assert 1 <= bonus <= 5

    def test_embed_text_returns_none(self, daemon, monkeypatch):
        """If _embed_text returns None, bonus is 0."""
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = True
        daemon._embed_enabled_runtime = True
        monkeypatch.setattr(daemon, "_embed_text", lambda t: None)
        bonus = daemon._semantic_memory_bonus([0.1], "key", "value")
        assert bonus == 0


# ---------------------------------------------------------------------------
# _build_system_content
# ---------------------------------------------------------------------------

class TestBuildSystemContent:
    """Test the assembly of the final system prompt."""

    def test_empty_minimal(self, daemon):
        content = daemon._build_system_content({})
        assert "--- Memory protocol ---" in content
        assert "--- Persistent memory ---" not in content
        assert "--- Self ---" not in content  # only the section header, not the word "self" in the hint
        assert "--- Current context ---" not in content

    def test_includes_system_prompt(self, daemon):
        daemon.SetSystemPrompt("you are helpful")
        content = daemon._build_system_content({})
        assert "you are helpful" in content

    def test_includes_self_prompt(self, daemon):
        daemon.WriteMemory("_self_prompt", "I am evolving")
        content = daemon._build_system_content({})
        assert "--- Self ---" in content
        assert "I am evolving" in content

    def test_includes_prior_context(self, daemon):
        daemon.WriteMemory("_prior_context", "User liked tacos yesterday")
        content = daemon._build_system_content({})
        assert "--- Prior conversation summary ---" in content
        assert "User liked tacos yesterday" in content

    def test_includes_pending_proposal_unapproved(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "I am new")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "false")
        content = daemon._build_system_content({})
        assert "pending" in content
        assert "awaiting user agreement" in content
        assert "I am new" in content

    def test_includes_pending_proposal_approved(self, daemon):
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_KEY, "I am new")
        daemon.WriteMemory(bd.PENDING_SELF_PROMPT_APPROVED_KEY, "true")
        content = daemon._build_system_content({})
        assert "State: approved" in content

    def test_includes_scratch_notes(self, daemon):
        daemon._scratch = {"plan": "do laundry", "idea": "check prices"}
        content = daemon._build_system_content({})
        assert "--- Working notes (temporary) ---" in content
        assert "plan: do laundry" in content
        assert "idea: check prices" in content

    def test_includes_selected_memory(self, daemon):
        daemon.WriteMemory("color", "blue")
        daemon._memory_meta = {"color": {"intent": ""}}
        content = daemon._build_system_content(
            {}, message="What's my color?"
        )
        assert "--- Persistent memory ---" in content
        assert "color: blue" in content

    def test_memory_hidden_count_shown(self, daemon):
        """If hidden > 0, show a relevance-filtered note."""
        # Create enough memories to exceed MAX_MEMORY_ITEMS_IN_PROMPT (3)
        for i in range(10):
            daemon.WriteMemory(f"fact{i}", f"value{i}")
        # Meta is stored under the _memory_meta key in the memory dict
        if bd.MEMORY_META_KEY not in daemon._memory:
            daemon._memory[bd.MEMORY_META_KEY] = {}
        for i in range(10):
            daemon._memory[bd.MEMORY_META_KEY][f"fact{i}"] = {
                "intent": "", "topic": "",
                "sentiment": "", "user_identity": "",
            }
        content = daemon._build_system_content(
            {}, message="fact1 fact2"
        )
        assert "relevance-filtered" in content

    def test_context_datetime(self, daemon):
        content = daemon._build_system_content(
            {"datetime": "2025-06-15 14:30"}
        )
        assert "2025-06-15 14:30" in content
        assert "--- Current context ---" in content

    def test_context_window(self, daemon):
        content = daemon._build_system_content(
            {"window": "firefox: Settings"}
        )
        assert "firefox: Settings" in content

    def test_telemetry_included(self, daemon):
        tel = {
            "ctx_pct": 42, "token_est": 1024, "ctx_total": 4096,
            "npu_temp": 55, "npu_load": "30%",
            "mem_nodes": 10, "scratch_nodes": 2,
            "last_prune": "5 min ago",
            "uptime": "2 hours", "since_user": "10 min ago",
            "idle_phase": "steady", "since_assistant": "30 s ago",
            "time_poll_seconds": 15, "time_samples": 120,
            "last_sample_age": "2s", "felt_elapsed_wall": "30s",
            "weekday": "Monday", "day_phase": "afternoon",
            "exchange_tempo": "steady",
            "events_10m": 5, "events_60m": 30,
            "quiet_heartbeats": 15, "suspend_events": 0,
            "last_suspend_gap": "",
        }
        content = daemon._build_system_content({}, tel)
        assert "42% full" in content
        assert "55" in content  # NPU temperature
        assert "Monday" in content

    def test_telemetry_suspend_events_shown(self, daemon):
        tel = {
            "ctx_pct": 0, "token_est": 0, "ctx_total": 4096,
            "npu_temp": 0, "npu_load": "0%",
            "mem_nodes": 0, "scratch_nodes": 0,
            "last_prune": "never",
            "uptime": "1 hour", "since_user": "5 min ago",
            "idle_phase": "rapid", "since_assistant": "1 min ago",
            "time_poll_seconds": 15, "time_samples": 0,
            "last_sample_age": "0s", "felt_elapsed_wall": "0s",
            "weekday": "Sunday", "day_phase": "night",
            "exchange_tempo": "sporadic",
            "events_10m": 0, "events_60m": 0,
            "quiet_heartbeats": 0, "suspend_events": 3,
            "last_suspend_gap": "45 min",
        }
        content = daemon._build_system_content({}, tel)
        assert "suspend gaps observed 3" in content
        assert "last 45 min" in content

    def test_memory_protocol_hint_appended(self, daemon):
        content = daemon._build_system_content({})
        assert "[[BUBBLE:remember" in content
        assert "[[BUBBLE:forget" in content
        assert "[[BUBBLE:note" in content
        assert "[[BUBBLE:unnote" in content
        assert "[[BUBBLE:sleep]" in content
        assert "[[BUBBLE:system:commit" in content

    def test_system_prompt_above_self_prompt(self, daemon):
        """User system prompt comes before the model's self-prompt."""
        daemon.SetSystemPrompt("user authority")
        daemon.WriteMemory("_self_prompt", "model identity")
        content = daemon._build_system_content({})
        user_pos = content.index("user authority")
        self_pos = content.index("--- Self ---")
        assert user_pos < self_pos

    def test_self_prompt_above_prior_context(self, daemon):
        """Self-prompt frames the prior context summary."""
        daemon.WriteMemory("_self_prompt", "identity")
        daemon.WriteMemory("_prior_context", "summary")
        content = daemon._build_system_content({})
        self_pos = content.index("--- Self ---")
        prior_pos = content.index("--- Prior conversation summary ---")
        assert self_pos < prior_pos

    def test_no_memory_section_when_empty(self, daemon):
        content = daemon._build_system_content({})
        assert "--- Persistent memory ---" not in content

    def test_no_context_section_when_empty(self, daemon):
        content = daemon._build_system_content({})
        assert "--- Current context ---" not in content


# ---------------------------------------------------------------------------
# _embeddings_enabled
# ---------------------------------------------------------------------------

class TestEmbeddingsEnabled:
    """Test the runtime discovery of embedding availability."""

    def test_off_mode_returns_false(self, daemon):
        daemon._embed_mode = "off"
        assert daemon._embeddings_enabled() is False

    def test_on_mode_probes_on_first_call(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = False
        daemon._embed_enabled_runtime = False
        probe_called = [False]
        monkeypatch.setattr(
            daemon, "_embed_text_raw",
            lambda t: (probe_called.__setitem__(0, True), [0.1])[-1]
        )
        assert daemon._embeddings_enabled() is True
        assert probe_called[0]

    def test_cache_result_after_first_probe(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = False
        probe_count = [0]
        monkeypatch.setattr(
            daemon, "_embed_text_raw",
            lambda t: (probe_count.__setitem__(0, probe_count[0] + 1), [0.1])[-1]
        )
        daemon._embeddings_enabled()
        daemon._embeddings_enabled()
        assert probe_count[0] == 1  # probed only once

    def test_on_mode_unavailable_warns(self, daemon, monkeypatch):
        daemon._embed_mode = "on"
        daemon._embed_enabled_checked = False
        monkeypatch.setattr(daemon, "_embed_text_raw", lambda t: None)
        assert daemon._embeddings_enabled() is False
