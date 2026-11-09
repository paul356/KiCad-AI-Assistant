// Shell HTML template for the WebView conversation panel
// This file is loaded once via SetPage at startup

// Polyfill for localStorage and sessionStorage in WebView2
// WebView2 has Tracking Prevention enabled by default which blocks storage access
// These polyfills provide in-memory storage that works regardless of tracking protection
(function() {
    function createStorage() {
        var store = {};
        return {
            getItem: function(key) { return Object.hasOwn(store, key) ? store[key] : null; },
            setItem: function(key, value) { store[key] = String(value); },
            removeItem: function(key) { delete store[key]; },
            clear: function() { store = {}; },
            get length() { return Object.keys(store).length; },
            key: function(i) { var keys = Object.keys(store); return i < keys.length ? keys[i] : null; }
        };
    }
    try {
        Object.defineProperty(window, 'localStorage', {
            writable: true,
            configurable: true,
            value: createStorage()
        });
    } catch(e) {}
    try {
        Object.defineProperty(window, 'sessionStorage', {
            writable: true,
            configurable: true,
            value: createStorage()
        });
    } catch(e) {}
})();

function _escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _msgBlock(sender, senderColor, bgColor, bodyHtml, timestamp) {
    var ts = timestamp ? '<span style="float:right;font-size:8pt;color:#999;font-weight:normal">' + _escapeHtml(timestamp) + '</span>' : '';
    return '<table class="msg"><tr><td style="background:' + bgColor + '">'
        + '<b><span style="color:' + senderColor + '">' + _escapeHtml(sender)
        + '</span></b>' + ts + '<br>' + bodyHtml + '</td></tr></table>';
}

function _toolCallHtml(e) {
    var r = e.result || {};
    var ok = (typeof r === 'object') ? (r.success !== false) : true;
    var icon = ok ? '\u2713' : '\u2717';
    var iconColor = ok ? '#2e7d32' : '#c62828';
    var css = ok ? 'tool-entry tool-ok' : 'tool-entry tool-err';
    var args = typeof e.args === 'string' ? _escapeHtml(e.args) : _escapeHtml(JSON.stringify(e.args, null, 2));
    var result = typeof e.result === 'string' ? _escapeHtml(e.result) : _escapeHtml(JSON.stringify(e.result, null, 2));
    return '<details class="tools tool-details" style="margin:2px 8px">'
        + '<summary class="tool-summary"><span style="color:' + iconColor + '">' + icon + '</span> '
        + '<span style="color:#444;font-weight:600">\u21B3 ' + _escapeHtml(e.name)
        + '</span></summary>'
        + '<div class="tool-body ' + css + '">'
        + '<span style="color:#444">args:</span><br><pre style="margin:2px 0">' + args + '</pre>'
        + '<span style="color:#444">result:</span><br>'
        + '<pre style="margin:2px 0;max-height:400px;overflow-y:auto">' + result + '</pre>'
        + '</div></details>';
}

function _autorouteHtml(e) {
    var ok = !!e.success;
    var icon = ok ? '\u2713' : '\u2717';
    var iconColor = ok ? '#2e7d32' : '#c62828';
    var css = ok ? 'tool-entry tool-ok' : 'tool-entry tool-err';
    var msg = _escapeHtml(e.message || '');
    var output = _escapeHtml(e.output || '(no output)');
    return '<details class="tools" style="margin:2px 8px">'
        + '<summary><span style="color:' + iconColor + '">' + icon + '</span> '
        + '<b>Auto Route</b> \u2014 ' + msg + '</summary>'
        + '<div class="' + css + '">'
        + '<pre style="margin:4px 0;max-height:300px;overflow-y:auto">'
        + output + '</pre></div></details>';
}

function _pdfTextsHtml(pdfTexts) {
    if (!pdfTexts || pdfTexts.length === 0) return '';
    var html = '';
    for (var i = 0; i < pdfTexts.length; i++) {
        var p = pdfTexts[i];
        var icon = p.error ? '\u2717' : '\u2713';
        var iconColor = p.error ? '#c62828' : '#2e7d32';
        var css = p.error ? 'tool-entry tool-err' : 'tool-entry tool-ok';
        html += '<details class="tools" style="margin:2px 0">'
            + '<summary><span style="color:' + iconColor + '">' + icon + '</span> '
            + '<span style="color:#444;font-weight:600">\uD83D\uDCC4 ' + _escapeHtml(p.name)
            + '</span></summary>'
            + '<div class="tool-body ' + css + '">'
            + '<pre style="margin:2px 0;max-height:300px;overflow-y:auto">'
            + _escapeHtml(p.text || '') + '</pre></div></details>';
    }
    return html;
}

function _renderEntry(e) {
    try {
        var div = document.createElement('div');
        switch (e.type) {
            case 'user':
                var userBody = _escapeHtml(e.text || '').replace(/\n/g, '<br>');
                if (e.pdf_texts && e.pdf_texts.length > 0) {
                    userBody += _pdfTextsHtml(e.pdf_texts);
                }
                div.innerHTML = _msgBlock('You', '#1565C0', '#E3F2FD',
                    userBody, e.timestamp || '');
                break;
            case 'ai':
                div.innerHTML = _msgBlock('AI', '#00695C', '#E8F5E9',
                    e.text || '', e.timestamp || '');
                break;
            case 'tool_call':
                div.innerHTML = _toolCallHtml(e);
                break;
            case 'status':
                div.innerHTML = '<p style="margin:2px 8px"><font color="'
                    + (e.color || '#1E1E1E') + '">' + _escapeHtml(e.text || '')
                    + '</font></p>';
                break;
            case 'autoroute_log':
                div.innerHTML = _autorouteHtml(e);
                break;
            default:
                div.innerHTML = '<p style="margin:2px 8px;color:#999">[unknown: '
                    + _escapeHtml(String(e.type)) + ']</p>';
        }
        return div.firstElementChild || div;
    } catch (err) {
        var d = document.createElement('p');
        d.style.cssText = 'margin:2px 8px;color:#BE6400';
        d.textContent = '[render error: ' + err.message + ']';
        return d;
    }
}

function _shouldScrollBottom() {
    var sh = document.body ? document.body.scrollHeight : 0;
    var sy = window.scrollY || 0;
    var ih = window.innerHeight || 0;
    return (sh - ih - sy) < 80;
}

// ---- Jump to latest ----
// A floating button that returns the reader to the newest output.  Its
// visibility is purely positional: it reuses _shouldScrollBottom(), so it
// shares the 80 px follow threshold instead of duplicating the constant.
function _updateJumpLatest() {
    var btn = document.getElementById('jump-latest');
    if (!btn) return;
    btn.style.display = _shouldScrollBottom() ? 'none' : 'block';
}

function _jumpToLatest() {
    window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    _updateJumpLatest();
}

(function _installJumpLatest() {
    function install() {
        // shell.js runs from <head>, so the button only exists once the body
        // has been parsed.
        var btn = document.getElementById('jump-latest');
        if (!btn) return;
        btn.addEventListener('click', _jumpToLatest);
        window.addEventListener('scroll', _updateJumpLatest, { passive: true });
        window.addEventListener('resize', _updateJumpLatest);
        _updateJumpLatest();
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', install);
    } else {
        install();
    }
})();

window._updateConversation = function(entriesJson, scrollMode, replaceDocument) {
    try {
        // Measure the reader's position before mutating the DOM: new content
        // changes scrollHeight, so a post-mutation check always looks "away".
        var stick = _shouldScrollBottom();
        var conv = document.getElementById('conversation');
        if (!conv) return 'error:no conversation div';
        var frag = document.createDocumentFragment();
        var entries = JSON.parse(entriesJson);
        for (var i = 0; i < entries.length; i++) {
            frag.appendChild(_renderEntry(entries[i]));
        }
        // A rebuild must not move the reader or collapse tool results they
        // expanded: capture both, in document order, before wiping the DOM.
        // A replacement (session load/restore/clear) starts a fresh document
        // and must not inherit state from the previous conversation.
        var replace = replaceDocument === true;
        var sy = replace ? 0 : window.scrollY;
        var openRows = [];
        if (!replace) {
            var oldRows = document.querySelectorAll('details.tools');
            for (var j = 0; j < oldRows.length; j++) {
                openRows.push(oldRows[j].open);
            }
        }
        conv.innerHTML = '';
        conv.appendChild(frag);
        var sw = document.getElementById('stream-wrapper');
        if (sw) sw.style.display = 'none';
        document.getElementById('pending-ai-text').innerHTML = '';
        if (!replace) {
            // Restore by index; a longer new list just gets its defaults (closed).
            var newRows = document.querySelectorAll('details.tools');
            for (var k = 0; k < openRows.length && k < newRows.length; k++) {
                newRows[k].open = openRows[k];
            }
        }
        // A replacement starts at the top (sy === 0); a preserve keeps the
        // reader's offset.  The browser clamps to the new document's range.
        window.scrollTo(0, sy);
        if (scrollMode === 'bottom' || (scrollMode === 'follow' && stick)) {
            window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        }
        _updateJumpLatest();
        return 'ok:' + entries.length;
    } catch (err) {
        return 'error:' + err.message;
    }
};

window._appendEntry = function(entryJson, scrollMode) {
    try {
        // Measure before appendChild: the new entry itself changes the height.
        var stick = _shouldScrollBottom();
        var entry = JSON.parse(entryJson);
        var conv = document.getElementById('conversation');
        if (!conv) return 'error:no conversation div';
        conv.appendChild(_renderEntry(entry));
        var sw = document.getElementById('stream-wrapper');
        if (sw) sw.style.display = 'none';
        document.getElementById('pending-ai-text').innerHTML = '';
        if (scrollMode === 'bottom' || (scrollMode === 'follow' && stick)) {
            window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        }
        _updateJumpLatest();
        return 'ok:' + entry.type;
    } catch (err) {
        return 'error:' + err.message;
    }
};

window._updateStream = function(html, scrollMode) {
    var el = document.getElementById('pending-ai-text');
    if (!el) return;
    // Measure before replacing the streamed text grows the document.
    var stick = _shouldScrollBottom();
    el.innerHTML = html;
    var sw = document.getElementById('stream-wrapper');
    if (sw) sw.style.display = '';
    if (scrollMode === 'bottom' || (scrollMode === 'follow' && stick)) {
        window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    }
    _updateJumpLatest();
};

window._clearConversation = function() {
    document.getElementById('conversation').innerHTML = '';
    var sw = document.getElementById('stream-wrapper');
    if (sw) sw.style.display = 'none';
    document.getElementById('pending-ai-text').innerHTML = '';
    window.scrollTo(0, 0);
    _updateJumpLatest();
};

// Click-to-collapse: results expand via the summary (native toggle).
// Clicking the tool body (args/result area) collapses the details element.
// Text selection is protected from interference:
//  - only a genuine drag (mousedown then movement beyond normal click
//    jitter) is treated as a selection gesture, never collapses
//  - an active selection at click time prevents collapse
//  - double-click (word / line selection) cancels the pending collapse that
//    its first click would otherwise schedule
(function _installToolCollapse() {
    var _downX = 0, _downY = 0, _drag = false, _collapseTimer = null;

    document.addEventListener('mousedown', function(e) {
        _downX = e.clientX;
        _downY = e.clientY;
        _drag = false;
    }, true);
    document.addEventListener('mousemove', function(e) {
        // Human clicks wobble a few pixels; only flag movements well beyond
        // that (10px radius) as drags that could create a text selection.
        var dx = e.clientX - _downX, dy = e.clientY - _downY;
        if (!_drag && (dx * dx + dy * dy) > 100) {
            _drag = true;
        }
    }, true);
    document.addEventListener('dblclick', function() {
        if (_collapseTimer) {
            clearTimeout(_collapseTimer);
            _collapseTimer = null;
        }
    }, true);

    function _onToolBodyClick(e) {
        if (e.button !== undefined && e.button !== 0) return;  // left button only
        var toolBody = e.target.closest('.tool-body');
        if (!toolBody) return;
        if (_drag) return;  // genuine drag-select, not a click
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed) return;  // active selection
        // Resolve the target by DOM position, never by id: getElementById
        // returns the first match, so a duplicate details id (restored
        // session restarts the seq counter) would collapse the wrong row —
        // or no-op on an already closed earlier row.
        var details = toolBody.closest('details.tools');
        if (!details || !details.open) return;
        // Defer: the first click of a double-click would collapse before the
        // dblclick event arrives; cancel the collapse when one follows.
        clearTimeout(_collapseTimer);
        _collapseTimer = setTimeout(function() {
            _collapseTimer = null;
            details.open = false;
        }, 250);
    }
    document.addEventListener('click', _onToolBodyClick);
})();

// ---- Search / Find in conversation ----
// State: array of DOM nodes that contain matches, plus the current index.
window.__findMatches = [];
window.__findIdx = -1;

window._findText = function(query) {
    _clearFind();
    window.__findMatches = [];
    window.__findIdx = -1;
    if (!query || query.length === 0) return '0/0';

    var conv = document.getElementById('conversation');
    if (!conv) return '0/0';

    // Walk all text nodes inside the conversation container
    var walker = document.createTreeWalker(conv, NodeFilter.SHOW_TEXT, null, false);
    var lowerQuery = query.toLowerCase();
    var textNodes = [];
    while (walker.nextNode()) {
        var node = walker.currentNode;
        // Skip nodes inside <style> or <script>
        if (node.parentNode && (node.parentNode.tagName === 'STYLE' || node.parentNode.tagName === 'SCRIPT')) continue;
        // Skip empty / whitespace-only nodes
        if (!node.nodeValue || !node.nodeValue.trim()) continue;
        textNodes.push(node);
    }

    for (var i = 0; i < textNodes.length; i++) {
        var node = textNodes[i];
        var text = node.nodeValue;
        var lower = text.toLowerCase();
        var idx = lower.indexOf(lowerQuery);
        if (idx === -1) continue;

        // Replace all occurrences in this text node with <mark> wrappers
        var parent = node.parentNode;
        var fragment = document.createDocumentFragment();
        var lastEnd = 0;
        while (idx !== -1) {
            // Text before match
            if (idx > lastEnd) {
                fragment.appendChild(document.createTextNode(text.substring(lastEnd, idx)));
            }
            // The match wrapped in <mark>
            var mark = document.createElement('mark');
            mark.className = 'search-match';
            mark.textContent = text.substring(idx, idx + query.length);
            fragment.appendChild(mark);
            window.__findMatches.push(mark);
            lastEnd = idx + query.length;
            idx = lower.indexOf(lowerQuery, lastEnd);
        }
        // Remaining text after last match
        if (lastEnd < text.length) {
            fragment.appendChild(document.createTextNode(text.substring(lastEnd)));
        }
        parent.replaceChild(fragment, node);
    }

    return window.__findMatches.length > 0 ? ('1/' + window.__findMatches.length) : '0/0';
};

window._findTextAndJump = function(query) {
    var result = _findText(query);
    if (window.__findMatches.length > 0) {
        _findJump(0);
    }
    return result;
};

window._findJump = function(idx) {
    if (window.__findMatches.length === 0) return '0/0';
    // De-highlight previous
    for (var i = 0; i < window.__findMatches.length; i++) {
        window.__findMatches[i].classList.remove('search-active');
    }
    // Clamp index
    var n = window.__findMatches.length;
    var newIdx = ((idx % n) + n) % n;
    window.__findIdx = newIdx;
    var el = window.__findMatches[newIdx];
    el.classList.add('search-active');
    // Walk up and expand any parent <details> elements so the match is visible
    var parent = el.parentNode;
    while (parent) {
        if (parent.tagName === 'DETAILS' && !parent.hasAttribute('open')) {
            parent.setAttribute('open', '');
        }
        parent = parent.parentNode;
    }
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return (newIdx + 1) + '/' + n;
};

window._findNext = function() {
    return _findJump((window.__findIdx + 1) % (window.__findMatches.length || 1));
};

window._findPrev = function() {
    return _findJump((window.__findIdx - 1 + (window.__findMatches.length || 1)) % (window.__findMatches.length || 1));
};

window._clearFind = function() {
    var marks = document.querySelectorAll('mark.search-match');
    for (var i = marks.length - 1; i >= 0; i--) {
        var mark = marks[i];
        var parent = mark.parentNode;
        if (parent) {
            parent.replaceChild(document.createTextNode(mark.textContent), mark);
            parent.normalize();
        }
    }
    window.__findMatches = [];
    window.__findIdx = -1;
};
