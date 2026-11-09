/**
 * Unit tests for the three-mode conversation scroll contract in shell.js.
 *
 * Modes: 'bottom' always pins to the newest entry; 'follow' pins only when
 * the reader was within 80 px of the bottom before the DOM changed; 'none'
 * never scrolls to the bottom.  Rebuilds also preserve scroll/open state and
 * replacement documents start at the top.
 *
 * Uses jsdom.  window.innerHeight and document.body.scrollHeight are set
 * explicitly, and scrollHeight is derived from DOM content where the test
 * needs to prove that "was at bottom" is measured before mutation.
 *
 * Usage: node tests/unit/ui/test_shell_autoscroll.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM } = require('jsdom');

const shellJsPath = path.join(__dirname, '..', '..', '..', 'kicad_plugin', 'ui', 'shell.js');
const shellJs = fs.readFileSync(shellJsPath, 'utf-8');

const BODY_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body>
<div id="conversation"></div>
<table id="stream-wrapper"><tr><td id="pending-ai-text"></td></tr></table>
</body></html>`;

const INNER_HEIGHT = 500;

// Build a fresh DOM + VM sandbox so each scenario starts clean.
function makeEnv(options) {
    options = options || {};
    const dom = new JSDOM(BODY_HTML, { url: 'http://localhost/' });
    const document = dom.window.document;
    const conversation = document.getElementById('conversation');
    const pending = document.getElementById('pending-ai-text');

    // Explicit height model: base height plus optional DOM-dependent growth.
    const initialHeight = options.initialHeight === undefined ? 1000 : options.initialHeight;
    const heightMode = options.heightMode || 'static';
    const heightFor = function() {
        if (heightMode === 'conversation') {
            return initialHeight + conversation.childNodes.length * 100;
        }
        if (heightMode === 'stream') {
            return initialHeight + pending.textContent.length * 10;
        }
        return initialHeight;
    };
    Object.defineProperty(document.body, 'scrollHeight', {
        get: heightFor,
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
    sandbox.innerHeight = INNER_HEIGHT;
    sandbox.scrollY = 0;
    sandbox.scrollTo = function(x, y) {
        scrollCalls.push([x, y]);
        sandbox.scrollY = y;
    };

    const context = vm.createContext(sandbox);
    new vm.Script(shellJs).runInContext(context);

    return {
        dom,
        document,
        conversation,
        pending,
        sandbox,
        scrollCalls,
        height: heightFor,
    };
}

const ENTRIES = [
    { type: 'user', text: 'hello' },
    { type: 'tool_call', name: 'first_tool', args: {}, result: { success: true } },
    { type: 'tool_call', name: 'second_tool', args: {}, result: { success: true } },
];

const ENTRIES_JSON = JSON.stringify(ENTRIES);

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

// Each section is isolated: a thrown assertion must not abort later sections.
function runSection(title, fn) {
    section(title);
    try {
        fn();
    } catch (err) {
        failed++;
        console.error('  FAIL: section threw: ' + (err && err.stack ? err.stack : err));
    }
}

function atBottom(env) {
    env.sandbox.scrollY = env.height() - INNER_HEIGHT;
}

// ---- _updateConversation ----

runSection('1. follow while at bottom scrolls to the newest entry', function() {
    const env = makeEnv();
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    atBottom(env);
    env.scrollCalls.length = 0;

    env.sandbox._updateConversation(ENTRIES_JSON, 'follow');

    assert(env.scrollCalls.length === 2,
        'follow restores position then scrolls to bottom: got ' + JSON.stringify(env.scrollCalls));
    assert(env.sandbox.scrollY === env.height(),
        'followed the grown document: got ' + env.sandbox.scrollY + ' expected ' + env.height());
});

runSection('2. follow while scrolled up keeps position and open rows', function() {
    const env = makeEnv();
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    const rows = toolRows(env.document);
    if (rows.length < 2) {
        assert(false, 'expected two tool rows: got ' + rows.length);
        return;
    }
    rows[0].open = true;
    env.sandbox.scrollY = 0; // 1000 - 500 - 0 = 500 px from bottom

    env.sandbox._updateConversation(ENTRIES_JSON, 'follow');

    assert(env.sandbox.scrollY === 0,
        'scrolled-up reader is not moved: got ' + env.sandbox.scrollY);
    const after = toolRows(env.document);
    assert(after[0].open === true, 'expanded tool result survives the rebuild');
    assert(after[1].open === false, 'collapsed tool result stays collapsed');
});

runSection('2a. follow measures before the rebuild grows the document', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    // Empty conversation => 1000 px document; reader parked at its bottom.
    assert(env.height() === 1000, 'initial document height: got ' + env.height());
    atBottom(env);
    env.scrollCalls.length = 0;

    env.sandbox._updateConversation(ENTRIES_JSON, 'follow');

    assert(env.height() === 1300,
        'the rebuild itself grew the document: got ' + env.height());
    assert(env.sandbox.scrollY === env.height(),
        'at-bottom reader follows the rebuilt document: got ' + env.sandbox.scrollY
        + ' expected ' + env.height());
    const last = env.scrollCalls[env.scrollCalls.length - 1];
    assert(last && last[0] === 0 && last[1] === env.height(),
        'last scroll targets the post-rebuild height: got ' + JSON.stringify(last));
});

runSection('2b. follow measures before rebuild when the reader is scrolled up', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._updateConversation(ENTRIES_JSON, 'follow');

    assert(env.height() === 1300,
        'the rebuild itself grew the document: got ' + env.height());
    assert(env.sandbox.scrollY === 0,
        'scrolled-up reader is not moved by the rebuild: got ' + env.sandbox.scrollY);
    assert(!env.scrollCalls.some(function(call) { return call[1] === env.height(); }),
        'no scroll to the grown document bottom: got ' + JSON.stringify(env.scrollCalls));
});

runSection('3. bottom scrolls even when the reader is scrolled up', function() {
    const env = makeEnv();
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._updateConversation(ENTRIES_JSON, 'bottom');

    assert(env.sandbox.scrollY === env.height(),
        'bottom wins over the reader position: got ' + env.sandbox.scrollY);
    const last = env.scrollCalls[env.scrollCalls.length - 1];
    assert(last && last[0] === 0 && last[1] === env.height(),
        'last scroll targets document height: got ' + JSON.stringify(last));
});

runSection('4. none never scrolls to the bottom and preserves state', function() {
    const env = makeEnv();
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    const rows = toolRows(env.document);
    if (rows.length < 2) {
        assert(false, 'expected two tool rows: got ' + rows.length);
        return;
    }
    rows[0].open = true;
    env.sandbox.scrollY = 0;

    env.sandbox._updateConversation(ENTRIES_JSON, 'none');

    assert(env.sandbox.scrollY === 0, 'none keeps the scroll offset: got ' + env.sandbox.scrollY);
    assert(toolRows(env.document)[0].open === true, 'none preserves expanded rows');
});

runSection('5. replacement starts at top and drops old open flags', function() {
    const env = makeEnv();
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    const rows = toolRows(env.document);
    if (rows.length < 2) {
        assert(false, 'expected two tool rows: got ' + rows.length);
        return;
    }
    rows[0].open = true;
    env.sandbox.scrollY = 300;

    env.sandbox._updateConversation(SESSION_B_JSON, 'none', true);

    assert(env.sandbox.scrollY === 0,
        'replacement resets the old scroll offset: got ' + env.sandbox.scrollY);
    const after = toolRows(env.document);
    assert(after.length === 2, 'replacement rendered its own rows: got ' + after.length);
    if (after.length >= 2) {
        assert(after[0].open === false && after[1].open === false,
            'replacement rows start closed');
    }
});

// ---- _appendEntry ----

runSection('6. _appendEntry follow measures before appending', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    // 0 children -> height 1000; reader at the bottom of that document.
    env.sandbox.scrollY = 1000 - INNER_HEIGHT;
    env.scrollCalls.length = 0;

    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'new' }), 'follow');

    assert(env.sandbox.scrollY === env.height(),
        'at-bottom reader follows the grown document: got ' + env.sandbox.scrollY
        + ' expected ' + env.height());
    assert(env.height() === 1100, 'append grew the document: got ' + env.height());
});

runSection('7. _appendEntry follow leaves a scrolled-up reader alone', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'new' }), 'follow');

    assert(env.sandbox.scrollY === 0,
        'scrolled-up reader is not moved by an append: got ' + env.sandbox.scrollY);
    assert(env.scrollCalls.length === 0, 'no scroll call for a scrolled-up reader');
});

runSection('8. _appendEntry bottom and none modes', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    env.sandbox.scrollY = 0;

    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'one' }), 'bottom');
    assert(env.sandbox.scrollY === env.height(), 'bottom pins to the newest entry');
    assert(env.height() === 1100, 'first append height: got ' + env.height());

    env.sandbox.scrollY = 100;
    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'two' }), 'none');
    assert(env.sandbox.scrollY === 100, 'none keeps the offset: got ' + env.sandbox.scrollY);
});

// ---- _updateStream ----

runSection('9. _updateStream follow measures before replacing text', function() {
    const env = makeEnv({ heightMode: 'stream' });
    // No streamed text yet -> height 1000; reader at the bottom.
    env.sandbox.scrollY = 1000 - INNER_HEIGHT;
    env.scrollCalls.length = 0;

    env.sandbox._updateStream('x'.repeat(20), 'follow');

    assert(env.sandbox.scrollY === env.height(),
        'at-bottom reader follows the grown stream: got ' + env.sandbox.scrollY
        + ' expected ' + env.height());
    assert(env.height() === 1200, 'streamed text grew the document: got ' + env.height());
});

runSection('10. _updateStream follow leaves a scrolled-up reader alone', function() {
    const env = makeEnv({ heightMode: 'stream' });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._updateStream('x'.repeat(20), 'follow');

    assert(env.sandbox.scrollY === 0,
        'scrolled-up reader is not moved by streaming: got ' + env.sandbox.scrollY);
    assert(env.scrollCalls.length === 0, 'no scroll call for a scrolled-up reader');
});

runSection('11. _updateStream bottom and none modes', function() {
    const env = makeEnv({ heightMode: 'stream' });
    env.sandbox.scrollY = 0;

    env.sandbox._updateStream('x'.repeat(20), 'bottom');
    assert(env.sandbox.scrollY === env.height(), 'bottom pins to streamed text height');

    env.sandbox.scrollTo = function(x, y) {
        env.scrollCalls.push([x, y]);
        env.sandbox.scrollY = y;
    };
    const before = env.sandbox.scrollY;
    env.sandbox._updateStream('y'.repeat(5), 'none');
    assert(env.sandbox.scrollY === before,
        'none keeps the offset: got ' + env.sandbox.scrollY + ' expected ' + before);
});

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);