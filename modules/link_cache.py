"""
Link cache module for smart navigation v2.

Provides fuzzy search over page links, tabs, and interactive elements.
Supports partial/abbreviated queries without punctuation or case sensitivity.
Collects not only <a href> but also [role="tab"], [role="button"], and other clickable elements.
"""

import logging
import re
from typing import Any, Dict, List, Set

logger = logging.getLogger("FirefoxMCP_LinkCache")

# Stop words to ignore when matching abbreviated queries (RU + EN)
_STOP_WORDS: Set[str] = {
    "и", "в", "на", "с", "по", "к", "о", "об", "от", "до", "за", "из", "у",
    "для", "не", "но", "а", "или", "это", "как", "все", "the", "a", "an",
    "and", "or", "in", "on", "at", "to", "for", "of", "is", "it", "this",
    "that", "with", "from", "by", "as", "are", "was", "were", "be", "been",
}


class LinkCache:
    """Page link and interactive element cache for instant fuzzy search."""

    def __init__(self) -> None:
        self.links: List[Dict[str, str]] = []
        self.current_url: str = ""
        self._dirty: bool = True

    async def refresh(self, page: Any) -> None:
        """Parse all visible links AND interactive elements from the current page."""
        js_code = """
        () => {
            const results = [];
            const seen = new Set();

            const isVisible = (el) => {
                if (!el) return false;
                try {
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                } catch(e) { return false; }
            };

            const getText = (el) => {
                let text = (el.textContent || '').trim().replace(/\\s+/g, ' ').substring(0, 200);
                if (!text) text = el.getAttribute('aria-label') || '';
                if (!text) text = el.getAttribute('title') || '';
                if (!text) text = el.getAttribute('alt') || '';
                if (!text) text = el.getAttribute('placeholder') || '';
                return text.trim();
            };

            // 1. Standard links <a href>
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                if (!href || href === '#' || href.startsWith('javascript:') ||
                    href.startsWith('data:') || seen.has(href)) return;
                if (!isVisible(a)) return;
                seen.add(href);
                const text = getText(a);
                if (!text) return;
                results.push({ text: text, href: href, type: 'link' });
            });

            // 2. JS Tabs and buttons (YouTube chips, navigation tabs, etc.)
            const tabSelectors = [
                '[role="tab"]',
                '[role="button"]',
                'yt-tab-shape',
                'ytd-guide-entry-renderer a',
                'tp-yt-paper-tab',
                '.yt-spec-tabs__item',
                'button.yt-spec-button-shape-next',
                '.chip-cloud-chip',
                'ytd-chip-cloud-chip-renderer',
            ];

            for (const sel of tabSelectors) {
                document.querySelectorAll(sel).forEach(el => {
                    if (!isVisible(el)) return;
                    const text = getText(el);
                    if (!text || text.length < 2) return;

                    // Try to find href if it's inside an <a> or has one
                    let href = '';
                    if (el.tagName === 'A' && el.href) {
                        href = el.href;
                    } else {
                        const innerA = el.querySelector('a[href]');
                        if (innerA) href = innerA.href;
                    }

                    // Create unique key to avoid duplicates
                    const key = href || ('element:' + text.substring(0, 50));
                    if (seen.has(key)) return;
                    seen.add(key);

                    results.push({
                        text: text,
                        href: href,
                        type: href ? 'tab_link' : 'tab_element',
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || ''
                    });
                });
            }

            // 3. Clickable elements with aria-label (like/dislike buttons, icon buttons)
            document.querySelectorAll('button[aria-label], [role="button"][aria-label]').forEach(el => {
                if (!isVisible(el)) return;
                const ariaLabel = el.getAttribute('aria-label') || '';
                const text = getText(el) || ariaLabel;
                if (!text || text.length < 2) return;
                const key = 'btn:' + text.substring(0, 80);
                if (seen.has(key)) return;
                seen.add(key);
                results.push({ text: text, href: '', type: 'button', tag: el.tagName.toLowerCase() });
            });

            return results;
        }
        """
        try:
            self.links = await page.evaluate(js_code)
            self.current_url = page.url
            self._dirty = False
            logger.debug(f"LinkCache refreshed: {len(self.links)} elements found")
        except Exception as e:
            logger.warning(f"LinkCache.refresh error: {e}")
            self.links = []

    async def ensure_fresh(self, page: Any) -> None:
        """Refresh cache if URL changed or cache is marked dirty."""
        if self._dirty or page.url != self.current_url:
            await self.refresh(page)

    def mark_dirty(self) -> None:
        """Mark cache as needing refresh."""
        self._dirty = True

    @staticmethod
    def _normalize(s: str) -> str:
        """Normalize string: lowercase, remove punctuation, collapse whitespace."""
        s = s.strip().lower()
        # Remove punctuation but keep letters, digits, spaces
        s = re.sub(r'[^\w\s]', ' ', s, flags=re.UNICODE)
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    @staticmethod
    def _get_meaningful_words(s: str) -> Set[str]:
        """Extract meaningful words (skip stop words and single chars)."""
        normalized = LinkCache._normalize(s)
        words = set()
        for w in normalized.split():
            if len(w) >= 2 and w not in _STOP_WORDS:
                words.add(w)
        return words

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Fast Levenshtein distance for short strings."""
        la, lb = len(a), len(b)
        if abs(la - lb) > max(5, min(la, lb) // 2):
            return 10 ** 9
        prev = list(range(lb + 1))
        for i in range(1, la + 1):
            curr = [i] + [0] * lb
            for j in range(1, lb + 1):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            prev = curr
        return prev[lb]

    def search(self, query: str) -> List[Dict[str, Any]]:
        """
        Search links by query with priority ordering:
        1. Exact match (case/punctuation insensitive)
        2. Full substring containment
        3. Token subset match (ALL query words present in target) - NEW
        4. Partial token overlap (>=60% query words present) - NEW
        5. Fuzzy match (Levenshtein)
        
        Handles abbreviated queries like "приказ мосты" matching
        "Приказ уничтожить все мосты - снос переправ..."
        """
        q_norm = self._normalize(query)
        if not q_norm:
            return []

        q_words = self._get_meaningful_words(query)

        results_exact: List[Dict[str, Any]] = []
        results_contains: List[Dict[str, Any]] = []
        results_token_subset: List[Dict[str, Any]] = []
        results_partial_overlap: List[Dict[str, Any]] = []
        results_fuzzy: List[Dict[str, Any]] = []

        for link in self.links:
            t_norm = self._normalize(link['text'])

            # Skip empty
            if not t_norm:
                continue

            # 1. Exact match
            if t_norm == q_norm:
                results_exact.append({**link, 'match_type': 'exact', 'score': 0})
                continue

            # 2. Substring containment (query is inside text, or text inside query)
            if q_norm in t_norm or t_norm in q_norm:
                score = abs(len(t_norm) - len(q_norm))
                results_contains.append({**link, 'match_type': 'contains', 'score': score})
                continue

            # 3. Token subset match (ALL query words present in target text)
            if q_words:
                t_words = self._get_meaningful_words(link['text'])
                if q_words.issubset(t_words):
                    # Score based on how many extra words target has
                    extra_words = len(t_words) - len(q_words)
                    results_token_subset.append({**link, 'match_type': 'token_subset', 'score': extra_words})
                    continue

            # 4. Partial token overlap (>=60% query words present)
            if q_words and t_words:
                overlap = q_words.intersection(t_words)
                if len(overlap) >= max(1, int(len(q_words) * 0.6)):
                    overlap_ratio = len(overlap) / len(q_words)
                    results_partial_overlap.append({**link, 'match_type': 'partial_overlap', 'score': 10 - int(overlap_ratio * 10)})
                    continue

            # 5. Fuzzy match (Levenshtein)
            dist = self._levenshtein(q_norm, t_norm)
            if dist <= max(3, len(q_norm) // 3):
                results_fuzzy.append({**link, 'match_type': 'fuzzy', 'score': dist})

        # Combine results in priority order
        all_results = results_exact + results_contains + results_token_subset + results_partial_overlap + results_fuzzy
        # Sort by score (lower is better)
        all_results.sort(key=lambda x: x['score'])
        return all_results[:20]  # Return top 20 results