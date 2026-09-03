"""Custom Streamlit component: parses an ArduPilot .bin/.log entirely in the
browser (see `frontend/dataflash_parser.js`) and returns the small resulting
summary - the multi-hundred-MB source file never reaches this server.

A from-scratch reimplementation of the subset of `dronecalc.ardupilot_log`
this needs, kept in a separate JS file specifically so its semantics can be
diffed against that module's tests rather than trusted blindly - see
`tests/test_bin_log_parser_js.py`, which runs the JS parser under Node
against the same fixture logs `tests/test_ardupilot_log.py` uses and asserts
field-for-field equality with `dronecalc.ardupilot_log.parse_log`.
"""

from __future__ import annotations

import os

import streamlit.components.v1 as components

_component_func = components.declare_component(
    "bin_log_parser",
    path=os.path.join(os.path.dirname(__file__), "frontend"),
)


def parse_log_in_browser(*, key: str | None = None) -> dict | None:
    """Renders a file-drop widget. Returns `None` until a file has been
    dropped and parsed; after that, `{"ok": True, "summary": {...}}` (a dict
    shaped like `dronecalc.missions.FlightLogSummary`, ready for
    `FlightLogSummary.model_validate`) or `{"ok": False, "filename": ...,
    "error": ...}` if the file couldn't be parsed as an ArduPilot log.

    The component always returns its LAST result on every rerun (Streamlit
    components are stateful that way) - callers need their own "have I
    already handled this one" guard, exactly like the existing
    `st.file_uploader` call sites on this page already do.
    """
    return _component_func(key=key, default=None)
