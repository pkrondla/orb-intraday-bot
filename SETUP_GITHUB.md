# GitHub Repository Setup Guide

## Step 1: Install Git for Windows

1. **Download Git**: Go to https://git-scm.com/download/win
2. **Install**: Run the installer with default settings
3. **Verify Installation**: Open PowerShell and run `git --version`

## Step 2: Create GitHub Repository

### Option A: Using GitHub Website (Recommended)
1. **Go to GitHub**: Visit https://github.com
2. **Sign In**: Log in to your GitHub account (create one if needed)
3. **Create Repository**: 
   - Click the "+" icon in the top right
   - Select "New repository"
   - Repository name: `aj-intraday-orb-strategy`
   - Description: `AJ Intraday ORB Trading Strategy - Advanced pattern recognition for Nifty/Bank Nifty futures`
   - Set to **Public** (or Private if you prefer)
   - **DO NOT** initialize with README (we already have one)
   - Click "Create repository"

### Option B: Using GitHub CLI (After installing Git)
```bash
# Install GitHub CLI first: https://cli.github.com/
gh repo create aj-intraday-orb-strategy --public --description "AJ Intraday ORB Trading Strategy - Advanced pattern recognition for Nifty/Bank Nifty futures"
```

## Step 3: Initialize Local Repository

Open PowerShell in your project directory and run:

```bash
# Navigate to your project directory
cd "C:\Users\windows\OneDrive\Desktop\Trading\trade-automation"

# Initialize Git repository
git init

# Configure your Git identity (replace with your info)
git config user.name "Your Name"
git config user.email "your.email@example.com"

# Add all files to staging
git add .

# Create initial commit
git commit -m "Initial commit: AJ Intraday ORB Strategy with ATR-based risk management

- Complete Pine Script v6 implementation
- 16 trading patterns with psychological insights
- ATR-based dynamic position sizing
- Advanced whipsaw and fake breakout detection
- 10-column trade management table
- Comprehensive documentation (747 lines)
- Production-ready with error-free compilation"
```

## Step 4: Connect to GitHub

Replace `YOUR_USERNAME` with your actual GitHub username:

```bash
# Add GitHub remote
git remote add origin https://github.com/YOUR_USERNAME/aj-intraday-orb-strategy.git

# Push to GitHub
git push -u origin main
```

## Step 5: Verify Setup

1. **Check GitHub**: Visit your repository URL to see your files
2. **Clone Test**: Try cloning in a different directory to verify:
   ```bash
   git clone https://github.com/YOUR_USERNAME/aj-intraday-orb-strategy.git
   ```

## Common Issues & Solutions

### Issue: Git not recognized
**Solution**: Restart PowerShell after installing Git, or add Git to PATH manually.

### Issue: Authentication failed
**Solutions**:
1. **Personal Access Token**: Use GitHub Personal Access Token instead of password
2. **SSH Keys**: Set up SSH keys for authentication
3. **GitHub CLI**: Use `gh auth login` for easier authentication

### Issue: Repository already exists
**Solution**: If you created the repo with README, either:
1. Delete and recreate without README, or
2. Pull first: `git pull origin main --allow-unrelated-histories`

## Repository Structure

After setup, your repository will contain:

```
aj-intraday-orb-strategy/
├── README.md                           # Main documentation
├── .gitignore                         # Git ignore patterns
├── SETUP_GITHUB.md                    # This setup guide
├── AJ_Intraday_ORB_Strategy_ATR.pine # Main strategy (ATR-based)
├── AJ_Intraday_ORB_Strategy.pine     # Original strategy
├── intraday-setup.md                 # Complete implementation docs
└── aj-intraday-setup-notes.docx      # Setup notes
```

## Next Steps

1. **Branch Strategy**: Consider creating branches for development:
   ```bash
   git checkout -b development
   git checkout -b feature/new-patterns
   ```

2. **Regular Commits**: Commit changes regularly:
   ```bash
   git add .
   git commit -m "Add new pattern recognition feature"
   git push
   ```

3. **Collaboration**: If working with others, use Pull Requests for code review

4. **Releases**: Tag important versions:
   ```bash
   git tag -a v1.0 -m "Production release with all 16 patterns"
   git push origin v1.0
   ```

## Security Best Practices

- ✅ **DO** commit: Strategy code, documentation, setup guides
- ❌ **DON'T** commit: Personal trading data, API keys, account information
- ✅ **Use** .gitignore to exclude sensitive files
- ✅ **Review** commits before pushing to avoid sensitive data leaks

## Support

If you encounter issues:
1. Check Git documentation: https://git-scm.com/doc
2. GitHub Help: https://docs.github.com/
3. Review this setup guide for common solutions
