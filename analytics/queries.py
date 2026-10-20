"""The analytics queries, in one place: used by the verification against the
generator's ground truth, by the benchmark, and (with time filters) by Grafana."""

QUERIES = {
    "events by type": """
        SELECT event_name, count() AS events
        FROM analytics.events GROUP BY event_name ORDER BY event_name""",

    "daily active users": """
        SELECT day, uniqMerge(users) AS users, uniqMerge(sessions) AS sessions
        FROM analytics.daily_users GROUP BY day ORDER BY day""",

    "funnel by channel": """
        SELECT channel,
               countIf(level >= 1) AS viewed_product,
               countIf(level >= 2) AS added_to_cart,
               countIf(level >= 3) AS started_checkout,
               countIf(level >= 4) AS purchased,
               round(purchased / viewed_product, 4) AS conversion
        FROM (
            SELECT session_id, any(channel) AS channel,
                   windowFunnel(3600)(toDateTime(ts), event_name = 'product_view', event_name = 'add_to_cart',
                                      event_name = 'checkout_start', event_name = 'purchase') AS level
            FROM analytics.events
            WHERE event_name IN ('product_view', 'add_to_cart', 'checkout_start', 'purchase')
            GROUP BY session_id)
        GROUP BY channel ORDER BY channel""",

    "checkout to purchase by variant": """
        SELECT variant, countIf(checkout) AS checkouts, countIf(checkout AND bought) AS purchases,
               round(purchases / checkouts, 4) AS rate
        FROM (
            SELECT session_id, any(variant) AS variant,
                   max(event_name = 'checkout_start') AS checkout, max(event_name = 'purchase') AS bought
            FROM analytics.events WHERE event_name IN ('checkout_start', 'purchase') GROUP BY session_id)
        GROUP BY variant ORDER BY variant""",

    "mobile payment rate by app version": """
        SELECT app_version, toDate(min(first_ts)) AS first_day, toDate(max(first_ts)) AS last_day,
               countIf(checkout) AS checkouts, round(countIf(checkout AND bought) / checkouts, 4) AS rate
        FROM (
            SELECT session_id, any(app_version) AS app_version, min(ts) AS first_ts,
                   max(event_name = 'checkout_start') AS checkout, max(event_name = 'purchase') AS bought
            FROM analytics.events
            WHERE device = 'mobile' AND event_name IN ('checkout_start', 'purchase')
            GROUP BY session_id)
        GROUP BY app_version ORDER BY app_version""",

    "weekly retention by channel": """
        SELECT channel, count() AS users,
               round(avg(r[2]), 4) AS week_1, round(avg(r[3]), 4) AS week_2, round(avg(r[4]), 4) AS week_4
        FROM (
            SELECT user_id, any(channel) AS channel,
                   retention(week = first_week, week = first_week + 1, week = first_week + 2,
                             week = first_week + 4) AS r
            FROM (SELECT user_id, channel, toRelativeWeekNum(ts) AS week,
                         min(toRelativeWeekNum(ts)) OVER (PARTITION BY user_id) AS first_week
                  FROM analytics.events WHERE event_name = 'page_view')
            WHERE first_week <= (SELECT toRelativeWeekNum(max(ts)) FROM analytics.events) - 4
            GROUP BY user_id)
        GROUP BY channel ORDER BY week_1 DESC""",

    "revenue by channel": """
        SELECT channel, count() AS orders, sum(revenue) AS total_revenue, round(avg(revenue)) AS avg_order
        FROM analytics.events WHERE event_name = 'purchase' GROUP BY channel ORDER BY total_revenue DESC""",

    "top products": """
        SELECT product_id, countIf(event_name = 'product_view') AS views,
               countIf(event_name = 'purchase') AS orders, round(orders / views, 4) AS view_to_order
        FROM analytics.events WHERE product_id != '' GROUP BY product_id ORDER BY orders DESC LIMIT 10""",
}
