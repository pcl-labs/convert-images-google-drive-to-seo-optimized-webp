# Component API Reference

This document provides a complete reference for all Jinja component macros in the design system.

## Button Component

**Location:** `templates/components/elements/button.html`

**Macro:** `button`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `label` | string | required | Button text to display |
| `variant` | string | `"primary"` | Button style variant: `"primary"`, `"secondary"`, `"destructive"`, or `"ghost"` |
| `attrs` | string | `""` | Additional HTML attributes (e.g., `'type="submit"'`, `'@click="..."'`) |
| `with_spinner` | boolean | `False` | Show loading spinner indicator |
| `spinner_id` | string | `None` | Optional ID for the spinner element |
| `extra_classes` | string | `""` | Additional CSS classes to apply |

### Variants

- **primary**: Blue background (`--primary`), white text, used for primary actions
- **secondary**: Muted background, used for secondary actions
- **destructive**: Red background (`--destructive`), used for destructive actions
- **ghost**: Transparent background, used for subtle actions

### Examples

```jinja
{% from 'components/elements/button.html' import button %}

{# Basic primary button #}
{{ button('Submit', variant='primary', attrs='type="submit"') }}

{# Button with spinner #}
{{ button('Loading...', variant='primary', with_spinner=True) }}

{# Disabled button #}
{{ button('Disabled', variant='secondary', attrs='disabled') }}
```

---

## Card Component

**Location:** `templates/components/elements/card.html`

**Macro:** `card`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | string | `None` | Optional card header title |
| `actions` | string | `None` | Optional HTML for action buttons/links in header |

### Usage

Uses Jinja's `call` block syntax:

```jinja
{% from 'components/elements/card.html' import card %}

{% call card(title='My Card', actions='<a href="/link">Action</a>') %}
  <p>Card content goes here</p>
{% endcall %}
```

### Styling

- Uses `.surface` class for background and border
- Includes shadow and rounded corners
- Header section appears when `title` or `actions` are provided

---

## Input Component

**Location:** `templates/components/elements/input.html`

**Macro:** `input`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | string | required | Input `name` attribute (also used for `id`) |
| `label` | string | `None` | Optional label text |
| `type` | string | `"text"` | Input type: `"text"`, `"email"`, `"password"`, `"url"`, etc. |
| `value` | string | `""` | Input value |
| `placeholder` | string | `""` | Placeholder text |
| `error` | string | `None` | Error message to display below input |
| `attrs` | string | `""` | Additional HTML attributes (e.g., `'required'`, `'disabled'`) |

### Examples

```jinja
{% from 'components/elements/input.html' import input %}

{# Basic input with label #}
{{ input('email', label='Email Address', type='email', placeholder='user@example.com') }}

{# Input with error #}
{{ input('username', label='Username', error='Username is required', attrs='required') }}

{# Disabled input #}
{{ input('readonly', label='Read Only', attrs='disabled') }}
```

### Styling

- Uses `.field` class for consistent styling
- Includes focus states with ring color
- Error messages use `text-destructive` color
- Labels use `text-contentMuted` color

---

## Alert Component

**Location:** `templates/components/elements/alert.html`

**Macro:** `alert`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `kind` | string | `"info"` | Alert type: `"info"`, `"success"`, or `"error"` |
| `text` | string | `""` | Alert message (supports HTML via `` `|safe` `` filter) |
| `dismissible` | boolean | `True` | Show close button |

### Variants

- **info**: Blue/primary color scheme
- **success**: Green/accent color scheme
- **error**: Red/destructive color scheme

### Examples

```jinja
{% from 'components/elements/alert.html' import alert %}

{{ alert('info', 'This is an informational message.') }}
{{ alert('success', 'Operation completed successfully!') }}
{{ alert('error', 'An error occurred.') }}
{{ alert('info', 'Non-dismissible alert', dismissible=False) }}
```

---

## Badge Component

**Location:** `templates/components/elements/badge.html`

**Macro:** `badge`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | string | required | Badge text |
| `status` | string | `"default"` | Status variant: `"running"`, `"queued"`, `"completed"`, `"failed"`, `"disconnected"`, or `"default"` |

### Status Variants

- **running**: Green/accent color (active states)
- **queued**: Muted color (pending states)
- **completed**: Green/accent color (success states)
- **failed**: Red/destructive color (error states)
- **disconnected**: Yellow/warning color (warning states)
- **default**: Muted color (neutral states)

### Examples

```jinja
{% from 'components/elements/badge.html' import badge %}

{{ badge('Active', status='running') }}
{{ badge('Pending', status='queued') }}
{{ badge('Error', status='failed') }}
```

---

## Modal Component

**Location:** `templates/components/overlays/modal.html`

**Macro:** `modal`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | string | required | Unique modal ID (used for event dispatching) |
| `title` | string | `"Confirm"` | Modal header title |
| `confirm_text` | string | `"Confirm"` | Confirm button text |
| `cancel_text` | string | `"Cancel"` | Cancel button text |

### Usage

Uses Jinja's `call` block syntax:

```jinja
{% from 'components/overlays/modal.html' import modal %}

{% call modal('delete-item', title='Delete Item', confirm_text='Delete', cancel_text='Cancel') %}
  <p>Are you sure you want to delete this item?</p>
{% endcall %}
```

### Opening Modals

To open a modal, dispatch an event:

```html
<button @click="$dispatch('open-delete-item')">Delete</button>
```

The modal ID is used in the event name: `open-{id}`

### Confirming Actions

Listen for the confirm event:

```html
<div @confirm-delete-item.window="handleDelete()">
  <!-- Modal content -->
</div>
```

---

## Table Component

**Location:** `templates/components/data/table.html`

**Macro:** `table`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `headers` | list | `[]` | List of header column names |

### Usage

Uses Jinja's `call` block syntax:

```jinja
{% from 'components/data/table.html' import table %}

{% call table(headers=["Name", "Status", "Created"]) %}
  <tr>
    <td class="px-3 py-2 md:px-4 md:py-3 text-content">Item 1</td>
    <td class="px-3 py-2 md:px-4 md:py-3">{{ badge('Active', status='running') }}</td>
    <td class="px-3 py-2 md:px-4 md:py-3 text-contentMuted">2024-01-15</td>
  </tr>
{% endcall %}
```

### Styling

- Headers use `bg-surfaceMuted/60` background
- Rows use `divide-y divide-border` for separators
- Text colors use `text-content` and `text-contentMuted`

---

## Dropdown Component

**Location:** `templates/components/elements/dropdown.html`

**Macro:** `dropdown`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `trigger` | string | required | HTML for the dropdown trigger button |
| `align` | string | `"left"` | Alignment: `"left"` or `"right"` |
| `width` | string | `"md"` | Dropdown width: `"sm"`, `"md"`, `"lg"`, or `"xl"` |

### Usage

Uses Jinja's `call` block syntax with Alpine.js:

```jinja
{% from 'components/elements/dropdown.html' import dropdown %}

{% set trigger %}
  <button @click="toggle()" class="...">Menu</button>
{% endset %}

{% call dropdown(trigger, align='right', width='sm') %}
  <nav class="py-1 text-sm" role="menu">
    <a href="#" class="block px-3 py-2 text-content hover:bg-surfaceMuted/60" role="menuitem">Option 1</a>
    <a href="#" class="block px-3 py-2 text-content hover:bg-surfaceMuted/60" role="menuitem">Option 2</a>
  </nav>
{% endcall %}
```

### Alpine.js Integration

The dropdown uses Alpine.js for state management. The trigger should call `toggle()` and the dropdown panel uses `x-show="open"` and `@click.away="close()"`.

---

## Integration Card Component

**Location:** `templates/components/elements/integration_card.html`

**Macro:** `integration_card`

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `s` | dict | required | Service metadata dict with keys: `key`, `name`, `subtitle`, `description` |
| `data` | dict | required | Integration data dict with `connected` boolean |
| `csrf_token` | string | required | CSRF token for forms |

### Example

```jinja
{% from 'components/elements/integration_card.html' import integration_card %}

{{ integration_card(
  {'key': 'drive', 'name': 'Google Drive', 'subtitle': 'Connect Drive'},
  {'connected': True},
  csrf_token
) }}
```

---

## Design Tokens

All components use semantic design tokens defined in `assets/css/input.css`. These tokens can be updated in one place to change the entire theme.

### Token Format: RGB for Opacity Support

**Important:** All color tokens are defined in RGB format (space-separated values, e.g., `30 41 59`) rather than hex to support Tailwind's opacity modifiers. This allows using opacity utilities like `/50`, `/80`, etc. in Tailwind classes.

Example:
- ✅ `bg-surfaceMuted/50` - Works with RGB format (produces 50% opacity)
- ❌ Hex format would not support opacity modifiers

### Color Tokens

All colors are defined as RGB values in `assets/css/input.css`:

- `--bg`: Page background (2 6 23 - slate-950)
- `--surface`: Card/panel background (15 23 42 - slate-900)
- `--surface-muted`: Muted surface background (30 41 59 - slate-800)
- `--border`: Border color (31 41 55 - slate-800)
- `--content`: Primary text color (226 232 240 - slate-200)
- `--content-muted`: Muted text color (148 163 184 - slate-400)
- `--primary`: Primary brand color (2 132 199 - sky-600)
- `--primary-contrast`: Primary text on primary background (255 255 255)
- `--destructive`: Error/destructive color (225 29 72 - rose-600)
- `--destructive-contrast`: Text on destructive background (255 255 255)
- `--accent`: Success/accent color (34 197 94 - green-500)
- `--accent-contrast`: Text on accent background (5 46 22)
- `--warning`: Warning color (234 179 8 - yellow-500)
- `--warning-contrast`: Text on warning background (66 32 6)
- `--ring`: Focus ring color (56 189 248 - sky-400)

### Token Naming Convention

**Tailwind Utility Classes:**
- Always use the `bg-` prefix when using Tailwind utilities (e.g., `bg-surface-muted`, not `surface-muted`)
- Use kebab-case: `bg-surface-muted` (recommended)
- CamelCase also available: `bg-surfaceMuted` (for consistency with existing code)
- Both formats support opacity modifiers: `bg-surface-muted/50` or `bg-surfaceMuted/50`

**Semantic Utility Classes:**
- Direct utility classes like `.surface` and `.surface-muted` are available for use in templates
- These are defined in `@layer components` in `assets/css/input.css`

**Examples:**
```html
<!-- ✅ Correct: Using Tailwind utility with bg- prefix -->
<div class="bg-surface-muted rounded-xl p-4">Content</div>

<!-- ✅ Also correct: Using semantic utility class -->
<div class="surface-muted rounded-xl p-4">Content</div>

<!-- ✅ Correct: With opacity modifier -->
<div class="bg-surfaceMuted/50 hover:bg-surfaceMuted/80">Content</div>

<!-- ❌ Incorrect: Missing bg- prefix in Tailwind utility -->
<div class="surface-muted rounded-xl p-4">Content</div>
```

### Spacing Tokens

Standard Tailwind spacing scale (0.25rem increments)

### Radius Tokens

- `--radius-xs`: 0.25rem
- `--radius-sm`: 0.375rem
- `--radius-md`: 0.5rem
- `--radius-lg`: 0.75rem

### Shadow Tokens

- `--shadow-sm`: Small shadow (0 1px 2px 0 rgb(0 0 0 / 0.2))
- `--shadow-md`: Medium shadow (0 4px 6px -1px rgb(0 0 0 / 0.25))
- `--shadow-lg`: Large shadow (0 10px 15px -3px rgb(0 0 0 / 0.3))

### Typography Tokens

- `--font-sans`: System sans-serif font stack
- `--font-mono`: System monospace font stack

### Using Tokens in Tailwind Config

Tokens are mapped in `tailwind.config.js` to enable first-class Tailwind utility usage:

```js
colors: {
  bg: 'rgb(var(--bg))',
  surface: 'rgb(var(--surface))',
  'surface-muted': 'rgb(var(--surface-muted) / <alpha-value>)',
  surfaceMuted: 'rgb(var(--surface-muted) / <alpha-value>)',
  // ... etc
}
```

The `<alpha-value>` placeholder allows Tailwind to inject opacity values when using modifiers like `/50`.

---

## Best Practices

1. **Always use component macros** instead of raw HTML when possible
2. **Use semantic tokens** (`bg-surface`, `text-content`) instead of hard-coded colors
3. **Follow variant patterns** - use the predefined variants rather than custom classes
4. **Maintain consistency** - use the same components across the application
5. **Test accessibility** - ensure proper ARIA labels and keyboard navigation

---

## Visual Reference

See `/styleguide` for a live visual reference of all components and their variants.

