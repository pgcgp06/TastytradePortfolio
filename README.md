# Tastytrade Options Portfolio Manager

A local, single-file dashboard that pulls your Tastytrade positions and buying
power via the official API, then screens a watchlist for new option-trade
candidates using a mechanical, Tastytrade-style methodology.

> **Educational tool, not investment advice.** All analysis runs locally in your
> browser from the data you provide. Always confirm against live quotes before trading.

## What it does

- **Authenticates** to the Tastytrade API with OAuth2 (refresh token).
- **Pulls** your open positions and buying power for the selected account.
- **Groups** option legs into strategies (verticals, strangles, iron condors, …)
  so risk and buying power reflect the combined structure.
- **Flags** management actions (21 DTE, ITM short legs, profit targets, …).
- **Screens** your uploaded watchlist CSV and proposes the **best of 9 strategies**
  per candidate — with ~45 DTE, strikes at ~16Δ / ~30Δ, sizing and a management
  plan — ranked by potential and fit to your book. The **/VX** you enter tilts the
  ranking (low /VX → favour high individual IVR; high /VX → favour diversification).

The 9 strategies considered: Short Strangle, Short Put, Put Credit Spread,
Call Credit Spread, Diagonal, Broken Wing Butterfly, Jade Lizard, Put Ratio
Spread, Iron Condor.

## Requirements

- **Python 3** (standard library only — nothing to install).
- A Tastytrade **personal OAuth application** + grant (refresh token).
  See <https://developer.tastytrade.com/oauth/>.

## Setup & usage

```bash
py tastytrade_portfolio.py
```

1. **First run** asks for your OAuth *client secret* and *refresh token*. They are
   stored in `%LOCALAPPDATA%\TastytradePortfolio\oauth.json` (outside this repo) so
   later runs are non-interactive. The refresh token doesn't expire — revoke it on
   the site anytime.
2. Pick your account; the script fetches positions, buying power and 52-week ranges.
3. Enter the current **/VX** level (or set it later in the dashboard).
4. It writes and opens **`dashboard.html`** with your data embedded.
5. In the dashboard, click **Upload watchlist CSV** and drop your Tastytrade
   watchlist export to get the top trade candidates.

### Watchlist CSV

Any CSV with a `Symbol` column works. Recognised columns (case/spacing-insensitive):
`Mid/Price`, `IV Rank`, `IV Idx` (raw IV — needed for concrete strike estimates),
`Liquidity`, `Earnings At`, `Corr SPY`, `52w High`, `52w Low`.

## Files

| File | Purpose |
|---|---|
| `tastytrade_portfolio.py` | API fetcher + dashboard generator |
| `dashboard-template.html` | The dashboard UI + analysis engine (source) |
| `dashboard.html` | Generated output with your data — *git-ignored* |
| `portfolio.json` | Your fetched data — *git-ignored* |

## Privacy

Your credentials (`Token.txt`, `oauth.json`) and your real broker data
(`portfolio.json`, `dashboard.html`) are **git-ignored** and never leave your
machine. The dashboard makes no automatic network calls — it only reads the data
you paste or upload.
