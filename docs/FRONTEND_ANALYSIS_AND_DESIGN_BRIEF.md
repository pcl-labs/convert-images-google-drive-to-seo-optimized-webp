# Frontend Analysis & Design Brief: Codex-like Blog Editor

## Executive Summary

This document provides a comprehensive analysis of the current frontend architecture, identifies gaps between the UI and the new structured API, and outlines requirements for building a Codex/Cursor/Windsurf-like blog editing interface. The analysis reveals that while the backend API is production-ready with section-based editing, version control, and conflict resolution, the frontend is still using legacy document-centric patterns and lacks the granular editing capabilities the API supports.

---

## Table of Contents

1. [Current Frontend Architecture](#current-frontend-architecture)
2. [New API Capabilities](#new-api-capabilities)
3. [UI Gap Analysis](#ui-gap-analysis)
4. [Legacy Code to Remove](#legacy-code-to-remove)
5. [Design Requirements](#design-requirements)
6. [Technical Implementation Guide](#technical-implementation-guide)
7. [API Reference for Frontend](#api-reference-for-frontend)

---

## Current Frontend Architecture

### Technology Stack

- **Templating**: Jinja2 templates (server-side rendering)
- **Styling**: Tailwind CSS
- **JavaScript**: Vanilla JS (no framework)
- **HTTP Client**: Fetch API + HTMX (for partial updates)
- **Routing**: FastAPI server-side routes (`/dashboard/*`)
- **Authentication**: JWT cookies + CSRF tokens

### Current Page Structure

```
/dashboard/documents                    → Document list page
/dashboard/documents/{document_id}      → Document detail page (main blog editor)
/dashboard/documents/{document_id}/versions/{version_id} → Version viewer
/dashboard/jobs                         → Job queue page
/dashboard/integrations                 → OAuth integrations
/dashboard/settings                     → User settings
```

### Current Blog Editing Flow

1. **Document Detail Page** (`/dashboard/documents/{document_id}`)
   - Shows document metadata (Drive sync status, source)
   - Blog generation form (content type, instructions, tone)
   - Version viewer showing latest version
   - Sections list (read-only, shows summary only)
   - Pipeline timeline (job status)

2. **Version Viewer Component** (`version_viewer.html`)
   - Tabs: Outline / MDX / HTML
   - Sections displayed as cards with:
     - Section title
     - Summary text (truncated)
     - "Regenerate" button (legacy route)
   - No inline editing
   - No version history UI
   - No diff viewing

### Current JavaScript Patterns

**Location**: Inline `<script>` tags in templates

**Key Functions**:
- `handleDocumentGenerate()` - Calls `/v1/content/blog_from_document` (old API)
- `switchVersionTab()` - Tab switching for outline/mdx/html
- `renderDocFlash()` - Flash message display
- `buildDocumentPayload()` - Form serialization

**HTTP Patterns**:
- Uses `fetch()` for blog generation
- Uses HTMX for form submissions (section regenerate)
- No project-based API calls
- No section patching
- No version management

### Current Data Flow

```
User Action → Form Submit → Server Route → Database → Template Render
```

**Example: Section Regenerate**
```
User clicks "Regenerate" 
→ HTMX POST /dashboard/documents/{id}/sections/{index}/regenerate
→ Server calls start_generate_blog_job() (queues full blog regeneration)
→ Returns flash message
→ Page doesn't update (no section-level updates)
```

**Problems**:
- Full blog regeneration for single section edits
- No optimistic updates
- No conflict handling
- No real-time feedback
- Document-centric (not project-centric)

---

## New API Capabilities

### Project-Based Structure

All new endpoints are under `/api/v1/projects/{project_id}/blog/`:

**Key Concept**: Projects wrap documents. A project has:
- `project_id` (primary identifier)
- `document_id` (links to underlying document)
- `status` (pending, transcript_ready, embedded, blog_generated, failed)
- YouTube URL (if applicable)

### Available Endpoints

#### 1. Section Management

**GET `/api/v1/projects/{project_id}/blog/sections`**
- Returns list of sections with metadata
- Response: `ProjectSectionListResponse`
  ```json
  {
    "project_id": "proj-123",
    "document_id": "doc-456",
    "version_id": "v789",
    "sections": [
      {
        "section_id": "sec-0",
        "index": 0,
        "title": "Introduction",
        "word_count": 150
      }
    ]
  }
  ```

**POST `/api/v1/projects/{project_id}/blog/sections/patch`**
- AI-powered section editing
- Request: `PatchSectionRequest`
  ```json
  {
    "section_id": "sec-0",
    "instructions": "Make this more concise and add a call-to-action"
  }
  ```
- Response: `PatchSectionResponse` with updated section
- **Features**:
  - Uses transcript chunks for context (vector search)
  - Creates new version automatically
  - Optimistic concurrency control (409 on conflicts)
  - Returns updated `body_mdx` for the section

#### 2. Version Management

**GET `/api/v1/projects/{project_id}/blog/versions`**
- List all versions with metadata
- Response: `ProjectVersionsResponse`
  ```json
  {
    "project_id": "proj-123",
    "document_id": "doc-456",
    "versions": [
      {
        "version_id": "v789",
        "version": 1,
        "created_at": "2025-01-15T10:00:00Z",
        "source": "patch_section",
        "title": "My Blog Post"
      }
    ]
  }
  ```

**GET `/api/v1/projects/{project_id}/blog/versions/{version_id}`**
- Get full version details
- Response: `ProjectVersionDetail`
  - Includes `frontmatter`, `body_mdx`, `outline`, `sections` (full data)

**GET `/api/v1/projects/{project_id}/blog/diff?from_version_id={v1}&to_version_id={v2}`**
- Compare two versions
- Response: `ProjectBlogDiff`
  ```json
  {
    "project_id": "proj-123",
    "document_id": "doc-456",
    "from_version_id": "v1",
    "to_version_id": "v2",
    "changed_sections": ["sec-0", "sec-2"],
    "diff_body_mdx": "--- v1\n+++ v2\n..."
  }
  ```

**POST `/api/v1/projects/{project_id}/blog/versions/{version_id}/revert`**
- Revert to a previous version
- Creates new version (doesn't delete history)
- Response: `ProjectVersionDetail` (new version)

**GET `/api/v1/projects/{project_id}/blog/export`**
- Export latest MDX
- Simple JSON response with `body_mdx`

#### 3. Blog Generation (Existing, Enhanced)

**POST `/api/v1/projects/{project_id}/blog/generate`**
- Generate blog from transcript
- Returns job_id or inline blog (depending on settings)

**GET `/api/v1/projects/{project_id}/blog`**
- Get latest blog version
- Response: `ProjectBlog` (summary view)

### Key API Features

1. **Stable Section IDs**: Each section has a `section_id` (e.g., `"sec-0"`) that persists across versions
2. **Optimistic Updates**: Uses `update_document_latest_version_if_match()` for conflict detection
3. **Version History**: Full audit trail with source tracking (`patch_section`, `revert`, etc.)
4. **Context-Aware Editing**: Section patching uses vector search to find relevant transcript chunks
5. **Conflict Resolution**: Returns 409 with clear message when concurrent edits detected

---

## UI Gap Analysis

### What's Missing

#### 1. **Project-Centric Navigation**
- **Current**: All routes use `document_id`
- **Needed**: Routes should use `project_id`
- **Impact**: Can't access new API endpoints without project context

#### 2. **Section Editing UI**
- **Current**: Read-only sections with "Regenerate" button
- **Needed**: 
  - Inline section editor
  - Instruction input field
  - Loading states during AI processing
  - Error handling (409 conflicts, network errors)
  - Success feedback with updated content

#### 3. **Version History UI**
- **Current**: No version history display
- **Needed**:
  - Version list sidebar/timeline
  - Version metadata (source, timestamp, title)
  - Click to view any version
  - Diff viewer (unified diff + changed sections highlight)
  - Revert button per version

#### 4. **Section Display**
- **Current**: Shows only `summary` field
- **Needed**: 
  - Show full `body_mdx` content
  - Display word counts
  - Show section index/title clearly
  - Collapsible sections for long content

#### 5. **Conflict Handling**
- **Current**: No conflict detection
- **Needed**:
  - Detect 409 responses
  - Show conflict warning modal
  - Offer reload + retry flow
  - Visual indicators for stale data

#### 6. **Real-Time Updates**
- **Current**: Page refresh required to see changes
- **Needed**:
  - Optimistic UI updates
  - Poll for version updates after edits
  - Auto-refresh section list after patching

#### 7. **Export Functionality**
- **Current**: Download via `/dashboard/documents/{id}/versions/{vid}/download`
- **Needed**: Use new `/api/v1/projects/{id}/blog/export` endpoint

### Current vs. Desired User Flow

**Current Flow**:
```
1. User views document detail page
2. Sees sections as read-only cards
3. Clicks "Regenerate" → Full blog regeneration job queued
4. Waits for job completion
5. Refreshes page to see changes
```

**Desired Flow (Codex-like)**:
```
1. User views project blog editor
2. Sees sections as editable blocks (like code blocks)
3. Clicks "Edit" on a section → Inline editor opens
4. Types instructions → AI processes section only
5. Sees updated section immediately (optimistic update)
6. Can view version history, diffs, revert
7. All changes tracked with version history
```

---

## Legacy Code to Remove

### 1. Legacy Section Regenerate Route

**File**: `src/workers/api/web.py`
**Route**: `POST /dashboard/documents/{document_id}/sections/{section_index}/regenerate`
**Lines**: 1390-1446

**Why Remove**:
- Uses `section_index` (unreliable, can change)
- Triggers full blog regeneration (inefficient)
- No version tracking
- Document-centric (should be project-centric)

**Replacement**: Use `POST /api/v1/projects/{project_id}/blog/sections/patch`

### 2. Legacy Blog Generation Route

**File**: `src/workers/api/web.py`
**Route**: `POST /dashboard/documents/{document_id}/generate`
**Lines**: 1329-1387

**Status**: Keep but enhance
- Still needed for initial blog generation
- Should call project API internally
- Consider redirecting to project-based route

### 3. Legacy Version Viewer Template Logic

**File**: `src/workers/templates/documents/partials/version_viewer.html`
**Lines**: 66-92 (sections display)

**Issues**:
- Uses `section.order` instead of `section_id`
- Form action uses old route
- No section editing UI
- Shows only summary, not full content

**Action**: Refactor to use project API and add editing UI

### 4. Legacy JavaScript Functions

**File**: `src/workers/templates/documents/partials/detail_content.html`
**Lines**: 293-348 (`handleDocumentGenerate`)

**Issues**:
- Calls `/v1/content/blog_from_document` (old endpoint)
- Should use project API

**Action**: Update to use `/api/v1/projects/{project_id}/blog/generate`

### 5. Document-Centric Navigation

**Current Pattern**: All links use `/dashboard/documents/{document_id}`

**Needed**: Project-based routes:
- `/dashboard/projects/{project_id}` (project detail/blog editor)
- `/dashboard/projects/{project_id}/versions` (version history)
- `/dashboard/projects/{project_id}/versions/{version_id}` (version viewer)

**Note**: Documents still exist, but projects are the primary entity for blog editing.

---

## Design Requirements

### Codex/Cursor/Windsurf Inspiration

These tools share common patterns:
1. **Block-based editing**: Content organized into editable blocks
2. **Inline editing**: Click to edit, no separate edit page
3. **Version control**: Git-like history with diffs
4. **Optimistic updates**: UI updates immediately, syncs in background
5. **Conflict resolution**: Clear handling of concurrent edits
6. **Context-aware**: AI uses surrounding content for better edits

### UI Components Needed

#### 1. **Project Blog Editor Page**

**Route**: `/dashboard/projects/{project_id}`

**Layout**:
```
┌─────────────────────────────────────────────────────────┐
│ Header: Project Title | Status Badge | Export Button    │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────┐  ┌─────────────────────────┐ │
│  │                      │  │ Version History Sidebar │ │
│  │  Section 1 [Edit]   │  │ • v3 (current)          │ │
│  │  ──────────────────  │  │ • v2 (patch_section)   │ │
│  │  Content here...     │  │ • v1 (initial)          │ │
│  │                      │  │                         │ │
│  │  Section 2 [Edit]   │  │ [View Diff] [Revert]   │ │
│  │  ──────────────────  │  └─────────────────────────┘ │
│  │  Content here...     │                              │
│  │                      │                              │
│  └──────────────────────┘                              │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

**Features**:
- Left: Main content area with sections
- Right: Version history sidebar (collapsible)
- Each section is an editable block
- Click "Edit" → Inline editor opens
- Type instructions → AI processes → Section updates

#### 2. **Section Block Component**

**Visual Design**:
```
┌─────────────────────────────────────────────────────┐
│ Section 1: Introduction                    [Edit]  │
│ ─────────────────────────────────────────────────── │
│                                                     │
│ This is the section content. It can be long...    │
│                                                     │
│ Word count: 150                                     │
└─────────────────────────────────────────────────────┘
```

**States**:
- **Default**: Read-only display
- **Editing**: Inline editor with instruction input
- **Processing**: Loading spinner + disabled state
- **Error**: Error message + retry button
- **Conflict**: Warning banner + reload prompt

**Interaction**:
- Click "Edit" → Editor expands below section
- Editor has:
  - Textarea for instructions
  - "Apply Changes" button
  - "Cancel" button
- On submit → Optimistic update → API call → Update UI

#### 3. **Version History Sidebar**

**Visual Design**:
```
┌─────────────────────────────┐
│ Version History             │
│ ───────────────────────────│
│                             │
│ v3 (current)                │
│ • 2 min ago                 │
│ • patch_section             │
│ [View] [Diff]              │
│                             │
│ v2                          │
│ • 15 min ago                │
│ • patch_section             │
│ [View] [Diff] [Revert]     │
│                             │
│ v1                          │
│ • 1 hour ago                │
│ • initial                   │
│ [View] [Diff]              │
└─────────────────────────────┘
```

**Features**:
- List all versions (newest first)
- Show metadata (timestamp, source)
- Highlight current version
- Click version → View that version
- Click "Diff" → Show diff modal
- Click "Revert" → Confirm → Revert to that version

#### 4. **Diff Viewer Modal**

**Visual Design**:
```
┌─────────────────────────────────────────────┐
│ Diff: v1 → v2                    [Close]   │
├─────────────────────────────────────────────┤
│                                             │
│ Changed Sections:                           │
│ • Section 1 (sec-0)                         │
│ • Section 3 (sec-2)                         │
│                                             │
│ ┌─────────────────────────────────────────┐ │
│ │ Unified Diff:                           │ │
│ │ --- v1                                  │ │
│ │ +++ v2                                  │ │
│ │ @@ -1,3 +1,3 @@                          │ │
│ │ -Old text                                │ │
│ │ +New text                                │ │
│ └─────────────────────────────────────────┘ │
│                                             │
└─────────────────────────────────────────────┘
```

**Features**:
- Show changed section IDs
- Display unified diff
- Syntax highlighting (optional)
- Side-by-side comparison (optional enhancement)

#### 5. **Conflict Resolution Modal**

**Visual Design**:
```
┌─────────────────────────────────────────────┐
│ Conflict Detected                [Close]   │
├─────────────────────────────────────────────┤
│                                             │
│ This blog was updated by another process    │
│ while you were editing.                     │
│                                             │
│ Your changes:                               │
│ • Section 1 (sec-0)                         │
│                                             │
│ [Reload & Retry]  [Discard Changes]       │
│                                             │
└─────────────────────────────────────────────┘
```

**Behavior**:
- Auto-show on 409 response
- Offer reload + retry
- Or discard changes
- Show what sections were being edited

### Design Principles

1. **Progressive Enhancement**: Works without JavaScript (fallback to full page loads)
2. **Optimistic Updates**: UI updates immediately, syncs in background
3. **Clear Feedback**: Loading states, success messages, error handling
4. **Accessibility**: Keyboard navigation, ARIA labels, screen reader support
5. **Mobile Responsive**: Works on tablets/phones (sidebar collapses)
6. **Performance**: Lazy load versions, debounce API calls, cache responses

### Color & Typography

**Current System** (Tailwind-based):
- Uses existing design tokens (`text-content`, `text-contentMuted`, `border-border`, etc.)
- Maintain consistency with current UI

**New Elements**:
- Success: Green accent (`border-accent/40 bg-accent/5`)
- Error: Red destructive (`border-destructive/40 bg-destructive/5`)
- Warning: Yellow/orange for conflicts
- Loading: Subtle spinner animations

---

## Technical Implementation Guide

### Phase 1: Project-Based Routing

**Tasks**:
1. Create new route: `GET /dashboard/projects/{project_id}`
2. Create template: `templates/projects/detail.html`
3. Update navigation to include projects
4. Add project lookup helper (get project from document_id if needed)

**Files to Create**:
- `src/workers/templates/projects/detail.html`
- `src/workers/templates/projects/partials/blog_editor.html`
- `src/workers/templates/projects/partials/version_history.html`

**Files to Modify**:
- `src/workers/api/web.py` (add routes)

### Phase 2: Section Editing UI

**Tasks**:
1. Create section block component
2. Add inline editor (textarea + submit button)
3. Implement API call to `/api/v1/projects/{id}/blog/sections/patch`
4. Add loading/error states
5. Implement optimistic updates

**JavaScript Functions Needed**:
```javascript
// Load sections for a project
async function loadProjectSections(projectId) {
  const response = await fetch(`/api/v1/projects/${projectId}/blog/sections`);
  return await response.json();
}

// Patch a section
async function patchSection(projectId, sectionId, instructions) {
  const response = await fetch(
    `/api/v1/projects/${projectId}/blog/sections/patch`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section_id: sectionId, instructions })
    }
  );
  if (response.status === 409) {
    throw new Error('CONFLICT');
  }
  return await response.json();
}

// Handle section edit
async function handleSectionEdit(projectId, sectionId) {
  const instructions = prompt('Enter editing instructions:');
  if (!instructions) return;
  
  // Optimistic update
  updateSectionUI(sectionId, { loading: true });
  
  try {
    const result = await patchSection(projectId, sectionId, instructions);
    updateSectionUI(sectionId, { content: result.section.body_mdx });
  } catch (error) {
    if (error.message === 'CONFLICT') {
      showConflictModal();
    } else {
      showError(error.message);
    }
  }
}
```

### Phase 3: Version History UI

**Tasks**:
1. Create version history sidebar component
2. Load versions: `GET /api/v1/projects/{id}/blog/versions`
3. Display version list with metadata
4. Add "View Version" functionality
5. Add "Diff" functionality
6. Add "Revert" functionality

**JavaScript Functions Needed**:
```javascript
// Load versions
async function loadVersions(projectId) {
  const response = await fetch(`/api/v1/projects/${projectId}/blog/versions`);
  return await response.json();
}

// Load specific version
async function loadVersion(projectId, versionId) {
  const response = await fetch(
    `/api/v1/projects/${projectId}/blog/versions/${versionId}`
  );
  return await response.json();
}

// Get diff between versions
async function getDiff(projectId, fromVersionId, toVersionId) {
  const response = await fetch(
    `/api/v1/projects/${projectId}/blog/diff?from_version_id=${fromVersionId}&to_version_id=${toVersionId}`
  );
  return await response.json();
}

// Revert to version
async function revertToVersion(projectId, versionId) {
  const response = await fetch(
    `/api/v1/projects/${projectId}/blog/versions/${versionId}/revert`,
    { method: 'POST' }
  );
  return await response.json();
}
```

### Phase 4: Conflict Handling

**Tasks**:
1. Detect 409 responses
2. Show conflict modal
3. Implement reload + retry flow
4. Handle stale data gracefully

**Implementation**:
```javascript
function handleConflict(error, projectId, sectionId, instructions) {
  const modal = createConflictModal({
    message: 'This blog was updated while you were editing.',
    onRetry: () => {
      // Reload latest version, then retry
      loadProjectSections(projectId).then(() => {
        patchSection(projectId, sectionId, instructions);
      });
    },
    onDiscard: () => {
      // Just reload, don't retry
      loadProjectSections(projectId);
    }
  });
  showModal(modal);
}
```

### Phase 5: Legacy Cleanup

**Tasks**:
1. Remove legacy section regenerate route
2. Update document detail page to redirect to project page (if project exists)
3. Remove old JavaScript functions
4. Update templates to use project API

**Migration Strategy**:
- Keep document routes for backward compatibility
- Add redirects: `/dashboard/documents/{id}` → `/dashboard/projects/{project_id}` (if project exists)
- Deprecate old routes with warnings
- Remove after migration period

### State Management

**Current**: No state management (server-side rendering)

**Recommended Approach**:
- Keep server-side rendering for initial load
- Use JavaScript for interactive updates
- Store minimal state in DOM (data attributes)
- Poll for updates after edits (optional)

**Example**:
```html
<div data-section-id="sec-0" data-project-id="proj-123">
  <!-- Section content -->
</div>
```

### Error Handling

**Patterns**:
1. **Network Errors**: Show toast notification, allow retry
2. **409 Conflicts**: Show conflict modal with reload option
3. **Validation Errors**: Show inline error messages
4. **Server Errors (500)**: Show generic error, log details

**Implementation**:
```javascript
async function handleApiCall(apiCall) {
  try {
    return await apiCall();
  } catch (error) {
    if (error.response?.status === 409) {
      handleConflict(error);
    } else if (error.response?.status >= 500) {
      showError('Server error. Please try again later.');
    } else {
      showError(error.message || 'An error occurred.');
    }
    throw error;
  }
}
```

### Performance Considerations

1. **Lazy Loading**: Load versions on demand (when sidebar opens)
2. **Debouncing**: Debounce API calls if implementing auto-save
3. **Caching**: Cache version list, invalidate on updates
4. **Pagination**: Paginate versions if many exist (API supports limit=50)
5. **Optimistic Updates**: Update UI immediately, sync in background

---

## API Reference for Frontend

### Authentication

All API calls require authentication via JWT cookie. Include credentials:

```javascript
fetch('/api/v1/projects/{id}/blog/sections', {
  credentials: 'same-origin'
});
```

### Error Responses

**409 Conflict**:
```json
{
  "detail": "Blog has been updated since this version was loaded; please reload and retry"
}
```

**404 Not Found**:
```json
{
  "detail": "Project not found"
}
```

**422 Unprocessable Entity** (validation error):
```json
{
  "detail": [
    {
      "loc": ["body", "instructions"],
      "msg": "ensure this value has at least 1 characters",
      "type": "value_error.any_str.min_length"
    }
  ]
}
```

### Response Formats

All responses follow the Pydantic models defined in `src/workers/api/models.py`. See that file for exact schemas.

### Rate Limiting

Currently no rate limiting, but consider:
- Debouncing section patch requests
- Limiting version history polling
- Caching responses

---

## Migration Checklist

### For Developers

- [ ] Create project detail page route
- [ ] Create project blog editor template
- [ ] Implement section loading (GET /sections)
- [ ] Implement section patching (POST /sections/patch)
- [ ] Add loading/error states
- [ ] Implement conflict handling
- [ ] Create version history sidebar
- [ ] Implement version viewing
- [ ] Implement diff viewing
- [ ] Implement revert functionality
- [ ] Add optimistic updates
- [ ] Test conflict scenarios
- [ ] Remove legacy routes
- [ ] Update navigation
- [ ] Add project redirects from document pages

### For Designers

- [ ] Design section block component
- [ ] Design inline editor
- [ ] Design version history sidebar
- [ ] Design diff viewer modal
- [ ] Design conflict resolution modal
- [ ] Create loading state animations
- [ ] Design error states
- [ ] Ensure mobile responsiveness
- [ ] Create iconography for actions
- [ ] Design empty states (no versions, no sections)

---

## Conclusion

The backend API is production-ready and provides all the capabilities needed for a Codex-like editing experience. The frontend needs significant updates to leverage these capabilities, but the architecture is sound and the migration path is clear.

**Key Takeaways**:
1. **API is ready**: All endpoints exist and are well-designed
2. **UI needs work**: Current UI is document-centric and read-only
3. **Migration is straightforward**: Clear path from current to desired state
4. **Legacy code exists**: Old routes can be removed after migration

**Next Steps**:
1. Review this document with design/development team
2. Create detailed mockups based on design requirements
3. Implement Phase 1 (project routing)
4. Iterate through phases 2-5
5. Test thoroughly, especially conflict scenarios
6. Remove legacy code after migration period

---

## Appendix: Code Examples

### Example: Section Block Component (HTML)

```html
<div class="section-block" data-section-id="sec-0" data-project-id="proj-123">
  <div class="section-header">
    <h3>Section 1: Introduction</h3>
    <button onclick="editSection('sec-0')">Edit</button>
  </div>
  <div class="section-content">
    <p>This is the section content...</p>
    <span class="word-count">150 words</span>
  </div>
  <div class="section-editor hidden">
    <textarea placeholder="Enter editing instructions..."></textarea>
    <button onclick="applyEdit('sec-0')">Apply Changes</button>
    <button onclick="cancelEdit('sec-0')">Cancel</button>
  </div>
</div>
```

### Example: Version History Component (HTML)

```html
<aside class="version-history">
  <h3>Version History</h3>
  <ul>
    <li class="version-item current">
      <div class="version-header">
        <span class="version-id">v3</span>
        <span class="version-time">2 min ago</span>
      </div>
      <div class="version-meta">
        <span class="version-source">patch_section</span>
      </div>
      <div class="version-actions">
        <button onclick="viewVersion('v3')">View</button>
        <button onclick="viewDiff('v2', 'v3')">Diff</button>
      </div>
    </li>
    <!-- More versions... -->
  </ul>
</aside>
```

---

**Document Version**: 1.0  
**Last Updated**: 2025-01-15  
**Author**: AI Analysis  
**Status**: Ready for Design/Development Review

