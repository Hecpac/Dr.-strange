"""Check day-1 engagement on the 5 pieces published 2026-05-22."""
import json, time, os, re
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/engagement"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())

PIECES = [
    {
        'id': 'li_v1',
        'name': 'LinkedIn V1 — "I built an AI agent that runs my business 24/7"',
        'url': 'https://www.linkedin.com/feed/update/urn:li:activity:7463676709028941824/',
        'platform': 'linkedin',
        'baseline': {'impressions': 37, 'reactions': 2, 'comments': 0},
    },
    {
        'id': 'x_thread_v1',
        'name': 'X thread V1 (same topic, 8 tweets)',
        'url': 'https://x.com/HectorPach71777/status/2057914349990121964',
        'platform': 'x',
        'baseline': {'views': 4, 'likes': 0, 'reposts': 0},
    },
    {
        'id': 'li_ng_comment',
        'name': 'Comment on Andrew Ng post (Anthropic course)',
        'url': 'https://www.linkedin.com/posts/andrewyng_new-course-build-ai-agents-that-generate-ugcPost-7462912139121352704-H14S/',
        'platform': 'linkedin',
        'baseline': None,
    },
    {
        'id': 'x_unclebob_reply',
        'name': 'Reply to Uncle Bob Martin (swarm-forge)',
        'url': 'https://x.com/unclebobmartin/status/2057907070431543325',
        'platform': 'x',
        'baseline': None,
    },
]

report = []

def extract_li(page):
    """Extract LinkedIn post metrics from current page DOM."""
    js = """
    () => {
        const out = {};
        // Reactions count — usually a button with aria-label like "23 reactions"
        const reactionBtns = Array.from(document.querySelectorAll('button[aria-label*="eaction"], button[aria-label*="React"]'));
        for (const b of reactionBtns) {
            const m = (b.getAttribute('aria-label') || '').match(/(\\d[\\d,.]*)/);
            if (m) { out.reactions_raw = m[1]; break; }
        }
        // Comment count
        const commentSpans = Array.from(document.querySelectorAll('button[aria-label*="omment"], [class*="comments-comment-social-bar"] span'));
        for (const s of commentSpans) {
            const txt = (s.textContent || '').trim();
            const m = txt.match(/^(\\d[\\d,.]*)$/);
            if (m) { out.comments_raw = m[1]; break; }
        }
        // Impressions
        const all = Array.from(document.querySelectorAll('*'));
        for (const el of all) {
            const t = (el.textContent || '').trim();
            const m = t.match(/^(\\d[\\d,.]*)\\s+(impressions?|impresiones)$/i);
            if (m) { out.impressions = m[1]; break; }
        }
        // Fallback impressions from text scan
        if (!out.impressions) {
            const body = (document.body.innerText || '');
            const m = body.match(/(\\d[\\d,.]+)\\s+(impressions?|impresiones)/i);
            if (m) out.impressions = m[1];
        }
        // Repost count
        const body2 = document.body.innerText || '';
        const repMatch = body2.match(/(\\d+)\\s*reposts?/i);
        if (repMatch) out.reposts = repMatch[1];
        return out;
    }
    """
    return page.evaluate(js)

def extract_x(page):
    """Extract X tweet metrics from current page DOM."""
    js = """
    () => {
        const out = {};
        // The main tweet container
        const tweet = document.querySelector('article[data-testid="tweet"]');
        if (!tweet) return out;
        // Look for metric labels via aria-label on the buttons (replies, retweets, likes, views)
        const buttons = Array.from(tweet.querySelectorAll('button, a'));
        for (const b of buttons) {
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            const m = aria.match(/^(\\d[\\d,.\\s]*?)[\\s,]+(repl|retweet|repost|like|bookmark|view)/i);
            if (m) {
                const num = m[1].replace(/[\\s,]/g, '');
                const kind = m[2].toLowerCase();
                if (kind.startsWith('repl')) out.replies = num;
                else if (kind.startsWith('retweet') || kind.startsWith('repost')) out.reposts = num;
                else if (kind.startsWith('like')) out.likes = num;
                else if (kind.startsWith('bookmark')) out.bookmarks = num;
                else if (kind.startsWith('view')) out.views = num;
            }
        }
        // Alternate: scan visible text for numbers next to icons
        const tweetText = tweet.innerText || '';
        const viewMatch = tweetText.match(/(\\d[\\d,.]*)\\s*(K|M)?\\s*(Visualizaciones|views)/i);
        if (viewMatch && !out.views) out.views = viewMatch[1] + (viewMatch[2] || '');
        return out;
    }
    """
    return page.evaluate(js)

def shot(page, label):
    p = f"{ART}/eng_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    for piece in PIECES:
        result = {'piece': piece['name'], 'url': piece['url'], 'platform': piece['platform']}

        # Find or open a tab for the right platform
        page = None
        target_host = 'linkedin.com' if piece['platform'] == 'linkedin' else 'x.com'
        for p in ctx.pages:
            if target_host in p.url:
                page = p
                break
        if not page:
            page = ctx.new_page()
            page.set_viewport_size({'width': 1400, 'height': 950})

        page.goto(piece['url'], wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        # Dismiss any modal
        try:
            for sel in ['button[aria-label*="Dismiss"]', 'button.artdeco-modal__dismiss']:
                els = page.locator(sel)
                for i in range(els.count()):
                    el = els.nth(i)
                    try:
                        if el.is_visible(timeout=800):
                            el.click(timeout=2000)
                    except Exception:
                        continue
        except Exception:
            pass
        time.sleep(2)

        # Scroll a bit to load all metrics
        try:
            page.mouse.wheel(0, 500)
            time.sleep(1)
        except Exception:
            pass

        if piece['platform'] == 'linkedin':
            metrics = extract_li(page)
        else:
            metrics = extract_x(page)

        result['metrics_now'] = metrics
        result['baseline'] = piece.get('baseline')
        result['screenshot'] = shot(page, piece['id'])

        report.append(result)
        print(f"\n=== {piece['name']} ===")
        print(f"URL: {piece['url']}")
        print(f"Baseline: {piece.get('baseline')}")
        print(f"Now: {metrics}")

    # Save full report
    out_path = f"{ART}/engagement_day1_{ts}.json"
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n\nFull report saved: {out_path}")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback; traceback.print_exc()
