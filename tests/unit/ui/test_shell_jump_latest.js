/**
 * Unit tests for the "jump to latest" button in shell.js.
 *
 * The button is purely a function of scroll position: it is visible only when
 * the reader is away from the bottom, using the same _shouldScrollBottom()
 * threshold as 'follow'.  Clicking it jumps to the newest output and hides it.
 * Every DOM-mutating entry point re-measures after its write.
 *
 * Uses jsdom.  window.innerHeight and document.body.scrollHeight are set
 * explicitly, and scrollTo is recorded so the click path can be asserted.
 *
 * Usage: node tests/unit/ui/test_shell_jump_latest.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { JSDOM } = require('jsdom');

const shellJsPath = path.join(__dirname, '..', '..', '..', 'kicad_plugin', 'ui', 'shell.js');
const shellJs = fs.readFileSync(shellJsPath, 'utf-8');

const BODY_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>#jump-latest{display:none}</style>
</head>
<body>
<div id="conversation"></div>
<table id="stream-wrapper"><tr><td id="pending-ai-text"></td></tr></table>
<button id="jump-latest" type="button" aria-label="Jump to latest message">↓</button>
</body></html>`;

const BODY_HTML_NO_BUTTON = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>#jump-latest{display:none}</style>
</head>
<body>
<div id="conversation"></div>
<table id="stream-wrapper"><tr><td id="pending-ai-text"></td></tr></table>
</body></html>`;

const INNER_HEIGHT = 500;

// Build a fresh DOM + VM sandbox so each scenario starts clean.
function makeEnv(options) {
    options = options || {};
    const dom = new JSDOM(options.withButton === false ? BODY_HTML_NO_BUTTON : BODY_HTML, {
        url: 'http://localhost/',
    });
    const document = dom.window.document;
    const conversation = document.getElementById('conversation');
    const pending = document.getElementById('pending-ai-text');

    // Explicit height model: base height plus optional DOM-dependent growth.
    const initialHeight = options.initialHeight === undefined ? 1000 : options.initialHeight;
    const heightMode = options.heightMode || 'static';
    let extraHeight = 0;
    const heightFor = function() {
        if (heightMode === 'conversation') {
            return initialHeight + conversation.childNodes.length * 100 + extraHeight;
        }
        if (heightMode === 'stream') {
            return initialHeight + pending.textContent.length * 10 + extraHeight;
        }
        return initialHeight + extraHeight;
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
        // Browsers clamp to the scrollable range; model the landing position,
        // not just the argument production passed.
        const max = Math.max(0, heightFor() - INNER_HEIGHT);
        sandbox.scrollY = Math.max(0, Math.min(y, max));
    };
    // shell.js registers scroll/resize listeners on window at DOMContentLoaded.
    sandbox.addEventListener = function(type, fn, opts) {
        dom.window.addEventListener(type, fn, opts);
    };
    sandbox.removeEventListener = function(type, fn, opts) {
        dom.window.removeEventListener(type, fn, opts);
    };

    const context = vm.createContext(sandbox);
    new vm.Script(shellJs).runInContext(context);

    const env = {
        dom,
        document,
        conversation,
        pending,
        sandbox,
        scrollCalls,
        height: heightFor,
        button: document.getElementById('jump-latest'),
    };
    // shell.js defers installation until the body exists (it runs from <head>).
    env.install = function() {
        document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
    };
    env.scroll = function() {
        dom.window.dispatchEvent(new dom.window.Event('scroll'));
    };
    env.resize = function() {
        dom.window.dispatchEvent(new dom.window.Event('resize'));
    };
    env.grow = function(px) {
        extraHeight += px;
    };
    return env;
}

const ENTRIES = [
    { type: 'user', text: 'hello' },
    { type: 'tool_call', name: 'first_tool', args: {}, result: { success: true } },
    { type: 'tool_call', name: 'second_tool', args: {}, result: { success: true } },
];

const ENTRIES_JSON = JSON.stringify(ENTRIES);

function hidden(env) {
    // The shell hides the button with a stylesheet rule; the JS toggles the
    // inline display.  Computed style reflects both.
    return env.dom.window.getComputedStyle(env.button).display === 'none';
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

// ---- Default state ----

runSection('1. hidden at the bottom of a short document', function() {
    const env = makeEnv({ initialHeight: 400 });

    assert(hidden(env), 'button starts hidden');
    env.install();
    assert(hidden(env), 'content shorter than the viewport is at the bottom: hidden');
    env.sandbox._updateJumpLatest();
    assert(hidden(env), 'an explicit update keeps it hidden');
});

// ---- Threshold ----

runSection('2. visibility tracks the 80 px follow boundary', function() {
    const env = makeEnv();
    env.install();

    env.sandbox.scrollY = 500; // 0 px from bottom
    env.scroll();
    assert(hidden(env), 'at the bottom: hidden');

    env.sandbox.scrollY = 421; // 79 px from bottom
    env.scroll();
    assert(env.sandbox._shouldScrollBottom() === true,
        '79 px away is inside the follow threshold');
    assert(hidden(env), '79 px away: hidden');

    env.sandbox.scrollY = 420; // exactly 80 px from bottom
    env.scroll();
    assert(env.sandbox._shouldScrollBottom() === false,
        '80 px away is outside the follow threshold');
    assert(!hidden(env), '80 px away: visible');

    env.sandbox.scrollY = 0;
    env.resize();
    assert(!hidden(env), 'resize keeps a scrolled-up reader visible');

    env.sandbox.scrollY = 500;
    env.resize();
    assert(hidden(env), 'resize at the bottom hides it');
});

// ---- Click path ----

runSection('3. clicking the button jumps to the bottom and hides it', function() {
    const env = makeEnv();
    env.install();
    env.sandbox.scrollY = 0;
    env.scroll();
    assert(!hidden(env), 'a scrolled-up reader sees the button');

    env.scrollCalls.length = 0;
    env.button.click();

    assert(env.sandbox.scrollY === env.height() - INNER_HEIGHT,
        'click lands at the clamped bottom (scrollHeight - innerHeight): got '
        + env.sandbox.scrollY);
    const last = env.scrollCalls[env.scrollCalls.length - 1];
    assert(last && last[0] === 0 && last[1] === env.height(),
        'last scroll targets the document bottom: got ' + JSON.stringify(last));
    assert(hidden(env), 'the button hides itself after the jump');
});

runSection('4. follow stays pinned after the jump', function() {
    const env = makeEnv({ heightMode: 'stream' });
    env.install();
    env.sandbox.scrollY = 0;
    env.scroll();
    env.button.click();
    assert(env.sandbox.scrollY === env.height() - INNER_HEIGHT,
        'jumped to the clamped initial bottom');

    env.scrollCalls.length = 0;
    env.sandbox._updateStream('x'.repeat(20), 'follow');

    assert(env.height() === 1200, 'streamed text grew the document: got ' + env.height());
    assert(env.sandbox.scrollY === env.height() - INNER_HEIGHT,
        'follow lands at the clamped new bottom: got ' + env.sandbox.scrollY);
    assert(hidden(env), 'still at the bottom: hidden');
});

// ---- Scrolled-up reader ----

runSection('5. a scrolled-up reader is left alone and sees the button', function() {
    const env = makeEnv({ heightMode: 'stream' });
    env.install();
    env.sandbox.scrollY = 0;
    env.scroll();
    env.scrollCalls.length = 0;

    env.sandbox._updateStream('x'.repeat(20), 'follow');

    assert(env.height() === 1200, 'streamed text grew the document: got ' + env.height());
    assert(env.sandbox.scrollY === 0,
        'follow does not move a scrolled-up reader: got ' + env.sandbox.scrollY);
    assert(env.scrollCalls.length === 0, 'no scroll call for a scrolled-up reader');
    assert(!hidden(env), 'the button stays visible');
});

// ---- DOM-mutating entry points ----

runSection('6. a replacement document hides it', function() {
    const env = makeEnv({ heightMode: 'stream', initialHeight: 400 });
    env.install();
    env.sandbox._updateStream('x'.repeat(60), 'none');
    assert(env.height() === 1000, 'the stream made the document long: got ' + env.height());
    assert(!hidden(env), 'the long document shows the button');

    env.sandbox._updateConversation(ENTRIES_JSON, 'none', true);

    assert(env.height() === 400,
        'the replacement dropped the stream text: got ' + env.height());
    assert(env.sandbox.scrollY === 0, 'replacement starts at the top');
    assert(hidden(env), 'top of a short replacement is the bottom: hidden');
});

runSection('7. _clearConversation hides it', function() {
    const env = makeEnv({ heightMode: 'stream', initialHeight: 400 });
    env.install();
    env.sandbox._updateStream('x'.repeat(60), 'none');
    assert(!hidden(env), 'the long document shows the button');

    env.sandbox._clearConversation();

    assert(env.height() === 400, 'clear dropped the stream text: got ' + env.height());
    assert(env.sandbox.scrollY === 0, 'clear returns to the top');
    assert(hidden(env), 'nothing left to jump to: hidden');
});

runSection('8. _appendEntry re-measures after the DOM write', function() {
    const env = makeEnv({ heightMode: 'conversation' });
    env.install();
    env.sandbox.scrollY = 500; // at the bottom of the 1000 px document
    env.scroll();
    assert(hidden(env), 'at the bottom: hidden');

    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'new' }), 'none');

    assert(env.height() === 1100, 'the append grew the document: got ' + env.height());
    assert(env.sandbox.scrollY === 500, 'none keeps the offset: got ' + env.sandbox.scrollY);
    assert(!hidden(env), 'the reader is now 100 px from the bottom: visible');
});

// ---- Layout-only changes ----

runSection('9. a toggle after layout growth shows the button', function() {
    const env = makeEnv();
    env.install();
    env.sandbox.scrollY = env.height() - INNER_HEIGHT; // at the bottom
    env.scroll();
    assert(hidden(env), 'at the bottom: hidden');

    const details = env.document.createElement('details');
    env.conversation.appendChild(details);
    env.grow(200); // expanding the row pushed the newest output below
    details.dispatchEvent(new env.dom.window.Event('toggle', { bubbles: false }));

    assert(env.height() === 1200, 'the layout grew: got ' + env.height());
    assert(!hidden(env), 'the non-bubbling toggle is captured and reveals the button');
});

runSection('10. a toggle without growth keeps it hidden', function() {
    const env = makeEnv();
    env.install();
    env.sandbox.scrollY = env.height() - INNER_HEIGHT; // at the bottom
    env.scroll();
    assert(hidden(env), 'at the bottom: hidden');

    const details = env.document.createElement('details');
    env.conversation.appendChild(details);
    details.dispatchEvent(new env.dom.window.Event('toggle', { bubbles: false }));

    assert(env.height() === 1000, 'the document did not grow: got ' + env.height());
    assert(hidden(env), 'toggle with no growth leaves it hidden');
});

// ---- Defensive ----

runSection('11. a document without the button does not throw', function() {
    const env = makeEnv({ withButton: false, heightMode: 'conversation' });

    env.install();
    env.sandbox._updateJumpLatest();
    env.sandbox._jumpToLatest();
    env.sandbox._updateStream('streamed text', 'follow');
    env.sandbox._appendEntry(JSON.stringify({ type: 'status', text: 'x' }), 'none');
    env.sandbox._updateConversation(ENTRIES_JSON, 'none');
    env.sandbox._clearConversation();

    assert(env.button === null, 'the fixture has no jump-latest button');
});

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);