"""The claude_limit_alerts table: the Claude usage alert's memory (spec 2026-10-02,
claude-limits-alert). The web's watcher is its one reader and writer.

A claim is an upsert that writes only when it raises the stored percent, so the process whose
claim wrote is the one that posts; a release puts the read value back only while the claim is
still the one it took.
"""

CLAUDE_ALERTED = """
SELECT alerted_percent FROM claude_limit_alerts
WHERE limit_window = %(limit_window)s AND resets_at = %(resets_at)s
"""

CLAIM_CLAUDE_ALERT = """
INSERT INTO claude_limit_alerts AS c (limit_window, resets_at, alerted_percent)
VALUES (%(limit_window)s, %(resets_at)s, %(target)s)
ON CONFLICT (limit_window, resets_at) DO UPDATE SET alerted_percent = EXCLUDED.alerted_percent
WHERE c.alerted_percent < EXCLUDED.alerted_percent
"""

RELEASE_CLAUDE_ALERT = """
UPDATE claude_limit_alerts SET alerted_percent = %(previous)s
WHERE limit_window = %(limit_window)s AND resets_at = %(resets_at)s
    AND alerted_percent = %(target)s
"""
