# ORB Intraday Bot - Trading Strategy

## Python paper-trading bot (`orb_intraday_bot`)

Paper only: it never places an order (`LIVE_TRADING` must stay false, and the broker client is read-only). It trades Nifty futures signals and tracks the matching CE/PE option as a paper position. The rules are in `intraday-setup.md`; progress is in `PROGRESS.md`.

Setup: `pip install -e .[dev]`, copy `.env.example` to `.env` and fill in the data-only Groww credentials (and optionally Telegram).

```
orb-bot check                       # connection, contract, pivots, option chain, database
orb-bot replay --last 30            # past sessions on real futures candles (synthetic option prices)
orb-bot replay 2026-10-14 --events  # one day, with every signal and exit
orb-bot sweep --last 60            # compare config variants on the same candles
orb-bot report                      # forward-test statistics from the database
orb-bot reconcile                   # today's live run against a replay of the same candles
orb-bot run                         # live paper session; start before 09:15 IST, it exits after 15:30
orb-bot status                      # is a run alive
orb-bot summary [YYYY-MM-DD]        # trades and P&L from the database
```

`python -m orb_intraday_bot <command>` works too. Tests: `python -m pytest`.

The sections below describe the original Pine Script indicator.

## Overview
A sophisticated intraday trading strategy for Nifty/Bank Nifty futures using Opening Range Breakout (ORB) methodology with advanced pattern recognition and institutional-quality risk management.

## Key Features

### 🎯 Dynamic ORH/ORL Detection
- **Run-Based Formation**: ORH is the peak of the run of higher highs from the first candle of the day. ORL is the low of the run of lower lows. No fixed time window
- **Real-time Detection**: Each level is set once per day, at the close of the candle that ends its run
- **Precise Entry Points**: Identifies exact turning points rather than arbitrary time-based levels

### 📊 16 Trading Patterns
1. **Standard ORB**: Long/Short with 3-phase process
2. **Trap Patterns**: Bull/Bear traps with case differentiation (C1/C2)
3. **Consolidation Breakouts**: Direct entries after 45+ min consolidation
4. **Advanced Whipsaws**: Fake breakouts, both sides traps, momentum reversals

### 🧠 Psychological Intelligence
- **Market Structure Validation**: LL/HH rule compliance
- **Participant Psychology**: Different expectations for conservative vs aggressive cases
- **Golden Ratio Entries**: 61.8% retracement for optimal timing
- **Sustainability Checks**: 3-bar validation for genuine moves

### 💰 Revolutionary Risk Management
- **ATR-Based Position Sizing**: Pattern-specific multipliers (0.8x to 2.0x)
- **Dynamic Capital Management**: Configurable risk per trade (1% default)
- **Position Limits**: Maximum simultaneous positions with blocking logic
- **Real-time Exposure Tracking**: Monitor total capital at risk

### 📈 Advanced Trade Management
- **10-Column Trade Table**: Complete trade lifecycle tracking
- **Unified Exit System**: Target Hit, SL Hit, EOD closure with visual markers
- **Zone-Based Filtering**: Automatic signal filtering based on pivot zones
- **Mandatory Exits**: R2/R3 and S2/S3 exit alerts

## Files

### Pine Script Strategies
- **`AJ_Intraday_ORB_Strategy_ATR.pine`** - Main strategy with ATR-based risk management (LATEST)
- **`AJ_Intraday_ORB_Strategy.pine`** - Original strategy version

### Documentation
- **`intraday-setup.md`** - Complete implementation documentation (747 lines)
- **`aj-intraday-setup-notes.docx`** - Setup notes and guidelines

## Quick Start

1. **Setup Requirements**
   - TradingView account with Pine Script access
   - 3-minute chart timeframe (MANDATORY)
   - Nifty/Bank Nifty futures symbols

2. **Installation**
   - Copy the Pine Script code from `AJ_Intraday_ORB_Strategy_ATR.pine`
   - Create new indicator in TradingView Pine Editor
   - Paste code and save

3. **Configuration**
   - Set trading capital (₹500,000 default)
   - Configure risk per trade (1% recommended)
   - Adjust position limits (5 max simultaneous)
   - Select appropriate table position

## Strategy Parameters

### Trading Session
- **Market Hours**: 9:15 AM to 3:30 PM IST
- **Timeframe**: 3 minutes (strictly enforced)
- **Instruments**: Nifty/Bank Nifty futures

### Risk Management
- **Capital**: ₹500,000 (configurable)
- **Risk Per Trade**: 1% of capital
- **Max Positions**: 5 simultaneous
- **Stop Loss**: ATR-based with pattern-specific multipliers

### Pattern Recognition
- **ORH Formation**: run continues while `high >= high[1]` from the first candle (equal highs extend it); the first candle with `high < high[1]` ends it, and ORH = `high[1]`
- **ORL Formation**: run continues while `low <= low[1]` from the first candle (equal lows extend it); the first candle with `low > low[1]` ends it, and ORL = `low[1]`
- **Validation**: Multi-touch consolidation with 3+ touches
- **Structure**: LL/HH rule compliance required

## Technical Implementation

### Technology Stack
- **Pine Script v6**: Latest version with optimal performance
- **Array-Based Architecture**: Modern trade management system
- **Real-time Processing**: Works on live and historical data
- **Memory Optimization**: 500 label limit with efficient management

### Performance Features
- **Error-Free Compilation**: Comprehensive testing and validation
- **Scalable Design**: Handles multiple simultaneous patterns
- **Professional UI**: Clean chart design with detailed tooltips
- **Dark Mode Optimized**: High contrast for trading environments

## Zone-Based Trading Rules

### Market Zones
- **Bullish Zone (>R1)**: Only bullish trades allowed
- **Neutral Zone (R1-S1)**: Scalp and run mode
- **Bearish Zone (<S1)**: Only bearish trades allowed

### Pivot Levels
- **Calculation**: Standard pivot formula with R1-R4, S1-S4
- **Targeting**: Sequential progression (ORH→P→R1→R2)
- **Mandatory Exits**: Automatic alerts at major levels

## Advanced Features

### Whipsaw Detection
- **2LL/3LL Patterns**: Lower low formations for reversal
- **2HH/3HH Patterns**: Higher high formations for reversal
- **Fake Breakouts**: Consolidation-based reversal signals
- **Both Sides Trap**: 4-phase maximum trapping detection

### Trade Analytics
- **Complete Lifecycle**: Entry time, exit time, P&L tracking
- **Real-time Updates**: Status changes and profit/loss monitoring
- **Historical Analysis**: Full trade history with timestamps
- **Performance Metrics**: Success rates across pattern types

## Current Status

### ✅ Production Ready (December 2024)
- **16 Trading Patterns**: All implemented and operational
- **ATR Risk Management**: Revolutionary position sizing system
- **Exit Detection**: Unified system with visual markers
- **Trade Table**: 10-column comprehensive tracking
- **Error-Free**: Complete Pine Script v6 compliance

### 🚀 Ready for Live Trading
This system provides institutional-quality pattern recognition with psychological insights, advanced risk management, and professional-grade user experience for Indian markets.

## Support & Documentation

For detailed setup instructions, pattern explanations, and trading guidelines, refer to `intraday-setup.md` which contains comprehensive documentation with 747 lines covering all aspects of the strategy.

## License

This project is for educational and personal trading purposes. Please ensure compliance with your local trading regulations.

---

**Disclaimer**: Trading involves risk. Past performance does not guarantee future results. Always trade responsibly and within your risk tolerance.
