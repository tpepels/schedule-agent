import io
import json

from schedule_agent.stream_decoder import decode_stream, render_event


def _render(event):
    return "".join(render_event(event))


def test_init_event_summarises_session():
    out = _render(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "abcdef0123456789",
            "model": "claude-opus-4-7",
            "permissionMode": "bypassPermissions",
        }
    )
    assert "session=abcdef012345" in out
    assert "model=claude-opus-4-7" in out
    assert "permission=bypassPermissions" in out


def test_hook_events_are_silent():
    # Hook lifecycle events fire on every session start; rendering them
    # would bury the actual conversation under setup noise.
    assert _render({"type": "system", "subtype": "hook_started"}) == ""
    assert _render({"type": "system", "subtype": "hook_response"}) == ""


def test_text_delta_is_streamed_verbatim():
    out = _render(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello "},
            },
        }
    )
    assert out == "Hello "


def test_thinking_delta_is_streamed_verbatim():
    out = _render(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "pondering..."},
            },
        }
    )
    assert out == "pondering..."


def test_tool_use_block_announces_name():
    out = _render(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Bash", "id": "x"},
            },
        }
    )
    assert "[tool_use: Bash]" in out


def test_tool_result_message_renders_content():
    out = _render(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "files: 3"},
                ]
            },
        }
    )
    assert "[tool_result]" in out
    assert "files: 3" in out


def test_tool_result_with_structured_content_extracts_text():
    out = _render(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "text": "first chunk\n"},
                            {"type": "text", "text": "second chunk"},
                        ],
                    }
                ]
            },
        }
    )
    assert "first chunk\nsecond chunk" in out


def test_result_event_summarises_run():
    out = _render(
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1234,
            "num_turns": 2,
            "total_cost_usd": 0.0759,
        }
    )
    assert "[result] success" in out
    assert "duration=1234ms" in out
    assert "turns=2" in out
    assert "cost=$0.0759" in out


def test_unknown_event_types_are_silent():
    # rate_limit_event and similar housekeeping shouldn't add log noise.
    assert _render({"type": "rate_limit_event"}) == ""
    assert _render({"type": "stream_event", "event": {"type": "ping"}}) == ""


def test_decode_stream_passes_through_non_json_lines():
    # The wrapper script's `[schedule-agent] start ...` banner is plain
    # text, not stream-json. Pre-stream stderr from claude isn't JSON
    # either. Don't drop it on the floor.
    out = io.StringIO()
    decode_stream(["[schedule-agent] start job=abc\n", "claude: warning foo\n"], out)
    assert "[schedule-agent] start job=abc" in out.getvalue()
    assert "claude: warning foo" in out.getvalue()


def test_decode_stream_skips_blank_lines():
    out = io.StringIO()
    decode_stream(["\n", "  \n", ""], out)
    assert out.getvalue() == ""


def test_decode_stream_flushes_per_event():
    # `tail -f` on the log relies on every event being flushed
    # immediately. Use a stream that records flush calls to verify.
    flushes = []

    class Recorder(io.StringIO):
        def flush(self):
            flushes.append(self.tell())
            super().flush()

    out = Recorder()
    events = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "a", "model": "m"}),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hi"},
                },
            }
        ),
        json.dumps({"type": "result", "subtype": "success"}),
    ]
    decode_stream((line + "\n" for line in events), out)
    # One flush per event, monotonically growing.
    assert len(flushes) == 3
    assert flushes == sorted(flushes)
    assert flushes[-1] == len(out.getvalue())


def test_end_to_end_renders_recorded_session_shape():
    # Mirrors the structure of an actual `claude -p --output-format
    # stream-json --include-partial-messages` run: init, text streaming,
    # message_stop, result. Acts as a regression guard against accidental
    # changes to the rendered log layout.
    events = [
        {"type": "system", "subtype": "hook_started"},
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-4-7"},
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": "text"}},
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hi "},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "there"},
            },
        },
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "result", "subtype": "success", "duration_ms": 500, "num_turns": 1},
    ]
    out = io.StringIO()
    decode_stream((json.dumps(e) + "\n" for e in events), out)
    text = out.getvalue()
    assert "[claude] session=s1" in text
    assert "[assistant]" in text
    assert "Hi there" in text
    assert "[result] success duration=500ms turns=1" in text
