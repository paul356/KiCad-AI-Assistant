/**
 * Unit tests for conversation rebuild scroll/expansion preservation in shell.js.
 *
 * Uses jsdom to simulate the browser DOM and verify that _updateConversation
 * keeps the user's scroll position and the open state of tool results, while
 * 'bottom' still pins the view to the newest entry.
 *
 * Usage: node tests/unit/ui/test_shell_autoscroll.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM } = require('jsdom');

// Load shell.js source
const shellJsPath = path.join(__dirname, '..', '..', '..', 'kicad_plugin', 'ui', 'shell.js');
const shellJs = fs.readFileSync(shellJsPath, 'utf-8');

const BODY_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>
<div id="conversation"></div>
<table id="stream-wrapper"><tr><td id="pending-ai-text"></td></tr></table>
</body></html>`;

// Build a fresh DOM + VM sandbox so each scenario starts clean.
function makeEnv() {
    const dom = new JSDOM(BODY_HTML, { url: 'http://localhost/' });
    const document = dom.window.document;
    // jsdom reports scrollHeight as 0; give it a deterministic value.
    Object.defineProperty(document.body, 'scrollHeight', {
        value: 900,
        configurable: true,
    });

    const scrollCalls = [];
    const sandbox = {
        window: undefined, // set to the sandbox itself below
        document: document,
        NodeFilter: dom.window.NodeFilter,
        HTMLElement: dom.window.HTMLElement,
        console: console,
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (id) => clearTimeout(id),
        getSelection: function() {
            return dom.window.getSelection ? dom.window.getSelection() : { isCollapsed: true };
        },
    };
    sandbox.window = sandbox; // circular: window === sandbox
    sandbox.scrollY = 0;
    sandbox.scrollTo = function(x, y) {
        scrollCalls.push([x, y]);
    };

    const context = vm.createContext(sandbox);
    new vm.Script(shellJs).runInContext(context);
    return { dom, document, sandbox, scrollCalls };
}

const ENTRIES = [
    { type: 'user', text: 'hello' },
    { type: 'tool_call', name: 'first_tool', args: {}, result: { success: true } },
    { type: 'tool_call', name: 'second_tool', args: {}, result: { success: true } },
];

const ENTRIES_JSON = JSON.stringify(ENTRIES);

function toolRows(document) {
    return document.querySelectorAll('details.tools');
}

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

// (a) 'preserve' keeps window.scrollY and re-opens the same tool results.
section('1. _updateConversation(..., "preserve") keeps scroll and open rows');
{
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    let rows = toolRows(document);
    assert(rows.length === 2, 'two tool rows rendered: got ' + rows.length);
    rows[0].open = true;
    rows[1].open = false;

    sandbox.scrollY = 120;
    scrollCalls.length = 0;
    const result = sandbox._updateConversation(ENTRIES_JSON, 'preserve');

    rows = toolRows(document);
    assert(result === 'ok:3', 'update returns ok with entry count: got ' + result);
    assert(rows[0].open === true, 'an expanded first row stays expanded');
    assert(rows[1].open === false, 'a collapsed second row stays collapsed');
    assert(scrollCalls.length === 1, 'preserve scrolls exactly once (restore): got ' + scrollCalls.length);
    assert(scrollCalls[0][0] === 0 && scrollCalls[0][1] === 120,
        'restores window.scrollY: got ' + JSON.stringify(scrollCalls[0]));
}

// (b) 'bottom' scrolls to the document height (bottom wins over restore).
section('2. _updateConversation(..., "bottom") pins to document height');
{
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    toolRows(document)[0].open = true;

    sandbox.scrollY = 120;
    scrollCalls.length = 0;
    sandbox._updateConversation(ENTRIES_JSON, 'bottom');

    assert(scrollCalls.length === 2,
        'bottom restores first, then scrolls to bottom: got ' + JSON.stringify(scrollCalls));
    const last = scrollCalls[scrollCalls.length - 1];
    assert(last[0] === 0 && last[1] === document.body.scrollHeight,
        'last scroll is document height: got ' + JSON.stringify(last));
    assert(toolRows(document)[0].open === true, 'bottom still keeps expanded rows open');
}

// (c) closed details stay closed across a rebuild.
section('3. Closed details stay closed across a rebuild');
{
    const { document, sandbox } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    const rows = toolRows(document);
    for (let i = 0; i < rows.length; i++) {
        rows[i].open = false;
    }
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    const after = toolRows(document);
    assert(after.length === 2, 'two rows after rebuild: got ' + after.length);
    assert(!after[0].open && !after[1].open, 'all rows remain collapsed');
}

// (d) Appending an entry leaves earlier open states untouched.
section('4. _appendEntry keeps earlier open states');
{
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    let rows = toolRows(document);
    rows[0].open = true;

    scrollCalls.length = 0;
    const result = sandbox._appendEntry(
        JSON.stringify({ type: 'status', text: 'still working', color: '#1E1E1E' }),
        'preserve'
    );

    rows = toolRows(document);
    assert(result === 'ok:status', 'append returns ok: got ' + result);
    assert(rows[0].open === true, 'earlier expanded row stays expanded');
    assert(rows[1].open === false, 'earlier collapsed row stays collapsed');
    assert(scrollCalls.length === 0, 'append does not force a scroll: got ' + scrollCalls.length);
}

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);