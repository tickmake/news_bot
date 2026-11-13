"""The dashboard behind its login: `streamlit run serve.py`.

`st.App` serves app.py as an ASGI app, and `auth.LoginGate` runs in front of
Streamlit's own middleware, so no page, file or websocket answers without a
session (auth.py). Settings come from DASHBOARD_PASSWORD_HASH and friends.
"""

import logging

import streamlit as st
from starlette.middleware import Middleware

import auth

# Streamlit configures only its own loggers; give the login's lines a handler.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
auth.LOG.addHandler(_handler)
auth.LOG.setLevel(logging.INFO)
auth.LOG.propagate = False

app = st.App("app.py", middleware=[Middleware(auth.LoginGate, **auth.settings_from_env())])
