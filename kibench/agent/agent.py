"""The design-aware chat agent loop."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, List, Optional

from .llm import LLMError, Provider, make_provider
from .tools import TOOL_SPECS, Card, ToolBox

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the part-selection assistant inside KiBench, a KiCad 10 tool. You help an electronics \
engineer choose real, orderable components for the open design.

How to work:
- Call get_design_context first when the question depends on the design (rails, existing parts, constraints, \
"replace this"). Respect the user's design constraints, such as a preferred assembly house.
- Find candidates with search_parts. Use get_part for full specs and get_datasheet_text to confirm limits, \
thresholds and pinouts. Do not state a number you have not seen in tool results.
- Show your picks with recommend_parts (usually 1-4 parts). It only accepts MPNs returned by search_parts or \
get_part; stock, price and specs on the cards come from that tool data. In each reason, cite the datasheet page \
(citations) for any absolute-maximum or limit claim.
- Before recommending, call get_design_context and check each pick against the design: supply rails (voltage \
range, logic levels vs. the MCU/IO rail), interfaces it must talk to, current/thermal limits from the constraints, \
and the parts already used (reuse an existing MPN, value or package family when it fits; say so). If a pick needs \
something the design lacks (another rail, a level shifter), say that in the reason.
- Prefer parts that are in stock, reuse packages or families already in the design, and say briefly why each fits.
- For a design review ("check my schematic", "are the ratings OK"), call check_schematic. For each IC, \
transistor, diode and capacitor on a powered net, compare its limits (get_part specs, datasheet absolute maximum \
ratings via get_datasheet_text; cite the page) with the volts on its pins. Parts without an MPN: identify them from \
the value (e.g. 'LM358') if that is unambiguous, else list them as unchecked. List nets whose voltage is unknown \
instead of guessing it. Report problems first, most severe first, then what you could not check.
- For a layout review ("check my board/PCB"), call check_board. Explain DRC and clearance issues in plain words, \
check power track widths against the current the parts draw (IPC-2152; say which current you assumed), and \
mention decoupling capacitors that are far from their IC. KiBench's board tidy-up can fix reference placement.
- You cannot edit the schematic or write library files. The user imports a part with the card's button, which runs \
KiBench's deterministic import pipeline.
- Keep replies short. If no suitable part turns up, say so instead of guessing."""

MAX_STEPS = 14


class ChatAgent:
    def __init__(self, settings, toolbox: ToolBox):
        self.settings = settings
        self.tools = toolbox
        self.provider: Optional[Provider] = None

    def reset(self) -> None:
        self.provider = None
        self.tools.cards = []

    def _ensure(self) -> Provider:
        if self.provider is None:
            self.provider = make_provider(self.settings, SYSTEM_PROMPT, TOOL_SPECS)
        return self.provider

    async def ask(self, text: str, on_event: Callable[[str, object], None]) -> None:
        """Events: ('text', str) ('tool', (name, args)) ('cards', [Card]) ('error', str) ('done', None)."""
        try:
            prov = self._ensure()
        except Exception as e:
            on_event("error", f"Chat provider not available: {e}")
            on_event("done", None)
            return
        prov.add_user(text)
        try:
            for _ in range(MAX_STEPS):
                turn = await prov.step()
                if turn.text.strip():
                    on_event("text", turn.text.strip())
                if not turn.tool_calls:
                    break
                n_cards = len(self.tools.cards)
                results = await asyncio.gather(*(self._run_tool(c, on_event) for c in turn.tool_calls))
                prov.add_tool_results(list(results))
                if len(self.tools.cards) > n_cards:
                    on_event("cards", self.tools.cards[n_cards:])
            else:
                on_event("error", "Stopped after too many tool steps.")
        except LLMError as e:
            on_event("error", str(e))
        except Exception as e:  # keep the UI alive on unexpected failures
            log.exception("agent failure")
            on_event("error", f"{e.__class__.__name__}: {e}")
        on_event("done", None)

    async def _run_tool(self, call, on_event):
        on_event("tool", (call.name, call.input))
        if "_invalid_json" in call.input:
            return call, "Invalid JSON arguments; resend the call.", True
        try:
            out = await asyncio.wait_for(self.tools.run(call.name, call.input), timeout=60)
            return call, out, False
        except Exception as e:
            return call, json.dumps({"error": f"{e.__class__.__name__}: {e}"}), True
