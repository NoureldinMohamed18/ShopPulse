-- Ad-hoc analytics cookbook (advanced SQL). Run with:  python -m shoppulse sql analytics
-- Each query is separated by a line starting with "-- name:".

-- name: top3_products_per_category
-- CTE + ROW_NUMBER: best three products inside every category
WITH ranked AS (
    SELECT category, title, ROUND(SUM(total_usd), 2) AS revenue_usd,
           ROW_NUMBER() OVER (PARTITION BY category ORDER BY SUM(total_usd) DESC) AS rn
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
    GROUP BY category, title
)
SELECT category, title, revenue_usd FROM ranked WHERE rn <= 3 ORDER BY category, rn;

-- name: customer_repeat_purchase_gap
-- LAG: days between consecutive orders of the same customer
WITH o AS (
    SELECT customer_id, order_date,
           LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS prev_date
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
)
SELECT customer_id, ROUND(AVG(julianday(order_date) - julianday(prev_date)), 1) AS avg_gap_days, COUNT(prev_date) AS repeat_orders
FROM o
WHERE prev_date IS NOT NULL
GROUP BY customer_id
ORDER BY repeat_orders DESC
LIMIT 20;

-- name: cancellation_rate_by_country
-- Conditional aggregation
SELECT country,
       COUNT(*) AS orders,
       ROUND(100.0 * SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) / COUNT(*), 1) AS cancel_pct
FROM v_sales_enriched
GROUP BY country
ORDER BY cancel_pct DESC;

-- name: weekend_vs_weekday
SELECT CASE is_weekend WHEN 1 THEN 'weekend' ELSE 'weekday' END AS day_type,
       COUNT(*) AS orders, ROUND(AVG(total_usd), 2) AS avg_order_usd
FROM v_sales_enriched
WHERE status IN ('shipped', 'delivered')
GROUP BY is_weekend;

-- name: customers_who_moved
-- SCD2 in action: customers that have more than one historical version
SELECT customer_id, COUNT(*) AS versions, MIN(valid_from) AS first_seen, GROUP_CONCAT(city, ' -> ') AS city_history
FROM (SELECT * FROM dim_customer WHERE customer_id > 0 ORDER BY customer_id, valid_from)
GROUP BY customer_id
HAVING COUNT(*) > 1;

-- name: browse_to_buy_funnel
SELECT event_type, SUM(n_events) AS events,
       ROUND(100.0 * SUM(n_events) / (SELECT SUM(n_events) FROM fact_events_daily WHERE event_type = 'page_view'), 1) AS pct_of_views
FROM fact_events_daily
WHERE event_type IN ('page_view', 'add_to_cart', 'purchase')
GROUP BY event_type
ORDER BY events DESC;
