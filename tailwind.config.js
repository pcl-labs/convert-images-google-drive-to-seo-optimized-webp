/**** Tailwind CLI config ****/
/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './src/workers/templates/**/*.html',
  ],
  theme: {
    extend: {
      colors: {
        // Semantic tokens (for theming) - using RGB format for opacity support
        bg: 'rgb(var(--bg))',
        surface: 'rgb(var(--surface))',
        'surface-muted': 'rgb(var(--surface-muted) / <alpha-value>)',
        surfaceMuted: 'rgb(var(--surface-muted) / <alpha-value>)',
        border: 'rgb(var(--border))',
        content: 'rgb(var(--content))',
        contentMuted: 'rgb(var(--content-muted))',
        primary: 'rgb(var(--primary) / <alpha-value>)',
        primaryContrast: 'rgb(var(--primary-contrast))',
        destructive: 'rgb(var(--destructive) / <alpha-value>)',
        destructiveContrast: 'rgb(var(--destructive-contrast))',
        accent: 'rgb(var(--accent))',
        accentContrast: 'rgb(var(--accent-contrast))',
        warning: 'rgb(var(--warning))',
        warningContrast: 'rgb(var(--warning-contrast))',
        ring: 'rgb(var(--ring) / <alpha-value>)',
        // Standard Tailwind colors (still available for utility use)
        // These are the default Tailwind color palette
      },
      borderRadius: {
        xs: 'var(--radius-xs)',
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)'
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)'
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'monospace']
      }
    }
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography')
  ],
}
