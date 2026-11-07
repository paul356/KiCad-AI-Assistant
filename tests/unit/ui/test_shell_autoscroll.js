/**
 * Unit tests for conversation rebuild scroll/expansion preservation in shell.js.
 *
 * Uses jsdom to simulate the browser DOM and verify that _updateConversation
 * keeps the user's scroll position and the open state of tool results, while
 * 'bottom' still pins the view to the newest entry and replacement mode starts
 * a fresh document.
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

// A different session's conversation, used to prove replacement does not
// inherit state from the previous document.
const SESSION_B = [
    { type: 'user', text: 'session B' },
    { type: 'tool_call', name: 'b_first', args: {}, result: { success: true } },
    { type: 'tool_call', name: 'b_second', args: {}, result: { success: true } },
];

const SESSION_B_JSON = JSON.stringify(SESSION_B);

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

// Each section is isolated: a thrown assertion must not abort later sections,
// and the exit code still reflects every failure.
function runSection(title, fn) {
    section(title);
    try {
        fn();
    } catch (err) {
        failed++;
        console.error('  FAIL: section threw: ' + (err && err.stack ? err.stack : err));
    }
}

// (a) 'preserve' keeps window.scrollY and re-opens the same tool results.
runSection('1. _updateConversation(..., "preserve") keeps scroll and open rows', function() {
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    let rows = toolRows(document);
    assert(rows.length === 2, 'two tool rows rendered: got ' + rows.length);
    if (rows.length < 2) return;
    rows[0].open = true;
    rows[1].open = false;

    sandbox.scrollY = 120;
    scrollCalls.length = 0;
    const result = sandbox._updateConversation(ENTRIES_JSON, 'preserve');

    rows = toolRows(document);
    assert(result === 'ok:3', 'update returns ok with entry count: got ' + result);
    assert(rows[0].open === true, 'an expanded first row stays expanded');
    assert(rows[1].open === false, 'a collapsed second row stays collapsed');
    assert(scrollCalls.length === 1,
        'preserve scrolls exactly once (restore): got ' + scrollCalls.length);
    if (scrollCalls.length === 1) {
        assert(scrollCalls[0][0] === 0 && scrollCalls[0][1] === 120,
            'restores window.scrollY: got ' + JSON.stringify(scrollCalls[0]));
    }
});

// (b) 'bottom' scrolls to the document height (bottom wins over restore).
runSection('2. _updateConversation(..., "bottom") pins to document height', function() {
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    toolRows(document)[0].open = true;

    sandbox.scrollY = 120;
    scrollCalls.length = 0;
    sandbox._updateConversation(ENTRIES_JSON, 'bottom');

    assert(scrollCalls.length === 2,
        'bottom restores first, then scrolls to bottom: got ' + JSON.stringify(scrollCalls));
    if (scrollCalls.length > 0) {
        const last = scrollCalls[scrollCalls.length - 1];
        assert(last[0] === 0 && last[1] === document.body.scrollHeight,
            'last scroll is document height: got ' + JSON.stringify(last));
    }
    assert(toolRows(document)[0].open === true, 'bottom still keeps expanded rows open');
});

// (c) closed details stay closed across a rebuild.
runSection('3. Closed details stay closed across a rebuild', function() {
    const { document, sandbox } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    const rows = toolRows(document);
    for (let i = 0; i < rows.length; i++) {
        rows[i].open = false;
    }
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    const after = toolRows(document);
    assert(after.length === 2, 'two rows after rebuild: got ' + after.length);
    if (after.length >= 2) {
        assert(!after[0].open && !after[1].open, 'all rows remain collapsed');
    }
});

// (d) Appending an entry leaves earlier open states untouched.
runSection('4. _appendEntry keeps earlier open states', function() {
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    let rows = toolRows(document);
    if (rows.length < 2) {
        assert(false, 'expected two tool rows before append: got ' + rows.length);
        return;
    }
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
});

// (e) Replacement mode starts a fresh document: no inherited open flags or
// scroll offset (session load/restore/clear).
runSection('5. Replacement mode does not inherit previous document state', function() {
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    let rows = toolRows(document);
    if (rows.length < 2) {
        assert(false, 'expected two tool rows before replacement: got ' + rows.length);
        return;
    }
    rows[0].open = true;
    sandbox.scrollY = 120;

    scrollCalls.length = 0;
    const result = sandbox._updateConversation(SESSION_B_JSON, 'preserve', true);

    rows = toolRows(document);
    assert(result === 'ok:3', 'replacement returns ok with entry count: got ' + result);
    assert(rows.length === 2, 'new session renders its own rows: got ' + rows.length);
    if (rows.length >= 2) {
        assert(rows[0].open === false && rows[1].open === false,
            'replacement rows start closed (no inherited open flags)');
    }
    assert(scrollCalls.length === 0,
        'replacement does not restore the old scroll offset: got ' + JSON.stringify(scrollCalls));
});

// (f) Replacement mode still honours an explicit bottom request.
runSection('6. Replacement with "bottom" pins to the new document height', function() {
    const { document, sandbox, scrollCalls } = makeEnv();
    sandbox._updateConversation(ENTRIES_JSON, 'preserve');
    const before = toolRows(document);
    if (before.length < 1) {
        assert(false, 'expected a tool row before replacement: got ' + before.length);
        return;
    }
    before[0].open = true;
    sandbox.scrollY = 120;

    scrollCalls.length = 0;
    sandbox._updateConversation(SESSION_B_JSON, 'bottom', true);

    const rows = toolRows(document);
    if (rows.length >= 2) {
        assert(rows[0].open === false && rows[1].open === false,
            'replacement rows start closed even when scrolling bottom');
    } else {
        assert(false, 'expected two replacement rows: got ' + rows.length);
    }
    assert(scrollCalls.length === 1 && scrollCalls[0][0] === 0
        && scrollCalls[0][1] === document.body.scrollHeight,
        'replacement-bottom scrolls once to document height: got ' + JSON.stringify(scrollCalls));
});

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);