/** Airframe theme-v6 glyphs; all accompany text and stay out of accessible names. */
const names = {
  search: 'search',
  plus: 'plus',
  'arrow-up-right': 'arrow-right',
  check: 'check',
  exit: 'egress',
} as const;

export function Icon({
  name,
  className = '',
}: {
  name: keyof typeof names | 'port';
  className?: string;
}) {
  if (name === 'port') {
    return (
      <span className={`af-mark af-hue--spaceport ${className}`.trim()} aria-hidden="true">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <use href="/airframe/spaceport-glyph.svg#spaceport" />
        </svg>
      </span>
    );
  }
  return (
    <svg
      className={`af-ic icon ${className}`.trim()}
      viewBox="0 0 20 20"
      aria-hidden="true"
      focusable="false"
    >
      <use href={`/airframe/airframe-icons.svg#af-${names[name]}`} />
    </svg>
  );
}
