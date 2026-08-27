/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        base: 'rgb(var(--base-c) / <alpha-value>)',
        surface: 'rgb(var(--surface-c) / <alpha-value>)',
        panel: 'rgb(var(--panel-c) / <alpha-value>)',
        raised: 'rgb(var(--raised-c) / <alpha-value>)',
        line: 'var(--line)',
        'line-strong': 'var(--line-strong)',
        ink: 'rgb(var(--ink-c) / <alpha-value>)',
        dim: 'rgb(var(--dim-c) / <alpha-value>)',
        faint: 'rgb(var(--faint-c) / <alpha-value>)',
        accent: 'rgb(var(--accent-c) / <alpha-value>)',
        'accent-dim': 'rgb(var(--accent-dim-c) / <alpha-value>)',
        critical: 'rgb(var(--critical-c) / <alpha-value>)',
        important: 'rgb(var(--important-c) / <alpha-value>)',
        interesting: 'rgb(var(--interesting-c) / <alpha-value>)',
        routine: 'rgb(var(--routine-c) / <alpha-value>)',
        ok: 'rgb(var(--ok-c) / <alpha-value>)',
        warn: 'rgb(var(--warn-c) / <alpha-value>)',
        bad: 'rgb(var(--bad-c) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'JetBrains Mono', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '0.9rem' }],
      },
      boxShadow: {
        panel: '0 1px 0 0 rgba(255,255,255,0.02) inset, 0 8px 30px -12px rgba(0,0,0,0.6)',
        glow: '0 0 24px -6px var(--accent-glow)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0', transform: 'translateY(4px)' }, to: { opacity: '1', transform: 'none' } },
        'pulse-soft': { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.45' } },
      },
      animation: {
        'fade-in': 'fade-in 0.22s ease-out',
        'pulse-soft': 'pulse-soft 2s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
