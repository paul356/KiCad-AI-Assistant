/**
 * Unit tests for the search/find functions in shell.js.
 *
 * Uses jsdom to simulate the browser DOM and verify that _findText,
 * _findNext, _findPrev, and _clearFind work correctly.
 *
 * Usage: node tests/unit/ui/test_shell_search.js
 */

const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

// Load shell.js source
const shellJsPath = path.join(__dirname, '..', '..', '..', 'kicad_plugin', 'ui', 'shell.js');
const shellJs = fs.readFileSync(shellJsPath, 'utf-8');

// Create a DOM with a conversation div containing test entries
const dom = new JSDOM(`<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>
<div id="conversation">
  <table class="msg"><tr><td style="background:#E3F2FD">
    <b><span style="color:#1565C0">You</span></b><br>Place a resistor R1 on the schematic
  </td></tr></table>
  <table class="msg"><tr><td style="background:#E8F5E9">
    <b><span style="color:#00695C">AI</span></b><br>I'll place resistor R1 at coordinates (100, 200).
  </td></tr></table>
  <details class="tools tool-details" id="tool_1" open="">
    <summary class="tool-summary"><span style="color:#2e7d32">&#10003;</span>
    <span style="color:#444;font-weight:600">&#8609; place_symbol</span></summary>
    <div class="tool-body tool-entry tool-ok" data-details="tool_1">
      <span style="color:#444">args:</span><br><pre style="margin:2px 0">{"reference": "R1", "x": 100, "y": 200}</pre>
      <span style="color:#444">result:</span><br><pre style="margin:2px 0">{"success": true}</pre>
    </div>
  </details>
  <!-- Collapsed tool call — testing auto-expand on search -->
  <details class="tools tool-details" id="tool_2">
    <summary class="tool-summary"><span style="color:#2e7d32">&#10003;</span>
    <span style="color:#444;font-weight:600">&#8609; add_wire</span></summary>
    <div class="tool-body tool-entry tool-ok" data-details="tool_2">
      <span style="color:#444">args:</span><br><pre style="margin:2px 0">{"from": "R1-pad1", "to": "C1-pad2"}</pre>
      <span style="color:#444">result:</span><br><pre style="margin:2px 0">{"net_code": 33, "success": true}</pre>
    </div>
  </details>
  <table class="msg"><tr><td style="background:#E8F5E9">
    <b><span style="color:#00695C">AI</span></b><br>Done! Resistor R1 placed successfully.
  </td></tr></table>
</div>
</body></html>`, { url: 'http://localhost/' });

// Expose DOM globals
global.window = dom.window;
global.document = dom.window.document;
global.NodeFilter = dom.window.NodeFilter;
global.HTMLElement = dom.window.HTMLElement;

// Override scrollIntoView (not implemented in jsdom)
dom.window.HTMLElement.prototype.scrollIntoView = function() {};

// Evaluate shell.js in a sandboxed context where `window` is the global object
const sandbox = {
    window: undefined, // will be set to sandbox itself
    document: dom.window.document,
    NodeFilter: dom.window.NodeFilter,
    HTMLElement: dom.window.HTMLElement,
    console: console,
};
sandbox.window = sandbox; // circular: window === sandbox

const vm = require('vm');
const context = vm.createContext(sandbox);
const script = new vm.Script(shellJs);
script.runInContext(context);

// Export functions from sandbox
const { _findText, _findJump, _findNext, _findPrev, _clearFind, _findTextAndJump } = sandbox;

// ---- Test runner ----
let passed = 0;
let failed = 0;

function assert(condition, msg) {
    if (condition) {
        passed++;
    } else {
        failed++;
        console.error('  FAIL: ' + msg);
    }
}

function section(title) {
    console.log('\n' + title);
}

// ---- Tests ----

section('1. _findText basic search');
{
    const result = _findText('resistor');
    assert(result === '1/3', 'should find 3 matches of "resistor": got ' + result);
    const marks = document.querySelectorAll('mark.search-match');
    assert(marks.length === 3, 'should have 3 mark elements: got ' + marks.length);
}

section('2. _findText case-insensitive');
{
    _clearFind();
    const result = _findText('RESISTOR');
    assert(result === '1/3', 'case-insensitive should find 3 matches: got ' + result);
}

section('3. _findText empty query');
{
    _clearFind();
    const result = _findText('');
    assert(result === '0/0', 'empty query returns 0/0: got ' + result);
}

section('4. _findText no match');
{
    _clearFind();
    const result = _findText('zzznotfound');
    assert(result === '0/0', 'no match returns 0/0: got ' + result);
    const marks = document.querySelectorAll('mark.search-match');
    assert(marks.length === 0, 'no mark elements after no-match search');
}

section('5. _findJump navigation');
{
    _clearFind();
    _findText('R1');
    const jumpResult = _findJump(0);
    assert(jumpResult === '1/5', 'first jump: got ' + jumpResult);
    const activeAfter0 = document.querySelectorAll('mark.search-active');
    assert(activeAfter0.length === 1, 'one active mark after jump(0)');

    _findJump(1);
    const activeAfter1 = document.querySelectorAll('mark.search-active');
    assert(activeAfter1.length === 1, 'one active mark after jump(1)');
}

section('6. _findNext / _findPrev wraparound');
{
    _clearFind();
    _findText('R1');
    _findJump(4); // 5/5
    const nextResult = _findNext();
    assert(nextResult === '1/5', 'next wraps to first: got ' + nextResult);
    const prevResult = _findPrev();
    assert(prevResult === '5/5', 'prev wraps to last: got ' + prevResult);
}

section('7. _clearFind restores text');
{
    _clearFind();
    _findText('resistor');
    _clearFind();
    const marks = document.querySelectorAll('mark.search-match, mark.search-active');
    assert(marks.length === 0, 'all marks cleared: got ' + marks.length);
    const conv = document.getElementById('conversation');
    const text = conv.textContent;
    assert(text.includes('Place a resistor R1'), 'text restored after clear');
    assert(text.includes('Resistor R1 placed'), 'text restored after clear');
}

section('8. Multiple searches clear previous');
{
    _findText('resistor');
    _findText('place');
    const marks = document.querySelectorAll('mark.search-match');
    const allMarksText = Array.from(marks).map(m => m.textContent.toLowerCase());
    assert(allMarksText.every(t => t === 'place'), 'all marks should be "place": got ' + JSON.stringify(allMarksText));
}

section('9. Search inside tool call details');
{
    _clearFind();
    _findText('place_symbol');
    const marks = document.querySelectorAll('mark.search-match');
    assert(marks.length === 1, '"place_symbol" appears once (in tool summary): got ' + marks.length);
    // Verify the match is inside the <details> element
    const details = document.getElementById('tool_1');
    assert(details.contains(marks[0]), 'match is inside the tool call details element');
}

section('10. Auto-expand collapsed details on match');
{
    _clearFind();
    // tool_2 is collapsed (no "open" attribute)
    const tool2 = document.getElementById('tool_2');
    assert(!tool2.hasAttribute('open'), 'tool_2 should start collapsed');

    // Search for text only inside the collapsed tool result
    _findTextAndJump('net_code');
    const tool2After = document.getElementById('tool_2');
    const marks = document.querySelectorAll('mark.search-match');
    assert(marks.length === 1, '"net_code" appears once: got ' + marks.length);
    // Verify match is inside tool_2
    assert(tool2After.contains(marks[0]), 'match should be inside tool_2');

    assert(tool2After.hasAttribute('open'), 'tool_2 should be auto-expanded when match is inside it');
}

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);
