# Migration Guide: SSE Notifications to HTTP Polling

## Overview

This document provides step-by-step instructions for migrating the notification system from Server-Sent Events (SSE) to HTTP polling. This migration eliminates session dependencies that cause exceptions in authentication flows and improves compatibility with Cloudflare Workers Python timeout limits.

## Current Architecture

### SSE Implementation
- **Endpoint**: `GET /api/stream` (handled by `api_stream` in `src/workers/api/web.py`)
- **Frontend**: Uses `EventSource` API in `src/workers/templates/base.html` (lines 150-223)
- **Backend**: `src/workers/api/notifications_stream.py` manages long-running SSE connections
- **Session Dependency**: Uses `last_notification_id` stored in session for cursor tracking
- **Issues**: 
  - Sessions cause exceptions in auth flows
  - Long-running connections hit Workers timeout limits (30s default, 300s max)
  - Requires session middleware to be active

### Target Architecture

- **Endpoint**: `GET /api/notifications?after_id=<cursor>&limit=50` (already exists)
- **Frontend**: HTTP polling with `setInterval` and `fetch()`
- **Cursor Storage**: `localStorage.getItem('last_notification_id')` (client-side)
- **Benefits**:
  - No session dependency
  - Stateless, short-lived requests
  - Works within Workers timeout limits
  - Simpler error handling

## Analysis of Current Code

### Files Involved

1. **Frontend**:
   - `src/workers/templates/base.html` (lines 148-223): SSE EventSource implementation
   - `src/workers/templates/activity/index.html`: Listens to `notification:created` events

2. **Backend**:
   - `src/workers/api/web.py`:
     - Line 64: Import of `notifications_stream_response`
     - Lines 968-972: `/api/stream` route handler
     - Lines 931-935: `/api/notifications` endpoint (already supports `after_id`)
   - `src/workers/api/notifications_stream.py`: Complete SSE implementation
   - `src/workers/api/app_factory.py`:
     - Line 40: Import of `cancel_all_sse_connections`
     - Lines 101-103: SSE cleanup in shutdown handler

3. **Tests**:
   - `tests/api/test_sessions.py` (lines 195-244): `test_notifications_stream_reuses_session_cursor`

4. **Documentation**:
   - `docs/feature-plan-next-horizon.md` (lines 38-41): References SSE notifications

### Key Dependencies

- **Pipeline Streams**: `src/workers/api/pipeline_stream.py` is **independent** - it does NOT use sessions and handles job-specific pipeline events separately. Do NOT modify this.
- **Toast System**: The toast notification system (`src/workers/templates/components/overlays/toast.html`) listens to `window` custom events (`toast`, `notification:created`) - these events must continue to be dispatched.
- **Activity Page**: `src/workers/templates/activity/index.html` listens to `notification:created` events to dynamically add notifications to the feed.

### Current Behavior to Preserve

1. **Deduplication**: Uses `window.__notifiedIds` Set with hash-based keys
2. **Toast History**: Persists to `sessionStorage` (TOAST_STORAGE_KEY = '__toast_history_v1')
3. **Custom Events Dispatched**:
   - `toast` - for toast overlay component
   - `notification:created` - for activity feed and other listeners
   - `document:activity` - when notification has `document_id` context
   - `job:activity` - when notification has `job_id` context
4. **HTMX Triggers**:
   - `refreshDocument` on document detail elements
   - `refreshJob` on job detail elements
   - `refreshDocuments` on documents page
   - `refreshJobs` on jobs page
5. **Error Handling**: Exponential backoff retry (1s → 2s → 4s → max 30s)

## Migration Steps

### Step 1: Frontend - Replace SSE with Polling

**File**: `src/workers/templates/base.html`

**Location**: Lines 148-223 (the entire SSE EventSource block)

**Action**: Replace the SSE implementation with HTTP polling.

**Implementation Details**:

```javascript
// Replace lines 148-223 with polling implementation
const NOTIFICATION_POLL_INTERVAL = 5000; // 5 seconds
const NOTIFICATION_CURSOR_KEY = 'last_notification_id';
let pollTimer = null;
let retryDelay = 1000;
const maxDelay = 30000;

function pollNotifications() {
  const lastId = localStorage.getItem(NOTIFICATION_CURSOR_KEY);
  const url = lastId 
    ? `/api/notifications?after_id=${encodeURIComponent(lastId)}&limit=50`
    : `/api/notifications?limit=50`;
  
  fetch(url, {
    credentials: 'include', // Include cookies for auth
    headers: {
      'Accept': 'application/json',
    }
  })
  .then(response => {
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  })
  .then(data => {
    const notifications = data.notifications || [];
    if (notifications.length > 0) {
      // Process notifications in reverse order (oldest first)
      // since API returns newest first
      notifications.reverse().forEach(n => {
        processNotification(n);
      });
      // Update cursor to most recent notification ID
      const latestId = notifications[notifications.length - 1].id;
      if (latestId) {
        localStorage.setItem(NOTIFICATION_CURSOR_KEY, latestId);
      }
    }
    // Reset retry delay on success
    retryDelay = 1000;
    // Schedule next poll
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollNotifications, NOTIFICATION_POLL_INTERVAL);
  })
  .catch(error => {
    console.error('Notification poll error:', error);
    // Exponential backoff on error
    const delay = Math.min(maxDelay, retryDelay);
    retryDelay = Math.min(maxDelay, retryDelay * 2);
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(pollNotifications, delay);
    // Show error toast
    window.dispatchEvent(new CustomEvent('toast', { 
      detail: { type: 'error', text: 'Live updates disconnected, retrying...' } 
    }));
  });
}

function processNotification(n) {
  // Reuse existing notification processing logic from SSE handler
  const key = hashKey(`${n.id}:${n.level}:${n.text || ''}`);
  if (window.__notifiedIds.has(key)) return;
  rememberToastKey(key);
  
  const ctx = (typeof n.context === 'object' && n.context !== null) ? n.context : {};
  n.context = ctx;
  const type = n.level === 'error' ? 'error' : (n.level === 'success' ? 'success' : 'info');
  let href = ctx.href || null;
  if (!href) {
    if (ctx.document_id) {
      href = `/dashboard/documents/${ctx.document_id}`;
    } else if (ctx.job_id) {
      href = `/dashboard/jobs/${ctx.job_id}`;
    }
  }
  
  // Dispatch all the same events as before
  window.dispatchEvent(new CustomEvent('toast', { detail: { type, text: n.text, href } }));
  window.dispatchEvent(new CustomEvent('notification:created', { detail: n }));
  if (ctx.document_id) {
    window.dispatchEvent(new CustomEvent('document:activity', { detail: n }));
  }
  if (ctx.job_id) {
    window.dispatchEvent(new CustomEvent('job:activity', { detail: n }));
  }
  
  // HTMX triggers (keep existing logic)
  const docEl = ctx.document_id ? document.querySelector(`[data-document-detail][data-document-id="${ctx.document_id}"]`) : null;
  if (docEl && window.htmx) {
    htmx.trigger(docEl, 'refreshDocument');
  }
  const jobEl = ctx.job_id ? document.querySelector(`[data-job-detail][data-job-id="${ctx.job_id}"]`) : null;
  if (jobEl && window.htmx) {
    htmx.trigger(jobEl, 'refreshJob');
  }
  const docPage = document.querySelector('[data-documents-page]');
  if (docPage && window.htmx) {
    htmx.trigger(docPage, 'refreshDocuments');
  }
  const jobsPage = document.querySelector('[data-jobs-page]');
  if (jobsPage && window.htmx) {
    htmx.trigger(jobsPage, 'refreshJobs');
  }
}

// Start polling when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  // Initialize notification polling
  pollNotifications();
  
  // Pause polling when tab is hidden, resume when visible
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    } else {
      if (!pollTimer) {
        pollNotifications();
      }
    }
  });
  
  // Clean up on unload
  window.addEventListener('beforeunload', () => {
    if (pollTimer) clearTimeout(pollTimer);
  });
});
```

**Key Changes**:
- Remove `EventSource` and `openES()` function
- Remove `window.__es` references
- Replace with `fetch()`-based polling
- Store cursor in `localStorage` instead of session
- Keep all existing event dispatching logic
- Add visibility change handler to pause/resume polling

### Step 2: Backend - Remove SSE Endpoint

**File**: `src/workers/api/web.py`

**Changes**:

1. **Remove import** (line 64):
   ```python
   # Remove this line:
   from .notifications_stream import notifications_stream_response
   ```

2. **Remove route handler** (lines 968-972):
   ```python
   # Remove or comment out this entire route:
   @router.get("/api/stream")
   async def api_stream(request: Request, user: dict = Depends(get_current_user)):
       db = ensure_db()
       session = getattr(request.state, "session", None)
       return notifications_stream_response(request, db, user, session=session)
   ```

3. **Keep `/api/notifications` endpoint** (lines 931-935) - it already works correctly:
   ```python
   @router.get("/api/notifications")
   async def api_list_notifications(request: Request, user: dict = Depends(get_current_user), after_id: Optional[str] = None, limit: int = 50):
       db = ensure_db()
       notifs = await list_notifications(db, user["user_id"], after_id=after_id, limit=min(max(limit, 1), 100))
       return JSONResponse({"notifications": notifs}, headers={"Cache-Control": "no-store"})
   ```

### Step 3: Backend - Remove SSE Module

**File**: `src/workers/api/notifications_stream.py`

**Action**: Delete the entire file.

**Verification**: Ensure `pipeline_stream.py` has no dependencies on this file (it doesn't - they're independent).

**Note**: The `cancel_all_sse_connections()` function is only used for notification streams, not pipeline streams. Pipeline streams have their own `cancel_all_pipeline_streams()` function.

### Step 4: Backend - Update App Factory

**File**: `src/workers/api/app_factory.py`

**Changes**:

1. **Remove import** (line 40):
   ```python
   # Remove this line:
   from .notifications_stream import cancel_all_sse_connections
   ```

2. **Remove SSE cleanup** (lines 101-103):
   ```python
   # Remove this block from shutdown_cleanup():
   try:
       sse_count = await cancel_all_sse_connections()
       if sse_count > 0:
           app_logger.info("Cancelled %s SSE connections", sse_count)
   except Exception as exc:  # pragma: no cover - defensive logging
       app_logger.debug("Failed to cancel SSE connections: %s", exc)
   ```

### Step 5: Tests - Update or Remove SSE Tests

**File**: `tests/api/test_sessions.py`

**Action**: Remove or update the test `test_notifications_stream_reuses_session_cursor` (lines 195-244).

**Options**:

1. **Remove entirely** if no longer relevant
2. **Replace with polling test** that verifies:
   - `/api/notifications` returns correct data with `after_id` parameter
   - Cursor-based pagination works correctly
   - Notifications are returned in correct order

**Example replacement test** (if desired):

```python
async def test_notifications_polling_with_cursor(db):
    """Test that /api/notifications supports cursor-based pagination."""
    user_id = "user-1"
    
    # Create test notifications
    await create_notification(db, "notif-1", user_id, "info", "First")
    await create_notification(db, "notif-2", user_id, "info", "Second")
    await create_notification(db, "notif-3", user_id, "info", "Third")
    
    # Get first batch
    notifs1 = await list_notifications(db, user_id, after_id=None, limit=2)
    assert len(notifs1) == 2
    assert notifs1[0]["id"] == "notif-3"  # Newest first
    
    # Get next batch using cursor
    last_id = notifs1[-1]["id"]
    notifs2 = await list_notifications(db, user_id, after_id=last_id, limit=2)
    assert len(notifs2) == 1
    assert notifs2[0]["id"] == "notif-1"
```

### Step 6: Documentation Updates

**File**: `docs/feature-plan-next-horizon.md`

**Location**: Lines 38-41

**Action**: Update references to reflect polling approach:

```markdown
- **Live notifications & activity stream**:
  - Migrated from SSE to HTTP polling for better Workers compatibility
  - Client-side cursor management via localStorage
  - Polls `/api/notifications?after_id=<cursor>` every 5 seconds
  - No session dependency required
```

## Verification Checklist

After implementing the migration, verify:

- [ ] Frontend polls `/api/notifications` correctly
- [ ] Cursor is stored and retrieved from `localStorage`
- [ ] Notifications appear as toasts without duplicates
- [ ] `notification:created` events are dispatched correctly
- [ ] Activity page receives and displays new notifications
- [ ] HTMX triggers fire for document/job/page refreshes
- [ ] Error handling and retry logic works
- [ ] Polling pauses when tab is hidden
- [ ] No session dependencies remain in notification flow
- [ ] `/api/notifications` endpoint works with `after_id` parameter
- [ ] Pipeline streams (`/api/pipelines/stream`) still work independently
- [ ] No console errors in browser
- [ ] No server errors in logs

## Testing Strategy

### Manual Testing

1. **Basic Polling**:
   - Open browser console
   - Verify polling requests appear every 5 seconds
   - Check `localStorage` contains `last_notification_id`

2. **Notification Flow**:
   - Trigger a notification (e.g., complete a job)
   - Verify toast appears
   - Verify notification appears in activity feed
   - Verify cursor updates in `localStorage`

3. **Error Handling**:
   - Disconnect network
   - Verify error toast appears
   - Verify exponential backoff retry
   - Reconnect network
   - Verify polling resumes

4. **Tab Visibility**:
   - Open tab with app
   - Switch to another tab (tab becomes hidden)
   - Verify polling stops (check Network tab)
   - Switch back to app tab
   - Verify polling resumes

5. **Multiple Tabs**:
   - Open app in two tabs
   - Trigger notification
   - Verify both tabs receive notification
   - Verify each tab maintains its own cursor

### Automated Testing

- Update existing tests to use polling instead of SSE
- Add tests for cursor management
- Add tests for error handling and retry logic
- Test pagination with `after_id` parameter

## Performance Considerations

### Polling Interval

- **Current**: 5 seconds (configurable via `NOTIFICATION_POLL_INTERVAL`)
- **Trade-offs**:
  - Shorter interval = more timely updates but higher server load
  - Longer interval = lower server load but delayed updates
- **Recommendation**: Start with 5-10 seconds, adjust based on usage patterns

### Optimization Opportunities

1. **Adaptive Polling**: Increase interval when no notifications received
2. **Batch Processing**: Process multiple notifications in single poll
3. **Request Deduplication**: Prevent multiple simultaneous polls
4. **Background Sync**: Use Background Sync API when tab is hidden (future enhancement)

## Rollback Plan

If issues arise, rollback steps:

1. Revert frontend changes in `base.html` (restore SSE EventSource)
2. Restore `/api/stream` route in `web.py`
3. Restore `notifications_stream.py` module
4. Restore SSE cleanup in `app_factory.py`
5. Restore tests in `test_sessions.py`

**Note**: Sessions must be re-enabled for rollback to work fully.

## Migration Timeline

1. **Phase 1**: Implement frontend polling (can run alongside SSE temporarily)
2. **Phase 2**: Remove SSE endpoint and backend code
3. **Phase 3**: Update tests and documentation
4. **Phase 4**: Monitor and optimize polling interval

## Related Issues

- Sessions causing exceptions in auth flows
- Cloudflare Workers timeout limits (30s default, 300s max)
- Session middleware disabled in `app_factory.py` (line 267)

## References

- Existing `/api/notifications` endpoint: `src/workers/api/web.py:931-935`
- `list_notifications` function: `src/workers/api/database.py:1832-1855`
- Toast component: `src/workers/templates/components/overlays/toast.html`
- Activity page: `src/workers/templates/activity/index.html`

