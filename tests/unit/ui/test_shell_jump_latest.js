/**
 * Unit tests for the jump-to-edge hooks in shell.js.
 *
 * The native wx panel buttons call these functions; the shell no longer owns
 * a floating button and keeps no scroll-position state.  Both hooks land on
 * the requested edge and leave the reader where they are when already there.
 *
 * Uses jsdom.  window.innerHeight and document.body.scrollHeight are set
 * explicitly, and scrollTo is recorded so the calls can be asserted.
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

    const height = options.height === undefined ? 1200 : options.height;
    Object.defineProperty(document.body, 'scrollHeight', {
        get: function() {
            return height;
        },
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
        const max = Math.max(0, height - INNER_HEIGHT);
        sandbox.scrollY = Math.max(0, Math.min(y, max));
    };

    const context = vm.createContext(sandbox);
    new vm.Script(shellJs).runInContext(context);

    return {
        dom,
        document,
        sandbox,
        scrollCalls,
        height: height,
    };
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

function lastCall(env) {
    return env.scrollCalls[env.scrollCalls.length - 1];
}

// ---- Hooks exist ----

runSection('1. the shell exposes both jump hooks', function() {
    const env = makeEnv();

    assert(typeof env.sandbox._jumpToLatest === 'function', '_jumpToLatest is a function');
    assert(typeof env.sandbox._jumpToStart === 'function', '_jumpToStart is a function');
    assert(env.document.getElementById('jump-latest') === null,
        'the shell no longer contains a floating jump button');
});

// ---- Jump to latest ----

runSection('2. _jumpToLatest scrolls to document.body.scrollHeight', function() {
    const env = makeEnv({ height: 1200 });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._jumpToLatest();

    const call = lastCall(env);
    assert(call && call[0] === 0 && call[1] === 1200,
        'scroll targets the full document height: got ' + JSON.stringify(call));
    assert(env.sandbox.scrollY === 1200 - INNER_HEIGHT,
        'the browser lands at the clamped bottom: got ' + env.sandbox.scrollY);
});

runSection('3. _jumpToLatest is a no-op at the bottom', function() {
    const env = makeEnv({ height: 1200 });
    env.sandbox.scrollY = 1200 - INNER_HEIGHT;
    env.scrollCalls.length = 0;

    env.sandbox._jumpToLatest();

    assert(env.sandbox.scrollY === 1200 - INNER_HEIGHT,
        'the reader stays at the bottom: got ' + env.sandbox.scrollY);
});

runSection('4. _jumpToLatest keeps a short document at the top', function() {
    const env = makeEnv({ height: 400 });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._jumpToLatest();

    assert(env.sandbox.scrollY === 0,
        'nothing to scroll in a short document: got ' + env.sandbox.scrollY);
});

// ---- Jump to start ----

runSection('5. _jumpToStart scrolls to the top', function() {
    const env = makeEnv({ height: 1200 });
    env.sandbox.scrollY = 1200 - INNER_HEIGHT;
    env.scrollCalls.length = 0;

    env.sandbox._jumpToStart();

    const call = lastCall(env);
    assert(call && call[0] === 0 && call[1] === 0,
        'scroll targets the top: got ' + JSON.stringify(call));
    assert(env.sandbox.scrollY === 0, 'the reader lands at the top: got ' + env.sandbox.scrollY);
});

runSection('6. _jumpToStart is a no-op at the top', function() {
    const env = makeEnv({ height: 1200 });
    env.sandbox.scrollY = 0;
    env.scrollCalls.length = 0;

    env.sandbox._jumpToStart();

    assert(env.sandbox.scrollY === 0, 'the reader stays at the top: got ' + env.sandbox.scrollY);
});

// ---- Summary ----
console.log('\n' + '='.repeat(50));
console.log(`Results: ${passed} passed, ${failed} failed (${passed + failed} total)`);
process.exit(failed > 0 ? 1 : 0);