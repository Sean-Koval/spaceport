/** Small, local SVGs. All icons accompany text and stay out of accessible names. */
const paths = {
  port: 'M3 3h6v3H6v12h3v3H3V3Zm18 0h-6v3h3v12h-3v3h6V3ZM9 12h6m-3-3v6',
  search: 'm16 16 5 5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  plus: 'M12 5v14M5 12h14',
  'arrow-up-right': 'M6 18 18 6M6 6h12v12',
  check: 'm5 12 4 4L19 6',
  exit: 'M9 4H4v16h5m-1-8h13m-5-5 5 5-5 5',
} as const;

export function Icon({ name, className = '' }: { name: keyof typeof paths; className?: string }) {
  return (
    <svg
      className={`icon ${className}`.trim()}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={paths[name]} />
    </svg>
  );
}
