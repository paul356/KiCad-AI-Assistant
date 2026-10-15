"""Shared UI colour constants (wx-free).

Plain hex strings with zero imports so any module — the wx-free LLM client
and stream-event state machine as well as the wx-bound panel — can import
them without pulling in a display dependency.  This is the single source of
truth for these values; keep the panel and notice renderers in sync via
these constants, never by re-declaring the hex literals.
"""

# Amber — transient notices / wait progress (panel's _C_WARN / _C_WARN_HEX).
WARN_HEX = "#BE6400"
# Red — errors / failures (panel's _C_ERR / _C_ERR_HEX).
ERR_HEX = "#BE1E1E"
