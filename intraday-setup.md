# ORB Intraday Bot - Strategy Specification (Paper-Trading Bot)

## 1. Purpose and Source of Truth

This document is the rule set for the Python paper-trading bot. It is built from the teacher's notes (`aj-intraday-setup-notes.docx`). Where the notes and the Pine indicator disagree, **the notes win**. The Pine indicator (`AJ_Intraday_ORB_Strategy_ATR.pine`) is only a reference. The previous Pine-oriented version of this document is kept in `intraday-setup.pine-version.md`.

Items marked **[Assumption]** are not stated in the notes. They are defaults chosen to make the rule testable, and each one is a config flag.

**Scope**
- Instrument: **Nifty futures only** (current-month contract), using 3-minute candles. Bank Nifty is out of scope.
- Trades: paper by default. A broker order is sent only with `LIVE_MODE=live`, `LIVE_ARMED=true`, `DRY_RUN=false`. Telegram `/stop` and `/start` switch a day off and on. With `DRY_RUN=true` (the default) the same pipeline runs but the broker order step is skipped (see `docs/RUNBOOK.md`).
- The bot records signals, tracks exits and calculates P&L.
- A long signal buys a CE option. A short signal buys a PE option.
- Every 3-minute candle produces a clear report to the user.

**Chart rule:** always use the Nifty futures chart and futures price. The notes allow the spot chart for freak highs and lows, but this bot never uses it. All levels (ORH, ORL, pivots), signals, stops, targets and zone checks use the futures price. Spot is not used for any decision.

**Contract roll:** the bot uses the current-month Nifty futures contract, as on the "continuous: current contract in front" chart in the notes. It stays on that contract through its expiry day and rolls to the next contract on the first session after expiry. The expiry date comes from the broker's expiry list, not a hard-coded rule. A contract is never changed mid-session. Pivots use the previous session's high, low and closing price of the same contract, as its daily bar shows them (see section 4). A `roll_days_before_expiry` setting (default 0) lets you roll earlier if liquidity thins out near expiry.

## 2. Opening Range (ORH / ORL)

The notes' rule: the opening range ends at the point where price stops making higher highs or lower lows.

1. **ORH:** from the first candle of the day, follow the run of candles that each make a higher or equal high than the one before. The first candle with a lower high ends the run. The peak candle's high is the ORH. An equal high extends the run.
2. **ORL:** the same on the low side. Follow candles that each make a lower or equal low. The first candle with a higher low ends the run. The lowest candle's low is the ORL. An equal low extends the run.
3. There is no fixed number of candles. The run can be the first candle alone.
4. The two runs are tracked separately. Each level is set once per day at the close of the candle that ends its run, and is reset on the next trading day.

Formulas:
- ORH run continues while `high >= high[1]`. The first candle with `high < high[1]` ends it, and ORH = `high[1]`.
- ORL run continues while `low <= low[1]`. The first candle with `low > low[1]` ends it, and ORL = `low[1]`.
- Example: highs 24,520, 24,540, 24,560, 24,550 give ORH 24,560. Highs 24,520, 24,540, 24,540, 24,530 give ORH 24,540.

## 3. Entry Setups

All setups below are mirrored for the short side. "Swing" means the pullback high or low before the retest.

### 3.1 Standard ORB Long

- **Sign:** a 3-minute candle closes above ORH.
- **Retest:** price comes back down and retests ORH.
- **Confirmation:** price then breaks above the previous swing or candle high. A break is enough, and a close is not required.
  - **[Assumption]** "Previous swing" is the most recent swing high before the retest pullback (a candle whose high is not below the one before it and is above the one after it, from the sign candle to the retest candle). If there is none, it is the highest high from the sign candle through the retest candle.
  - **[Assumption]** The retest counts if the low comes within 1 point of ORH (config `retest_tolerance_points`). This absorbs tick differences between data feeds (Groww and TradingView differ by up to about 0.7 point on a high).
  - One retest per sign. Retest and confirmation are on different candles.
- **Entry:** the previous candle's low.
  - **[Assumption]** The notes use the same words for the short side. I read it as a mirror: the previous candle's high.
  - **[Assumption]** The paper fill happens at that price only if price traded there. Otherwise it fills at the confirmation candle's close and is labelled as such.
- **Stop:** see Section 5.

### 3.2 Standard ORB Short

- **Sign:** a candle closes below ORL.
- **Retest:** price comes up and retests ORL.
- **Confirmation:** price breaks below the previous swing or candle low.
- **Entry:** the mirror of 3.1.

### 3.3 Bull Trap (short)

- **Sign:** after an ORH breakout, a candle closes back below ORH.
- **Reverse retest:** the next pullback candle (green). It does not need to touch ORH.
- **Confirmation and entry:** price breaks the low of the pullback candle. Entry is at that low.
- **Stop and target:** the stop is ORH or 35 points. The target is ORL.

### 3.4 Bear Trap (long)

- **Sign:** after an ORL breakdown, a candle closes back above ORL.
- **Reverse retest:** the next pullback candle (red). It does not need to touch ORL.
- **Confirmation and entry:** price breaks the high of the pullback candle. Entry is at that high.
- **Stop and target:** the stop is ORL or 35 points. The target is ORH.

### 3.5 ORB Follow-up Failure (Case 1 and Case 2)

- **Case 1:** the ORB confirmation never came. Price closed back inside the OR, and a bull trap or bear trap formed.
  - More chance of a reverse retest, and fewer participants are trapped.
- **Case 2:** the ORB was confirmed, then the follow-up failed and price closed back inside the OR.
  - More participants are trapped, there is no reverse retest, and momentum is higher.
- **Entry (both cases):** an aggressive entry at the close of the candle that closes back inside the OR. Short for a bull trap, long for a bear trap.
- **Stop:** the standard 35 points.
- **Neutral zone:** aggressive trades are not allowed there. These trades wait for the confirmation entry in 3.3 or 3.4 instead.

### 3.6 Additional Whipsaws

- **After an ORB:** price rejects the OR level two or more times after the confirmation, and the stop does not hit. This is a re-entry for continuation, because momentum comes after the whipsaws.
- **After a bull trap or bear trap:** two or more rejections at ORH or ORL after the trap confirmation. This is also a continuation re-entry.
- **Case 1 trades** are more likely to see these whipsaws, because fewer sellers are trapped and there is no panic.
- **Detection [Assumption]:** two or more rejections at the level, with no close beyond the stop.

### 3.7 Consolidation Breakout (direct entry)

- **Conditions:** multiple rejections at ORH or ORL for 45 to 60 minutes, then a candle closes above ORH or below ORL.
  - **[Assumption]** "Multiple" means at least 3 touches.
- **Entry:** directly at the breakout. There is no retest.
- **Stop:** 35 points on futures.
- **Target:** the OR range projected from ORH or ORL, or the pivot targets, whichever is nearer.
- **Expectation:** good momentum.

### 3.8 Whipsaws and Minor Price Action

- **Rule of LL and HH:** every rise has at least one lower low, and every fall has at least one higher high.
- **2LL and 3LL:** inside a rise, a pullback that makes two or three lower lows is a whipsaw. Entry is long on the break above the pullback's high (continuation).
- **2HH and 3HH:** inside a fall, a bounce that makes two or three higher highs is a whipsaw. Entry is short on the break below the bounce's low (continuation).
- **Stop for these:** [Assumption] the structure's extreme or 35 points, whichever is closer.
- **Consolidation + fake breakdown:** a sharp move up. Look for a long. The mirror is a fake breakout followed by a sharp move down, which is a short.
  - Entry is near the 50% retracement of the sharp reversal leg.
  - The stop is beyond the fake extreme, or 35 points. The target is the opposite end of the consolidation.
  - **[Assumption]** The 50% reference leg is from the fake extreme to the low (or high) of the reversal.
- **Both sides trap and move:** the notes give the title only, with no rules. This is off by default and not traded.

### 3.9 Trending Day

Two or more setups in the same direction mark a trending day.

- **Downtrend:** Bull Trap + ORL Breakdown, or Failed Bear Trap + ORL Breakdown.
- **Uptrend:** Bear Trap + ORH Breakout, or Failed Bull Trap + ORH Breakout.
- It is shown to the user as a label. It does not change the trade rules.
- **[Assumption]** A Failed Bear Trap is a bear trap whose reversal fails: price closes back below ORL, which is a short signal. A Failed Bull Trap is the mirror.

## 4. Pivots and Zones

- **Pivot type:** traditional daily pivots from the previous day's OHLC, with R1 to R5 and S1 to S5. Checked against TradingView's "Pivot Points Standard" (Traditional, Auto, daily-based values) on 16 Oct 2026: every level matches.
  - P = (H + L + C) / 3, R1 = 2P − L, S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L).
  - R3 = 2P + H − 2L, S3 = 2P + L − 2H, R4 = 3P + H − 3L, S4 = 3P + L − 3H, R5 = 4P + H − 4L, S5 = 4P + L − 4H.
  - H, L and C are the previous session's values for the same futures contract, as the daily bar shows them, not the 3-minute candles:
    - H and L come from the daily bar, which includes the pre-open trade (so L can be below every 3-minute candle).
    - C is the exchange's closing price, not the last candle's close. Source, best first: the exchange's previous close from the live quote (only at the morning start); the volume-weighted average of the 1-minute closes from 15:00 to 15:40; the last 3-minute candle. The run banner shows which was used.
    - The live run saves each morning's values to `data/history/<symbol>.levels.json`; replay, sweep and reconcile read them. A replay of a day the bot did not run uses the VWAP close, which is within a few paise of the exchange's number on the day checked, but is not guaranteed exact.
- Pivots are for reference. Entries and exits come from the OR rules.

| Zone | Rule |
|---|---|
| R1 to P to S1 (neutral) | No aggressive trades. Only standard entries, in scalp and run mode, aiming at the nearest pivot. **Targets:** T2 is the nearest pivot still inside the zone (P, else R1 / S1), at least 10 points away. T1 is the nearest level (ORH / ORL or a pivot) strictly between the entry and T2 and at least 10 points away; otherwise halfway. R2 / S2 are not aims in this zone. Every standard setup uses this pair (ORB, trap confirmation, additional whipsaw, 2LL/3LL/2HH/3HH, fake break). |
| Above R1 | Bullish bias. Only bullish trades: ORH Breakout and Bear Trap. Blocked: Bull Traps, ORL Breakdown, Failed Bear Trap. **No shorts.** |
| R1 to R2 | Standard bullish trade. Aggressive, do not miss it. |
| Below S1 | Bearish bias. Only bearish trades: ORL Breakdown, Bull Trap, Failed Bear Trap. Blocked: all bullish trades. **No longs.** |
| S1 to S2 | Standard bearish trade. Aggressive, do not miss it. |

**R2 and S2**
- They are strong resistance and support. Price rarely breaks them in one attempt, and there is a lot of consolidation and rejection there.
- Touching R2 or S2 closes all positions in the matching direction. T2 also fires when the futures extreme comes within `targets.t2_proximity_points` (default 3) of that level, so a rejection a few points in front still books. The fill is at the band (or at R2/S2 / the open if price traded through).
- If R2 is broken, price tends to go through R3 to R4. If S2 is broken, it goes through S3 to S4.
- **R2 and S2 as OR levels:** a break of R2 is traded as an ORB long with R2 acting as ORH. A break of S2 is traded as an ORB short with S2 acting as ORL. The target is R4 or S4.
- **R4 and S4:** close all positions.
- **Do not trade** rejections or bounces from R2, R4, S2 and S4.

## 5. Stops and Targets (Futures Points)

### Stop loss (Nifty futures points)

| Setup | Stop |
|---|---|
| ORB long or short | A candle close back below ORH (above ORL), **or** a hard stop of 35 Nifty futures points. Whichever comes first. |
| Bull or bear trap | ORH (ORL) or 35 points, whichever is closer |
| Case 1 and Case 2 | The standard 35 points |
| Consolidation breakout | 35 points |
| R2 or S2 broken and traded as an OR level | A close back below R2 (above S2), or 35 points |

- The hard stop distance is the position-size reference. It is 35 points, or the distance to the level if that is closer.
- The close-below-ORH exit has no price. It triggers at candle close and acts as an extra early exit.
- **[Assumption]** There is a safety floor of 10 points on the stop distance.

### Targets

**T2 (final target)**

| Setup | T2 |
|---|---|
| Bull trap | ORL |
| Bear trap | ORH |
| Consolidation breakout | OR range projected from ORH or ORL, or the pivot target, whichever is nearer |
| ORB and the rest | R2 (long) or S2 (short). R4 or S4 for trades on a broken R2 or S2. |

T2 is capped by the mandatory exits in Section 4.

**T2 proximity [Assumption]:** T2 is reached when the bar's high (long) or low (short) comes within `targets.t2_proximity_points` of T2 (default 3, max 5, 0 = exact touch). R2/S2 often reject a few points in front of the print. The OPENED card still shows the chart level (S2); the trigger is that many points in front. The buffer shrinks so T2 keeps at least 10 points of room from the entry. Fill at the band; if price tagged T2 or gapped through it, fill at T2 or the open (unchanged). This does not start a new trade; a break of S2 is still a later `PIVOT_BREAKOUT` after close + retest + confirm.

**T1 [Assumption]:** the notes do not define T1. It is the nearest pivot level between entry and T2. If there is none, it is the halfway point to T2.

## 6. Option Layer (CE and PE)

### 6.1 Strike selection

- A long signal buys a CE. A short signal buys a PE.
- The strike is found from the **Nifty futures price**: round it to the nearest 50 to get the ATM strike, then add the one-strike-in-the-money strike.
  - For a CE, in the money means the lower strike. For a PE, it means the higher strike.
- Read the delta of both from the option chain, and pick the one with |delta| between 0.50 and 0.60, closest to 0.55.
- If neither is in that band, take the candidate whose |delta| is closest to 0.55 and flag it.
- Futures usually trade above spot, so the delta check is what confirms the strike. Delta is the only spot-derived input. It is read from the option chain and used for conversion only, not for any signal.
- Use the nearest weekly expiry. If it expires today, roll to the next one.
- Example: with Nifty futures at 22,600, the ATM call is the 22,600 CE.
- These options are chosen for balanced liquidity, high sensitivity to the index and lower decay than deep out-of-the-money contracts.

### 6.2 Delta conversion

Options do not move point for point with the index. The points below are Nifty futures points.

```text
option_premium_change = futures_points_moved × |delta|

option_SL = entry_LTP − (futures_stop_points × |delta|)
option_T1 = entry_LTP + (futures_T1_points × |delta|)
option_T2 = entry_LTP + (futures_T2_points × |delta|)
```

- The notes give a fixed 20-point option stop. The delta formula reproduces it: 35 points × about 0.55 gives about 19. If delta is missing, the fixed 20 points is the fallback.
- Delta and the three option prices are fixed at entry and recorded with the trade.

**Worked example (from the plan):**
- Nifty futures 22,600, a 22,600 CE at ₹150, delta 0.55.
- The futures stop is 40 points, T1 is 60 points and T2 is 120 points.
- Option stop = 150 − 40 × 0.55 = **₹128**.
- Option T1 = 150 + 60 × 0.55 = **₹183**, where 50% of the quantity is booked.
- Option T2 = 150 + 120 × 0.55 = **₹216**, where the remainder is closed.

### 6.3 Booking and sizing

- **At T1:** book 50% of the quantity and move the stop on the rest to the entry price. **[Assumption]**
- **At T2:** close the remainder.
- **With 1 lot:** there is no partial booking. T1 only moves the stop to cost.
- **Lots** = floor(risk amount ÷ (option stop points × lot size)).
  - **[Assumption]** Capital is ₹5,00,000 and risk is 1% per trade, which is ₹5,000.
  - The lot size comes from the broker's instruments file, not a hard-coded number.
- **[Assumption]** Maximum 5 open positions.

## 7. Exits

An open paper trade is closed by the first of these:

1. The hard stop on futures, or a candle close beyond the stop level (Section 5).
2. The option stop from Section 6.2.
3. T1 (partial) and T2.
4. The mandatory pivot exits: R2 or S2 touched, and R4 or S4 for trades on a broken R2 or S2.
5. End of day at 15:30 IST.

Exit checks run on the live futures price where available, and always at candle close.

## 8. Paper-Trading Output

On every 3-minute candle the bot prints:
- The candle's OHLC, ORH and ORL, the pivot zone and any open positions with mark-to-market.
- Signals fired or blocked, and the reason.
- Exits, with P&L.
- A "no signal" line when nothing happened.

For each trade, it records the entry, stops, targets, delta, option prices, quantity, risk in rupees, exit reason and P&L. P&L is shown two ways: futures points, and option ₹ after charges using the real option LTP.

## 9. Differences from the Pine Indicator

These are intentional, because the notes win:

- The Pine indicator's ATR-based stop multipliers, 3-bar sustainability check, 5-tick tolerance, 20-bar structure gate, 1.5× both-sides target and 61.8% entry are not used. The notes contain none of them.
- Pine's mandatory exits are R2/R3 and S2/S3. The notes use R2/S2 and R4/S4.
- Pine's trending-day pairings for the "Failed" traps are reversed from the notes.
- Pine's ORB entry (confirmation candle's low) is replaced by the previous candle's low.
- Pine's whipsaw reversals are replaced by continuation trades.
- Pine ignores some setups because its session checks are always true. The bot implements all of them.
- Pine hard-codes a lot of 50 and ₹2,500 risk in some table rows. The bot does not.

## 10. Open Items

1. T1 definition and the 50% booking with the stop moved to entry.
2. The ORB "previous candle low" fill rule.
3. The 10-point stop floor.
4. The Failed Bear Trap and Failed Bull Trap definitions.
5. The 50% reference leg for fake breakouts and breakdowns.
6. Pivot type: settled. Traditional, checked against TradingView on 16 Oct 2026, and the inputs (daily-bar high and low, exchange closing price) now match too (section 4).
7. Phase 3 detection details (all candle-close based):
   - Retest and confirmation must be on different candles (the order inside one candle is unknown).
   - A candle that breaks the swing but closes back inside the level counts as a failure.
   - A retest is a candle whose low comes within the tolerance of the level (or below it) while it does not close back inside. Failure (close back inside) has no tolerance.
   - The notes say "retests ORH and makes high to previous swing/candle". The swing rule and the 1-point retest tolerance are assumptions. The looser "previous candle high is enough" reading is not used.
   - Bull/bear trap pullback: the most recent green (red) candle after the sign; a gap through its low (high) fills at the open.
   - Case 1 and Case 2 aggressive entries use the opposite OR level as target (the notes give the target only for the trap confirmation entry).
   - R2/S2 breakouts have no trap or Case 1/2 follow-up (the notes say not to trade rejections from R2/S2).
8. Phase 4 detection details (all candle-close based, all config under `whipsaw`, `structure`, `fake_break`, `consolidation`):
   - **Additional whipsaw:** starts after an ORB confirmation (ORH/ORL only) or a trap's reverse-retest entry. A rejection is a candle whose low (high for shorts) comes within the retest tolerance of the level; consecutive touching candles count once. The ORB version is cancelled by a close beyond the level, the trap version by price trading through it. After 2 rejections, the entry is the break of the last rejection candle's high (low). One entry per parent. Aggressive Case 1/2 entries do not start whipsaw tracking.
   - **Consolidation breakout:** a rejection is a candle reaching ORH (ORL) within the tolerance and closing inside. At least 3 rejections, the oldest 45 to 60 minutes old (rolling window), then a close beyond the level: enter at that close. It shares its sign with the ORB sign on the same candle, so only one trade is taken. Target is the OR range projected from the level or the next pivot, whichever is nearer (Phase 5 resolves the pivot).
   - **2LL/3LL and 2HH/3HH:** a rise is tracked as the highest high so far, with a rise low that steps up each time a pullback holds. A pullback counts the candles that make a new low after the peak (the peak candle's low is the first reference). With 2 or 3 such lows, a break of the peak is the entry (a gap fills at the open). 4 or more lower lows, or a pullback below the rise low, breaks the structure. The rise must be at least 20 points. Stop is the pullback extreme (or 35 points), target R2 (S2 for shorts).
   - **Fake breakdown/breakout:** a consolidation is the last 10 candles within 45 points, with each edge touched at least twice. A fake break leaves the range and closes back inside within 3 candles. The reversal leg runs from the fake extreme to the highest high since, must reach 20 points, and the entry is its 50% retracement. Stop is the fake extreme (or 35 points), target the opposite end of the range. Cancelled if price trades through the fake extreme, closes outside the range again, reaches the range end without retracing, or 10 candles pass.
   - **Trending day:** informational label only. Uptrend = ORH breakout with a Bear Trap or Failed Bull Trap. Downtrend = ORL breakdown with a Bull Trap or Failed Bear Trap. A Failed Bear/Bull Trap is a new close beyond ORL/ORH after the trap.
   - **Both-sides trap:** not implemented. Enabling the flag raises an error.
9. Phase 5 decisions (stops, targets, zone filter, arbiter):
   - **Stop reference (confirmed by you):** `stop_points` = the distance to the structural level, capped at 35 and raised to the 10-point floor. It is used **only to size the lots**. The trade's stop is 35 points (for close-based setups also the candle-close exit, whichever comes first). Example: the 15 Oct ORB is sized on 16.2 points but stopped at 35, so the loss at the stop is about twice the Rs 5,000 budget. This is accepted. `sizing.risk_basis: hard_stop` would size on the real stop instead.
   - **Touch stops (traps, structure, fake breaks)** still use the level when it is closer than 35 points, as in section 5, so their stop and lot sizing use the same distance. Say so if you want 35 points for those too.
   - **T2:** from the setup's target reference (section 5 table), except in the neutral zone (below). A trade entered below R2 is capped at R2 (touching R2 closes everything). A trade on a broken R2, or entered beyond R2, targets R4. Entry beyond R4 (S4) is rejected. A target closer than 10 points is rejected. T2 also fires `t2_proximity_points` in front of the level (default 3).
   - **Neutral-zone targets (confirmed 26 Oct 2026):** every standard entry uses scalp-and-run. T2 is the nearest pivot still inside R1–S1 (P, else R1 for a long / S1 for a short), at least 10 points away. T1 is the nearest of ORH/ORL and any pivot strictly between the entry and that T2, also at least 10 points away; otherwise halfway. R2/S2 are not aims. A 3LL long at 22546.4 with ORH 22575 / P 22621 / R2 23037 becomes T1 22575 (ORH) and T2 22621 (P), not T1 P / T2 R2.
   - **Consolidation target:** the nearer of the OR-range projection and the nearest pivot at least 10 points away.
   - **T1:** outside the neutral zone, the nearest pivot strictly between entry and T2, else halfway. No minimum distance.
   - **Aggressive entries** (blocked in the neutral zone): Case 1/2 follow-ups and the consolidation breakout (the notes label the direct no-retest entry "aggressive"). The zone is read from the signal candle's close. Without previous-session pivots, the zone is neutral and pivot targets fail.
   - **One trade per sign.** A signal blocked by the zone filter does not use up its sign, so the standard trigger of the same sign can still be taken.
   - **Priority on the same candle** (assumption, `arbiter.priority`): ORB, R2/S2 breakout, trap confirmation, Case 1, Case 2, consolidation, additional whipsaw, fake break, 2LL/3LL/2HH/3HH. A lower-priority signal in the opposite direction on the same candle is dropped.
   - **Opposite open position** blocks a new trade (assumption, `arbiter.allow_opposite_positions`).
   - **Caps** (assumptions): 6 trades per day, 2 of them from 2LL/3LL/2HH/3HH, 5 open positions.
10. Phase 6 decisions (option layer):
   - **Chain data:** Groww reports a delta of 0 when it has no value. Zero, a wrong-signed delta (a negative CE or positive PE) and anything above 1 are treated as missing. A quote with no price is never selected.
   - **Strike:** ATM is the futures price rounded to the nearest 50, with halves rounding up. The ITM1 candidate is one step lower for a CE and one step higher for a PE. Of the candidates with |delta| in 0.50 to 0.60 (inclusive), the one closest to 0.55 wins, and ATM wins a tie. On the live check, a 23,049 futures price picked the 23,000 CE (delta 0.53) and the 23,050 PE (delta 0.53).
   - **Fallbacks** (flagged on the plan): no candidate in the band takes the closest delta (`delta_out_of_band`). No delta at all takes ATM (`delta_missing`), the option stop becomes the fixed 20 points, and T1/T2 use the 0.55 target delta (`target_delta_assumed`). The notes give no fallback for the targets, so that last part is an assumption.
   - **Entry price:** the chain LTP at signal time, with prices rounded to the 0.05 tick. Slippage and charges come in Phase 7.
   - **Lots:** `floor(budget / (option stop points x lot size))`, where the lot size comes from the instruments file. If one lot already risks more than the budget, the trade is rejected. A premium outlay above the capital is also rejected.
   - **Option stop and sizing:** the option stop is `entry - futures_stop x delta`, where the futures stop is the trade's real stop (35 points, or the level for touch stops). The lot count is sized on the level distance (`sizing.risk_basis: stop_plan`, the default you chose). `hard_stop` sizes on the real stop instead. Each plan records the loss it was sized for (`sized_risk_inr`) and the loss if stopped out (`risk_at_stop_inr`).
   - **Partial booking:** at T1 whole lots are booked (`t1_book_fraction`, at least 1, at most lots minus 1). With one lot nothing is booked and the stop only moves to cost.
   - **Expiry:** the nearest weekly expiry strictly after today, from the API (today it is Monday 19 Oct 2026, two sessions away, so theta is heavy).
11. Phase 7 decisions (paper engine, exits, charges, storage):
   - **What triggers an exit:** futures prices drive SL, T1 and T2. The option price supplies the fill. A worse CE/PE fill must not move those futures levels (extra rupee risk is accepted). The broker CE/PE stop-loss is a **crash net only**: the lower of the unshifted plan option SL and a 35-point conversion from the fill, never raised after a worse fill. If that net is missing, live option LTP at the same price is the fallback (`OPTION_STOP`). The option-price T1/T2 from section 6.2 are recorded but do not trigger exits.
   - **Option stop for close-based setups (ORB, R2/S2 breakout):** the exit is a candle close back inside the level or the 35-point stop, whichever comes first, so the option-price stop is `entry - 35 x delta`. The level distance is used only for the lot count.
   - **Order inside one candle** is unknown, so replay is conservative: the stop is checked before targets (a candle that reaches both is a loss), and after T1 the break-even stop applies from the next candle. A gap through a stop fills at the open (worse). A gap through a target fills at the open (better).
   - **Candle-close exit:** a close strictly beyond the level exits at that close. A close exactly on the level does not. Live polls never trigger it.
   - **Entry timing:** a position opens at the close of the signal candle and is first checked on the next candle. Anything the signal candle did after the entry price traded is not checked (an unavoidable blind spot at candle granularity).
   - **T1:** books `t1_book_lots` whole lots and moves the stop to the futures entry (option: the entry premium). With one lot only the stop moves. T2 closes what is left, including when the extreme comes within `t2_proximity_points` of T2. R2/S2 and R4/S4 are mandatory exits because T2 is capped at them.
   - **End of day:** open positions close at the close of the 15:27 candle (15:30), at the live LTP if given.
   - **Fill prices:** a live poll fills at the live option LTP. A replay, or a touch inside a closed candle, models the option price as `entry + sign x futures move x delta`. Slippage (`paper.slippage_option_points`, default 0) is paid on entry and on every exit.
   - **Charges** (`paper.charges`, all assumptions): brokerage the lower of Rs 20 and 0.1% per order, STT 0.15% on the sell side, exchange 0.03503%, SEBI 0.0001%, stamp 0.003% on the buy side, GST 18% on brokerage, exchange and SEBI. Check them against a Groww contract note.
   - **Storage:** SQLite in `DATA_DIR` (`paper_trades.sqlite`, ignored by git). The signal id is the primary key and the position id, so replaying a candle or restarting never records or opens the same trade twice. Open positions are restored from the database. A trade whose option plan fails is marked `released` and its sign is freed in the signal engine.
   - **P&L** is shown as futures points (lot-weighted, no charges) and option rupees (gross, charges, net).
12. Phase 8 decisions (live runtime):
   - **Data:** REST polling (the Groww streaming feed does not connect from this machine). Each 3-minute candle is fetched `session.candle_publish_delay_sec` after its boundary and retried every `runtime.fetch_retry_sec` until published.
   - **Catch-up:** whatever candles are not yet processed are processed in order. A late start, a restart or a stall are the same case: patterns are rebuilt from the day's candles, open positions are restored from the database, and missed candles are applied to them. A signal older than `runtime.max_signal_age_sec` (120 s) is shown as MISSED, recorded as rejected (stale), and never entered late at an old price.
   - **Live prices:** while a position is open the futures and option prices are polled every `runtime.poll_interval_sec` (15 s), so stops and targets are also checked between candle closes.
   - **Watchdog:** alert once if no new candle arrives `runtime.stall_alert_sec` (180 s) after it was due, and once after 3 failed fetches in a row. Data errors are retried. An error in the strategy code stops the run (positions stay saved; start again to continue).
   - **End of day:** the run finishes after the 15:27 candle, prints the day summary and exits. If the last candle never arrives, open positions are closed at the last price 5 minutes after the close. No candles by `runtime.no_session_exit_time` (09:45) means a holiday: exit cleanly.
   - **Monitoring:** `heartbeat.json` in `DATA_DIR` is rewritten every few seconds. `orb-bot status` reads it. A second run is refused while a fresh heartbeat (under 2 minutes) from another process exists.
   - **Alerts:** Telegram is optional (`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`). Sent: opened trades, exits, MISSED signals, stall and error alerts, the day summary (and every candle report if `runtime.notify_candle_reports` is true). A failed send is logged without the token and ignored.
   - **Commands:** `orb-bot run | replay DATE... [--quiet] | status | summary [DATE] | check [--send-test-alert]` (or `python -m orb_intraday_bot ...`). Reports go to the console and `DATA_DIR/logs/orb-bot-YYYY-MM-DD.log`.
13. Phase 9 findings and decisions (validation). Replay of the 54 sessions Groww has for the current contract (30 Jul to 15 Oct 2026), real futures candles, synthetic option prices:
   - **Baseline:** 212 trades (3.9 a day), win rate 52%, +373 futures points (+0.07 R a trade), profit factor 0.97 after charges. Excluding the single best day (13 Oct, +344 points) the total is about +29 points. The result is **not distinguishable from zero** and the two halves of the sample disagree (setup and zone results flip), so no tuning below is a proven improvement.
   - **By setup:** structure whipsaws carry the result (80 trades, +514 points), ORB breakout is negative (48 trades, -177 points, 46% wins), pivot breakout and Case 2 are slightly negative, traps, Case 1, fake breaks about flat or positive. Neutral-zone trades lose (-186 points), bullish-zone trades win (+405 on 23 trades), which fits the zone rules in section 4.
   - **Costs decide it:** with one option point of slippage per fill the profit factor falls to 0.79, with two to 0.65. The edge per trade (+1.8 futures points, about 1 option point) is smaller than realistic costs. The forward test must measure the real option spread at entry and exit.
   - **T1 cap (`targets.t1_max_distance_points`, off):** capping a far T1 at 50, 75, 100 or 150 points changes the result by -90, -13, -6 and 0 points. It does not help, so it stays off. A far T1 is harmless because T1 only books half and moves the stop to cost.
   - **Break-even stop after T1 (`targets.move_sl_to_entry_after_t1`, now wired):** it was a config switch that did nothing. Without it the replay gives +693 points (win rate 40%, longest losing streak 9 instead of 7). Left at true as the notes say; it is your call.
   - **Sizing on the real stop (`sizing.risk_basis: hard_stop`):** same points, but profit factor 1.10 against 0.97 and the maximum drawdown falls from Rs 124k to Rs 72k, because a stopped-out trade loses the budget instead of up to twice it. Your rule (level distance for lots) is unchanged.
   - **Structure whipsaw knobs:** `min_rise_points` 10/20/30/40 gives +336/+373/+302/+488 points, no pattern, so it stays at 20. Switching structure off loses 531 points. One or three trades a day instead of two both do worse.
   - **Data:** Groww serves 3-minute candles for the current contract only, and only back to the contract's listing (about three months). 26 of the 54 sessions have missing candles (thin trading, mostly July and August; 29 Sep has 95). Results on the 28 complete sessions: +421 points, profit factor 0.99, the same picture.
   - **Replay behaviour:** a session whose last candle is missing is closed at the last price (as the live runner does). Sanity checks (daily cap, duplicates, fills add up, stop at most 35 points, T1 before T2, no overlapping opposite trades) pass on all sessions.
   - **Tools:** `orb-bot replay [DATE...] [--from --to --last N] [--set key=value] [--csv FILE]`, `orb-bot sweep` (variants on the same cached candles), `orb-bot report` (forward-test statistics from the database), `orb-bot reconcile [DATE]` (a live day against a replay of the same candles). Candles are cached in `DATA_DIR/history`.
   - **Forward test:** `FORWARD_TEST.md` and `scripts/forward_test/` (a scheduled task that starts the session every weekday and reconciles after it).
