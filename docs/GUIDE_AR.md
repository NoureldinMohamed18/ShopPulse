# شرح مشروع ShopPulse (الـ Capstone) - بالعربي

> الملف ده بنفس أسلوب مشاريعنا: **الفكرة ← المتطلبات ← المشاكل ← الخطوات ← الكود**.
> الهدف إنك تفهم كل سطر وتقدر تشرحه في المقابلة، مش بس تشغّله.

---

## 1) الفكرة

متجر إلكتروني عنده بيانات متفرقة في 4 أماكن مختلفة، والإدارة عايزة تعرف: *بنبيع كام؟ إيه أكتر منتج؟ العملاء بيشتروا منين؟ والداتا نضيفة ولا لأ؟*

دورك كـ Data Engineer إنك تبني **المنصة** اللي بتجمع وتنضّف وتخزّن وتقدّم البيانات دي بشكل **تلقائي وموثوق**. ده بالظبط اللي بيتطلب من الـ junior في أي شركة.

```
4 مصادر  →  Extract  →  Transform + فحص جودة  →  Load  →  Warehouse  →  API / Dashboard / Report
                                  ↓
                          بيانات مرفوضة (quarantine)
```

## 2) المتطلبات (وإزاي اتغطت)

| المتطلب | الحل في المشروع | الملف |
|---|---|---|
| قراءة من قاعدة بيانات علائقية | SQLite (طلبات + عملاء) | `sources/oltp.py` |
| Web scraping | كتالوج المنتجات بـ BeautifulSoup + pagination | `sources/catalog.py` |
| REST API خارجي | أسعار العملات + retries + fallback | `sources/rates.py` |
| NoSQL | أحداث التصفح في MongoDB + aggregation + indexes | `sources/events.py` |
| تنضيف وتحويل | Pandas / NumPy | `transform.py` |
| جودة بيانات | قواعد تفحص قبل الـ warehouse | `quality.py` |
| Data Warehouse | Star schema + SCD2 | `sql/ddl.sql`, `warehouse.py` |
| SQL متقدم | Window functions, CTEs, Views, Triggers, Indexes | `sql/*.sql` |
| تقديم النتائج | Flask API + Dash + Seaborn | `api.py`, `dashboard.py`, `report.py` |
| أتمتة | Airflow DAG | `dags/shoppulse_dag.py` |
| نشر | Docker + docker-compose + GitHub Actions | `Dockerfile`, `.github/` |
| مراقبة | logging + run history + `/metrics` + `/api/monitoring` | `monitoring.py` |
| اختبارات | pytest (91 اختبار، coverage حوالي 95%) | `tests/` |

## 3) المشاكل اللي واجهتنا (دي أهم جزء في المقابلة)

| المشكلة | ليه بتحصل | الحل |
|---|---|---|
| order id مكرر | نفس الطلب اتبعت مرتين | نحتفظ بآخر نسخة (`drop_duplicates`) |
| كمية سالبة / سعر فاضي / تاريخ غلط / status غريب | إدخال بشري أو bug | الصف يروح **quarantine** مع `reject_reason` (مش بيتمسح بصمت) |
| API العملات يقع (503) أو يرجّع JSON بايظ | حاجة برا سيطرتنا | retry بـ backoff ← بعدين آخر نسخة محفوظة ← بعدين أسعار ثابتة |
| صفحة scraping تفشل | الموقع بطيء/واقع | نتخطاها ونكمّل، مش بنوقف الـ run كله |
| تشغيل الـ pipeline مرتين | إعادة تشغيل بعد فشل | **idempotent**: upsert، مفيش تكرار |
| عميل غيّر مدينته | الداتا بتتغير مع الوقت | **SCD2**: نسخة قديمة + نسخة جديدة، والطلبات القديمة تفضل على المدينة القديمة |
| طلب جه قبل ما العميل يتسجل | ترتيب وصول البيانات | بنربطه بـ "Unknown member" (مفتاح `-1`) بدل ما يفشل |
| نقرا كل الداتا كل مرة (بطيء) | الحجم بيكبر | **incremental** بـ watermark على `updated_at` |
| فشل في نص الـ load | disk/connection | transaction واحدة: يا كله يتحمّل يا ولا حاجة (rollback) |
| الـ pipeline "نجح" بس الأرقام غلط | مفيش خطأ تقني | فحوصات جودة + مراقبة منطق الأعمال (نزول مفاجئ في الإيراد) |

## 4) ربط المشروع بمحتوى الكورس

| المحتوى | بيظهر في |
|---|---|
| Overview of Data Engineering (ETL، lifecycle: ingestion ← transformation ← storage ← serving، Airflow للـ orchestration) | `pipeline.py` بمراحله التلاتة + `dags/` |
| Data Pipeline Session 1 (data integration، ETL مقابل ELT، batch / incremental / CDC) | ETL batch + extraction تزايدي بالـ watermark |
| Data Pipeline Session 2 (الـ pipeline كـ DAG، Airflow: scheduler, DAG files, retries) | `dags/shoppulse_dag.py` |
| Python for Data Engineering + Pandas + NumPy + Advanced Pandas | `transform.py` (`np.select`, `groupby`, `pivot_table`, `merge`, `map`) |
| Databases & SQL + Advanced SQL | `sql/` (JOIN, CTE, `LAG`, `RANK`, `NTILE`, moving average، triggers، indexes، views) |
| Python for Databases & APIs | `sqlite3` + `requests` في `sources/` |
| NoSQL / MongoDB | `sources/events.py` |
| Web Scraping | `sources/catalog.py` |
| Data Warehouse & Modeling | star schema, SCD2, surrogate keys, `dim_date` |
| Matplotlib / Seaborn (EDA) | `report.py` |
| Plotly Dash | `dashboard.py` |
| CI/CD, DataOps, GitHub Actions | `.github/workflows/ci.yml` |
| Deployment Essentials (dev/test/prod، Docker، Git، clean code، modularization) | `Dockerfile`, `docker-compose.yml`, `SHOPPULSE_ENV`, هيكل الـ packages |
| Monitoring & Debugging (4 أبعاد: operational / data quality / resources / business) | `monitoring.py` بيغطي الأربعة |

**اللي مش موجود (بقرارك: مفيش cloud):** Cloud Foundations, Azure Synapse Pipelines, Stream Analytics, Get started on Azure. دول **النسخة التانية** الطبيعية من المشروع (نفس الفكرة على Azure + streaming) - اذكرها في المقابلة كـ "next steps".

## 5) إزاي تشغّله (خطوة بخطوة)

```bash
# 1) البيئة
python -m venv .venv && source .venv/bin/activate        # على ويندوز: .venv\Scripts\activate
pip install -r requirements-dev.txt && pip install -e .

# 2) كل حاجة بأمر واحد (بدون نت)
python -m shoppulse demo
#    هيعمل: seed للمصادر ← pipeline كامل ← تقرير في data/reports

# 3) شغّل الخدمات
python -m shoppulse api            # http://127.0.0.1:5000/api/kpis
python -m shoppulse dashboard      # http://127.0.0.1:8050

# 4) الاختبارات والـ lint
pytest --cov=shoppulse
ruff check .
```

## 6) تجارب بإيدك (دي اللي هتخليك تفهم فعلًا)

**التجربة 1 - Incremental:** المتجر بيبيع أكتر، والـ pipeline يقرا الجديد بس.
```bash
python -m shoppulse seed --days 5
python -m shoppulse run --offline          # شوف سطر "Extracted ... since=..." في اللوج
```
لاحظ إنه قرا طلبات الأيام الجديدة + صفوف الحدّ الأخير (عمدًا: `>=` مش `>` - ليه؟ اقرا README).

**التجربة 2 - SCD2:** عميل يغيّر مدينته.
```bash
python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/oltp/shop_oltp.db")
c.execute("UPDATE customers SET city='Atlantis', country='Nowhere' WHERE customer_id=7")
c.commit()
EOF
python -m shoppulse run --offline --as-of 2026-12-01
python -m shoppulse sql customers_who_moved      # هتشوف: القديمة -> Atlantis
```

**التجربة 3 - تكسّر الجودة:** امسح عميل عنده طلبات.
```bash
python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/oltp/shop_oltp.db")
c.execute("DELETE FROM customers WHERE customer_id=9")
c.commit()
EOF
python -m shoppulse run --mode full --offline    # هيقف برسالة واضحة، والـ warehouse ما اتلمسش
python -m shoppulse status                       # الـ run مسجل failed
```

**التجربة 4 - الـ API يكتب:** طلب جديد يعدّي على كل الطبقات.
```bash
curl -X POST localhost:5000/api/orders -H 'Content-Type: application/json' \
     -d '{"customer_id":1,"product_sku":"SKU-0001","quantity":2,"unit_price":15.5}'
python -m shoppulse run --offline
curl localhost:5000/api/kpis
```
(لو ضبطت `SHOPPULSE_API_KEY` ضيف `-H 'X-API-Key: ...'`.)

**التجربة 5 - SQL المتقدم:** `python -m shoppulse sql list` وبعدين شغّل كل query واقرا الـ SQL بتاعها في `sql/analytics.sql`.

## 7) أسئلة مقابلة متوقعة (وإجاباتها من المشروع)

1. **إيه الفرق بين ETL و ELT؟ وإنت استخدمت إيه؟** ETL: بنحوّل قبل التحميل. استخدمنا ETL لأننا عايزين نفحص الجودة ونرفض الصفوف الوحشة *قبل* ما توصل للـ warehouse.
2. **يعني إيه idempotent ولماذا مهم؟** تشغيل نفس الـ load مرتين يدّي نفس النتيجة. مهم عشان أي إعادة تشغيل بعد فشل ما تكرّرش الداتا. عندنا upsert + row hash + اختبارات بتثبت ده.
3. **إزاي بتتعامل مع تغيّر بيانات العميل؟** SCD Type 2: بنقفل النسخة القديمة (`valid_to`) وبنضيف جديدة. والـ fact بيربط بالنسخة الصحيحة وقت الطلب (point-in-time join).
4. **ليه surrogate keys؟** عشان نقدر نحتفظ بأكتر من نسخة لنفس الـ business key، وعشان مانتأثرش لو الـ source غيّر المفاتيح.
5. **إزاي بتقرا الداتا الجديدة بس؟** watermark على `updated_at`. بنستخدم `>=` وبنعتمد إن الـ load idempotent، فالقراءة المتكررة لصفوف الحدّ آمنة.
6. **لو الـ API الخارجي وقع؟** retries بـ exponential backoff، وبعدها نسخة محفوظة، وبعدها قيم ثابتة، ويتسجل warning في اللوج.
7. **الداتا الوحشة بتروح فين؟** quarantine مع سبب الرفض. ما بنمسحش حاجة بصمت.
8. **إزاي بتراقب الـ pipeline؟** 4 أبعاد: تشغيلي (نجاح/مدة/تأخر)، جودة بيانات، موارد (ذاكرة/ديسك)، ومنطق أعمال (نزول الإيراد >40%). متاح عبر `/api/monitoring` و`/metrics` (Prometheus).
9. **ليه Docker و CI؟** نفس البيئة في أي مكان، والـ CI بيشغّل lint + اختبارات + smoke test عند كل push قبل ما حاجة توصل للـ main.
10. **إيه اللي هتعمله لو الداتا كبرت 100 ضعف؟** SQLite ← PostgreSQL أو cloud warehouse، Spark/chunking للـ transform، partitioning، orchestration مُدار (Azure Data Factory / Airflow مُدار)، وstreaming للأحداث.

## 8) قبل ما تقدّم بيه - Checklist مهم

الجزء ده اتجرّب فعليًا أثناء بناء المشروع: **الاختبارات (91 ناجح)، الـ lint، الـ CLI، الـ API على gunicorn، سيرفر الـ dashboard وcallback بتاعه، والتجارب 1-4.**

الأجزاء دي اتكتبت بس **ماتشغلتش** في بيئة البناء (مفيش Docker ولا Airflow ولا GitHub هناك)، فلازم تجرّبها إنت قبل ما تحطها على الـ CV:

- [ ] `docker compose up --build` شغّال وبتفتح `localhost:5000/api/kpis` و`localhost:8050`.
- [ ] `docker compose --profile airflow up` وتظهر الـ DAG `shoppulse_daily` في الـ UI وتشغّلها مرة (ممكن تحتاج تعدّل إصدار صورة Airflow أو الـ requirements).
- [ ] تعمل push على GitHub وتتأكد إن الـ Actions كلها خضرا.
- [ ] تاخد screenshots: الـ dashboard، الـ Actions الخضرا، الـ Airflow graph، وصور من `data/reports/`.

لو أي حاجة فيهم فشلت، ابعتلي رسالة الخطأ ونصلحها مع بعض.

## 9) إزاي تعرضه

**GitHub:**
```bash
cd shoppulse && git init && git add . && git commit -m "ShopPulse: end-to-end data platform"
git branch -M main
git remote add origin https://github.com/<username>/shoppulse.git
git push -u origin main
```
(مجلد `data/` متعمله ignore، ما بيترفعش.)

**في الـ CV (مثال، عدّل الأرقام بعد ما تجرّب):**
> **ShopPulse - End-to-end data platform** (Python, SQL, Pandas, MongoDB, Flask, Airflow, Docker, GitHub Actions)
> - Built an ETL pipeline integrating 4 sources (SQL, web scraping, REST API, MongoDB) into a star-schema warehouse with SCD Type 2 and incremental, idempotent loads.
> - Added a data-quality gate, row quarantine, run history and monitoring (operational, quality, resource, business); 91 automated tests (~95% coverage) with CI on GitHub Actions.
> - Served results through a REST API, Plotly Dash dashboard and Seaborn report; containerised with Docker Compose and orchestrated with an Airflow DAG.

**في المقابلة:** ابدأ بالمشكلة (جدول القسم 3)، مش بالأدوات. وقول بصراحة إن الداتا محاكاة، وإن الـ cloud هو الخطوة الجاية - ده بيدّي مصداقية.
