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
    <div class="tool-body tool-entry tool-ok">
      <span style="color:#444">args:</span><br><pre style="margin:2px 0">{"reference": "R1", "x": 100, "y": 200}</pre>
      <span style="color:#444">result:</span><br><pre style="margin:2px 0">{"success": true}</pre>
    </div>
  </details>
  <!-- Collapsed tool call — testing auto-expand on search -->
  <details class="tools tool-details" id="tool_2">
    <summary class="tool-summary"><span style="color:#2e7d32">&#10003;</span>
    <span style="color:#444;font-weight:600">&#8609; add_wire</span></summary>
    <div class="tool-body tool-entry tool-ok">
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
    // Host globals absent from bare vm contexts but used by the shell's
    // click-to-collapse handler.
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    __listeners: {},
    addEventListener: function(type, fn) {
        if (!this.__listeners[type]) this.__listeners[type] = [];
        this.__listeners[type].push(fn);
    },
    getSelection: function() {
        return dom.window.getSelection ? dom.window.getSelection() : { isCollapsed: true };
    },
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
    const clearResult = _clearFind();
    assert(clearResult === '0/0', '_clearFind returns explicit 0/0: got ' + clearResult);
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

section('11. Script message posts kind/seq/count for async results');
{
    sandbox.kcaa = {
        messages: [],
        postMessage: function(payload) { this.messages.push(JSON.parse(payload)); },
    };

    _findTextAndJump('resistor', 7);
    const msgs = sandbox.kcaa.messages;
    assert(msgs.length === 1, 'one search should post one message: got ' + msgs.length);
    assert(msgs[0].kind === 'find', 'payload kind should be find: got ' + msgs[0].kind);
    assert(msgs[0].seq === 7, 'payload seq should echo the Python sequence: got ' + msgs[0].seq);
    assert(msgs[0].count === 3, 'payload count should be the match total: got ' + msgs[0].count);
    assert(msgs[0].query === 'resistor', 'payload query should be the query: got ' + msgs[0].query);

    _findNext(8);
    assert(msgs.length === 2, 'next should post another message');
    assert(msgs[1].seq === 8 && msgs[1].count === 3,
        'next posts the new seq and unchanged total: got ' + JSON.stringify(msgs[1]));

    _clearFind(9);
    assert(msgs.length === 3, 'clear should post a message');
    assert(msgs[2].seq === 9 && msgs[2].count === 0,
        'clear posts seq and zero matches: got ' + JSON.stringify(msgs[2]));

    const noHit = _findTextAndJump('zzznotfound', 10);
    assert(noHit === '0/0', 'no-match find returns explicit 0/0: got ' + noHit);
    assert(msgs.length === 4, 'no-match find still posts one message');
    assert(msgs[3].seq === 10 && msgs[3].count === 0,
        'no-match posts seq and zero count: got ' + JSON.stringify(msgs[3]));
}

// ---- Click-to-collapse ----
(async function() {
    section('12. Body click collapses an open tool row');
    {
        const tool1 = document.getElementById('tool_1');
        assert(tool1.hasAttribute('open'), 'tool_1 should start open');
        const body = tool1.querySelector('.tool-body');
        body.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
        // The collapse is deferred 250 ms as a double-click guard; give the
        // timer time to fire.
        await new Promise((resolve) => global.setTimeout(resolve, 350));
        assert(!tool1.hasAttribute('open'), 'tool_1 should be collapsed after a body click');
    }

    section('13. Duplicate details ids: a body click closes only the clicked row');
    {
        const conv = document.getElementById('conversation');
        // Mimic a session-restore id collision: two rows share one details id.
        conv.insertAdjacentHTML('beforeend',
            '<details class="tools tool-details" id="dup_row" open=""><summary class="tool-summary">first</summary>' +
            '<div class="tool-body" id="dup_body_first">first body</div></details>' +
            '<details class="tools tool-details" id="dup_row" open=""><summary class="tool-summary">second</summary>' +
            '<div class="tool-body" id="dup_body_second">second body</div></details>');
        const firstRow = document.getElementById('dup_body_first').closest('details');
        const secondRow = document.getElementById('dup_body_second').closest('details');
        document.getElementById('dup_body_second')
            .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
        await new Promise((resolve) => global.setTimeout(resolve, 350));
        assert(firstRow.hasAttribute('open'), 'earlier duplicate-id row must stay open (no wrong-target collapse)');
        assert(!secondRow.hasAttribute('open'), 'clicked later row collapses despite the shared id');
    }

    section('14. Large match counts stay bounded per visible window');
    {
        const conv = document.getElementById('conversation');
        const saved = conv.innerHTML;
        let html = '';
        for (let i = 0; i < 2000; i++) html += '<span>x</span>';
        conv.innerHTML = html;

        const result = _findText('x');
        assert(result === '1/2000', 'true total is reported: got ' + result);
        assert(sandbox.__findTotal === 2000, 'total hits: got ' + sandbox.__findTotal);
        assert(sandbox.__findMatches.length <= 2000,
            'marks stay within MAX_MARKS: got ' + sandbox.__findMatches.length);

        const clearResult = _clearFind();
        assert(clearResult === '0/0', 'clear returns explicit 0/0 after a large search');
        assert(conv.textContent === 'x'.repeat(2000), 'clearing restores the text losslessly');

        conv.innerHTML = saved;
        _clearFind();
    }

    section('15. Only blocks near the viewport are highlighted');
    {
        const conv = document.getElementById('conversation');
        const saved = conv.innerHTML;
        const savedHeight = sandbox.innerHeight;
        const savedScrollY = sandbox.scrollY;
        conv.innerHTML = '';
        for (let i = 0; i < 6; i++) {
            const block = document.createElement('span');
            block.textContent = 'needle';
            block.__rect = { top: i * 1000, bottom: i * 1000 + 900, height: 900 };
            block.getBoundingClientRect = function() { return this.__rect; };
            conv.appendChild(block);
        }
        sandbox.innerHeight = 100;
        sandbox.scrollY = 0;

        const result = _findText('needle');
        assert(result === '1/6', 'off-screen hits still count: got ' + result);
        const blocks = Array.from(conv.children);
        assert(blocks[0].querySelectorAll('mark.search-match').length === 1,
            'visible block is marked');
        assert(blocks[1].querySelectorAll('mark.search-match').length === 0,
            'block more than two screens below is not marked');

        sandbox.scrollY = 2000;
        (sandbox.__listeners.scroll || []).forEach((fn) => fn());
        await new Promise((resolve) => global.setTimeout(resolve, 220));

        assert(blocks[1].querySelectorAll('mark.search-match').length === 1,
            'scrolling marks the newly visible block');
        assert(blocks[2].querySelectorAll('mark.search-match').length === 1,
            'second-screen block marked after scroll');
        assert(blocks[3].querySelectorAll('mark.search-match').length === 0,
            'block beyond the window stays unmarked');

        sandbox.innerHeight = savedHeight;
        sandbox.scrollY = savedScrollY;
        conv.innerHTML = saved;
        _clearFind();
    }

    section('16. Clearing normalizes once per parent and is lossless');
    {
        const conv = document.getElementById('conversation');
        const saved = conv.innerHTML;
        conv.innerHTML = '';
        for (let i = 0; i < 50; i++) {
            const block = document.createElement('span');
            block.textContent = 'aaa aaa aaa';
            conv.appendChild(block);
        }
        const before = conv.textContent;

        const NodeProto = dom.window.Node.prototype;
        const originalNormalize = NodeProto.normalize;
        let normalizeCalls = 0;
        const parents = new Set();
        NodeProto.normalize = function() {
            normalizeCalls++;
            parents.add(this);
            return originalNormalize.call(this);
        };
        try {
            _findText('aaa');
            assert(sandbox.__findMatches.length === 150,
                'three marks per block: got ' + sandbox.__findMatches.length);
            _clearFind();
            assert(normalizeCalls <= parents.size,
                'one normalize per parent: ' + normalizeCalls + ' calls for ' + parents.size + ' parents');

            _findText('zzznotfound');
            _clearFind();
            assert(normalizeCalls <= parents.size * 2,
                'no per-mark normalize: ' + normalizeCalls + ' calls for ' + parents.size + ' parents');
            assert(conv.textContent === before, 'conversation text is unchanged after clearing');
        } finally {
            NodeProto.normalize = originalNormalize;
            conv.innerHTML = saved;
            _clearFind();
        }
    }

    // ---- Summary ----
    console.log('\n' + '='.repeat(50));
    console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
    process.exit(failed > 0 ? 1 : 0);
})().catch((err) => {
    console.error('Unhandled test error:', err);
    process.exit(1);
});
