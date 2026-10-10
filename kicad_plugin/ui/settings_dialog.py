"""
Settings dialog: lets the engineer configure the LLM provider and API key.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    import wx

    _WX_AVAILABLE = True
except ImportError:
    _WX_AVAILABLE = False


if _WX_AVAILABLE:

    class SettingsDialog(wx.Dialog):
        """Simple dialog for editing plugin settings."""

        _PROVIDERS = ["openai", "anthropic", "ollama", "bedrock"]
        # Suggested default model per provider. Used only to pre-fill the Model
        # field when switching providers if it is empty or still holds another
        # provider's default — a custom entry is never overwritten. The Bedrock
        # default uses the US cross-region inference-profile prefix
        # ("us.anthropic.<id>"), which newer Claude models on Bedrock require
        # for on-demand throughput; swap the prefix for your region (e.g.
        # "eu."/"apac.") or drop it to a plain "anthropic.<id>" if your account
        # supports direct on-demand access.
        _DEFAULT_MODELS = {
            "openai": "gpt-4o",
            "anthropic": "claude-opus-5-5",
            "ollama": "llama3.1",
            "bedrock": "us.anthropic.claude-opus-5-5",
        }
        # User-Agents proven accepted (HTTP 200) at opencode.ai's edge in
        # issue #149; urllib's default (Python-urllib/...) is rejected (403).
        _USER_AGENTS = [
            "python-requests/2.32.3",
            "Go-http-client/1.1",
            "okhttp/4.9.0",
            "axios/1.7.0",
            "curl/8.14.1",
            "Mozilla/5.0",
        ]
        # Vertical gap between form rows in the FlexGridSizer (must match the
        # vgap literal used in _build_ui).
        _GRID_VGAP = 6

        def __init__(self, parent, settings) -> None:
            super().__init__(parent, title="AI Assistant Settings", size=(560, 620))
            self._settings = settings
            self._build_ui()

        def _build_ui(self) -> None:
            vbox = wx.BoxSizer(wx.VERTICAL)
            grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            grid.AddGrowableCol(1, 1)

            # Scrollable viewport for the form: rows taller than the fixed
            # window height gain a vertical scrollbar instead of being
            # clipped. Buttons stay outside this container (see below).
            self._scrolled = wx.ScrolledWindow(self)
            self._scrolled.SetScrollRate(5, 5)
            self._scrolled.SetSizer(grid)

            # Provider
            grid.Add(
                wx.StaticText(self._scrolled, label="LLM Provider:"), 0, wx.ALIGN_CENTER_VERTICAL
            )
            self._provider = wx.Choice(self._scrolled, choices=self._PROVIDERS)
            idx = (
                self._PROVIDERS.index(self._settings.llm_provider)
                if self._settings.llm_provider in self._PROVIDERS
                else 0
            )
            self._provider.SetSelection(idx)
            self._provider.Bind(wx.EVT_CHOICE, self._on_provider_changed)
            grid.Add(self._provider, 1, wx.EXPAND)

            # API Key
            grid.Add(wx.StaticText(self._scrolled, label="API Key:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._api_key = wx.TextCtrl(
                self._scrolled, value=self._settings.llm_api_key, style=wx.TE_PASSWORD
            )
            grid.Add(self._api_key, 1, wx.EXPAND)

            # Model
            grid.Add(wx.StaticText(self._scrolled, label="Model:"), 0, wx.ALIGN_CENTER_VERTICAL)
            self._model = wx.TextCtrl(self._scrolled, value=self._settings.llm_model)
            grid.Add(self._model, 1, wx.EXPAND)

            # Supports vision
            grid.Add(
                wx.StaticText(self._scrolled, label="Model supports vision:"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._supports_vision = wx.CheckBox(self._scrolled)
            self._supports_vision.SetValue(self._settings.llm_supports_vision)
            self._supports_vision.SetToolTip(
                "Enable when the model accepts image input (e.g. gpt-4o, claude-3.x, llava). "
                "Disable for text-only models to avoid sending image data they cannot process."
            )
            grid.Add(self._supports_vision, 1)

            # Custom base URL
            grid.Add(
                wx.StaticText(self._scrolled, label="Custom endpoint URL:"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._base_url = wx.TextCtrl(self._scrolled, value=self._settings.llm_base_url)
            grid.Add(self._base_url, 1, wx.EXPAND)

            # --- Amazon Bedrock (shown only when Provider = bedrock) ---
            # Leave the AWS credential fields blank to authenticate with a
            # Bedrock API key (put it in the API Key field above); fill them in
            # to sign requests with AWS SigV4 instead. The label+field widgets
            # are tracked in self._aws_rows so they can be shown/hidden by
            # provider (see _update_aws_visibility).
            self._aws_rows: list[tuple[wx.Window, wx.Window]] = []

            region_label = wx.StaticText(self._scrolled, label="AWS Region:")
            grid.Add(region_label, 0, wx.ALIGN_CENTER_VERTICAL)
            self._aws_region = wx.TextCtrl(
                self._scrolled, value=getattr(self._settings, "llm_aws_region", "") or ""
            )
            self._aws_region.SetHint("us-east-1")
            self._aws_region.SetToolTip(
                "Bedrock only. AWS region for the bedrock-runtime endpoint "
                "(e.g. us-east-1). Also used for SigV4 signing."
            )
            grid.Add(self._aws_region, 1, wx.EXPAND)
            self._aws_rows.append((region_label, self._aws_region))

            access_label = wx.StaticText(self._scrolled, label="AWS Access Key ID:")
            grid.Add(access_label, 0, wx.ALIGN_CENTER_VERTICAL)
            self._aws_access_key_id = wx.TextCtrl(
                self._scrolled, value=getattr(self._settings, "llm_aws_access_key_id", "") or ""
            )
            self._aws_access_key_id.SetToolTip(
                "Bedrock only. Leave blank to use a Bedrock API key (API Key field). "
                "Set this plus the secret key to authenticate with AWS SigV4."
            )
            grid.Add(self._aws_access_key_id, 1, wx.EXPAND)
            self._aws_rows.append((access_label, self._aws_access_key_id))

            secret_label = wx.StaticText(self._scrolled, label="AWS Secret Access Key:")
            grid.Add(secret_label, 0, wx.ALIGN_CENTER_VERTICAL)
            self._aws_secret_access_key = wx.TextCtrl(
                self._scrolled,
                value=getattr(self._settings, "llm_aws_secret_access_key", "") or "",
                style=wx.TE_PASSWORD,
            )
            self._aws_secret_access_key.SetToolTip("Bedrock only. AWS secret key for SigV4 auth.")
            grid.Add(self._aws_secret_access_key, 1, wx.EXPAND)
            self._aws_rows.append((secret_label, self._aws_secret_access_key))

            token_label = wx.StaticText(self._scrolled, label="AWS Session Token:")
            grid.Add(token_label, 0, wx.ALIGN_CENTER_VERTICAL)
            self._aws_session_token = wx.TextCtrl(
                self._scrolled,
                value=getattr(self._settings, "llm_aws_session_token", "") or "",
                style=wx.TE_PASSWORD,
            )
            self._aws_session_token.SetToolTip(
                "Bedrock only. Optional STS session token for temporary AWS credentials."
            )
            grid.Add(self._aws_session_token, 1, wx.EXPAND)
            self._aws_rows.append((token_label, self._aws_session_token))

            # User-Agent
            grid.Add(
                wx.StaticText(self._scrolled, label="User-Agent:"), 0, wx.ALIGN_CENTER_VERTICAL
            )
            self._user_agent = wx.ComboBox(
                self._scrolled, choices=self._USER_AGENTS, style=wx.CB_DROPDOWN
            )
            if self._settings.llm_user_agent:
                # Editable combo: values outside the proven list are shown
                # as-is, so the user can keep or replace the text.
                self._user_agent.SetValue(self._settings.llm_user_agent)
            else:
                # Empty (legacy default): the proven default is pre-selected.
                self._user_agent.SetValue(self._USER_AGENTS[0])
            self._user_agent.SetToolTip(
                "User-Agent sent on OpenAI/Anthropic-compatible requests. Pick a "
                "value proven accepted (HTTP 200) at opencode.ai's edge in issue "
                "#149, or type your own; urllib's default (Python-urllib/...) is "
                "rejected (HTTP 403)."
            )
            grid.Add(self._user_agent, 1, wx.EXPAND)

            # Python executable
            grid.Add(
                wx.StaticText(self._scrolled, label="Python executable:"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._python = wx.TextCtrl(self._scrolled, value=self._settings.python_executable)
            self._python.SetHint("auto-detect (leave blank)")
            grid.Add(self._python, 1, wx.EXPAND)

            # MCP server port
            grid.Add(
                wx.StaticText(self._scrolled, label="MCP server port (0=auto):"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._port = wx.SpinCtrl(
                self._scrolled, value=str(self._settings.server_port), min=0, max=65535
            )
            grid.Add(self._port, 1, wx.EXPAND)

            # Show tool log
            grid.Add(
                wx.StaticText(self._scrolled, label="Show tool log by default:"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._show_tool_log = wx.CheckBox(self._scrolled)
            self._show_tool_log.SetValue(self._settings.show_tool_log)
            grid.Add(self._show_tool_log, 1)

            # Context window management
            grid.Add(
                wx.StaticText(self._scrolled, label="Context window (tokens):"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._context_tokens = wx.SpinCtrl(
                self._scrolled, min=1000, max=2_000_000, initial=self._settings.llm_context_tokens
            )
            grid.Add(self._context_tokens, 1, wx.EXPAND)

            grid.Add(
                wx.StaticText(self._scrolled, label="Compaction threshold (0–1):"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._compact_threshold = wx.SpinCtrlDouble(
                self._scrolled,
                min=0.1,
                max=0.95,
                inc=0.05,
                initial=self._settings.llm_compact_threshold,
            )
            self._compact_threshold.SetDigits(2)
            grid.Add(self._compact_threshold, 1, wx.EXPAND)

            grid.Add(
                wx.StaticText(self._scrolled, label="Compaction target (0–1):"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._compact_target = wx.SpinCtrlDouble(
                self._scrolled,
                min=0.05,
                max=0.90,
                inc=0.05,
                initial=self._settings.llm_compact_target_threshold,
            )
            self._compact_target.SetDigits(2)
            grid.Add(self._compact_target, 1, wx.EXPAND)

            grid.Add(
                wx.StaticText(self._scrolled, label="Max output tokens (0=default):"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._max_tokens = wx.SpinCtrl(
                self._scrolled, min=0, max=1_000_000, initial=self._settings.llm_max_tokens
            )
            grid.Add(self._max_tokens, 1, wx.EXPAND)

            grid.Add(
                wx.StaticText(self._scrolled, label="Recent turns to keep:"),
                0,
                wx.ALIGN_CENTER_VERTICAL,
            )
            self._keep_recent_turns = wx.SpinCtrl(
                self._scrolled, min=1, max=20, initial=self._settings.llm_keep_recent_turns
            )
            grid.Add(self._keep_recent_turns, 1, wx.EXPAND)

            # Size the scroll region to the form's full height so rows beyond the
            # window edge are reachable via the scrollbar.
            self._scrolled.Layout()
            self._scrolled.FitInside()
            vbox.Add(self._scrolled, 1, wx.ALL | wx.EXPAND, 10)

            # Buttons (fixed, outside the scrolling area)
            btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
            vbox.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, 8)

            self.SetSizer(vbox)
            self.Layout()

            # Mouse wheel anywhere over the form scrolls only: with every
            # control bound to the same handler and propagation stopped, the
            # wheel neither moves focus to the widget under the cursor (e.g.
            # the port spinner) nor makes it react/change value. Click and
            # Tab focus behavior is untouched.
            self._scrolled.Bind(wx.EVT_MOUSEWHEEL, self._on_scroll_wheel)
            for child in self._scrolled.GetChildren():
                child.Bind(wx.EVT_MOUSEWHEEL, self._on_scroll_wheel)

            # Apply the provider's default model on open too, so a stored
            # provider whose Model field still holds another provider's default
            # (e.g. bedrock + leftover "gpt-4o") is corrected without needing to
            # toggle the dropdown. Also set the AWS fields' visibility to match
            # the stored provider.
            self._maybe_fill_default_model()
            self._update_aws_visibility()

        def _on_scroll_wheel(self, event) -> None:
            """Scroll the form by the wheel's rotation; never touch focus."""
            steps = event.GetWheelRotation() / (event.GetWheelDelta() or 120)
            x, y = self._scrolled.GetViewStart()
            # Each scroll unit is one 5 px scroll-rate step; a notch is about
            # 12 units (~60 px, roughly 3 form rows).
            self._scrolled.Scroll(x, y - int(round(steps * 12)))
            event.StopPropagation()

        def _on_provider_changed(self, event) -> None:
            """React to a provider selection change: suggest a default model
            and show/hide the Bedrock-only AWS fields."""
            self._maybe_fill_default_model()
            self._update_aws_visibility()

        def _update_aws_visibility(self) -> None:
            """Show the AWS/Bedrock credential rows only for the bedrock
            provider; hide them (without clearing their values) otherwise, then
            reflow the scrolled form."""
            show = self._PROVIDERS[self._provider.GetSelection()] == "bedrock"
            for label, field in self._aws_rows:
                label.Show(show)
                field.Show(show)
            self._scrolled.Layout()
            self._scrolled.FitInside()

        def _maybe_fill_default_model(self) -> None:
            """Pre-fill the Model field with the selected provider's default.

            Runs both on dialog open and when the provider changes. Only
            pre-fills when the Model field is empty or still holds one of the
            known per-provider defaults, so a model the user typed is never
            overwritten.
            """
            provider = self._PROVIDERS[self._provider.GetSelection()]
            default = self._DEFAULT_MODELS.get(provider)
            if not default:
                return
            current = self._model.GetValue().strip()
            if current == "" or current in self._DEFAULT_MODELS.values():
                self._model.SetValue(default)

        def apply_to(self, settings) -> bool:
            """Write dialog values back to settings object.

            Returns False (and shows an error) if validation fails.
            """
            compact_threshold = self._compact_threshold.GetValue()
            compact_target = self._compact_target.GetValue()
            if compact_target >= compact_threshold:
                wx.MessageBox(
                    "Compaction target must be strictly less than compaction threshold.\n"
                    f"(target={compact_target:.2f}, threshold={compact_threshold:.2f})",
                    "Invalid Settings",
                    wx.OK | wx.ICON_ERROR,
                    self,
                )
                return False
            settings.llm_provider = self._PROVIDERS[self._provider.GetSelection()]
            settings.llm_api_key = self._api_key.GetValue().strip()
            settings.llm_model = self._model.GetValue().strip()
            settings.llm_supports_vision = self._supports_vision.GetValue()
            settings.llm_base_url = self._base_url.GetValue().strip()
            settings.llm_aws_region = self._aws_region.GetValue().strip()
            settings.llm_aws_access_key_id = self._aws_access_key_id.GetValue().strip()
            settings.llm_aws_secret_access_key = self._aws_secret_access_key.GetValue().strip()
            settings.llm_aws_session_token = self._aws_session_token.GetValue().strip()
            settings.llm_user_agent = self._user_agent.GetValue().strip()
            settings.python_executable = self._python.GetValue().strip()
            settings.server_port = self._port.GetValue()
            settings.show_tool_log = self._show_tool_log.GetValue()
            settings.llm_context_tokens = self._context_tokens.GetValue()
            settings.llm_compact_threshold = compact_threshold
            settings.llm_compact_target_threshold = compact_target
            settings.llm_keep_recent_turns = self._keep_recent_turns.GetValue()
            settings.llm_max_tokens = self._max_tokens.GetValue()
            return True

else:

    class SettingsDialog:  # type: ignore[no-redef]
        def __init__(self, parent, settings) -> None:
            pass

        def ShowModal(self):
            return 0

        def apply_to(self, settings) -> bool:
            return True

        def Destroy(self) -> None:
            pass
