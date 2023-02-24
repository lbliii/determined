/* eslint-disable ava/no-ignored-test-files */
import { cleanup } from '@testing-library/react';
import Schema from 'async-validator';
import test from 'ava';
import { JSDOM } from 'jsdom';
import 'micro-observables/batchingForReactDom';
import { addHook } from 'pirates';
import td from 'testdouble';
import 'whatwg-fetch';

import { noOp } from 'shared/utils/service';

/**
 * To clean up the async-validator console warning that get generated during testing.
 * https://github.com/yiminghe/async-validator#how-to-avoid-global-warning
 */
Schema.warning = noOp;

const dom = new JSDOM('<!DOCTYPE html>', {
  pretendToBeVisual: true,
  runScripts: 'dangerously',
  url: 'http://localhost/',
});

// @ts-expect-error jsdom has missing globals
globalThis.global = globalThis.window = dom.window;
globalThis.document = dom.window.document;

// misc. globals
globalThis.Storage = dom.window.Storage;

require('shared/prototypes');

Object.defineProperty(window, 'matchMedia', {
  value: () => ({
    addEventListener: td.function(),
    addListener: td.function(), // deprecated
    dispatchEvent: td.function(),
    matches: false,
    onchange: null,
    removeEventListener: td.function(),
    removeListener: td.function(), // deprecated
  }),
});

global.ResizeObserver = require('resize-observer-polyfill');

test.after(cleanup);

addHook(
  () => {
    return 'module.exports = new Proxy({}, { get(_target, prop) { if (prop === "__esModule") return false; return prop } })';
  },
  { exts: ['.css', '.scss'] },
);

addHook(
  (_code, filename) => {
    return `module.exports = "${filename}"`;
  },
  { exts: ['.svg'] },
);
