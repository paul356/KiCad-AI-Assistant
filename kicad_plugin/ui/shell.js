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
    var uid = 'tool_' + (e._seq || Math.random().toString(36).slice(2));
    return '<details class="tools tool-details" id="' + uid + '" style="margin:2px 8px">'
        + '<summary class="tool-summary"><span style="color:' + iconColor + '">' + icon + '</span> '
        + '<span style="color:#444;font-weight:600">\u21B3 ' + _escapeHtml(e.name)
        + '</span></summary>'
        + '<div class="tool-body ' + css + '" data-details="' + uid + '">'
        + '<span style="color:#444">args:</span><br><pre style="margin:2px 0">' + args + '</pre>'
        + '<span style="color:#444">result:</span><br><pre style="margin:2px 0">' + result + '</pre>'
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
        var uid = 'pdf_' + i + '_' + Math.random().toString(36).slice(2);
        html += '<details class="tools" id="' + uid + '" style="margin:2px 0">'
            + '<summary><span style="color:' + iconColor + '">' + icon + '</span> '
            + '<span style="color:#444;font-weight:600">\uD83D\uDCC4 ' + _escapeHtml(p.name)
            + '</span></summary>'
            + '<div class="tool-body ' + css + '" data-details="' + uid + '">'
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

window._updateConversation = function(entriesJson, scrollBehavior) {
    try {
        var conv = document.getElementById('conversation');
        if (!conv) return 'error:no conversation div';
        var frag = document.createDocumentFragment();
        var entries = JSON.parse(entriesJson);
        for (var i = 0; i < entries.length; i++) {
            frag.appendChild(_renderEntry(entries[i]));
        }
        conv.innerHTML = '';
        conv.appendChild(frag);
        var sw = document.getElementById('stream-wrapper');
        if (sw) sw.style.display = 'none';
        document.getElementById('pending-ai-text').innerHTML = '';
        if (scrollBehavior === 'bottom') {
            window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        }
        return 'ok:' + entries.length;
    } catch (err) {
        return 'error:' + err.message;
    }
};

window._appendEntry = function(entryJson, scrollBehavior) {
    try {
        var entry = JSON.parse(entryJson);
        var conv = document.getElementById('conversation');
        if (!conv) return 'error:no conversation div';
        conv.appendChild(_renderEntry(entry));
        var sw = document.getElementById('stream-wrapper');
        if (sw) sw.style.display = 'none';
        document.getElementById('pending-ai-text').innerHTML = '';
        if (scrollBehavior === 'bottom') {
            window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        }
        return 'ok:' + entry.type;
    } catch (err) {
        return 'error:' + err.message;
    }
};

window._updateStream = function(html, scrollToBottom) {
    var el = document.getElementById('pending-ai-text');
    if (!el) return;
    el.innerHTML = html;
    var sw = document.getElementById('stream-wrapper');
    if (sw) sw.style.display = '';
    if (scrollToBottom) {
        window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
    }
};

window._clearConversation = function() {
    document.getElementById('conversation').innerHTML = '';
    var sw = document.getElementById('stream-wrapper');
    if (sw) sw.style.display = 'none';
    document.getElementById('pending-ai-text').innerHTML = '';
    window.scrollTo(0, 0);
};

// Click-to-collapse: clicking the tool body (args/result area) collapses
// the details element.  Clicking the summary still expands natively.
// Text selection is preserved: if the user has an active text selection
// the click does NOT collapse (allowing copy via Ctrl+C).
(function _installToolCollapse() {
    function _onToolBodyClick(e) {
        var toolBody = e.target.closest('.tool-body');
        if (!toolBody) return;
        // Don't collapse if the user is selecting / copying text.
        var sel = window.getSelection();
        if (sel && sel.type === 'Range' && sel.toString().length > 0) return;
        var detailsId = toolBody.getAttribute('data-details');
        if (!detailsId) return;
        var details = document.getElementById(detailsId);
        if (details && details.open) {
            details.open = false;
        }
    }
    if (document.readyState !== 'loading') {
        document.addEventListener('click', _onToolBodyClick);
    } else {
        document.addEventListener('DOMContentLoaded', function() {
            document.addEventListener('click', _onToolBodyClick);
        });
    }
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
