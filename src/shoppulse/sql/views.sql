-- Reporting views: the analytics layer reads these, never the raw tables.
-- Only revenue-generating orders count (cancelled/pending are excluded).

DROP VIEW IF EXISTS v_sales_enriched;
CREATE VIEW v_sales_enriched AS
SELECT  f.order_id,
        f.order_date,
        d.year, d.quarter, d.month, d.month_name, d.day_name, d.is_weekend,
        c.customer_id, c.full_name, c.city, c.country,
        p.product_sku, p.title, p.category, p.price_band,
        f.quantity, f.unit_price_usd, f.total_usd, f.currency, f.status
FROM fact_orders f
JOIN dim_date     d ON d.date_key     = f.date_key
JOIN dim_customer c ON c.customer_key = f.customer_key
JOIN dim_product  p ON p.product_key  = f.product_key;

DROP VIEW IF EXISTS v_daily_sales;
CREATE VIEW v_daily_sales AS
WITH daily AS (
    SELECT order_date,
           COUNT(*)                    AS orders,
           ROUND(SUM(total_usd), 2)    AS revenue_usd,
           SUM(quantity)               AS units
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
    GROUP BY order_date
)
SELECT order_date, orders, revenue_usd, units,
       ROUND(SUM(revenue_usd) OVER (ORDER BY order_date), 2)                                  AS running_revenue_usd,
       ROUND(AVG(revenue_usd) OVER (ORDER BY order_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS revenue_7d_avg
FROM daily;

DROP VIEW IF EXISTS v_monthly_growth;
CREATE VIEW v_monthly_growth AS
WITH m AS (
    SELECT substr(order_date, 1, 7) AS month, ROUND(SUM(total_usd), 2) AS revenue_usd
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
    GROUP BY 1
)
SELECT month, revenue_usd,
       LAG(revenue_usd) OVER (ORDER BY month) AS prev_month_usd,
       ROUND(100.0 * (revenue_usd - LAG(revenue_usd) OVER (ORDER BY month))
             / NULLIF(LAG(revenue_usd) OVER (ORDER BY month), 0), 1) AS growth_pct
FROM m;

DROP VIEW IF EXISTS v_top_products;
CREATE VIEW v_top_products AS
WITH p AS (
    SELECT product_sku, title, category,
           SUM(quantity) AS units, ROUND(SUM(total_usd), 2) AS revenue_usd
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
    GROUP BY product_sku, title, category
)
SELECT product_sku, title, category, units, revenue_usd,
       RANK() OVER (ORDER BY revenue_usd DESC)                       AS overall_rank,
       RANK() OVER (PARTITION BY category ORDER BY revenue_usd DESC) AS category_rank
FROM p;

DROP VIEW IF EXISTS v_customer_value;
CREATE VIEW v_customer_value AS
WITH c AS (
    SELECT customer_id, full_name, city, country,
           COUNT(*) AS orders, ROUND(SUM(total_usd), 2) AS revenue_usd,
           MIN(order_date) AS first_order, MAX(order_date) AS last_order
    FROM v_sales_enriched
    WHERE status IN ('shipped', 'delivered')
    GROUP BY customer_id, full_name, city, country
)
SELECT *,
       NTILE(4) OVER (ORDER BY revenue_usd DESC) AS value_quartile   -- 1 = best customers
FROM c;

DROP VIEW IF EXISTS v_country_sales;
CREATE VIEW v_country_sales AS
SELECT country, COUNT(*) AS orders, ROUND(SUM(total_usd), 2) AS revenue_usd,
       ROUND(AVG(total_usd), 2) AS avg_order_usd
FROM v_sales_enriched
WHERE status IN ('shipped', 'delivered')
GROUP BY country;
