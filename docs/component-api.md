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

## Page Layout Macros

**Location:** `templates/components/layout/page.html`

These macros provide consistent, mobile-friendly scaffolding for dashboard pages.

### `page_container`

Wraps page content in standard padding and vertical spacing.

```jinja
{% from 'components/layout/page.html' import page_container %}

{% call page_container() %}
  <!-- Page content -->
{% endcall %}
```

### `page_header`

Renders the primary heading with optional description and actions. Actions can be any HTML snippet, such as button groups or dropdown triggers.

```jinja
{% set actions %}
  <a href="/dashboard/jobs" class="btn-secondary">All Jobs</a>
{% endset %}

{{ page_header('Documents', 'Manage registered sources', actions=actions) }}
```

### `page_section`

Wraps secondary content in a padded surface. Use `surface=False` to remove the default card styling.

```jinja
{% call page_section(title='Stats', description='Past 30 days') %}
  <p class="text-sm text-content">Content goes here.</p>
{% endcall %}
```

---

## Data List Pattern

Tables have been replaced with responsive cards so data reads well on mobile. Each list item is a `surface` card with stacked metadata.

```jinja
<div class="space-y-3">
  {% for item in items %}
    <article class="surface rounded-lg p-4 space-y-2">
      <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h3 class="text-sm font-semibold text-content">{{ item.title }}</h3>
        {{ badge(item.status_label, status=item.status) }}
      </div>
      <p class="text-xs text-contentMuted">Created {{ item.created_at }}</p>
    </article>
  {% endfor %}
</div>
```

Use `flex` utilities to align actions (e.g., “View” links) on larger screens while keeping a single-column flow on mobile.

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

All components consume the Material Design 3 token set defined in `assets/css/input.css` and surfaced through Tailwind utilities inside `tailwind.config.js`. The tokens are stored as CSS custom properties using RGB triplets so opacity modifiers (`/60`, `/20`, etc.) work everywhere.

### Color Roles

| Role | CSS Variable | Tailwind Utility | Notes |
| --- | --- | --- | --- |
| Primary | `--md-sys-color-primary` | `bg-primary`, `text-primary` | Baseline brand tone (light: #6750A4, dark: #D0BCFF). |
| On Primary | `--md-sys-color-on-primary` | `text-on-primary` | Text/icons that sit on top of primary surfaces. |
| Primary Container | `--md-sys-color-primary-container` | `bg-primary-container` | Used for filled tonal buttons/cards. |
| Secondary | `--md-sys-color-secondary` | `bg-secondary`, `text-secondary` | Supportive tone for chips, filters, etc. |
| Tertiary | `--md-sys-color-tertiary` | `bg-tertiary`, `text-tertiary` | Expressive tone (maps to former “accent”). |
| Error | `--md-sys-color-error` | `bg-error`, `text-error` | Destructive emphasis; includes `error-container` utilities. |
| Surface | `--md-sys-color-surface` | `bg-surface` | Primary background. |
| Surface Containers | `--md-sys-color-surface-container[-low|high|highest]` | `bg-surface-container*` | Density-aware layers for cards, sheets, nav. |
| Surface Variant | `--md-sys-color-surface-variant` | `bg-surface-variant`, `text-on-surface-variant` | Used for dividers, outlines, subdued containers. |
| Outline | `--md-sys-color-outline` | `border-outline` | Border + hairline accents. |
| Inverse Surface | `--md-sys-color-inverse-surface` | `bg-inverse-surface` | Snackbars, banners, dark-on-light flips. |

Legacy aliases (`bg`, `surfaceMuted`, `content`, etc.) still resolve to the new roles so existing templates remain stable while new work adopts the Material nomenclature. Switch to the explicit roles whenever you touch a component.

### Typography Scale

- Utility classes such as `text-display-large`, `text-headline-medium`, `text-body-small`, and `text-label-large` map directly to the Material type ramp.
- The classes are defined in `@layer utilities` inside `assets/css/input.css` and use the `--md-ref-typeface-brand` / `--md-ref-typeface-plain` tokens for consistent letter spacing, line height, and weight.
- Use `{% from 'macros/typography.html' import type_specimen %}` when building documentation or templates that must stay aligned with the token set.

### Spacing, Shape, and Elevation

- Spacing tokens follow a `dp-*` scale (`dp-1` = 4 dp = `0.25rem`, `dp-8` = 48 dp = `3rem`). You can mix them with Tailwind spacing utilities: `p-dp-4`, `gap-dp-2`, etc.
- Shape tokens expose the Material corner radius scale via Tailwind’s `rounded-*` classes (`rounded-sm` = 8 dp, `rounded-xl` = 28 dp). Semantic utilities such as `.surface` and `.badge` consume the same variables.
- Elevation tokens appear both as CSS variables (`--md-sys-elevation-level1` … `level5`) and Tailwind shadows (`shadow-elevation-3`). Surface helpers, buttons, and future components should use these levels instead of ad-hoc shadows.

### Motion Tokens

- Durations (`--md-sys-motion-duration-short1` … `long4`) and easing curves (`--md-sys-motion-easing-standard`, `--md-sys-motion-easing-emphasized`, etc.) are available as Tailwind transition utilities (`duration-motion-short-3`, `ease-motion-emphasized`).
- `static/js/motion.js` exports `window.materialMotion` which Alpine components can call to apply standardized enter/exit choreography.

### How Tailwind Consumes the Tokens

`tailwind.config.js` reads the custom properties via helper functions so every utility keeps opacity support:

```js
const colorVar = (token) => `rgb(var(--${token}) / <alpha-value>)`;

module.exports = {
  theme: {
    extend: {
      colors: {
        primary: colorVar('md-sys-color-primary'),
        'on-primary': colorVar('md-sys-color-on-primary'),
        surface: colorVar('md-sys-color-surface'),
        // ...see config for all roles
      },
    },
  },
};
```

Use these utilities (`bg-surface-container`, `text-on-surface-variant`, `shadow-elevation-2`, etc.) in templates instead of hard-coded values to stay on the Material track.

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

