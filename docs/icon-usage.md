# Icon Usage Guide

We use Heroicons with a custom macro system. Icons are defined in `templates/components/elements/icon.html`.

## How to Add Icons

1. Go to [heroicons.com](https://heroicons.com)
2. Find the icon you need (e.g., "bars-3" for menu)
3. Click on the icon to view the SVG code
4. Copy the `<path>` element content (or the entire path data)
5. Paste it into the corresponding section in `templates/components/elements/icon.html`

## Usage Examples

```jinja2
{% from 'components/elements/icon.html' import icon %}

{# Basic usage #}
{{ icon('menu') }}

{# With custom size #}
{{ icon('close', size='h-6 w-6') }}

{# With additional classes #}
{{ icon('email', class='text-primary') }}

{# In a button #}
<button>
  {{ icon('arrow-left', size='h-4 w-4', class='mr-2') }}
  Back
</button>
```

## Available Icons (add SVG paths as needed)

- `menu` - Hamburger menu (bars-3)
- `close` or `x` - Close/X mark (x-mark)
- `email` or `envelope` - Email icon (envelope)
- `arrow-left` or `back` or `chevron-left` - Back arrow (chevron-left)
- `arrow-right` - Right arrow (arrow-right)
- `external-link` - External link (arrow-top-right-on-square)
- `chevron-down` - Dropdown chevron (chevron-down)
- `spinner` - Loading spinner (arrow-path with animate-spin)

## Adding New Icons

1. Add a new `elif` condition in `icon.html`:
```jinja2
{% elif name == 'your-icon-name' %}
  <svg class="{{ icon_class }}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    {# Paste Heroicons SVG path here #}
  </svg>
```

2. Use it in templates:
```jinja2
{{ icon('your-icon-name') }}
```




