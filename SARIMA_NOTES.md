# SARIMA Forecasting Model — Technical Notes
## Project: Amateur Apparel POS Forecasting System

---

## 1. What is SARIMA?

SARIMA stands for **Seasonal AutoRegressive Integrated Moving Average**.
Model notation: `SARIMA(p,d,q)(P,D,Q)[s]`

This project uses: **SARIMA(1,1,1)(1,1,1)[7]**

| Parameter | Meaning |
|-----------|---------|
| p=1 | 1 autoregressive lag (yesterday's value influences today) |
| d=1 | 1-order differencing (removes trend so series is stationary) |
| q=1 | 1 moving-average lag (yesterday's forecast error is corrected) |
| P=1 | 1 seasonal autoregressive lag (same day last week influences today) |
| D=1 | 1 seasonal differencing (removes weekly seasonality) |
| Q=1 | 1 seasonal moving-average lag |
| s=7 | Seasonal period = 7 days (weekly pattern) |

---

## 2. How the Forecast is Generated

### Step 1: Build a Full Daily Time Series
- All sales from `sales.json` and `sales_archive.json` are merged.
- A continuous daily time series is created from the **first recorded sale date** to **today**.
- Days with **zero sales** are included as 0 (not skipped). This is critical for SARIMA stationarity.
- The data spans **~1 year** to give SARIMA enough seasonal cycles to learn from.

### Step 2: Handle Sparse Data (Bulk Orders + Store Closures)
**Problem A — Bulk Orders:**
- A single day might have 20+ units, while adjacent days have 1-3.
- Raw SARIMA would over-predict because of these outlier spikes.
- **Solution:** Log-transform (`log(1 + x)`) the series before fitting. This compresses large values and makes variance more uniform.

**Problem B — Store Closures (0-sale days):**
- If the store is closed 3-4 consecutive days, SARIMA sees a long run of zeros.
- This inflates the "zero" signal and causes under-prediction after reopening.
- **Solution:** If more than 60% of data points are zero, we apply **linear interpolation** through zero-runs before fitting. This fills in plausible values between the last sale and the next sale, so the model learns the underlying demand rather than the closure pattern.
- After forecasting, the inverse transform (`exp(x) - 1`) restores original scale.

**Problem C — Short History (< 14 days):**
- Seasonal SARIMA(1,1,1)(1,1,1)[7] requires at least 2 full seasonal periods (14 days).
- If data is too short, the model **falls back to ARIMA(1,1,1)** (non-seasonal).
- If even that fails (e.g. only 1-2 data points), it falls back to a **7-day moving average**.

### Step 3: Fit SARIMA Per Category
- Separate SARIMA models are fitted for **Jackets**, **T-shirts**, and **Shorts**.
- This is because each category has different sales volumes, trends, and seasonal patterns.
- Model fitting uses `statsmodels.tsa.statespace.SARIMAX` with 200 max iterations.
- `enforce_stationarity=False` and `enforce_invertibility=False` are set to avoid convergence errors on small datasets.

### Step 4: Apply Philippine Seasonal Adjustments
After the SARIMA forecast is generated, a **shape adjustment** is applied using Philippine-specific multipliers:

**Day-of-Week Multipliers:**
- Saturday: 1.65x (weekend peak)
- Sunday: 1.50x
- Friday: 1.10x
- Monday–Thursday: 0.85–0.95x

**Monthly Multipliers (Philippine retail seasonality):**
- December: 2.40x (Christmas peak)
- November: 1.75x (pre-Christmas rush, All Saints/Souls' Day)
- January: 1.35x (New Year, gift purchases)
- June: 1.25x (graduation, back-to-school)
- July–August: 0.90x (rainy season slowdown)

**Holiday Multipliers:**
- Christmas Day (Dec 25): 0.10x — store likely closed
- New Year's Day (Jan 1): 0.15x — minimal sales
- Christmas Eve (Dec 24): 0.20x — family gatherings, store may close early
- Maundy Thursday / Good Friday / Black Saturday: 0.25–0.30x
- Most other public holidays: 0.55–0.70x

**How the adjustment works:**
1. SARIMA gives a raw daily forecast (total volume correct, shape based on historical pattern).
2. Multiply each day by its seasonal factor to reshape the curve.
3. Rescale so the **total units match the SARIMA total** — this preserves SARIMA's volume prediction while redistributing units to realistic days.
4. This means: SARIMA determines *how much* to sell; the PH calendar determines *when* to sell it.

### Step 5: Monthly Aggregation (for 3/6/9/12-month forecasts)
- The daily SARIMA forecast is run for the full period (e.g. 365 steps for 12 months).
- Daily results are then summed by `YYYY-MM` to produce a monthly summary table.
- Philippine seasonal notes are displayed alongside each month to help the store owner understand demand drivers.

---

## 3. Why SARIMA Over Simple Moving Average?

| Feature | Simple MA / EMA | SARIMA |
|---------|----------------|--------|
| Uses all historical data | ✅ | ✅ |
| Captures weekly seasonality | ❌ | ✅ |
| Captures long-term trend | Partially | ✅ |
| Handles irregular gaps | ❌ | ✅ (with preprocessing) |
| Seasonal holiday adjustment | ❌ | ✅ (post-processing) |
| Long-range (monthly) forecasts | ❌ (degrades fast) | ✅ |
| Statistically principled | ❌ | ✅ |

---

## 4. Answering Specific Business Questions

### "What if there is a bulk order one day, but no sales on other days?"
- The log-transform compresses the spike so SARIMA doesn't over-predict.
- Sparse zero interpolation fills in the surrounding days with plausible baseline demand.
- The model will predict a moderate level of sales (not a repeat spike, not zero) — reflecting that bulk orders are rare but the underlying demand is non-zero.

### "What if the store is closed 3-4 days in a week?"
- Those days appear as 0 in the time series.
- If >60% of points in the series are zero, interpolation is applied.
- Holiday multipliers (in the seasonal adjustment step) further reduce forecasted sales on known closure days.
- The model correctly distinguishes "closed" (systematic zeros) from "slow day" (low but non-zero) when given enough history.

### "Does store closure affect forecast accuracy?"
- Yes, consistent closures (e.g. every Sunday) will be learned by SARIMA's seasonal component.
- Random closures (e.g. typhoon days) add noise. With 1 year of data, the model averages over these outliers.
- The seasonal multipliers further dampen predictions on high-risk closure days (holidays).

---

## 5. Model Fallback Chain

```
Has ≥ 14 days of data?
    ├── YES → Try SARIMA(1,1,1)(1,1,1)[7]
    │         ├── Success → Apply PH seasonal adjustment → Return forecast
    │         └── Fail → Try ARIMA(1,1,1)
    │                     ├── Success → Apply PH seasonal adjustment → Return forecast
    │                     └── Fail → Use 7-day moving average → Return forecast
    └── NO  → Try ARIMA(1,1,1)
              ├── Success → Apply PH seasonal adjustment → Return forecast
              └── Fail → Use 7-day moving average → Return forecast
```

---

## 6. Dependencies

```
statsmodels >= 0.14  (SARIMAX implementation)
numpy >= 1.24        (array operations, log transform)
flask                (API endpoint /api/sarima_forecast)
```

Install with:
```bash
pip install statsmodels numpy --break-system-packages
```

---

## 7. API Endpoint

`GET /api/sarima_forecast?duration=<dur>`

Duration values:
- `1d` — 1 day (tomorrow)
- `7d` — 7 days (1 week)
- `14d` — 14 days (2 weeks)
- `21d` — 21 days (3 weeks)
- `28d` — 28 days (4 weeks)
- `3m` — 3 months (~90 days)
- `6m` — 6 months (~180 days)
- `9m` — 9 months (~270 days)
- `12m` — 12 months (~365 days)

Response:
```json
{
  "labels": ["2026-05-11", "2026-05-12", ...],
  "forecast": {
    "Jackets":  [3, 2, 5, ...],
    "T-shirts": [4, 3, 6, ...],
    "Shorts":   [2, 1, 4, ...]
  },
  "steps": 90,
  "duration": "3m"
}
```

The frontend (`forecast.html`) calls this endpoint and:
- Renders a **daily table** for weekly forecasts (with holiday badges).
- Renders a **monthly summary table + bar chart** for monthly forecasts.
