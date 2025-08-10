# AJ Intraday ORB Trading Strategy

## Overview
A sophisticated intraday trading strategy for Nifty/Bank Nifty futures using Opening Range Breakout (ORB) methodology with advanced pattern recognition and institutional-quality risk management.

## Key Features

### 🎯 Dynamic ORH/ORL Detection
- **Pattern-Based Formation**: Uses 3-candle pattern recognition instead of fixed time windows
- **Real-time Detection**: ORH/ORL can form anytime during trading session when pattern confirms
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
- **ORH Formation**: `high < high[1] and high[1] > high[2]`
- **ORL Formation**: `low > low[1] and low[1] < low[2]`
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
