"""Shared-team password gate.

One password for the whole team, checked against `st.secrets["app_password"]`.
There are no user accounts, so there is no per-user attribution or history: if
you later need to know who changed a component, this is the piece to replace.

Deliberately small. It stops a stranger who finds the URL from editing the
library; it is not a defence against anyone who has the password and should not
be treated as one. The comparison is constant-time so the password cannot be
recovered a character at a time by timing the response.

When no password is configured the gate stays open, so local development and the
test suite need no secrets file. That is safe only because a missing secret
cannot happen by accident in the cloud - Streamlit Community Cloud has no
`secrets.toml` unless you create one - but it does mean a deployment with the
secret misspelled is an open deployment. `require_login` says so in the sidebar
whenever it lets someone through unauthenticated.
"""

from __future__ import annotations

import hmac

import streamlit as st

SESSION_KEY = "_authenticated"


def _configured_password() -> str | None:
    try:
        value = st.secrets.get("app_password")
    except Exception:  # noqa: BLE001 - no secrets file at all is the common case
        return None
    return str(value) if value else None


def require_login() -> None:
    """Block the page until the shared password is entered.

    Call once, at the top of every page, before anything reads the library.
    """
    password = _configured_password()

    if password is None:
        st.sidebar.caption("⚠️ No password configured — this instance is open to anyone.")
        return

    if st.session_state.get(SESSION_KEY):
        with st.sidebar:
            if st.button("Sign out", width="stretch"):
                st.session_state.pop(SESSION_KEY, None)
                st.rerun()
        return

    st.title("Daedalus Calculator")
    st.caption("Enter the team password to continue.")
    entered = st.text_input("Password", type="password", key="_password_input")
    if entered:
        if hmac.compare_digest(entered, password):
            st.session_state[SESSION_KEY] = True
            del st.session_state["_password_input"]
            st.rerun()
        else:
            st.error("That password is not right.")
    st.stop()
