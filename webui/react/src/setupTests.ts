import Schema from 'async-validator';
import { JSDOM } from 'jsdom';
import 'micro-observables/batchingForReactDom';
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
