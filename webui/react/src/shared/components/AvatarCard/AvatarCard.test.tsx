import test from 'ava';
import React from 'react';

import { DarkLight } from 'shared/themes';
import { hasClass, hasStyle, inDocument, renderInContainer } from 'test/utils';

import AvatarCard, { Props } from './AvatarCard';

const setup = (props: Props) => renderInContainer(<AvatarCard {...props} />);

test('should display one-word name', (t) => {
  const { getByText } = setup({ darkLight: DarkLight.Light, displayName: 'Admin' });
  inDocument(t, getByText('A'));
  inDocument(t, getByText('Admin'));
});

test('should display two-words name', (t) => {
  const { getByText } = setup({ darkLight: DarkLight.Light, displayName: 'Dio Brando' });
  inDocument(t, getByText('DB'));
  inDocument(t, getByText('Dio Brando'));
});

test('should display three-words name', (t) => {
  const { getByText } = setup({
    darkLight: DarkLight.Light,
    displayName: 'Gold Experience Requiem',
  });
  inDocument(t, getByText('GR'));
  inDocument(t, getByText('Gold Experience Requiem'));
});

test('should be light mode color', (t) => {
  const { container } = setup({ darkLight: DarkLight.Light, displayName: 'Admin' });
  hasStyle(t, container.querySelector('#avatar'), { 'background-color': 'hsl(290, 63%, 60%)' });
});

test('should be dark mode color', (t) => {
  const { container } = setup({ darkLight: DarkLight.Dark, displayName: 'Admin' });
  hasStyle(t, container.querySelector('#avatar'), {
    'background-color': 'hsl(290, 63%, 38%)',
  });
});

test('should not have a base class name', async (t) => {
  const { container } = setup({ darkLight: DarkLight.Light, displayName: 'test' });
  await hasClass(t, container.firstElementChild, 'base');
});

test('should have a class name', async (t) => {
  const { container } = setup({
    className: 'test-class',
    darkLight: DarkLight.Light,
    displayName: 'test',
  });
  const firstChild = container.firstElementChild;
  await hasClass(t, firstChild, 'base');
  await hasClass(t, firstChild, 'test-class');
});
