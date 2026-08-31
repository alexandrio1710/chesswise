"""Unit tests for coaching_chat.py: Ollama reachability detection, prompt/
context construction, and send_message()'s request/error handling against
a faked `requests` call (same _FakeResponse pattern test_opening_explorer.py
already uses) — no real Ollama server needed.
"""

import requests

import coaching_chat


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self.ok = status_code < 400
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


def _report(**overrides):
    base = {
        "headline": "Test headline",
        "game_count": 2,
        "time_control_stats": {"rapid": {"games": 2, "acpl": 30}},
        "structure_stats": {"london_symmetric": {"games": 1}},
        "highlighted_positions": [
            {
                "game_id": 1, "fen_before": "8/8/8/8/8/8/8/8 w - - 0 1",
                "played_move_san": "d4", "best_move_san": "e4",
                "close_alternatives": [
                    {"move_san": "e4", "eval_cp": 30, "mate_in": None},
                    {"move_san": "d4", "eval_cp": 25, "mate_in": None},
                ],
            },
        ],
        "what_to_work_on": ["Some checklist item."],
        "trend_note": "Drift rate is down.",
    }
    base.update(overrides)
    return base


class TestIsConfigured:
    def test_true_when_ollama_responds_ok(self, monkeypatch):
        monkeypatch.setattr(coaching_chat.requests, "get", lambda url, timeout=None: _FakeResponse(200))
        assert coaching_chat.is_configured() is True

    def test_false_when_ollama_returns_an_error_status(self, monkeypatch):
        monkeypatch.setattr(coaching_chat.requests, "get", lambda url, timeout=None: _FakeResponse(500))
        assert coaching_chat.is_configured() is False

    def test_false_when_ollama_is_unreachable(self, monkeypatch):
        def raise_connection_error(url, timeout=None):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(coaching_chat.requests, "get", raise_connection_error)
        assert coaching_chat.is_configured() is False


class TestReportContext:
    def test_includes_the_real_numbers_not_placeholders(self):
        context = coaching_chat._report_context(_report())
        assert "Test headline" in context
        assert "rapid" in context
        assert "london_symmetric" in context

    def test_includes_highlighted_position_detail(self):
        context = coaching_chat._report_context(_report())
        assert "d4" in context and "e4" in context
        assert "8/8/8/8/8/8/8/8 w - - 0 1" in context

    def test_missing_optional_fields_dont_crash(self):
        minimal = {"headline": "H", "game_count": 0}
        context = coaching_chat._report_context(minimal)
        assert "H" in context


class TestSystemPrompt:
    def test_embeds_the_report_context(self):
        prompt = coaching_chat._system_prompt(_report())
        assert "Test headline" in prompt

    def test_explicitly_frames_drift_candidates_as_not_objectively_bad(self):
        prompt = coaching_chat._system_prompt(_report())
        assert "not" in prompt.lower() and "objectively bad" in prompt.lower()

    def test_instructs_honesty_about_what_the_data_cant_answer(self):
        prompt = coaching_chat._system_prompt(_report())
        assert "say so" in prompt.lower()


class TestSendMessage:
    def test_raises_when_ollama_unreachable(self, monkeypatch):
        import pytest

        monkeypatch.setattr(coaching_chat, "is_configured", lambda: False)
        with pytest.raises(RuntimeError, match="Ollama isn't reachable"):
            coaching_chat.send_message(_report(), [], "What should I work on?")

    def test_successful_reply_is_returned(self, monkeypatch):
        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        monkeypatch.setattr(
            coaching_chat.requests, "post",
            lambda url, json, timeout: _FakeResponse(200, {"message": {"role": "assistant", "content": "Focus on the endgame."}}),
        )
        reply = coaching_chat.send_message(_report(), [], "What should I work on?")
        assert reply == "Focus on the endgame."

    def test_sends_system_prompt_and_history_in_the_right_shape(self, monkeypatch):
        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        captured = {}

        def fake_post(url, json, timeout):
            captured["payload"] = json
            return _FakeResponse(200, {"message": {"content": "ok"}})

        monkeypatch.setattr(coaching_chat.requests, "post", fake_post)
        history = [{"role": "user", "content": "Earlier question"}, {"role": "assistant", "content": "Earlier answer"}]
        coaching_chat.send_message(_report(), history, "New question")

        messages = captured["payload"]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1:3] == history
        assert messages[-1] == {"role": "user", "content": "New question"}
        assert captured["payload"]["stream"] is False

    def test_model_not_pulled_raises_a_clear_error(self, monkeypatch):
        import pytest

        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        monkeypatch.setattr(coaching_chat.requests, "post", lambda url, json, timeout: _FakeResponse(404))
        with pytest.raises(RuntimeError, match="ollama pull"):
            coaching_chat.send_message(_report(), [], "Hi")

    def test_timeout_raises_a_clear_error(self, monkeypatch):
        import pytest

        def raise_timeout(url, json, timeout):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        monkeypatch.setattr(coaching_chat.requests, "post", raise_timeout)
        with pytest.raises(RuntimeError, match="didn't respond"):
            coaching_chat.send_message(_report(), [], "Hi")

    def test_empty_reply_raises_a_clear_error(self, monkeypatch):
        import pytest

        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        monkeypatch.setattr(coaching_chat.requests, "post", lambda url, json, timeout: _FakeResponse(200, {"message": {"content": ""}}))
        with pytest.raises(RuntimeError, match="didn't return a text reply"):
            coaching_chat.send_message(_report(), [], "Hi")

    def test_history_is_trimmed_to_max_length(self, monkeypatch):
        monkeypatch.setattr(coaching_chat, "is_configured", lambda: True)
        captured = {}

        def fake_post(url, json, timeout):
            captured["payload"] = json
            return _FakeResponse(200, {"message": {"content": "ok"}})

        monkeypatch.setattr(coaching_chat.requests, "post", fake_post)
        long_history = [{"role": "user", "content": f"msg {i}"} for i in range(50)]
        coaching_chat.send_message(_report(), long_history, "New question")

        # system prompt + trimmed history + new user message
        assert len(captured["payload"]["messages"]) == 1 + coaching_chat.MAX_HISTORY_MESSAGES + 1
