"""Verify rendered text fits narrow panels without hiding timestamps or IDs."""

from playwright.sync_api import Page

LONG_ID = "exec_" + "identity0123456789" * 3
LONG_PATH = "/home/workgate/" + "nested-directory/" * 5 + "details.txt"
TIMESTAMP = "available · 10/16/2026, 8:36:13 AM"


def assert_narrow_text_readable(page: Page) -> None:
    # Exercise the real stylesheet on representative DOM shapes at a 130px
    # text column. Checking textContent alone cannot catch CSS ellipsis.
    failures = page.evaluate(
        """({longId, longPath, timestamp}) => {
            const host = document.createElement('section');
            host.style.cssText = 'position:absolute;left:-10000px;top:0;width:240px';
            document.body.append(host);
            const samples = [
                ['Session timestamp', `<button class="session-entry" style="width:130px"><span class="session-entry-meta">${timestamp}</span></button>`, '.session-entry-meta'],
                ['Session ID', `<dl class="session-detail-grid" style="width:130px;grid-template-columns:minmax(0,1fr)"><div><dd>${longId}</dd></div></dl>`, 'dd'],
                ['Audit timestamp', `<button class="audit-entry" style="width:130px"><span class="audit-entry-meta">${timestamp} · session_start</span></button>`, '.audit-entry-meta'],
                ['Audit details', `<div class="audit-detail-header" style="width:130px"><span>${timestamp}</span></div>`, 'span'],
                ['Dashboard system path', `<dl class="dashboard-details" style="width:130px;grid-template-columns:1fr"><div><dd>${longPath}</dd></div></dl>`, 'dd'],
                ['Dashboard event', `<div class="dashboard-list-header" style="width:130px"><strong>Alarm</strong><span>${longId}</span></div>`, 'span'],
                ['Network throughput', '<div class="dashboard-network-values" style="width:130px"><div><strong>1234.5 GB/s</strong></div><div><strong>1234.5 GB/s</strong></div></div>', 'strong'],
                ['Executor name', `<div class="executor-band-main" style="width:130px"><strong>${longId}</strong></div>`, 'strong'],
                ['Executor version', `<div class="executor-band-main" style="width:130px"><small>${longId}</small></div>`, 'small'],
                ['File name', `<div class="file-browser" style="width:240px"><button class="file-entry"><span class="file-entry-name">${longPath}</span><span class="file-entry-detail">10 KB</span><span class="file-entry-modified">10/16/2026</span></button></div>`, '.file-entry-name'],
                ['File modified date', `<div class="file-browser" style="width:240px"><button class="file-entry"><span class="file-entry-name">name.txt</span><span class="file-entry-detail">10 KB</span><span class="file-entry-modified">10/16/2026</span></button></div>`, '.file-entry-modified'],
                ['File preview name', `<div class="file-preview-header" style="width:130px"><strong>${longPath}</strong></div>`, 'strong'],
                ['File status', `<div id="file-state" style="width:130px">${longPath}</div>`, '#file-state'],
                ['Terminal shell', `<button class="terminal-session" style="width:130px">${longId}</button>`, '.terminal-session'],
                ['Location name', `<button class="file-location-row" style="width:130px"><span>${longId}</span></button>`, 'span'],
                ['Dashboard task', `<button class="dashboard-task-row" style="width:130px"><strong>${longPath}</strong></button>`, 'strong'],
            ];
            const failures = [];
            try {
                for (const [label, markup, selector] of samples) {
                    const holder = document.createElement('div');
                    holder.innerHTML = markup;
                    host.append(holder);
                    const node = holder.querySelector(selector);
                    const style = getComputedStyle(node);
                    const bounds = node.getBoundingClientRect();
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    const overflow = [...range.getClientRects()].some(
                        rect => rect.width > 0 && (rect.right > bounds.right + 2 || rect.left < bounds.left - 2)
                    );
                    const hidden = style.display === 'none' || bounds.width < 1 || bounds.height < 1;
                    const parentBounds = node.parentElement.getBoundingClientRect();
                    const exceedsParent = bounds.right > parentBounds.right + 2;
                    if (hidden || style.textOverflow === 'ellipsis' || style.whiteSpace === 'nowrap' ||
                        node.scrollWidth > node.clientWidth + 2 || overflow || exceedsParent) {
                        failures.push({label, hidden, textOverflow: style.textOverflow,
                            whiteSpace: style.whiteSpace, scrollWidth: node.scrollWidth,
                            clientWidth: node.clientWidth, overflow, exceedsParent});
                    }
                    holder.remove();
                }
            } finally { host.remove(); }
            return failures;
        }""",
        {"longId": LONG_ID, "longPath": LONG_PATH, "timestamp": TIMESTAMP},
    )
    assert not failures, failures
