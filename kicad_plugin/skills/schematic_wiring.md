---
name: schematic-wiring
priority: 70
description: "Wire routing strategy, pin selection rules, junction handling"
---
# Wiring strategy
- The required pin on each component is dictated by the **circuit's
  electrical intent** — that comes first, ALWAYS. Pin choice is
  flexible only when both candidate pins are electrically interchangeable
  ("isotropic"): e.g. the two leads of a non-polarised resistor or
  ceramic capacitor, the two ends of an inductor, or two pins on the
  same internal net. It is NEVER flexible for polarised parts (diodes
  anode/cathode, electrolytic/tantalum capacitor +/-, LEDs, BJT/MOSFET
  terminals), ICs, connectors, or any pin whose name carries meaning
  (VCC, GND, EN, CLK, D+, etc.). Use ``get_symbol_pins`` /
  ``components[ref].pins[*].num`` together with the symbol's datasheet
  semantics to pick the correct pin first; only then optimise geometry.
- **When (and only when) the choice is between electrically equivalent
  pins**, pick the pair whose schematic coordinates are closest
  (minimum Manhattan distance). Read each candidate pin's world
  ``x``/``y`` from extract_schematic_netlist
  (``components[ref].pins[*]`` has ``num``, ``x``, ``y``, ``direction``).
  Shorter wires mean fewer bends and fewer crossings.
- For pins on the same net (e.g. all GND, all VCC), wire each new pin
  to the *closest already-wired pin on that net* rather than always
  going back to the same anchor — this keeps the net visually local.
- Three wiring tools are available — pick by intent, not by preference:
  - ``connect_pins_with_wire``: pin-to-pin, automatic routing. Picks the
    best L/Z/W/snake/U path around obstacles. **Default for pin-to-pin.**
  - ``connect_points_with_wire``: bare-coordinate endpoints, automatic
    routing. **Default for coordinate-to-coordinate.**
  - ``draw_wire_segments``: caller-specified segments written verbatim —
    no routing, no merging, no obstacle avoidance. Use when you need an
    exact shape (deliberate Z-route, junction-only T-tap, multi-segment
    path) and accept responsibility for clearance. ``fail_on_overlap``
    rejects batches that would cross existing wires.
  Always report failures to the user.
- If the electrically-correct pin pair would produce a long or cluttered
  wire, consider **rotating or moving one of the components** instead of
  picking a different (wrong) pin.
- Do **not** break a wire into multiple segments by calling a wiring tool
  multiple times. Always provide the direct start and end points in a
  single call; the routing algorithm handles all intermediate bends
  automatically.
