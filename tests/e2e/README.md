# End-to-End Tests for Cloudflare Workers

These tests verify that the application works correctly when running in the actual Cloudflare Workers runtime with D1 database bindings.

## Prerequisites

1. **Wrangler dev must be running**: `wrangler dev`
   - Tests connect to `http://localhost:8787` by default
   - Can override with `WRANGLER_TEST_URL` environment variable

2. **D1 database must be configured** in `wrangler.toml`
   - Tests will use the actual D1 database binding

3. **Test dependencies**: `requests` (should already be installed)

## Running E2E Tests

```bash
# Start wrangler dev in one terminal
wrangler dev

# Run e2e tests in another terminal
pytest tests/e2e/ -v

# Run specific test file
pytest tests/e2e/test_auth_e2e.py -v

# Run with custom wrangler URL
WRANGLER_TEST_URL=http://localhost:8787 pytest tests/e2e/ -v
```

## Test Structure

- `conftest.py`: Shared fixtures and configuration
  - `check_wrangler_running`: Auto-skips tests if wrangler dev isn't running
  - `wrangler_client`: Requests session configured for wrangler dev
  - `make_url()`: Helper to construct full URLs

- `test_auth_e2e.py`: Auth-related end-to-end tests
  - Health checks
  - Login/logout flows
  - CSRF token handling
  - Protected endpoint access
  - Cookie management

## Differences from Unit Tests

| Aspect | Unit Tests (`tests/api/`) | E2E Tests (`tests/e2e/`) |
|--------|---------------------------|--------------------------|
| **Runtime** | FastAPI TestClient (in-process) | Actual Workers runtime |
| **Database** | SQLite (isolated per test) | D1 (shared, requires cleanup) |
| **Bindings** | Mocked | Real Cloudflare bindings |
| **Speed** | Fast | Slower (network calls) |
| **Isolation** | Complete | Requires manual cleanup |

## Adding New E2E Tests

1. Create test file in `tests/e2e/`
2. Use `wrangler_client` fixture for HTTP requests
3. Use `make_url()` helper for endpoint URLs
4. Tests will auto-skip if wrangler dev isn't running
5. **Important**: Clean up any test data you create (users, sessions, etc.)

## Example Test

```python
def test_my_feature(wrangler_client):
    """Test my feature against wrangler dev."""
    response = wrangler_client.get(make_url("/my-endpoint"))
    assert response.status_code == 200
```

## Notes

- E2E tests are slower and require wrangler dev to be running
- They test the actual Workers runtime, not just FastAPI logic
- Use for integration testing, not unit testing
- Consider using test-specific user IDs/emails to avoid conflicts

