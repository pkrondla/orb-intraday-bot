# Contributing to AJ Intraday ORB Strategy

## Overview
We welcome contributions to improve the AJ Intraday ORB trading strategy. This document outlines how to contribute effectively while maintaining the quality and integrity of the trading system.

## Types of Contributions

### 🔧 Code Improvements
- **Pattern Recognition**: New or enhanced trading patterns
- **Risk Management**: Improvements to ATR-based position sizing
- **Performance**: Optimization of Pine Script execution
- **Bug Fixes**: Corrections to existing functionality

### 📚 Documentation
- **Setup Guides**: Improvements to installation/configuration
- **Trading Guides**: Better explanations of patterns and psychology
- **Code Comments**: Enhanced inline documentation
- **Examples**: Real-world usage scenarios

### 🧪 Testing & Validation
- **Backtesting**: Historical performance analysis
- **Pattern Validation**: Confirmation of pattern detection accuracy
- **Edge Cases**: Testing unusual market conditions
- **Performance Testing**: Memory and execution optimization

## Development Guidelines

### Pine Script Standards
```pine
// Use Pine Script v6
//@version=6

// Clear function documentation
/// @function Calculate dynamic stop loss based on ATR and pattern type
/// @param entryPrice Entry price for the trade
/// @param signalType Type of signal (ORB_LONG, BULL_TRAP, etc.)
/// @param isLong True for long trades, false for short trades
/// @returns [stopPrice, atrDistance, atrValue, multiplier]
calculateDynamicStop(entryPrice, signalType, isLong) =>
    // Implementation...
```

### Code Quality Requirements
- **Error-Free Compilation**: All code must compile without errors
- **Memory Efficiency**: Respect Pine Script limitations (500 labels max)
- **Performance**: Optimize for real-time execution
- **Readability**: Clear variable names and logical structure

### Pattern Implementation Standards
- **Documentation**: Each pattern must be documented in `intraday-setup.md`
- **Visual Clarity**: Consistent label styling and colors
- **Psychological Basis**: Clear explanation of market psychology
- **Backtesting**: Historical validation of pattern effectiveness

## Contribution Process

### 1. Fork & Clone
```bash
# Fork the repository on GitHub
# Clone your fork
git clone https://github.com/YOUR_USERNAME/aj-intraday-orb-strategy.git
cd aj-intraday-orb-strategy
```

### 2. Create Feature Branch
```bash
# Create branch for your feature
git checkout -b feature/new-pattern-recognition
# or
git checkout -b fix/exit-detection-bug
# or
git checkout -b docs/improve-setup-guide
```

### 3. Development Standards

#### For New Patterns
1. **Research**: Study market behavior and participant psychology
2. **Implementation**: Code the pattern detection logic
3. **Testing**: Validate on historical data
4. **Documentation**: Update `intraday-setup.md` with:
   - Pattern description
   - Detection logic
   - Entry/exit rules
   - Psychology explanation
   - Risk management

#### For Bug Fixes
1. **Reproduce**: Confirm the bug exists
2. **Isolate**: Identify root cause
3. **Fix**: Implement minimal necessary changes
4. **Test**: Verify fix doesn't break other functionality
5. **Document**: Update comments and docs if needed

### 4. Testing Requirements

#### Pine Script Testing
```pine
// Test your changes on:
// 1. Live market data (current session)
// 2. Historical data (multiple days)
// 3. Different market conditions (trending, ranging, volatile)
// 4. Edge cases (gaps, halts, low volume)
```

#### Pattern Validation
- **Signal Accuracy**: Patterns trigger at correct times
- **False Positives**: Minimal incorrect signals
- **Performance**: No significant lag or memory issues
- **Visual Quality**: Labels display correctly without overlap

### 5. Documentation Updates
Always update relevant documentation:
- `README.md`: For new features or major changes
- `intraday-setup.md`: For pattern or setup changes
- Code comments: For complex logic
- `CHANGELOG.md`: Record of all changes (create if needed)

### 6. Commit Standards
```bash
# Use clear, descriptive commit messages
git commit -m "Add enhanced consolidation pattern detection

- Implement multi-touch validation system
- Add 3+ touch requirement for genuine consolidation
- Include 45-minute minimum duration filter
- Update documentation with pattern psychology"

# For bug fixes
git commit -m "Fix exit detection for EOD closure

- Correct end-of-day detection logic
- Handle timezone edge cases
- Update exit time tracking
- Resolve duplicate exit markers"
```

### 7. Pull Request Process
1. **Push Branch**: `git push origin feature/your-feature-name`
2. **Create PR**: Go to GitHub and create Pull Request
3. **Description**: Provide detailed description of changes
4. **Testing**: Include testing results and screenshots
5. **Review**: Respond to feedback and make requested changes

## Pull Request Template

```markdown
## Description
Brief description of changes and motivation.

## Type of Change
- [ ] New pattern implementation
- [ ] Bug fix
- [ ] Performance improvement
- [ ] Documentation update
- [ ] Code refactoring

## Testing
- [ ] Tested on live market data
- [ ] Tested on historical data
- [ ] No compilation errors
- [ ] No new performance issues
- [ ] Visual elements display correctly

## Pattern Details (if applicable)
- **Pattern Name**: 
- **Detection Logic**: 
- **Psychology**: 
- **Risk/Reward**: 

## Screenshots
Include before/after screenshots for visual changes.

## Checklist
- [ ] Code follows Pine Script v6 standards
- [ ] Documentation updated
- [ ] No sensitive data included
- [ ] Commit messages are clear
- [ ] All tests pass
```

## Code Review Criteria

### Functionality
- ✅ Pattern detection works accurately
- ✅ Risk management functions correctly
- ✅ No breaking changes to existing features
- ✅ Performance is acceptable

### Code Quality
- ✅ Clean, readable code structure
- ✅ Appropriate variable names
- ✅ Sufficient comments for complex logic
- ✅ Follows established coding patterns

### Documentation
- ✅ README.md updated if needed
- ✅ Pattern documented in setup guide
- ✅ Code comments explain complex logic
- ✅ Clear commit messages

## Getting Help

### Resources
- **Pine Script Documentation**: https://www.tradingview.com/pine-script-docs/
- **Strategy Documentation**: See `intraday-setup.md`
- **Setup Guide**: See `SETUP_GITHUB.md`

### Communication
- **Issues**: Use GitHub Issues for bug reports and feature requests
- **Discussions**: Use GitHub Discussions for questions and ideas
- **Code Review**: Comment on Pull Requests for code-specific questions

## Recognition

Contributors will be recognized in:
- `CONTRIBUTORS.md` file (will be created)
- Release notes for significant contributions
- GitHub contributors section

## License & Legal

- All contributions must be your original work
- Code must be suitable for educational/personal trading use
- No proprietary trading algorithms without permission
- Respect intellectual property rights

Thank you for contributing to the AJ Intraday ORB Strategy!
