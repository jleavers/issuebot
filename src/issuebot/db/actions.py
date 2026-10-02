"""The actions_minutes table's writes. The web's poller is their one caller (spec 2026-10-02).

A reading for an older month than the stored one is ignored rather than written: GitHub lagging
across a month boundary must neither overwrite the new month nor reset ``alerted_percent`` and
repeat last month's alerts. ``RECORD_READING`` then returns no row, which the caller reads as
"ignored".
"""

RECORD_READING = """
INSERT INTO actions_minutes AS a
    (account, period, used_minutes, included_minutes, observed_at, alerted_percent,
     error, error_at)
VALUES
    (%(account)s, %(period)s, %(used_minutes)s, %(included_minutes)s, %(observed_at)s, 0,
     NULL, NULL)
ON CONFLICT (account) DO UPDATE SET
    period = EXCLUDED.period,
    used_minutes = EXCLUDED.used_minutes,
    included_minutes = EXCLUDED.included_minutes,
    observed_at = EXCLUDED.observed_at,
    alerted_percent = CASE WHEN a.period = EXCLUDED.period THEN a.alerted_percent ELSE 0 END,
    error = NULL,
    error_at = NULL
WHERE a.period IS NULL OR EXCLUDED.period >= a.period
RETURNING alerted_percent
"""

RECORD_ERROR = """
INSERT INTO actions_minutes (account, error, error_at)
VALUES (%(account)s, %(error)s, %(error_at)s)
ON CONFLICT (account) DO UPDATE SET error = EXCLUDED.error, error_at = EXCLUDED.error_at
"""

CLAIM_ALERT = """
UPDATE actions_minutes SET alerted_percent = %(target)s
WHERE account = %(account)s AND period = %(period)s AND alerted_percent < %(target)s
"""

RELEASE_ALERT = """
UPDATE actions_minutes SET alerted_percent = %(previous)s
WHERE account = %(account)s AND period = %(period)s AND alerted_percent = %(target)s
"""
