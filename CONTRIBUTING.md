# Contributing

Thanks for helping improve USA Car Search. This project depends on several third-party listing sites, so the most useful contributions are reproducible bug reports, source-specific fixes, and documentation that helps users configure searches safely.

## Good First Contributions

- Fix setup or documentation gaps.
- Add safer defaults or clearer error messages.
- Improve parsing for an existing source when the site markup changes.
- Add tests or fixtures around normalization, filtering, deduplication, and deal rating logic.
- Improve Docker or CI behavior.

## Reporting Bugs

Open an issue and include:

- Operating system and whether you are using Docker, WSL2, or native Linux.
- Python version.
- Command used, for example `python3 usa-car-search.py --notify`.
- Enabled sources from `.env`.
- Which source failed: CarGurus, Cars.com, Craigslist, AutoTrader, Facebook Marketplace, eBay Motors, or auto.dev.
- Sanitized logs or traceback.
- Whether Chrome CDP was configured for AutoTrader.

Do not include API keys, Telegram bot tokens, eBay credentials, Facebook cookies, session files, or private URLs with tokens.

## Pull Requests

1. Open an issue first for large changes or new listing sources.
2. Keep changes focused: one bug fix or feature per PR.
3. Update `README.md` and `.env.example` when behavior or configuration changes.
4. Run a syntax check before opening a PR:
   ```bash
   python3 -m py_compile usa-car-search.py _at_worker.py ebay-oauth-setup.py
   ```
5. If Docker behavior changes, verify the image still builds:
   ```bash
   docker build -t usa-car-search:test .
   ```

## Adding A New Source

A new source should include:

- Clear enable/disable env var.
- Documented setup steps.
- Filtering by year, mileage, price, distance, color, and trim where possible.
- Stable listing IDs or a dedupe fingerprint.
- Conservative rate limiting and error handling.
- No checked-in credentials, cookies, or session data.

## Security

If you find a credential leak or security-sensitive issue, do not open a public issue with secrets. Rotate the exposed secret first, then open a sanitized report or contact the maintainer privately.
