import userEvent from '@testing-library/user-event';
import test from 'ava';
import React from 'react';

import { hasClass, hasStyle, renderInContainer } from 'test/utils';

import Icon, { IconSize } from './Icon';
import type { Props } from './Icon';

const setup = (props?: Props) => {
  const user = userEvent.setup();
  const view = renderInContainer(<Icon {...props} />);
  return { user, view };
};

test('should display a default icon', async (t) => {
  const { view } = setup();
  const firstChild = view.container.firstElementChild;
  await hasClass(t, firstChild, 'base', 'icon-star', 'medium');
  hasStyle(t, firstChild, { 'font-size': 'var(--icon-medium)' });
});

const sizeMacroTest = test.macro({
  exec: async (t, size: IconSize) => {
    const { view } = setup({ size });
    const firstChild = view.container.firstElementChild;
    await hasClass(t, firstChild, 'base', 'icon-star', size);
    hasStyle(t, firstChild, { 'font-size': `var(--icon-${size})` });
  },
  title: (_providedTitle, size: IconSize) => `should display a ${size}-size icon`,
});

test(sizeMacroTest, 'tiny');
test(sizeMacroTest, 'medium');
test(sizeMacroTest, 'large');
test(sizeMacroTest, 'big');
test(sizeMacroTest, 'great');
test(sizeMacroTest, 'huge');
test(sizeMacroTest, 'enormous');
test(sizeMacroTest, 'giant');
test(sizeMacroTest, 'jumbo');
test(sizeMacroTest, 'mega');

const nameMacroTest = test.macro({
  exec: async (t, name: string) => {
    const { view } = setup({ name });
    const firstChild = view.container.firstElementChild;
    await hasClass(t, firstChild, 'base', `icon-${name}`, 'medium');
  },
  title: (_providedTitle, name: string) => `should display a ${name} icon`,
});

test(nameMacroTest, 'star');
test(nameMacroTest, 'tasks');
test(nameMacroTest, 'tensor-board');
test(nameMacroTest, 'tensorflow');

// TODO: test `title`. cannot display title in test-library probably due to <ToolTip>
// screen.debug() doesnt show tooltip element somehow
