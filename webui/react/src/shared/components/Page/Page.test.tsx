import { render } from '@testing-library/react';
import test from 'ava';
import React from 'react';

import { inDocument, waitFor } from 'test/utils';

import Page, { Props } from './Page';

const HEADER = 'header of page';
const CHILDREN = 'children of page';

const setup = (props: Props) => {
  const container = document.createElement('div');
  document.body.append(container);
  return render(<Page {...props} />, { container });
};

test('should display page with header component', (t) => {
  const { getByText } = setup({ headerComponent: <>{HEADER}</> });
  inDocument(t, getByText(HEADER));
});
test('should display spinner when loading', async (t) => {
  const { container } = setup({ loading: true });
  await waitFor(t, (tt) => {
    tt.is(container.getElementsByClassName('ant-spin ant-spin-spinning').length, 1);
  });
});
test('should display children', (t) => {
  const { getByText } = setup({ children: CHILDREN });
  inDocument(t, getByText(CHILDREN));
});

test('should use correct class name', (t) => {
  const { container } = setup({ bodyNoPadding: true, stickyHeader: true });
  t.is(container.getElementsByClassName('base bodyNoPadding stickyHeader').length, 1);
});
