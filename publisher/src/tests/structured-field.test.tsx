import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { it, expect } from 'vitest';
import { StructuredField } from '../StructuredField';
import { namedSchema } from '../form-schema';
import { Value } from '../metadata';
function Demo({ name, initial }: { name: string; initial: unknown }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <StructuredField label="Field" schema={namedSchema(name)} value={value} onChange={setValue} />
      <div data-testid="preview">
        <Value value={value} />
      </div>
    </>
  );
}
it('preserves a literal null default separately from no default', async () => {
  render(
    <Demo
      name="FieldSchema"
      initial={{ type: 'string', nullable: true, default: { value: null } }}
    />,
  );
  expect(screen.getByTestId('preview')).toHaveTextContent('"value": null');
  await userEvent.click(screen.getByRole('button', { name: 'Omit Field / default' }));
  expect(screen.getByTestId('preview')).not.toHaveTextContent('default');
});
it('changes a decimal field type without preserving hidden incompatible settings', async () => {
  render(
    <Demo
      name="FieldSchema"
      initial={{ type: 'decimal', precision: 38, scale: 2, default: { value: '1.00' } }}
    />,
  );
  await userEvent.selectOptions(screen.getByLabelText('Field / type'), 'boolean');
  expect(screen.getByTestId('preview')).toHaveTextContent('"type": "boolean"');
  expect(screen.getByTestId('preview')).not.toHaveTextContent('precision');
  expect(screen.getByTestId('preview')).not.toHaveTextContent('default');
});
it('keeps tolerance as a string without binary floating point conversion', async () => {
  render(
    <Demo
      name="Fixture"
      initial={{ id: 'check', version: '1', inputs: {}, expected_rows: [], tolerance: '0.00' }}
    />,
  );
  const input = screen.getByLabelText('Field / tolerance');
  await userEvent.clear(input);
  await userEvent.type(input, '0.0000000000000000001');
  expect(screen.getByTestId('preview')).toHaveTextContent('"tolerance": "0.0000000000000000001"');
});
